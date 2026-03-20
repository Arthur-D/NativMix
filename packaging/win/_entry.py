"""
Windows entry point for PyInstaller bundle.

PyInstaller calls this script directly. It adds sys._MEIPASS to sys.path
so that 'import nativmix' finds the bundled package, then delegates to
the real main() function.
"""
import sys
import os

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    # Bundled: _MEIPASS contains the extracted package tree.
    sys.path.insert(0, sys._MEIPASS)

from nativmix.main import main

main()
