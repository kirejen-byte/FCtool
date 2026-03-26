"""
Application path helper for PyInstaller compatibility.
When frozen as an EXE, __file__ points to a temp extraction dir.
Data files (config, caches) should live next to the EXE instead.
"""

import os
import sys


def app_dir() -> str:
    """Return the directory where the app's data files live.

    - Frozen (PyInstaller --onefile): directory containing the .exe
    - Normal Python: directory containing the main script
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))
