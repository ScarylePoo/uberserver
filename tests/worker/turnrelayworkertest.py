"""
Worker-unit test for relay hosting (issue #27): the server_turn.txt parser and the
TURNCREDENTIALS command. Runs fully in-process, no server and no DB needed.

The wire contract is fixed by the shipped coilbox client: a success reply is exactly four
space-separated fields and the client silently drops the line if any of them is empty or
contains a space, so the field checks here are correctness checks, not tidiness.

The credential itself is draft-uberti-behave-turn-rest-00. This test recomputes the HMAC
independently of the server code, so a change to how the password is derived fails here
rather than at a coturn nobody is watching.

Run: activate the venv, then python3 tests/worker/turnrelayworkertest.py
"""
import os as _os, sys as _sys
_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir))
_sys.path[:0] = [_ROOT, _os.path.join(_ROOT, "protocol"),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
import base64
import hashlib
import hmac
import sys
import time

from DataHandler import TURN_DEFAULT_TTL, parse_turn_config
from protocol import Protocol as ProtocolModule

URI = "turn:relay.example.org:3478"
SECRET = "a_long_random_string"
USER_ID = 4242

errors = []
def check(cond, label):
    if not cond: errors.append(label)


class FakeRoot:
    """Just the parts of DataHandler that Protocol touches for this command."""
    def __init__(self, uri=URI, secret=SECRET, ttl=TURN_DEFAULT_TTL):
        self.turn_uri = uri
        self.turn_secret = secret
        self.turn_ttl = ttl
        self.recent_turn_credentials = {}
        self.SayHooks = None
    def turn_enabled(self):
        return bool(self.turn_uri and self.turn_secret)
    def getUserDB(self): pass
    def getVerificationDB(self): pass
    def getBanDB(self): pass
    def getContentDB(self): pass


class FakeClient:
    def __init__(self):
        self.sent = []
        self.user_id = USER_ID
        self.session_id = 1
        self.username = "relaytester"
    def Send(self, data, command=None):
        self.sent.append(data)


def ask(root):
    """Send TURNCREDENTIALS and hand back the single reply line."""
    client = FakeClient()
    ProtocolModule.Protocol(root).in_TURNCREDENTIALS(client)
    return client.sent[-1] if client.sent else ""


def compflags(root):
    client = FakeClient()
    ProtocolModule.Protocol(root).in_LISTCOMPFLAGS(client)
    return client.sent[-1].split()[1:]


# --- the config parser -------------------------------------------------------------
check(parse_turn_config([URI, SECRET]) == (URI, SECRET, TURN_DEFAULT_TTL),
      "two lines should parse to (uri, secret, default ttl), got %r" % (parse_turn_config([URI, SECRET]),))
check(TURN_DEFAULT_TTL == 43200,
      "the default lifetime should be 12 hours (43200s), got %r" % (TURN_DEFAULT_TTL,))
check(parse_turn_config([URI, SECRET, "60"])[2] == 60,
      "line 3 should set the lifetime")
check(parse_turn_config(["# a comment", "", URI, SECRET]) == (URI, SECRET, TURN_DEFAULT_TTL),
      "blank and commented lines should be skipped")

def rejects(lines, label):
    try:
        parse_turn_config(lines)
    except ValueError:
        return
    errors.append("malformed config should raise ValueError: %s" % label)

rejects([], "empty file")
rejects([URI], "URI but no secret")
rejects(["turn:relay example.org:3478", SECRET], "URI containing a space")
rejects([URI, SECRET, "twelve hours"], "non-numeric lifetime")
rejects([URI, SECRET, "0"], "zero lifetime")
rejects([URI, SECRET, "-1"], "negative lifetime")


# --- a successful request ----------------------------------------------------------
root = FakeRoot()
before = int(time.time())
reply = ask(root)
after = int(time.time())

parts = reply.split(" ")
check(parts[0] == "TURNCREDENTIALS", "success reply should start with TURNCREDENTIALS, got %r" % (reply,))
check(len(parts) == 5,
      "reply should be the command plus exactly 4 fields, got %d fields: %r" % (len(parts) - 1, reply))

if len(parts) == 5:
    _, uri, username, password, ttl_field = parts
    check(uri == URI, "field 1 should be the configured URI, got %r" % (uri,))
    for name, field in (("uri", uri), ("username", username), ("password", password)):
        check(field != "", "%s must not be empty" % name)
        check(not any(c.isspace() for c in field), "%s must not contain whitespace: %r" % (name, field))

    # ttl: a plain base-10 integer, and the default lifetime
    check(ttl_field == str(TURN_DEFAULT_TTL),
          "ttl field should be %d, got %r" % (TURN_DEFAULT_TTL, ttl_field))
    check(ttl_field.isdigit() and int(ttl_field) == TURN_DEFAULT_TTL,
          "ttl field must parse as a plain integer, got %r" % (ttl_field,))

    # username: "<unix expiry>:<account id>", expiring one lifetime from now
    check(username.count(":") == 1, "username should be '<expiry>:<account id>', got %r" % (username,))
    expiry_str, uid_str = username.split(":")
    check(uid_str == str(USER_ID), "username should carry the account id, got %r" % (uid_str,))
    expiry = int(expiry_str)
    check(before + TURN_DEFAULT_TTL <= expiry <= after + TURN_DEFAULT_TTL,
          "expiry should be now + %ds, got %d (now was %d..%d)" % (TURN_DEFAULT_TTL, expiry, before, after))

    # password: recomputed here rather than trusted
    expected = base64.b64encode(
        hmac.new(SECRET.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()).decode("utf-8")
    check(password == expected,
          "password should be base64(hmac_sha1(secret, username)), got %r, expected %r" % (password, expected))
    check(password != base64.b64encode(
        hmac.new(b"wrong_secret", username.encode("utf-8"), hashlib.sha1).digest()).decode("utf-8"),
        "password must depend on the configured secret")


# --- the lifetime is the operator's, not a constant --------------------------------
short = FakeRoot(ttl=90)
before = int(time.time())
parts = ask(short).split(" ")
check(len(parts) == 5 and parts[4] == "90", "a configured lifetime should reach the wire, got %r" % (parts,))
if len(parts) == 5:
    check(int(parts[2].split(":")[0]) - before == 90,
          "expiry should move with the configured lifetime, got %r" % (parts[2],))


# --- rate limit --------------------------------------------------------------------
root = FakeRoot()
burst = [ask(root) for _ in range(4)]
check(all(r.startswith("TURNCREDENTIALS ") for r in burst[:3]),
      "the first 3 requests should succeed, got %r" % (burst[:3],))
check(burst[3].startswith("TURNCREDENTIALSFAILED "),
      "the 4th request in a burst should be refused, got %r" % (burst[3],))
check(len(burst[3].split(" ", 1)[1].strip()) > 0,
      "a refusal should carry a reason a person can act on, got %r" % (burst[3],))
check(root.recent_turn_credentials[USER_ID] == 3,
      "a refused request should not burn a further slot, counter is %r" % (root.recent_turn_credentials,))

# the counter decays (server.py runs decrement_dict on a loop), and then it works again
root.recent_turn_credentials[USER_ID] -= 1
check(ask(root).startswith("TURNCREDENTIALS "), "a decayed counter should allow another credential")


# --- no relay configured -----------------------------------------------------------
for label, root in (("no uri", FakeRoot(uri=None)),
                    ("no secret", FakeRoot(secret=None)),
                    ("neither", FakeRoot(uri=None, secret=None))):
    reply = ask(root)
    check(reply.startswith("TURNCREDENTIALSFAILED "),
          "%s should fail cleanly, got %r" % (label, reply))


# --- a URI that would shift the fields is refused, not sent ------------------------
# parse_turn_config rejects this at startup, so reaching it means the config was set some
# other way. Either way a malformed success line must never go out.
reply = ask(FakeRoot(uri="turn:relay example.org:3478"))
check(reply.startswith("TURNCREDENTIALSFAILED "),
      "a URI containing a space should be refused, not shifted onto the wire, got %r" % (reply,))


# --- LISTCOMPFLAGS advertises 'r' only when there is a relay -----------------------
configured = compflags(FakeRoot())
unconfigured = compflags(FakeRoot(uri=None, secret=None))
check("r" in configured, "COMPFLAGS should advertise 'r' when a relay is configured, got %r" % (configured,))
check("r" not in unconfigured, "COMPFLAGS should omit 'r' when no relay is configured, got %r" % (unconfigured,))
check([f for f in configured if f != "r"] == unconfigured,
      "only 'r' should differ between the two, got %r vs %r" % (configured, unconfigured))
check("r" in ProtocolModule.flag_map and "r" in ProtocolModule.optional_flags,
      "'r' must stay a known optional flag on every server, whatever the config")


if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: server_turn.txt parses, TURNCREDENTIALS mints a valid credential, and 'r' is advertised only when configured")
