# Isolate the SQLite caches that modules create at import time
# (gamma_exposure, options_flow, iv_rank, oi_delta, ...) so test runs don't
# litter the repo root or share state with a real deployment.
#
# Module DB paths resolve via db_utils.data_path() (DATA_DIR > Railway volume
# > /tmp) at import time, and test modules are imported during collection --
# before any fixture runs. So DATA_DIR must be set HERE, at conftest import
# time, for the redirect to take effect. The session fixture additionally
# chdirs into the tmp dir to catch anything still writing relative paths at
# runtime (chdir can't happen at import time: it breaks pytest's testpaths
# resolution).

import os
import sys
import tempfile

import pytest

# Make the project root importable regardless of where pytest is invoked.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="qspx-tests-")
os.environ["DATA_DIR"] = _TMP


@pytest.fixture(scope="session", autouse=True)
def _isolate_cache_dir():
    cwd = os.getcwd()
    os.chdir(_TMP)
    yield _TMP
    os.chdir(cwd)
