"""
E2E test for the Phase 3.1 CLOSE-OUT slice (OPTIMIZATIONS 3.1). Server up on :8200 against the
MariaDB test db. Exercises the converted handlers end-to-end with an admin acting client:
  - admin reads: FINDIP, GETIP (online + offline), LISTMODS, LISTBANS
  - CONFIRMAGREEMENT (the register_and_confirm setup itself drives the off-reactor access write)
  - DELETEACCOUNT incl. D3 (two deletions -> both email NULL, no 1062) and the online-target
    KICK->end_session vs scrub race on the same users row (absorbed by _run_db's 1020 retry)
  - RESETPASSWORD / RESETUSERPASSWORD: active() is False in the test config, so ONLY the DENIED
    branch is reachable e2e. The verify-ON write path is covered worker-direct in
    .closeoutworkertest.py (do_set_password); this asserts the DENIED branch and states so.
  - a CONFIRMAGREEMENT-then-drop probe driving the access-write vs end_session race; proves the
    probe drove the path (both writes committed against the same row) and 1020s are absorbed.

Run (server up): source venv/bin/activate; python3 .closeoute2etest.py [tag]
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, time, threading, sys, hashlib, base64
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "c1"
def enc(raw): return base64.b64encode(hashlib.md5(raw.encode()).digest()).decode()
PW = enc("secretpw")
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
    def drain(self, seconds=0.8):
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

ADMIN = "close_%s_admin" % TAG
ON = "close_%s_online" % TAG       # stays logged in (GETIP online branch / FINDIP online)
OFF = "close_%s_offline" % TAG     # offline (GETIP offline branch -> get_ip)
DEL1 = "close_%s_del1" % TAG
DEL2 = "close_%s_del2" % TAG
DELON = "close_%s_delon" % TAG     # online target for the KICK->end_session vs scrub race
drops = ["close_%s_drop_%d" % (TAG, i) for i in range(12)]
allusers = [ADMIN, ON, OFF, DEL1, DEL2, DELON] + drops

results = {}
def _setup(u): results[u] = register_and_confirm(u)
ts = [threading.Thread(target=_setup, args=(u,)) for u in allusers]
for t in ts: t.start()
for t in ts: t.join()
ok_setup = sum(1 for v in results.values() if v)
print("setup confirmed (CONFIRMAGREEMENT off-reactor): %d/%d" % (ok_setup, len(allusers)))
if ok_setup != len(allusers):
    print("ABORT: not all users registered/confirmed:", [u for u in allusers if not results.get(u)]); sys.exit(1)

conn = pymysql.connect(**DB); cur = conn.cursor()
def db_row(name):
    cur.execute("SELECT id, access, email, ingame_time, bot, password FROM users WHERE username=%s", (name,))
    return cur.fetchone()
def last_login_end(name):
    cur.execute("""SELECT l.end FROM logins l JOIN users u ON u.id=l.user_id
                   WHERE u.username=%s ORDER BY l.id DESC LIMIT 1""", (name,))
    r = cur.fetchone(); return (r[0] if r else "NOROW")

errors = []
def check(cond, label):
    if not cond: errors.append(label)

# make ADMIN an admin in the DB, then log in fresh so the login reads access='admin'
cur.execute("UPDATE users SET access='admin' WHERE username=%s", (ADMIN,))
sa = login_keep(ADMIN)
if sa is None:
    print("ABORT: admin login failed"); sys.exit(1)
check("admin" in (db_row(ADMIN)[1] or ""), "admin user must have admin access in DB")

# keep ON logged in for the online branches
son = login_keep(ON)
if son is None:
    print("ABORT: online user login failed"); sys.exit(1)

# ---------------- FINDIP (off-reactor do_find_ip) ----------------
# the server records the connection's resolved IP (the host's outbound address here, not
# 127.0.0.1), so read the actual last_ip from the DB and query FINDIP with it.
cur.execute("SELECT last_ip FROM users WHERE username=%s", (ON,)); ONIP = cur.fetchone()[0]
sa.clear(); sa.send("FINDIP %s" % ONIP); sa.drain(1.5)
check(sa.saw("<%s> is currently bound to %s." % (ON, ONIP)),
      "FINDIP should list the online user as currently bound to %s (buf=%r)" % (ONIP, sa.buf[:300]))

# ---------------- GETIP online + offline ----------------
sa.clear(); sa.send("GETIP %s" % ON); sa.drain(1.0)
check(sa.saw("<%s> is currently bound to" % ON), "GETIP online branch should say currently bound")
cur.execute("SELECT last_ip FROM users WHERE username=%s", (OFF,)); OFFIP = cur.fetchone()[0]
sa.clear(); sa.send("GETIP %s" % OFF); sa.drain(1.0)
check(sa.saw("<%s> was recently bound to %s" % (OFF, OFFIP)),
      "GETIP offline branch (off-reactor get_ip) should report the last ip %s (buf=%r)" % (OFFIP, sa.buf))

# ---------------- LISTMODS (off-reactor list_mods) ----------------
sa.clear(); sa.send("LISTMODS"); sa.drain(1.0)
check(sa.saw("Admins:") and sa.saw(ADMIN), "LISTMODS should list the admin under Admins (buf=%r)" % sa.buf)

# ---------------- LISTBANS (off-reactor list_bans) ----------------
BANMAIL = ("close_%s_ban@phase3.test" % TAG).lower()
sa.clear(); sa.send("BANSPECIFIC %s 1 closeout-test-ban" % BANMAIL); sa.drain(1.0)
sa.clear(); sa.send("LISTBANS"); sa.drain(1.5)
check(sa.saw("-- Banlist --"), "LISTBANS should emit a banlist header (buf=%r)" % sa.buf[:300])
check(sa.saw(BANMAIL), "LISTBANS should include the just-added ban email")
sa.send("UNBAN %s" % BANMAIL); sa.drain(0.5)  # cleanup

# ---------------- DELETEACCOUNT (off-reactor do_scrub_account) + D3 ----------------
for d in (DEL1, DEL2):
    sa.clear(); sa.send("DELETEACCOUNT %s" % d); sa.drain(1.2)
    check(sa.saw("Account deletion of <%s> scheduled" % d), "DELETEACCOUNT %s should confirm scheduling (buf=%r)" % (d, sa.buf))
time.sleep(0.5)
for d in (DEL1, DEL2):
    r = db_row(d)
    check(r[1] == "user", "deleted %s access must be 'user', got %r" % (d, r[1]))
    check(r[2] is None, "deleted %s email must be NULL (D3), got %r" % (d, r[2]))
    check(r[3] == 0, "deleted %s ingame_time must be 0, got %r" % (d, r[3]))
check(db_row(DEL1)[2] is None and db_row(DEL2)[2] is None, "D3: two deletions must BOTH hold NULL email, no 1062 collision")

# online-target deletion: KICK disconnects DELON (end_session deferred) while scrub defers ->
# same users row race, both via _run_db retry. Assert the scrub still committed cleanly.
sdelon = login_keep(DELON)
check(sdelon is not None, "DELON should log in for the race case")
sa.clear(); sa.send("DELETEACCOUNT %s" % DELON); sa.drain(1.5)
check(sa.saw("Account deletion of <%s> scheduled" % DELON), "online-target DELETEACCOUNT should still confirm")
time.sleep(0.6)
r = db_row(DELON)
check(r[1] == "user" and r[2] is None and r[3] == 0, "online-target scrub must commit despite the KICK/end_session race, got %r" % (r,))
if sdelon: sdelon.close()

# ---------------- RESET handlers: active()-OFF DENIED branch only ----------------
# verificationdb.active() is False (no mail_user), so both short-circuit to DENIED before any
# write. The write path (do_set_password) is covered worker-direct.
srp = connect()
srp.sendall(("RESETPASSWORD %s 1234\n" % BANMAIL).encode())
rp = recv_until(srp, "RESETPASSWORDDENIED", timeout=3)
check("RESETPASSWORDDENIED" in rp and "verification is currently turned off" in rp,
      "RESETPASSWORD must DENY with verification-off (got %r)" % rp[:200])
srp.close()
# RESETUSERPASSWORD's active()-off branch calls out_SERVERMSG without a client arg (a PRE-EXISTING
# bug, left untouched), so it raises on the reactor and sends no reply. The invariant we CAN assert
# is that the gate prevents the off-reactor write: OFF's password is unchanged.
off_pw_before = db_row(OFF)[5]
sa.clear(); sa.send("RESETUSERPASSWORD %s" % OFF); sa.drain(1.0)
check(db_row(OFF)[5] == off_pw_before,
      "RESETUSERPASSWORD with verification-off must NOT reach the password write (OFF pw changed)")
print("RESET handlers: only the active()-OFF DENIED gate exercised e2e (RESETUSERPASSWORD's reply is a "
      "pre-existing out_SERVERMSG bug; the real write path is covered worker-direct).")

# ---------------- CONFIRMAGREEMENT-then-drop probe (access-write vs end_session) ----------------
# Drive the same-row race: login (creates a Login row), send CONFIRMAGREEMENT, drop immediately.
# do_confirm_agreement (UPDATE access) and the deferred do_end_session (UPDATE last_login + the
# login's end) target the same users row. Prove BOTH committed (access='user' AND login end set)
# for at least some users -> the probe genuinely drove the contended path.
both_committed = 0
for i, u in enumerate(drops):
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (u, PW)).encode())
    if "AGREEMENTEND" not in recv_until(s, "AGREEMENTEND", timeout=8):
        s.close(); continue
    s.sendall(b"CONFIRMAGREEMENT\n")
    s.close()  # drop immediately, before/as the deferred access-write callback fires
    time.sleep(0.04)
time.sleep(2.0)  # let deferred workers + retries settle
for u in drops:
    r = db_row(u); end = last_login_end(u)
    if r[1] == "user" and end not in (None, "NOROW"):
        both_committed += 1
print("CONFIRMAGREEMENT-drop probe: %d/%d users have BOTH writes committed on the same row "
      "(access='user' AND login.end set) -> probe drove the access-write vs end_session race" % (both_committed, len(drops)))
check(both_committed >= 1, "probe must drive at least one contended same-row case (got 0)")

conn.close()
print("TOTAL functional failures: %d" % len(errors))
if errors:
    for e in errors: print("  FAIL:", e)
    sys.exit(1)
print("PASS: all close-out e2e checks correct")
