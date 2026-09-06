"""
End-to-end tier: MOVERELAYEDHOST (issue #43) against a server that has a relay configured.

The shared test server boots without a server_turn.txt, so this script brings its own: a
second lobby server, on its own port, started from a temporary directory holding nothing but
a server_turn.txt. That keeps the configured half of relay hosting testable over real sockets
without touching whatever config the operator has left in the repository root.

What it proves, over the wire, with a battle that stays open the whole time:
  - a host moves its battle and a client joining afterwards is told the new address AND the
    new port. The port is the trap: a rebuilt TURN allocation moves both, MOVERELAYEDHOST is
    the only line carrying either, and a move that updated the address alone would leave the
    battle exactly as unreachable as it was
  - a client already holding the battle in its list is told, if it asked for relay support
  - a client that did not ask for relay support is told nothing at all
  - a non-host sitting in the battle cannot move it
  - a private address is refused, and the battle stays where it was
  - a battle opened without a relay is not convertible into a relayed one

The relay-less refusals are in moverelayedhosttest.py, which uses the shared server.
"""
import os as _os, sys as _sys
_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir))
_sys.path[:0] = [_ROOT, _os.path.join(_os.path.dirname(__file__), _os.pardir)]
import base64, hashlib, shutil, socket, ssl, subprocess, sys, tempfile, time

import testenv

HOST = testenv.HOST
PORT = testenv.PORT + 100  # its own server, so it cannot collide with the shared one
NATPORT = PORT + 1

RAW_PW = "secretpw"
PW = base64.b64encode(hashlib.md5(RAW_PW.encode()).digest()).decode()

# Real public addresses: the documentation ranges are refused along with everything else
# nobody can route to.
OPEN_IP, OPEN_PORT = "185.199.108.153", "30001"
MOVED_IP, MOVED_PORT = "151.101.1.140", "41234"

CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # hosting requires TLS, the cert is self-signed
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
    c.read_until("\n", 5)
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


def battleopened(text, battle_id=None):
    """The last BATTLEOPENED in a stream, optionally for one battle."""
    for line in reversed(text.splitlines()):
        if line.startswith("BATTLEOPENED ") and (battle_id is None or line.split(" ")[1] == battle_id):
            return line
    return ""


def refusal(text):
    """The MOVERELAYEDHOSTFAILED line in a stream, which arrives among unrelated traffic.

    Other people log in and out while a test runs, so ADDUSER and REMOVEUSER turn up in the
    same read. Match on the line, not on the front of the buffer.
    """
    for line in text.splitlines():
        if line.startswith("MOVERELAYEDHOSTFAILED "):
            return line.strip()
    return ""


def pair(line):
    """The (address, port) a client would dial for this battle: fields 5 and 6."""
    fields = line.split(" ")
    return (fields[5], fields[6]) if len(fields) > 6 else ("", "")


def open_relayed_battle(host, ip=OPEN_IP, port=OPEN_PORT, relayed=True, title="move test"):
    if relayed:
        host.send("RELAYEDHOST %s %s" % (ip, port))
        got = host.read_for(0.6)
        check("RELAYEDHOSTFAILED" not in got,
              "RELAYEDHOST should be accepted, got %r" % (got,))
    host.send("OPENBATTLE 0 0 * %s 8 4660 0 4660 spring\t105.0\tDeltaSiegeDry\t%s\tBA"
              % (port, title))
    opened = host.read_for(1.5)
    line = battleopened(opened)
    check(line != "", "OPENBATTLE should produce a BATTLEOPENED, got:\n%s" % opened)
    return line.split(" ")[1]


def start_server(workdir):
    env = dict(_os.environ)
    env["PYTHONPATH"] = _os.pathsep.join(
        [_ROOT, _os.path.join(_ROOT, "protocol")]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    with open(_os.path.join(workdir, "server_turn.txt"), "w") as f:
        f.write("turn:relay.example.org:3478\na_long_random_shared_secret\n")
    proc = subprocess.Popen(
        [sys.executable, _os.path.join(_ROOT, "server.py"),
         "-p", str(PORT), "-n", str(NATPORT), "-s", testenv.DB_URL],
        cwd=workdir, env=env)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=2) as s:
                s.settimeout(2)
                if s.recv(64):
                    return proc
        except OSError:
            time.sleep(0.3)
    proc.terminate()
    raise SystemExit("relay-configured lobby server did not become ready on %s:%d" % (HOST, PORT))


HOSTU, INROOM, PLAIN, LATE1, LATE2, LATE3, DIRECT = (
    "mvrhost", "mvrinroom", "mvrplain", "mvrlate1", "mvrlate2", "mvrlate3", "mvrdirect")

