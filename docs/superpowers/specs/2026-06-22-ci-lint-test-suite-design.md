# CI: Linting + Test Suite (GitHub Actions)

**Date:** 2026-06-22
**Status:** Approved design, pending spec review

## Goal

Add Continuous Integration to uberserver. The repo currently has **no linting** and
**no automated test suite** — the 20 integration/worker test scripts we wrote during the
Phase 3 async-DB work live as untracked, dot-hidden files at the repo root and only print
results. There is a **stale `.travis.yml`** (Python 3.5, `trusty`, deps that no longer match
`requirements.txt`) that nobody relies on.

This PR delivers, in one branch:

1. A **linter** (ruff) wired into CI, blocking, with the existing high-signal findings fixed.
2. The 20 scripts turned into a **real, tracked pytest suite** that signals pass/fail via exit
   codes and runs in CI against a MariaDB service.
3. A **GitHub Actions** workflow replacing Travis (Travis is deleted, its two standalone
   self-tests migrated into GHA).

## Decisions (locked with user)

| Decision | Choice |
|---|---|
| CI provider | GitHub Actions; **delete `.travis.yml`**, migrate its 2 self-tests |
| Linter | ruff, `select = ["F", "E9"]`, **blocking** |
| Existing lint findings | **Fix all 55**, including the 10 `F821` NameError bugs (confidence flagged per-fix) |
| Test runner | **pytest wrapper** — each script is a parametrized subprocess test asserting exit 0 |
| Test layout | Move scripts into **`tests/` subdirectories**, tracked, de-dot-prefixed |

## Architecture

### Component 1 — Linter

- **Tool:** ruff (added to `requirements-dev.txt`).
- **Config:** `pyproject.toml` `[tool.ruff.lint]` with `select = ["F", "E9"]`.
  - Rationale: ruff's default ruleset flags **9,478** violations on this legacy codebase —
    almost entirely its own style idiom (tabs, `x; y` one-liners ~570, bare `except` ~59).
    Blocking on those is a non-starter. Pyflakes (`F`) + syntax errors (`E9`) is the
    high-signal subset: **55** findings, the "real bug" class.
- **Enforcement:** CI `lint` job runs `ruff check` and fails the build on any `F`/`E9`.

#### The 55 findings to fix in this PR

- **~45 mechanical** (safe): `F401` unused-import (30), `F841` unused-variable (10),
  `F507` `%`-format arg-count mismatch (4), `F402` import-shadowed-by-loop-var (1).
  Auto-fixed where ruff offers it; reviewed before commit.
- **10 `F821` undefined-name — genuine latent `NameError` bugs** in error/edge paths.
  Each fixed with its confidence surfaced to the user during implementation:

  | Location | Undefined name | Likely fix (to verify in-context) | Confidence |
  |---|---|---|---|
  | `protocol/Protocol.py:2457` | `host` | `battle.host` (used 2 lines up) | high |
  | `SQLUsers.py:1432` | `reason`, `wait_duration` | `entry.reason`, `entry.expiry` | high |
  | `protocol/Battle.py:69` | `username` | `client.username` | med-high |
  | `protocol/Protocol.py:2658` | `battle_id` | `battle.battle_id` from method scope | med |
  | `protocol/Protocol.py:1527` | `dbridgedClient` | typo for `bridgedClient` | med |
  | `Client.py:199` | `msg_length_limit` | locate the limit constant | med |
  | `protocol/Protocol.py:1306` | `bot_client` | needs lookup — trace intent | low |
  | `protocol/Protocol.py:3238` | `cs` (×2) | counter object not in scope — trace intent | low |

  Low-confidence sites are investigated by reading surrounding code before any edit; if intent
  can't be established with confidence, that is flagged to the user rather than guessed silently.

### Component 2 — Test suite layout

Move the 20 root dotfiles into tracked locations, dropping the `.` prefix:

