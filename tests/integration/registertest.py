"""
e2e test for PR A (OPTIMIZATIONS 3.1): in_REGISTER two-hop async flow + the unique-constraint
race guard. Needs the server running on :8200 against uberserver_test.

Covers: functional accept + exactly one row; syntax denials; sequential duplicate -> denied;
and the headline CONCURRENT same-name race: N sockets REGISTER the same fresh name at once;
the DB unique index must yield exactly one row and exactly one REGISTRATIONACCEPTED, the rest
REGISTRATIONDENIED (do_register_insert catching IntegrityError).

Run: source venv/bin/activate; python3 .registertest.py
"""
import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, threading, sys
import pymysql

TAG = sys.argv[1] if len(sys.argv) > 1 else "rg1"
N = 8  # concurrent same-name registrants
raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()
DB = DB_KWARGS

def recv_until(s, substrs, timeout=6):
    if isinstance(substrs, str): substrs = [substrs]
    s.settimeout(timeout); buf = b""
    try:
        while not any(sub.encode() in buf for sub in substrs):
            chunk = s.recv(8192)
            if not chunk: break
            buf += chunk
    except socket.timeout: pass
    return buf.decode(errors="replace")

def connect():
    s = socket.create_connection((HOST, PORT)); recv_until(s, "\n"); return s

def do_register(user, pw=PW):
    s = connect()
    s.sendall(("REGISTER %s %s\n" % (user, pw)).encode())
    resp = recv_until(s, ["REGISTRATIONACCEPTED", "REGISTRATIONDENIED"])
    s.close()
    if "REGISTRATIONACCEPTED" in resp: return "ok"
    if "REGISTRATIONDENIED" in resp: return resp.strip()
    return "no-reply"

conn = pymysql.connect(**DB); cur = conn.cursor()
def row_count(name):
    cur.execute("SELECT COUNT(*) FROM users WHERE username=%s", (name,)); return cur.fetchone()[0]
def wipe(*names):
    if names:
        fmt = ",".join(["%s"] * len(names))
        cur.execute("DELETE FROM users WHERE username IN (%s)" % fmt, names)

errors = []
def check(cond, label):
    if not cond: errors.append(label)

func = "reg_%s_func" % TAG
dup = "reg_%s_dup" % TAG
racers = ["reg_%s_race" % TAG]  # SAME name for all concurrent registrants
wipe(func, dup, racers[0])

# ===================== FUNCTIONAL =====================
r = do_register(func)
check(r == "ok", "fresh register should be ACCEPTED, got %r" % (r,))
check(row_count(func) == 1, "fresh register should create one row (got %d)" % row_count(func))

# bad password syntax (empty) -> denied, no row
badpw = "reg_%s_badpw" % TAG; wipe(badpw)
rb = do_register(badpw, pw="")
check(rb != "ok" and "REGISTRATIONDENIED" in rb, "empty password should be denied, got %r" % (rb,))
check(row_count(badpw) == 0, "denied register must not create a row")

# ===================== SEQUENTIAL DUPLICATE =====================
r2 = do_register(func)
check(r2 != "ok" and "already in use" in r2, "duplicate username should be denied as in-use, got %r" % (r2,))
check(row_count(func) == 1, "duplicate register must not add a second row (got %d)" % row_count(func))

# ===================== CONCURRENT SAME-NAME RACE =====================
name = racers[0]
wipe(name)
results = [None] * N
def reg_race(i):
    results[i] = do_register(name)
ts = [threading.Thread(target=reg_race, args=(i,)) for i in range(N)]
t0 = time.time()
for t in ts: t.start()
for t in ts: t.join()
dt = time.time() - t0
accepted = sum(1 for r in results if r == "ok")
denied = sum(1 for r in results if r != "ok" and r and "REGISTRATIONDENIED" in r)
rows = row_count(name)
print("concurrent same-name: %d threads in %.2fs -> accepted=%d denied=%d db_rows=%d" % (N, dt, accepted, denied, rows))
check(rows == 1, "same-name race must leave exactly one row (got %d)" % rows)
check(accepted == 1, "same-name race must accept exactly one (got %d)" % accepted)
check(accepted + denied == N, "every concurrent register should get a definitive reply (acc=%d den=%d of %d)" % (accepted, denied, N))

wipe(func, dup, name, badpw)
conn.close()
if errors:
    print("FAIL (%d):" % len(errors))
    for e in errors: print("  -", e)
    sys.exit(1)
print("PASS: in_REGISTER async two-hop + unique-constraint race guard correct")
