"""Pytest fixtures for the uberserver test suite.

Tiers:
  - worker tests (tests/worker/*.py): in-process against MariaDB, need only the `db` fixture.
  - e2e tests (tests/integration/*.py): socket clients, need a running server -> the `server`
    fixture, which first seeds the channels that must exist before the server boots.

Each script is run as its own subprocess (preserving the standalone scripts unchanged); the
`run_script` fixture asserts a zero exit code and surfaces stdout/stderr on failure.

Configuration (DB URL, server host/port) comes from tests/testenv.py, which reads env vars and
defaults to the local socket recipe. Tests share one server and one database and therefore must
run sequentially (do not use pytest-xdist -n).
"""
import os
import socket
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import testenv  # noqa: E402

# Child processes (server + scripts) import repo-root modules (SQLUsers, ...) and tests/testenv.
_CHILD_ENV = dict(os.environ)
_CHILD_ENV["PYTHONPATH"] = os.pathsep.join(
    [ROOT, HERE] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))

# Channels that must exist in the DB before the server boots (DataHandler loads channels once at
# startup). (name, antispam, store_history)
SEED_CHANNELS = [
    ("saychan_t1", 0, 1),   # saytest: history ordering, antispam off so senders aren't muted
    ("histchan_c1", 0, 1),  # channelmsgtest: GETCHANNELMESSAGES history
]


class _FakeRoot:
    """Minimal root for session_manager.__init__ (it only stores root + runs create_all)."""


def _ensure_schema():
    import sqlalchemy
    import SQLUsers
    engine = sqlalchemy.create_engine(testenv.DB_URL)
    SQLUsers.session_manager(_FakeRoot(), engine)  # __init__ -> metadata.create_all(engine)
    engine.dispose()


def _tail(path, n=80):
    try:
        with open(path) as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(no %s)" % path


def _wait_ready(host, port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2) as s:
                s.settimeout(2)
                if s.recv(64):  # server sends a TASSERVER greeting on connect
                    return True
        except OSError:
            time.sleep(0.3)
    return False


@pytest.fixture(scope="session")
def db():
    """Ensure the schema exists (auto-created by session_manager) and hand back pymysql kwargs."""
    _ensure_schema()
    return testenv.DB_KWARGS


@pytest.fixture(scope="session")
def seeded_channels(db):
    import pymysql
    conn = pymysql.connect(**testenv.DB_KWARGS)
    try:
        cur = conn.cursor()
        for name, antispam, store_history in SEED_CHANNELS:
            cur.execute("SELECT id FROM channels WHERE name=%s", (name,))
            if cur.fetchone():
                cur.execute("UPDATE channels SET antispam=%s, store_history=%s WHERE name=%s",
                            (antispam, store_history, name))
            else:
                cur.execute(
                    "INSERT INTO channels (name, antispam, store_history) VALUES (%s,%s,%s)",
                    (name, antispam, store_history))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="session")
def server(seeded_channels):
    log = os.path.join(ROOT, "server.log")
    proc = subprocess.Popen(
        [sys.executable, "server.py", "-p", str(testenv.PORT), "-s", testenv.DB_URL],
        cwd=ROOT, env=_CHILD_ENV)
    if not _wait_ready(testenv.HOST, testenv.PORT, timeout=40):
        _terminate(proc)
        pytest.fail("lobby server did not become ready on %s:%d\n--- server.log tail ---\n%s"
                    % (testenv.HOST, testenv.PORT, _tail(log)))
    yield proc
    _terminate(proc)


def _terminate(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def run_script():
    def _run(path):
        r = subprocess.run([sys.executable, path], cwd=ROOT, env=_CHILD_ENV,
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            pytest.fail("%s exited %d\n--- stdout ---\n%s\n--- stderr ---\n%s"
                        % (os.path.basename(path), r.returncode, r.stdout, r.stderr))
        return r
    return _run
