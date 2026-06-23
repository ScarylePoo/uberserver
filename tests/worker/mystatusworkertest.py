"""
Worker-unit test for PR B (OPTIMIZATIONS 3.1): the new off-reactor DB units
do_set_ingame_time + do_end_session. Runs in-process against the MariaDB test db
(no server needed). Mirrors _run_db: call the worker, then commit_guard/close_guard,
then re-read in a fresh session to assert the persisted row.

RED expectation before implementation: AttributeError (functions don't exist yet).

Run: source venv/bin/activate; python3 .mystatusworkertest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_URL
import sys, datetime
import sqlalchemy
import SQLUsers

URL = DB_URL
U = "phase3_wkr_user"

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

# ---- fixture: a fresh user with one open login row ----
def reset_fixture():
    s = sm.sess()
    s.query(SQLUsers.User).filter(SQLUsers.User.username == U).delete()  # cascade removes logins
    s.commit()
    entry = SQLUsers.User(U, "pw", "1.2.3.4", None)
    entry.ingame_time = 5
    s.add(entry)
    s.flush()
    login = SQLUsers.Login(datetime.datetime.now(), entry.id, "1.2.3.4", "agent", "", "", "", "??")
    s.add(login)
    s.commit()
    uid = entry.id
    sm.close_guard()
    return uid

def run_worker(fn, *args):
    # mirror DataHandler._run_db's commit/close ownership for a single attempt
    try:
        res = fn(*args)
        sm.commit_guard()
        return res
    finally:
        sm.close_guard()

def db_user(uid):
    s = sm.sess()
    row = s.query(SQLUsers.User).filter(SQLUsers.User.id == uid).first()
    out = (row.ingame_time, row.last_login, row.username) if row else None
    sm.close_guard()
    return out

def db_last_login_end(uid):
    s = sm.sess()
    row = s.query(SQLUsers.Login).filter(SQLUsers.Login.user_id == uid).order_by(SQLUsers.Login.id.desc()).first()
    out = row.end if row else "no-login"
    sm.close_guard()
    return out

# ===================== do_set_ingame_time =====================
uid = reset_fixture()
ret = run_worker(udb.do_set_ingame_time, U, 42)
check(ret == uid, "do_set_ingame_time should return the user id (got %r, want %r)" % (ret, uid))
check(db_user(uid)[0] == 42, "ingame_time not persisted as 42 (got %r)" % (db_user(uid)[0],))
# idempotent / retry-safe: re-running with the same absolute value stays 42, never doubles
run_worker(udb.do_set_ingame_time, U, 42)
check(db_user(uid)[0] == 42, "ingame_time not retry-safe (got %r, want 42)" % (db_user(uid)[0],))
# unknown user -> None, no crash
check(run_worker(udb.do_set_ingame_time, "no_such_user_xyz", 9) is None, "missing user should return None")

# ===================== do_end_session =====================
uid = reset_fixture()
before = db_user(uid)[1]
ret = run_worker(udb.do_end_session, uid)
check(ret == U, "do_end_session should return the username (got %r)" % (ret,))
end = db_last_login_end(uid)
check(isinstance(end, datetime.datetime), "login end not set (got %r)" % (end,))
after = db_user(uid)[1]
check(after is not None and (before is None or after >= before), "last_login not refreshed")
# second call: login already ended -> None, end timestamp not overwritten
end1 = db_last_login_end(uid)
ret2 = run_worker(udb.do_end_session, uid)
check(ret2 is None, "second do_end_session should return None (login already ended), got %r" % (ret2,))
check(db_last_login_end(uid) == end1, "already-ended login end was overwritten")
# unknown user -> None
check(run_worker(udb.do_end_session, 999999999) is None, "missing user end_session should return None")

# cleanup
s = sm.sess(); s.query(SQLUsers.User).filter(SQLUsers.User.username == U).delete(); s.commit(); sm.close_guard()

if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: do_set_ingame_time + do_end_session worker units correct")
