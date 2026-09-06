"""
End-to-end tier: MOVERELAYEDHOST (issue #43) against a server with no TURN relay configured.

The shared test server boots without a server_turn.txt, which is the state every existing
deployment is in, so the properties under test here are the negative ones. A move is refused
with a reason somebody can read, whether or not the sender is hosting anything, and a battle
whose host sent one is advertised exactly as it would have been if the line had never been
sent. Two battles are opened, one by a host that tries to move it and one by a host that does
not, and what an onlooker is told about each is compared line for line.

The configured half, where the battle really does move, is covered over real sockets by
moverelayedhostrelaytest.py, which brings its own relay-configured server, and in-process by
tests/worker/moverelayedhostworkertest.py.
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import HOST, PORT
import socket, ssl, hashlib, base64, re, time, sys

raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()
HOST_MOVE = "mvnrhostm"
HOST_PLAIN = "mvnrhostp"
IDLE = "mvnridle"
WATCHER = "mvnrwatcher"
RELAY_IP = "185.199.108.153"  # a real public address: the documentation ranges are refused
MOVED_IP = "151.101.1.140"

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


def refusal(text):
    """The MOVERELAYEDHOSTFAILED line in a stream, which arrives among unrelated traffic."""
    for line in text.splitlines():
        if line.startswith("MOVERELAYEDHOSTFAILED "):
            return line.strip()
    return ""


def normalise(line, host_user, battle_id):
    """Strip the parts that differ by construction: the host's name and the battle id."""
    line = re.sub(r"__battle__\d+", "BCHAN", line.strip()).replace(host_user, "HOST")
    return re.sub(r"(?<= )%s(?= |$)" % re.escape(battle_id), "BID", line)


def hosted(host_user, flags, move):
    """Open a battle, optionally trying to move it, and report what was seen."""
    host = login(host_user, flags)
    said = ""
    if move:
        host.send("RELAYEDHOST %s 30001" % RELAY_IP)
        host.read_for(0.6)

    host.send("OPENBATTLE 0 0 * 30001 8 4660 0 4660 spring\t105.0\tDeltaSiegeDry\tmove host test\tBA")
    opened = host.read_for(1.5)
    battle_id = None
    for line in opened.splitlines():
        if line.startswith("BATTLEOPENED "):
            battle_id = line.split(" ")[1]
    check(battle_id is not None, "OPENBATTLE should produce a BATTLEOPENED, got:\n%s" % opened)

    if move:
        host.send("MOVERELAYEDHOST %s 41234" % MOVED_IP)
        said = refusal(host.read_for(1.0))

    # a client logging in afterwards is told about the battle in the same BATTLEOPENED form,
    # which is the line a joiner actually dials
    watcher = login(WATCHER, "u sp r")
    seen = ""
    for line in watcher.login_text.splitlines():
        if line.startswith("BATTLEOPENED "):
            seen = line
    watcher.close()
    host.close()
    time.sleep(0.5)  # let the server tear the battle down before the next one opens
    return said, normalise(seen, host_user, battle_id or "")


for user in (HOST_MOVE, HOST_PLAIN, IDLE, WATCHER):
    register(user)
print("waiting 3s for the delayed-registration window...")
time.sleep(3)

# a client hosting nothing at all, so the relay gate is reached before anything else
idle = login(IDLE, "u sp r")
idle.send("MOVERELAYEDHOST %s 41234" % MOVED_IP)
idle_said = refusal(idle.read_for(1.0))
idle.close()

move_said, move_battle = hosted(HOST_MOVE, "u sp r", move=True)
_, plain_battle = hosted(HOST_PLAIN, "u sp", move=False)
print("MOVERELAYEDHOST while hosting nothing ->", idle_said)
print("MOVERELAYEDHOST while hosting         ->", move_said)
print("battle after a refused move ->", move_battle)
print("battle nobody touched       ->", plain_battle)

for said, who in ((idle_said, "a client hosting nothing"), (move_said, "a host")):
    check(said != "", "a server with no relay should refuse %s a move" % who)
    check(len(said.split(" ", 1)[-1]) > 10,
          "the refusal to %s should carry a reason a person can read, got %r" % (who, said))

check(move_battle.startswith("BATTLEOPENED "),
      "the battle should have been advertised at all, got %r" % (move_battle,))
check(MOVED_IP not in move_battle,
      "a refused move must not reach the battle, got %r" % (move_battle,))
check(move_battle.split(" ")[3] == "0",
      "natType should still be 0, got %r" % (move_battle,))
check(move_battle == plain_battle,
      "a refused move must leave the battle exactly as it was:\n  %r\n  %r"
      % (move_battle, plain_battle))

if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
