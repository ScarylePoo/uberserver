import os as _os, sys as _sys
_sys.path[:0] = [_os.path.join(_os.path.dirname(__file__), _os.pardir, _os.pardir),
                 _os.path.join(_os.path.dirname(__file__), _os.pardir)]
from testenv import DB_KWARGS, HOST, PORT
import socket, hashlib, base64, time, threading, sys, json
import pymysql

TAG = "t1"   # channel is seeded as saychan_t1 pre-boot
N = 8        # concurrent senders
K = 15       # messages per sender
CHAN = "saychan_%s" % TAG
raw_pw = "secretpw"
PW = base64.b64encode(hashlib.md5(raw_pw.encode()).digest()).decode()
DB = DB_KWARGS

def recv_until(s, substr, timeout=8):
    s.settimeout(timeout); buf = b""
    try:
        while substr.encode() not in buf:
            chunk = s.recv(8192)
            if not chunk: break
            buf += chunk
    except socket.timeout: pass
    return buf.decode(errors="replace")

class Sock:
    def __init__(self, s): self.s = s; self.buf = ""
    def send(self, line): self.s.sendall((line + "\n").encode())
    def drain(self, seconds=0.5):
        self.s.settimeout(seconds)
        try:
            while True:
                chunk = self.s.recv(8192)
                if not chunk: break
                self.buf += chunk.decode(errors="replace")
        except socket.timeout: pass
    def close(self):
        try: self.s.close()
        except: pass

def connect():
    s = socket.create_connection((HOST, PORT)); recv_until(s, "\n"); return s

def register_and_confirm(user):
    # idempotent: works for a brand-new user (agreement flow) and an already-confirmed one
    s = connect()
    s.sendall(("REGISTER %s %s\n" % (user, PW)).encode()); recv_until(s, "\n")
    time.sleep(2.5)
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=8)
    if "AGREEMENT" in resp and "LOGININFOEND" not in resp:
        s.sendall(b"CONFIRMAGREEMENT\n")
        resp += recv_until(s, "LOGININFOEND")
    ok = "LOGININFOEND" in resp; s.close(); return ok

def login_keep(user):
    s = connect()
    s.sendall(("LOGIN %s %s 0 * TestClient\n" % (user, PW)).encode())
    resp = recv_until(s, "LOGININFOEND", timeout=8)
    if "ACCEPTED" not in resp or "LOGININFOEND" not in resp:
        s.close(); return None
    return Sock(s)

listener = "say_%s_L" % TAG
getter = "say_%s_G" % TAG
senders = ["say_%s_s%d" % (TAG, i) for i in range(N)]
allusers = [listener, getter] + senders

results = {}
def _setup(u): results[u] = register_and_confirm(u)
ts = [threading.Thread(target=_setup, args=(u,)) for u in allusers]
for t in ts: t.start()
for t in ts: t.join()
if sum(1 for v in results.values() if v) != len(allusers):
    print("ABORT: not all users registered:", {k: v for k, v in results.items() if not v}); sys.exit(1)
print("setup confirmed: %d/%d" % (len(allusers), len(allusers)))

# clean any prior history for this channel so GETCHANNELMESSAGES 0 returns only this run
conn = pymysql.connect(**DB); cur = conn.cursor()
cur.execute("SELECT id FROM channels WHERE name=%s", (CHAN,))
row = cur.fetchone()
if not row: print("ABORT: channel %s not seeded (seed pre-boot)" % CHAN); sys.exit(1)
chan_id = row[0]
cur.execute("DELETE FROM channel_history WHERE channel_id=%s", (chan_id,))
print("channel %s id=%d, history cleared" % (CHAN, chan_id))

# listener joins and stays, capturing the live broadcast order
L = login_keep(listener)
L.send("JOIN %s" % CHAN); L.drain(0.6)

# senders join
sender_socks = []
for s in senders:
    sk = login_keep(s)
    sk.send("JOIN %s" % CHAN); sk.drain(0.4)
    sender_socks.append(sk)

# concurrent blast: each sender sends K messages "S<i>N<seq>" as fast as possible
def blast(i):
    sk = sender_socks[i]
    for seq in range(K):
        sk.send("SAY %s S%dN%d" % (CHAN, i, seq))

t0 = time.time()
ts = [threading.Thread(target=blast, args=(i,)) for i in range(N)]
for t in ts: t.start()
for t in ts: t.join()

# collect the live order from the listener until we have N*K SAID lines (or timeout)
want = N * K
live = []
deadline = time.time() + 8
seen_buf = ""
while len(live) < want and time.time() < deadline:
    L.drain(0.5)
    # parse complete SAID lines from L.buf
    lines = L.buf.split("\n")
    L.buf = lines.pop()  # keep partial
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("SAID %s " % CHAN):
            # "SAID <chan> <username> <msg>"
            parts = ln.split(" ", 3)
            if len(parts) == 4:
                live.append(parts[3])
dt = time.time() - t0
print("sent %d msgs from %d senders in %.2fs; listener captured %d live SAID" % (want, N, dt, len(live)))

# wait for the per-channel store queue to drain, then read stored order via GETCHANNELMESSAGES
time.sleep(1.5)
G = login_keep(getter)
G.send("JOIN %s" % CHAN); G.drain(0.6)
G.send("GETCHANNELMESSAGES %s 0" % CHAN)
stored = []  # (id, msg)
deadline = time.time() + 6
while len(stored) < want and time.time() < deadline:
    G.drain(0.5)
    lines = G.buf.split("\n")
    G.buf = lines.pop()
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("JSON "): continue
        try: obj = json.loads(ln[5:])
        except Exception: continue
        said = obj.get("SAID")
        if not said or said.get("chanName") != CHAN: continue
        stored.append((int(said["id"]), said["msg"]))

stored.sort(key=lambda x: x[0])
stored_msgs = [m for (_id, m) in stored]
print("stored history rows: %d" % len(stored_msgs))

# also confirm the DB row count directly
cur.execute("SELECT COUNT(*) FROM channel_history WHERE channel_id=%s", (chan_id,))
db_count = cur.fetchone()[0]
conn.close()

errors = []
if db_count != want:
    errors.append("DB row count %d != expected %d (lost or extra stored messages)" % (db_count, want))
if len(live) != want:
    errors.append("listener captured %d live SAID != expected %d" % (len(live), want))
if len(stored_msgs) != want:
    errors.append("GETCHANNELMESSAGES returned %d != expected %d" % (len(stored_msgs), want))

# the core invariant: stored id-order == live broadcast order
if stored_msgs and live and stored_msgs != live:
    # find first divergence for a useful message
    div = next((j for j in range(min(len(stored_msgs), len(live))) if stored_msgs[j] != live[j]), None)
    errors.append("ORDER MISMATCH at index %s: stored=%s live=%s" % (
        div, stored_msgs[max(0, (div or 0)-2):(div or 0)+3], live[max(0, (div or 0)-2):(div or 0)+3]))

# per-sender monotonicity sanity (each sender's own messages must stay in send order)
for i in range(N):
    seqs = [int(m[m.index("N")+1:]) for m in stored_msgs if m.startswith("S%dN" % i)]
    if seqs != sorted(seqs):
        errors.append("sender %d out of order in stored history: %s" % (i, seqs))

for sk in [L, G] + sender_socks: sk.close()

if errors:
    print("FAIL:")
    for e in errors: print("  ", e)
    sys.exit(1)
else:
    print("PASS: stored history id-order == live broadcast order; no messages lost (%d msgs)" % want)
