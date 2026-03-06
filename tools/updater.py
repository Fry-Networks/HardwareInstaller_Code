"""
FryNetworks Installer Updater

Checks the latest GitHub release for a newer MSI, downloads it, verifies (optional)
SHA256, and invokes msiexec to install. Designed to be run silently from a scheduled
task (per-user, non-elevated).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, cast


DEFAULT_REPO = "FryDevsTestingLab/HardwareInstaller"
DEFAULT_TASK_NAME = "FryNetworksUpdater"
DEFAULT_EMBEDDED_TOKEN = os.getenv("EMBEDDED_GITHUB_TOKEN", "")


def log_path(custom: Optional[Path] = None) -> Path:
    if custom:
        return custom
    base = Path(os.getenv("LOCALAPPDATA", tempfile.gettempdir()))
    return base / "FryNetworks" / "Updater" / "updater.log"


def write_log(msg: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text((dest.read_text() if dest.exists() else "") + msg + "\n", encoding="utf-8")


def normalize_version(ver: str) -> str:
    ver = ver.strip()
    return ver if ver.startswith("v") else f"v{ver}"


def read_version_from_installer(dir_path: Path) -> Optional[str]:
    """
    Look for frynetworks_installer_v*.exe next to the updater and parse the version.
    """
    pattern = "frynetworks_installer_v*.exe"
    for candidate in dir_path.glob(pattern):
        name = candidate.name
        # expect frynetworks_installer_vX.Y.Z.exe
        if "_v" in name:
            part = name.split("_v", 1)[-1].rsplit(".", 1)[0]
            return normalize_version(part)
    return None


def fetch_json(url: str, token: Optional[str] = None) -> dict:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url: str, dest: Path, token: Optional[str] = None) -> None:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


def find_asset(release: dict, suffix: str) -> Optional[dict]:
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(suffix.lower()):
            return asset
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_msiexec(msi_path: Path, quiet: bool, log_file: Path) -> None:
    args = ["msiexec.exe", "/i", str(msi_path)]
    if quiet:
        args.append("/qn")
    write_log(f"Running: {' '.join(args)}", log_file)
    subprocess.Popen(args)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Update FryNetworks Installer from latest GitHub release.")
    p.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo owner/name (default: %(default)s)")
    p.add_argument("--current-version", help="Current version (e.g., v3.6.0). If omitted, infer from installer exe name in the updater directory.")
    p.add_argument("--token", help="GitHub token for higher rate limits/private repos (optional).")
    p.add_argument("--quiet", action="store_true", help="Install MSI silently (/qn).")
    p.add_argument("--log", type=Path, help="Log file path (default: %%LOCALAPPDATA%%/FryNetworks/Updater/updater.log).")
    p.add_argument("--dry-run", action="store_true", help="Do not download/install, just report actions.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    log_file = log_path(args.log)

    try:
        current_version = normalize_version(args.current_version) if args.current_version else None
        if not current_version:
            current_version = read_version_from_installer(Path(__file__).resolve().parent)
        write_log(f"Current version: {current_version or 'unknown'}", log_file)

        token = args.token or os.environ.get("GITHUB_TOKEN") or DEFAULT_EMBEDDED_TOKEN or None

        release = fetch_json(f"https://api.github.com/repos/{args.repo}/releases/latest", token)
        remote_ver = normalize_version(release.get("tag_name", ""))
        write_log(f"Latest release: {remote_ver}", log_file)

        if current_version and remote_ver and remote_ver == current_version:
            write_log("No update needed.", log_file)
            return 0

        msi_asset = find_asset(release, ".msi")
        if not msi_asset:
            write_log("No MSI asset found in latest release.", log_file)
            return 1

        msi_url = msi_asset.get("browser_download_url")
        if not msi_url:
            write_log("MSI asset missing download URL.", log_file)
            return 1
        msi_name = msi_asset.get("name", "update.msi")
        dest = Path(tempfile.gettempdir()) / msi_name

        if args.dry_run:
            write_log(f"[dry-run] Would download {msi_url} to {dest}", log_file)
            return 0

        write_log(f"Downloading {msi_url} to {dest}", log_file)
        download(cast(str, msi_url), dest, token)

        sha_asset = find_asset(release, ".sha256")
        if sha_asset:
            sha_url = sha_asset.get("browser_download_url")
            if not sha_url:
                write_log("Checksum asset missing download URL; skipping checksum verification.", log_file)
            else:
                sha_dest = dest.with_suffix(dest.suffix + ".sha256")
                write_log(f"Downloading checksum {sha_url}", log_file)
                download(cast(str, sha_url), sha_dest, token)
                expected = sha_dest.read_text().split()[0].strip()
                actual = sha256_file(dest)
                if expected.lower() != actual.lower():
                    write_log(f"Checksum mismatch: expected {expected}, got {actual}", log_file)
                    return 1
            write_log("Checksum verified.", log_file)

        run_msiexec(dest, args.quiet, log_file)
        write_log("Update triggered (msiexec launched).", log_file)
        return 0
    except urllib.error.HTTPError as e:
        write_log(f"HTTP error: {e}", log_file)
        return 1
    except Exception as e:  # noqa: BLE001
        write_log(f"Update failed: {e}", log_file)
        return 1


if __name__ == "__main__":
    sys.exit(main())
