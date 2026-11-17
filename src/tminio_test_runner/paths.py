"""Locations shared by the source checkout and the self-contained release archive."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
CONFIG = ROOT / "config"
