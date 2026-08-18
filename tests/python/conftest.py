# Ensure the source layout `python/locatorforge/` is importable during testing.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
