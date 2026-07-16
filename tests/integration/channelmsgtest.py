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
    def collect_said_raw(self, chan, want, timeout=8.0):
        # GETCHANNELMESSAGES has no end marker; collect `want` SAID frames for `chan` or
        # give up after `timeout`. Returns (said_dicts, other_frames) where other_frames
        # holds any non-SAID JSON frames seen along the way (e.g. the truncation notice).
        deadline = time.time() + timeout
        got = []
        others = []
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
                if said is None:
                    others.append(obj)
                    continue
                if said.get("chanName") != chan:
                    continue
                got.append(said)
        return got, others

    def collect_said(self, chan, want, timeout=4.0):
        # arrival-order (id, userName, msg, ex_msg) tuples
        said, _ = self.collect_said_raw(chan, want, timeout)
        return [(int(m["id"]), m["userName"], m["msg"], m["ex_msg"]) for m in said]
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

def login_keep(user, compat=None):
    # compat flags ride in the login sentence: "<agent>\t<last_id>\t<flags>"
    s = connect()
    if compat:
        s.sendall(("LOGIN %s %s 0 * TestClient\t0\t%s\n" % (user, PW, compat)).encode())
    else:
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

# ---------- read cap: newest-N, truncation signal, timestamp dialects ----------
# Seeded AFTER the checks above so their exact-count assertions are unaffected.
CAP = 200
EXTRA = 205  # push the channel past the cap
conn = pymysql.connect(**DB)
cur = conn.cursor()
seed_time = "2030-01-01 00:00:00"  # distinctive + stable: lets us pin the exact timestamp
rows = [(chan_id, A[0], None, seed_time, "cap%d" % i, 0) for i in range(EXTRA)]
cur.executemany(
    "INSERT INTO channel_history (channel_id, user_id, bridged_id, time, msg, ex_msg) VALUES (%s,%s,%s,%s,%s,%s)",
    rows)
conn.close()
print("seeded %d extra rows to exceed the %d cap" % (EXTRA, CAP))

# expected microsecond value for seed_time, computed the same way the server does
# (naive datetime -> local epoch), so this pins the dialect without hardcoding a TZ.
import datetime as _dt
_seed_dt = _dt.datetime(2030, 1, 1, 0, 0, 0)
EXPECT_US = int(_seed_dt.timestamp()) * 1000000
EXPECT_SEC = str(int(time.mktime(_seed_dt.timetuple())))

def check_cap(user, compat, label):
    sk = login_keep(user, compat)
    if sk is None:
        errors.append("%s: login failed" % label); return None
    try:
        sk.send("JOIN %s" % CHAN)
        sk.drain(0.6)
        sk.send("GETCHANNELMESSAGES %s 0" % CHAN)
        said, others = sk.collect_said_raw(CHAN, CAP)
        if len(said) != CAP:
            errors.append("%s: got %d SAID, want exactly %d (cap)" % (label, len(said), CAP))
            return None
        # newest-N, not oldest-N: the last seeded row must be present, the first must not
        msgs = [m["msg"] for m in said]
        if msgs[-1] != "cap%d" % (EXTRA - 1):
            errors.append("%s: newest message is %r, want %r" % (label, msgs[-1], "cap%d" % (EXTRA - 1)))
        if "m0" in msgs:
            errors.append("%s: oldest message m0 present - returned oldest-N not newest-N" % label)
        ids = [int(m["id"]) for m in said]
        if ids != sorted(ids):
            errors.append("%s: ids not ascending" % label)
        return said, others
    finally:
        sk.close()

def report(label, before):
    # print PASS only if this section actually added no errors, else print what broke
    new = errors[before:]
    if new:
        print("FAIL: %s" % label)
        for e in new:
            print("  ", e)
    else:
        print("PASS: %s" % label)

# legacy client: string-of-seconds, and no truncation frame (it has no field for one)
n = len(errors)
r = check_cap(viewers[1], None, "legacy")
if r:
    said, others = r
    t = said[-1]["time"]
    if not (isinstance(t, str) and t == EXPECT_SEC):
        errors.append("legacy: time=%r (%s), want str %r" % (t, type(t).__name__, EXPECT_SEC))
    if any("CHANNELMESSAGESTRUNCATED" in o for o in others):
        errors.append("legacy: got a truncation frame it cannot parse")
report("legacy dialect -> string seconds, no truncation frame", n)

# jsonchat client: integer microseconds + an explicit truncation notice
n = len(errors)
r = check_cap(viewers[2], "jsonchat", "jsonchat")
if r:
    said, others = r
    t = said[-1]["time"]
    if not (isinstance(t, int) and t == EXPECT_US):
        errors.append("jsonchat: time=%r (%s), want int %r" % (t, type(t).__name__, EXPECT_US))
    trunc = [o["CHANNELMESSAGESTRUNCATED"] for o in others if "CHANNELMESSAGESTRUNCATED" in o]
    if not trunc:
        errors.append("jsonchat: no truncation frame despite %d rows > %d cap" % (EXTRA, CAP))
    elif trunc[0].get("oldestId") != int(said[0]["id"]):
        errors.append("jsonchat: truncation oldestId=%r, want %r" % (trunc[0].get("oldestId"), said[0]["id"]))
report("jsonchat dialect -> int microseconds + truncation notice", n)

# a cursor near the head must NOT report truncation (the cap only bites on a cold start)
n = len(errors)
sk = login_keep(viewers[3], "jsonchat")
if sk is not None:
    sk.send("JOIN %s" % CHAN)
    sk.drain(0.6)
    conn = pymysql.connect(**DB); cur = conn.cursor()
    cur.execute("SELECT MAX(id) FROM channel_history WHERE channel_id=%s", (chan_id,))
    max_id = cur.fetchone()[0]
    conn.close()
    sk.send("GETCHANNELMESSAGES %s %d" % (CHAN, max_id - 3))
    said, others = sk.collect_said_raw(CHAN, 3, timeout=3.0)
    if len(said) != 3:
        errors.append("resume: got %d SAID, want 3" % len(said))
    elif any("CHANNELMESSAGESTRUNCATED" in o for o in others):
        errors.append("resume: truncation reported for a 3-message catch-up under the cap")
    sk.close()
report("short resume returns 3 messages, no truncation", n)

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
