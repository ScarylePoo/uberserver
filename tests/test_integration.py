"""End-to-end tier: socket clients run against a live server (the `server` fixture).

Each script in tests/integration/ is executed as a subprocess and must exit 0.

changeemaildropprobe.py is excluded: it is a positive-control diagnostic probe that drives a
disconnect race and tells a human to grep server.log -- it asserts nothing CI can verify and
always exits 0 when setup succeeds, so it cannot meaningfully pass or fail here.
"""
import glob
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
EXCLUDE = {"changeemaildropprobe.py"}
SCRIPTS = sorted(p for p in glob.glob(os.path.join(HERE, "integration", "*.py"))
                 if os.path.basename(p) not in EXCLUDE)


@pytest.mark.parametrize("script", SCRIPTS, ids=[os.path.basename(p) for p in SCRIPTS])
def test_integration(script, server, run_script):
    run_script(script)
