"""
e2e for offline direct messages: queue a PM to a logged-out user, then assert it is
delivered on their next login, in the right dialect, and deleted afterwards.

Covers: sender echo + row written, legacy delivery (plain SAIDPRIVATE, no timestamp),
jsonchat delivery (JSON SAIDPRIVATE carrying the ORIGINAL send time in microseconds),
delete-on-delivery, the per-pair cap collapsing into one tombstone, bots refused with
nothing stored, and an ignored sender seeing success with nothing stored.

Run: source venv/bin/activate; python3 tests/integration/offlinemsgtest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, sys, json, datetime
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "o1"
raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()

A = "om_%s_sender" % TAG     # sender, stays online
B = "om_%s_legacy" % TAG     # recipient on an old lobby
C = "om_%s_rich" % TAG       # recipient advertising jsonchat
BOT = "om_%s_bot" % TAG      # bot recipient: must refuse
IG = "om_%s_ignorer" % TAG   # ignores A
CAPU = "om_%s_cap" % TAG     # cap victim
ALL = [A, B, C, BOT, IG, CAPU]

DB = DB_KWARGS
errors = []

def check(cond, label):
    if not cond:
        errors.append(label)

def report(label, before):
    new = errors[before:]
    if new:
        print("FAIL: %s" % label)
        for e in new:
            print("  ", e)
    else:
        print("PASS: %s" % label)

# ---------- socket helpers ----------
def recv_until(s, substr, timeout=8):
    s.settimeout(timeout)
    buf = b""
    try:
        while substr.encode() not in buf:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    return buf.decode(errors="replace")

class Sock:
    def __init__(self, s, preamble=""):
        self.s = s
        self.buf = preamble
    def send(self, line):
        self.s.sendall((line + "\n").encode())
    def drain(self, seconds=1.0):
        self.s.settimeout(seconds)
        try:
            while True:
                chunk = self.s.recv(8192)
                if not chunk:
                    break
                self.buf += chunk.decode(errors="replace")
        except socket.timeout:
            pass
        return self.buf
    def json_frames(self):
        out = []
        for ln in self.buf.split("\n"):
            ln = ln.strip()
            if not ln.startswith("JSON "):
                continue
            try:
                out.append(json.loads(ln[5:]))
            except Exception:
                pass
        return out
    def lines(self, prefix):
        return [ln.strip() for ln in self.buf.split("\n") if ln.strip().startswith(prefix)]
    def close(self):
        try: self.s.close()
        except Exception: pass

def connect():
    s = socket.create_connection((HOST, PORT))
    recv_until(s, "\n")
    return s

def register_and_confirm(user):
    s = connect()
    s.sendall(("REGISTER %s %s\n" % (user, PW)).encode())
    recv_until(s, "\n")
    time.sleep(2.5)
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    recv_until(s, "AGREEMENTEND")
    s.sendall(b"CONFIRMAGREEMENT\n")
    ok = "LOGININFOEND" in recv_until(s, "LOGININFOEND")
    s.close()
    return ok

def login_keep(user, compat=None):
    s = connect()
    if compat:
        s.sendall(("LOGIN %s %s 0 * TestClient\t0\t%s\n" % (user, PW, compat)).encode())
    else:
        s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=8)
    if "ACCEPTED" not in resp or "LOGININFOEND" not in resp:
        s.close()
        return None
    # keep whatever arrived with LOGININFOEND: delivery races the login reply
    return Sock(s, preamble=resp)

# ---------- db helpers ----------
def q(sql, args=()):
    conn = pymysql.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        return cur.fetchall()
    finally:
        conn.close()

def x(sql, args=()):
    conn = pymysql.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        conn.commit()
    finally:
        conn.close()

def uid(name):
    r = q("SELECT id FROM users WHERE username=%s", (name,))
    return r[0][0] if r else None

def msg_rows(sender, recipient):
    return q("""SELECT o.id, o.msg, o.ex_msg, o.dropped_count, o.time FROM offline_messages o
                WHERE o.sender_user_id=%s AND o.recipient_user_id=%s ORDER BY o.id""",
             (uid(sender), uid(recipient)))

# ---------- setup ----------
for u in ALL:
    if not register_and_confirm(u):
        print("ABORT: could not register %s" % u); sys.exit(1)
print("setup confirmed: %d/%d" % (len(ALL), len(ALL)))

x("UPDATE users SET bot=1 WHERE username=%s", (BOT,))
x("DELETE FROM offline_messages")
# IG ignores A (via the db: IGNORE needs both online, and we want A's target offline)
x("DELETE FROM ignores WHERE user_id=%s", (uid(IG),))
x("INSERT INTO ignores (user_id, ignored_user_id, reason, time) VALUES (%s,%s,%s,NOW())",
  (uid(IG), uid(A), "test"))

sender = login_keep(A)
if sender is None:
    print("ABORT: sender could not log in"); sys.exit(1)

# ---------- 1. queue to an offline user: echo + row written ----------
n = len(errors)
sender.send("SAYPRIVATE %s hello while away" % B)
sender.drain(1.5)
echo = [ln for ln in sender.lines("SAYPRIVATE") if B in ln]
check(bool(echo), "sender should get a SAYPRIVATE echo for an offline target, buf=%r" % sender.buf[-200:])
r = msg_rows(A, B)
check(len(r) == 1, "one row should be queued, got %d" % len(r))
check(r and r[0][1] == "hello while away", "stored msg wrong: %r" % (r and r[0][1],))
report("queue to offline user -> echo + row stored", n)

# ---------- 2. bot recipient: FAILED, nothing stored ----------
n = len(errors)
sender.buf = ""
sender.send("SAYPRIVATE %s !host smallmap" % BOT)
sender.drain(1.5)
check("FAILED" in sender.buf and "bots" in sender.buf,
      "PM to an offline bot should be FAILED, buf=%r" % sender.buf[-200:])
check(len(msg_rows(A, BOT)) == 0, "a message to a bot must not be stored")
report("offline bot refused, nothing stored", n)

# ---------- 3. ignored sender: echo (indistinguishable), nothing stored ----------
n = len(errors)
sender.buf = ""
sender.send("SAYPRIVATE %s you cannot see this" % IG)
sender.drain(1.5)
check(bool([ln for ln in sender.lines("SAYPRIVATE") if IG in ln]),
      "an ignored sender must still get an echo, buf=%r" % sender.buf[-200:])
check("FAILED" not in sender.buf, "an ignored sender must not be able to detect the ignore")
check(len(msg_rows(A, IG)) == 0, "an ignored sender's message must never be stored")
report("ignored sender -> echo, nothing stored", n)

# ---------- 4. legacy delivery: plain SAIDPRIVATE, then deleted ----------
n = len(errors)
b = login_keep(B)
if b is None:
    errors.append("legacy recipient could not log in")
else:
    b.drain(2.0)
    said = [ln for ln in b.lines("SAIDPRIVATE") if A in ln and "hello while away" in ln]
    check(bool(said), "legacy client should receive plain SAIDPRIVATE, buf=%r" % b.buf[-300:])
    check(not any("SAIDPRIVATE" in json.dumps(f) for f in b.json_frames()),
          "legacy client must not receive JSON frames")
    time.sleep(1.0)  # let the delete deferred land
    check(len(msg_rows(A, B)) == 0, "delivered message must be deleted (delete-on-delivery)")
    b.close()
report("legacy delivery -> plain SAIDPRIVATE + delete-on-delivery", n)

# ---------- 5. jsonchat delivery: original timestamp preserved ----------
n = len(errors)
# backdate the row so "original send time" is unmistakably not "login time"
sent_at = datetime.datetime.now().replace(microsecond=0) - datetime.timedelta(hours=9)
x("DELETE FROM offline_messages")
x("""INSERT INTO offline_messages (sender_user_id, recipient_user_id, time, msg, ex_msg, dropped_count)
     VALUES (%s,%s,%s,%s,0,0)""", (uid(A), uid(C), sent_at, "sent nine hours ago"))
expect_us = int(sent_at.timestamp()) * 1000000

c = login_keep(C, "jsonchat")
if c is None:
    errors.append("jsonchat recipient could not log in")
else:
    c.drain(2.0)
    frames = [f["SAIDPRIVATE"] for f in c.json_frames() if "SAIDPRIVATE" in f]
    check(len(frames) == 1, "jsonchat client should get exactly one JSON SAIDPRIVATE, got %d (buf=%r)"
          % (len(frames), c.buf[-300:]))
    if frames:
        f = frames[0]
        check(f.get("userName") == A, "frame userName=%r, want %r" % (f.get("userName"), A))
        check(f.get("msg") == "sent nine hours ago", "frame msg=%r" % (f.get("msg"),))
        t = f.get("time")
        check(isinstance(t, int), "frame time should be an int, got %r (%s)" % (t, type(t).__name__))
        check(t == expect_us, "frame time=%r, want the ORIGINAL send time %r (not login time)" % (t, expect_us))
    # no plain SAIDPRIVATE for a jsonchat client - it would double up
    check(not [ln for ln in c.lines("SAIDPRIVATE") if not ln.startswith("JSON")],
          "jsonchat client must not also get a plain SAIDPRIVATE")
    time.sleep(1.0)
    check(len(msg_rows(A, C)) == 0, "delivered message must be deleted")
    c.close()
report("jsonchat delivery -> JSON SAIDPRIVATE with the original timestamp", n)

# ---------- 6. per-pair cap collapses into one tombstone ----------
n = len(errors)
x("DELETE FROM offline_messages")
CAP = 50
sender.buf = ""
for i in range(CAP + 1):
    sender.send("SAYPRIVATE %s capmsg%d" % (CAPU, i))
    time.sleep(0.02)  # stay under the byte-rate flood limit
deadline = time.time() + 15
while time.time() < deadline:
    r = msg_rows(A, CAPU)
    if len(r) == CAP + 1:  # CAP content + 1 tombstone
        break
    time.sleep(0.3)
r = msg_rows(A, CAPU)
content = [row for row in r if row[1] is not None]
tombs = [row for row in r if row[1] is None]
check(len(content) == CAP, "content should be capped at %d, got %d" % (CAP, len(content)))
check(len(tombs) == 1, "exceeding the cap should leave exactly one tombstone, got %d" % len(tombs))
check(tombs and tombs[0][3] == 1, "tombstone dropped_count should be 1, got %r" % (tombs and tombs[0][3],))
check(content and content[0][1] == "capmsg1", "oldest should have been dropped, first is %r" % (content and content[0][1],))
report("51st message to one user collapses the oldest into a tombstone", n)

# ---------- 7. tombstone is delivered, in both dialects ----------
n = len(errors)
x("DELETE FROM offline_messages")
x("""INSERT INTO offline_messages (sender_user_id, recipient_user_id, time, msg, ex_msg, dropped_count)
     VALUES (%s,%s,NOW(),NULL,0,7)""", (uid(A), uid(CAPU)))
cu = login_keep(CAPU)
if cu is None:
    errors.append("cap user could not log in (legacy tombstone)")
else:
    cu.drain(2.0)
    check("SERVERMSG" in cu.buf and "expired" in cu.buf,
          "legacy client should get a plain-text tombstone notice, buf=%r" % cu.buf[-300:])
    cu.close()
    time.sleep(1.0)
    check(len(msg_rows(A, CAPU)) == 0, "a delivered tombstone must be deleted")
report("legacy tombstone -> SERVERMSG notice + deleted", n)

n = len(errors)
x("""INSERT INTO offline_messages (sender_user_id, recipient_user_id, time, msg, ex_msg, dropped_count)
     VALUES (%s,%s,NOW(),NULL,0,7)""", (uid(A), uid(CAPU)))
cu = login_keep(CAPU, "jsonchat")
if cu is None:
    errors.append("cap user could not log in (jsonchat tombstone)")
else:
    cu.drain(2.0)
    drops = [f["OFFLINEMESSAGESDROPPED"] for f in cu.json_frames() if "OFFLINEMESSAGESDROPPED" in f]
    check(len(drops) == 1, "jsonchat client should get one tombstone frame, got %d (buf=%r)" % (len(drops), cu.buf[-300:]))
    if drops:
        check(drops[0].get("count") == 7, "tombstone count=%r, want 7" % (drops[0].get("count"),))
        check(drops[0].get("userName") == A, "tombstone userName=%r, want %r" % (drops[0].get("userName"), A))
    cu.close()
report("jsonchat tombstone -> OFFLINEMESSAGESDROPPED frame", n)

# ---------- 8. online path is unchanged ----------
n = len(errors)
x("DELETE FROM offline_messages")
b = login_keep(B)
if b is None:
    errors.append("recipient could not log in for the online check")
else:
    b.drain(1.0); b.buf = ""
    sender.buf = ""
    sender.send("SAYPRIVATE %s live message" % B)
    b.drain(1.5)
    check(bool([ln for ln in b.lines("SAIDPRIVATE") if "live message" in ln]),
          "an online PM must still be delivered live, buf=%r" % b.buf[-200:])
    check(len(msg_rows(A, B)) == 0, "an online PM must not be queued")
    b.close()
report("online PM still delivered live and never queued", n)

sender.close()
x("DELETE FROM offline_messages")
x("DELETE FROM ignores WHERE user_id=%s", (uid(IG),))

if errors:
    print("FAIL: %d errors total" % len(errors))
    sys.exit(1)
print("PASS: offline direct messages e2e")
sys.exit(0)
