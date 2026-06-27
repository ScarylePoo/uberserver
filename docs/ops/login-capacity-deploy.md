# Deploy & ops runbook — login latency / connection capacity changes

These changes lift two ceilings found under uberstress load testing: the fixed 10/sec login
throttle (caused multi-second LOGIN latency) and the `RLIMIT_NOFILE / 2` connection cap
(caused `dial_error` past ~512 connections). The code changes ship in the repo; **two of the
fixes only take effect after operator action on the server box.**

## What changed in the repo (no action needed beyond deploying the code)

- **Adaptive login draining.** The fixed `login_drain_rate = 10/sec` is gone. Logins now run
  inline as fast as the send path stays healthy; they only queue under write-buffer
  backpressure. (`DataHandler.py`, `protocol/Protocol.py`, `twistedserver.py`)
- **Ignore list fetched off the reactor.** `do_login` now returns the user's ignore list with
  the login snapshot, so `_SendLoginInfo` no longer runs a synchronous DB query on the
  reactor thread. (`SQLUsers.py`, `protocol/Protocol.py`)
- **Connection cap formula.** `maxclients` is now `RLIMIT_NOFILE - 256` instead of
  `RLIMIT_NOFILE / 2`. (`twistedserver.py`)
- **Listen backlog.** `reactor.listenTCP(..., backlog=1024)` instead of the Twisted default
  of 50. (`server.py`)
- **systemd `LimitNOFILE=65536`** added to `uberserver.service` and `uberserver-dev.service`.

## Operator steps on the server (scary)

### 1. Deploy code + reload the systemd unit

```sh
# as the deploy user, in the checkout (e.g. /home/lobby/uberserver)
git pull                       # or however the box is updated

sudo cp systemd/uberserver.service /etc/systemd/system/uberserver.service
sudo systemctl daemon-reload
sudo systemctl restart uberserver
```

`LimitNOFILE` is read at process start, so the restart is what actually raises the fd limit.

### 2. Confirm the new fd limit took effect

```sh
cat /proc/$(pgrep -f 'server.py')/limits | grep 'open files'
# Max open files  65536  65536  files   <- soft and hard should both be 65536
```

If it still shows 1024, the unit override didn't load — re-check the file path and
`daemon-reload`. The new player cap is then `65536 - 256 = 65280`.

### 3. Raise `net.core.somaxconn` if it's below the backlog

The `backlog=1024` we request is silently clamped by the kernel to `net.core.somaxconn`.
Default is 128 on many distros (4096 on newer kernels).

```sh
sysctl net.core.somaxconn
# if < 1024:
sudo sysctl -w net.core.somaxconn=1024
echo 'net.core.somaxconn = 1024' | sudo tee /etc/sysctl.d/99-uberserver.conf
```

(Requires a server restart to re-listen with the larger effective backlog.)

## Verification — re-run the uberstress sweep

Re-run the 100 -> 1999 connection sweep against the restarted server (the same A/B harness
used to find the problem). Expected, vs the pre-fix baseline:

| Metric            | Before          | After (target)                      |
|-------------------|-----------------|-------------------------------------|
| LOGIN p50         | ~7000 ms        | low hundreds of ms (~the 168 ms 100-conn baseline) |
| LOGIN p99         | ~14900 ms (timeout wall) | well under the client timeout |
| `login_error`     | ~300+ (queue timeouts) | 0 at the tested scale         |
| `dial_error`      | ~conns − 506    | 0 up to the new cap                 |

## Known related limit — `max_threads` (DB worker pool)

With the fixed throttle gone, the **effective login throughput limiter is now the
off-reactor DB thread pool**, `max_threads` (default **10**, clamped to `pool_size = 50`).
Each login does two sequential worker DB calls (`precheck_login`, then `do_login`), so peak
login rate is roughly `max_threads / (2 x DB_round_trip)`. If the verification sweep shows
login latency bounded by DB-pool queueing rather than backpressure, raise `max_threads`
(and `pool_size` to match). This is a config knob, not a code change.

## Tuning — `login_backpressure_limit`

`DataHandler.login_backpressure_limit` (default **50**) is the count of simultaneously
write-buffer-backed-up clients above which new logins start queueing. Fast clients rarely
trip it; tune during the verification sweep if draining stalls (lower it) or if the server
over-admits under genuine send overload (it is already engaging — check for dropped slow
readers in the log).

## Out of scope — architectural ceiling

All protocol work runs on a single Twisted reactor thread. Lifting the fd cap removes the
artificial limit, but at several thousand concurrent players the single reactor thread
becomes the real wall (each login does O(N) work; broadcasts are O(N)). That needs sharding
/ multi-process and is explicitly not part of this work. CPU headroom is large today (~20%
under the test load), so fds are the binding constraint for now.
