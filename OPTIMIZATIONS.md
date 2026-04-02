# Uberserver Performance Optimizations

This document outlines a planned series of performance improvements to uberserver,
prioritized by impact and implementation complexity. The goal is to increase concurrent
user and battle capacity without requiring any changes to the lobby protocol or client
software.

Current estimated capacity (unoptimized):
- ~500-1000 concurrent idle users
- ~50-100 simultaneous battles
- ~20-30 simultaneous logins before degradation

Target capacity (fully optimized):
- ~3,000-5,000 concurrent idle users
- ~300-500 simultaneous battles
- ~100-200 logins per second

All changes should be implemented and tested **one phase at a time**. Do not combine
phases into a single pull request. Each phase should be committed, deployed, and tested
under real load before moving to the next.

---

## Phase 1 — Easy Wins (Low Risk, High Reward)

These are self-contained changes with minimal risk of introducing bugs. Start here.

### 1.1 Cache Ban Checks in Memory

**File:** `SQLUsers.py` — `BansHandler.check_ban()`

**Problem:** Every single login hits the database to check if the user or their IP is
banned. Bans change rarely but this query runs constantly.

**Fix:** Add an in-memory dict cache with a TTL of ~5 minutes. On a cache hit, skip the
DB query entirely. Invalidate the cache entry when a ban is added or removed.

**Expected impact:** Significant reduction in DB load during login storms. Bans still
take effect within 5 minutes of being issued.

---

### 1.2 Cache User Lookups

**File:** `SQLUsers.py` — `UsersHandler.clientFromUsername()` and `clientFromID()`

**Problem:** Offline user lookups (e.g. checking if a username exists, loading a user
for a channel op check) go to the database every time. There is already an in-memory
dict for *online* users but offline lookups bypass it.

**Fix:** Add a small LRU cache (e.g. 500 entries) for offline user lookups with a TTL
of ~2 minutes. Use Python's `functools.lru_cache` or a simple dict with timestamps.

**Expected impact:** Reduces DB queries during channel setup, op checks, and ban lookups
by a significant margin.

---

### 1.3 Batch DB Commits in SQLUsers.py

**File:** `SQLUsers.py` — throughout

**Problem:** `self.sess().commit()` is called after almost every single field update.
Under load this means dozens of individual commits per login, each of which flushes
to disk.

**Fix:** Audit all commit() calls and batch related operations into single transactions
where safe to do so. For example, updating `last_login`, `last_ip`, `last_agent`, and
`ingame_time` at login should be a single commit, not four separate ones.

**Expected impact:** Reduces disk I/O significantly under load. MariaDB handles batched
transactions much more efficiently than many small ones.

---

### 1.4 Reduce Redundant DB Queries on Login

**File:** `SQLUsers.py` — `UsersHandler.login_user()` and related

**Problem:** The login flow makes several sequential DB queries that could be combined:
user lookup, ban check, login record insert, access level check. Each is a round trip
to the database.

**Fix:** Where possible combine queries. For example the user lookup and ban check can
share the same session context and be executed closer together, avoiding redundant
session overhead.

**Expected impact:** 30-40% improvement in login throughput.

---

### 1.5 Make Connection Pool Size Configurable

**File:** `DataHandler.py`

**Problem:** `self.pool_size = 50` is hardcoded. On a well-resourced server this is
unnecessarily low. On a low-resource server it may be too high.

**Fix:** Read pool size from an environment variable or config file, defaulting to 50.
Document recommended values based on available RAM (rough guide: 1 pool connection uses
~2-5MB of RAM depending on query complexity).

**Expected impact:** Allows tuning for specific hardware without code changes.

---

## Phase 2 — Smarter Broadcasting (Medium Effort, High Impact for Battles)

### 2.1 Per-Channel Pub/Sub Broadcast

**File:** `DataHandler.py` — `multicast()` and `broadcast()`

**Problem:** `multicast()` iterates over all session IDs linearly for every message sent
to a channel. For a channel with 200 users, every single chat message triggers 200
iterations. Under heavy load with many active channels this becomes very expensive.

```python
# Current approach - O(n) over ALL clients for every channel message
for session_id in session_ids:
    client = self.clientFromSession(session_id)
    ...
    client.Send(msg)
```

**Fix:** Maintain a per-channel set of transport references (not session IDs) so that
broadcasting to a channel is a direct iteration over only that channel's members, with
no dict lookups required per message.

**Expected impact:** Dramatic improvement in high-traffic channel and battle scenarios.
A server with 20 active battles of 10 players each currently does 200 iterations per
battle message. With this fix it does 10.

---

### 2.2 Login Queue

**File:** `DataHandler.py` and `protocol/Protocol.py` — `in_LOGIN()`

**Problem:** When many users connect simultaneously (e.g. server restart, scheduled
event) the login handler hammers the DB with concurrent requests. Each login does
multiple DB operations. Under a storm this causes cascading slowdowns.

