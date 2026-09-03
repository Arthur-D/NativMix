import os
import subprocess
import sys
from pathlib import Path


def test_conftest_forces_offscreen_before_qt_initialization() -> None:
    conftest_path = Path(__file__).with_name("conftest.py")
    script = """
import runpy
import sys

runpy.run_path(sys.argv[1])
from PyQt6.QtGui import QGuiApplication

app = QGuiApplication([])
print(app.platformName())
app.quit()
"""
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "xcb"

    result = subprocess.run(
        [sys.executable, "-c", script, str(conftest_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offscreen"
