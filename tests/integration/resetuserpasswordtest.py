"""
End-to-end tier: RESETUSERPASSWORD answers a moderator when email is switched off (issue #58).

The test server has no server_email_account.txt, so account recovery is disabled. The handler
used to call out_SERVERMSG without the client, which raised a TypeError and sent nothing, so the
moderator never learnt why the command did nothing.

Run: activate the venv, then python3 tests/integration/resetuserpasswordtest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "r1"
PW = base64.b64encode(hashlib.md5(b"secretpw").digest()).decode()
MOD = "rup_%s_mod" % TAG
TARGET = "rup_%s_target" % TAG

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

def login_keep(user):
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    resp = recv_until(s, "LOGININFOEND")
    if "ACCEPTED" not in resp:
        s.close(); return None
    return s


if not (register_and_confirm(MOD) and register_and_confirm(TARGET)):
    print("ABORT: could not register the test users"); sys.exit(1)

conn = pymysql.connect(**DB_KWARGS); cur = conn.cursor()
cur.execute("UPDATE users SET access='mod' WHERE username=%s", (MOD,))
conn.close()

mod = login_keep(MOD)
if not mod:
    print("ABORT: moderator could not log in"); sys.exit(1)

mod.sendall(("RESETUSERPASSWORD %s\n" % TARGET).encode())
got = read_for(mod, 1.5)
print("RESETUSERPASSWORD -> %r" % got.splitlines())
check("SERVERMSG Email verification is currently turned off, account recovery is disabled" in got,
      "RESETUSERPASSWORD with email off should tell the moderator why, got %r" % got)

# the connection must still work, which it would not if the handler had raised mid-read
mod.sendall(b"PING\n")
check("PONG" in read_for(mod, 1.0), "the moderator's connection should still answer PING")
mod.close()


if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
