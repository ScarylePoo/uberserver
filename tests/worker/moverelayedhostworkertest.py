"""
Worker-unit test for MOVERELAYEDHOST (issue #43): moving an open battle to a rebuilt relay
allocation. Runs the real in_MOVERELAYEDHOST, in_RELAYEDHOST, in_OPENBATTLE and
client_AddBattle in-process against fakes, no server and no DB needed.

Three properties matter.

The port moves with the address. A rebuilt allocation lands on a new address *and* a new
port, and MOVERELAYEDHOST is the only line carrying either, so a move that updated the
address alone would leave the battle exactly as unreachable as it was. Every assertion here
checks both fields.

Only the host moves a battle, and only a relayed one. A spectator sitting in somebody else's
battle must not be able to send everybody in it to an address of its choosing, and a battle
opened at a working direct address must not be converted, because everyone already holding
that address would be stranded on it.

Nobody else's conversation changes. BATTLEHOSTMOVED goes only to clients that asked for relay
support at login, and a battle nobody moves is advertised exactly as it was.

Run: activate the venv, then python3 tests/worker/moverelayedhostworkertest.py
"""
import os as _os, sys as _sys
_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir))
_sys.path[:0] = [_ROOT, _os.path.join(_ROOT, "protocol"),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
import sys

from protocol import Protocol as ProtocolModule

# Real public addresses, because the documentation ranges are refused along with everything
# else nobody can route to. OPEN_V4 is where the battle opens, MOVED_V4 is the rebuilt
# allocation it moves to.
OPEN_V4 = "185.199.108.153"
MOVED_V4 = "151.101.1.140"
MOVED_V6 = "2606:4700:4700::1111"
LOBBY_IP = "192.0.2.1"
LOBBY_LAN = "10.0.0.2"
HOST_WAN = "81.2.69.142"
HOST_LAN = "192.168.1.10"
OUTSIDER = "31.13.72.36"

OPEN_PORT = "30001"
MOVED_PORT = "41234"

errors = []
def check(cond, label):
    if not cond: errors.append(label)


class FakeChannelDB:
    """Battles are never registered channels, which is all LEAVEBATTLE asks about."""
    @staticmethod
    def registered(channel):
        return False


class FakeSayHooks:
    @staticmethod
    def hook_OPENBATTLE(protocol, client, title):
        return title


class FakeRoot:
    def __init__(self, relay=True):
        self.turn_uri = "turn:relay.example.org:3478" if relay else None
        self.turn_secret = "a_long_random_string" if relay else None
        self.online_ip = LOBBY_IP
        self.local_ip = LOBBY_LAN
        self.trusted_proxies = set()
        self.channels = {}
        self.battles = {}
        self.usernames = {}
        self.sessions = {}
        self.nextbattle = 0
        self.broadcasts = []
        self.SayHooks = FakeSayHooks
        self.channeldb = FakeChannelDB
        self.protocol = ProtocolModule.Protocol(self)
    def turn_enabled(self):
        return bool(self.turn_uri and self.turn_secret)
    def clientFromSession(self, session_id):
        return self.sessions.get(session_id)
    def clientFromID(self, user_id, fromdb=False):
        return None
    def broadcast(self, message, chan=None, ignore=set(), state=None, flag=None, not_flag=None):
        self.broadcasts.append(message)
    def broadcast_battle(self, message, battle_id, ignore=None):
        self.broadcasts.append(message)
    def getUserDB(self): pass
    def getVerificationDB(self): pass
    def getBanDB(self): pass
    def getContentDB(self): pass


class FakeClient:
    _next_session = 1
    def __init__(self, root, username, compat=("u", "sp"), ip=HOST_WAN, local_ip=None):
        self.sent = []
        self.username = username
        self.compat = set(compat)
        self.ip_address = ip
        self.local_ip = local_ip if local_ip else ip
        self.user_id = FakeClient._next_session
        self.session_id = FakeClient._next_session
        FakeClient._next_session += 1
        self.static = True  # skips Channel.recordUse, which wants a db
        self.TLS = True     # in_OPENBATTLE refuses to host without it
        self.bot = False
        self.channels = set()
        self.battle_bots = {}
        self.scriptPassword = None
        self.current_battle = None
        self.pending_battle = None
        self.relayed_host_ip = None
        self.hostport = None
        self.udpport = None
        self.ignored = {}
        self.battlestatus = {'ready':'0', 'id':'0000', 'ally':'0000', 'mode':'0',
                             'sync':'00', 'side':'00', 'handicap':'0000000'}
        self.teamcolor = '0'
        root.sessions[self.session_id] = self
        root.usernames[username] = self
    def Send(self, data, command=None):
        self.sent.append(data)
    def SendBattle(self, battle, data):
        self.sent.append(data)


def reply(client, fn, *args):
    """Run a command and hand back its refusal, or "" when it was accepted.

    A move that succeeds sends the host its own BATTLEHOSTMOVED, so the refusal is the
    *FAILED line rather than simply the first thing the sender was sent.
    """
    before = len(client.sent)
    fn(*args)
    for line in client.sent[before:]:
        if line.split(" ")[0].endswith("FAILED"):
            return line
    return ""


def relayedhost(root, client, ip, port=OPEN_PORT):
    return reply(client, root.protocol.in_RELAYEDHOST, client, ip, port)


def moverelayedhost(root, client, ip, port=MOVED_PORT):
    return reply(client, root.protocol.in_MOVERELAYEDHOST, client, ip, port)


def openbattle(root, host, port=OPEN_PORT, title="relay move test"):
    root.protocol.in_OPENBATTLE(host, "0", "0", "*", port, "8", "4660", "0", "4660",
                                "spring\t105.0\tDeltaSiegeDry\t%s\tBA" % title)
    return root.battles[host.current_battle]


def advertised_to(root, client, battle):
    """The BATTLEOPENED a client logging in now would be sent for this battle.

    client_AddBattle is the one place the address and port are worked out, and login
    (_login_finish_now) and broadcast_AddBattle both go through it, so this is what a
    late arrival is actually told.
    """
    return root.protocol.client_AddBattle(client, battle)


def field(line, n):
    return line.split(" ")[n] if line else ""


def address_of(line):
    return field(line, 5)


def port_of(line):
    return field(line, 6)


def moved_lines(client):
    return [line for line in client.sent if line.startswith("BATTLEHOSTMOVED ")]


def relayed_battle(host_compat=("u", "sp", "r")):
    """A relay host with an open battle, an onlooker with 'r' and one without."""
    FakeClient._next_session = 1  # the battle channel is named after the host's account id
    root = FakeRoot()
    host = FakeClient(root, "movehost", compat=host_compat)
    watcher = FakeClient(root, "watcher_r", compat=("u", "sp", "r"), ip=OUTSIDER)
    plain = FakeClient(root, "watcher_plain", compat=("u", "sp"), ip=OUTSIDER)
    relayedhost(root, host, OPEN_V4)
    battle = openbattle(root, host)
    for c in (host, watcher, plain):
        c.sent = []
    return root, host, watcher, plain, battle


# --- the happy path: address and port both move -----------------------------------
root, host, watcher, plain, battle = relayed_battle()
opened = advertised_to(root, watcher, battle)
check(address_of(opened) == OPEN_V4,
      "the battle should open at the relay, got %r" % (opened,))
check(port_of(opened) == OPEN_PORT,
      "the battle should open at the OPENBATTLE port, got %r" % (opened,))

check(moverelayedhost(root, host, MOVED_V4) == "",
      "a public address should be accepted in silence, got %r" % (host.sent,))

after = advertised_to(root, watcher, battle)
check(address_of(after) == MOVED_V4,
      "a client arriving after the move should be told the new address, got %r" % (after,))
# the trap: a move that only changed the address would leave the battle on the old port,
# which is exactly as unreachable as the old address was
check(port_of(after) == MOVED_PORT,
      "a client arriving after the move should be told the new port, got %r" % (after,))
check(field(after, 3) == "0",
      "natType should still be 0 after a move, got %r" % (after,))

# everything else about the battle is the line it was
def without_address(line):
    fields = line.split(" ")
    fields[5] = "IP"
    fields[6] = "PORT"
    return " ".join(fields)

check(without_address(after) == without_address(opened),
      "only the address and port should differ across a move:\n  %r\n  %r" % (opened, after))

# a joiner behind the same NAT as the host is told the relay too, as it was at OPENBATTLE
samewan = FakeClient(root, "samewan", compat=("u", "sp"), ip=HOST_WAN, local_ip="192.168.1.11")
same = advertised_to(root, samewan, battle)
check(address_of(same) == MOVED_V4 and port_of(same) == MOVED_PORT,
      "a same-WAN joiner should be told the moved relay pair, got %r" % (same,))


# --- who is told, and what ---------------------------------------------------------
check(moved_lines(watcher) == ["BATTLEHOSTMOVED %s %s %s" % (battle.battle_id, MOVED_V4, MOVED_PORT)],
      "an 'r' client already holding the battle should be told it moved, got %r" % (watcher.sent,))
check(moved_lines(host) == ["BATTLEHOSTMOVED %s %s %s" % (battle.battle_id, MOVED_V4, MOVED_PORT)],
      "the host should get its own move back as the acknowledgement, got %r" % (host.sent,))
check(plain.sent == [],
      "a client that did not ask for relay support should hear nothing, got %r" % (plain.sent,))


# --- IPv6 moves too, in canonical form ---------------------------------------------
root, host, watcher, plain, battle = relayed_battle()
check(moverelayedhost(root, host, "2606:4700:4700:0000:0000:0000:0000:1111", "3478") == "",
      "an expanded IPv6 address should be accepted, got %r" % (host.sent,))
after = advertised_to(root, watcher, battle)
check(address_of(after) == MOVED_V6 and port_of(after) == "3478",
      "IPv6 should move canonicalised, got %r" % (after,))


# --- only the host moves it --------------------------------------------------------
root, host, watcher, plain, battle = relayed_battle()
joiner = FakeClient(root, "joiner", compat=("u", "sp", "r"), ip=OUTSIDER)
battle.joinBattle(joiner)
joiner.sent = []
reply_text = moverelayedhost(root, joiner, MOVED_V4)
check(reply_text.startswith("MOVERELAYEDHOSTFAILED "),
      "a non-host sitting in the battle should be refused, got %r" % (reply_text,))
check(battle.relayed_ip == OPEN_V4 and str(battle.port) == OPEN_PORT,
      "a refused non-host must not move the battle, got %r:%r" % (battle.relayed_ip, battle.port))
check(moved_lines(watcher) == [],
      "a refused move must announce nothing, got %r" % (watcher.sent,))

# and neither does somebody in no battle at all
outsider = FakeClient(root, "nobattle", compat=("u", "sp", "r"), ip=OUTSIDER)
reply_text = moverelayedhost(root, outsider, MOVED_V4)
check(reply_text.startswith("MOVERELAYEDHOSTFAILED "),
      "a client hosting nothing should be refused, got %r" % (reply_text,))


# --- a battle that was never relayed is not converted -------------------------------
FakeClient._next_session = 1
root = FakeRoot()
direct_host = FakeClient(root, "directhost", compat=("u", "sp", "r"))
watcher = FakeClient(root, "watcher_r", compat=("u", "sp", "r"), ip=OUTSIDER)
battle = openbattle(root, direct_host)
before = advertised_to(root, watcher, battle)
direct_host.sent = []
watcher.sent = []
reply_text = moverelayedhost(root, direct_host, MOVED_V4)
check(reply_text.startswith("MOVERELAYEDHOSTFAILED "),
      "a direct battle should not be convertible to a relayed one, got %r" % (reply_text,))
check(battle.relayed_ip is None,
      "a refused conversion must leave the battle direct, got %r" % (battle.relayed_ip,))
check(advertised_to(root, watcher, battle) == before,
      "a refused conversion must leave the battle exactly as it was:\n  %r\n  %r"
      % (before, advertised_to(root, watcher, battle)))
check(moved_lines(watcher) == [],
      "a refused conversion must announce nothing, got %r" % (watcher.sent,))


# --- addresses and ports that are refused -------------------------------------------
refusals = [
    ("127.0.0.1", "loopback"),
    ("::1", "IPv6 loopback"),
    ("10.0.0.5", "a private range"),
    ("192.168.1.10", "a private range"),
    ("172.16.3.4", "a private range"),
    ("169.254.7.7", "link-local"),
    ("100.64.0.1", "carrier-grade NAT"),
    ("0.0.0.0", "the unspecified address"),
    ("224.0.0.1", "multicast"),
    ("fd00::1", "an IPv6 unique-local address"),
    ("fe80::1", "an IPv6 link-local address"),
    ("2001:db8::1", "the IPv6 documentation range"),
    (LOBBY_IP, "the lobby server's own address"),
    (LOBBY_LAN, "the lobby server's LAN address"),
    ("not.an.address", "a string that is not an address"),
    ("", "an empty address"),
]
for bad, why in refusals:
    root, host, watcher, plain, battle = relayed_battle()
    reply_text = moverelayedhost(root, host, bad)
    check(reply_text.startswith("MOVERELAYEDHOSTFAILED "),
          "%s (%r) should be refused with a reason, got %r" % (why, bad, reply_text))
    check(len(reply_text.split(" ", 1)[-1]) > 10,
          "%s should be refused with something a person can read, got %r" % (why, reply_text))
    check(battle.relayed_ip == OPEN_V4 and str(battle.port) == OPEN_PORT,
          "%s must leave the battle where it was, got %r:%r" % (why, battle.relayed_ip, battle.port))
    check(moved_lines(watcher) == [], "%s must announce nothing, got %r" % (why, watcher.sent))

for bad_port, why in [("0", "port zero"), ("65536", "a port past the top of the range"),
                      ("-1", "a negative port"), ("relay", "a port that is not a number")]:
    root, host, watcher, plain, battle = relayed_battle()
    reply_text = moverelayedhost(root, host, MOVED_V4, port=bad_port)
    check(reply_text.startswith("MOVERELAYEDHOSTFAILED "),
          "%s should be refused, got %r" % (why, reply_text))
    check(battle.relayed_ip == OPEN_V4 and str(battle.port) == OPEN_PORT,
          "%s must leave the battle where it was, got %r:%r" % (why, battle.relayed_ip, battle.port))


# --- the two gates every relay command has ------------------------------------------
root, host, watcher, plain, battle = relayed_battle(host_compat=("u", "sp"))
# a host with no 'r' cannot have opened a relayed battle in practice, but the flag is
# checked on its own so that dropping it mid-session cannot be used to move anything
reply_text = moverelayedhost(root, host, MOVED_V4)
check(reply_text.startswith("MOVERELAYEDHOSTFAILED "),
      "a client without the 'r' flag should be refused, got %r" % (reply_text,))

FakeClient._next_session = 1
norelay = FakeRoot(relay=False)
norelay_host = FakeClient(norelay, "norelayhost", compat=("u", "sp", "r"))
battle = openbattle(norelay, norelay_host)
norelay_host.sent = []
reply_text = moverelayedhost(norelay, norelay_host, MOVED_V4)
check(reply_text.startswith("MOVERELAYEDHOSTFAILED "),
      "a server with no relay should refuse a move, got %r" % (reply_text,))
check(battle.relayed_ip is None,
      "a refused move on a relayless server must change nothing, got %r" % (battle.relayed_ip,))


# --- the sequence a single overloaded command would have broken ----------------------
# A relay host reopening its battle sends RELAYEDHOST while the old one is still open.
# in_OPENBATTLE reads the staged address before the in_LEAVEBATTLE that closes the old
# battle, so "the sender is hosting" does not tell a move apart from a re-host. Keeping
# MOVERELAYEDHOST separate leaves this working.
root, host, watcher, plain, battle = relayed_battle()
first_id = battle.battle_id
relayedhost(root, host, MOVED_V4, port=MOVED_PORT)
check(battle.relayed_ip == OPEN_V4 and str(battle.port) == OPEN_PORT,
      "RELAYEDHOST must not move the battle already open, got %r:%r"
      % (battle.relayed_ip, battle.port))
second = openbattle(root, host, port=MOVED_PORT, title="reopened")
check(second.battle_id != first_id, "the reopen should be a new battle")
reopened = advertised_to(root, watcher, second)
check(address_of(reopened) == MOVED_V4 and port_of(reopened) == MOVED_PORT,
      "the reopened battle should be advertised at the new relay, got %r" % (reopened,))
check(moved_lines(watcher) == [],
      "reopening a battle is not a move and must announce none, got %r" % (watcher.sent,))


if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: MOVERELAYEDHOST moves an open relayed battle's address and port together, "
      "refuses everybody but its host, and leaves every other client's conversation alone")
