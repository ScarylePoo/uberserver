"""
End-to-end tier: a command's #id is on its reply and on nothing else (issue #60).

The id was stored on the Client and never cleared, so every later line to that client carried
it until the next command. Replies sent from a deferred DB callback had the opposite problem,
carrying the id of whatever command the client had sent since, or none.

  unrelated line      after '#5 PING', a SAIDPRIVATE from another player carries no id
  deferred reply      '#7 GETIP <offline>' then '#8 PING' in one write: the GETIP reply is #7
  deferred, no next   '#9 LISTBANS' then a bare 'PING' in one write: the LISTBANS reply is #9
  errors and refusals '#10 NOTACOMMAND' is answered with #10 on both lines

Run: activate the venv, then python3 tests/integration/msgidtest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "m1"
PW = base64.b64encode(hashlib.md5(b"secretpw").digest()).decode()
MOD = "mid_%s_mod" % TAG
TALKER = "mid_%s_talk" % TAG
OFFLINE = "mid_%s_off" % TAG

errors = []
def check(cond, label):
    if not cond: errors.append(label)

def recv_until(s, substr, timeout=8):
    s.settimeout(timeout); buf = b""
    try:
        while substr.encode() not in buf:
            chunk = s.recv(8192)
            if not chunk: break
            buf += chunk
    except socket.timeout: pass
    return buf.decode(errors="replace")

def read_for(s, seconds):
    s.settimeout(seconds); buf = b""
    try:
        while True:
            chunk = s.recv(8192)
            if not chunk: break
            buf += chunk
    except socket.timeout: pass
    return [ln for ln in buf.decode(errors="replace").splitlines() if ln.strip()]

def connect():
    s = socket.create_connection((HOST, PORT)); recv_until(s, "\n"); return s

def register_and_confirm(user):
    s = connect()
    s.sendall(("REGISTER %s %s\n" % (user, PW)).encode()); recv_until(s, "\n")
    time.sleep(2.5)
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode()); recv_until(s, "AGREEMENTEND")
    s.sendall(b"CONFIRMAGREEMENT\n")
    ok = "LOGININFOEND" in recv_until(s, "LOGININFOEND"); s.close(); return ok

def login_keep(user):
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    resp = recv_until(s, "LOGININFOEND")
    if "ACCEPTED" not in resp:
        s.close(); return None
    read_for(s, 0.5)  # drain the rest of the login burst
    return s

def exchange(s, lines, seconds=1.5):
    """Send several commands in one write, so the server reads them back to back."""
    s.sendall(("".join(ln + "\n" for ln in lines)).encode())
    got = read_for(s, seconds)
    print("%-44r -> %r" % (lines, got))
    return got


for u in (MOD, TALKER, OFFLINE):
    if not register_and_confirm(u):
        print("ABORT: could not register %s" % u); sys.exit(1)

conn = pymysql.connect(**DB_KWARGS); cur = conn.cursor()
cur.execute("UPDATE users SET access='mod' WHERE username=%s", (MOD,))
cur.execute("SELECT last_ip FROM users WHERE username=%s", (OFFLINE,)); OFFLINE_IP = cur.fetchone()[0]
conn.close()

mod = login_keep(MOD)
talker = login_keep(TALKER)
if not (mod and talker):
    print("ABORT: test users could not log in"); sys.exit(1)
read_for(mod, 0.5)  # the ADDUSER for TALKER, who logged in after MOD

# the reply to an id'd command carries the id
got = exchange(mod, ["#5 PING"])
check(got == ["#5 PONG"], "'#5 PING' should be answered '#5 PONG', got %r" % got)

# a line that has nothing to do with that command does not
talker.sendall(("SAYPRIVATE %s hello there\n" % MOD).encode())
got = read_for(mod, 1.0)
print("%-44r -> %r" % ("(SAYPRIVATE from %s)" % TALKER, got))
check("SAIDPRIVATE %s hello there" % TALKER in got,
      "a private message from another player should arrive with no id, got %r" % got)

# a deferred reply carries the id of the command that asked, not of a later one
got = exchange(mod, ["#7 GETIP %s" % OFFLINE, "#8 PING"])
check("#8 PONG" in got, "'#8 PING' should be answered '#8 PONG', got %r" % got)
check("#7 SERVERMSG <%s> was recently bound to %s" % (OFFLINE, OFFLINE_IP) in got,
      "the deferred GETIP reply should carry #7, got %r" % got)

# and still carries it when the next command had no id at all
got = exchange(mod, ["#9 LISTBANS", "PING"])
check("PONG" in got, "a bare PING should be answered with a bare PONG, got %r" % got)
check("#9 SERVERMSG Banlist is empty" in got,
      "the deferred LISTBANS reply should carry #9, got %r" % got)

# refusals from _handle itself carry the id on both lines
got = exchange(mod, ["#10 NOTACOMMAND"])
check(len(got) == 2 and all(ln.startswith("#10 ") for ln in got),
      "both lines refusing '#10 NOTACOMMAND' should carry #10, got %r" % got)

# after all of that, an unrelated line is still clean
talker.sendall(("SAYPRIVATE %s second\n" % MOD).encode())
got = read_for(mod, 1.0)
print("%-44r -> %r" % ("(SAYPRIVATE from %s)" % TALKER, got))
check("SAIDPRIVATE %s second" % TALKER in got,
      "a later private message should still arrive with no id, got %r" % got)

mod.close(); talker.close()


if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
