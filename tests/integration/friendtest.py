import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, threading, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "f1"
M = 20          # independent concurrent accept pairs
raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()
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
    def drain(self, seconds=0.6):
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
pair_req = ["frq_%s_r%d" % (TAG, i) for i in range(M)]   # requesters (concurrency)
pair_tgt = ["frq_%s_t%d" % (TAG, i) for i in range(M)]   # targets (concurrency)
funcs = ["fn_%s_A" % TAG, "fn_%s_B" % TAG, "fn_%s_C" % TAG]  # functional scenario users
race = ["rc_%s_R" % TAG, "rc_%s_T" % TAG]                # same-pair double-accept
allusers = pair_req + pair_tgt + funcs + race

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
fmt = ",".join(["%s"] * len(allusers))
cur.execute("SELECT id, username FROM users WHERE username IN (%s)" % fmt, allusers)
idmap = {name: uid for (uid, name) in cur.fetchall()}

def fr_count(a, b):  # friend request a->b
    cur.execute("SELECT COUNT(*) FROM friendRequests WHERE user_id=%s AND friend_user_id=%s", (a, b)); return cur.fetchone()[0]
def friends_count(a, b):
    cur.execute("SELECT COUNT(*) FROM friends WHERE (first_user_id=%s AND second_user_id=%s) OR (first_user_id=%s AND second_user_id=%s)", (a, b, b, a)); return cur.fetchone()[0]
def wipe(a, b):
    cur.execute("DELETE FROM friendRequests WHERE (user_id=%s AND friend_user_id=%s) OR (user_id=%s AND friend_user_id=%s)", (a, b, b, a))
    cur.execute("DELETE FROM friends WHERE (first_user_id=%s AND second_user_id=%s) OR (first_user_id=%s AND second_user_id=%s)", (a, b, b, a))

errors = []
def check(cond, label):
    if not cond: errors.append(label)

# ===================== FUNCTIONAL (sequential, online target) =====================
A, B, C = funcs
aid, bid, cid = idmap[A], idmap[B], idmap[C]
wipe(aid, bid); wipe(aid, cid); wipe(bid, cid)
skA, skB, skC = login_keep(A), login_keep(B), login_keep(C)

# self request
skA.clear(); skA.send("FRIENDREQUEST userName=%s" % A); skA.drain()
check(skA.saw("Can't send friend request to self"), "self-request not rejected")

# request to nonexistent
skA.clear(); skA.send("FRIENDREQUEST userName=nobody_%s_xyz" % TAG); skA.drain()
check(skA.saw("No such user"), "nonexistent-target not rejected")

# A -> B with msg; B should get notified, DB should hold the request
skA.clear(); skB.clear()
skA.send("FRIENDREQUEST userName=%s\tmsg=hello" % B); skA.drain(); skB.drain()
check(skB.saw("FRIENDREQUEST userName=%s" % A) and skB.saw("msg=hello"), "B did not receive friend request notification")
check(fr_count(aid, bid) == 1, "request A->B not stored (count=%d)" % fr_count(aid, bid))

# duplicate request -> silent, no second row
skA.clear(); skA.send("FRIENDREQUEST userName=%s\tmsg=again" % B); skA.drain()
check(fr_count(aid, bid) == 1, "duplicate request created extra row (count=%d)" % fr_count(aid, bid))

# B accepts -> both notified, friends row, request gone
skA.clear(); skB.clear()
skB.send("ACCEPTFRIENDREQUEST userName=%s" % A); skA.drain(); skB.drain()
check(skB.saw("FRIEND userName=%s" % A), "accepter B did not get FRIEND reply")
check(skA.saw("FRIEND userName=%s" % B), "requester A not notified of acceptance")
check(friends_count(aid, bid) == 1, "friendship not created (count=%d)" % friends_count(aid, bid))
check(fr_count(aid, bid) == 0, "request not removed after accept")

# already friends -> request rejected. NOTE: userdb.are_friends() is pre-existing-asymmetric
# (matches first_user_id==X OR second_user_id==Y, not the specific pair). The accept stored
# the row as (first=B, second=A), so the guard fires for B->A. This is faithful to master.
skB.clear(); skB.send("FRIENDREQUEST userName=%s" % A); skB.drain()
check(skB.saw("Already friends with user"), "already-friends not rejected")

# A unfriends B -> both notified, row gone
skA.clear(); skB.clear()
skA.send("UNFRIEND userName=%s" % B); skA.drain(); skB.drain()
check(skA.saw("UNFRIEND userName=%s" % B), "unfriender A did not get UNFRIEND reply")
check(skB.saw("UNFRIEND userName=%s" % A), "ex-friend B not notified of unfriend")
check(friends_count(aid, bid) == 0, "friendship not removed (count=%d)" % friends_count(aid, bid))