```
tests/
  config.py            # NEW: central env-driven config (DB URL, host, port)
  conftest.py          # NEW: pytest fixtures (db schema, channel pre-seed, server boot)
  test_worker.py       # NEW: parametrizes the 6 in-process worker scripts
  test_integration.py  # NEW: parametrizes the 14 socket/e2e scripts
  worker/              # the 6 *workertest scripts (in-process, DB only)
  integration/         # the 14 socket scripts (need running server + DB)
```

(Existing `tests/TestLobbyClient.py`, `stresstest.py`, etc. are unrelated manual tools — left
in place, untouched.)

**Script categorization** (already verified):
- **6 worker (in-process, DB only):** `changeemailworkertest`, `changepasswordworkertest`,
  `closeoutworkertest`, `mystatusworkertest`, `registerworkertest`, `renameaccountworkertest`.
- **14 e2e (socket → running server):** `changeemaildropprobe`, `changeemailtest`,
  `changepasswordtest`, `channelmsgtest`, `closeoute2etest`, `concurrent`, `friendtest`,
  `ignoretest`, `logintest`, `logouttest`, `registertest`, `renameaccounttest`, `saytest`,
  `socialtest`.

### Component 3 — `tests/config.py` (DB URL / host / port from env)

Every script currently **hardcodes** the local recipe:
`mysql+pymysql://tomjn@localhost/uberserver_test?unix_socket=/tmp/mysql.sock` and
`HOST, PORT = "127.0.0.1", 8200`. CI's MariaDB service speaks **TCP with user/password**, so
these must be configurable. `tests/config.py` exposes:

```python
DB_URL = os.environ.get("UBERSERVER_TEST_DB_URL",
    "mysql+pymysql://tomjn@localhost/uberserver_test?unix_socket=/tmp/mysql.sock")
HOST = os.environ.get("UBERSERVER_TEST_HOST", "127.0.0.1")
PORT = int(os.environ.get("UBERSERVER_TEST_PORT", "8200"))
```

