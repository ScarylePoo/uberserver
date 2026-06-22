"""Worker-tier tests: in-process units run against MariaDB (no server needed).

Each script in tests/worker/ is executed as a subprocess and must exit 0.
"""
import glob
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = sorted(glob.glob(os.path.join(HERE, "worker", "*.py")))


@pytest.mark.parametrize("script", SCRIPTS, ids=[os.path.basename(p) for p in SCRIPTS])
def test_worker(script, db, run_script):
    run_script(script)
