"""
Worker-unit test for LOGIN (OPTIMIZATIONS 3.1 follow-up): do_login, the off-reactor
"write the login record" unit, extended to also return the user's ignore list so the
reactor callback (_SendLoginInfo) no longer issues a synchronous get_ignored_user_ids
query on the reactor thread. Runs in-process against the MariaDB test db (no server
needed). Mirrors _run_db (call fn -> commit_guard -> close_guard).

RED expectation before implementation: do_login returns a bare OfflineClient (not a
tuple), so the (snapshot, ignored) contract checks fail.

Run: source venv/bin/activate; python3 .loginworkertest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_URL
import sys
import sqlalchemy
import SQLUsers

URL = DB_URL
A = "phase3_login_a"   # the user logging in
B = "phase3_login_b"   # ignored by A
C = "phase3_login_c"   # ignored by A
PW = "pw_encoded"

class FakeRoot:
    def __init__(self, engine):
        self.session_manager = SQLUsers.session_manager(self, engine)

engine = sqlalchemy.create_engine(URL, pool_size=5, pool_recycle=3600)
root = FakeRoot(engine)
sm = root.session_manager
udb = SQLUsers.UsersHandler(root)

errors = []
def check(cond, label):
    if not cond: errors.append(label)

def uid(name):
    s = sm.sess()
    row = s.query(SQLUsers.User).filter(SQLUsers.User.username == name).first()
    i = row.id if row else None
    sm.close_guard(); return i

def wipe(*names):
    s = sm.sess()
    for n in names:
        row = s.query(SQLUsers.User).filter(SQLUsers.User.username == n).first()
        if row:
            s.query(SQLUsers.Ignore).filter(SQLUsers.Ignore.user_id == row.id).delete()
            s.query(SQLUsers.Ignore).filter(SQLUsers.Ignore.ignored_user_id == row.id).delete()
        s.query(SQLUsers.User).filter(SQLUsers.User.username == n).delete()
    s.commit(); sm.close_guard()

def make_user(name):
    s = sm.sess()
    s.add(SQLUsers.User(name, PW, "1.2.3.4", None, "user"))
    s.commit(); sm.close_guard()

def add_ignore(user_id, ignored_user_id):
    s = sm.sess()
    s.add(SQLUsers.Ignore(user_id, ignored_user_id, None))
    s.commit(); sm.close_guard()

def run_worker(fn, *args):
    try:
        res = fn(*args)
        sm.commit_guard()
        return res
    finally:
        sm.close_guard()

LOGIN_ARGS = ("9.9.9.9", "agenttest", "0", "0", "127.0.0.1", "??")  # ip, agent, last_sys, last_mac, local_ip, country

# --- user with ignores: returns (snapshot, [ignored ids]) ---
wipe(A, B, C)
make_user(A); make_user(B); make_user(C)
aid, bid, cid = uid(A), uid(B), uid(C)
add_ignore(aid, bid); add_ignore(aid, cid)

ret = run_worker(udb.do_login, A, *LOGIN_ARGS)
check(isinstance(ret, tuple) and len(ret) == 2,
      "do_login should return a (snapshot, ignored_ids) tuple, got %r" % (type(ret).__name__,))
snap = ret[0] if isinstance(ret, tuple) else ret
ignored = ret[1] if (isinstance(ret, tuple) and len(ret) > 1) else None
check(snap is not None and snap.username == A,
      "snapshot should be the OfflineClient for the user, got %r" % (snap,))
check(ignored is not None and set(ignored) == {bid, cid},
      "ignored_ids should be the user's ignore list {%r,%r}, got %r" % (bid, cid, ignored))
# the login record must still be written (last_agent updated)
check(snap is not None and snap.last_agent == "agenttest",
      "do_login should still write the login record (last_agent), got %r" % (getattr(snap, 'last_agent', None),))

# --- user with no ignores: returns (snapshot, []) ---
wipe(A); make_user(A)
ret2 = run_worker(udb.do_login, A, *LOGIN_ARGS)
snap2 = ret2[0] if isinstance(ret2, tuple) else ret2
ignored2 = ret2[1] if (isinstance(ret2, tuple) and len(ret2) > 1) else None
check(snap2 is not None and snap2.username == A and ignored2 == [],
      "no-ignores user should return (snapshot, []), got %r" % (ret2,))

# --- vanished user: returns (None, []) ---
wipe(A)
ret3 = run_worker(udb.do_login, A, *LOGIN_ARGS)
check(isinstance(ret3, tuple) and ret3[0] is None and ret3[1] == [],
      "vanished user should return (None, []), got %r" % (ret3,))

wipe(A, B, C)
if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: do_login writes the login record and returns the ignore list off-reactor")
