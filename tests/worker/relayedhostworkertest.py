"""
Worker-unit test for RELAYEDHOST (issue #32): the address a relay host names for its battle.
Runs the real in_RELAYEDHOST, in_OPENBATTLE and client_AddBattle in-process against fakes, no
server and no DB needed.

Two properties matter. A relayed battle is advertised at the relay for every recipient, which
means the same-WAN-IP branch of client_AddBattle has to go as well as the other two: two
players behind one NAT reach a relayed battle through the relay, not across their own LAN. And
a battle nobody named an address for is advertised exactly as it was before, so every case
here is run twice, once with RELAYEDHOST and once without, and the two are compared.

Run: activate the venv, then python3 tests/worker/relayedhostworkertest.py
"""
import os as _os, sys as _sys
_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir))
_sys.path[:0] = [_ROOT, _os.path.join(_ROOT, "protocol"),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
import sys

from protocol import Protocol as ProtocolModule

# A relay allocation is on the public internet, and the documentation ranges (203.0.113.0/24,
# 198.51.100.0/24, 2001:db8::/32) are not, so they are refused along with everything else
# nobody can route to. That leaves real addresses as the only way to write these tests.
RELAY_V4 = "185.199.108.153"
RELAY_V6 = "2606:4700:4700::1111"
LOBBY_IP = "192.0.2.1"        # only ever compared against, never advertised
LOBBY_LAN = "10.0.0.2"
# A lobby with a real public address, for the cases where the relay is behind its NAT and
# so lives at that address. LOBBY_IP above is a documentation address, refused before the
# own-address check is ever reached.
LOBBY_PUBLIC = "104.16.132.229"
RELAY_LAN = "10.42.42.20"       # line 4 of server_turn.txt: the relay behind the lobby's NAT
HOST_WAN = "81.2.69.142"
HOST_LAN = "192.168.1.10"
OUTSIDER = "31.13.72.36"

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
    def __init__(self, relay=True, lan_ip=None, online_ip=LOBBY_IP):
        self.turn_uri = "turn:relay.example.org:3478" if relay else None
        self.turn_secret = "a_long_random_string" if relay else None
        self.turn_lan_ip = lan_ip  # line 4 of server_turn.txt: relay behind the lobby's NAT
        self.online_ip = online_ip
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
        self.battlestatus = {'ready':'0', 'id':'0000', 'ally':'0000', 'mode':'0',
                             'sync':'00', 'side':'00', 'handicap':'0000000'}
        self.teamcolor = '0'
        root.sessions[self.session_id] = self
        root.usernames[username] = self
    def Send(self, data, command=None):
        self.sent.append(data)


def relayedhost(root, client, ip, port="30001"):
    """Send RELAYEDHOST and hand back the reply, or "" when it was accepted in silence."""
    before = len(client.sent)
    root.protocol.in_RELAYEDHOST(client, ip, port)
    after = client.sent[before:]
    return after[0] if after else ""


def openbattle(root, host, title="relay test"):
    root.protocol.in_OPENBATTLE(host, "0", "0", "*", "30001", "8", "4660", "0", "4660",
                                "spring\t105.0\tDeltaSiegeDry\t%s\tBA" % title)


def advertised(client):
    for line in reversed(client.sent):
        if line.startswith("BATTLEOPENED "):
            return line
    return ""


def battle_address(line):
    """The <ip> field of a BATTLEOPENED, which is field 5."""
    return line.split(" ")[5] if line else ""


# --- what RELAYEDHOST accepts -----------------------------------------------------
root = FakeRoot()
host = FakeClient(root, "relayhost", compat=("u", "sp", "r"))

check(relayedhost(root, host, RELAY_V4) == "",
      "a public IPv4 relay address should be accepted in silence, got %r" % (host.sent,))
check(host.relayed_host_ip == RELAY_V4,
      "the address should be held against the client, got %r" % (host.relayed_host_ip,))

check(relayedhost(root, host, RELAY_V6) == "",
      "a public IPv6 relay address should be accepted, got %r" % (host.sent,))
check(host.relayed_host_ip == RELAY_V6,
      "IPv6 should be held as sent, got %r" % (host.relayed_host_ip,))

# one address, one spelling: what reaches a joining client is the canonical form
relayedhost(root, host, "2606:4700:4700:0000:0000:0000:0000:1111")
check(host.relayed_host_ip == RELAY_V6,
      "an expanded IPv6 address should be canonicalised, got %r" % (host.relayed_host_ip,))


# --- what it refuses --------------------------------------------------------------
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
    ("ff02::1", "IPv6 multicast"),
    ("2001:db8::1", "the IPv6 documentation range"),
    (LOBBY_IP, "the lobby server's own address"),
    (LOBBY_LAN, "the lobby server's LAN address"),
    ("not.an.address", "a string that is not an address"),
    ("010.0.0.1", "an octal-looking address"),
    ("", "an empty address"),
]
for bad, why in refusals:
    victim = FakeClient(root, "refused_%s" % why.replace(" ", "_"), compat=("u", "sp", "r"))
    reply = relayedhost(root, victim, bad)
    check(reply.startswith("RELAYEDHOSTFAILED "),
          "%s (%r) should be refused with a reason, got %r" % (why, bad, reply))
    check(len(reply.split(" ", 1)[-1]) > 10,
          "%s should be refused with something a person can read, got %r" % (why, reply))
    check(victim.relayed_host_ip is None,
          "%s should leave no address behind, got %r" % (why, victim.relayed_host_ip))

