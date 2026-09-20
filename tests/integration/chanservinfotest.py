"""
End-to-end tier: ChanServ :info closes the operator list once (issue #59).

The closing bracket was added inside the loop over operators, so two operators read
'[bob] carol]'. This registers a channel, then asks :info with no operators, one, and two.

Run: activate the venv, then python3 tests/integration/chanservinfotest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, re, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "c1"
PW = base64.b64encode(hashlib.md5(b"secretpw").digest()).decode()
MOD = "csi_%s_mod" % TAG
OP1 = "csi_%s_opa" % TAG
OP2 = "csi_%s_opb" % TAG
CHAN = "csinfo_%s" % TAG

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

def chanserv(s, text, expect):
    """PM ChanServ and hand back its reply line that contains expect."""
    s.sendall(("SAYPRIVATE ChanServ %s\n" % text).encode())
    got = recv_until(s, expect)
    for line in got.splitlines():
        if line.startswith("SAIDPRIVATE ChanServ ") and expect in line:
            return line
    return None

def operator_list(s):
    line = chanserv(s, ":info %s" % CHAN, " info: ")
    m = re.search(r"Operator list is (.*?)\. Currently contains", line or "")
    return m.group(1) if m else None


for u in (MOD, OP1, OP2):
    if not register_and_confirm(u):
        print("ABORT: could not register %s" % u); sys.exit(1)

conn = pymysql.connect(**DB_KWARGS); cur = conn.cursor()
cur.execute("UPDATE users SET access='mod' WHERE username=%s", (MOD,))
conn.close()

mod = login_keep(MOD)
if not mod:
    print("ABORT: moderator could not log in"); sys.exit(1)
mod.sendall(("JOIN %s\n" % CHAN).encode()); recv_until(mod, "JOIN %s" % CHAN)
check(chanserv(mod, ":register %s" % CHAN, "Successfully registered") is not None,
      "ChanServ should register %s" % CHAN)

got = operator_list(mod)
print("no operators  -> %r" % got)
check(got == "empty", "no operators should read 'empty', got %r" % got)

check(chanserv(mod, ":op %s %s" % (CHAN, OP1), "added <%s>" % OP1) is not None, "ChanServ should op %s" % OP1)
got = operator_list(mod)
print("one operator  -> %r" % got)
check(got == "[%s]" % OP1, "one operator should read [%s], got %r" % (OP1, got))

check(chanserv(mod, ":op %s %s" % (CHAN, OP2), "added <%s>" % OP2) is not None, "ChanServ should op %s" % OP2)
got = operator_list(mod)
print("two operators -> %r" % got)
names = sorted([OP1, OP2])
check(got in ("[%s %s]" % tuple(names), "[%s %s]" % tuple(reversed(names))),
      "two operators should read [%s %s] in either order, got %r" % (names[0], names[1], got))
mod.close()


if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
