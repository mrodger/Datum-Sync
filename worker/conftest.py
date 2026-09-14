import sys
from pathlib import Path

# The worker's modules are imported as top-level names (`import app`), so
# `pytest worker/tests` from the repository root needs this directory on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
