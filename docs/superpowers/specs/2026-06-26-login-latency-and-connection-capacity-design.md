# Login latency & connection capacity under load — design

**Date:** 2026-06-26
**Status:** Draft for review

## Problem

uberstress load tests against the production-like server (`lobby.recoilengine.org:8200`),
swept at 100 / 500 / 1000 / 1500 / 1999 concurrent connections, show two distinct
failure modes once load exceeds ~500 connections:

| conns | dial_error | login_ok | login_error | LOGIN p50 | LOGIN p99 |
|------:|-----------:|---------:|------------:|----------:|----------:|
| 100   | 0          | 100      | 0           | 168 ms    | 179 ms    |
| 500   | 0          | 200      | 300         | 7613 ms   | 14946 ms  |
| 1000  | 494        | 172      | 334         | 7103 ms   | 14933 ms  |
| 1500  | 994        | 170      | 336         | 7046 ms   | 14584 ms  |
| 1999  | 1493       | 170      | 336         | 7005 ms   | 14619 ms  |

Test conditions: verification/confirmation disabled, ~2000 users pre-seeded in the DB,
one real client (skylobby) online. Server-side CPU stayed **≤20%, spiky-then-idle**
throughout — the box was waiting, not computing.

Two independent ceilings:

1. **LOGIN latency** — p50 ~7 s, p99 pinned at ~14.9 s. The uniform ~14,9xx ms p99/max
   across every run is the **client login timeout**, and `login_error` counts are clients
   giving up in a queue, not auth failures (the users exist).
2. **Connection ceiling ~506** — `login_ok + login_error` is pinned at ~506 at every level
   ≥500; everything beyond becomes `dial_error` (`dial_error ≈ conns − 506`).

## Diagnosis

### Cause 1 — obsolete fixed-rate login throttle (LOGIN latency)

`in_LOGIN` (`protocol/Protocol.py:1028`) gates logins to `login_drain_rate = 10/sec`
(`DataHandler.py:126`); overflow sits in a FIFO drained 10-at-a-time by a 1 s
`LoopingCall`, `drain_login_queue` (`DataHandler.py:664`, started in `server.py:62`).

Arithmetic matches the data: 500 conns over a 10 s ramp = ~50 logins/sec arriving vs
10/sec drained → the queue grows ~40/sec; a client tolerates ~15 s of queue wait before
timing out, so once the queue passes ~150 deep every further arrival times out
(`login_error ≈ 300`), and only what drains inside ~15 s succeeds (`login_ok ≈ 170–200`).
p50 7 s = mid-queue; p99 15 s = the timeout wall.

The **spike-then-idle CPU profile is the literal signature of the 1 s drain tick**: a burst
of 10 logins, then the reactor sleeps until the next tick.

This throttle (OPTIMIZATIONS.md §2.2) was added in Phase 2 because each login then did
**synchronous DB I/O on the reactor thread**. **Phase 3 (merged) moved all login DB work
off the reactor** via `deferToThread` (`precheck_login` / `do_login`). The throttle now
limits logins that no longer block the reactor on DB — its original justification is gone.

### Cause 2 — connection cap = `RLIMIT_NOFILE / 2` (dial_error)

`twistedserver.py:10-11`:

```python
maxhandles, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
maxclients = int(maxhandles / 2)
```

`connectionMade` (`twistedserver.py:26`) rejects every connection past `maxclients` with
`DENIED to many connections` + `transport.abortConnection()`. No systemd unit sets
`LimitNOFILE`, so the process runs the OS default soft limit of **1024 → maxclients = 512**.
The observed ~506 is 512 minus skylobby and a few battle-host bots. The abrupt server-side
abort is what the load tester records as a dial failure.

The `/2` formula is wrong, not just low: a connected player normally costs **exactly one
fd** (the TCP socket — `startTLS` upgrades that same socket in place and does not allocate a
second fd; the NAT server is one shared UDP socket). The real non-client fd overhead is
roughly constant (~100: DB pool `pool_size = 50`, listening sockets, logs, GeoIP, transient
IP-detection HTTP). Reserving 50% scales the reserve with the wrong variable — at
nofile=1024 it wastes ~400 usable slots; at nofile=65536 it would "reserve" 32,768 for ~100
of real overhead.

The default-50 listen backlog (`reactor.listenTCP` in `server.py:51` passes no `backlog`)
is a secondary contributor and becomes the *next* bottleneck once the fd cap is lifted.
(`DataHandler.createSocket()` sets `backlog=100` but is dead code — never called.)

**Server-side confirmation (one check):**
`cat /proc/$(pgrep -f server.py)/limits | grep "open files"` should show 1024.

## Fixes

### Lever 1 — adaptive login draining (write-buffer backpressure) + remove reactor DB call

