"""
End-to-end tier: moderator lookups answer when there is nothing to report (issue #61).

GETIP, FINDIP, SETBOTMODE and GETUSERINFO each sent nothing in some cases, so a client could
not tell "nothing found" from a lost reply and had to wait out its own timeout.

  GETIP        a user that does not exist, and an offline user with no stored IP
  FINDIP       an address no account has used, and a closing line after a non-empty list
  SETBOTMODE   a user that does not exist
  GETUSERINFO  another user's details, asked for by an account below mod

Run: activate the venv, then python3 tests/integration/emptylookuptest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "e1"
PW = base64.b64encode(hashlib.md5(b"secretpw").digest()).decode()
MOD = "elk_%s_mod" % TAG
PLAIN = "elk_%s_user" % TAG
NOIP = "elk_%s_noip" % TAG
MISSING = "elk_%s_nobody" % TAG
UNUSED_IP = "203.0.113.77"  # TEST-NET-3, never a real client address

errors = []
def check(cond, label):
    if not cond: errors.append(label)

def recv_until(s, substr, timeout=8):
    s.settimeout(timeout); buf = b""
    try:
        while substr.encode() not in buf:
            chunk = s.recv(8192)
            if not chunk: break
            buf += chunk
    except socket.timeout: pass
    return buf.decode(errors="replace")

def read_for(s, seconds):
    s.settimeout(seconds); buf = b""
    try:
        while True:
            chunk = s.recv(8192)
            if not chunk: break
            buf += chunk
    except socket.timeout: pass
    return [ln for ln in buf.decode(errors="replace").splitlines() if ln.strip()]

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
    resp = recv_until(s, "LOGININFOEND")
    if "ACCEPTED" not in resp:
        s.close(); return None
    read_for(s, 0.5)  # drain the rest of the login burst
    return s

def ask(s, line, seconds=1.5):
    s.sendall((line + "\n").encode())
    got = read_for(s, seconds)
    print("%-40s -> %r" % (line, got))
    return got


for u in (MOD, PLAIN, NOIP):
    if not register_and_confirm(u):
        print("ABORT: could not register %s" % u); sys.exit(1)

conn = pymysql.connect(**DB_KWARGS); cur = conn.cursor()
cur.execute("UPDATE users SET access='mod' WHERE username=%s", (MOD,))
cur.execute("UPDATE users SET last_ip=NULL WHERE username=%s", (NOIP,))
cur.execute("SELECT last_ip FROM users WHERE username=%s", (PLAIN,)); PLAIN_IP = cur.fetchone()[0]
conn.close()

mod = login_keep(MOD)
plain = login_keep(PLAIN)
if not (mod and plain):
    print("ABORT: test users could not log in"); sys.exit(1)
read_for(mod, 0.5)  # the ADDUSER for PLAIN, who logged in after MOD

# GETIP
got = ask(mod, "GETIP %s" % MISSING)
check(got == ["SERVERMSG User <%s> does not exist" % MISSING],
      "GETIP for a user that does not exist should say so, got %r" % got)

got = ask(mod, "GETIP %s" % NOIP)
check(got == ["SERVERMSG No IP address is known for <%s>" % NOIP],
      "GETIP for an offline user with no stored IP should say so, got %r" % got)

# FINDIP
got = ask(mod, "FINDIP %s" % UNUSED_IP)
check(got == ["SERVERMSG No accounts found for %s" % UNUSED_IP],
      "FINDIP for an unused address should say no accounts were found, got %r" % got)

got = ask(mod, "FINDIP %s" % PLAIN_IP)
check(any(("<%s> is currently bound to %s." % (PLAIN, PLAIN_IP)) in ln for ln in got),
      "FINDIP should still list %s on %s, got %r" % (PLAIN, PLAIN_IP, got))
check(bool(got) and got[-1] == "SERVERMSG -- End of accounts found for %s --" % PLAIN_IP,
      "FINDIP should close a non-empty list with an end line, got %r" % got)

# SETBOTMODE
got = ask(mod, "SETBOTMODE %s 1" % MISSING)
check(got == ["SERVERMSG User <%s> does not exist" % MISSING],
      "SETBOTMODE for a user that does not exist should say so, got %r" % got)

# GETUSERINFO below mod
got = ask(plain, "GETUSERINFO %s" % MOD)
check(got == ["SERVERMSG GETUSERINFO failed. Insufficient rights.",
              "FAILED msg=Insufficient rights.\tcmd=GETUSERINFO"],
      "GETUSERINFO about another user from a non-mod should be refused, got %r" % got)

# negative controls: the answers that already worked are unchanged
got = ask(plain, "GETUSERINFO")
check(any(ln.startswith("SERVERMSG Registration date: ") for ln in got),
      "GETUSERINFO with no name should still show the caller's own details, got %r" % got)
got = ask(mod, "GETIP %s" % PLAIN)
check(got == ["SERVERMSG <%s> is currently bound to %s" % (PLAIN, PLAIN_IP)],
      "GETIP for an online user should still report their address, got %r" % got)

mod.close(); plain.close()


if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
