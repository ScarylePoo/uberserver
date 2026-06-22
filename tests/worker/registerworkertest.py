"""
Worker-unit test for PR A (OPTIMIZATIONS 3.1): do_register_insert, the off-reactor INSERT
that catches the unique-constraint IntegrityError race and reports ('taken',). Runs
in-process against the MariaDB test db (no server needed). Mirrors _run_db.

RED expectation before implementation: AttributeError (do_register_insert doesn't exist).

Run: source venv/bin/activate; python3 .registerworkertest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_URL
import sys
import sqlalchemy
import SQLUsers

URL = DB_URL
U1 = "phase3_reg_a"
U2 = "phase3_reg_b"
U3 = "phase3_reg_c"
EMAIL = "phase3reg@example.com"

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

def run_worker(fn, *args):
    try:
        res = fn(*args)
        sm.commit_guard()
        return res
    finally:
        sm.close_guard()

def count(name):
    s = sm.sess()
    n = s.query(SQLUsers.User).filter(SQLUsers.User.username == name).count()
    sm.close_guard(); return n

wipe(U1, U2, U3)

# fresh insert -> ok + new id, one row
ret = run_worker(udb.do_register_insert, U1, "pw", "1.2.3.4", None)
check(isinstance(ret, tuple) and ret[0] == 'ok', "fresh insert should return ('ok', id), got %r" % (ret,))
check(isinstance(ret[1], int) and ret[1] > 0, "fresh insert should return a real id, got %r" % (ret,))
check(count(U1) == 1, "fresh insert should create exactly one row (got %d)" % count(U1))

# duplicate username -> taken, still one row, session still usable
ret2 = run_worker(udb.do_register_insert, U1, "pw2", "1.2.3.4", None)
check(ret2 == ('taken',), "duplicate username should return ('taken',), got %r" % (ret2,))
check(count(U1) == 1, "duplicate username must not create a second row (got %d)" % count(U1))

# session still usable after the caught IntegrityError: a different user inserts fine
ret3 = run_worker(udb.do_register_insert, U2, "pw", "1.2.3.4", None)
check(ret3[0] == 'ok', "insert after a caught IntegrityError should still work, got %r" % (ret3,))

# duplicate email -> taken (email column is unique too)
wipe(U3)
ru = run_worker(udb.do_register_insert, U3, "pw", "1.2.3.4", EMAIL)
check(ru[0] == 'ok', "insert with email should work, got %r" % (ru,))
rd = run_worker(udb.do_register_insert, U2 + "x", "pw", "1.2.3.4", EMAIL)
check(rd == ('taken',), "duplicate email should return ('taken',), got %r" % (rd,))

wipe(U1, U2, U3, U2 + "x")
if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: do_register_insert handles fresh insert + unique-constraint race")