Replace the fixed 10/sec rate with **write-buffer-backpressure-gated draining**, reusing the
existing §2.3 producer machinery.

- Maintain a counter on `DataHandler` (`_root`) of currently back-pressured clients.
  Increment in `Chat.pauseProducing` (`twistedserver.py:68`), decrement in
  `resumeProducing` / `stopProducing`.
- `in_LOGIN`: if the login queue is empty **and** the server is not back-pressured, run
  `login_now` inline (immediate — no tick wait). Otherwise append to the FIFO and send the
  existing "you are in the login queue" `SERVERMSG`. This preserves FIFO fairness and means
  **no queuing happens at all under normal load** → latency collapses to ~DB round-trip.
- `drain_login_queue`: drain the queue while not back-pressured, **re-checking backpressure
  each iteration** so that as state-dumps fill send buffers mid-drain the count rises and
  draining self-limits. Remove `login_drain_rate` / `logins_this_second`.
- Backpressure threshold is **configurable** (a small N, not 0, since each login's state-dump
  is a large transient write that may briefly pause the receiving client). Default value is a
  starting point to be tuned during verification.

**Paired change (required):** move the one remaining synchronous reactor-thread DB call out
of the login path. `_SendLoginInfo` (`protocol/Protocol.py:1207`) calls
`get_ignored_user_ids` on the reactor. Fold that query into the existing **`do_login`
worker** (`SQLUsers.py:618`) — it already runs off-reactor and has `dbuser.id` — and return
the ignore list alongside the `OfflineClient` snapshot. `_login_finish` / `_SendLoginInfo`
then read `client.ignored` from already-fetched data with no reactor DB call and no extra
callback hop.

Rationale: write-buffer pressure is a good **brake** but is blind to DB/reactor saturation.
Under the tested profile (fast-reading clients, 20% CPU) it rarely trips, so its practical
effect is "let logins flow freely, brake only on genuine slow-client overload" — which is
the desired latency win. But with the synchronous `get_ignored_user_ids` left in place,
freely-flowing logins would simply re-block the reactor on DB I/O that the brake cannot see.
Moving it off the reactor closes that blind spot.

### Lever 2 — raise the connection ceiling (dial_error)

1. **systemd:** add `LimitNOFILE=65536` to `systemd/uberserver.service` (and
   `uberserver-dev.service` for parity). Takes effect on service restart.
2. **Formula:** change `twistedserver.py` from `maxclients = int(maxhandles / 2)` to a fixed
   reserve: `maxclients = maxhandles - RESERVE`, `RESERVE = 256` (comfortably covers the DB
   pool, listeners, logs, transient fds). At nofile=65536 this yields ~65,280 client slots.
3. **Backlog:** pass an explicit `backlog` to `reactor.listenTCP` in `server.py:51` (e.g.
   1024) so the kernel accept queue isn't the next bottleneck. Note OS `net.core.somaxconn`
   caps the effective backlog; flag for ops if it needs raising.

### Lever 3 — verification

Re-run the uberstress 100 → 1999 sweep against the patched + redeployed server (the existing
A/B harness is the feedback loop). Success criteria:

- LOGIN p50 drops from ~7 s to the low hundreds of ms (≈ the 100-conn baseline of 168 ms).
- `login_error` → 0 (no queue-timeout giveups) at the tested scale.
- `dial_error` → 0 up to the new connection cap.
- Tune the Lever-1 backpressure threshold if draining stalls or over-admits.

Deployment note: `LimitNOFILE` and code both require a service restart on the remote box
(coordinated with scary). The local dev box is for code-path smoke tests only, not a
performance baseline.

## Out of scope — known architectural ceiling

Lifting the fd cap removes the *artificial* limit. The *real* ceiling underneath is the
**single Twisted reactor thread**: every login fires an O(N) state-dump plus an O(N)
`broadcast_AddUser`, and all ongoing play (status, chat, battle updates) is serialized on
that one thread. At 20% CPU there is ample headroom, so fds are the binding constraint for
the foreseeable future, but at *several thousand* concurrent players the reactor itself
becomes the wall. That is an architectural change (sharding / multi-process), explicitly
**not** part of this work.

## Files touched

- `protocol/Protocol.py` — `in_LOGIN`, `_SendLoginInfo` (read ignore list from snapshot)
- `DataHandler.py` — backpressure counter, `drain_login_queue`, remove `login_drain_rate`
- `SQLUsers.py` — `do_login` returns the ignore list
- `twistedserver.py` — backpressure counter hooks; `maxclients` formula
- `server.py` — `listenTCP(..., backlog=...)`
- `systemd/uberserver.service`, `systemd/uberserver-dev.service` — `LimitNOFILE`
- `OPTIMIZATIONS.md` — note §2.2 throttle superseded by adaptive draining
