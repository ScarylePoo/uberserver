"""
End-to-end tier: RELAYEDHOSTFAILED is written before the answer to the OPENBATTLE behind it.

A relay host sends RELAYEDHOST and OPENBATTLE back to back without waiting in between, so a
refusal and a battle answer are both in flight at once. A client cannot act on the refusal
until it knows whether the battle opened, which makes the order the two are written in a thing
it depends on. The coilbox side reads the refusal only after the battle answer and asked for
the ordering to be stated rather than assumed, on pull 36.

This pins it. relayedhosttest.py covers what RELAYEDHOST does. This covers when the answer
arrives relative to the battle, which that test cannot see because it reads between the sends.

Both lines go out in one write here, which is what a client that is not waiting actually does.

Run: activate the venv, then python3 tests/integration/relayedhostorderingtest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import HOST, PORT
import socket, ssl, hashlib, base64, time, sys

raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()
USER = "rhostorder"
RELAY_IP = "185.199.108.153"  # a real public address: the documentation ranges are refused

CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # hosting requires TLS, cert is self-signed
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

errors = []
def check(cond, label):
    if not cond: errors.append(label)


def connect():
    s = socket.create_connection((HOST, PORT))
    s.settimeout(2)
    try: s.recv(4096)          # greeting
    except OSError: pass
    s.sendall(b"STLS\n")
    time.sleep(0.4)
    try: s.recv(4096)          # OK
    except OSError: pass
    return CTX.wrap_socket(s)


def read_for(s, seconds, stop=None):
    """Drain for a fixed window, or until stop appears. Returns (text, chunk_count)."""
    buf, chunks = "", 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        s.settimeout(max(0.05, deadline - time.time()))
        try:
            got = s.recv(65536)
        except (socket.timeout, ssl.SSLWantReadError, OSError):
            break
        if not got:
            break
        buf += got.decode(errors="replace")
        chunks += 1
        if stop and stop in buf:
            break
    return buf, chunks


s = connect()
s.sendall(("REGISTER %s %s\n" % (USER, PW)).encode())
print("REGISTER ->", read_for(s, 1.0)[0].strip())
s.close()
print("waiting 3s for the delayed-registration window...")
time.sleep(3)

s = connect()
s.sendall(("LOGIN %s %s 0 * TestClient\t0\tu sp r\n" % (USER, PW)).encode())
login, _ = read_for(s, 10, stop="LOGININFOEND")
if "AGREEMENTEND" in login and "LOGININFOEND" not in login:
    s.sendall(b"CONFIRMAGREEMENT\n")
    more, _ = read_for(s, 10, stop="LOGININFOEND")
    login += more
check("LOGININFOEND" in login, "%s should log in, got:\n%s" % (USER, login))

# one write, both lines, which is what a client that does not wait in between sends
s.sendall(("RELAYEDHOST %s 30001\n" % RELAY_IP).encode()
          + b"OPENBATTLE 0 0 * 30001 8 4660 0 4660 spring\t105.0\tDeltaSiegeDry\tordering\tBA\n")
stream, chunks = read_for(s, 4, stop="REQUESTBATTLESTATUS")
s.close()

lines = [ln for ln in stream.splitlines() if ln.strip()]
print("received in %d chunk(s):" % chunks)
for ln in lines:
    print("   ", ln)


def index_of(prefix):
    for i, ln in enumerate(lines):
        if ln.startswith(prefix):
            return i
    return None


refusal = index_of("RELAYEDHOSTFAILED ")
check(refusal is not None,
      "an unconfigured server should refuse RELAYEDHOST, got %r" % (lines,))

# the battle answer is the ordinary pair, whichever way it went
answer = index_of("OPENBATTLE ")
if answer is None:
    answer = index_of("OPENBATTLEFAILED ")
check(answer is not None,
      "the OPENBATTLE behind it should still be answered, got %r" % (lines,))

if refusal is not None and answer is not None:
    check(refusal < answer,
          "RELAYEDHOSTFAILED should be written before the OPENBATTLE answer, got %r" % (lines,))

# BATTLEOPENED is the line a client watches for, and it comes after the refusal too
opened = index_of("BATTLEOPENED ")
if refusal is not None and opened is not None:
    check(refusal < opened,
          "RELAYEDHOSTFAILED should be written before BATTLEOPENED, got %r" % (lines,))

# and the refused address is not the one the battle was advertised at
if opened is not None:
    check(RELAY_IP not in lines[opened],
          "a refused address must not reach the battle, got %r" % (lines[opened],))

if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
