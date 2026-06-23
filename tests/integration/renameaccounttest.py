"""
E2E test for RENAMEACCOUNT (OPTIMIZATIONS 3.1): the off-reactor do_rename_account worker wired
through in_RENAMEACCOUNT + _renameaccount_done/_failed. Requires the server up on :8200 against
the MariaDB test db. Asserts protocol replies + DB username column + Rename record, plus the
concurrent rename-to-taken race (exactly one winner) and a disconnect-mid-rename 1020 probe
(must show 0 NEW unhandled 1020s thanks to the deferred end_session this branch is stacked on).

Run (server already up): source venv/bin/activate; python3 .renameaccounttest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, threading, sys, os
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "r1"
def enc(raw): return base64.b64encode(hashlib.md5(raw.encode()).digest()).decode()
PW = enc("secretpw")
DB = DB_KWARGS
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")

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
    def drain(self, seconds=0.8):
        self.s.settimeout(seconds)
        try:
            while True:
                chunk = self.s.recv(8192)
                if not chunk: break
                self.buf += chunk.decode(errors="replace")
        except socket.timeout: pass
        except OSError: pass  # server disconnected us (e.g. Remove('renaming')); keep what we got
    def saw(self, substr): return substr in self.buf
    def clear(self): self.buf = ""
    def close(self):
        try: self.s.close()
        except: pass

def connect():
    s = socket.create_connection((HOST, PORT)); recv_until(s, "\n"); return s

def register_and_confirm(user, pw=PW):
    s = connect()
    s.sendall(("REGISTER %s %s\n" % (user, pw)).encode()); recv_until(s, "\n")
    time.sleep(2.5)
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, pw)).encode()); recv_until(s, "AGREEMENTEND")
    s.sendall(b"CONFIRMAGREEMENT\n")
    ok = "LOGININFOEND" in recv_until(s, "LOGININFOEND"); s.close(); return ok

def login_keep(user, pw=PW):
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, pw)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=8)
    if "ACCEPTED" not in resp or "LOGININFOEND" not in resp:
        s.close(); return None
    return Sock(s)

conn = pymysql.connect(**DB); cur = conn.cursor()
def db_has(name):
    cur.execute("SELECT id FROM users WHERE username=%s", (name,)); return cur.fetchone()
def rename_count(uid, original):
    cur.execute("SELECT COUNT(*) FROM renames WHERE user_id=%s AND original=%s", (uid, original))
    return cur.fetchone()[0]
def wipe(*names):
    for n in names:
        cur.execute("SELECT id FROM users WHERE username=%s", (n,))
        row = cur.fetchone()
        if row:
            cur.execute("DELETE FROM renames WHERE user_id=%s", (row[0],))
            cur.execute("DELETE FROM logins WHERE user_id=%s", (row[0],))
            cur.execute("DELETE FROM users WHERE id=%s", (row[0],))

errors = []
def check(cond, label):
    if not cond: errors.append(label)

# ---------- users ----------
F  = "ren_%s_F" % TAG          # functional rename source
FN = "ren_%s_Fn" % TAG         # functional rename target
T  = "ren_%s_T" % TAG          # an already-taken name
X  = "ren_%s_X" % TAG          # race contender 1
Y  = "ren_%s_Y" % TAG          # race contender 2
SH = "ren_%s_SH" % TAG         # shared race target
allnames = [F, FN, T, X, Y, SH]
wipe(*allnames)

results = {}
def _setup(u): results[u] = register_and_confirm(u)
ts = [threading.Thread(target=_setup, args=(u,)) for u in (F, T, X, Y)]
for t in ts: t.start()
for t in ts: t.join()
if sum(1 for v in results.values() if v) != 4:
    print("ABORT: not all users registered: %r" % results); sys.exit(1)
print("setup confirmed: %d/4" % sum(1 for v in results.values() if v))

# ===================== FUNCTIONAL =====================
# rename to self -> reactor pre-check denial, DB unchanged
sk = login_keep(F)
if sk is None: print("ABORT: functional login failed"); sys.exit(1)
sk.clear(); sk.send("RENAMEACCOUNT %s" % F); sk.drain()
check(sk.saw("Failed to rename to <%s>: You already have that username." % F), "rename-to-self message wrong: %r" % sk.buf)
check(db_has(F) is not None, "rename-to-self must leave the user intact")

# rename to a TAKEN name -> denial, DB unchanged
sk.clear(); sk.send("RENAMEACCOUNT %s" % T); sk.drain()
check(sk.saw("Failed to rename to <%s>: Username already exists." % T), "rename-to-taken message wrong: %r" % sk.buf)
check(db_has(F) is not None and db_has(T) is not None, "rename-to-taken must leave both users intact")

# valid rename -> DB renamed + Rename record + old name freed; server disconnects us.
# NOTE: the success SERVERMSG races Remove()'s abortConnection (immediate RST, pre-existing), so
# its delivery is best-effort; the DB is the authoritative success signal and is what we assert.
uid_before = db_has(F)[0]
sk.clear(); sk.send("RENAMEACCOUNT %s" % FN); sk.drain(1.2)
sk.close(); time.sleep(0.3)
check(db_has(FN) is not None, "valid rename did not persist the new username")
check(db_has(F) is None, "old username should be freed after rename")
check(db_has(FN)[0] == uid_before, "rename should keep the same user id")
check(rename_count(uid_before, F) == 1, "a Rename(original=%s) should be recorded" % F)
print("functional checks done (%d failures so far)" % len(errors))

# ===================== CONCURRENCY: rename-to-taken race =====================
# X and Y both try to rename to the SAME free name SH simultaneously. Exactly one must win
# (renamed + disconnected), the other must be denied "Username already exists." - and the DB
# must end with exactly ONE user named SH. This exercises the residual IntegrityError(1062) net
# (both pass the early query, one flush wins, the loser's UNIQUE violation is caught -> denied).
uid_X, uid_Y = db_has(X)[0], db_has(Y)[0]
race = {}
def race_one(u):
    s = login_keep(u)
    if s is None: race[u] = "loginfail"; return
    s.send("RENAMEACCOUNT %s" % SH); s.drain(1.5)
    # the denial message is reliable (denied path does not disconnect); the winner is disconnected
    # by abortConnection so its success message is best-effort - we identify the winner via the DB.
    race[u] = "denied" if s.saw("Username already exists.") else "other"
    s.close()
ts = [threading.Thread(target=race_one, args=(u,)) for u in (X, Y)]
for t in ts: t.start()
for t in ts: t.join()
time.sleep(0.3)
def name_of(uid):
    cur.execute("SELECT username FROM users WHERE id=%s", (uid,)); r = cur.fetchone(); return r[0] if r else None
winners = [uid for uid in (uid_X, uid_Y) if name_of(uid) == SH]
cur.execute("SELECT COUNT(*) FROM users WHERE username=%s", (SH,)); sh_count = cur.fetchone()[0]
denied = [u for u, v in race.items() if v == "denied"]
check(len(winners) == 1, "race: exactly one user row must end named %s, winners=%r race=%r" % (SH, winners, race))
check(sh_count == 1, "race: exactly one user must hold %s, db count=%d" % (SH, sh_count))
check(len(denied) == 1, "race: exactly one client must see the 'already exists' denial, got %r" % (race,))
print("rename-to-taken race: winner_uid=%r denied=%r sh_count=%d" % (winners, denied, sh_count))

# ===================== DISCONNECT-MID-RENAME 1020 PROBE =====================
# A fully-logged-in client's off-reactor users-row write races the (now deferred) end_session on
# disconnect. Count NEW '1020' / 'Unhandled Error' lines in server.log across a burst of
# rename-then-immediate-drop. Stacked on #16 (deferred end_session w/ retry) -> must be 0.
def log_size():
    try: return os.path.getsize(LOG)
    except OSError: return 0
def log_tail(since):
    with open(LOG, "r", errors="replace") as f:
        f.seek(since); return f.read()
probe_users = []
for i in range(10):
    u = "ren_%s_p%d" % (TAG, i); probe_users.append(u)
before = log_size()
def probe_one(u):
    if not register_and_confirm(u): return
    s = login_keep(u)
    if s is None: return
    s.send("RENAMEACCOUNT %sx" % u)   # +x: a fresh, free target name
    s.close()                          # drop immediately, before the deferred write resolves
pt = [threading.Thread(target=probe_one, args=(u,)) for u in probe_users]
for t in pt: t.start()
for t in pt: t.join()
time.sleep(1.5)  # let deferred writes + end_session settle
delta = log_tail(before)
n_1020 = delta.count("1020")
n_unhandled = delta.count("Unhandled Error")
print("disconnect-mid-rename probe: %d renames-then-drop; NEW 1020=%d, Unhandled=%d" % (len(probe_users), n_1020, n_unhandled))
check(n_1020 == 0, "disconnect-mid-rename produced %d NEW '1020' lines (end_session race not absorbed)" % n_1020)
check(n_unhandled == 0, "disconnect-mid-rename produced %d NEW 'Unhandled Error' lines" % n_unhandled)

# cleanup
wipe(F, FN, T, X, Y, SH, *[u + "x" for u in probe_users], *probe_users)
conn.close()

print("TOTAL failures: %d" % len(errors))
if errors:
    for e in errors: print("  FAIL:", e)
    sys.exit(1)
print("PASS: all RENAMEACCOUNT e2e checks correct")
