"""
Worker-unit tests for the Phase 3.1 CLOSE-OUT slice (OPTIMIZATIONS 3.1): the off-reactor
workers do_confirm_agreement, do_scrub_account, do_set_password, do_find_ip, and the reactor
helper generate_password. Runs in-process against the MariaDB test db (no server needed).
Mirrors _run_db (call fn -> commit_guard -> close_guard) and asserts via fresh-session re-reads.

Covers the gated-active() handlers' WRITE path (RESETPASSWORD/RESETUSERPASSWORD) that the e2e
cannot reach (active() is False in the test config), plus the D3 email-collision invariant.

Run: source venv/bin/activate; python3 .closeoutworkertest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_URL
import sys, hashlib, base64
import sqlalchemy
import SQLUsers

URL = DB_URL
A = "phase3_close_a"
B = "phase3_close_b"
C = "phase3_close_c"
GONE = "phase3_close_gone"
MAIL_A = "phase3_close_a@example.test"
MAIL_B = "phase3_close_b@example.test"
MAIL_NEW = "phase3_close_new@example.test"
FINDIP = "9.9.9.9"

def enc(raw): return base64.b64encode(hashlib.md5(raw.encode()).digest()).decode()

class FakeProtocol:
    # generate_password asserts via self._root.protocol._validPasswordSyntax; the worker test
    # independently re-derives the hash, so a permissive stub is enough to let it run.
    def _validPasswordSyntax(self, password): return True, ""

class FakeRoot:
    def __init__(self, engine):
        self.session_manager = SQLUsers.session_manager(self, engine)
        self.protocol = FakeProtocol()

engine = sqlalchemy.create_engine(URL, pool_size=5, pool_recycle=3600)
root = FakeRoot(engine)
sm = root.session_manager
udb = SQLUsers.UsersHandler(root)

errors = []
def check(cond, label):
    if not cond: errors.append(label)

def wipe(*names):
    s = sm.sess()
    for n in names:
        s.query(SQLUsers.User).filter(SQLUsers.User.username == n).delete()
    s.commit(); sm.close_guard()

def make_user(name, email, access="user", last_ip="1.2.3.4"):
    s = sm.sess()
    s.add(SQLUsers.User(name, "pw_encoded", last_ip, email, access))
    s.commit(); sm.close_guard()

def run_worker(fn, *args):
    try:
        res = fn(*args)
        sm.commit_guard()
        return res
    finally:
        sm.close_guard()

def row(name):
    s = sm.sess()
    r = s.query(SQLUsers.User).filter(SQLUsers.User.username == name).first()
    out = None if r is None else (r.id, r.access, r.email, r.ingame_time, r.bot, r.password)
    sm.close_guard(); return out

wipe(A, B, C, GONE)

# ============================== do_confirm_agreement ==============================
# flips access agreement->user, writes ONLY access, returns uid; vanished -> None
make_user(A, MAIL_A, access="agreement")
s = sm.sess(); r = s.query(SQLUsers.User).filter(SQLUsers.User.username == A).first()
r.ingame_time = 777; s.commit(); sm.close_guard()
ret = run_worker(udb.do_confirm_agreement, A)
check(isinstance(ret, int) and ret > 0, "do_confirm_agreement should return a real uid, got %r" % (ret,))
r = row(A)
check(r[1] == "user", "access must become 'user', got %r" % (r[1],))
check(r[3] == 777, "ingame_time must NOT be clobbered by the access-only write, got %r" % (r[3],))
check(r[2] == MAIL_A, "email must NOT be clobbered, got %r" % (r[2],))
check(run_worker(udb.do_confirm_agreement, GONE) is None, "vanished user must return None")
wipe(A)

# ============================== do_scrub_account (D1-D4) ==============================
# zeroes ingame_time/bot, forces access 'user', sets email=None (D3), writes the hashed pw;
# returns ('ok', uid); vanished -> ('gone',)
make_user(A, MAIL_A, access="admin")
s = sm.sess(); r = s.query(SQLUsers.User).filter(SQLUsers.User.username == A).first()
uid_a = r.id; r.ingame_time = 555; r.bot = 1; s.commit(); sm.close_guard()
H = enc("scrubpw")
ret = run_worker(udb.do_scrub_account, uid_a, H)
check(ret == ('ok', uid_a), "do_scrub_account should return ('ok', uid), got %r" % (ret,))
r = row(A)
check(r[1] == "user", "scrub must force access 'user', got %r" % (r[1],))
check(r[2] is None, "scrub must NULL the email (D3), got %r" % (r[2],))
check(r[3] == 0, "scrub must zero ingame_time, got %r" % (r[3],))
check(r[4] == 0, "scrub must zero bot, got %r" % (r[4],))
check(r[5] == H, "scrub must write the supplied hashed password, got %r" % (r[5],))
check(run_worker(udb.do_scrub_account, 0, H) == ('gone',), "scrub of a missing id must return ('gone',)")

# D3: scrub a SECOND account -> both end with email=None, no 1062 collision on ''
make_user(B, MAIL_B, access="user")
uid_b = row(B)[0]
ret_b = run_worker(udb.do_scrub_account, uid_b, enc("scrubpw2"))
check(ret_b == ('ok', uid_b), "second scrub should also succeed (no 1062), got %r" % (ret_b,))
check(row(A)[2] is None and row(B)[2] is None, "both scrubbed accounts must hold NULL email (D3)")
wipe(A, B)

# ============================== do_set_password ==============================
# writes the hashed pw, returns ('ok', username, email); add-email path; email-taken -> denied
make_user(A, MAIL_A, access="user")
uid_a = row(A)[0]
H2 = enc("resetpw")
ret = run_worker(udb.do_set_password, uid_a, H2)
check(ret == ('ok', A, MAIL_A), "do_set_password should return ('ok', username, email), got %r" % (ret,))
check(row(A)[5] == H2, "do_set_password must persist the hashed pw, got %r" % (row(A)[5],))

# add-email path: RESETUSERPASSWORD adding an address to an account that had none
make_user(C, None, access="user")
uid_c = row(C)[0]
ret = run_worker(udb.do_set_password, uid_c, enc("p3"), MAIL_NEW)
check(ret == ('ok', C, MAIL_NEW), "add-email path should return the new email, got %r" % (ret,))
check(row(C)[2] == MAIL_NEW, "add-email path must persist the email, got %r" % (row(C)[2],))

# email-taken: C tries to take A's email -> ('denied', ...) via 1062, C's email unchanged
make_user(B, MAIL_B, access="user")  # B holds MAIL_B
ret = run_worker(udb.do_set_password, uid_c, enc("p4"), MAIL_B)
check(ret[0] == 'denied', "taking another user's email must be denied via 1062, got %r" % (ret,))
check(row(C)[2] == MAIL_NEW, "denied add-email must leave C's email unchanged, got %r" % (row(C)[2],))
# session still usable after the 1062 rollback
ret = run_worker(udb.do_set_password, uid_a, enc("p5"))
check(ret[0] == 'ok', "session must remain usable after a 1062 rollback, got %r" % (ret,))
check(run_worker(udb.do_set_password, 0, enc("p6"))[0] == 'denied', "missing id must be denied")
wipe(A, B, C)

# ============================== do_find_ip ==============================
make_user(A, MAIL_A, last_ip=FINDIP)
make_user(B, MAIL_B, last_ip=FINDIP)
make_user(C, None, last_ip="8.8.8.8")
res = run_worker(udb.do_find_ip, FINDIP)
names = sorted(t[0] for t in res)
check(A in names and B in names and C not in names, "do_find_ip must return only the matching ips, got %r" % (names,))
check(all(isinstance(t, tuple) and len(t) == 2 for t in res), "do_find_ip rows must be plain 2-tuples, got %r" % (res,))
check(all(t[1] is None or isinstance(t[1], str) for t in res), "last_login must be a plain isoformat string or None, got %r" % (res,))
wipe(A, B, C)

# ============================== generate_password ==============================
raw1, h1 = udb.generate_password()
raw2, h2 = udb.generate_password()
check(len(raw1) == 10 and h1 == enc(raw1), "generate_password hash must be base64(md5(raw)), got raw=%r h=%r" % (raw1, h1))
check(raw1 != raw2, "generate_password must mint a fresh value each call (got identical)")

# ============================== DISCRIMINATION CHECK ==============================
# break the do_scrub_account D3 invariant expectation and confirm the harness catches it.
disc = []
def dcheck(cond, label):
    if not cond: disc.append(label)
make_user(A, MAIL_A, access="agreement")
uid_a = row(A)[0]
run_worker(udb.do_scrub_account, uid_a, enc("d"))
dcheck(row(A)[2] == MAIL_A, "DISCRIMINATION: expected email UNCHANGED (wrong on purpose)")
wipe(A)
if not disc:
    errors.append("DISCRIMINATION FAILED: a deliberately-wrong assertion was not caught")
else:
    print("discrimination check OK (deliberately-wrong assertion caught: scrub DOES null the email)")

if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: do_confirm_agreement / do_scrub_account / do_set_password / do_find_ip / generate_password")
