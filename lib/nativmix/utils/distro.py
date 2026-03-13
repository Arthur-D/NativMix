"""
Utility for distribution detection.
"""

from pathlib import Path

def is_fedora() -> bool:
    """Check if the current distribution is Fedora or Nobara."""
    return Path("/etc/fedora-release").exists() or Path("/etc/nobara-release").exists()
