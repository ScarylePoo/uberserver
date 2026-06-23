import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, threading, sys, json
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "c1"
M = 20          # concurrent viewers issuing GETCHANNELMESSAGES
ROUNDS = 5      # repeat the concurrent burst to widen the threaded-read window
NMSG = 6        # seeded history messages
raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()
CHAN = "histchan_%s" % TAG

DB = DB_KWARGS

# ---------- socket helpers ----------
def recv_until(s, substr, timeout=8):
    s.settimeout(timeout)
    buf = b""
    try:
        while substr.encode() not in buf:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    return buf.decode(errors="replace")

class Sock:
    def __init__(self, s):
        self.s = s
        self.buf = ""
    def send(self, line):
        self.s.sendall((line + "\n").encode())
    def drain(self, seconds=0.5):
        # read whatever is available for `seconds`, append to buffer
        self.s.settimeout(seconds)
        try:
            while True:
                chunk = self.s.recv(8192)
                if not chunk:
                    break
                self.buf += chunk.decode(errors="replace")
        except socket.timeout:
            pass
    def collect_said(self, chan, want, timeout=4.0):
        # GETCHANNELMESSAGES has no end marker; collect `want` SAID lines for `chan`
        # or give up after `timeout`. Returns list of (id, userName, msg, ex_msg) in
        # arrival order.
        deadline = time.time() + timeout
        got = []
        while len(got) < want and time.time() < deadline:
            self.s.settimeout(max(0.05, deadline - time.time()))
            try:
                chunk = self.s.recv(8192)
                if not chunk:
                    break
                self.buf += chunk.decode(errors="replace")
            except socket.timeout:
                break
            lines = self.buf.split("\n")
            self.buf = lines.pop()  # keep partial last line
            for ln in lines:
                ln = ln.strip()
                if not ln.startswith("JSON "):
                    continue
                try:
                    obj = json.loads(ln[5:])
                except Exception:
                    continue
                said = obj.get("SAID")
                if not said or said.get("chanName") != chan:
                    continue
                got.append((int(said["id"]), said["userName"], said["msg"], said["ex_msg"]))
        return got
    def close(self):
        try: self.s.close()
        except: pass

def connect():
    s = socket.create_connection((HOST, PORT))
    recv_until(s, "\n")  # greeting
    return s

def register_and_confirm(user):
    s = connect()
    s.sendall(("REGISTER %s %s\n" % (user, PW)).encode())
    recv_until(s, "\n")
    time.sleep(2.5)
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    recv_until(s, "AGREEMENTEND")
    s.sendall(b"CONFIRMAGREEMENT\n")
    ok = "LOGININFOEND" in recv_until(s, "LOGININFOEND")
    s.close()
    return ok

def login_keep(user):
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=8)
    if "ACCEPTED" not in resp or "LOGININFOEND" not in resp:
        s.close()
        return None
    return Sock(s)

# ---------- users ----------
viewers = ["chm_%s_v%d" % (TAG, i) for i in range(M)]
authors = ["chm_%s_a0" % TAG, "chm_%s_a1" % TAG]
allusers = viewers + authors

results = {}
def _setup(u): results[u] = register_and_confirm(u)
ts = [threading.Thread(target=_setup, args=(u,)) for u in allusers]
for t in ts: t.start()
for t in ts: t.join()
ok_setup = sum(1 for v in results.values() if v)
print("setup confirmed: %d/%d" % (ok_setup, len(allusers)))
if ok_setup != len(allusers):
    print("ABORT: not all users registered"); sys.exit(1)

# ---------- seed channel history directly in the DB ----------
conn = pymysql.connect(**DB)
cur = conn.cursor()
cur.execute("SELECT id FROM channels WHERE name=%s", (CHAN,))
row = cur.fetchone()
if not row:
    print("ABORT: channel %s not seeded (seed it before boot)" % CHAN); sys.exit(1)
chan_id = row[0]

fmt = ",".join(["%s"] * len(authors))
cur.execute("SELECT id, username FROM users WHERE username IN (%s)" % fmt, authors)
amap = {name: uid for (uid, name) in cur.fetchall()}
A = [amap[authors[0]], amap[authors[1]]]

# idempotent re-runs: wipe this channel's history, reseed NMSG rows
cur.execute("DELETE FROM channel_history WHERE channel_id=%s", (chan_id,))
now = time.strftime("%Y-%m-%d %H:%M:%S")
expected = []   # (userName, msg, ex_msg) in insertion order == id order
for i in range(NMSG):
    uid = A[i % 2]
    uname = authors[i % 2]
    msg = "m%d" % i
    ex = i % 2
    cur.execute(
        "INSERT INTO channel_history (channel_id, user_id, bridged_id, time, msg, ex_msg) VALUES (%s,%s,%s,%s,%s,%s)",
        (chan_id, uid, None, now, msg, ex))
    expected.append((uname, msg, bool(ex)))
conn.close()
print("seeded %d history rows in channel %s (id=%d)" % (NMSG, CHAN, chan_id))

# ---------- concurrent reads ----------
errors = []
def check_viewer(v):
    sk = login_keep(v)
    if sk is None:
        errors.append("%s: login failed" % v); return
    try:
        sk.send("JOIN %s" % CHAN)
        sk.drain(0.6)  # let JOIN confirmation flush
        for r in range(ROUNDS):
            sk.send("GETCHANNELMESSAGES %s 0" % CHAN)
            got = sk.collect_said(CHAN, NMSG)
            if len(got) != NMSG:
                errors.append("%s round%d: got %d SAID, want %d" % (v, r, len(got), NMSG)); continue
            ids = [g[0] for g in got]
            if ids != sorted(ids):
                errors.append("%s round%d: ids not ascending: %s" % (v, r, ids))
            payload = [(g[1], g[2], g[3]) for g in got]
            if payload != expected:
                errors.append("%s round%d: payload=%s exp=%s" % (v, r, payload, expected))
    finally:
        sk.close()

t0 = time.time()
ts = [threading.Thread(target=check_viewer, args=(v,)) for v in viewers]
for t in ts: t.start()
for t in ts: t.join()
dt = time.time() - t0
print("concurrent GETCHANNELMESSAGES: %d viewers x %d rounds in %.2fs" % (M, ROUNDS, dt))
if errors:
    print("FAIL: %d errors" % len(errors))
    for e in errors[:15]:
        print("  ", e)
else:
    print("PASS: all GETCHANNELMESSAGES replies correct + ordered under concurrency")

# ---------- error-path check: bad last_msg_id -> FAILED ----------
sk = login_keep(viewers[0])
if sk is not None:
    sk.send("JOIN %s" % CHAN)
    sk.drain(0.4)
    sk.send("GETCHANNELMESSAGES %s notanumber" % CHAN)
    sk.drain(0.6)
    if "FAILED" in sk.buf and "Invalid id" in sk.buf:
        print("PASS: invalid last_msg_id -> FAILED Invalid id")
    else:
        print("WARN: invalid last_msg_id did not yield expected FAILED (buf tail: %r)" % sk.buf[-200:])
    sk.close()

# ---------- disconnect-during-call check ----------
dropped = 0
for i in range(10):
    sk = login_keep(viewers[i % M])
    if sk is None: continue
    sk.send("JOIN %s" % CHAN)
    sk.send("GETCHANNELMESSAGES %s 0" % CHAN)
    sk.close()  # drop immediately, often before the reactor callback runs
    dropped += 1
print("disconnect-during-call: issued %d drop-after-send (inspect server.log for tracebacks)" % dropped)

sys.exit(1 if errors else 0)
