"""
Worker-unit test for RENAMEACCOUNT (OPTIMIZATIONS 3.1): do_rename_account, the off-reactor
"verify newname is free, then write the new username + a Rename record" unit. Runs in-process
against the MariaDB test db (no server needed). Mirrors _run_db (call fn -> commit_guard ->
close_guard) and asserts via a fresh-session re-read.

RED expectation before implementation: AttributeError (do_rename_account doesn't exist).

NOTE: the residual rename-to-taken IntegrityError(1062) path is a CONCURRENCY path (two
flushes racing) - it cannot be exercised single-threaded here (the early uniqueness query
always sees committed state), so it is covered by the concurrent e2e test, not this unit.

Run: source venv/bin/activate; python3 .renameaccountworkertest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_URL
import sys
import sqlalchemy
import SQLUsers

URL = DB_URL
A = "phase3_rename_a"
B = "phase3_rename_b"
NEW = "phase3_rename_new"
GONE = "phase3_rename_gone"

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
        rows = s.query(SQLUsers.User).filter(SQLUsers.User.username == n).all()
        for r in rows:
            s.query(SQLUsers.Rename).filter(SQLUsers.Rename.user_id == r.id).delete()
        s.query(SQLUsers.User).filter(SQLUsers.User.username == n).delete()
    s.commit(); sm.close_guard()

def make_user(name):
    s = sm.sess()
    s.add(SQLUsers.User(name, "pw_encoded", "1.2.3.4", None, "user"))
    s.commit(); sm.close_guard()

def run_worker(fn, *args):
    try:
        res = fn(*args)
        sm.commit_guard()
        return res
    finally:
        sm.close_guard()

def username_of(name):
    s = sm.sess()
    row = s.query(SQLUsers.User).filter(SQLUsers.User.username == name).first()
    out = row.username if row else None
    sm.close_guard(); return out

def rename_count(uid, original):
    s = sm.sess()
    n = s.query(SQLUsers.Rename).filter(SQLUsers.Rename.user_id == uid,
                                        SQLUsers.Rename.original == original).count()
    sm.close_guard(); return n

wipe(A, B, NEW, NEW.upper(), A.upper())

# 1. happy path: A -> NEW returns ('ok', uid, NEW); row renamed; a Rename(original=A) recorded
make_user(A)
ret = run_worker(udb.do_rename_account, A, NEW)
check(isinstance(ret, tuple) and ret[0] == 'ok', "rename should return ('ok', uid, newname), got %r" % (ret,))
check(len(ret) == 3 and ret[2] == NEW, "ok verdict should carry the newname, got %r" % (ret,))
check(isinstance(ret[1], int) and ret[1] > 0, "ok verdict should carry a real uid, got %r" % (ret,))
check(username_of(NEW) == NEW, "username should be persisted as NEW, got %r" % (username_of(NEW),))
check(username_of(A) is None, "old username must no longer exist, got %r" % (username_of(A),))
check(rename_count(ret[1], A) == 1, "a Rename(original=%r) should be recorded, count=%r" % (A, rename_count(ret[1], A)))
wipe(A, NEW)

# 2. newname taken by ANOTHER user -> ('denied', 'Username already exists.'), A unchanged
make_user(A); make_user(B)
ret2 = run_worker(udb.do_rename_account, A, B)
check(ret2 == ('denied', 'Username already exists.'), "rename to a taken name should be denied, got %r" % (ret2,))
check(username_of(A) == A, "denied rename must leave the source username unchanged, got %r" % (username_of(A),))
wipe(A, B)

# 3. case-only collision with ANOTHER user (CI collation) -> denied
make_user(A); make_user(B)
ret3 = run_worker(udb.do_rename_account, B, A.upper())
check(ret3 == ('denied', 'Username already exists.'), "case-variant of another user's name should be denied, got %r" % (ret3,))
check(username_of(B) == B, "denied case-collision must leave source unchanged, got %r" % (username_of(B),))
wipe(A, B)

# 4. case-only SELF-rename (CI collation: early query matches own row) -> denied, preserved behaviour
make_user(A)
ret4 = run_worker(udb.do_rename_account, A, A.upper())
check(ret4 == ('denied', 'Username already exists.'), "case-only self-rename should be denied (CI collation), got %r" % (ret4,))
check(username_of(A) == A, "denied self-rename must leave username unchanged, got %r" % (username_of(A),))
wipe(A, A.upper())

# 5. vanished user -> ('denied', "...don't seem to exist...")
ret5 = run_worker(udb.do_rename_account, GONE, NEW)
check(ret5 == ('denied', "You don't seem to exist anymore. Contact an admin or moderator."),
      "rename of a missing user should report non-existence, got %r" % (ret5,))

# 6. session still usable after the denied paths: a real rename still works
make_user(A)
ret6 = run_worker(udb.do_rename_account, A, NEW)
check(ret6[0] == 'ok', "rename after denied paths should still work, got %r" % (ret6,))
check(username_of(NEW) == NEW, "rename after denied paths should persist, got %r" % (username_of(NEW),))
wipe(A, NEW)

if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: do_rename_account frees-checks the newname, writes username+Rename, and is retry-safe")
