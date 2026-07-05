"""Make ``import vitals`` work when pytest runs from the repo root."""

import pathlib
import sys

_SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
