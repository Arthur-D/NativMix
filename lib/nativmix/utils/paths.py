"""
Platform detection and path management for NativMix.

Provides OS-specific detection (Arch/CachyOS, Debian/Ubuntu, SteamOS, Windows)
and the canonical paths for config, data, logs, and binaries on each platform.

All paths follow XDG conventions on Linux and the Windows standard AppData layout.
SteamOS is treated as a special Arch-based system with an immutable root
filesystem, so only user-space paths are used there.

Usage
-----
    from nativmix.utils.paths import get_platform, get_config_dir, get_log_dir
"""

from __future__ import annotations

import os
import platform
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System-wide install paths (set by PKGBUILD / AUR)
# ---------------------------------------------------------------------------

_SYSTEM_ASSETS = Path("/usr/share/nativmix/assets")

# Local development path (relative to this file inside the source tree)
_LOCAL_ASSETS = Path(__file__).resolve().parent.parent.parent.parent / "assets"


# ---------------------------------------------------------------------------
# OS Detection
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _read_os_release() -> dict[str, str]:
    """
    Parse /etc/os-release into a key=value dict.

    Returns an empty dict on Windows or when the file is missing.
    """
    result: dict[str, str] = {}
    for path in (Path("/etc/os-release"), Path("/usr/lib/os-release")):
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, _, value = line.partition("=")
                        result[key.strip()] = value.strip().strip('"')
                return result
            except OSError:
                pass
    return result


@lru_cache(maxsize=1)
def get_platform() -> str:
    """
    Return a canonical platform string for the current environment.

    Possible values:
        "arch"    – Arch Linux, CachyOS, Manjaro, etc.
        "debian"  – Debian, Ubuntu, Linux Mint, Pop!_OS, etc.
        "steamos" – SteamOS (Arch-based, immutable root filesystem)
        "windows" – Microsoft Windows
        "linux"   – Any other Linux distribution
        "other"   – macOS, BSDs, unknown

    The value is cached after the first call.
    """
    if platform.system() == "Windows":
        return "windows"

    if platform.system() != "Linux":
        return "other"

    os_release = _read_os_release()
    os_id      = os_release.get("ID", "").lower()
    id_like    = os_release.get("ID_LIKE", "").lower()
    name       = os_release.get("NAME", "").lower()

    # SteamOS check first (it's Arch-based but needs special treatment)
    if "steamos" in name or "steam" in os_id:
        return "steamos"

    # Arch-family detection
    if os_id == "arch" or "arch" in id_like.split():
        return "arch"

    # Debian-family detection
    debian_ids = {"debian", "ubuntu", "linuxmint", "pop", "raspbian", "kali",
                  "elementary", "zorin", "mx", "neon"}
    if os_id in debian_ids or "debian" in id_like.split() or "ubuntu" in id_like.split():
        return "debian"

    return "linux"


def is_arch()    -> bool: return get_platform() == "arch"
def is_debian()  -> bool: return get_platform() == "debian"
def is_steamos() -> bool: return get_platform() == "steamos"
def is_windows() -> bool: return get_platform() == "windows"
def is_linux()   -> bool: return get_platform() in ("arch", "debian", "steamos", "linux")


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def get_config_dir() -> Path:
    """
    Return the user config directory for NativMix.

    Linux / SteamOS  →  $XDG_CONFIG_HOME/nativmix/     (default: ~/.config/nativmix/)
    Windows          →  %APPDATA%\\nativmix\\
    """
    if is_windows():
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "nativmix"

    # Linux (Arch, Debian, SteamOS, generic)
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "nativmix"


def get_data_dir() -> Path:
    """
    Return the user data directory for NativMix.

    Linux / SteamOS  →  $XDG_DATA_HOME/nativmix/     (default: ~/.local/share/nativmix/)
    Windows          →  %APPDATA%\\nativmix\\data\\
    """
    if is_windows():
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "nativmix" / "data"

    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "nativmix"


def get_log_dir() -> Path:
    """
    Return the platform-appropriate log directory.

    Linux / SteamOS  →  $XDG_CACHE_HOME/nativmix/logs  (default: ~/.cache/nativmix/logs)
    Windows          →  %LOCALAPPDATA%\\nativmix\\logs\\
    """
    if is_windows():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "NativMix" / "Logs"

    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "nativmix" / "logs"


def get_binary_dir() -> Path:
    """
    Return the expected binary installation directory.

    Arch / CachyOS   →  /usr/bin/
    Debian / Ubuntu  →  /usr/local/bin/
    SteamOS          →  ~/.local/bin/   (immutable root filesystem)
    Windows          →  %LOCALAPPDATA%\\nativmix\\
    """
    plat = get_platform()
    if plat == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "nativmix"
    if plat == "steamos":
        return Path.home() / ".local" / "bin"
    if plat == "debian":
        return Path("/usr/local/bin")
    # arch / generic linux
    return Path("/usr/bin")


def get_autostart_dir() -> Path:
    """
    Return the XDG autostart directory (always user-space, never system-wide).

    SteamOS, Arch, Debian  →  ~/.config/autostart/
    Windows                →  not applicable (returns empty Path)
    """
    if is_windows():
        return Path()   # autostart is handled via the Windows registry elsewhere
    return Path.home() / ".config" / "autostart"


def get_ipc_socket_path() -> str:
    """
    Return a user-specific, fully qualified socket path for IPC.

    On Linux, this is /tmp/nativmix_ipc_<uid>.sock.
    """
    if is_windows():
        return "nativmix_ipc" # Placeholder for Windows named pipes if needed

    uid = os.getuid()
    return f"/tmp/nativmix_ipc_{uid}.sock"


# ---------------------------------------------------------------------------
# Asset / icon resolution
# ---------------------------------------------------------------------------

def get_icon_path() -> Path | None:
    """
    Return the absolute path to the NativMix application icon.

    Search order:
      1. /usr/share/nativmix/assets/icon.png  (AUR / PKGBUILD system install)
      2. <project_root>/assets/icon.png        (local development checkout)
      3. None → caller should use QIcon.fromTheme("nativmix")
    """
    for assets_dir in (_SYSTEM_ASSETS, _LOCAL_ASSETS):
        candidate = assets_dir / "icon.png"
        if candidate.exists():
            logger.debug("Icon found: %s", candidate)
            return candidate

    logger.debug("No icon file found; caller should use QIcon.fromTheme fallback")
    return None


# ---------------------------------------------------------------------------
# Startup diagnostics
# ---------------------------------------------------------------------------

def log_platform_info() -> None:
    """Log detected platform and all resolved paths at INFO level."""
    plat = get_platform()
    os_release = _read_os_release()
    logger.info(
        "Platform detected: %s (%s %s)",
        plat,
        os_release.get("NAME", platform.system()),
        os_release.get("VERSION_ID", platform.release()),
    )
    logger.info("Config dir : %s", get_config_dir())
    logger.info("Data dir   : %s", get_data_dir())
    logger.info("Log dir    : %s", get_log_dir())
    logger.info("Binary dir : %s", get_binary_dir())
