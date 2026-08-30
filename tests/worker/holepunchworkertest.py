"""
Worker-unit test for hole-punched battles (issue #35): the CLIENTIPPORT line a host is sent
so it can punch a hole to each joiner. Runs the real NATServer handler, Protocol._udp_packet
and Battle.joinBattle in-process against fake clients. No server and no DB needed.

Issue #35 found two send sites that look the host up in a username-keyed dictionary using
something that is not a username. Reading further, the path is dead before either is reached:

  NATServer.handle      the datagram is read as bytes and tested against a str-keyed dict,
                        so the username never matches and _udp_packet is never called
  NATServer.handle      Client has no _protocol attribute (one hit in the whole repo)
  Protocol._udp_packet  a session id used as a username key, and SendBattle, which no class
                        defines
  Battle.joinBattle     a client object used as a username key

client.udpport is written only inside _udp_packet, so with the front door shut it stays 0 and
the joinBattle site is unreachable too. That is why neither KeyError has ever been reported.

This drives the whole chain, front door first, so a fix to only part of it still fails.

Run: activate the venv, then python3 tests/worker/holepunchworkertest.py
"""
import os as _os, sys as _sys
_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir))
_sys.path[:0] = [_ROOT, _os.path.join(_ROOT, "protocol"),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
import sys

from twisted.internet import reactor

import NATServer
from protocol import Protocol as ProtocolModule
from protocol.Battle import Battle

errors = []
def check(cond, label):
    if not cond: errors.append(label)

def guard(label, fn, *a, **kw):
    """Run something that is expected to work, recording a raise as a failure not a crash.

    Every defect here surfaces as an exception, so the run has to survive one to report the
    rest of them."""
    try:
        return fn(*a, **kw)
    except Exception as e:
        errors.append("%s raised %s: %s" % (label, type(e).__name__, e))
        return None


class FakeRoot:
    def __init__(self):
        self.usernames = {}
        self.sessions = {}
        self.battles = {}
        self.admin_messages = []
        self.broadcasts = []
        self.SayHooks = None
        self.protocol = ProtocolModule.Protocol(self)
    def clientFromSession(self, session_id):
        return self.sessions.get(session_id)
    def clientFromUsername(self, username, fromdb=False):
        return self.usernames.get(username)
    def clientFromID(self, user_id, fromdb=False):
        return None
    def admin_broadcast(self, message):
        self.admin_messages.append(message)
    def broadcast(self, message, chan=None, ignore=set(), state=None, flag=None, not_flag=None):
        self.broadcasts.append(message)
    def getUserDB(self): pass
    def getVerificationDB(self): pass
    def getBanDB(self): pass
    def getContentDB(self): pass


class FakeClient:
    _next_session = 1
    def __init__(self, root, username, ip="203.0.113.7", local_ip=None):
        self.sent = []
        self.username = username
        self.compat = set(("u", "sp"))
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
        self.udpport = 0  # Client.py's own starting value
        self.battlestatus = {'ready':'0', 'id':'0000', 'ally':'0000', 'mode':'0',
                             'sync':'00', 'side':'00', 'handicap':'0000000'}
        self.teamcolor = '0'
        root.usernames[username] = self
        root.sessions[self.session_id] = self
    def Send(self, data, command=None):
        self.sent.append(data)


class FakeUDPSocket:
    """Stands in for the datagram socket NATServer replies on."""
    def __init__(self):
        self.replies = []
    def sendto(self, data, addr):
        self.replies.append((data, addr))


def datagram(root, payload, addr):
    """Drive one UDP packet through the real NATServer handler.

    The handler runs on the UDP server's own thread and hands _udp_packet to the reactor,
    because it mutates client state and writes to a transport. runUntilCurrent drains that
    queue here, standing in for the reactor loop that would normally run it."""
    sock = FakeUDPSocket()
    guard("a UDP probe %r" % payload, NATServer.handler, (payload, sock), addr, None, root)
    reactor.runUntilCurrent()
    return sock


def open_battle(root, host, natType):
    battle = Battle(root, "__battle__0")
    battle.battle_id = 0
    battle.hashcode = 0
    battle.natType = natType
    battle.host = host.session_id
    battle.joinBattle(host)
    host.current_battle = battle.battle_id
    root.battles[battle.battle_id] = battle
    host.sent = []
    return battle


def clientipports(lines):
    return [line for line in lines if line.split(" ")[0] == "CLIENTIPPORT"]


# --- the front door: a UDP probe naming a logged-in user reaches _udp_packet ---------
root = FakeRoot()
host = FakeClient(root, "punchhost", ip="198.51.100.1")
battle = open_battle(root, host, natType=1)
joiner = FakeClient(root, "punchjoiner", ip="203.0.113.7")
guard("joining a hole-punched battle", battle.joinBattle, joiner)
joiner.current_battle = battle.battle_id
host.sent = []
joiner.sent = []

sock = datagram(root, b"punchjoiner\n", ("203.0.113.7", 40000))
check(sock.replies and sock.replies[0][0] == b"PONG",
      "the probe should still be answered with PONG, got %r" % (sock.replies,))
check(joiner.udpport == 40000,
      "the probe should record the joiner's UDP port, got %r" % (joiner.udpport,))
check("UDPSOURCEPORT 40000" in joiner.sent,
      "the joiner should be told its own source port, got %r" % (joiner.sent,))
check(clientipports(host.sent) == ["CLIENTIPPORT punchjoiner 203.0.113.7 40000"],
      "the host should be told the joiner's address and port, got %r" % (host.sent,))


# --- the work is handed to the reactor, not done on the UDP thread ------------------
# server.py runs the NAT server on its own thread. _udp_packet writes to a client's transport
# and mutates client state, so doing it there would race the reactor. Nothing should have
# happened until the reactor queue is drained.
root = FakeRoot()
host = FakeClient(root, "threadhost", ip="198.51.100.1")
battle = open_battle(root, host, natType=1)
prober = FakeClient(root, "threadjoiner", ip="203.0.113.7")
guard("joining a hole-punched battle", battle.joinBattle, prober)
prober.current_battle = battle.battle_id
host.sent = []

reactor.runUntilCurrent()  # start from an empty queue
sock = FakeUDPSocket()
NATServer.handler((b"threadjoiner\n", sock), ("203.0.113.7", 40000), None, root)
check(sock.replies and sock.replies[0][0] == b"PONG",
      "PONG is answered on the UDP thread, got %r" % (sock.replies,))
check(host.sent == [] and prober.udpport == 0,
      "nothing should touch client state on the UDP thread, got %r / %r"
      % (host.sent, prober.udpport))
reactor.runUntilCurrent()
check(clientipports(host.sent) == ["CLIENTIPPORT threadjoiner 203.0.113.7 40000"],
      "the reactor should then do the work, got %r" % (host.sent,))


# --- a probe naming nobody, and one from a spoofed address --------------------------
root = FakeRoot()
host = FakeClient(root, "punchhost2", ip="198.51.100.1")
battle = open_battle(root, host, natType=1)
joiner = FakeClient(root, "punchjoiner2", ip="203.0.113.7")
guard("joining a hole-punched battle", battle.joinBattle, joiner)
joiner.current_battle = battle.battle_id
host.sent = []

datagram(root, b"nosuchuser\n", ("203.0.113.7", 40000))
check(host.sent == [], "a probe naming nobody should tell the host nothing, got %r" % (host.sent,))

datagram(root, b"punchjoiner2\n", ("192.0.2.99", 40000))
check(host.sent == [],
      "a probe from an address the user does not hold should tell the host nothing, got %r"
      % (host.sent,))
check(len(root.admin_messages) == 1 and "NAT spoof" in root.admin_messages[0],
      "a spoofed probe should be reported to moderators, got %r" % (root.admin_messages,))

# the host's own probe is not echoed back to itself
host.sent = []
datagram(root, b"punchhost2\n", ("198.51.100.1", 41000))
check(clientipports(host.sent) == [],
      "a host's own probe should not send it its own address, got %r" % (host.sent,))


# --- joining a hole-punched battle once the joiner already has a port ---------------
# The second joiner arrives after its own probe, which is the case Battle.joinBattle covers.
root = FakeRoot()
host = FakeClient(root, "punchhost3", ip="198.51.100.1")
battle = open_battle(root, host, natType=1)
late = FakeClient(root, "latejoiner", ip="203.0.113.30")
late.udpport = 40500
before = len(host.sent)
guard("joining with a known UDP port", battle.joinBattle, late)
during_join = host.sent[before:]
check(clientipports(during_join) == ["CLIENTIPPORT latejoiner 203.0.113.30 40500"],
      "joining with a known port should tell the host, got %r" % (during_join,))

# a joiner that has not probed yet has no port to send
quiet = FakeClient(root, "quietjoiner", ip="203.0.113.31")
before = len(host.sent)
guard("joining with no UDP port", battle.joinBattle, quiet)
check(clientipports(host.sent[before:]) == [],
      "a joiner with no UDP port yet should send nothing, got %r" % (host.sent[before:],))


# a host that has probed and then opens its own battle is not sent its own address
root = FakeRoot()
selfhost = FakeClient(root, "selfhost", ip="198.51.100.2")
selfhost.udpport = 40900
selfbattle = Battle(root, "__battle__9")
selfbattle.battle_id = 9
selfbattle.hashcode = 0
selfbattle.natType = 1
selfbattle.host = selfhost.session_id
guard("a host joining its own battle", selfbattle.joinBattle, selfhost)
check(clientipports(selfhost.sent) == [],
      "a host joining its own battle should not be sent its own address, got %r"
      % (selfhost.sent,))

# --- a battle that does not hole punch is untouched ---------------------------------
root = FakeRoot()
host = FakeClient(root, "directhost", ip="198.51.100.1")
battle = open_battle(root, host, natType=0)
direct = FakeClient(root, "directjoiner", ip="203.0.113.7")
direct.udpport = 40000
before = len(host.sent)
guard("joining a natType 0 battle", battle.joinBattle, direct)
check(clientipports(host.sent[before:]) == [],
      "a natType 0 battle should send no CLIENTIPPORT, got %r" % (host.sent[before:],))

# and neither is a probe from somebody in one
direct.current_battle = battle.battle_id
host.sent = []
datagram(root, b"directjoiner\n", ("203.0.113.7", 40000))
check(clientipports(host.sent) == [],
      "a probe in a natType 0 battle should send no CLIENTIPPORT, got %r" % (host.sent,))
check(direct.udpport == 40000,
      "the port should still be recorded for a direct battle, got %r" % (direct.udpport,))


# --- a probe from somebody in no battle at all --------------------------------------
root = FakeRoot()
lone = FakeClient(root, "lonely", ip="203.0.113.50")
datagram(root, b"lonely\n", ("203.0.113.50", 40000))
check(lone.udpport == 40000,
      "a user in no battle should still have its port recorded, got %r" % (lone.udpport,))
check("UDPSOURCEPORT 40000" in lone.sent,
      "a user in no battle should still be told its source port, got %r" % (lone.sent,))


if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: a hole-punched battle tells its host each joiner's address and port")
