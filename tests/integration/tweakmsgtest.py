"""
End-to-end tier: SPADS tweak commands survive the message length limit (issue #56).

A player changes unit stats for one battle by sending the autohost a compiled tweak set as
battle chat, "!bset tweakunits<n> <base64>", one command per slot. A slot is 16000 base64
characters, so before this change every one of them was dropped by the length check in
Client.HandleProtocolCommands and never reached the battle.

Two properties are checked against a live server. A 16000 character tweak command arrives at
the host intact, and an ordinary battle message of the same size is still refused with the
SERVERMSG that names the limit, so the exemption has not turned into a general raise.
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import HOST, PORT
import socket, ssl, hashlib, base64, time, sys

raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()
BATTLE_HOST = "tweakhost"
TWEAKER = "tweakplayer"
CHATTER = "tweakchatter"
PAYLOAD = "A" * 16000  # the size the tooling players use today

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


def login(user):
    c = Client()
    c.send("LOGIN %s %s 0 * TestClient\t0\tu sp" % (user, PW))
    got = c.read_until("AGREEMENTEND", 10)
    if "AGREEMENTEND" in got:
        c.send("CONFIRMAGREEMENT")
        got += c.read_until("LOGININFOEND", 10)
    check("LOGININFOEND" in got, "%s should log in, got:\n%s" % (user, got))
    return c


def join_battle(user, battle_id):
    c = login(user)
    c.send("JOINBATTLE %s" % battle_id)
    got = c.read_until("JOINBATTLE ", 8)
    check("JOINBATTLEFAILED" not in got, "%s should join the battle, got:\n%s" % (user, got))
    return c


for user in (BATTLE_HOST, TWEAKER, CHATTER):
    register(user)
print("waiting 3s for the delayed-registration window...")
time.sleep(3)

host = login(BATTLE_HOST)
host.send("OPENBATTLE 0 0 * 30001 8 4660 0 4660 spring\t105.0\tDeltaSiegeDry\ttweak command test\tBA")
opened = host.read_for(1.5)
battle_id = None
for line in opened.splitlines():
    if line.startswith("BATTLEOPENED "):
        battle_id = line.split(" ")[1]
if battle_id is None:
    print("ABORT: OPENBATTLE produced no BATTLEOPENED, got:\n%s" % opened)
    sys.exit(1)
print("battle %s opened" % battle_id)

# a tweak slot, the size a real one is
tweaker = join_battle(TWEAKER, battle_id)
host.read_for(0.5)
tweaker.send("SAYBATTLE !bset tweakunits1 %s" % PAYLOAD)
tweak_reply = tweaker.read_for(1.5)
at_host = host.read_for(2.0)

check("message length limit" not in tweak_reply,
      "the sender should not be told the tweak command was too long, got:\n%s" % tweak_reply[:200])
check(("!bset tweakunits1 " + PAYLOAD) in at_host,
      "the host should receive the whole tweak command, got %d chars:\n%s"
      % (len(at_host), at_host[:200]))

# the same size of ordinary battle chat, which is not a tweak command
chatter = join_battle(CHATTER, battle_id)
host.read_for(0.5)
chatter.send("SAYBATTLE %s" % PAYLOAD)
chat_reply = chatter.read_for(1.5)
chat_at_host = host.read_for(2.0)

check("message length limit" in chat_reply,
      "ordinary chat of the same size should still be refused, got:\n%s" % chat_reply[:200])
check(PAYLOAD not in chat_at_host,
      "ordinary chat of the same size should not reach the host, got %d chars" % len(chat_at_host))

for c in (chatter, tweaker, host):
    c.close()

if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
