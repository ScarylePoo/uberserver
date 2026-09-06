"""
End-to-end tier: a rejected command is answered with FAILED as well as SERVERMSG (issue #51).

The three rejection paths in Protocol._handle told the client in a SERVERMSG written for a
person, so a client that wanted to stop waiting on the command it just sent had to match an
English sentence. Nothing pins that wording: three shipped uberservers word the same rejection
differently, which issue #51 records. This checks the structured line is there too.

All three paths are reachable before login, which is what this drives:
  incorrect arguments  LOGIN with none, the case issue #51 measured against live servers
  unknown command      a command that does not exist
  insufficient rights  SAY, which needs the 'user' access level a fresh client does not have

The SERVERMSG assertions matter as much as the FAILED ones. This is an addition, so every
existing client has to see the byte-for-byte conversation it saw before.

Run: activate the venv, then python3 tests/integration/failedonrejectedtest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import HOST, PORT
import socket, time, sys

errors = []
def check(cond, label):
    if not cond: errors.append(label)


class Client:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT))
        self.buf = ""
        self.read_for(0.5)  # drain the greeting

    def send(self, line):
        self.s.sendall((line + "\n").encode())

    def read_for(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.s.settimeout(max(0.05, deadline - time.time()))
            try:
                chunk = self.s.recv(8192)
            except socket.timeout:
                break
            if not chunk:
                break
            self.buf += chunk.decode(errors="replace")
        out, self.buf = self.buf, ""
        return out

    def close(self):
        try: self.s.close()
        except OSError: pass


def reject(line):
    """Send one line to a fresh connection and hand back what came back, split into lines."""
    c = Client()
    c.send(line)
    got = c.read_for(1.0)
    c.close()
    return [ln for ln in got.splitlines() if ln.strip()]


def tags(failed_line):
    """Parse the tag payload of a FAILED line into a dict, as _dictToTags wrote it."""
    payload = failed_line.split(" ", 1)[1] if " " in failed_line else ""
    out = {}
    for pair in payload.split("\t"):
        key, sep, value = pair.partition("=")
        if sep:
            out[key] = value
    return out


CASES = [
    ("LOGIN", "LOGIN", "LOGIN failed. Incorrect arguments."),
    ("NOTACOMMAND args here", "NOTACOMMAND", "NOTACOMMAND failed. Unknown command. (args='args here')"),
    ("SAY #main hello", "SAY", "SAY failed. Insufficient rights."),
]

for sent, command, servermsg in CASES:
    lines = reject(sent)
    print("%-24s -> %r" % (sent, lines))

    # the sentence a person reads is unchanged
    check(("SERVERMSG " + servermsg) in lines,
          "%r should still answer %r, got %r" % (sent, "SERVERMSG " + servermsg, lines))

    # and the structured line a client reads is now there too
    failed = [ln for ln in lines if ln.startswith("FAILED ")]
    check(len(failed) == 1,
          "%r should answer with exactly one FAILED, got %r" % (sent, lines))
    if len(failed) != 1:
        continue

    parsed = tags(failed[0])
    check(parsed.get("cmd") == command,
          "%r should name cmd=%s, got %r" % (sent, command, failed[0]))
    check(parsed.get("msg", "") != "",
          "%r should carry a reason in msg, got %r" % (sent, failed[0]))
    check(set(parsed) == {"cmd", "msg"},
          "%r should carry only cmd and msg, got %r" % (sent, failed[0]))

    # the FAILED reason should not repeat the command, which is already in the cmd tag
    check(not parsed.get("msg", "").startswith(command + " failed."),
          "the msg tag should not repeat the command name, got %r" % (failed[0],))


# A command name carrying a tab, which is the one character that means something inside a
# FAILED frame. Commands are split out of the line on spaces, so a tab does reach the tag
# writer. What comes back is 'cmd=NOTA\tCOMMAND', and a tag parser reads that as cmd=NOTA with
# a trailing fragment it drops, because out_FAILED writes cmd last. So the frame stays sound
# and the echo is truncated. Nothing that is a real command can contain a tab.
lines = reject("NOTA\tCOMMAND x")
print("%-24s -> %r" % ("NOTA<tab>COMMAND x", lines))
failed = [ln for ln in lines if ln.startswith("FAILED ")]
check(len(failed) == 1, "a tabbed command should still answer one FAILED, got %r" % (lines,))
if failed:
    parsed = tags(failed[0])
    check(set(parsed) == {"cmd", "msg"},
          "a tabbed command must not inject a third tag, got %r -> %r" % (failed[0], parsed))
    check(parsed.get("cmd", "").startswith("NOTA"),
          "a tabbed command should still be named back, got %r" % (failed[0],))


# negative control: a command that is accepted answers neither
c = Client()
c.send("PING")
pong = c.read_for(1.0)
c.close()
print("%-24s -> %r" % ("PING", pong.splitlines()))
check("PONG" in pong, "PING should still be answered with PONG, got %r" % (pong,))
check("FAILED" not in pong, "an accepted command should not produce a FAILED, got %r" % (pong,))
check("SERVERMSG" not in pong, "an accepted command should not produce a SERVERMSG, got %r" % (pong,))


if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
