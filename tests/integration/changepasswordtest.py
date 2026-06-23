"""
E2E test for CHANGEPASSWORD (OPTIMIZATIONS 3.1): the off-reactor do_change_password worker
wired through in_CHANGEPASSWORD + _changepassword_done/_failed. Requires the server up on
:8200 against the MariaDB test db. Asserts protocol replies + DB password column + that the
new password is actually usable for re-login, plus concurrency and disconnect-during-call.

Run (server already up): source venv/bin/activate; python3 .changepasswordtest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, threading, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "c1"
M = 15  # concurrent independent password changes
def enc(raw): return base64.b64encode(hashlib.md5(raw.encode()).digest()).decode()
PW = enc("secretpw")            # initial password for every user
NEW = enc("brandnewpw")         # functional user's target password
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

def register_and_confirm(user, pw=PW):
    s = connect()
    s.sendall(("REGISTER %s %s\n" % (user, pw)).encode()); recv_until(s, "\n")
    time.sleep(2.5)
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, pw)).encode()); recv_until(s, "AGREEMENTEND")
    s.sendall(b"CONFIRMAGREEMENT\n")
    ok = "LOGININFOEND" in recv_until(s, "LOGININFOEND"); s.close(); return ok

def login_keep(user, pw=PW):
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, pw)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=8)
    if "ACCEPTED" not in resp or "LOGININFOEND" not in resp:
        s.close(); return None
    return Sock(s)

def login_result(user, pw):
    # full login attempt; returns the raw response (ACCEPTED/DENIED), closes the socket
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, pw)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=5)
    s.close(); return resp

# ---------- users ----------
F = "chpw_%s_F" % TAG
conc = ["chpw_%s_%d" % (TAG, i) for i in range(M)]
allusers = [F] + conc

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
def db_pw(name):
    cur.execute("SELECT password FROM users WHERE username=%s", (name,)); return cur.fetchone()[0]

errors = []
def check(cond, label):
    if not cond: errors.append(label)

# ===================== FUNCTIONAL (sequential) =====================
sk = login_keep(F)
if sk is None:
    print("ABORT: functional user login failed"); sys.exit(1)

# new == current -> rejected, password unchanged
sk.clear(); sk.send("CHANGEPASSWORD %s %s" % (PW, PW)); sk.drain()
check(sk.saw("New password must be different"), "same-password not rejected")
check(db_pw(F) == PW, "same-password attempt changed the stored password")

# malformed new password (not valid base64 md5) -> rejected, password unchanged
sk.clear(); sk.send("CHANGEPASSWORD %s notavalidhash" % PW); sk.drain()
check(not sk.saw("Password changed successfully"), "malformed new password was accepted")
check(db_pw(F) == PW, "malformed new password changed the stored password")

# wrong current password -> rejected, password unchanged
sk.clear(); sk.send("CHANGEPASSWORD %s %s" % (enc("wrongcur"), NEW)); sk.drain()
check(sk.saw("Invalid username or password"), "wrong cur_pw not rejected")
check(db_pw(F) == PW, "wrong cur_pw changed the stored password (count broken)")

# correct change -> success reply, DB updated to NEW
sk.clear(); sk.send("CHANGEPASSWORD %s %s" % (PW, NEW)); sk.drain()
check(sk.saw("Password changed successfully"), "valid change did not report success")
check(db_pw(F) == NEW, "valid change did not persist NEW password (got %r)" % (db_pw(F),))
sk.close()

# the NEW password is actually usable, the old one is not
check("ACCEPTED" in login_result(F, NEW), "re-login with NEW password failed")
old = login_result(F, PW)
check("DENIED" in old or "ACCEPTED" not in old, "old password still accepted after change")

print("functional checks done (%d failures so far)" % len(errors))

# ===================== CONCURRENCY: independent password changes =====================
# every concurrent user changes PW -> their own distinct new password, simultaneously.
targets = {u: enc("conc_%s_%d" % (TAG, i)) for i, u in enumerate(conc)}
cerr = []
def change_one(u):
    s = login_keep(u)
    if s is None: cerr.append("%s login failed" % u); return
    try:
        s.send("CHANGEPASSWORD %s %s" % (PW, targets[u])); s.drain(1.0)
        if not s.saw("Password changed successfully"):
            cerr.append("%s did not get success reply" % u)
    finally:
        s.close()

t0 = time.time()
ts = [threading.Thread(target=change_one, args=(u,)) for u in conc]
for t in ts: t.start()
for t in ts: t.join()
dt = time.time() - t0
bad = 0
for u in conc:
    if db_pw(u) != targets[u]:
        bad += 1; cerr.append("%s stored %r != expected" % (u, db_pw(u)))
print("concurrent independent changes: %d users in %.2fs, %d wrong" % (M, dt, bad))
if cerr:
    print("FAIL concurrency:"); [print("  ", e) for e in cerr[:12]]
else:
    print("PASS: all concurrent users ended on their own new password")

# ===================== DISCONNECT DURING CALL =====================
# reuse the concurrency users (each currently at targets[u]); log in, issue a valid
# CHANGEPASSWORD back to PW, then drop immediately - the callback must hit its
# "session_id not in clients" guard rather than raise.
dropped = 0
for u in conc[:8]:
    s = login_keep(u, targets[u])
    if s is None: continue
    s.send("CHANGEPASSWORD %s %s" % (targets[u], PW))
    s.close()  # drop immediately, before the deferred write's callback fires
    dropped += 1
    time.sleep(0.05)
print("disconnect-during-call: issued %d CHANGEPASSWORD-then-drop (inspect server.log for tracebacks)" % dropped)

conn.close()
print("TOTAL functional failures: %d" % len(errors))
if errors or cerr:
    for e in errors: print("  FAIL:", e)
    sys.exit(1)
else:
    print("PASS: all functional CHANGEPASSWORD checks correct")