**Fix:** Implement a simple FIFO login queue with a configurable drain rate (e.g. 10
logins per second). Clients that are queued receive a `SERVERMSG` informing them they
are in a queue. This is transparent to all existing clients — they already handle
`SERVERMSG` gracefully.

**Expected impact:** Prevents login storms from degrading the entire server. Turns a
hard failure mode into graceful queuing.

---

## Phase 3 — Async Database (High Effort, Highest Impact)

This is the single most impactful change and the most complex. Do not attempt this
until Phases 1 and 2 are stable and tested.

### 3.1 Run DB Calls in Twisted Thread Pool via deferToThread

**Files:** `SQLUsers.py`, `DataHandler.py`, `protocol/Protocol.py`

**Problem:** SQLAlchemy database calls are synchronous and blocking. When they run
inside Twisted's event loop they block *all* other clients from being served until the
query completes. Under load this means a slow query (e.g. a complex ban check) stalls
the entire server.

**Fix:** Wrap all DB operations using Twisted's `deferToThread()` so they run in a
thread pool without blocking the event loop. The event loop stays free to handle other
connections while DB queries execute in the background.

```python
# Current (blocks event loop)
def in_LOGIN(self, client, ...):
    user = self.userdb.clientFromUsername(username)  # blocks here
    ...

# Target (non-blocking)
from twisted.internet.threads import deferToThread

def in_LOGIN(self, client, ...):
    d = deferToThread(self.userdb.clientFromUsername, username)
    d.addCallback(self._login_continue, client, ...)
```

**What makes this hard:**
- Every DB call in the codebase needs to be audited and potentially converted
- SQLAlchemy sessions are not thread-safe by default — the session management in
  `session_manager` needs careful review to ensure each thread gets its own session
- Callback chains in Twisted can be harder to reason about than linear code
- Race conditions become possible where they weren't before (e.g. two simultaneous
  logins for the same username)

**Recommended approach:**
1. Start with the login flow only — convert `in_LOGIN` and its DB calls first
2. Test thoroughly before touching anything else
3. Work through the remaining protocol handlers one at a time
4. Add locking where needed for operations that must be atomic (e.g. username
   uniqueness check + insert)

**Expected impact:** The single biggest improvement. Login throughput could increase
5-10x. The server becomes responsive under load that would currently cause it to
stall completely.

---

## Implementation Notes

### Testing Each Phase

Before and after each phase, test with the stress test client included in the repo:

```bash
cd tests
python3 stresstest.py --host 127.0.0.1 --port 8200 --users 100
```

Increase `--users` gradually. Watch `server.log` for errors and measure response times.

### Suggested Tooling

- **`py-spy`** — sampling profiler for Python, can profile a running server without
  stopping it: `pip install py-spy && py-spy top --pid <server_pid>`
- **`htop`** — watch CPU and memory usage during load tests
- **MariaDB slow query log** — enable to identify which queries are taking longest:
  ```sql
  SET GLOBAL slow_query_log = 'ON';
  SET GLOBAL long_query_time = 0.1;
  ```

### Order of Implementation

```
Phase 1.1 → 1.2 → 1.3 → 1.4 → 1.5
    ↓
Phase 2.1 → 2.2
    ↓
Phase 3.1
```

Do not skip ahead. Each phase builds on the stability of the previous one. Phase 3
especially assumes the caching from Phase 1 is in place (reduces the number of DB
calls that need to be made async).

### Bringing This Back to an AI Assistant

When resuming this work with an AI assistant (Claude or otherwise):

1. Zip the full repository and upload it
2. Upload this file (`OPTIMIZATIONS.md`)
3. State which phase you want to work on
4. The assistant will have everything it needs to proceed

The codebase context that matters most:
- `SQLUsers.py` — all database operations
- `DataHandler.py` — server state, broadcast, session management
- `protocol/Protocol.py` — all client command handlers
- `twistedserver.py` — Twisted integration layer

---

## Background: What Was Already Fixed

Before these optimizations were scoped, the following compatibility issues were
resolved to get the server running on Ubuntu 24.04 with Python 3.12:

| File | Fix |
|---|---|
| `requirements.txt` | Replaced `GeoIP==1.3.2` with `geoip2==4.8.0` |
| `requirements.txt` | Bumped `mysqlclient` to `2.2.4` |
| `requirements.txt` | Bumped `pyOpenSSL` to `24.0.0` |
| `requirements.txt` | Pinned `SQLAlchemy` to `1.4.52` (last 1.x release; 2.x removed `mapper()`) |
| `requirements.txt` | Added `service-identity==24.1.0` for Twisted TLS |
| `ip2country.py` | Full rewrite to use `geoip2` + MaxMind GeoLite2 database |
| `protocol/Protocol.py` | `inspect.getargspec` → `inspect.getfullargspec` (removed in Python 3.11) |
| `Client.py` | Fixed invalid escape sequences `\%` and `\)` (SyntaxWarning → future error) |
| `SQLUsers.py` | Patched `_send_email` to support external SMTP with STARTTLS and auth |
| `DataHandler.py` | Extended `server_email_account.txt` to support 5-line SMTP config |
