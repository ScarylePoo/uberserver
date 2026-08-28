"""
End-to-end tier: joiner addresses (issue #28) against a server with no TURN relay configured.

The shared test server boots without a server_turn.txt, which is the state every existing
deployment is in, so the property under test here is the negative one: a battle join must look
exactly as it did before, whether or not the host asked for relay support. The same battle is
hosted twice, once by a host advertising 'r' and once by a host that does not, and the two
hosts' received streams are compared line for line rather than merely searched for CLIENTIP.

The configured half, where a relay host is sent CLIENTIP with the joiner's public address, is
covered in-process by tests/worker/joineraddressworkertest.py, which needs no server and
cannot be disturbed by whatever config the operator has left in the repository root.
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import HOST, PORT
import socket, ssl, hashlib, base64, re, time, sys

raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()
HOST_R = "jaddrhostr"
HOST_PLAIN = "jaddrhostp"
JOINER = "jaddrjoiner"

# hosting requires TLS (in_OPENBATTLE), and the test server's certificate is self-signed
CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

errors = []
def check(cond, label):
    if not cond: errors.append(label)


class Client:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT))
        self.buf = ""
        self.read_for(0.5)
        self.send("STLS")
        self.read_until("OK", 5)
        self.s = CTX.wrap_socket(self.s)
        self.read_for(0.5)

    def send(self, line):
        self.s.sendall((line + "\n").encode())

    def read_until(self, substr, timeout=8):
        deadline = time.time() + timeout
        while substr not in self.buf and time.time() < deadline:
            self.s.settimeout(max(0.1, deadline - time.time()))
            try:
                chunk = self.s.recv(8192)
            except (socket.timeout, ssl.SSLWantReadError):
                break
            if not chunk:
                break
            self.buf += chunk.decode(errors="replace")
        out, self.buf = self.buf, ""
        return out

    def read_for(self, seconds):
        """Drain everything that arrives in a fixed window, for streams with no end marker."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.s.settimeout(max(0.05, deadline - time.time()))
            try:
                chunk = self.s.recv(8192)
            except (socket.timeout, ssl.SSLWantReadError):
                break
            if not chunk:
                break
            self.buf += chunk.decode(errors="replace")
        out, self.buf = self.buf, ""
        return out

    def close(self):
        try: self.s.close()
        except OSError: pass


def register(user):
    c = Client()
    c.send("REGISTER %s %s" % (user, PW))
    print("REGISTER %s -> %s" % (user, c.read_until("\n", 5).strip()))
    c.close()


def login(user, flags):
    c = Client()
    c.send("LOGIN %s %s 0 * TestClient\t0\t%s" % (user, PW, flags))
    got = c.read_until("AGREEMENTEND", 10)
    if "AGREEMENTEND" in got:
        c.send("CONFIRMAGREEMENT")
        got += c.read_until("LOGININFOEND", 10)
    check("LOGININFOEND" in got, "%s should log in, got:\n%s" % (user, got))
    return c


def open_battle(host, title):
    host.send("OPENBATTLE 0 0 * 8452 8 4660 0 4660 spring\t105.0\tDeltaSiegeDry\t%s\tBA" % title)
    opened = host.read_for(1.5)
    battle_id = None
    for line in opened.splitlines():
        if line.startswith("BATTLEOPENED "):
            battle_id = line.split(" ")[1]
    check(battle_id is not None, "OPENBATTLE should produce a BATTLEOPENED, got:\n%s" % opened)
    return battle_id


def normalise(lines, host_user, battle_id):
    """Strip the parts that differ by construction: the host's name and the battle id."""
    out = []
    for line in lines:
        if not line.strip():
            continue
        line = re.sub(r"__battle__\d+", "BCHAN", line.strip()).replace(host_user, "HOST")
        out.append(re.sub(r"(?<= )%s(?= |$)" % re.escape(battle_id), "BID", line))
    return out


def hosted_join(host_user, flags):
    """Host a battle, have the joiner join it, and hand back what the host saw."""
    host = login(host_user, flags)
    battle_id = open_battle(host, "relay join test")
    joiner = login(JOINER, "u sp")
    joiner.read_for(0.5)
    host.read_for(0.5)  # discard the joiner's ADDUSER etc, keeping only the join itself

    joiner.send("JOINBATTLE %s" % battle_id)
    joiner.read_for(1.5)
    seen = host.read_for(1.5)

    joiner.send("LEAVEBATTLE")
    joiner.read_for(0.5)
    joiner.close()
    host.close()
    time.sleep(0.5)  # let the server tear the battle down before the next one opens
    return normalise(seen.splitlines(), host_user, battle_id)


for user in (HOST_R, HOST_PLAIN, JOINER):
    register(user)
print("waiting 3s for the delayed-registration window...")
time.sleep(3)

relay_host = hosted_join(HOST_R, "u sp r")
plain_host = hosted_join(HOST_PLAIN, "u sp")
print("host with 'r'    ->", relay_host)
print("host without 'r' ->", plain_host)

check(any(line.startswith("JOINEDBATTLE ") for line in relay_host),
      "the join should have happened at all, got %r" % (relay_host,))
check(not any(line.startswith("CLIENTIP ") for line in relay_host),
      "no relay is configured, so no CLIENTIP should be sent, got %r" % (relay_host,))
check(relay_host == plain_host,
      "advertising 'r' must not change the conversation on a relay-less server:\n  %r\n  %r"
      % (relay_host, plain_host))

if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
