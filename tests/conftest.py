"""Make the project importable and keep tests fully offline (no API/key)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# A dummy key so config/clients import without a real secret; no calls are made.
os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test-dummy")