# accept with no request
skC.clear(); skC.send("ACCEPTFRIENDREQUEST userName=%s" % A); skC.drain()
check(skC.saw("No such friend request"), "accept-without-request not rejected")

# decline path: A -> C, C declines (no success reply), request gone
skA.clear(); skA.send("FRIENDREQUEST userName=%s" % C); skA.drain()
check(fr_count(aid, cid) == 1, "request A->C not stored")
skC.clear(); skC.send("DECLINEFRIENDREQUEST userName=%s" % A); skC.drain()
check(fr_count(aid, cid) == 0, "request not removed after decline")
# decline with no request -> rejected
skC.clear(); skC.send("DECLINEFRIENDREQUEST userName=%s" % A); skC.drain()
check(skC.saw("No such friend request"), "decline-without-request not rejected")

for sk in (skA, skB, skC): sk.close()
print("functional checks done (%d failures so far)" % len(errors))

# ===================== CONCURRENCY: independent accept pairs =====================
# seed one request per pair (requester -> target), then all targets accept concurrently.
for i in range(M):
    rid, tid = idmap[pair_req[i]], idmap[pair_tgt[i]]
    wipe(rid, tid)
    cur.execute("INSERT INTO friendRequests (user_id, friend_user_id, msg, time) VALUES (%s,%s,%s,%s)",
                (rid, tid, "m%d" % i, time.strftime("%Y-%m-%d %H:%M:%S")))
print("seeded %d independent requests" % M)

cerr = []
def accept_pair(i):
    tgt = pair_tgt[i]; req = pair_req[i]
    sk = login_keep(tgt)
    if sk is None: cerr.append("%s login failed" % tgt); return
    try:
        sk.send("ACCEPTFRIENDREQUEST userName=%s" % req); sk.drain(1.0)
        if not sk.saw("FRIEND userName=%s" % req):
            cerr.append("%s did not get FRIEND reply" % tgt)
    finally:
        sk.close()

t0 = time.time()
ts = [threading.Thread(target=accept_pair, args=(i,)) for i in range(M)]
for t in ts: t.start()
for t in ts: t.join()
dt = time.time() - t0
# verify every pair friended exactly once, every request removed
bad = 0
for i in range(M):
    rid, tid = idmap[pair_req[i]], idmap[pair_tgt[i]]
    if friends_count(rid, tid) != 1 or fr_count(rid, tid) != 0:
        bad += 1
        cerr.append("pair %d: friends=%d requests=%d" % (i, friends_count(rid, tid), fr_count(rid, tid)))
print("concurrent independent accepts: %d pairs in %.2fs, %d bad" % (M, dt, bad))
if cerr:
    print("FAIL concurrency:"); [print("  ", e) for e in cerr[:12]]
else:
    print("PASS: all independent pairs friended exactly once, requests cleared")

# ===================== SAME-PAIR DOUBLE-ACCEPT RACE =====================
R, T = race
rid, tid = idmap[R], idmap[T]
wipe(rid, tid)
cur.execute("INSERT INTO friendRequests (user_id, friend_user_id, msg, time) VALUES (%s,%s,%s,%s)",
            (rid, tid, "race", time.strftime("%Y-%m-%d %H:%M:%S")))
# T accepts the same request from two connections at once
def dbl():
    sk = login_keep(T)
    if sk is None: return
    sk.send("ACCEPTFRIENDREQUEST userName=%s" % R); sk.drain(1.0); sk.close()
ts = [threading.Thread(target=dbl) for _ in range(2)]
for t in ts: t.start()
for t in ts: t.join()
fc = friends_count(rid, tid)
print("same-pair double-accept: resulting friends rows = %d (1 ideal; >1 is the documented no-unique-constraint race)" % fc)

# ===================== DISCONNECT DURING CALL =====================
dropped = 0
for i in range(8):
    sk = login_keep(pair_tgt[i % M])
    if sk is None: continue
    sk.send("UNFRIEND userName=%s" % pair_req[i % M])
    sk.close()  # drop immediately
    dropped += 1
print("disconnect-during-call: issued %d UNFRIEND-then-drop (inspect server.log for tracebacks)" % dropped)

conn.close()
print("TOTAL functional failures: %d" % len(errors))
if errors or cerr:
    for e in errors: print("  FAIL:", e)
    sys.exit(1)
else:
    print("PASS: all functional friend-mutation checks correct")
