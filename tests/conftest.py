"""
tests/conftest.py
    Shared setup: makes the project root importable so `from umpire...`
    works regardless of where pytest is invoked from.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
