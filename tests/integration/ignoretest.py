import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, threading, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "i1"
M = 20          # independent concurrent ignore pairs
raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()
DB = DB_KWARGS

def recv_until(s, substr, timeout=8):
    s.settimeout(timeout); buf = b""
    try:
        while substr.encode() not in buf:
            chunk = s.recv(8192)
            if not chunk: break
            buf += chunk
    except socket.timeout: pass
    return buf.decode(errors="replace")

class Sock:
    def __init__(self, s): self.s = s; self.buf = ""
    def send(self, line): self.s.sendall((line + "\n").encode())
    def drain(self, seconds=0.6):
        self.s.settimeout(seconds)
        try:
            while True:
                chunk = self.s.recv(8192)
                if not chunk: break
                self.buf += chunk.decode(errors="replace")
        except socket.timeout: pass
    def saw(self, substr): return substr in self.buf
    def clear(self): self.buf = ""
    def close(self):
        try: self.s.close()
        except: pass

def connect():
    s = socket.create_connection((HOST, PORT)); recv_until(s, "\n"); return s

def register_and_confirm(user):
    s = connect()
    s.sendall(("REGISTER %s %s\n" % (user, PW)).encode()); recv_until(s, "\n")
    time.sleep(2.5)
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode()); recv_until(s, "AGREEMENTEND")
    s.sendall(b"CONFIRMAGREEMENT\n")
    ok = "LOGININFOEND" in recv_until(s, "LOGININFOEND"); s.close(); return ok

def login_keep(user):
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=8)
    if "ACCEPTED" not in resp or "LOGININFOEND" not in resp:
        s.close(); return None
    return Sock(s)

# ---------- users ----------
iss = ["ign_%s_i%d" % (TAG, i) for i in range(M)]   # concurrency issuers
tgt = ["ign_%s_t%d" % (TAG, i) for i in range(M)]   # concurrency targets
funcs = ["fn_%s_I" % TAG, "fn_%s_T" % TAG, "fn_%s_M" % TAG]  # issuer, normal target, mod target
race = ["rc_%s_R" % TAG, "rc_%s_Q" % TAG]           # same-pair double-ignore
allusers = iss + tgt + funcs + race

results = {}
def _setup(u): results[u] = register_and_confirm(u)
ts = [threading.Thread(target=_setup, args=(u,)) for u in allusers]
for t in ts: t.start()
for t in ts: t.join()
ok_setup = sum(1 for v in results.values() if v)
print("setup confirmed: %d/%d" % (ok_setup, len(allusers)))
if ok_setup != len(allusers):
    print("ABORT: not all users registered"); sys.exit(1)

conn = pymysql.connect(**DB); cur = conn.cursor()
fmt = ",".join(["%s"] * len(allusers))
cur.execute("SELECT id, username FROM users WHERE username IN (%s)" % fmt, allusers)
idmap = {name: uid for (uid, name) in cur.fetchall()}

def ig_count(a, b):  # ignore rows a -> b
    cur.execute("SELECT COUNT(*) FROM ignores WHERE user_id=%s AND ignored_user_id=%s", (a, b)); return cur.fetchone()[0]
def wipe(a, b):
    cur.execute("DELETE FROM ignores WHERE (user_id=%s AND ignored_user_id=%s) OR (user_id=%s AND ignored_user_id=%s)", (a, b, b, a))

errors = []
def check(cond, label):
    if not cond: errors.append(label)

# ===================== FUNCTIONAL (sequential) =====================
I, T, MOD = funcs
iid, tid, mid = idmap[I], idmap[T], idmap[MOD]
# make MOD a moderator (offline target: exercises offline resolve + access check)
cur.execute("UPDATE users SET access='mod' WHERE id=%s", (mid,))
wipe(iid, tid); wipe(iid, mid)
skI, skT = login_keep(I), login_keep(T)

# missing userName (tags present, no userName)
skI.clear(); skI.send("IGNORE x=1"); skI.drain()
check(skI.saw("Missing userName argument"), "missing-userName not rejected")

# no such user
skI.clear(); skI.send("IGNORE userName=nobody_%s_xyz" % TAG); skI.drain()
check(skI.saw("No such user"), "nonexistent-target not rejected")

# moderator (offline)
skI.clear(); skI.send("IGNORE userName=%s" % MOD); skI.drain()
check(skI.saw("Can't ignore a moderator"), "mod-target not rejected")
check(ig_count(iid, mid) == 0, "mod ignore row wrongly created")

# self
skI.clear(); skI.send("IGNORE userName=%s" % I); skI.drain()
check(skI.saw("Can't ignore self"), "self-ignore not rejected")

# ignore T with reason -> reply, DB row, memory updated
skI.clear(); skI.send("IGNORE userName=%s\treason=spammer" % T); skI.drain()
check(skI.saw("IGNORE userName=%s" % T) and skI.saw("reason=spammer"), "IGNORE reply (with reason) missing")
check(ig_count(iid, tid) == 1, "ignore row I->T not stored (count=%d)" % ig_count(iid, tid))

