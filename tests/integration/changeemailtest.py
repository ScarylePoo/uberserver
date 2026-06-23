"""
E2E test for CHANGEEMAIL (OPTIMIZATIONS 3.1): the off-reactor do_change_email worker wired
through in_CHANGEEMAIL + _changeemail_done/_failed. Requires the server up on :8200 against
the MariaDB test db. Asserts protocol replies + DB email column, plus the concurrent
change-to-taken-email race (worker 1062 path) and a disconnect-during-call probe.

CHANGEEMAIL does NOT disconnect on success, so the CHANGEEMAILACCEPTED reply IS reliable
(unlike RENAMEACCOUNT, whose success precedes an immediate RST).

NOTE on verification: the test config has no mail_user, so verificationdb.active() is False
and verify() returns (True, '') for a blank code. This e2e therefore exercises the
active()-OFF branch + the real users-row write path; it does NOT exercise the verify-ON
(code-required) path.

Run (server already up): source venv/bin/activate; python3 .changeemailtest.py [tag]
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, time, threading, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "e1"
M = 12  # concurrent independent email changes
import hashlib, base64
def enc(raw): return base64.b64encode(hashlib.md5(raw.encode()).digest()).decode()
PW = enc("secretpw")
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
    def drain(self, seconds=0.8):
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
    # NOTE: with verification inactive the server stores email=NULL at REGISTER regardless
    # of any email arg, so emails under test are established via CHANGEEMAIL / DB below.
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
F = "cmail_%s_F" % TAG
T = "cmail_%s_T" % TAG  # holds a known "taken" email for the functional pre-check denial
conc = ["cmail_%s_%d" % (TAG, i) for i in range(M)]
X = "cmail_%s_X" % TAG
Y = "cmail_%s_Y" % TAG
allusers = [F, T] + conc + [X, Y]

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
def db_email(name):
    cur.execute("SELECT email FROM users WHERE username=%s", (name,)); return cur.fetchone()[0]

errors = []
def check(cond, label):
    if not cond: errors.append(label)

# ===================== FUNCTIONAL (sequential) =====================
sk = login_keep(F)
if sk is None:
    print("ABORT: functional user login failed"); sys.exit(1)

# establish a known "taken" email on user T (via the real CHANGEEMAIL path)
TAKEN = ("cmail_%s_taken@phase3.test" % TAG).lower()
st = login_keep(T)
if st is None:
    print("ABORT: helper user T login failed"); sys.exit(1)
st.send("CHANGEEMAIL %s" % TAKEN); st.drain()
check(st.saw("CHANGEEMAILACCEPTED %s" % TAKEN), "helper T failed to set its email")
check(db_email(T) == TAKEN, "helper T email not persisted (got %r)" % (db_email(T),))
st.close()

# valid change -> ACCEPTED reply + SERVERMSG, DB updated (server lowercases the email)
NEWF = ("cmail_%s_F_new@phase3.test" % TAG).lower()
sk.clear(); sk.send("CHANGEEMAIL %s" % NEWF); sk.drain()
check(sk.saw("CHANGEEMAILACCEPTED %s" % NEWF), "valid change did not report CHANGEEMAILACCEPTED (buf=%r)" % sk.buf)
check(sk.saw("Your email address has been changed to %s" % NEWF), "valid change missing SERVERMSG confirmation")
check(db_email(F) == NEWF, "valid change did not persist NEW email (got %r)" % (db_email(F),))

# change to an email taken by ANOTHER user (T) -> DENIED at the reactor pre-check, DB unchanged
sk.clear(); sk.send("CHANGEEMAIL %s" % TAKEN); sk.drain()
check(sk.saw("CHANGEEMAILDENIED"), "change to a taken email was not denied")
check(db_email(F) == NEWF, "denied change must leave the stored email unchanged (got %r)" % (db_email(F),))
sk.close()
print("functional checks done (%d failures so far)" % len(errors))

# ===================== CONCURRENCY: independent email changes =====================
targets = {u: ("cmail_%s_%d_new@phase3.test" % (TAG, i)).lower() for i, u in enumerate(conc)}
cerr = []
def change_one(u):
    s = login_keep(u)
    if s is None: cerr.append("%s login failed" % u); return
    try:
        s.send("CHANGEEMAIL %s" % targets[u]); s.drain(1.0)
        if not s.saw("CHANGEEMAILACCEPTED"):
            cerr.append("%s did not get ACCEPTED reply" % u)
    finally:
        s.close()

t0 = time.time()
ts = [threading.Thread(target=change_one, args=(u,)) for u in conc]
for t in ts: t.start()
for t in ts: t.join()
dt = time.time() - t0
bad = 0
for u in conc:
    if db_email(u) != targets[u]:
        bad += 1; cerr.append("%s stored %r != expected" % (u, db_email(u)))
print("concurrent independent changes: %d users in %.2fs, %d wrong" % (M, dt, bad))
if cerr:
    print("FAIL concurrency:"); [print("  ", e) for e in cerr[:12]]
else:
    print("PASS: all concurrent users ended on their own new email")

# ===================== CHANGE-TO-TAKEN-EMAIL RACE (worker 1062 path) =====================
# X and Y both change to the SAME new email simultaneously. Both may pass the reactor
# pre-check (email free at check time), both defer do_change_email; exactly one flush wins
# (commits), the other hits UNIQUE(email)=1062 and is denied. Whichever path each loser
# takes (reactor pre-check OR worker 1062), the invariant is: exactly one winner.
SHARED = ("cmail_%s_shared@phase3.test" % TAG).lower()
race_replies = {}
def race_change(u):
    s = login_keep(u)
    if s is None: race_replies[u] = "LOGINFAIL"; return
    try:
        s.send("CHANGEEMAIL %s" % SHARED); s.drain(1.5)
        race_replies[u] = "ACCEPTED" if s.saw("CHANGEEMAILACCEPTED") else ("DENIED" if s.saw("CHANGEEMAILDENIED") else "NONE")
    finally:
        s.close()
ts = [threading.Thread(target=race_change, args=(u,)) for u in (X, Y)]
for t in ts: t.start()
for t in ts: t.join()
winners = [u for u in (X, Y) if db_email(u) == SHARED]
accepted = [u for u in (X, Y) if race_replies.get(u) == "ACCEPTED"]
check(len(winners) == 1, "exactly one user must own the shared email, got winners=%r replies=%r" % (winners, race_replies))
check(len(accepted) == 1, "exactly one user must get ACCEPTED, got %r (replies=%r)" % (accepted, race_replies))
check(accepted == winners, "the ACCEPTED user must be the DB winner, accepted=%r winners=%r" % (accepted, winners))
print("taken-email race: winners=%r replies=%r" % (winners, race_replies))

# ===================== DISCONNECT DURING CALL =====================
# log in, issue a valid CHANGEEMAIL, drop immediately - the callback must hit its
# "session_id not in clients" guard rather than raise, and the do_change_email /
# do_end_session writes race the same users row (absorbed by _run_db's 1020 retry).
# Inspect server.log for DB-error / 1020 lines (see the positive-control runs).
dropped = 0
for i, u in enumerate(conc[:8]):
    s = login_keep(u)
    if s is None: continue
    s.send("CHANGEEMAIL cmail_%s_drop_%d@phase3.test" % (TAG, i))
    s.close()  # drop immediately, before the deferred write's callback fires
    dropped += 1
    time.sleep(0.03)
print("disconnect-during-call: issued %d CHANGEEMAIL-then-drop (inspect server.log for 1020/DB-error lines)" % dropped)

conn.close()
print("TOTAL functional failures: %d" % len(errors))
if errors or cerr:
    for e in errors: print("  FAIL:", e)
    sys.exit(1)
else:
    print("PASS: all CHANGEEMAIL e2e checks correct")
