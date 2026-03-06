"""Central naming helpers for FryNetworks binaries and services.

Keep all filename and unit-name formats here so the rest of the codebase
can import them and remain consistent.
"""
from pathlib import Path
from typing import Optional


def poc_prefix(miner_code: str) -> str:
    return f"FRY_PoC_{miner_code}_v"


def gui_prefix(miner_code: str) -> str:
    return f"FRY_{miner_code}_v"


def poc_asset(miner_code: str, version: str, windows: bool = True) -> str:
    base = f"{poc_prefix(miner_code)}{version}"
    return base + (".exe" if windows else "")


def gui_asset(miner_code: str, version: str, windows: bool = True) -> str:
    base = f"{gui_prefix(miner_code)}{version}"
    return base + (".exe" if windows else "")


def poc_glob(miner_code: str, windows: bool = True) -> str:
    """Return a glob pattern matching PoC assets for this miner.

    Example: FRY_PoC_BM_v*.exe
    """
    return (f"{poc_prefix(miner_code)}*" + (".exe" if windows else ""))


def poc_windows_service_name(miner_code: str, version: str) -> str:
    """Return Windows service name format (no .exe)."""
    return f"FRY_PoC_{miner_code}_v{version}"


def poc_unit_name(miner_code: str) -> str:
    """Return systemd unit filename for service."""
    return f"FRY_PoC_{miner_code}.service"


def is_poc_filename(filename: str) -> bool:
    return "FRY_PoC_" in filename


def is_gui_filename(filename: str) -> bool:
    return filename.startswith("FRY_") and not filename.startswith("FRY_PoC_")
