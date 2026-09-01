# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for NativMix Windows build.

Run from the repository root:
    pyinstaller packaging/win/nativmix.spec

Output: dist/NativMix/  (directory bundle, fed into Inno Setup)
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_all

ROOT = Path(SPECPATH).parent.parent   # repo root, e.g. C:/code/nativmix

# ── PyQt6 ─────────────────────────────────────────────────────────────────────
# collect_all pulls in binaries (Qt DLLs), data (plugins, translations) and
# hidden imports so we don't have to enumerate every Qt submodule manually.
pyqt6_datas, pyqt6_binaries, pyqt6_hidden = collect_all("PyQt6")
zeroconf_datas, zeroconf_binaries, zeroconf_hidden = collect_all("zeroconf")

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    [str(ROOT / "packaging" / "win" / "_entry.py")],
    pathex=[str(ROOT / "lib")],          # so 'import nativmix' resolves
    binaries=[*pyqt6_binaries, *zeroconf_binaries],
    datas=[
        # Application assets (icon, etc.)
        (str(ROOT / "assets" / "icon.ico"),  "assets"),
        (str(ROOT / "assets" / "icon.png"),  "assets"),
        (str(ROOT / "assets" / "icon.svg"),  "assets"),
        # Qt data (plugins, translations, …)
        *pyqt6_datas,
        *zeroconf_datas,
        # mido ships backend Python files as data
        *collect_data_files("mido"),
    ],
    hiddenimports=[
        *pyqt6_hidden,
        *zeroconf_hidden,
        # Qt network is used by QLocalServer / QLocalSocket (IPC)
        "PyQt6.QtNetwork",
        # MIDI stack
        "mido",
        "mido.backends.rtmidi",
        "rtmidi",
        # Windows audio backend
        "pycaw",
        "pycaw.pycaw",
        "comtypes",
        "comtypes.client",
        "comtypes.server",
        "comtypes.server.localserver",
        # Process resolver
        "psutil",
        "psutil._pswindows",
        # Hardware / serial
        "serial",
        "serial.tools.list_ports",
        "serial.tools.list_ports_windows",
        # Misc
        "setproctitle",
        # NativMix modules (ensure nothing is tree-shaken away)
        "nativmix",
        "nativmix.main",
        "nativmix.metadata",
        "nativmix.audio.base",
        "nativmix.audio.wasapi_manager",
        "nativmix.utils.proc_resolver",
        "nativmix.utils.paths",
        "nativmix.utils.config_manager",
        "nativmix.hardware.arduino",
        "nativmix.hardware.midi",
        "nativmix.hardware.remote_midi",
        "nativmix.gui.main_window",
        "nativmix.gui.settings_panel",
        "nativmix.gui.tray_icon",
        "nativmix.gui.theme",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Linux-only modules — safe to exclude on Windows
    excludes=[
        "pulsectl",
        "nativmix.audio.manager",   # PipeWireManager
        "nativmix.utils.routing",   # pw-link helpers
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="nativmix",
    debug=False,
    strip=False,
    upx=True,
    console=False,                        # no black console window
    icon=str(ROOT / "assets" / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        # Qt DLLs must not be UPX-compressed — they self-extract and break
        "Qt6Core.dll",
        "Qt6Gui.dll",
        "Qt6Widgets.dll",
        "Qt6Network.dll",
    ],
    name="NativMix",
)
