"""
Disconnect-mid-CHANGEEMAIL probe (OPTIMIZATIONS 3.1): drives the do_change_email vs
do_end_session race on the same users row by issuing CHANGEEMAIL then dropping the socket
immediately. Run repeatedly; then grep server.log for 1020 / "CHANGEEMAIL DB error" /
"end_session persist failed". Used as a positive control: with the 1020 retry removed (and a
forced overlap window) these lines appear; with the retry in place they vanish.

Run (server up): source venv/bin/activate; python3 .changeemaildropprobe.py [tag] [n]
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import HOST, PORT
import socket, time, sys, hashlib, base64
TAG = sys.argv[1] if len(sys.argv) > 1 else "p1"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 20
def enc(raw): return base64.b64encode(hashlib.md5(raw.encode()).digest()).decode()
PW = enc("secretpw")

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

users = ["cdrop_%s_%d" % (TAG, i) for i in range(N)]
for u in users:
    if not register_and_confirm(u):
        print("ABORT: setup failed for %s" % u); sys.exit(1)
print("setup: %d users" % N)

issued = 0
for i, u in enumerate(users):
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (u, PW)).encode())
    if "LOGININFOEND" not in recv_until(s, "LOGININFOEND", timeout=8):
        s.close(); continue
    s.sendall(("CHANGEEMAIL cdrop_%s_%d_new@phase3.test\n" % (TAG, i)).encode())
    s.close()  # drop immediately, before the deferred write's callback fires
    issued += 1
print("issued %d CHANGEEMAIL-then-drop cycles" % issued)
time.sleep(2)  # let the deferred workers + retries settle
print("done; grep server.log for 1020 / DB-error / persist-failed")
