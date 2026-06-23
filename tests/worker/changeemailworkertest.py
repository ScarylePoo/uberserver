"""
Worker-unit test for CHANGEEMAIL (OPTIMIZATIONS 3.1): do_change_email, the off-reactor
"write ONLY the users.email field" unit. Runs in-process against the MariaDB test db (no
server needed). Mirrors _run_db (call fn -> commit_guard -> close_guard) and asserts via a
fresh-session re-read.

RED expectation before implementation: AttributeError (do_change_email doesn't exist).

Unlike do_rename_account, do_change_email has NO early uniqueness query (the reactor owns the
get_user_id_with_email pre-check), so the residual change-to-taken IntegrityError(1062) path
IS reachable single-threaded here: flush() sends the UPDATE and the DB's UNIQUE(email) raises.

Run: source venv/bin/activate; python3 .changeemailworkertest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_URL
import sys
import sqlalchemy
import SQLUsers

URL = DB_URL
A = "phase3_cemail_a"
B = "phase3_cemail_b"
GONE = "phase3_cemail_gone"
MAIL_A = "phase3_cemail_a@example.test"
MAIL_B = "phase3_cemail_b@example.test"
MAIL_NEW = "phase3_cemail_new@example.test"

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

def make_user(name, email):
    s = sm.sess()
    s.add(SQLUsers.User(name, "pw_encoded", "1.2.3.4", email, "user"))
    s.commit(); sm.close_guard()

def run_worker(fn, *args):
    try:
        res = fn(*args)
        sm.commit_guard()
        return res
    finally:
        sm.close_guard()

def email_of(name):
    s = sm.sess()
    row = s.query(SQLUsers.User).filter(SQLUsers.User.username == name).first()
    out = row.email if row else None
    sm.close_guard(); return out

def name_with_email(email):
    s = sm.sess()
    row = s.query(SQLUsers.User).filter(SQLUsers.User.email == email).first()
    out = row.username if row else None
    sm.close_guard(); return out

wipe(A, B, GONE)

# 1. happy path: A's email A -> NEW returns ('ok', uid, NEW); row updated; old email gone
make_user(A, MAIL_A)
ret = run_worker(udb.do_change_email, A, MAIL_NEW)
check(isinstance(ret, tuple) and ret[0] == 'ok', "change should return ('ok', uid, newmail), got %r" % (ret,))
check(len(ret) == 3 and ret[2] == MAIL_NEW, "ok verdict should carry the newmail, got %r" % (ret,))
check(isinstance(ret[1], int) and ret[1] > 0, "ok verdict should carry a real uid, got %r" % (ret,))
check(email_of(A) == MAIL_NEW, "email should be persisted as NEW, got %r" % (email_of(A),))
check(name_with_email(MAIL_A) is None, "the old email must no longer be on any row, got %r" % (name_with_email(MAIL_A),))
wipe(A)

# 2. change to an email taken by ANOTHER user -> ('denied', taken msg) via 1062; A unchanged
make_user(A, MAIL_A); make_user(B, MAIL_B)
ret2 = run_worker(udb.do_change_email, A, MAIL_B)
check(ret2 == ('denied', "another user is already registered to the email address '%s'" % MAIL_B),
      "change to a taken email should be denied via 1062, got %r" % (ret2,))
check(email_of(A) == MAIL_A, "denied change must leave the source email unchanged, got %r" % (email_of(A),))
check(email_of(B) == MAIL_B, "the other user's email must be untouched, got %r" % (email_of(B),))
wipe(A, B)

# 3. only email is written: ingame_time / access are NOT clobbered by the worker
make_user(A, MAIL_A)
s = sm.sess()
row = s.query(SQLUsers.User).filter(SQLUsers.User.username == A).first()
row.ingame_time = 4242; row.access = "moderator"
s.commit(); sm.close_guard()
ret3 = run_worker(udb.do_change_email, A, MAIL_NEW)
check(ret3[0] == 'ok', "email-only change should succeed, got %r" % (ret3,))
check(email_of(A) == MAIL_NEW, "email should change, got %r" % (email_of(A),))
s = sm.sess()
row = s.query(SQLUsers.User).filter(SQLUsers.User.username == A).first()
check(row.ingame_time == 4242, "ingame_time must be left untouched by do_change_email, got %r" % (row.ingame_time,))
check(row.access == "moderator", "access must be left untouched by do_change_email, got %r" % (row.access,))
sm.close_guard()
wipe(A)

# 4. self-email (same value) does not 1062 against the row's own value -> ('ok', ...)
make_user(A, MAIL_A)
ret4 = run_worker(udb.do_change_email, A, MAIL_A)
check(ret4[0] == 'ok', "changing email to its own current value should not 1062, got %r" % (ret4,))
check(email_of(A) == MAIL_A, "self-email should remain, got %r" % (email_of(A),))
wipe(A)

# 5. vanished user -> ('denied', "...don't seem to exist...")
ret5 = run_worker(udb.do_change_email, GONE, MAIL_NEW)
check(ret5 == ('denied', "You don't seem to exist anymore. Contact an admin or moderator."),
      "change for a missing user should report non-existence, got %r" % (ret5,))

# 6. session still usable after the denied/1062 paths: a real change still works
make_user(A, MAIL_A)
ret6 = run_worker(udb.do_change_email, A, MAIL_NEW)
check(ret6[0] == 'ok', "change after denied paths should still work, got %r" % (ret6,))
check(email_of(A) == MAIL_NEW, "change after denied paths should persist, got %r" % (email_of(A),))
wipe(A)

if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: do_change_email writes ONLY email, denies taken-email via 1062, and is retry-safe")
