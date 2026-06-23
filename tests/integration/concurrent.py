import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import HOST, PORT
import socket, hashlib, base64, time, threading, sys

TAG = sys.argv[1] if len(sys.argv) > 1 else "r1"
N = 30          # distinct concurrent logins
K = 6           # simultaneous logins of the SAME user (race)
raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()

def recv_until(s, substr, timeout=8):
    s.settimeout(timeout)
    buf = b""
    try:
        while substr.encode() not in buf:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    return buf.decode(errors="replace")

def connect():
    s = socket.create_connection((HOST, PORT))
    recv_until(s, "\n")  # greeting
    return s

def register_and_confirm(user):
    s = connect()
    s.sendall(("REGISTER %s %s\n" % (user, PW)).encode())
    recv_until(s, "\n")
    time.sleep(2.5)  # CONFIRMAGREEMENT requires >2s since register
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    recv_until(s, "AGREEMENTEND")
    s.sendall(b"CONFIRMAGREEMENT\n")
    ok = "LOGININFOEND" in recv_until(s, "LOGININFOEND")
    s.close()
    return ok

def login_once(user):
    # returns ('accepted'|'denied'|'timeout', raw)
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=8)
    s.close()
    if "ACCEPTED" in resp and "LOGININFOEND" in resp:
        return "accepted"
    if "DENIED" in resp:
        return "denied"
    return "timeout"

# ---- setup: register + confirm N+1 users concurrently ----
users = ["pc_%s_%d" % (TAG, i) for i in range(N)]
race_user = "pc_%s_race" % TAG
all_setup = users + [race_user]
results = {}
def _setup(u): results[u] = register_and_confirm(u)
ts = [threading.Thread(target=_setup, args=(u,)) for u in all_setup]
for t in ts: t.start()
for t in ts: t.join()
print("setup confirmed: %d/%d" % (sum(1 for v in results.values() if v), len(all_setup)))

# ---- test 1: N distinct concurrent logins ----
login_results = {}
def _login(u): login_results[u] = login_once(u)
t0 = time.time()
ts = [threading.Thread(target=_login, args=(u,)) for u in users]
for t in ts: t.start()
for t in ts: t.join()
dt = time.time() - t0
acc = sum(1 for v in login_results.values() if v == "accepted")
print("DISTINCT: %d/%d accepted in %.2fs (%s)" % (acc, N, dt, dict((k,login_results[k]) for k in list(login_results)[:0])))
bad = {u:r for u,r in login_results.items() if r != "accepted"}
if bad: print("  non-accepted:", bad)

# ---- test 2: K simultaneous logins of the SAME user (race) ----
# Each thread connects + LOGINs, then KEEPS THE SOCKET OPEN until all K have responded,
# so a winner stays in `usernames` while the losers attempt (otherwise a fast
# connect-login-disconnect would free the name and the next attempt would win sequentially).
race_results = [None]*K
race_socks = [None]*K
barrier = threading.Barrier(K)
def _race(i):
    s = connect()
    race_socks[i] = s
    barrier.wait()  # release all at once
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (race_user, PW)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=8)
    if "ACCEPTED" in resp and "LOGININFOEND" in resp:
        race_results[i] = "accepted"
    elif "DENIED" in resp:
        race_results[i] = "denied"
    else:
        race_results[i] = "timeout"
ts = [threading.Thread(target=_race, args=(i,)) for i in range(K)]
for t in ts: t.start()
for t in ts: t.join()
for s in race_socks:
    try: s.close()
    except: pass
accepted = race_results.count("accepted")
denied = race_results.count("denied")
print("RACE same-user: accepted=%d denied=%d other=%d (results=%s)" % (accepted, denied, K-accepted-denied, race_results))
print("RACE verdict:", "PASS (exactly one winner)" if accepted == 1 else "FAIL")
sys.exit(0 if accepted == 1 else 1)
