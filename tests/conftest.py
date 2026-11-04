"""Run every test in a throw-away copy of the project folder, never touching real data."""
import os
import shutil
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="kdp_test_"))
shutil.copy(Path(__file__).resolve().parent.parent / "config.yaml", _tmp / "config.yaml")
os.environ["KDP_AGENT_ROOT"] = str(_tmp)

import pytest  # noqa: E402
from kdp_agent import settings  # noqa: E402


@pytest.fixture(autouse=True)
def clean_folders():
    for name in ("books", "data"):
        shutil.rmtree(_tmp / name, ignore_errors=True)
    settings.ensure_folders()
    yield
