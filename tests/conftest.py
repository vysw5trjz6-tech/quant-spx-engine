# Run tests from a tmp dir so that modules which create SQLite caches at
# import time (gamma_exposure, options_flow, iv_rank, oi_delta, ...) don't
# litter the repo root with .db files.

import os
import sys
import tempfile

import pytest

# Make the project root importable regardless of where pytest is invoked.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture(scope="session", autouse=True)
def _isolate_cache_dir():
    tmp = tempfile.mkdtemp(prefix="qspx-tests-")
    cwd = os.getcwd()
    os.chdir(tmp)
    yield tmp
    os.chdir(cwd)