workdir = tempfile.mkdtemp(prefix="uberserver-relay-")
proc = start_server(workdir)
try:
    for user in (HOSTU, INROOM, PLAIN, LATE1, LATE2, LATE3, DIRECT):
        register(user)
    print("waiting 3s for the delayed-registration window...")
    time.sleep(3)

    # --- a relayed battle, opened and then moved ------------------------------------
    host = login(HOSTU, "u sp r")
    battle_id = open_relayed_battle(host)

    inroom = login(INROOM, "u sp r")     # asks for relay support, and sits in the battle
    plain = login(PLAIN, "u sp")         # does not, and only watches the list
    before_inroom = battleopened(inroom.login_text, battle_id)
    before_plain = battleopened(plain.login_text, battle_id)
    check(pair(before_inroom) == (OPEN_IP, OPEN_PORT),
          "the battle should open at the relay pair, got %r" % (before_inroom,))
    check(pair(before_plain) == (OPEN_IP, OPEN_PORT),
          "every client should see the same opening pair, got %r" % (before_plain,))

    inroom.send("JOINBATTLE %s * sp1" % battle_id)
    inroom.read_until("REQUESTBATTLESTATUS", 5)
    host.read_for(0.5)
    plain.read_for(0.5)

    host.send("MOVERELAYEDHOST %s %s" % (MOVED_IP, MOVED_PORT))
    host_saw = host.read_for(1.0)
    inroom_saw = inroom.read_for(1.0)
    plain_saw = plain.read_for(1.0)
    print("host after the move  ->", host_saw.strip().replace("\n", " | "))
    print("in-room 'r' client   ->", inroom_saw.strip().replace("\n", " | "))
    print("client without 'r'   ->", repr(plain_saw))

    expected = "BATTLEHOSTMOVED %s %s %s" % (battle_id, MOVED_IP, MOVED_PORT)
    check(expected in inroom_saw,
          "an 'r' client holding the battle should be told it moved, expected %r, got %r"
          % (expected, inroom_saw))
    check(expected in host_saw,
          "the host should get its own move back as the acknowledgement, got %r" % (host_saw,))
    check("MOVERELAYEDHOSTFAILED" not in host_saw,
          "a valid move should not be refused, got %r" % (host_saw,))
    check("BATTLEHOSTMOVED" not in plain_saw and MOVED_IP not in plain_saw,
          "a client that did not ask for relay support should not be told the battle moved, "
          "got %r" % (plain_saw,))

    # the whole point: a client that turns up afterwards dials the rebuilt allocation
    late = login(LATE1, "u sp")
    moved_line = battleopened(late.login_text, battle_id)
    print("battle after the move ->", moved_line)
    check(pair(moved_line) == (MOVED_IP, MOVED_PORT),
          "a client arriving after the move should be told the new address and port, got %r"
          % (moved_line,))
    check(moved_line.split(" ")[3] == "0",
          "natType should still be 0 after a move, got %r" % (moved_line,))
    # everything but the pair is the line it was
    def blanked(line):
        f = line.split(" ")
        f[5], f[6] = "IP", "PORT"
        return " ".join(f)
    check(blanked(moved_line) == blanked(before_plain),
          "only the address and port should differ across a move:\n  %r\n  %r"
          % (before_plain, moved_line))
    late.close()

    # --- a non-host in the battle cannot move it -------------------------------------
    inroom.send("MOVERELAYEDHOST %s %s" % (OPEN_IP, OPEN_PORT))
    said = refusal(inroom.read_for(1.0))
    print("non-host move        ->", said)
    check(said != "", "a non-host sitting in the battle should be refused")
    check(len(said.split(" ", 1)[-1]) > 10,
          "the refusal should carry a reason a person can read, got %r" % (said,))

    # --- a private, loopback or malformed address is refused --------------------------
    for bad, why in (("192.168.1.10", "a private address"), ("127.0.0.1", "loopback"),
                     ("not.an.address", "a string that is not an address")):
        host.send("MOVERELAYEDHOST %s 5000" % bad)
        said = refusal(host.read_for(0.8))
        print("move to %-31s ->" % why, said)
        check(said != "", "%s (%r) should be refused, got nothing" % (why, bad))

    late = login(LATE2, "u sp")
    still = battleopened(late.login_text, battle_id)
    check(pair(still) == (MOVED_IP, MOVED_PORT),
          "a refused move must leave the battle where it was, got %r" % (still,))
    late.close()

    inroom.close()
    plain.close()
    host.close()
    time.sleep(0.5)  # let the server tear the battle down before the next one opens

    # --- a battle opened without a relay is not converted -----------------------------
    direct = login(DIRECT, "u sp r")
    direct_id = open_relayed_battle(direct, relayed=False, port="30002", title="direct test")
    watcher = login(LATE3, "u sp r")
    direct_before = battleopened(watcher.login_text, direct_id)
    watcher.read_for(0.3)

    direct.send("MOVERELAYEDHOST %s %s" % (MOVED_IP, MOVED_PORT))
    said = refusal(direct.read_for(1.0))
    announced = watcher.read_for(0.8)
    print("move a direct battle ->", said)
    check(said != "", "a battle opened without a relay should not be convertible")
    check("BATTLEHOSTMOVED" not in announced,
          "a refused conversion must announce nothing, got %r" % (announced,))
    check(pair(direct_before)[1] == "30002",
          "the direct battle should have opened at its OPENBATTLE port, got %r" % (direct_before,))

    watcher.close()
    direct.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(workdir, ignore_errors=True)

if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
