#!/usr/bin/env python3
"""
FryNetworks Installer Build CLI Tool

Command-line interface for building the installer with version management.

Usage:
    python build_cli.py                    # Build with current version
    python build_cli.py --version 1.0.1    # Build with specific version
    python build_cli.py --bump patch       # Increment patch version
    python build_cli.py --bump minor       # Increment minor version
    python build_cli.py --bump major       # Increment major version
    python build_cli.py --show             # Show current version
"""

import argparse
import hashlib
import subprocess
import sys
import os
from pathlib import Path
import re
import shutil
from typing import Optional, List


def _detect_platform() -> str:
    """Return 'windows' or 'linux' based on the current host OS."""
    if sys.platform.startswith("win") or os.name == "nt":
        return "windows"
    return "linux"


def _version_key(platform: Optional[str]) -> str:
    return "WINDOWS_VERSION" if (platform or _detect_platform()) == "windows" else "LINUX_VERSION"


def get_current_version(platform: Optional[str] = None):
    """Read current version from version.py."""
    version_file = Path(__file__).parent / "version.py"
    if not version_file.exists():
        return "1.0.0"
    
    content = version_file.read_text()
    key = _version_key(platform)
    match = re.search(rf'{key}\s*=\s*["\']([^"\']+)["\']', content)
    if match:
        return match.group(1)
    return "1.0.0"


def parse_version(version_str):
    """Parse version string into major, minor, patch."""
    parts = version_str.split('.')
    if len(parts) != 3:
        raise ValueError(f"Invalid version format: {version_str}. Expected MAJOR.MINOR.PATCH")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        raise ValueError(f"Invalid version format: {version_str}. All parts must be integers")


def bump_version(current_version, bump_type):
    """Bump version based on type (major, minor, patch)."""
    major, minor, patch = parse_version(current_version)
    
    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        raise ValueError(f"Invalid bump type: {bump_type}. Must be 'major', 'minor', or 'patch'")


def update_version_file(new_version: str, platform: Optional[str] = None):
    """Update version.py with the platform-specific version."""
    version_file = Path(__file__).parent / "version.py"
    
    if not version_file.exists():
        print(f"Error: version.py not found at {version_file}")
        return False
    
    target_platform = platform or _detect_platform()
    key = _version_key(target_platform)
    pattern = rf'{key}\s*=\s*["\'][^"\']+["\']'
    replacement = f'{key} = "{new_version}"'
    content = version_file.read_text()
    content, count = re.subn(pattern, replacement, content, count=1)
    if count == 0:
        print(f"Error: Unable to locate {key} in version.py")
        return False
    
    version_file.write_text(content)
    print(f"V Updated {key} to {new_version}")
    return True


def run_build(version):
    """Run the platform-specific build script with the specified version."""
    build_dir = Path(__file__).parent
    is_windows = sys.platform.startswith('win') or os.name == 'nt'

    build_script = build_dir / ("build_installer.ps1" if is_windows else "build_installer.sh")

    if not build_script.exists():
        print(f"Error: Build script not found at {build_script}")
        return False

    print(f"\n{'='*60}")
    print(f"Building FryNetworks Installer v{version} ({'windows' if is_windows else 'linux'})")
    print(f"{'='*60}\n")

    try:
        if is_windows:
            cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(build_script), "-Version", version]
        else:
            cmd = ["bash", str(build_script), version]

        result = subprocess.run(cmd, cwd=build_script.parent, check=False)
        return result.returncode == 0
    except Exception as e:
        print(f"Error running build script: {e}")
        return False


# --- MSI helpers (WiX) ---

