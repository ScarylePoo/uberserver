# Phase 3 — Async Database (OPTIMIZATIONS.md 3.1) — Design

Status: in progress. Base: `origin/master` @ c87633a (includes Phase 1 caching,
Phase 2.1 broadcast, 2.2 login queue, 2.3 write buffers).

## Goal
Stop synchronous SQLAlchemy calls from blocking the single Twisted reactor thread by
running DB I/O in the reactor thread pool via `twisted.internet.threads.deferToThread`.
Convert the LOGIN flow first, test under real concurrent load, then convert other
handlers one group at a time.

## Core invariant being broken
The codebase is correct today only because everything runs on one reactor thread:
- `session_manager` hands out ONE shared `self.session` (SQLUsers.py). A SQLAlchemy
  Session is not thread-safe.
- Shared in-memory state (`clients`, `usernames`, `user_ids`, `channels`, `battles`,
  `Channel.users`/`user_clients`) is mutated assuming the reactor thread.
- ORM rows are session-bound; after commit+close they detach. The 1.4 optimization
  passes a live `User` row `check_banned` -> `login_user`, which only works within one
  session/thread.

## Agreed decisions
1. PR slicing: incremental. PR A = infra (no behavior change); PR B = async `in_LOGIN_now`;
   then one PR per later handler group.
2. State ownership: REACTOR-THREAD OWNERSHIP. Worker threads do pure DB I/O and return
   plain data (dict / OfflineClient snapshot). ALL shared-state mutation + `client.Send`
   happen in reactor-thread Deferred callbacks (callbacks fire on the reactor thread).
   No new locks. Uniqueness re-checked in the callback (callbacks are serialized).
3. `max_threads` default = 10 (Twisted default) for a real DB; stays 1 for sqlite.
4. Test DB: MariaDB (sqlite forces max_threads=1, no pool, check_same_thread — cannot
   exercise the threaded path).

## Pre-existing cross-thread access (not widened)
`NATServer.py:32` reads `_root.usernames[msg]` from the UDP thread and dispatches
`_udp_packet`. This is pre-existing. Reactor-ownership adds no new writer, so no new lock
is introduced for it. Out of scope for Phase 3.

## PR A — thread-safe session_manager + thread-pool wiring (no runtime change)
- `session_manager`: `scoped_session(sessionmaker(bind=engine, autoflush=True))`.
  - `sess()` -> `self.sessionmaker()` (thread-local).
  - `commit_guard`/`rollback_guard`/`close_guard`: guard on `registry.has()`;
    `close_guard` uses `.remove()`. Behaviorally identical on one thread.
- `max_threads`: real setting (default 10, sqlite=1); enforce `pool_size >= max_threads`;
  `reactor.suggestThreadPoolSize(max_threads)` before `reactor.run()`.
- Dormant until PR B; verify zero behavior change against MariaDB. Do NOT claim the
  threaded path is verified in PR A.

## PR B — async `in_LOGIN_now`
Split into:
- Reactor pre-checks (memory only): syntax, `username in usernames`, nasty(name).
- `deferToThread` worker (pure DB, one session): check_login_user, _check_delayed?
  (memory — keep on reactor), check_banned, login_user, get_ignored_user_ids. Returns
  `('denied', reason)` or `('ok', snapshot, ignored_ids)`. Live row stays inside worker;
  returns plain values only.
- Reactor callback `_login_continue`: re-check uniqueness, copy fields to client,
  agreement branch OR `_SendLoginInfo` (state dump + broadcast). errback -> out_DENIED.

### Ordering note
Current order interleaves memory checks between DB calls (check_login -> delayed-reg ->
check_banned). Preserve DENIED-message ordering: keep delayed-reg on the reactor side at
the correct point, or split into two deferred hops. Decide during PR B implementation.

### Races introduced and handling
- Double login same username: re-check `username in usernames` / `user_id in user_ids`
  in the reactor callback before insert. No lock (callbacks serialized).
- Detached row reuse: whole check_banned->login_user chain inside one worker.
- Off-thread dict mutation: forbidden; all mutation in reactor callbacks.

## Later PRs
Convert remaining handler groups one at a time (register, channel ops, admin, etc.),
each: worker returns plain data, callback mutates state. Add DB-level uniqueness handling
(IntegrityError) for register where needed.
