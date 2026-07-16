"""
Worker-unit tests for the offline direct message store (do_enqueue_offline_message,
do_fetch_offline_messages, do_delete_offline_messages). Runs in-process against the
MariaDB test db (no server needed). Mirrors _run_db.

RED expectation before implementation: AttributeError (the workers don't exist).

Run: source venv/bin/activate; python3 tests/worker/offlinemessageworkertest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_URL
from datetime import datetime, timedelta
import sys
import sqlalchemy
import SQLUsers

SENDER = "phase3_om_sender"
RECIP = "phase3_om_recip"
BOT = "phase3_om_bot"
OTHER = "phase3_om_other"
ALL = [SENDER, RECIP, BOT, OTHER]

class FakeRoot:
    def __init__(self, engine):
        self.session_manager = SQLUsers.session_manager(self, engine)

engine = sqlalchemy.create_engine(DB_URL, pool_size=5, pool_recycle=3600)
root = FakeRoot(engine)
sm = root.session_manager
udb = SQLUsers.UsersHandler(root)

errors = []
def check(cond, label):
    if not cond:
        errors.append(label)

def run_worker(fn, *args):
    # mirrors DataHandler._run_db: call the worker, commit, always close
    try:
        res = fn(*args)
        sm.commit_guard()
        return res
    finally:
        sm.close_guard()

def mkuser(name, bot=0):
    s = sm.sess()
    s.query(SQLUsers.User).filter(SQLUsers.User.username == name).delete()
    s.commit()
    u = SQLUsers.User(name, "pw", "1.2.3.4", None, 'user')
    u.bot = bot
    s.add(u)
    s.commit()
    uid = u.id
    sm.close_guard()
    return uid

def rows(sender_id, recipient_id):
    s = sm.sess()
    r = s.query(SQLUsers.OfflineMessage).filter(
        SQLUsers.OfflineMessage.sender_user_id == sender_id).filter(
        SQLUsers.OfflineMessage.recipient_user_id == recipient_id).order_by(
        SQLUsers.OfflineMessage.id).all()
    out = [(x.id, x.msg, x.ex_msg, x.dropped_count, x.time) for x in r]
    sm.close_guard()
    return out

def wipe_msgs():
    s = sm.sess()
    s.query(SQLUsers.OfflineMessage).delete()
    s.commit()
    sm.close_guard()

# ---------- setup ----------
sid = mkuser(SENDER)
rid = mkuser(RECIP)
bid = mkuser(BOT, bot=1)
oid = mkuser(OTHER)
wipe_msgs()

# ---------- happy path ----------
ret = run_worker(udb.do_enqueue_offline_message, sid, RECIP, "hello", False)
check(ret == ('ok', rid), "enqueue should return ('ok', recipient_id), got %r" % (ret,))
r = rows(sid, rid)
check(len(r) == 1, "enqueue should write exactly one row, got %d" % len(r))
check(r and r[0][1] == "hello", "stored msg should be 'hello', got %r" % (r and r[0][1],))
check(r and r[0][2] == False, "ex_msg should be False, got %r" % (r and r[0][2],))

# ex_msg is carried through (SAYPRIVATEEX)
run_worker(udb.do_enqueue_offline_message, sid, RECIP, "waves", True)
r = rows(sid, rid)
check(len(r) == 2 and r[1][2] == True, "ex_msg=True should round-trip, got %r" % (r,))

# ---------- unknown user ----------
ret = run_worker(udb.do_enqueue_offline_message, sid, "phase3_om_ghost", "hi", False)
check(ret == ('nouser',), "unknown recipient should return ('nouser',), got %r" % (ret,))

# ---------- bots are refused outright, and nothing is written ----------
ret = run_worker(udb.do_enqueue_offline_message, sid, BOT, "!host map", False)
check(ret == ('bot',), "bot recipient should return ('bot',), got %r" % (ret,))
check(len(rows(sid, bid)) == 0, "a message to a bot must not be stored at all")

# ---------- ignore: reported as success, but never written ----------
s = sm.sess()
s.add(SQLUsers.Ignore(oid, sid, "no thanks"))  # OTHER ignores SENDER
s.commit()
sm.close_guard()
ret = run_worker(udb.do_enqueue_offline_message, sid, OTHER, "hi", False)
check(ret == ('ok', oid), "an ignored sender must see success, got %r" % (ret,))
check(len(rows(sid, oid)) == 0, "an ignored sender's message must never be stored")

# ---------- self-send ----------
ret = run_worker(udb.do_enqueue_offline_message, sid, SENDER, "note to self", False)
check(ret == ('nouser',), "self-send should not queue, got %r" % (ret,))

# ---------- per-pair cap: oldest content collapses into one tombstone ----------
wipe_msgs()
CAP = SQLUsers.OFFLINE_MSG_MAX_PER_PAIR
for i in range(CAP):
    run_worker(udb.do_enqueue_offline_message, sid, RECIP, "m%d" % i, False)
r = rows(sid, rid)
check(len(r) == CAP, "should hold exactly %d before the cap bites, got %d" % (CAP, len(r)))
check(all(x[1] is not None for x in r), "no tombstone should exist before the cap is exceeded")

# one over the cap: still CAP content rows, plus exactly one tombstone
run_worker(udb.do_enqueue_offline_message, sid, RECIP, "over1", False)
r = rows(sid, rid)
content = [x for x in r if x[1] is not None]
tombs = [x for x in r if x[1] is None]
check(len(content) == CAP, "cap should hold content at %d, got %d" % (CAP, len(content)))
check(len(tombs) == 1, "exceeding the cap should create exactly one tombstone, got %d" % len(tombs))
check(tombs and tombs[0][3] == 1, "tombstone dropped_count should be 1, got %r" % (tombs and tombs[0][3],))
check(content and content[0][1] == "m1", "oldest content should have been dropped (want m1 first), got %r" % (content and content[0][1],))
check(content and content[-1][1] == "over1", "newest content should be 'over1', got %r" % (content and content[-1][1],))

# further overflow accumulates into the SAME tombstone, not new ones
run_worker(udb.do_enqueue_offline_message, sid, RECIP, "over2", False)
run_worker(udb.do_enqueue_offline_message, sid, RECIP, "over3", False)
r = rows(sid, rid)
tombs = [x for x in r if x[1] is None]
check(len(tombs) == 1, "overflow must collapse into ONE tombstone, got %d" % len(tombs))
check(tombs and tombs[0][3] == 3, "tombstone should count 3 drops, got %r" % (tombs and tombs[0][3],))
check(len([x for x in r if x[1] is not None]) == CAP, "content must stay capped at %d" % CAP)

# ---------- fetch resolves the sender username in-query ----------
wipe_msgs()
run_worker(udb.do_enqueue_offline_message, sid, RECIP, "first", False)
run_worker(udb.do_enqueue_offline_message, sid, RECIP, "second", True)
fetched = run_worker(udb.do_fetch_offline_messages, rid)
check(len(fetched) == 2, "fetch should return 2 rows, got %d" % len(fetched))
if len(fetched) == 2:
    (i0, s0, n0, t0, m0, e0, d0) = fetched[0]
    check(n0 == SENDER, "fetch should resolve the sender username, got %r" % (n0,))
    check(m0 == "first", "fetch should be ordered by id (want 'first'), got %r" % (m0,))
    check(e0 == False, "ex_msg should round-trip as False, got %r" % (e0,))
    check(isinstance(t0, datetime), "fetch should return a datetime, got %r" % (type(t0),))
    check(fetched[1][4] == "second" and fetched[1][5] == True, "second row should be the ex_msg, got %r" % (fetched[1],))

# fetch is scoped to the recipient
check(run_worker(udb.do_fetch_offline_messages, oid) == [], "fetch must not leak another user's messages")

# ---------- delete-on-delivery ----------
ids = [f[0] for f in fetched]
n = run_worker(udb.do_delete_offline_messages, ids)
check(n == 2, "delete should remove 2 rows, got %r" % (n,))
check(run_worker(udb.do_fetch_offline_messages, rid) == [], "delivered messages must be gone")
check(run_worker(udb.do_delete_offline_messages, []) == 0, "empty delete should be a no-op")

# ---------- tombstone survives a fetch/delete cycle only until delivered ----------
wipe_msgs()
s = sm.sess()
s.add(SQLUsers.OfflineMessage(sid, rid, datetime.now() - timedelta(days=30), None, False, 7))
s.commit()
sm.close_guard()
fetched = run_worker(udb.do_fetch_offline_messages, rid)
check(len(fetched) == 1 and fetched[0][4] is None, "a tombstone should be fetched like any row, got %r" % (fetched,))
check(fetched and fetched[0][6] == 7, "tombstone dropped_count should survive the fetch, got %r" % (fetched and fetched[0][6],))
run_worker(udb.do_delete_offline_messages, [fetched[0][0]])
check(run_worker(udb.do_fetch_offline_messages, rid) == [], "a delivered tombstone must be deleted")

wipe_msgs()
if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors:
        print("  -", e)
    sys.exit(1)
print("PASS: offline message workers (enqueue/cap/tombstone/ignore/bot/fetch/delete)")
