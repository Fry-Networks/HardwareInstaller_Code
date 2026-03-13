"""
FryNetworks Installer Version Management

This module provides version information for the installer.
Version follows semantic versioning: MAJOR.MINOR.PATCH
"""

import sys
from typing import Optional, Tuple

WINDOWS_VERSION = "1.0.5"
LINUX_VERSION = "1.1.4"
__build_date__ = None  # Set during build process


def _normalize_platform(platform: Optional[str] = None) -> str:
    """Normalize platform hints into 'windows' or 'linux'."""
    if isinstance(platform, str) and platform:
        normalized = platform.strip().lower()
        if normalized.startswith("win"):
            return "windows"
        if normalized.startswith("lin"):
            return "linux"
    return "windows" if sys.platform.startswith("win") else "linux"


def _split_version(version: str) -> Tuple[int, int, int]:
    """Split a semantic version string into (major, minor, patch)."""
    try:
        major, minor, patch = (int(part) for part in version.split("."))
        return major, minor, patch
    except Exception:
        return 0, 0, 0


def get_version(platform: Optional[str] = None) -> str:
    """Get the current version string for the specified platform."""
    key = _normalize_platform(platform)
    return WINDOWS_VERSION if key == "windows" else LINUX_VERSION


def get_version_tuple(platform: Optional[str] = None) -> Tuple[int, int, int]:
    """Get the version as a tuple (major, minor, patch) for the platform."""
    version = get_version(platform)
    return _split_version(version)


def get_build_date():
    """Get the build date if available."""
    return __build_date__


def get_version_info(platform: Optional[str] = None):
    """Get complete version information as a dictionary."""
    version = get_version(platform)
    major, minor, patch = get_version_tuple(platform)
    return {
        "version": version,
        "platform": _normalize_platform(platform),
        "major": major,
        "minor": minor,
        "patch": patch,
        "build_date": __build_date__,
    }


def get_all_versions():
    """Return the version string for each supported platform."""
    return {
        "windows": WINDOWS_VERSION,
        "linux": LINUX_VERSION,
    }


# Default module-level version info reflects the current runtime platform
__version__ = get_version()
VERSION_MAJOR, VERSION_MINOR, VERSION_PATCH = get_version_tuple()
