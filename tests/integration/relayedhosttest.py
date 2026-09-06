"""
End-to-end tier: RELAYEDHOST (issue #32) against a server with no TURN relay configured.

The shared test server boots without a server_turn.txt, which is the state every existing
deployment is in, so the properties under test here are the negative ones. RELAYEDHOST is
refused with a reason somebody can read, and the battle that follows is advertised exactly as
it would have been if the line had never been sent. Two battles are opened, one by a host that
sends RELAYEDHOST and one by a host that does not, and what an onlooker is told about each is
compared line for line.

The configured half, where the battle is advertised at the relay for every recipient, is
covered in-process by tests/worker/relayedhostworkertest.py, which needs no server and cannot
be disturbed by whatever config the operator has left in the repository root.
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import HOST, PORT
import socket, ssl, hashlib, base64, re, time, sys

raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()
HOST_R = "rhosthostr"
HOST_PLAIN = "rhosthostp"
WATCHER = "rhostwatcher"
RELAY_IP = "185.199.108.153"  # a real public address: the documentation ranges are refused

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
    c.login_text = got  # the battle list arrives during login, ahead of LOGININFOEND
    return c


def normalise(line, host_user, battle_id):
    """Strip the parts that differ by construction: the host's name and the battle id."""
    line = re.sub(r"__battle__\d+", "BCHAN", line.strip()).replace(host_user, "HOST")
    return re.sub(r"(?<= )%s(?= |$)" % re.escape(battle_id), "BID", line)


def hosted(host_user, flags, relayed):
    """Open a battle, optionally naming a relay address first, and report what was seen."""
    host = login(host_user, flags)
    refusal = ""
    if relayed:
        host.send("RELAYEDHOST %s 30001" % RELAY_IP)
        refusal = host.read_for(1.0).strip()

    host.send("OPENBATTLE 0 0 * 30001 8 4660 0 4660 spring\t105.0\tDeltaSiegeDry\trelayed host test\tBA")
    opened = host.read_for(1.5)
    battle_id = None
    for line in opened.splitlines():
        if line.startswith("BATTLEOPENED "):
            battle_id = line.split(" ")[1]
    check(battle_id is not None, "OPENBATTLE should produce a BATTLEOPENED, got:\n%s" % opened)

    # a client logging in afterwards is told about the battle in the same BATTLEOPENED form,
    # which is the line a joiner actually dials
    watcher = login(WATCHER, "u sp")
    seen = ""
    for line in watcher.login_text.splitlines():
        if line.startswith("BATTLEOPENED "):
            seen = line
    watcher.close()
    host.close()
    time.sleep(0.5)  # let the server tear the battle down before the next one opens
    return refusal, normalise(seen, host_user, battle_id or "")


for user in (HOST_R, HOST_PLAIN, WATCHER):
    register(user)
print("waiting 3s for the delayed-registration window...")
time.sleep(3)

refusal, relay_battle = hosted(HOST_R, "u sp r", relayed=True)
_, plain_battle = hosted(HOST_PLAIN, "u sp", relayed=False)
print("RELAYEDHOST reply ->", refusal)
print("battle after RELAYEDHOST ->", relay_battle)
print("battle without one      ->", plain_battle)

check(refusal.startswith("RELAYEDHOSTFAILED "),
      "a server with no relay should refuse RELAYEDHOST, got %r" % (refusal,))
check(len(refusal.split(" ", 1)[-1]) > 10,
      "the refusal should carry a reason a person can read, got %r" % (refusal,))

check(relay_battle.startswith("BATTLEOPENED "),
      "the battle should have been advertised at all, got %r" % (relay_battle,))
check(RELAY_IP not in relay_battle,
      "a refused address must not reach the battle, got %r" % (relay_battle,))
check(relay_battle.split(" ")[3] == "0",
      "natType should still be 0, got %r" % (relay_battle,))
check(relay_battle == plain_battle,
      "a refused RELAYEDHOST must leave the battle exactly as it was:\n  %r\n  %r"
      % (relay_battle, plain_battle))

if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