for bad_port, why in [("0", "port zero"), ("65536", "a port past the top of the range"),
                      ("-1", "a negative port"), ("relay", "a port that is not a number")]:
    victim = FakeClient(root, "badport_%s" % bad_port.strip("-"), compat=("u", "sp", "r"))
    reply = relayedhost(root, victim, RELAY_V4, port=bad_port)
    check(reply.startswith("RELAYEDHOSTFAILED "),
          "%s should be refused, got %r" % (why, reply))
    check(victim.relayed_host_ip is None,
          "%s should leave no address behind, got %r" % (why, victim.relayed_host_ip))

# a client that never asked for relay support has no business naming its own address
noflag = FakeClient(root, "noflaghost")
reply = relayedhost(root, noflag, RELAY_V4)
check(reply.startswith("RELAYEDHOSTFAILED "),
      "a client without the 'r' flag should be refused, got %r" % (reply,))
check(noflag.relayed_host_ip is None,
      "a refused client should hold no address, got %r" % (noflag.relayed_host_ip,))

# --- a relay behind the lobby's own NAT lives at the lobby's public address ---------
# coturn's external-ip is the NAT's public address, which is the lobby's online_ip, so with
# line 4 of server_turn.txt set that one own-address refusal has to lift. The LAN one stays.
nat_root = FakeRoot(lan_ip=RELAY_LAN, online_ip=LOBBY_PUBLIC)
nat_host = FakeClient(nat_root, "nathost", compat=("u", "sp", "r"))
reply = relayedhost(nat_root, nat_host, LOBBY_PUBLIC)
check(reply == "",
      "with the relay behind this NAT the lobby's own public address should be accepted, got %r" % (reply,))
check(nat_host.relayed_host_ip == LOBBY_PUBLIC,
      "the accepted address should be held for OPENBATTLE, got %r" % (nat_host.relayed_host_ip,))
nat_lan = FakeClient(nat_root, "natlanhost", compat=("u", "sp", "r"))
reply = relayedhost(nat_root, nat_lan, LOBBY_LAN)
check(reply.startswith("RELAYEDHOSTFAILED "),
      "line 4 must not open the door to the lobby's LAN address, got %r" % (reply,))
plain_root = FakeRoot(online_ip=LOBBY_PUBLIC)
plain_host = FakeClient(plain_root, "plainhost", compat=("u", "sp", "r"))
reply = relayedhost(plain_root, plain_host, LOBBY_PUBLIC)
check(reply.startswith("RELAYEDHOSTFAILED "),
      "without line 4 the lobby's own public address is still refused, got %r" % (reply,))

# and neither has anybody, on a server with no relay to be hosted through
norelay_root = FakeRoot(relay=False)
norelay_host = FakeClient(norelay_root, "norelayhost", compat=("u", "sp", "r"))
reply = relayedhost(norelay_root, norelay_host, RELAY_V4)
check(reply.startswith("RELAYEDHOSTFAILED "),
      "a server with no relay should refuse RELAYEDHOST, got %r" % (reply,))
check(norelay_host.relayed_host_ip is None,
      "a refused client should hold no address, got %r" % (norelay_host.relayed_host_ip,))


# --- every recipient is told the relay, whoever they are ---------------------------
def hosted(relayed, host_ip=HOST_WAN, host_lan=HOST_LAN):
    """Open a battle, relayed or not, and hand back what each kind of onlooker was told."""
    FakeClient._next_session = 1  # the battle channel is named after the host's account id
    root = FakeRoot()
    host = FakeClient(root, "host", compat=("u", "sp", "r"), ip=host_ip, local_ip=host_lan)
    # constructing a client registers it, and broadcast_AddBattle tells all of them
    FakeClient(root, "samewan", ip=host_ip, local_ip="192.168.1.11")
    FakeClient(root, "outsider", ip=OUTSIDER)
    if relayed:
        relayedhost(root, host, RELAY_V4)
    openbattle(root, host)
    return {name: advertised(client) for name, client in root.usernames.items()}

relayed = hosted(relayed=True)
direct = hosted(relayed=False)

for who in ("host", "samewan", "outsider"):
    check(battle_address(relayed[who]) == RELAY_V4,
          "%s should be told the relay address, got %r" % (who, relayed[who]))

# the same-WAN branch is the one it would be tempting to keep, so name what it used to do
check(battle_address(direct["samewan"]) == HOST_LAN,
      "without a relay a same-WAN joiner still gets the host's LAN address, got %r" % (direct["samewan"],))
check(battle_address(direct["outsider"]) == HOST_WAN,
      "without a relay an outsider still gets the host's WAN address, got %r" % (direct["outsider"],))

