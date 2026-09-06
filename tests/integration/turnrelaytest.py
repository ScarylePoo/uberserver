"""
End-to-end tier: relay hosting against a server with no TURN relay configured.

The shared test server boots without a server_turn.txt, which is the state every existing
deployment is in. It must therefore leave 'r' out of COMPFLAGS, still accept 'r' from a
client that sends it anyway, and answer TURNCREDENTIALS with a readable refusal rather than
a compatibility complaint or a dropped line.

The configured half of this is covered in-process by tests/worker/turnrelayworkertest.py,
which needs no server and cannot be disturbed by whatever config the operator has left in
the repository root.
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import HOST, PORT
import socket, hashlib, base64, time, sys

user = "relaynoturn"
raw_pw = "secretpw"
pw = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()

errors = []
def check(cond, label):
    if not cond: errors.append(label)

def recv_until(s, substr, timeout=5):
    s.settimeout(timeout)
    buf = b""
    try:
        while substr.encode() not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    return buf.decode(errors="replace")

s = socket.create_connection((HOST, PORT))
recv_until(s, "\n")

s.sendall(("REGISTER %s %s\n" % (user, pw)).encode())
print("REGISTER ->", recv_until(s, "\n").strip())

print("waiting 3s for the delayed-registration window...")
time.sleep(3)

# log in advertising 'r' alongside the mandatory flags. A server with no relay must not
# complain about it: the flag is known everywhere, it is only advertised conditionally.
s.sendall(("LOGIN %s %s 0 * TestClient\t0\tu sp r\n" % (user, pw)).encode())
login = recv_until(s, "AGREEMENTEND")
if "AGREEMENTEND" in login:
    s.sendall(b"CONFIRMAGREEMENT\n")
    login += recv_until(s, "LOGININFOEND")
check("LOGININFOEND" in login, "login should complete, got:\n%s" % login)
check("compatibility errors" not in login,
      "sending 'r' to a relay-less server must not raise a compatibility warning, got:\n%s" % login)

s.sendall(b"LISTCOMPFLAGS\n")
compflags = ""
for line in recv_until(s, "COMPFLAGS").splitlines():
    if line.startswith("COMPFLAGS"):
        compflags = line
print("LISTCOMPFLAGS ->", compflags)
flags = compflags.split()[1:]
check(compflags != "", "the server should answer LISTCOMPFLAGS")
check("r" not in flags, "'r' must not be advertised with no relay configured, got %r" % (flags,))
check("u" in flags and "sp" in flags, "the other flags should still be advertised, got %r" % (flags,))

s.sendall(b"TURNCREDENTIALS\n")
reply = ""
for line in recv_until(s, "TURNCREDENTIALS").splitlines():
    if line.startswith("TURNCREDENTIALS"):
        reply = line
print("TURNCREDENTIALS ->", reply)
check(reply.startswith("TURNCREDENTIALSFAILED "),
      "a relay-less server should refuse cleanly, got %r" % (reply,))
check(len(reply.split(" ", 1)[1].strip()) > 0 if " " in reply else False,
      "the refusal should carry a reason, got %r" % (reply,))
check("failed. Unknown command" not in reply and "Insufficient rights" not in reply,
      "TURNCREDENTIALS should be a known command available to a logged-in user, got %r" % (reply,))

s.close()
if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
