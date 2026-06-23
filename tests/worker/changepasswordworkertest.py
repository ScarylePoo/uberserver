"""
Worker-unit test for CHANGEPASSWORD (OPTIMIZATIONS 3.1): do_change_password, the
off-reactor "re-verify current password, then write the new one" unit. Runs in-process
against the MariaDB test db (no server needed). Mirrors _run_db (call fn -> commit_guard
-> close_guard) and asserts via a fresh-session re-read.

RED expectation before implementation: AttributeError (do_change_password doesn't exist).

Run: source venv/bin/activate; python3 .changepasswordworkertest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_URL
import sys
import sqlalchemy
import SQLUsers

URL = DB_URL
U = "phase3_chpw_a"
OLD = "oldpw_encoded"
NEW = "newpw_encoded"

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

def wipe(*names):
    s = sm.sess()
    for n in names:
        s.query(SQLUsers.User).filter(SQLUsers.User.username == n).delete()
    s.commit(); sm.close_guard()

def make_user(name, pw):
    s = sm.sess()
    s.add(SQLUsers.User(name, pw, "1.2.3.4", None, "user"))
    s.commit(); sm.close_guard()

def run_worker(fn, *args):
    try:
        res = fn(*args)
        sm.commit_guard()
        return res
    finally:
        sm.close_guard()

def stored_pw(name):
    s = sm.sess()
    row = s.query(SQLUsers.User).filter(SQLUsers.User.username == name).first()
    pw = row.password if row else None
    sm.close_guard(); return pw

wipe(U)
make_user(U, OLD)

# correct current password -> ('ok', id) and the new password is persisted
ret = run_worker(udb.do_change_password, U, OLD, NEW)
check(isinstance(ret, tuple) and ret[0] == 'ok', "correct cur_pw should return ('ok', id), got %r" % (ret,))
check(isinstance(ret[1], int) and ret[1] > 0, "correct cur_pw should return a real id, got %r" % (ret,))
check(stored_pw(U) == NEW, "correct cur_pw should persist the new password, got %r" % (stored_pw(U),))

# wrong current password -> ('denied', reason), password unchanged
wipe(U); make_user(U, OLD)
ret2 = run_worker(udb.do_change_password, U, "wrong_pw", NEW)
check(isinstance(ret2, tuple) and ret2[0] == 'denied', "wrong cur_pw should return ('denied', reason), got %r" % (ret2,))
check(stored_pw(U) == OLD, "wrong cur_pw must NOT change the password, got %r" % (stored_pw(U),))

# nonexistent user -> ('denied', reason), no crash, session still usable
wipe(U)
ret3 = run_worker(udb.do_change_password, U, OLD, NEW)
check(isinstance(ret3, tuple) and ret3[0] == 'denied', "missing user should return ('denied', reason), got %r" % (ret3,))

# session still usable after the denied paths: a real change still works
make_user(U, OLD)
ret4 = run_worker(udb.do_change_password, U, OLD, NEW)
check(ret4[0] == 'ok', "change after denied paths should still work, got %r" % (ret4,))
check(stored_pw(U) == NEW, "change after denied paths should persist, got %r" % (stored_pw(U),))

wipe(U)
if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: do_change_password verifies cur_pw, writes new_pw, and is retry-safe")
