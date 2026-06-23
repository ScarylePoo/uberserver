"""Central, env-driven test configuration for the uberserver test suite.

Defaults reproduce the documented local recipe (MariaDB over the unix socket, OS-user
socket auth, no password) so running scripts directly on a dev machine is unchanged.
CI overrides via environment variables to talk to a MariaDB service over TCP.

Exposes:
    DB_URL     - SQLAlchemy URL string  (worker tests: create_engine(DB_URL))
    DB_KWARGS  - dict for pymysql.connect(**DB_KWARGS)  (e2e tests: direct SQL seed/verify)
    HOST, PORT - the running lobby server's socket address

Environment variables (all optional):
    UBERSERVER_TEST_DB_NAME      default "uberserver_test"
    UBERSERVER_TEST_DB_USER      default "tomjn"
    UBERSERVER_TEST_DB_PASSWORD  default ""
    UBERSERVER_TEST_DB_HOST      default "" -> use unix socket; set non-empty for TCP (CI)
    UBERSERVER_TEST_DB_PORT      default "3306" (only used when DB_HOST is set)
    UBERSERVER_TEST_DB_SOCKET    default "/tmp/mysql.sock" (only used for the socket path)
    UBERSERVER_TEST_HOST         default "127.0.0.1" (lobby server)
    UBERSERVER_TEST_PORT         default "8200" (lobby server)
"""
import os

_NAME = os.environ.get("UBERSERVER_TEST_DB_NAME", "uberserver_test")
_USER = os.environ.get("UBERSERVER_TEST_DB_USER", "tomjn")
_PASSWORD = os.environ.get("UBERSERVER_TEST_DB_PASSWORD", "")
_HOST = os.environ.get("UBERSERVER_TEST_DB_HOST", "")
_PORT = int(os.environ.get("UBERSERVER_TEST_DB_PORT", "3306"))
_SOCKET = os.environ.get("UBERSERVER_TEST_DB_SOCKET", "/tmp/mysql.sock")

HOST = os.environ.get("UBERSERVER_TEST_HOST", "127.0.0.1")
PORT = int(os.environ.get("UBERSERVER_TEST_PORT", "8200"))

if _HOST:
    # TCP (CI): host/port + password auth.
    DB_KWARGS = dict(host=_HOST, port=_PORT, user=_USER, password=_PASSWORD,
                     database=_NAME, autocommit=True)
    DB_URL = "mysql+pymysql://%s:%s@%s:%d/%s" % (_USER, _PASSWORD, _HOST, _PORT, _NAME)
else:
    # unix socket (local dev): socket auth, no password.
    DB_KWARGS = dict(user=_USER, database=_NAME, unix_socket=_SOCKET, autocommit=True)
    DB_URL = "mysql+pymysql://%s@localhost/%s?unix_socket=%s" % (_USER, _NAME, _SOCKET)
