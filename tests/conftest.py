import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="polyweather-test-")
os.environ.setdefault("POLYWEATHER_DATA", _tmp)
os.environ.setdefault("POLYWEATHER_DB", f"sqlite:///{_tmp}/test.db")

import pytest  # noqa: E402

from polyweather.db import init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()
    yield
