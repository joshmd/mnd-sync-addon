import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("MND_DATA_DIR", "/data"))
