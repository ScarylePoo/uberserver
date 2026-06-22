import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, threading, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "s1"
M = 20          # concurrent viewers issuing list commands
ROUNDS = 5      # repeat the concurrent burst to widen the race window
raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()

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
    # buffered line reader so framed replies parse cleanly across commands
    def __init__(self, s):
        self.s = s
        self.buf = ""
    def send(self, line):
        self.s.sendall((line + "\n").encode())
    def read_block(self, end_tag, timeout=8):
        self.s.settimeout(timeout)
        try:
            while ("\n" + end_tag) not in ("\n" + self.buf) and not self.buf.startswith(end_tag) and end_tag not in self.buf:
                chunk = self.s.recv(8192)
                if not chunk:
                    break
                self.buf += chunk.decode(errors="replace")
        except socket.timeout:
            pass
        # split off everything up to and including the end_tag line
        lines = self.buf.split("\n")
        out, rest, done = [], [], False
        for ln in lines:
            if done:
                rest.append(ln)
            else:
                out.append(ln)
                if ln.strip() == end_tag:
                    done = True
        self.buf = "\n".join(rest)
        return out
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
viewers = ["soc_%s_v%d" % (TAG, i) for i in range(M)]
targets = ["soc_%s_t%d" % (TAG, i) for i in range(6)]   # T0..T5 shared targets
allusers = viewers + targets

results = {}
def _setup(u): results[u] = register_and_confirm(u)
ts = [threading.Thread(target=_setup, args=(u,)) for u in allusers]
for t in ts: t.start()
for t in ts: t.join()
ok_setup = sum(1 for v in results.values() if v)
print("setup confirmed: %d/%d" % (ok_setup, len(allusers)))
if ok_setup != len(allusers):
    print("ABORT: not all users registered"); sys.exit(1)

# ---------- seed relationships directly in the DB ----------
conn = pymysql.connect(**DB)
cur = conn.cursor()
fmt = ",".join(["%s"] * len(allusers))
cur.execute("SELECT id, username FROM users WHERE username IN (%s)" % fmt, allusers)
idmap = {name: uid for (uid, name) in cur.fetchall()}
assert len(idmap) == len(allusers), "missing ids: %s" % (set(allusers) - set(idmap))
T = [idmap[t] for t in targets]

# clean prior rows for these viewers (idempotent re-runs)
vids = [idmap[v] for v in viewers]
vfmt = ",".join(["%s"] * len(vids))
cur.execute("DELETE FROM ignores WHERE user_id IN (%s)" % vfmt, vids)
cur.execute("DELETE FROM friends WHERE first_user_id IN (%s)" % vfmt, vids)
cur.execute("DELETE FROM friendRequests WHERE friend_user_id IN (%s)" % vfmt, vids)

now = time.strftime("%Y-%m-%d %H:%M:%S")
for v in viewers:
    vid = idmap[v]
    # ignores: T0 with reason, T1 without
    cur.execute("INSERT INTO ignores (user_id, ignored_user_id, reason, time) VALUES (%s,%s,%s,%s)", (vid, T[0], "rsn_%s" % v, now))
    cur.execute("INSERT INTO ignores (user_id, ignored_user_id, reason, time) VALUES (%s,%s,%s,%s)", (vid, T[1], None, now))
    # friends: viewer is first_user_id for T2, second_user_id for T3 (union both directions)
    cur.execute("INSERT INTO friends (first_user_id, second_user_id, time) VALUES (%s,%s,%s)", (vid, T[2], now))
    cur.execute("INSERT INTO friends (first_user_id, second_user_id, time) VALUES (%s,%s,%s)", (T[3], vid, now))
    # friend requests TO viewer: from T4 with msg, from T5 without
    cur.execute("INSERT INTO friendRequests (user_id, friend_user_id, msg, time) VALUES (%s,%s,%s,%s)", (T[4], vid, "msg_%s" % v, now))
    cur.execute("INSERT INTO friendRequests (user_id, friend_user_id, msg, time) VALUES (%s,%s,%s,%s)", (T[5], vid, None, now))
conn.close()
print("seeded relationships for %d viewers" % len(viewers))

exp_ignore = {targets[0], targets[1]}
exp_friend = {targets[2], targets[3]}
exp_freq   = {targets[4], targets[5]}

# ---------- concurrent list reads ----------
def parse_names(lines, row_prefix):
    names = set()
    begin = end = 0
    for ln in lines:
        ln = ln.strip()
        if ln.endswith("BEGIN"): begin += 1
        if ln.endswith("LISTEND"): end += 1
        if ln.startswith(row_prefix + " "):
            # row form: "<PREFIX> userName=NAME\t..." possibly with tabs
            rest = ln[len(row_prefix)+1:]
            for tok in rest.split("\t"):
                if tok.strip().startswith("userName="):
                    names.add(tok.split("userName=",1)[1].strip())
    return names, begin, end

errors = []
def check_viewer(v):
    sk = login_keep(v)
    if sk is None:
        errors.append("%s: login failed" % v); return
    try:
        for _ in range(ROUNDS):
            sk.send("IGNORELIST")
            lines = sk.read_block("IGNORELISTEND")
            names, b, e = parse_names(lines, "IGNORELIST")
            if b != 1 or e != 1: errors.append("%s IGNORELIST framing begin=%d end=%d" % (v, b, e))
            if names != exp_ignore: errors.append("%s IGNORELIST names=%s exp=%s" % (v, names, exp_ignore))

            sk.send("FRIENDLIST")
            lines = sk.read_block("FRIENDLISTEND")
            names, b, e = parse_names(lines, "FRIENDLIST")
            if b != 1 or e != 1: errors.append("%s FRIENDLIST framing begin=%d end=%d" % (v, b, e))
            if names != exp_friend: errors.append("%s FRIENDLIST names=%s exp=%s" % (v, names, exp_friend))

            sk.send("FRIENDREQUESTLIST")
            lines = sk.read_block("FRIENDREQUESTLISTEND")
            names, b, e = parse_names(lines, "FRIENDREQUESTLIST")
            if b != 1 or e != 1: errors.append("%s FRIENDREQUESTLIST framing begin=%d end=%d" % (v, b, e))
            if names != exp_freq: errors.append("%s FRIENDREQUESTLIST names=%s exp=%s" % (v, names, exp_freq))
    finally:
        sk.close()

t0 = time.time()
ts = [threading.Thread(target=check_viewer, args=(v,)) for v in viewers]
for t in ts: t.start()
for t in ts: t.join()
dt = time.time() - t0

print("concurrent list reads: %d viewers x %d rounds in %.2fs" % (M, ROUNDS, dt))
if errors:
    print("FAIL: %d errors" % len(errors))
    for e in errors[:15]:
        print("  ", e)
    sys.exit(1)
else:
    print("PASS: all list replies well-formed and correct under concurrency")

# ---------- disconnect-during-call check ----------
# fire a list command then immediately drop the socket; the reactor callback should
# hit the `client.session_id not in clients` guard without raising. We can only assert
# the server stays up; check server.log for tracebacks afterward.
dropped = 0
for i in range(10):
    sk = login_keep("soc_%s_v%d" % (TAG, i % M))
    if sk is None: continue
    sk.send("FRIENDLIST")
    sk.close()  # drop immediately, often before the callback runs
    dropped += 1
print("disconnect-during-call: issued %d drop-after-send (inspect server.log for tracebacks)" % dropped)
