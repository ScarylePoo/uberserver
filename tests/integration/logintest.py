import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import HOST, PORT
import socket, hashlib, base64, time, sys

user = "phase3userB"
raw_pw = "secretpw"
pw = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()

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
greet = recv_until(s, "\n")
print("GREETING:", greet.strip())

s.sendall(("REGISTER %s %s\n" % (user, pw)).encode())
reg = recv_until(s, "\n")
print("REGISTER ->", reg.strip())

print("waiting 3s for delayed-registration window...")
time.sleep(3)

s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, pw)).encode())
resp = recv_until(s, "AGREEMENTEND")
print("LOGIN -> (first lines)")
for line in resp.splitlines()[:4]:
    print("   ", line)
if "AGREEMENTEND" in resp:
    print("   ...AGREEMENTEND received")

s.sendall(b"CONFIRMAGREEMENT\n")
acc = recv_until(s, "LOGININFOEND")
got_accepted = "ACCEPTED" in acc
got_end = "LOGININFOEND" in acc
print("CONFIRMAGREEMENT -> ACCEPTED=%s LOGININFOEND=%s" % (got_accepted, got_end))
ok = got_accepted and got_end
print("RESULT:", "PASS" if ok else "FAIL")
s.close()
sys.exit(0 if ok else 1)
