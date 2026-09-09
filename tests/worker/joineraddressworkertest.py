"""
Worker-unit test for joiner addresses (issue #28): the CLIENTIP line a relay host is sent
when somebody joins its battle. Runs the real Battle.joinBattle in-process against fake
clients, no server and no DB needed.

The property that matters most is the negative one. A host that knows nothing about relays
must see exactly the conversation it saw before, so every case here compares the host's whole
received stream against a baseline host, rather than only asserting that CLIENTIP is absent.

Run: activate the venv, then python3 tests/worker/joineraddressworkertest.py
"""
import os as _os, sys as _sys
_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir))
_sys.path[:0] = [_ROOT, _os.path.join(_ROOT, "protocol"),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
import sys

from protocol import Protocol as ProtocolModule
from protocol.Battle import Battle

PROXY = "10.0.0.1"

errors = []
def check(cond, label):
    if not cond: errors.append(label)


class FakeRoot:
    def __init__(self, relay=True):
        self.turn_uri = "turn:relay.example.org:3478" if relay else None
        self.turn_lan_ip = None
        self.turn_secret = "a_long_random_string" if relay else None
        self.trusted_proxies = set([PROXY])
        self.sessions = {}
        self.broadcasts = []
        self.SayHooks = None
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
    def __init__(self, root, username, compat=("u", "sp"), ip="203.0.113.7", local_ip=None):
        self.sent = []
        self.username = username
        self.compat = set(compat)
        self.ip_address = ip
        self.local_ip = local_ip if local_ip else ip
        self.session_id = FakeClient._next_session
        FakeClient._next_session += 1
        self.static = True  # skips Channel.recordUse, which wants a db
        self.channels = set()
        self.battle_bots = {}
        self.scriptPassword = None
        self.current_battle = None
        self.hostport = None
        self.udpport = None
        self.battlestatus = {'ready':'0', 'id':'0000', 'ally':'0000', 'mode':'0',
                             'sync':'00', 'side':'00', 'handicap':'0000000'}
        self.teamcolor = '0'
        root.sessions[self.session_id] = self
    def Send(self, data, command=None):
        self.sent.append(data)


def open_battle(root, host):
    battle = Battle(root, "__battle__0")
    battle.battle_id = 0
    battle.hashcode = 0
    battle.natType = 0
    battle.host = host.session_id
    battle.joinBattle(host)
    host.sent = []
    return battle


def join(root, battle, joiner):
    """Join a battle and hand back what the host received during that join."""
    host = root.clientFromSession(battle.host)
    before = len(host.sent)
    battle.joinBattle(joiner)
    return host.sent[before:]


def clientips(lines):
    return [line for line in lines if line.split(" ")[0] == "CLIENTIP"]


# --- a relay host is told each joiner's address ------------------------------------
root = FakeRoot()
host = FakeClient(root, "relayhost", compat=("u", "sp", "r"))
battle = open_battle(root, host)

first = join(root, battle, FakeClient(root, "joiner1"))
check(clientips(first) == ["CLIENTIP joiner1 203.0.113.7"],
      "a relay host should be told the joiner's address, got %r" % (first,))

# ahead of JOINEDBATTLE: the host has the address in hand by the time it hears about the join
if clientips(first):
    joined = [i for i, line in enumerate(first) if line.startswith("JOINEDBATTLE ")]
    check(joined and first.index(clientips(first)[0]) < joined[0],
          "CLIENTIP should arrive before JOINEDBATTLE, got %r" % (first,))

# a second joiner, mid-game or spectating, is the same case: joins run through one path
second = join(root, battle, FakeClient(root, "joiner2", ip="203.0.113.9"))
check(clientips(second) == ["CLIENTIP joiner2 203.0.113.9"],
      "every later joiner should get the same treatment, got %r" % (second,))

# the host's own join says nothing about the host
solo_root = FakeRoot()
solo_host = FakeClient(solo_root, "lonehost", compat=("u", "sp", "r"))
solo = Battle(solo_root, "__battle__1")
solo.battle_id = 1
solo.hashcode = 0
solo.natType = 0
solo.host = solo_host.session_id
solo.joinBattle(solo_host)
check(clientips(solo_host.sent) == [],
      "a host joining its own battle should not be sent its own address, got %r" % (solo_host.sent,))


# --- the address is the public one, not the LAN one -------------------------------
root = FakeRoot()
host = FakeClient(root, "relayhost", compat=("u", "sp", "r"))
battle = open_battle(root, host)

lan = join(root, battle, FakeClient(root, "lanjoiner", ip="198.51.100.4", local_ip="192.168.1.50"))
check(clientips(lan) == ["CLIENTIP lanjoiner 198.51.100.4"],
      "the joiner's public address should be sent, not local_ip, got %r" % (lan,))

# behind a trusted proxy the socket address is the proxy's, and the real one is local_ip
proxied = join(root, battle, FakeClient(root, "proxiedjoiner", ip=PROXY, local_ip="198.51.100.9"))
check(clientips(proxied) == ["CLIENTIP proxiedjoiner 198.51.100.9"],
      "behind a trusted proxy the joiner's own address should be sent, got %r" % (proxied,))


# --- nobody else's conversation changes -------------------------------------------
# The baseline is a host with no 'r' on a server with no relay, which is every deployment
# before this change. Compare the whole stream, not just the absence of CLIENTIP.
def conversation(relay, host_compat):
    root = FakeRoot(relay=relay)
    FakeClient._next_session = 1  # session ids appear in nothing sent, but keep runs identical
    host = FakeClient(root, "host", compat=host_compat)
    battle = open_battle(root, host)
    return join(root, battle, FakeClient(root, "joiner1"))

baseline = conversation(relay=False, host_compat=("u", "sp"))
check(clientips(baseline) == [], "the baseline host should get no CLIENTIP, got %r" % (baseline,))

no_flag = conversation(relay=True, host_compat=("u", "sp"))
check(no_flag == baseline,
      "a host that did not ask for relay support should see an unchanged conversation:\n  %r\n  %r"
      % (no_flag, baseline))

no_relay = conversation(relay=False, host_compat=("u", "sp", "r"))
check(no_relay == baseline,
      "a server with no relay should send nothing new even to a relay host:\n  %r\n  %r"
      % (no_relay, baseline))

relayed = conversation(relay=True, host_compat=("u", "sp", "r"))
check(relayed == ["CLIENTIP joiner1 203.0.113.7"] + baseline,
      "a relay host's conversation should be the baseline plus one line, got:\n  %r\n  %r"
      % (relayed, baseline))


if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: CLIENTIP reaches a relay host with the joiner's public address, and nobody else's conversation changes")