def _guess_installer_exe(dist_dir: Path) -> Optional[Path]:
    """
    Best-effort find the freshly built installer exe.
    Prefers frynetworks_installer*.exe in dist, picking the newest.
    """
    if not dist_dir.exists():
        return None
    candidates: List[Path] = []
    for pattern in ("frynetworks_installer*.exe", "*installer*.exe"):
        candidates.extend(dist_dir.glob(pattern))
    if not candidates:
        return None
    candidates = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def build_msi(
    version: str,
    manufacturer: str = "FryNetworks",
    exe_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    emit_checksum: bool = True,
) -> bool:
    """
    Build MSI using WiX candle/light and build/wix/installer.wxs.
    Requires WiX Toolset 3.x on PATH.
    """
    if not (sys.platform.startswith("win") or os.name == "nt"):
        print("MSI build is only supported on Windows hosts.")
        return False

    wix_dir = Path(__file__).parent / "packaging"
    wxs = wix_dir / "installer.wxs"
    if not wxs.exists():
        print(f"WiX source not found at {wxs}")
        return False

    dist_dir = Path(__file__).parent / "dist"
    exe_path = exe_path or _guess_installer_exe(dist_dir)
    if not exe_path or not exe_path.exists():
        print("Could not locate installer exe. Pass --msi-exe or ensure dist/ contains the built exe.")
        return False
    exe_path = exe_path.resolve()

    exe_name = exe_path.name
    output_dir = (output_dir or (wix_dir / "out")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    wixobj = output_dir / "installer.wixobj"
    msi_out = output_dir / f"fry_installer_{version}.msi"

    candle_exe = shutil.which("candle") or str(Path("C:/Program Files (x86)/WiX Toolset v3.14/bin/candle.exe"))
    light_exe = shutil.which("light") or str(Path("C:/Program Files (x86)/WiX Toolset v3.14/bin/light.exe"))

    candle_cmd = [
        candle_exe,
        f"-dInstallerExe={exe_path}",
        f"-dInstallerExeName={exe_name}",
        f"-dProductVersion={version}",
        f"-dManufacturer={manufacturer}",
        "-ext",
        "WixUIExtension",
        "-ext",
        "WixUtilExtension",
        "-o",
        str(wixobj),
        str(wxs),
    ]

    light_cmd = [
        light_exe,
        "-ext",
        "WixUIExtension",
        "-ext",
        "WixUtilExtension",
        "-o",
        str(msi_out),
        str(wixobj),
    ]

    print(f"\n[MSI] Using exe: {exe_path}")
    print(f"[MSI] Output:    {msi_out}")

    try:
        res_candle = subprocess.run(candle_cmd, cwd=wix_dir, check=False)
        if res_candle.returncode != 0:
            print("candle.exe failed; ensure WiX is installed and on PATH.")
            return False
        res_light = subprocess.run(light_cmd, cwd=wix_dir, check=False)
        if res_light.returncode != 0:
            print("light.exe failed; ensure WiX is installed and on PATH.")
            return False
        print(f"[MSI] Built {msi_out}")

        if emit_checksum:
            sha_path = msi_out.with_suffix(msi_out.suffix + ".sha256")
            h = hashlib.sha256()
            with open(msi_out, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            digest = h.hexdigest()
            sha_path.write_text(f"{digest}  {msi_out.name}\n", encoding="utf-8")
            print(f"[MSI] SHA256: {digest}")
            print(f"[MSI] Checksum written to {sha_path}")
        return True
    except FileNotFoundError:
        print("WiX tools not found (candle/light). Install WiX Toolset 3.x and ensure it's on PATH.")
        return False
    except Exception as e:
        print(f"MSI build failed: {e}")
        return False


def show_version_info():
    """Display current version information."""
    win_version = get_current_version("windows")
    lin_version = get_current_version("linux")
    win_major, win_minor, win_patch = parse_version(win_version)
    lin_major, lin_minor, lin_patch = parse_version(lin_version)
    
    print("\n" + "="*60)
    print("FryNetworks Installer - Version Information")
    print("="*60)
    print(f"Windows Version: {win_version}  (Major={win_major}, Minor={win_minor}, Patch={win_patch})")
    print(f"Linux Version:   {lin_version}  (Major={lin_major}, Minor={lin_minor}, Patch={lin_patch})")
    print("="*60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="FryNetworks Installer Build Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          Build with current version
  %(prog)s --version 1.2.3          Build with specific version
  %(prog)s --bump patch             Increment patch and build
  %(prog)s --bump minor             Increment minor and build
  %(prog)s --bump major             Increment major and build
  %(prog)s --show                   Show current version (no build)
  %(prog)s --bump patch --no-build  Increment version without building
        """
    )
    
    parser.add_argument(
        "--version",
        type=str,
        help="Specific version to build (e.g., 1.0.1)"
    )
    
    parser.add_argument(
        "--bump",
        choices=["major", "minor", "patch"],
        help="Bump version (major, minor, or patch)"
    )
    
    parser.add_argument(
        "--show",
        action="store_true",
        help="Show current version information and exit"
    )
    
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Update version but don't build (used with --bump or --version)"
    )
    parser.add_argument(
        "--msi",
        action="store_true",
        help="On Windows, also build an MSI via WiX after building the EXE"
    )
    parser.add_argument(
        "--msi-only",
        action="store_true",
        help="Build only the MSI from an existing EXE (skip rebuilding the EXE)"
    )
    parser.add_argument(
        "--msi-exe",
        type=str,
        help="Path to the installer EXE to wrap in MSI (default: latest *installer*.exe in dist/)"
    )
    parser.add_argument(
        "--msi-manufacturer",
        type=str,
        default="FryNetworks",
        help="Manufacturer string for MSI metadata"
    )
    parser.add_argument(
        "--msi-out-dir",
        type=str,
        help="Output directory for MSI (default: build/wix/out)"
    )
    parser.add_argument(
        "--msi-no-checksum",
        action="store_true",
        help="Do not write a .sha256 checksum next to the built MSI"
    )
    
    args = parser.parse_args()
    
    # Show version and exit
    if args.show:
        show_version_info()
        return 0
    
    target_platform = _detect_platform()
    current_version = get_current_version(target_platform)
    print(f"Current {target_platform} version: {current_version}")
    
    # Determine target version
    if args.version and args.bump:
        print("Error: Cannot specify both --version and --bump")
        return 1
    
    if args.bump:
        new_version = bump_version(current_version, args.bump)
        print(f"Bumping {target_platform} {args.bump} version: {current_version} -> {new_version}")
        if not update_version_file(new_version, target_platform):
            return 1
        target_version = new_version
    elif args.version:
        try:
            parse_version(args.version)
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        
        print(f"Setting {target_platform} version to: {args.version}")
        if not update_version_file(args.version, target_platform):
            return 1
        target_version = args.version
    else:
        target_version = current_version
    
    # MSI-only path (skip rebuilding EXE)
    if args.msi_only:
        if not (sys.platform.startswith("win") or os.name == "nt"):
            print("MSI builds are only supported on Windows hosts.")
            return 1
        exe_path = Path(args.msi_exe) if args.msi_exe else None
        out_dir = Path(args.msi_out_dir) if args.msi_out_dir else None
        msi_ok = build_msi(
            version=target_version,
            manufacturer=args.msi_manufacturer,
            exe_path=exe_path,
            output_dir=out_dir,
        )
        return 0 if msi_ok else 1

    # Build unless --no-build is specified
    if args.no_build:
        print(f"V {target_platform.capitalize()} version updated to {target_version} (build skipped)")
        return 0
    
    # Run the build
    success = run_build(target_version)
    
    if success:
        print(f"\nV Build completed successfully for version {target_version}")
        if args.msi and (sys.platform.startswith("win") or os.name == "nt"):
            exe_path = Path(args.msi_exe) if args.msi_exe else None
            out_dir = Path(args.msi_out_dir) if args.msi_out_dir else None
            msi_ok = build_msi(
                version=target_version,
                manufacturer=args.msi_manufacturer,
                exe_path=exe_path,
                output_dir=out_dir,
                emit_checksum=not args.msi_no_checksum,
            )
            return 0 if msi_ok else 1
        return 0
    else:
        print(f"\n? Build failed for version {target_version}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