Default preserves the local socket recipe so **local runs are unchanged**. Each script's
hardcoded literals are replaced with imports from this module. (Scripts add the repo root /
`tests/` to `sys.path` as needed; pattern mirrors `server.py`'s `sys.path.append`.)

### Component 4 — pytest wrapper + fixtures (`conftest.py`)

The scripts keep their internals; pytest drives them as subprocesses and asserts exit 0.

- **`test_worker.py`:** `@pytest.mark.parametrize` over the 6 worker scripts →
  `subprocess.run([sys.executable, script], env=...)`, `assert rc == 0` (stdout/stderr shown on
  failure). Needs only the `db` fixture.
- **`test_integration.py`:** parametrize over the 14 e2e scripts; depends on the `server`
  fixture (which depends on `db` + `seeded_channels`).

**Fixtures (session-scoped):**
- `db`: ensures an empty `uberserver_test` exists. Schema is **auto-created** by
  `session_manager.__init__ → metadata.create_all(engine)` (runs on first worker-test
  session_manager and on server boot) — no manual DDL needed.
- `seeded_channels`: before server boot, opens an engine → `session_manager` (triggers
  `create_all`) → inserts the fixed channel rows required by pre-boot-seeding tests
  (`saychan_t1` with `antispam=0, store_history=1`; the `channelmsgtest` channel). Required
  because `DataHandler` loads channels **once at startup**.
- `server`: boots `server.py -p <PORT> -s <DB_URL>` as a subprocess, waits for readiness
  (poll the greeting on the socket / "Started lobby server!" line), yields, then terminates.
  One shared server for all e2e tests.

**Pre-PR fixes to scripts (exit codes):** `logintest` and `concurrent` are print-only; add
`sys.exit(0/1)` based on their PASS/FAIL so the wrapper can detect failure. The other 18
already exit non-zero on failure.

### Component 5 — GitHub Actions workflow (`.github/workflows/ci.yml`)

Two jobs, run on push + PR:

- **`lint`** (ubuntu, Python 3.12): install ruff, `ruff check .`.
- **`test`** (ubuntu, Python 3.12):
  - `services: mariadb:` sidecar (TCP, with `MYSQL_DATABASE=uberserver_test`, a user/password,
    health-check before steps run).
  - Install **runtime deps via PyMySQL, not mysqlclient** (`mysqlclient` needs a C toolchain;
    our DB URL uses the `mysql+pymysql://` driver). A `requirements-dev.txt` pins
    `PyMySQL`, `pytest`, `ruff` on top of the runtime deps.
  - Env: `UBERSERVER_TEST_DB_URL=mysql+pymysql://<user>:<pw>@127.0.0.1:3306/uberserver_test`.
  - Steps: (1) migrated Travis self-tests — `python protocol/Protocol.py` (selftest) and
    `python SQLUsers.py` (sqlite save/load); (2) `pytest tests/` (worker + e2e tiers).

**`requirements-dev.txt` (new):**
```
-r requirements.txt
PyMySQL==1.2.0
pytest
ruff
```
(`requirements.txt` keeps `mysqlclient` for production parity; CI overrides the driver via the
pymysql URL. PyMySQL pin matches the documented local recipe.)

## Data flow (CI test job)

```
GHA test job
  └─ mariadb service (empty uberserver_test, TCP)
  └─ install requirements-dev.txt (pymysql, pytest, ruff)
  └─ self-tests: Protocol.selftest, SQLUsers sqlite          [migrated from Travis]
  └─ pytest tests/
       ├─ db fixture                 → create_all via session_manager
       ├─ test_worker.py (×6)        → subprocess each, assert rc==0   [DB only]
       ├─ seeded_channels fixture    → insert channel rows pre-boot
       ├─ server fixture             → boot server.py -p -s, await ready
       └─ test_integration.py (×14)  → subprocess each, assert rc==0   [socket→server]
```

## Error handling

- **Linter:** any `F`/`E9` finding fails the `lint` job (non-zero ruff exit). Local devs run the
  same `ruff check .`.
- **Worker/e2e scripts:** already (or made to) exit non-zero on failure; the pytest wrapper
  surfaces captured stdout/stderr on failure for diagnosis.
- **Server fixture:** if the server doesn't become ready within a timeout, the fixture fails
  loudly (dumps `server.log` tail) rather than letting every e2e test hang/timeout opaquely.
- **DB service:** GHA `health-cmd` gates steps until MariaDB accepts connections, avoiding
  flaky "connection refused" at job start.

## Testing the CI itself

- Push the branch; confirm both jobs run and **pass** on GHA.
- Deliberately break one assertion locally to confirm the pytest wrapper turns a script FAIL
  into a job failure (the print-only → exit-code fix is what makes this work).
- Confirm `ruff check .` is green after the 55 fixes.

## Out of scope

- Reformatting the codebase to satisfy ruff's 9,400+ style findings (separate effort if ever
  wanted; deliberately not in this PR).
- Converting the scripts' internals into native pytest assertions (the subprocess wrapper
  preserves them as-is; a future cleanup could inline them).
- Matrix testing across multiple Python versions (single 3.12 to start).
- macOS/Windows runners (Linux only — also sidesteps the documented macOS framework-venv quirk).

## Risks / open items

- **Server readiness detection** in the `server` fixture must be robust (poll the socket
  greeting, not a fixed sleep) or e2e tests flake.
- **Channel pre-seed schema:** inserting channel rows requires the `channels` table to exist
  first; the fixture instantiates a `session_manager` (→ `create_all`) before inserting.
- **F821 low-confidence fixes** (`cs`, `bot_client`): if in-context reading can't establish
  intent, surface to user rather than guess — may carve those into a noted follow-up after all.
- **`mariadb` service auth:** confirm the chosen user/password/host form matches what
  `pymysql` expects over TCP (vs the local socket auth the recipe used).