# MEMORY PATH: T pm's I -> I must NOT receive it (I.ignored holds T in memory)
skI.clear(); skT.clear()
skT.send("SAYPRIVATE %s blocked_probe_1" % I); skT.drain(); skI.drain()
check(not skI.saw("SAIDPRIVATE %s blocked_probe_1" % T), "ignored sender's PM leaked through (memory not updated)")

# already ignored -> rejected, no extra row
skI.clear(); skI.send("IGNORE userName=%s" % T); skI.drain()
check(skI.saw("User is already ignored"), "already-ignored not rejected")
check(ig_count(iid, tid) == 1, "already-ignored created extra row (count=%d)" % ig_count(iid, tid))

# unignore T -> reply, DB row gone, memory cleared
skI.clear(); skI.send("UNIGNORE userName=%s" % T); skI.drain()
check(skI.saw("UNIGNORE userName=%s" % T), "UNIGNORE reply missing")
check(ig_count(iid, tid) == 0, "ignore row not removed after UNIGNORE (count=%d)" % ig_count(iid, tid))

# MEMORY PATH: T pm's I -> now I MUST receive it (I.ignored no longer holds T)
skI.clear(); skT.clear()
skT.send("SAYPRIVATE %s allowed_probe_2" % I); skT.drain(); skI.drain()
check(skI.saw("SAIDPRIVATE %s allowed_probe_2" % T), "PM blocked after UNIGNORE (memory not cleared)")

# unignore when not ignored
skI.clear(); skI.send("UNIGNORE userName=%s" % T); skI.drain()
check(skI.saw("User is not ignored"), "unignore-not-ignored not rejected")

# unignore nonexistent user
skI.clear(); skI.send("UNIGNORE userName=nobody_%s_xyz" % TAG); skI.drain()
check(skI.saw("No such user"), "unignore-nonexistent not rejected")

for sk in (skI, skT): sk.close()
print("functional checks done (%d failures so far)" % len(errors))

# ===================== CONCURRENCY: independent ignore pairs =====================
for i in range(M):
    wipe(idmap[iss[i]], idmap[tgt[i]])
cerr = []
def ignore_pair(i):
    sk = login_keep(iss[i])
    if sk is None: cerr.append("%s login failed" % iss[i]); return
    try:
        sk.send("IGNORE userName=%s" % tgt[i]); sk.drain(1.0)
        if not sk.saw("IGNORE userName=%s" % tgt[i]):
            cerr.append("%s did not get IGNORE reply" % iss[i])
    finally:
        sk.close()

t0 = time.time()
ts = [threading.Thread(target=ignore_pair, args=(i,)) for i in range(M)]
for t in ts: t.start()
for t in ts: t.join()
dt = time.time() - t0
bad = 0
for i in range(M):
    if ig_count(idmap[iss[i]], idmap[tgt[i]]) != 1:
        bad += 1
        cerr.append("pair %d: ignores=%d" % (i, ig_count(idmap[iss[i]], idmap[tgt[i]])))
print("concurrent independent ignores: %d pairs in %.2fs, %d bad" % (M, dt, bad))
if cerr:
    print("FAIL concurrency:"); [print("  ", e) for e in cerr[:12]]
else:
    print("PASS: all independent pairs ignored exactly once")

# ===================== SAME-PAIR DOUBLE-IGNORE RACE =====================
R, Q = race
rid, qid = idmap[R], idmap[Q]
wipe(rid, qid)
# R ignores Q from two connections at once (no unique constraint -> may dup)
def dbl():
    sk = login_keep(R)
    if sk is None: return
    sk.send("IGNORE userName=%s" % Q); sk.drain(1.0); sk.close()
ts = [threading.Thread(target=dbl) for _ in range(2)]
for t in ts: t.start()
for t in ts: t.join()
dup = ig_count(rid, qid)
print("same-pair double-ignore: resulting ignores rows = %d (1 ideal; >1 is the documented no-unique-constraint race)" % dup)
# UNIGNORE must clear ALL rows even if duplicated (tolerant .delete(), no .one())
skR = login_keep(R)
skR.send("UNIGNORE userName=%s" % Q); skR.drain(1.0); skR.close()
heal = ig_count(rid, qid)
print("tolerant-delete heal after UNIGNORE: ignores rows = %d (must be 0)" % heal)
check(heal == 0, "tolerant UNIGNORE failed to clear duplicate rows (count=%d)" % heal)

# ===================== DISCONNECT DURING CALL =====================
dropped = 0
for i in range(8):
    wipe(idmap[iss[i % M]], idmap[tgt[i % M]])
    sk = login_keep(iss[i % M])
    if sk is None: continue
    sk.send("IGNORE userName=%s" % tgt[i % M])
    sk.close()  # drop immediately
    dropped += 1
print("disconnect-during-call: issued %d IGNORE-then-drop (inspect server.log for tracebacks)" % dropped)

conn.close()
print("TOTAL functional failures: %d" % len(errors))
if errors or cerr:
    for e in errors: print("  FAIL:", e)
    sys.exit(1)
else:
    print("PASS: all functional ignore checks correct")