# a host the lobby thinks is private is branch 2, and it is wrong under a relay as well
lan_relayed = hosted(relayed=True, host_ip="10.9.9.9", host_lan="10.9.9.9")
lan_direct = hosted(relayed=False, host_ip="10.9.9.9", host_lan="10.9.9.9")
check(battle_address(lan_relayed["outsider"]) == RELAY_V4,
      "a private-looking relay host should still advertise the relay, got %r" % (lan_relayed["outsider"],))
check(battle_address(lan_direct["outsider"]) == LOBBY_IP,
      "without a relay a private-looking host is still advertised at the lobby, got %r" % (lan_direct["outsider"],))

# --- a joiner on the lobby's LAN is told the relay's LAN address, nobody else is ----
# With line 4 set, a battle sitting at the lobby's public address is on this NAT. A joiner
# whose connection is from a private address is on the same LAN and would have to hairpin
# to reach the public one, so they get line 4 instead; everybody outside gets the public
# address as before. A battle at any other relay is translated for nobody.
def hosted_behind_nat(relay_ip):
    FakeClient._next_session = 1
    root = FakeRoot(lan_ip=RELAY_LAN, online_ip=LOBBY_PUBLIC)
    host = FakeClient(root, "host", compat=("u", "sp", "r"))
    FakeClient(root, "lanjoiner", compat=("u", "sp", "r"), ip="10.42.43.7", local_ip="10.42.43.7")
    FakeClient(root, "lanjoiner_plain", ip="10.42.43.8", local_ip="10.42.43.8")
    FakeClient(root, "outsider", ip=OUTSIDER)
    FakeClient(root, "samewan", ip=HOST_WAN, local_ip="192.168.1.11")
    relayedhost(root, host, relay_ip)
    openbattle(root, host)
    return {name: advertised(client) for name, client in root.usernames.items()}

behind_nat = hosted_behind_nat(LOBBY_PUBLIC)
for who in ("lanjoiner", "lanjoiner_plain"):
    check(battle_address(behind_nat[who]) == RELAY_LAN,
          "%s on the lobby's LAN should be told the relay's LAN address, got %r" % (who, behind_nat[who]))
for who in ("host", "outsider", "samewan"):
    check(battle_address(behind_nat[who]) == LOBBY_PUBLIC,
          "%s should be told the relay's public address, got %r" % (who, behind_nat[who]))

elsewhere = hosted_behind_nat(RELAY_V4)
for who in ("lanjoiner", "lanjoiner_plain", "host", "outsider", "samewan"):
    check(battle_address(elsewhere[who]) == RELAY_V4,
          "a battle at some other relay should be untouched for %s, got %r" % (who, elsewhere[who]))

# natType is the whole reason this needs no client support
for who, line in relayed.items():
    check(line.split(" ")[3] == "0",
          "%s should be told natType 0 for a relayed battle, got %r" % (who, line))

# everything but the address is the same line it always was
def without_address(line):
    fields = line.split(" ")
    fields[5] = "IP"
    return " ".join(fields)

for who in ("host", "samewan", "outsider"):
    check(without_address(relayed[who]) == without_address(direct[who]),
          "only the address should differ for %s:\n  %r\n  %r" % (who, relayed[who], direct[who]))


# --- the address belongs to one battle and no other -------------------------------
root = FakeRoot()
host = FakeClient(root, "twicehost", compat=("u", "sp", "r"))
relayedhost(root, host, RELAY_V4)
openbattle(root, host, "first")
check(battle_address(advertised(host)) == RELAY_V4,
      "the first battle should be relayed, got %r" % (advertised(host),))

host.sent = []
openbattle(root, host, "second")
check(battle_address(advertised(host)) == HOST_WAN,
      "a second battle with no RELAYEDHOST should be advertised at the host, got %r" % (advertised(host),))

# opening a battle while already in one runs LEAVEBATTLE first, which also forgets an
# address. The one that arrived with this OPENBATTLE has to survive that.
root = FakeRoot()
host = FakeClient(root, "rehost", compat=("u", "sp", "r"))
openbattle(root, host, "direct first")
relayedhost(root, host, RELAY_V4)
host.sent = []
openbattle(root, host, "relayed second")
check(battle_address(advertised(host)) == RELAY_V4,
      "reopening as a relay host should advertise the relay, got %r" % (advertised(host),))

# leaving without opening anything drops it, so it cannot attach itself to a later battle
root = FakeRoot()
host = FakeClient(root, "leaverhost", compat=("u", "sp", "r"))
relayedhost(root, host, RELAY_V4)
root.protocol.in_LEAVEBATTLE(host)
check(host.relayed_host_ip is None,
      "LEAVEBATTLE should forget a pending address, got %r" % (host.relayed_host_ip,))
host.sent = []
openbattle(root, host, "after leaving")
check(battle_address(advertised(host)) == HOST_WAN,
      "a forgotten address should not reach a later battle, got %r" % (advertised(host),))


if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: a relayed battle is advertised at the relay for every recipient, and a battle "
      "with no RELAYEDHOST is advertised exactly as before")
