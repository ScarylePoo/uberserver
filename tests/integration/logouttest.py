"""
e2e test for PR B (OPTIMIZATIONS 3.1): logout end_session deferred off the reactor.
Needs the server running on :8200 against uberserver_test.

Functional: login a user (creates a login row with end=NULL), disconnect, then poll the
db until the latest login row's `end` is populated - proving the deferred do_end_session
ran via the reactor callback path and committed. Concurrency: M users log in and drop
concurrently; every one of them must get its latest login ended (distinct rows, no
deadlock/contention).

Run: source venv/bin/activate; python3 .logouttest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, threading, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "lo1"
M = 20
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

def connect():
    s = socket.create_connection((HOST, PORT)); recv_until(s, "\n"); return s

def register_and_confirm(user):
    s = connect()
    s.sendall(("REGISTER %s %s\n" % (user, PW)).encode()); recv_until(s, "\n")
    time.sleep(2.5)
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode()); recv_until(s, "AGREEMENTEND")
    s.sendall(b"CONFIRMAGREEMENT\n")
    ok = "LOGININFOEND" in recv_until(s, "LOGININFOEND"); s.close(); return ok

def login_socket(user):
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=8)
    if "ACCEPTED" not in resp or "LOGININFOEND" not in resp:
        s.close(); return None
    return s

users = ["lo_%s_%d" % (TAG, i) for i in range(M)]
funcuser = "lo_%s_func" % TAG
allusers = users + [funcuser]

results = {}
def _setup(u): results[u] = register_and_confirm(u)
ts = [threading.Thread(target=_setup, args=(u,)) for u in allusers]
for t in ts: t.start()
for t in ts: t.join()
if sum(1 for v in results.values() if v) != len(allusers):
    print("ABORT: not all users registered/confirmed"); sys.exit(1)
print("setup confirmed: %d users" % len(allusers))

conn = pymysql.connect(**DB); cur = conn.cursor()
fmt = ",".join(["%s"] * len(allusers))
cur.execute("SELECT id, username FROM users WHERE username IN (%s)" % fmt, allusers)
idmap = {name: uid for (uid, name) in cur.fetchall()}

def latest_login(uid):
    cur.execute("SELECT id, end FROM logins WHERE user_id=%s ORDER BY id DESC LIMIT 1", (uid,))
    return cur.fetchone()  # (login_id, end) or None

def last_login_ts(uid):
    cur.execute("SELECT last_login FROM users WHERE id=%s", (uid,))
    return cur.fetchone()[0]

def poll_ended(uid, login_id, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = latest_login(uid)
        if row and row[0] == login_id and row[1] is not None:
            return True
        time.sleep(0.1)
    return False

errors = []
def check(cond, label):
    if not cond: errors.append(label)

# ===================== FUNCTIONAL =====================
fid = idmap[funcuser]
s = login_socket(funcuser)
check(s is not None, "functional user failed to log in")
time.sleep(0.3)
row = latest_login(fid)
check(row is not None and row[1] is None, "expected an open login row (end=NULL) while connected, got %r" % (row,))
login_id = row[0]
before_ll = last_login_ts(fid)
s.close()  # triggers connectionLost -> deferred do_end_session
ended = poll_ended(fid, login_id, timeout=6.0)
check(ended, "login end not populated after disconnect (deferred end_session did not run/commit)")
after_ll = last_login_ts(fid)
check(after_ll is not None and (before_ll is None or after_ll >= before_ll), "last_login not refreshed on logout")
print("functional logout: ended=%s" % ended)

# ===================== CONCURRENT LOGOUTS =====================
socks = {}
def do_login(u):
    socks[u] = login_socket(u)
ts = [threading.Thread(target=do_login, args=(u,)) for u in users]
for t in ts: t.start()
for t in ts: t.join()
# record each user's open login id, then drop all sockets at once
open_ids = {}
time.sleep(0.4)
for u in users:
    row = latest_login(idmap[u])
    if row and row[1] is None:
        open_ids[u] = row[0]
check(len(open_ids) == M, "expected %d open logins before mass disconnect, got %d" % (M, len(open_ids)))

t0 = time.time()
for u, s in socks.items():
    if s is not None:
        try: s.close()
        except: pass
# poll all ended
bad = []
for u in users:
    if u not in open_ids:
        continue
    if not poll_ended(idmap[u], open_ids[u], timeout=8.0):
        bad.append(u)
dt = time.time() - t0
print("concurrent logouts: %d users, %.2fs, %d not-ended" % (M, dt, len(bad)))
if bad:
    for u in bad[:10]: errors.append("concurrent logout not ended: %s" % u)

conn.close()
if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: deferred logout end_session correct (functional + %d concurrent)" % M)
