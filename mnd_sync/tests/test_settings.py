import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import settings


def test_mnd_data_dir_overrides_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MND_DATA_DIR", raising=False)
    assert importlib.reload(settings).DATA_DIR == Path("/data")
    monkeypatch.setenv("MND_DATA_DIR", str(tmp_path))
    assert importlib.reload(settings).DATA_DIR == tmp_path
