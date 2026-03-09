"""
Cross-platform service management for FryNetworks miners.

This module handles:
- Windows service management (NSSM-based)
- Linux systemd service management
- Service installation, removal, and control
- Configuration and monitoring
- Downloading service executables from GitHub
"""

import os
import sys
import json
import shutil
import subprocess
import requests
from pathlib import Path
from typing import Dict, Any, Optional, List
import uuid
import socket
import time

from .key_parser import MinerKeyParser
from . import naming

# Import encryption helpers for miner_config.enc creation
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

# Import the helpers from the tools package directly
from tools.external_api import (
    _get_1password_secret,
    _BUILD_CONFIG,
    get_external_api_client,
    get_external_api_client_if_complete,
)


"""
Resolve GitHub owner/repo/branch and optional token for downloading miner GUI release assets.

Resolution order (per install attempt):
 1) explicit options passed to install_service (github_repo/github_path or github_owner/github_repo, github_token)
 2) environment variables: GITHUB_REPO_PATH (owner/repo), GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH
    and GITHUB_TOKEN / GH_TOKEN
 3) 1Password via the CLI reference keys for repo path (op://VSCode/hardware_exe/Github_repo_test)
    (uses helper in external_api._get_1password_secret)
 4) embedded build_config.json github section (created at build time by build_installer.ps1)

Note: GitHub tokens are no longer required since the repositories are public.
"""


def _resolve_github_info(options: dict, repo_type: str) -> tuple[Optional[str], Optional[str], str, Optional[str]]:
    """Return (owner, repo, branch, token).

    branch defaults to 'main' if not provided.
    repo_type: 'gui' or 'poc' to select which repository configuration to use
    """
    owner = None
    repo = None
    branch = "main"
    token = None

    # 1) Options passed explicitly
    if options:
        # Accept either 'github_repo' (owner/repo) or separate owner/repo
        github_repo = options.get("github_repo") or options.get("github_path")
        if isinstance(github_repo, str) and '/' in github_repo:
            parts = github_repo.split('/', 1)
            owner, repo = parts[0].strip(), parts[1].strip()
        else:
            o = options.get("github_owner") or options.get("github_owner_name")
            r = options.get("github_repo_name") or options.get("github_repo")
            if o and r:
                owner, repo = str(o).strip(), str(r).strip()
        if options.get("github_branch"):
            branch = str(options.get("github_branch")).strip()
        if options.get("github_token"):
            token = str(options.get("github_token")).strip()

    # 2) Environment variables
    if not (owner and repo):
        env_repo_path = os.environ.get("GITHUB_REPO_PATH")
        if env_repo_path and '/' in env_repo_path:
            parts = env_repo_path.split('/', 1)
            owner, repo = parts[0].strip(), parts[1].strip()
        else:
            env_owner = os.environ.get("GITHUB_OWNER")
            env_repo = os.environ.get("GITHUB_REPO")
            if env_owner and env_repo:
                owner, repo = env_owner.strip(), env_repo.strip()
    env_branch = os.environ.get("GITHUB_BRANCH")
    if env_branch:
        branch = env_branch.strip()
    if not token:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    # 3) 1Password (use external_api helper if available)
    try:
        # Only attempt 1Password when running from source (development).
        # Packaged installers MUST NOT prompt end users for the maintainer's 1Password credentials.
        if not getattr(sys, 'frozen', False):
            # Try the specific op paths used by the build process
            if not (owner and repo):
                gh_path = _get_1password_secret('op://VSCode/hardware_exe/Github_repo_test')
                if gh_path and '/' in gh_path:
                    parts = gh_path.split('/', 1)
                    owner, repo = parts[0].strip(), parts[1].strip()
    except Exception:
        # If external_api or op CLI isn't available, ignore and continue
        pass

    # 4) Embedded build config fallback
    try:
        cfg = _BUILD_CONFIG if '_BUILD_CONFIG' in globals() else None
        if isinstance(cfg, dict):
            gh = cfg.get('github', {})
            # Check for repo_type specific configuration (gui/poc)
            if repo_type in gh and isinstance(gh[repo_type], dict):
                repo_config = gh[repo_type]
                if not (owner and repo):
                    gh_path = repo_config.get('path') or repo_config.get('repo')
                    if isinstance(gh_path, str) and '/' in gh_path:
                        parts = gh_path.split('/', 1)
                        owner, repo = parts[0].strip(), parts[1].strip()
                if not token:
                    gh_token = repo_config.get('token')
                    if isinstance(gh_token, str) and gh_token.strip():
                        token = gh_token.strip()
                b = repo_config.get('branch')
                if isinstance(b, str):
                    branch = b.strip()
    except Exception:
        pass

    return owner, repo, branch, token


def _normalize_version_for_platform(version: str, platform: Optional[str]) -> str:
    """Ensure platform-specific version prefixes are applied.

    The external API returns bare semantic versions for Linux (e.g., "1.2.0"),
    but GitHub release tags and asset names are prefixed with "linux-".
    """
    platform_name = (platform or "").lower()
    if platform_name.startswith("linux") and version and not version.lower().startswith("linux-"):
        return f"linux-{version}"
    return version


def _candidate_release_tags(version: Optional[str], platform: Optional[str] = None) -> List[str]:
    """Return the single release tag to try, with required prefixes."""
    version_str = (version or "").strip()
    if not version_str:
        return []

    platform_name = (platform or "").lower()

    # Linux tags must be prefixed with linux-
    if platform_name.startswith("linux") and not version_str.lower().startswith("linux-"):
        version_str = f"linux-{version_str}"

    # All tags must include a leading v (after any linux- prefix)
    if version_str.lower().startswith("linux-"):
        remainder = version_str[len("linux-"):]
        if not remainder.lower().startswith("v"):
            version_str = f"linux-v{remainder}"
    else:
        if not version_str.lower().startswith("v"):
            version_str = f"v{version_str}"

    return [version_str]


def _get_encryption_keys(name: str) -> tuple:
    """Read encryption salt/password for a partner from build config."""
    try:
        cfg = _BUILD_CONFIG if isinstance(_BUILD_CONFIG, dict) else {}
    except Exception:
        cfg = {}
    enc = cfg.get("encryption", {}).get(name, {})
    salt = enc.get("salt", "").encode() if enc.get("salt") else None
    password = enc.get("password", "") if enc.get("password") else None
    if not salt or not password:
        raise RuntimeError(f"Encryption keys for '{name}' not found in build config")
    return salt, password
BRIGHT_INFO_TEXT = (
    "When enabled, your device contributes to the fVPN network, which can boost the value "
    "of your fVPN earnings"
)
BRIGHT_DEFAULT_APP_NAME = "FryNetworks Miner"
# Public repo asset so consent dialog can fetch without auth
BRIGHT_DEFAULT_LOGO_URL = (
    "https://raw.githubusercontent.com/FryDevsTestingLab/HardwareInstaller/main/resources/"
    "frynetworks_logo_256.png"
)


def _normalize_sdk_approval_value(value: Any) -> Any:
    """Coerce SDK approval entries into allowed shapes (bool or dict with approved flag)."""
    if isinstance(value, dict):
        normalized = dict(value)
        if "approved" in normalized:
            normalized["approved"] = bool(normalized.get("approved"))
        return normalized
    return bool(value)


def _build_sdk_approval_payload(options: Optional[dict]) -> dict:
    """Return the plaintext sdk_approvals payload expected by the SDK config encoder."""
    opts = dict(options or {})
    approvals: dict[str, Any] = {}

    explicit = opts.get("sdk_approvals")
    if isinstance(explicit, dict):
        for key, value in explicit.items():
            approvals[str(key).lower()] = _normalize_sdk_approval_value(value)
    else:
        approvals["bright"] = _normalize_sdk_approval_value(opts.get("bright_opt_in", False))
        approvals["honeygain"] = _normalize_sdk_approval_value(opts.get("honeygain_opt_in", False))
        approvals["mysterium"] = _normalize_sdk_approval_value(opts.get("mysterium_opt_in", False))

    for name in ("bright", "honeygain", "mysterium"):
        approvals.setdefault(name, False)

    return {"approvals": approvals}


def _encrypt_sdk_config(payload: dict) -> dict:
    """Encrypt sdk_approvals payload into the sdk_config.enc structure."""
    salt, password = _get_encryption_keys("sdk")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
    token = Fernet(key).encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return {"data": token.decode("utf-8")}


def _locate_sdk_bundle() -> Optional[Path]:
    """Return the SDK directory packaged with the installer, if available."""
    candidates: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(Path(frozen_root))
    candidates.append(Path(__file__).parent.parent)
    for base in candidates:
        sdk_dir = base / "SDK"
        if sdk_dir.exists():
            return sdk_dir
    return None


def _get_partner_build_config(name: str) -> Dict[str, Any]:
    """Read partner integration settings from the embedded build config."""
    try:
        cfg = _BUILD_CONFIG if isinstance(_BUILD_CONFIG, dict) else {}
    except Exception:
        cfg = {}
    partners = cfg.get("partner_integrations", {}) or {}
    data = partners.get(name, {}) or {}
    return data


def _resolve_partner_secret(name: str, field: str, options: Optional[dict], env_var: str) -> Optional[str]:
    """Resolve partner secret from options, environment, or embedded config."""
    if options:
        manual = options.get(f"{name}_{field}")
        if isinstance(manual, str) and manual.strip():
            return manual.strip()
    # Do NOT fall back to environment variables. Partner secrets must be
    # provided via the embedded build config (created at build time from 1Password).
    data = _get_partner_build_config(name)
    secret = data.get(field)
    enabled = bool(data.get("enabled", False))
    if not enabled:
        return None
    if isinstance(secret, str) and secret.strip():
        return secret.strip()
    # If the build claims the integration is enabled but no secret is present,
    # this indicates a build-time configuration error. Fail fast so the issue
    # surfaces rather than silently skipping the integration.
    raise RuntimeError(
        f"Required partner secret '{name}.{field}' is missing from embedded build config. "
        "Ensure the value is provided via 1Password during the build."
    )


def _write_partner_secret_file(
    dest: Path,
    payload: Dict[str, Any],
    *,
    salt: bytes,
    password: str,
) -> None:
    """Encrypt partner secret using PBKDF2 + Fernet and write JSON payload."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
    token = Fernet(key)
    encrypted = {
        "data": token.encrypt(json.dumps(payload).encode("utf-8")).decode("utf-8"),
        "version": "1.0",
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(encrypted, fh, indent=2)


def _configure_honeygain_assets(base_dir: Path, sdk_root: Path, api_key: str, platform: str) -> list[str]:
    """Copy Honeygain SDK assets and create config + encrypted secret."""
    actions: list[str] = []
    sdk_dest = base_dir / "SDK"
    sdk_dest.mkdir(parents=True, exist_ok=True)

    win_src = sdk_root / "windows-honeygain-sdk"
    linux_src = sdk_root / "linux-honeygain-sdk"
    is_windows = platform.startswith("win")
    if is_windows:
        if win_src.exists():
            shutil.copytree(win_src, sdk_dest / "windows-honeygain-sdk", dirs_exist_ok=True)
            actions.append("Copied windows-honeygain-sdk assets")
        else:
            raise FileNotFoundError("Missing windows-honeygain-sdk assets in SDK bundle")
    else:
        if linux_src.exists():
            shutil.copytree(linux_src, sdk_dest / "linux-honeygain-sdk", dirs_exist_ok=True)
            actions.append("Copied linux-honeygain-sdk assets")
        else:
            raise FileNotFoundError("Missing linux-honeygain-sdk assets in SDK bundle")

    config_dir = base_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    log_dir = base_dir / "logs" / "honeygain"
    log_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = config_dir / "honeygain.json"
    honeygain_config = {
        "enabled": False,
        "sdk_root": "SDK",
        "library_path": None,
        "log_dir": str(log_dir),
        "poll_interval_seconds": 20,
    }
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(honeygain_config, fh, indent=2)
    actions.append(f"Wrote {cfg_path}")

    secret_path = config_dir / "honeygain.enc"
    honeygain_payload = {
        "api_key": api_key.strip(),
        "created_by": "installer",
        "config_version": "1.0",
    }
    _write_partner_secret_file(
        secret_path,
        honeygain_payload,
        salt=_get_encryption_keys("honeygain")[0],
        password=_get_encryption_keys("honeygain")[1],
    )
    actions.append(f"Wrote {secret_path}")
    return actions


def _configure_bright_assets(
    base_dir: Path, sdk_root: Path, app_id: str, bright_meta: Optional[dict] = None
) -> list[str]:
    """Copy Bright SDK assets (Windows only) and create config + consent file.

    bright_meta can include:
        app_name: friendly name shown on Bright consent
        logo_link: URL to logo shown on Bright consent
        benefit / agree_btn / disagree_btn / opt_out_txt: optional consent strings
    """
    actions: list[str] = []
    sdk_dest = base_dir / "SDK"
    sdk_dest.mkdir(parents=True, exist_ok=True)

    canonical_src = sdk_root / "windows-bright-sdk"
    bright_sdk_dest = sdk_dest / "windows-bright-sdk"
    copied = False

    if canonical_src.exists():
        shutil.copytree(canonical_src, bright_sdk_dest, dirs_exist_ok=True)
        actions.append("Copied windows-bright-sdk assets")
        copied = True

    if not copied:
        raise FileNotFoundError("Missing Bright SDK assets in SDK bundle")

    config_dir = base_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = config_dir / "bright.json"
    bright_config = {
        "enabled": False,
        "app_name": "Fry Bandwidth Miner",
        "lang": "en",
        "info_text": BRIGHT_INFO_TEXT,
        "poll_interval_seconds": 20,
    }
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(bright_config, fh, indent=2)
    actions.append(f"Wrote {cfg_path}")

    # Bright consent config (used by lum_sdk.dll / net_updater32.exe) lives alongside the SDK
    meta = dict(bright_meta or {})
    brd_cfg_path = bright_sdk_dest / "brd_config.json"
    brd_cfg = {
        "app_id": app_id.strip(),
        "app_name": meta.get("app_name") or BRIGHT_DEFAULT_APP_NAME,
        "logo_link": meta.get("logo_link") or BRIGHT_DEFAULT_LOGO_URL,
        "consent": bool(meta.get("consent", False)),
    }
    for key in ("benefit", "agree_btn", "disagree_btn", "opt_out_txt"):
        val = meta.get(key)
        if val:
            brd_cfg[key] = val
    with open(brd_cfg_path, "w", encoding="utf-8") as fh:
        json.dump(brd_cfg, fh, indent=2)
    actions.append(f"Wrote {brd_cfg_path}")
    return actions


def _configure_mysterium_assets(base_dir: Path, sdk_root: Path, platform: str) -> list[str]:
    """Copy Mysterium SDK assets for BM and create config.
    
    Credentials (API key, payout address, registration token) are handled by the GUI at runtime.
    """
    actions: list[str] = []
    sdk_dest = base_dir / "SDK"
    sdk_dest.mkdir(parents=True, exist_ok=True)

    is_windows = platform.startswith("win")
    
    if is_windows:
        src_folder = "windows-myst-sdk"
        executable_name = "myst.exe"
        supervisor_name = "myst_supervisor.exe"
    else:
        src_folder = "linux-amd64-myst-sdk"
        executable_name = "myst"
        supervisor_name = "myst_supervisor"

    myst_src = sdk_root / src_folder
    mysterium_sdk_dest = sdk_dest / src_folder

    if myst_src.exists():
        mysterium_sdk_dest.mkdir(parents=True, exist_ok=True)
        
        # Copy main executable
        myst_exe = myst_src / executable_name
        if myst_exe.exists():
            shutil.copy2(myst_exe, mysterium_sdk_dest / executable_name)
            if not is_windows:
                # Make executable on Linux
                import stat
                (mysterium_sdk_dest / executable_name).chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            actions.append(f"Copied {executable_name} to SDK folder")
        else:
            raise FileNotFoundError(f"Missing {executable_name} in {src_folder}")
        
        # Copy supervisor if present
        myst_supervisor = myst_src / supervisor_name
        if myst_supervisor.exists():
            shutil.copy2(myst_supervisor, mysterium_sdk_dest / supervisor_name)
            if not is_windows:
                import stat
                (mysterium_sdk_dest / supervisor_name).chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            actions.append(f"Copied {supervisor_name} to SDK folder")
        
        # Copy config folder if present
        config_src = myst_src / "config"
        if config_src.exists() and config_src.is_dir():
            shutil.copytree(config_src, mysterium_sdk_dest / "config", dirs_exist_ok=True)
            actions.append("Copied mysterium config assets")
    else:
        raise FileNotFoundError(f"Missing {src_folder} assets in SDK bundle")

    config_dir = base_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    log_dir = base_dir / "logs" / "mysterium"
    log_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = config_dir / "mysterium.json"
    mysterium_config = {
        "enabled": False,
        "sdk_root": "SDK",
        "executable_path": f"SDK/{src_folder}/{executable_name}",
        "log_dir": str(log_dir),
        "poll_interval_seconds": 20,
    }
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(mysterium_config, fh, indent=2)
    actions.append(f"Wrote {cfg_path}")

    return actions


def _prepare_partner_integrations(
    miner_code: str,
    base_dir: Path,
    options: Optional[dict],
    platform: str,
) -> list[str]:
    """Create Honeygain/Bright/Mysterium assets for BM installs.
    
    New behavior (v3.9.0+):
    - Installer ALWAYS stages all available SDKs for BM (no consent UI in installer)
    - All opt-ins are set to False by default in sdk_config.enc
    - GUI handles consent dialogs and activation
    - Each SDK unlocks +0.25x fVPN reward multiplier (max 1.00x with all 3)
    """
    opts = dict(options or {})
    if str(miner_code).upper() != "BM":
        return []

    stage_map = opts.get("_stage_partner_sdks")
    stage_all = isinstance(stage_map, bool) and stage_map
    stage_hg = stage_all or (isinstance(stage_map, dict) and stage_map.get("honeygain", False))
    stage_bright = stage_all or (isinstance(stage_map, dict) and stage_map.get("bright", False))
    stage_mysterium = stage_all or (isinstance(stage_map, dict) and stage_map.get("mysterium", False))

    # New flow: stage when available, but opt-in values should all be False
    # (GUI handles activation, not installer)
    hg_needed = stage_hg
    bright_needed = stage_bright
    mysterium_needed = stage_mysterium

    requested = hg_needed or bright_needed or mysterium_needed
    if not requested:
        return []

    sdk_root = _locate_sdk_bundle()
    if sdk_root is None:
        raise RuntimeError("SDK asset bundle is missing from the installer")

    actions: list[str] = []
    
    if hg_needed:
        api_key = _resolve_partner_secret("honeygain", "api_key", options, "HONEYGAIN_API_KEY")
        if api_key:
            # Stage Honeygain SDK with credentials (opt-in will be False)
            actions.extend(_configure_honeygain_assets(base_dir, sdk_root, api_key, platform))
        # else: Silently skip if credentials not available - GUI will handle

    if bright_needed:
        app_id = _resolve_partner_secret("bright", "app_id", options, "BRIGHT_APP_ID")
        if app_id and platform.startswith("win"):
            # Stage Bright SDK with credentials (opt-in will be False)
            bright_meta = {}
            cfg_meta = opts.get("bright_config")
            if isinstance(cfg_meta, dict):
                bright_meta = cfg_meta
            actions.extend(_configure_bright_assets(base_dir, sdk_root, app_id, bright_meta))
        # else: Silently skip if credentials not available or not Windows - GUI will handle

    if mysterium_needed:
        # Mysterium credentials are handled by the GUI at runtime
        # Always stage Mysterium SDK files (opt-in will be False)
        actions.extend(_configure_mysterium_assets(base_dir, sdk_root, platform))

    return actions


def get_external_ip() -> str:
    """
    Detect external IP address using a public IP service.

    Returns:
        str: External IP address (IPv4 or IPv6)

    Raises:
        RuntimeError: If IP detection fails
    """
    url = "https://api.ipify.org"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        ip = response.text.strip()

        if not ip:
            raise RuntimeError("Empty response from IP detection service")

        return ip

    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to detect external IP address: {e}")


class ServiceManager:
    """Cross-platform service management for miner installations."""
    
    def __init__(self, miner_code: str, version: str = "1.0.0"):
        """
        Initialize service manager.
        
        Args:
            miner_code: The miner code (BM, IDM, etc.)
            version: Software version
        """
        self.miner_code = miner_code.upper()
        self.version = version
        self.platform = sys.platform
        
        # Platform-specific configuration
        if self.platform.startswith('win'):
            self.service_manager = WindowsServiceManager(miner_code, version)
        else:
            self.service_manager = LinuxServiceManager(miner_code, version)
    
    def _get_platform_for_api(self) -> str:
        """Convert sys.platform to API platform string."""
        return "windows" if self.platform.startswith('win') else "linux"
    
    def install_service(self, miner_key: str, **options) -> Dict[str, Any]:
        """
        Install the miner service.
        
        Args:
            miner_key: Validated miner key
            **options: Installation options including:
                - progress_callback: Optional callback function for progress updates
            
        Returns:
            Installation result dictionary
        """
        return self.service_manager.install_service(miner_key, **options)
    
    def uninstall_service(
        self,
        install_dir: Optional[str] = None,
        preserve_data: bool = False,
        preserve_gui_processes: bool = False,
        **options,
    ) -> Dict[str, Any]:
        """Uninstall the miner service."""
        if install_dir is not None:
            options.setdefault("install_dir", install_dir)
        options.setdefault("preserve_data", preserve_data)
        if preserve_gui_processes:
            options.setdefault("preserve_gui_processes", True)
        return self.service_manager.uninstall_service(**options)
    
    def start_service(self) -> Dict[str, Any]:
        """Start the miner service."""
        return self.service_manager.start_service()
    
    def stop_service(self) -> Dict[str, Any]:
        """Stop the miner service.""" 
        return self.service_manager.stop_service()
    
    def get_service_status(self) -> str:
        """Get current service status."""
        return self.service_manager.get_service_status()
    
    def configure_autostart(self, enabled: bool) -> Dict[str, Any]:
        """Configure service autostart."""
        return self.service_manager.configure_autostart(enabled)
    
    def get_service_logs(self, lines: int = 50) -> Dict[str, str]:
        """Get recent service log entries."""
        return self.service_manager.get_service_logs(lines)


class WindowsServiceManager:
    """Windows-specific service management using NSSM."""
    # Shared SDK config creation for Windows installs
    def __init__(self, miner_code: str, version: str):
        """Initialize Windows service manager."""
        self.miner_code = miner_code
        self.version = version
        self.service_name = f"FRY_PoC_{miner_code}_v{version}"
        self.base_dir = self._get_base_directory()
        self.github_token: Optional[str] = None

    def _get_platform_for_api(self) -> str:
        return "windows"
    
    def _get_base_directory(self) -> Path:
        """Get the base installation directory."""
        programdata = os.environ.get("PROGRAMDATA", r"C:\\ProgramData")
        return Path(programdata) / "FryNetworks" / f"miner-{self.miner_code}"

    def _cleanup_old_windows_services(self) -> dict:
        """Remove older FRY PoC services so updates don't leave stale entries."""
        summary = {"actions": [], "warnings": []}
        prefix = naming.poc_prefix(self.miner_code)
        current = self.service_name
        old_services: list[str] = []

        # Discover existing PoC services for this miner code
        try:
            query = subprocess.run(
                ["sc", "query", "state=", "all"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if query.returncode == 0:
                for line in query.stdout.splitlines():
                    line = line.strip()
                    if line.upper().startswith("SERVICE_NAME:"):
                        svc_name = line.split(":", 1)[1].strip()
                        if svc_name.startswith(prefix) and svc_name != current:
                            old_services.append(svc_name)
            else:
                summary["warnings"].append("Could not query services to clean old PoC versions")
                return summary
        except Exception as e:
            summary["warnings"].append(f"Failed to query services for cleanup: {e}")
            return summary

        # Stop, kill, and delete any older versions we find
        for svc_name in old_services:
            summary["actions"].append(f"Found old PoC service: {svc_name}")
            try:
                subprocess.run(["sc", "stop", svc_name], capture_output=True, text=True, timeout=10, check=False)
                time.sleep(1)
            except Exception as e:
                summary["warnings"].append(f"Failed to stop {svc_name}: {e}")

            exe_name = f"{svc_name}.exe"
            try:
                import psutil

                for proc in psutil.process_iter(["pid", "name", "exe"]):
                    try:
                        proc_exe = proc.info.get("exe") or ""
                        proc_name = proc.info.get("name") or ""
                        if proc_exe and Path(proc_exe).name.lower() == exe_name.lower():
                            proc.kill()
                            summary["actions"].append(f"Killed process for {svc_name} (PID {proc.pid})")
                        elif proc_name.lower() == exe_name.lower():
                            proc.kill()
                            summary["actions"].append(f"Killed process for {svc_name} (PID {proc.pid})")
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except ImportError:
                try:
                    subprocess.run(["taskkill", "/F", "/IM", exe_name], capture_output=True, text=True, timeout=10, check=False)
                except Exception:
                    pass
            except Exception as e:
                summary["warnings"].append(f"Failed to kill processes for {svc_name}: {e}")

            try:
                delete_res = subprocess.run(["sc", "delete", svc_name], capture_output=True, text=True, timeout=10, check=False)
                if delete_res.returncode == 0:
                    summary["actions"].append(f"Deleted old service {svc_name}")
                else:
                    summary["warnings"].append(f"Deleting {svc_name} returned {delete_res.returncode}: {delete_res.stderr or delete_res.stdout}")
            except Exception as e:
                summary["warnings"].append(f"Failed to delete {svc_name}: {e}")

            exe_path = self.base_dir / exe_name
            if exe_path.exists():
                try:
                    exe_path.unlink()
                    summary["actions"].append(f"Removed old binary {exe_path.name}")
                except Exception as e:
                    summary["warnings"].append(f"Could not remove {exe_path.name}: {e}")

        return summary

    def _ensure_geolite_database(self, embedded_dir: Path) -> None:
        """Install the GeoLite2 database once so all binaries can use it."""
        try:
            programdata = os.environ.get("PROGRAMDATA", r"C:\\ProgramData")
            target_dir = Path(programdata) / "FryNetworks" / "GeoLite2"
            target_file = target_dir / "GeoLite2-Country.mmdb"
            if target_file.exists():
                return  # Already provisioned
            source_file = embedded_dir / "GeoLite2-Country.mmdb"
            if not source_file.exists():
                print("[warning] GeoLite2 database not found in embedded resources; skipping copy")
                return
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
            print(f"[info] Installed GeoLite2 database to {target_file}")
        except Exception as exc:
            print(f"[warning] Could not install GeoLite2 database: {exc}")

    def _migrate_config_files(self) -> None:
        """Move legacy root-level ENC files into the config/ folder if found.

        Safe no-op if files are already in the right place.
        """
        try:
            config_dir = self.base_dir / "config"
            config_dir.mkdir(exist_ok=True)
            for name in ("miner_config.enc", "install_config.enc", "sdk_config.enc", "installer_config.json"):
                root_path = self.base_dir / name
                new_path = config_dir / name
                if root_path.exists():
                    # If new_path doesn't exist or we want to prefer root contents, move it
                    try:
                        if not new_path.exists():
                            root_path.replace(new_path)
                        else:
                            # If both exist, prefer config version and remove root
                            root_path.unlink(missing_ok=True)
                    except Exception:
                        # Best effort; ignore failures
                        pass
        except Exception:
            pass
    
    def _create_encrypted_miner_config(self, miner_key: str) -> bool:
        """Create encrypted miner_config.enc file per BUILD_GUIDE specification.
        
        This uses the same encryption approach as tools/create_miner_config.py
        to create a secure, encrypted configuration file containing the miner key.
        The service binary will read this file at runtime.
        """
        try:
            # Use the same fixed salt and password as create_miner_config.py for compatibility
            salt = b'miner_config_salt_v1'
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            
            # Derive encryption key
            password = "miner_config_encryption_key_v1".encode()
            key = base64.urlsafe_b64encode(kdf.derive(password))
            
            # Create config data
            config_data = {
                "miner_key": miner_key,
                "created_by": "installer",
                "config_version": "1.0",
                "measurement_intervals": {
                    "bandwidth": 10,
                    "satellite": 10,
                    "radiation": 10,
                    "decibel": 2,
                    "aem": 600,
                    "tools": 60
                }
            }

            # Do NOT embed the API bearer token inside miner_config.enc —
            # keep it only in the installer build_config.json so the installer
            # and service can access it separately at runtime.

            # If this is BM, add partner-specific values from the embedded build config only.
            # Do NOT use environment-variable fallbacks; the build must embed these secrets.
            if self.miner_code == 'BM':
                from tools.external_api import _BUILD_CONFIG
                partner_cfg = _BUILD_CONFIG.get('partner_integrations', {}) if isinstance(_BUILD_CONFIG, dict) else {}

                # Honeygain
                hg_cfg = partner_cfg.get('honeygain', {}) or {}
                if hg_cfg.get('enabled'):
                    hg_key = hg_cfg.get('api_key')
                    if not hg_key:
                        raise RuntimeError("Embedded build config marks honeygain enabled but no api_key present")
                    config_data['honeygain_api_key'] = hg_key

                # Bright
                br_cfg = partner_cfg.get('bright', {}) or {}
                if br_cfg.get('enabled'):
                    br_id = br_cfg.get('app_id')
                    if not br_id:
                        raise RuntimeError("Embedded build config marks bright enabled but no app_id present")
                    config_data['bright_app_id'] = br_id

                # Mysterium
                myst_cfg = partner_cfg.get('mysterium', {}) or {}
                if myst_cfg.get('enabled'):
                    payout = myst_cfg.get('payout_addr') or myst_cfg.get('payout')
                    reg = myst_cfg.get('reg_token')
                    api = myst_cfg.get('api_key')
                    if not (payout and reg and api):
                        raise RuntimeError("Embedded build config marks mysterium enabled but missing one or more credentials")
                    config_data['mysterium_payout_addr'] = payout
                    config_data['mysterium_reg_token'] = reg
                    config_data['mysterium_api_key'] = api

            # Encrypt
            f = Fernet(key)
            config_json = json.dumps(config_data)
            encrypted_data = f.encrypt(config_json.encode())

            encrypted_config = {
                "data": encrypted_data.decode(),
                "version": "1.0"
            }

            # Write to config/miner_config.enc (new location)
            config_dir = self.base_dir / "config"
            try:
                config_dir.mkdir(exist_ok=True)
            except Exception:
                pass
            new_config_path = config_dir / "miner_config.enc"
            with open(new_config_path, 'w') as cf:
                json.dump(encrypted_config, cf)

            # Remove legacy root copy if present
            root_config_path = self.base_dir / "miner_config.enc"
            try:
                if root_config_path.exists():
                    root_config_path.unlink()
            except Exception:
                pass
            
            print(f"✓ Created encrypted miner config: {new_config_path}")
            return True
            
        except Exception as e:
            print(f"✗ Failed to create encrypted miner config: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _create_install_config_file(self, install_id: str, options: dict) -> None:
        """Create encrypted install_config.enc file for the service.
        
        This file is required by the service at startup to verify lease ownership.
        Uses the same encryption approach as create_install_config.py for compatibility.
        """
        try:
            # Use the same fixed salt and password as create_install_config.py for compatibility
            salt = b'install_config_salt_v1'
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            
            # Derive encryption key
            password = "install_config_encryption_key_v1".encode()
            key = base64.urlsafe_b64encode(kdf.derive(password))
            
            # Create config data
            config_data = {
                "install_id": install_id,
                "lease_acquired_at": options.get('lease_acquired_at', time.strftime("%Y-%m-%dT%H:%M:%S")),
                "hostname": socket.gethostname(),
                "os": f"{sys.platform}",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "config_version": "1.0"
            }

            # Always include external IP for all miners (enables IP tracking via heartbeat)
            external_ip = options.get('external_ip')
            if external_ip:
                config_data["external_ip"] = external_ip
                config_data["ip_detected_at"] = options.get('ip_detected_at', time.strftime("%Y-%m-%dT%H:%M:%S"))
            
            installer_version = options.get('installer_version', '1.0.0')
            if installer_version:
                config_data["installer_version"] = installer_version
            # Include version platform for GUI/service coordination
            version_platform = options.get('version_platform')
            if isinstance(version_platform, str) and version_platform:
                config_data["version_platform"] = version_platform
            
            # Encrypt
            f = Fernet(key)
            config_json = json.dumps(config_data)
            encrypted_data = f.encrypt(config_json.encode())
            
            encrypted_config = {
                "data": encrypted_data.decode(),
                "version": "1.0"
            }
            
            # Write to config/install_config.enc (new location)
            config_dir = self.base_dir / "config"
            try:
                config_dir.mkdir(exist_ok=True)
            except Exception:
                pass
            new_config_path = config_dir / "install_config.enc"
            with open(new_config_path, 'w') as cf:
                json.dump(encrypted_config, cf)

            # Also write a plaintext installer_config.json for GUI/service shared settings
            try:
                installer_cfg = {
                    "install_id": install_id,
                    "installer_version": installer_version,
                    "version_platform": version_platform or "",
                    "created_at": config_data.get("created_at"),
                }
                with open(config_dir / "installer_config.json", 'w', encoding='utf-8') as jf:
                    json.dump(installer_cfg, jf, indent=2)
            except Exception:
                # Non-fatal: continue even if plaintext config cannot be written
                pass

            # Remove legacy root copy if present
            root_config_path = self.base_dir / "install_config.enc"
            try:
                if root_config_path.exists():
                    root_config_path.unlink()
            except Exception:
                pass

            print(f"✓ Created encrypted install config: {new_config_path}")

        except Exception as e:
            print(f"✗ Failed to create encrypted install config: {e}")
            import traceback
            traceback.print_exc()
            raise RuntimeError(f"Failed to create install config: {e}")

    def update_installer_config(self, version_platform: str, installer_version: Optional[str] = None) -> bool:
        """Write or update plaintext installer_config.json with version platform.

        This is a lightweight, non-encrypted config used by GUI and service
        to coordinate which version platform to use (windows/linux/test-windows/test-linux).
        Returns True on success.
        """
        try:
            vp = str(version_platform or "").strip()
            if not vp:
                return False
            config_dir = self.base_dir / "config"
            try:
                config_dir.mkdir(exist_ok=True)
            except Exception:
                pass
            cfg_path = config_dir / "installer_config.json"
            data = {
                "version_platform": vp,
                "installer_version": installer_version or "",
            }
            with open(cfg_path, 'w', encoding='utf-8') as jf:
                json.dump(data, jf, indent=2)
            return True
        except Exception:
            return False

    def _write_ui_prefs(self, options: dict) -> None:
        """Persist simple UI preferences (e.g., screen size) to config/ui_prefs.json."""
        screen_size = (options or {}).get("screen_size")
        if not screen_size:
            return

        try:
            config_dir = self.base_dir / "config"
            config_dir.mkdir(exist_ok=True)
            ui_path = config_dir / "ui_prefs.json"

            # Load old data if present
            try:
                data = json.load(open(ui_path, "r", encoding="utf-8")) if ui_path.exists() else {}
            except Exception:
                data = {}

            # Add settings
            data["screen_size"] = str(screen_size)
            data["_comment"] = (
                "Screen-size presets (min → target): "
                "mobile 320×480→480×820, tablet 680×760→900×980, "
                "laptop 1100×780→1280×900, desktop 1280×900→1440×950, "
                "ultrawide 1440×950→1800×1000"
            )

            # Save
            with open(ui_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)

            print(f'✓ Wrote UI prefs: {ui_path}')

        except Exception as e:
            print(f'✗ Failed to write UI prefs: {e}')


    def _create_sdk_config_file(self, options: dict) -> bool:
        """Create encrypted sdk_config.enc reflecting SDK approvals."""
        try:
            payload = _build_sdk_approval_payload(options)
            encrypted_config = _encrypt_sdk_config(payload)

            config_dir = self.base_dir / "config"
            try:
                config_dir.mkdir(exist_ok=True)
            except Exception:
                pass

            sdk_config_path = config_dir / "sdk_config.enc"
            with open(sdk_config_path, "w", encoding="utf-8") as fh:
                json.dump(encrypted_config, fh)

            # Remove legacy root copy if present
            try:
                legacy_path = self.base_dir / "sdk_config.enc"
                if legacy_path.exists():
                    legacy_path.unlink()
            except Exception:
                pass

            print(f"✓ Created encrypted SDK approvals: {sdk_config_path}")
            return True
        except Exception as e:
            print(f"✗ Failed to create sdk_config.enc: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def install_service(self, miner_key: str, **options) -> Dict[str, Any]:
        """Install Windows service by instructing the miner GUI to do it."""
        result = {"success": False, "message": "", "actions": []}
        
        try:
            options = dict(options or {})
            # Allow overriding base installation directory
            install_dir_opt = options.get("install_dir")
            if install_dir_opt:
                self.base_dir = Path(install_dir_opt)

            # Ensure any legacy files are migrated into config/
            try:
                self._migrate_config_files()
            except Exception:
                pass

            # Remove any older PoC services so only the new version remains registered
            cleanup = self._cleanup_old_windows_services()
            if cleanup.get("actions"):
                result.setdefault("actions", []).extend(cleanup["actions"])
            if cleanup.get("warnings"):
                result.setdefault("warnings", []).extend(cleanup["warnings"])

            # Capture GitHub token from options or environment (used for release asset downloads)
            option_token = options.get("github_token")
            if option_token:
                self.github_token = option_token
            else:
                env_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
                if env_token:
                    self.github_token = env_token

            # Preflight: Check GUI and PoC asset availability BEFORE any side-effects (like creating folders)
            try:
                pre_ok, pre_attempts, _, _, pre_error = self._preflight_check_binaries(options)
            except Exception as _pre_e:
                # Fail safe: if preflight throws, continue as not ok with generic message
                pre_ok, pre_attempts, pre_error = False, [], f"Preflight check failed: {_pre_e}"

            result["download_attempts"] = pre_attempts
            result.setdefault("actions", []).append(f"Download attempts: {pre_attempts}")
            if not pre_ok:
                # If preflight says unavailable, stop here
                result["message"] = pre_error or "Required release assets are not available"
                return result
            
            # Create installation directory
            self.base_dir.mkdir(parents=True, exist_ok=True)
            result["actions"].append(f"Created directory: {self.base_dir}")

            install_id = self._ensure_install_id(options)

            # Download/copy NSSM and miner GUI
            copy_result = self._copy_service_files(options)
            # _copy_service_files returns (success, attempts, gui_version, poc_version)
            copy_ok, attempts, gui_version, poc_version = copy_result

            result["download_attempts"] = attempts
            result.setdefault("actions", []).append(f"Download attempts: {attempts}")
            if not copy_ok:
                # If user cancelled during download, surface a clean cancelled state
                try:
                    if attempts and isinstance(attempts[-1], dict) and attempts[-1].get("cancelled"):
                        result["message"] = "Installation cancelled by user"
                        result["cancelled"] = True
                        return result
                except Exception:
                    pass
                # Derive a more specific error message from attempts when possible
                message = "Failed to download/install miner files"
                try:
                    # Classify attempts by asset type using name (PoC assets contain 'PoC')
                    gui_attempts = [a for a in attempts if isinstance(a, dict) and a.get("name") and "PoC" not in str(a.get("name"))]
                    poc_attempts = [a for a in attempts if isinstance(a, dict) and a.get("name") and "PoC" in str(a.get("name"))]

                    def any_success(atts: list[dict]) -> bool:
                        return any(a.get("success") is True for a in atts)

                    def any_404(atts: list[dict]) -> bool:
                        return any(int(a.get("status_code", 0) or 0) == 404 for a in atts)

                    gui_ok = any_success(gui_attempts)
                    poc_ok = any_success(poc_attempts)

                    if not gui_ok and poc_ok:
                        if any_404(gui_attempts):
                            message = "GUI version not available (release tag not published)"
                        else:
                            message = "GUI download failed"
                    elif gui_ok and not poc_ok:
                        if any_404(poc_attempts):
                            message = "PoC version not available (release tag not published)"
                        else:
                            message = "PoC download failed"
                    elif not gui_ok and not poc_ok:
                        message = "GUI and PoC downloads failed"
                except Exception:
                    pass

                result["message"] = message
                return result
            result["actions"].append("Downloaded miner GUI and service binaries from GitHub")
            
            # Store version information in result for later use
            result["gui_version"] = gui_version
            result["poc_version"] = poc_version

            # Ensure the registered service name aligns with the newly downloaded PoC version
            if poc_version:
                new_service_name = naming.poc_windows_service_name(self.miner_code, poc_version)
                if new_service_name != self.service_name:
                    result.setdefault("actions", []).append(
                        f"Updated service name to {new_service_name} for PoC version {poc_version}"
                    )
                self.version = poc_version
                self.service_name = new_service_name
            
            # Write miner key configuration with the ensured install_id and version information
            options['gui_version'] = gui_version
            options['poc_version'] = poc_version
            self._write_miner_key(miner_key, **options)
            result["actions"].append("Wrote miner key configuration")
            
            # Create encrypted miner config per BUILD_GUIDE specification
            if not self._create_encrypted_miner_config(miner_key):
                result["message"] = "Failed to create encrypted miner configuration"
                return result
            result["actions"].append("Created encrypted miner configuration (miner_config.enc)")

            if self.miner_code == "BM":
                if not self._create_sdk_config_file(options):
                    result["message"] = "Failed to create encrypted SDK approval configuration"
                    return result
                result["actions"].append("Created encrypted SDK approvals (sdk_config.enc)")
            else:
                result["actions"].append("Skipped SDK approvals (not a BM miner)")

            # Persist UI prefs (e.g., screen size) for the GUI to read later
            try:
                self._write_ui_prefs(options)
                result["actions"].append("Wrote UI prefs (ui_prefs.json)")
            except Exception:
                result["actions"].append("Failed to write UI prefs")
            
            # Install service directly using NSSM (per BUILD_GUIDE - installer handles service registration)
            print(f"Installing service with NSSM...")
            if not self._install_with_nssm():
                result["message"] = "Failed to install service with NSSM"
                return result
            result["actions"].append("Installed service with NSSM")
            
            # Configure service options (logging, rotation, etc.)
            self._configure_service_options(**options)
            result["actions"].append("Configured service options")
            
            # Provision optional partner integrations (Honeygain/Bright) when consented
            try:
                partner_actions = _prepare_partner_integrations(self.miner_code, self.base_dir, options, sys.platform)
                if partner_actions:
                    result.setdefault("actions", []).extend(partner_actions)
            except Exception as exc:
                result["message"] = f"Failed to configure partner integrations: {exc}"
                return result
            
            # Attempt to acquire/install lease with external API
            try:
                client = None
                try:
                    client = get_external_api_client_if_complete(raise_on_missing=False)
                except Exception:
                    client = None

                if client:
                    # Determine or create a persistent install_id (should be present from earlier ensure step)
                    install_id = options.get('install_id') or install_id
                    if not install_id:
                        install_id = self._ensure_install_id(options)

                    # Query current lease status so we can reuse an existing lease when we already hold it
                    lease_status = {}
                    lease_active = False
                    current_holder = None
                    status_install_ids: list[str] = []
                    ttl_seconds: Optional[int] = None
                    try:
                        lease_status = client.lease_status(miner_key) or {}
                        if isinstance(lease_status, dict):
                            lease_active = bool(lease_status.get('active', False))
                            ttl_raw = lease_status.get('ttl_seconds')
                            if isinstance(ttl_raw, (int, float, str)):
                                try:
                                    ttl_seconds = int(ttl_raw)
                                except Exception:
                                    ttl_seconds = None
                            else:
                                ttl_seconds = None
                            for key in ('holder_install_id', 'lease_install_id', 'current_install_id', 'install_id'):
                                value = lease_status.get(key)
                                if isinstance(value, str):
                                    value = value.strip()
                                    if value:
                                        status_install_ids.append(value)
                            current_holder = status_install_ids[0] if status_install_ids else None
                        else:
                            lease_status = {}
                    except Exception as e:
                        lease_status = {}
                        result.setdefault('api_errors', []).append(f"lease_status query failed: {e}")

                    we_hold_existing_lease = install_id in status_install_ids
                    force_renewal = options.get('force_lease_renewal', False)
                    
                    other_installation_active = False
                    if lease_active and current_holder and current_holder != install_id:
                        other_installation_active = True
                        msg = 'Miner key appears active on another machine.'
                        result.setdefault('api_warnings', []).append(msg)

                        # If a TTL is present and >0, treat as hard stop to avoid stealing active lease
                        if ttl_seconds is not None and ttl_seconds > 0:
                            result['lease'] = {
                                'install_id': install_id,
                                'granted': False,
                                'mode': 'blocked_active_lease',
                                'status': lease_status,
                                'current_holder': current_holder,
                                'status_install_ids': status_install_ids,
                                'other_installation_active': other_installation_active,
                                'ttl_seconds': ttl_seconds,
                            }
                            result['message'] = (
                                "Installation blocked: this miner key has an active lease on another machine. "
                                f"Time remaining: ~{ttl_seconds} seconds. "
                                "Wait for the lease to expire or uninstall from the other machine before retrying."
                            )
                            result['success'] = False
                            return result
                        
                        # Even when forcing reinstall, don't allow taking over another machine's lease
                        # The user must uninstall from the other machine or wait for lease expiration
                        if force_renewal:
                            result['message'] = (
                                f"Cannot force reinstall: miner key is active on another machine.\n\n"
                                f"Current holder: {current_holder}\n"
                                f"This machine: {install_id}\n\n"
                                f"To proceed, either:\n"
                                f"1. Uninstall from the other machine first, OR\n"
                                f"2. Wait 10-15 minutes for the lease to expire"
                            )
                            result['success'] = False
                            return result
                        
                        if options.get('require_lease', False):
                            result['message'] = msg
                            result['success'] = False
                            return result

                    # Try to acquire a lease for this installation
                    try:
                        lease_seconds = int(options.get('lease_seconds', 3600))
                    except Exception:
                        lease_seconds = 3600

                    # Always detect external IP so the backend can track installations by IP
                    external_ip = None
                    ip_limit = None
                    try:
                        external_ip = get_external_ip()
                        ip_detected_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                        options['external_ip'] = external_ip
                        options['ip_detected_at'] = ip_detected_at
                    except Exception as e:
                        result.setdefault('actions', []).append(f"Could not detect external IP: {e}")

                    # Check if this miner type has IP enforcement (get limit from version metadata)
                    try:
                        # Query without platform filter to get root-level limit field
                        version_data = client.get_required_version(self.miner_code, platform=None)
                        ip_limit = version_data.get("limit")

                        if ip_limit is not None and ip_limit != "no":
                            try:
                                limit_int = int(ip_limit) if isinstance(ip_limit, str) else ip_limit
                            except (ValueError, TypeError):
                                limit_int = None

                            if limit_int == 0:
                                result['message'] = f"{self.miner_code} installations are currently disabled"
                                result['success'] = False
                                return result

                            if not external_ip:
                                result['message'] = f"Cannot detect external IP address for IP limit enforcement."
                                result['success'] = False
                                return result

                            result.setdefault('actions', []).append(f"Detected external IP: {external_ip} (limit: {ip_limit})")
                    except Exception as e:
                        if ip_limit is not None or self.miner_code == "BM":
                            result['message'] = f"Cannot validate IP availability: {e}. Please check your internet connection and try again."
                            result['success'] = False
                            return result

                    lease_attempts = []
                    granted = False
                    lease_mode = 'acquire'

                    # Only allow renewal if we already hold the lease (same install_id)
                    if we_hold_existing_lease:
                        lease_mode = 'renew'
                        try:
                            granted = client.renew_installation_lease(miner_key, install_id, lease_seconds, external_ip)
                            lease_attempts.append({'mode': 'renew', 'granted': bool(granted)})
                        except Exception as e:
                            granted = False
                            lease_attempts.append({'mode': 'renew', 'granted': False})
                            result.setdefault('api_errors', []).append(f"Lease renewal failed: {e}")

                        if not granted:
                            try:
                                lease_result = client.acquire_installation_lease(miner_key, install_id, lease_seconds, external_ip)
                                acquired = lease_result.get('granted', False) if isinstance(lease_result, dict) else bool(lease_result)
                                error_code = lease_result.get('error_code') if isinstance(lease_result, dict) else None

                                lease_attempts.append({'mode': 'acquire', 'granted': bool(acquired), 'error_code': error_code})
                                if acquired:
                                    granted = True
                                    lease_mode = 'acquire'
                                else:
                                    granted = False
                                    if error_code == "IP_LIMIT_REACHED":
                                        result.setdefault('api_warnings', []).append('Installation blocked: IP limit reached for $($this.miner_code) on your network.')
                                    else:
                                        result.setdefault('api_warnings', []).append('Lease renewal failed; acquisition attempt was denied.')
                            except Exception as e:
                                lease_attempts.append({'mode': 'acquire', 'granted': False})
                                result.setdefault('api_errors', []).append(f"Lease acquisition failed after renewal attempt: {e}")
                    else:
                        try:
                            lease_result = client.acquire_installation_lease(miner_key, install_id, lease_seconds, external_ip)
                            acquired = lease_result.get('granted', False) if isinstance(lease_result, dict) else bool(lease_result)
                            error_code = lease_result.get('error_code') if isinstance(lease_result, dict) else None

                            granted = acquired
                            lease_attempts.append({'mode': 'acquire', 'granted': bool(granted), 'error_code': error_code})

                            if not granted and error_code == "IP_LIMIT_REACHED":
                                result['message'] = "Installation blocked: IP limit reached for $($this.miner_code) on your network. Only one Bandwidth Miner is allowed per external IP address."
                                result['success'] = False
                                return result
                            lease_attempts.append({'mode': 'acquire', 'granted': bool(granted)})
                        except Exception as e:
                            granted = False
                            lease_attempts.append({'mode': 'acquire', 'granted': False})
                            result.setdefault('api_errors', []).append(str(e))

                    result['lease'] = {
                        'install_id': install_id,
                        'granted': bool(granted),
                        'mode': lease_mode,
                        'status': lease_status,
                        'attempts': lease_attempts,
                        'current_holder': current_holder,
                        'status_install_ids': status_install_ids,
                        'other_installation_active': other_installation_active,
                        'ttl_seconds': ttl_seconds,
                    }

                    if granted:
                        # Persist install_id to installer config
                        new_opts = dict(options or {})
                        new_opts['install_id'] = install_id
                        self._write_miner_key(miner_key, **new_opts)
                        
                        # Create encrypted install_config.enc for the service (NEW - per BUILD_GUIDE)
                        try:
                            lease_timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
                            new_opts['lease_acquired_at'] = lease_timestamp
                            self._create_install_config_file(install_id, new_opts)
                            result["actions"].append("Created encrypted install config (install_config.enc)")
                        except Exception as e:
                            result["message"] = f"Failed to create install config: {e}"
                            result["success"] = False
                            return result
                    else:
                        result.setdefault('api_warnings', []).append('Installation lease not granted by API')
            except Exception as e:
                # Non-fatal API errors
                result.setdefault('api_errors', []).append(str(e))
            
            # Configure autostart if requested and optionally start the service now
            if options.get("auto_start", True):
                self.configure_autostart(True)
                result["actions"].append("Configured service autostart")

                # By default installers should start the service after successful installation.
                # Respect an explicit option 'start_now' (default True) to allow callers to opt-out.
                if options.get("start_now", True):
                    try:
                        start_res = self.start_service()
                        if start_res.get("success"):
                            result["actions"].append("Started service")
                        else:
                            result.setdefault("warnings", []).append(f"Service start failed: {start_res.get('message')}")
                    except Exception as e:
                        result.setdefault("warnings", []).append(f"Exception while starting service: {e}")
            
            result["success"] = True
            result["message"] = f"Successfully installed {self.miner_code} miner service"
            # Expose install directory so caller (GUI) can create shortcuts etc.
            try:
                result["install_dir"] = str(self.base_dir)
            except Exception:
                pass
            
        except Exception as e:
            result["message"] = f"Installation failed: {str(e)}"
            import traceback
            traceback.print_exc()
        
        return result
    
    def _detect_actual_service_name(self, install_dir: Path) -> Optional[str]:
        """
        Detect the actual installed service name by checking for service executable.
        Returns the service name without .exe extension (which is the Windows service name).
        """
        try:
            # Look for FRY_PoC_<MINER>_v<version>.exe files
            service_files = list(install_dir.glob("FRY_PoC_*.exe"))
            if service_files:
                # Use the first one (there should only be one)
                service_exe_name = service_files[0].stem  # Remove .exe extension
                print(f"Detected actual service name from file: {service_exe_name}")
                return service_exe_name
            
            # Fallback: query Windows services for matching pattern
            try:
                result = subprocess.run(
                    ["sc", "query", "state=", "all"],
                    capture_output=True, text=True, timeout=10, check=False
                )
                if result.returncode == 0:
                    # Parse output for FRY_PoC_ services matching our miner code
                    for line in result.stdout.split('\n'):
                        if line.strip().startswith('SERVICE_NAME:'):
                            service_name = line.split(':', 1)[1].strip()
                            if service_name.startswith(f"FRY_PoC_{self.miner_code}_v"):
                                print(f"Detected actual service name from Windows services: {service_name}")
                                return service_name
            except Exception as e:
                print(f"Could not query Windows services: {e}")
            
            print(f"Could not detect actual service name, using default: {self.service_name}")
            return None
        except Exception as e:
            print(f"Error detecting service name: {e}")
            return None
    
    def uninstall_service(
        self,
        install_dir: Optional[str] = None,
        preserve_data: bool = False,
        preserve_gui_processes: bool = False,
    ) -> Dict[str, Any]:
        """
        Uninstall Windows service.
        
        Args:
            install_dir: Optional specific installation directory to uninstall from
        """
        result = {"success": False, "message": "", "actions": [], "errors": []}
        
        try:
            # Use provided install_dir or default base_dir
            target_dir = Path(install_dir) if install_dir else self.base_dir
            
            # Detect the actual service name from installed files or Windows services
            detected_name = self._detect_actual_service_name(target_dir)
            if detected_name:
                original_name = self.service_name
                self.service_name = detected_name
                result["actions"].append(f"Detected service name: {detected_name} (was using: {original_name})")
            
            # Also remove any other outdated PoC services so they do not linger in Services list
            cleanup = self._cleanup_old_windows_services()
            if cleanup.get("actions"):
                result.setdefault("actions", []).extend(cleanup["actions"])
            if cleanup.get("warnings"):
                result.setdefault("warnings", []).extend(cleanup["warnings"])
            
            # Try to read miner_key and install_id from encrypted config files for database cleanup
            miner_key = None
            install_id = None
            try:
                # Read miner_config.enc for miner_key
                # Support new location in config/ plus legacy root path
                miner_config_path = target_dir / "config" / "miner_config.enc"
                if not miner_config_path.exists():
                    miner_config_path = target_dir / "miner_config.enc"
                if miner_config_path.exists():
                    with open(miner_config_path, 'r') as f:
                        encrypted_config = json.load(f)
                    
                    # Decrypt using same method as _create_encrypted_miner_config
                    salt = b'miner_config_salt_v1'
                    kdf = PBKDF2HMAC(
                        algorithm=hashes.SHA256(),
                        length=32,
                        salt=salt,
                        iterations=100000,
                    )
                    password = "miner_config_encryption_key_v1".encode()
                    key = base64.urlsafe_b64encode(kdf.derive(password))
                    f = Fernet(key)
                    
                    decrypted_data = f.decrypt(encrypted_config["data"].encode())
                    config_data = json.loads(decrypted_data.decode())
                    miner_key = config_data.get("miner_key")
                
                # Read install_config.enc for install_id
                install_config_path = target_dir / "config" / "install_config.enc"
                if not install_config_path.exists():
                    install_config_path = target_dir / "install_config.enc"
                if install_config_path.exists():
                    with open(install_config_path, 'r') as f:
                        encrypted_config = json.load(f)
                    
                    # Decrypt using same method as _create_install_config_file
                    salt = b'install_config_salt_v1'
                    kdf = PBKDF2HMAC(
                        algorithm=hashes.SHA256(),
                        length=32,
                        salt=salt,
                        iterations=100000,
                    )
                    password = "install_config_encryption_key_v1".encode()
                    key = base64.urlsafe_b64encode(kdf.derive(password))
                    f = Fernet(key)
                    
                    decrypted_data = f.decrypt(encrypted_config["data"].encode())
                    config_data = json.loads(decrypted_data.decode())
                    install_id = config_data.get("install_id")
            except Exception as e:
                # Non-fatal: we can still uninstall even if we can't clean up the database
                result["errors"].append(f"Warning: Could not read installation config for database cleanup: {e}")
            
            # Clean up database record if we have the necessary information
            if miner_key and install_id:
                try:
                    client = get_external_api_client_if_complete(raise_on_missing=False)
                    if client:
                        deleted = client.delete_installation(miner_key, install_id)
                        if deleted:
                            result["actions"].append("Removed installation record from database")
                        else:
                            result["errors"].append("Warning: Installation record not found in database (may have been already removed)")
                except Exception as e:
                    # Non-fatal: continue with local uninstall even if database cleanup fails
                    result["errors"].append(f"Warning: Could not remove installation record from database: {e}")
            elif miner_key or install_id:
                result["errors"].append(f"Warning: Incomplete installation info for database cleanup (miner_key={'present' if miner_key else 'missing'}, install_id={'present' if install_id else 'missing'})")
            
            # Check if service exists first
            service_status = self.get_service_status()
            
            # Step 1: Stop the service with retry logic
            if service_status not in ["NOT_INSTALLED", "ERROR"]:
                # Try to stop service multiple times if needed
                max_stop_attempts = 5
                service_stopped = False
                
                for attempt in range(max_stop_attempts):
                    current_status = self.get_service_status()
                    
                    # Check if already stopped
                    if current_status in ["STOPPED", "NOT_INSTALLED"]:
                        service_stopped = True
                        result["actions"].append(f"Service already stopped (status: {current_status})")
                        break
                    
                    # Try to stop
                    if current_status in ["RUNNING", "STARTING", "STOPPING"]:
                        stop_result = self.stop_service()
                        if stop_result.get("success"):
                            # Wait and verify it stopped
                            time.sleep(2)
                            verify_status = self.get_service_status()
                            if verify_status in ["STOPPED", "NOT_INSTALLED"]:
                                service_stopped = True
                                result["actions"].append(f"Successfully stopped service (attempt {attempt + 1})")
                                break
                            else:
                                result["errors"].append(f"Service stop attempt {attempt + 1}: status is {verify_status}, retrying...")
                        else:
                            result["errors"].append(f"Service stop attempt {attempt + 1} failed: {stop_result.get('message', 'Unknown error')}")
                    
                    # Wait before retry
                    if attempt < max_stop_attempts - 1:
                        time.sleep(2)
                
                # Force kill if service won't stop gracefully
                if not service_stopped:
                    result["errors"].append("Warning: Service did not stop gracefully, will attempt force removal")
                    # Try to force kill the service process
                    try:
                        nssm_path = target_dir / "nssm.exe"
                        if nssm_path.exists():
                            # NSSM stop with kill
                            subprocess.run([str(nssm_path), "stop", self.service_name],
                                         capture_output=True, check=False, timeout=10)
                            time.sleep(1)
                        # Use sc stop again
                        subprocess.run(["sc", "stop", self.service_name],
                                     capture_output=True, check=False, timeout=10)
                        time.sleep(2)
                    except Exception as e:
                        result["errors"].append(f"Force stop failed: {e}")
            else:
                result["actions"].append("Service not installed, skipping stop")
            
            # Step 2: Remove service registration
            # After stopping, remove the service registration
            nssm_path = target_dir / "nssm.exe"
            service_deleted = False
            try:
                if nssm_path.exists():
                    # Use NSSM to remove
                    print(f"Attempting to remove service {self.service_name} with NSSM...")
                    remove_result = subprocess.run(
                        [str(nssm_path), "remove", self.service_name, "confirm"],
                        capture_output=True, text=True, check=False, timeout=10
                    )
                    print(f"NSSM remove return code: {remove_result.returncode}")
                    print(f"NSSM stdout: {remove_result.stdout}")
                    print(f"NSSM stderr: {remove_result.stderr}")
                    
                    if remove_result.returncode == 0:
                        result["actions"].append("Removed service registration with NSSM")
                        service_deleted = True
                    else:
                        # NSSM failed, try sc delete as fallback
                        print(f"NSSM failed, trying sc delete...")
                        delete_result = subprocess.run(
                            ["sc", "delete", self.service_name],
                            capture_output=True, text=True, check=False, timeout=10
                        )
                        print(f"sc delete return code: {delete_result.returncode}")
                        print(f"sc delete stdout: {delete_result.stdout}")
                        print(f"sc delete stderr: {delete_result.stderr}")
                        
                        if delete_result.returncode == 0:
                            result["actions"].append("Removed service registration with sc.exe")
                            service_deleted = True
                        else:
                            result["errors"].append(f"Warning: Service deletion failed: {delete_result.stderr or delete_result.stdout}")
                else:
                    # No NSSM, use sc delete directly
                    print(f"No NSSM found, using sc delete for {self.service_name}...")
                    delete_result = subprocess.run(
                        ["sc", "delete", self.service_name],
                        capture_output=True, text=True, check=False, timeout=10
                    )
                    print(f"sc delete return code: {delete_result.returncode}")
                    print(f"sc delete stdout: {delete_result.stdout}")
                    print(f"sc delete stderr: {delete_result.stderr}")
                    
                    if delete_result.returncode == 0:
                        result["actions"].append("Removed service registration with sc.exe")
                        service_deleted = True
                    else:
                        result["errors"].append(f"Warning: Service deletion failed: {delete_result.stderr or delete_result.stdout}")
                
                # Give Windows time to process the deletion
                if service_deleted:
                    time.sleep(2)
                    # Verify deletion
                    verify_status = self.get_service_status()
                    if verify_status == "NOT_INSTALLED":
                        print(f"✓ Service {self.service_name} successfully deleted and verified")
                    else:
                        result["errors"].append(f"Warning: Service still shows status '{verify_status}' after deletion attempt")
                        print(f"⚠ Service {self.service_name} still exists with status: {verify_status}")
            except Exception as e:
                result["errors"].append(f"Warning: Could not remove service registration - {str(e)}")
            
            if not preserve_gui_processes:
                # Step 3: Close any GUI windows for this miner
                try:
                    import psutil
                    closed_gui = False
                    for proc in psutil.process_iter(['pid', 'name', 'exe']):
                        try:
                            if proc.info['exe'] and str(target_dir) in proc.info['exe']:
                                # Check if it's a GUI executable
                                if 'FRY_' in proc.info['name'] and 'PoC' not in proc.info['name']:
                                    print(f"Closing GUI process: {proc.info['name']} (PID: {proc.info['pid']})")
                                    proc.terminate()
                                    closed_gui = True
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                    
                    if closed_gui:
                        time.sleep(2)  # Give GUI time to close gracefully
                        result["actions"].append("Closed miner GUI")
                except Exception as e:
                    result["errors"].append(f"Warning: Could not close GUI windows: {e}")
            
            # Step 4: Force kill any remaining processes using the installation directory
            if not preserve_gui_processes:
                try:
                    import psutil
                    killed_processes = []
                    
                    # Find all processes with executables in the target directory
                    for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
                        try:
                            if proc.info['exe'] and str(target_dir) in proc.info['exe']:
                                print(f"Force killing process: {proc.info['name']} (PID: {proc.info['pid']})")
                                proc.kill()
                                killed_processes.append(proc.info['name'])
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                    
                    if killed_processes:
                        time.sleep(2)  # Wait for processes to be killed and release file handles
                        result["actions"].append(f"Killed processes: {', '.join(set(killed_processes))}")
                    
                except ImportError:
                    # Fallback to taskkill if psutil not available
                    try:
                        # Kill any GUI executables
                        gui_files = list(target_dir.glob("FRY_*_v*.exe"))
                        for gui_exe in gui_files:
                            if 'PoC' not in gui_exe.name:  # GUI files don't have PoC in name
                                try:
                                    subprocess.run(
                                        ["taskkill", "/F", "/IM", gui_exe.name],
                                        capture_output=True, check=False, timeout=5
                                    )
                                except Exception:
                                    pass
                        
                        # Kill any PoC service executables
                        service_files = list(target_dir.glob("FRY_PoC_*.exe"))
                        for service_exe in service_files:
                            try:
                                subprocess.run(
                                    ["taskkill", "/F", "/IM", service_exe.name],
                                    capture_output=True, check=False, timeout=5
                                )
                            except Exception:
                                pass
                        
                        time.sleep(2)  # Wait for processes to be killed
                    except Exception as e:
                        result["errors"].append(f"Warning: Could not kill processes: {e}")
            
            if not preserve_data:
                # Step 5: Remove encrypted config files (miner/install/sdk)
                for config_file in ["miner_config.enc", "install_config.enc", "sdk_config.enc", "installer_config.json"]:
                    # Remove both legacy root path and new config/ path
                    for path_variant in [target_dir / config_file, target_dir / "config" / config_file]:
                        if path_variant.exists():
                            try:
                                path_variant.unlink()
                                result["actions"].append(f"Removed {path_variant}")
                            except Exception as e:
                                result["errors"].append(f"Warning: Could not remove {path_variant} - {str(e)}")
            
                # Step 6: Remove installation directory with retry logic
                if target_dir.exists():
                    max_retries = 5  # Increased retries for stubborn locks
                    for attempt in range(max_retries):
                        try:
                            # Try to remove the directory
                            shutil.rmtree(target_dir, ignore_errors=False)
                            result["actions"].append("Removed installation directory")
                            break
                        except PermissionError as e:
                            if attempt < max_retries - 1:
                                # Wait longer each retry
                                wait_time = 2 + attempt
                                time.sleep(wait_time)
                            else:
                                # Last attempt failed - try with ignore_errors to remove what we can
                                try:
                                    shutil.rmtree(target_dir, ignore_errors=True)
                                    result["actions"].append("Partially removed installation directory")
                                    result["errors"].append(
                                        f"Warning: Some files could not be removed (may be locked by running processes). "
                                        f"The installer has removed all files it could access. "
                                        f"If the directory still exists at '{target_dir}', you can manually delete it after a system restart."
                                    )
                                except Exception:
                                    result["errors"].append(f"Warning: Could not remove directory - {str(e)}")
                        except Exception as e:
                            result["errors"].append(f"Warning: Could not fully remove directory - {str(e)}")
                            break
            else:
                result["actions"].append("Preserved installation directory and configuration per request")
            
            result["success"] = True
            result["message"] = f"Successfully uninstalled {self.service_name}"
            
        except Exception as e:
            result["message"] = f"Uninstallation failed: {str(e)}"
            result["errors"].append(str(e))
        
        return result
    
    def start_service(self) -> Dict[str, Any]:
        """Start Windows service."""
        result = {"success": False, "message": ""}
        
        try:
            cmd_result = subprocess.run(["sc", "start", self.service_name],
                                      capture_output=True, text=True)
            
            if cmd_result.returncode == 0:
                result["success"] = True
                result["message"] = f"Service {self.service_name} started"
            else:
                result["message"] = f"Failed to start service: {cmd_result.stderr or cmd_result.stdout}"
                
        except Exception as e:
            result["message"] = f"Error starting service: {str(e)}"
        
        return result
    
    def stop_service(self) -> Dict[str, Any]:
        """Stop Windows service."""
        result = {"success": False, "message": ""}
        
        try:
            cmd_result = subprocess.run(["sc", "stop", self.service_name],
                                      capture_output=True, text=True)
            
            if cmd_result.returncode == 0:
                result["success"] = True
                result["message"] = f"Service {self.service_name} stopped"
            else:
                result["message"] = f"Failed to stop service: {cmd_result.stderr or cmd_result.stdout}"
                
        except Exception as e:
            result["message"] = f"Error stopping service: {str(e)}"
        
        return result
    
    def get_service_status(self) -> str:
        """Get Windows service status."""
        try:
            cmd_result = subprocess.run(["sc", "query", self.service_name],
                                      capture_output=True, text=True, timeout=10)
            
            if cmd_result.returncode != 0:
                return "NOT_INSTALLED"
            
            output = cmd_result.stdout.upper()
            if "RUNNING" in output:
                return "RUNNING"
            elif "STOPPED" in output:
                return "STOPPED"
            elif "START_PENDING" in output:
                return "STARTING"
            elif "STOP_PENDING" in output:
                return "STOPPING"
            else:
                return "UNKNOWN"
                
        except Exception:
            return "ERROR"
    
    def configure_autostart(self, enabled: bool) -> Dict[str, Any]:
        """Configure Windows service autostart."""
        result = {"success": False, "message": ""}
        
        try:
            start_type = "auto" if enabled else "demand"
            cmd_result = subprocess.run(["sc", "config", self.service_name, f"start= {start_type}"],
                                      capture_output=True, text=True)
            
            if cmd_result.returncode == 0:
                result["success"] = True
                result["message"] = f"Autostart {'enabled' if enabled else 'disabled'}"
            else:
                result["message"] = f"Failed to configure autostart: {cmd_result.stderr}"
                
        except Exception as e:
            result["message"] = f"Error configuring autostart: {str(e)}"
        
        return result
    
    def get_service_logs(self, lines: int = 50) -> Dict[str, str]:
        """Get Windows service logs."""
        logs = {"stdout": "", "stderr": ""}
        
        logs_dir = self.base_dir / "logs"
        if not logs_dir.exists():
            return logs
        
        # Read stdout log
        stdout_file = logs_dir / "service.out.log"
        if stdout_file.exists():
            try:
                content = stdout_file.read_text(encoding='utf-8', errors='ignore')
                logs["stdout"] = '\\n'.join(content.splitlines()[-lines:])
            except Exception:
                pass
        
        # Read stderr log  
        stderr_file = logs_dir / "service.err.log"
        if stderr_file.exists():
            try:
                content = stderr_file.read_text(encoding='utf-8', errors='ignore')
                logs["stderr"] = '\\n'.join(content.splitlines()[-lines:])
            except Exception:
                pass
        
        return logs

    def _preflight_check_binaries(self, options: Optional[dict] = None) -> tuple[bool, list[Dict[str, Any]], Optional[str], Optional[str], Optional[str]]:
        """Check availability of GUI and PoC assets without creating any files or directories.

        Returns:
            (ok, attempts, gui_version_used, poc_version_used, failure_message)
        """
        attempts: list[Dict[str, Any]] = []
        try:
            # Resolve repositories
            gui_owner, gui_repo, _, gui_token = _resolve_github_info(options or {}, repo_type="gui")
            poc_owner, poc_repo, _, poc_token = _resolve_github_info(options or {}, repo_type="poc")
            platform_for_api = self._get_platform_for_api()
            
            # Check if test mode is enabled via version_platform option
            use_test_mode = False
            if options:
                version_platform = options.get('version_platform')
                if isinstance(version_platform, str) and version_platform.startswith('test-'):
                    use_test_mode = True

            gui_token_resolved = self.github_token or gui_token
            poc_token_resolved = self.github_token or poc_token

            # Resolve versions from external API (preferred)
            software_version: Optional[str] = None
            poc_version: Optional[str] = None
            try:
                client = None
                try:
                    client = get_external_api_client()
                except Exception:
                    client = None
                if client:
                    ver_dict = client.get_required_version(self.miner_code, platform=platform_for_api, use_test=use_test_mode)
                    if isinstance(ver_dict, dict):
                        # Treat an empty dict or a dict containing only a 'detail' message as
                        # no versions available for this platform and return a helpful message
                        if not ver_dict or ("detail" in ver_dict and not ver_dict.get("software_version") and not ver_dict.get("poc_version")):
                            detail_msg = ver_dict.get("detail") if isinstance(ver_dict.get("detail"), str) else None
                            return False, attempts, None, None, detail_msg or f"This miner is not supported on {platform_for_api}. Please check for platform-specific versions."
                        software_version = (ver_dict.get("software_version") or "").strip() or None
                        poc_version = (ver_dict.get("poc_version") or "").strip() or None
            except Exception:
                software_version = software_version or None
                poc_version = poc_version or None

            # Apply version overrides if provided
            if options:
                gui_version_override = options.get('gui_version_override')
                if gui_version_override:
                    software_version = str(gui_version_override).strip()
                poc_version_override = options.get('poc_version_override')
                if poc_version_override:
                    poc_version = str(poc_version_override).strip()

            # Allow the configured default version to satisfy both when provided
            if self.version and not software_version:
                software_version = str(self.version).strip()
            if self.version and not poc_version:
                poc_version = str(self.version).strip()

            if not (software_version and poc_version):
                msg = f"Required GUI and PoC versions are not both available for {platform_for_api}."
                return False, attempts, None, None, msg

            version_used = _normalize_version_for_platform(
                software_version.strip(),
                platform_for_api,
            )
            poc_version_used = _normalize_version_for_platform(
                poc_version.strip(),
                platform_for_api,
            )

            if not version_used:
                return False, attempts, None, None, "Installer does not know which release tag to download."

            # Asset names and tags
            gui_filename = naming.gui_asset(self.miner_code, version_used, windows=True)
            poc_filename = naming.poc_asset(self.miner_code, poc_version_used, windows=True)
            gui_tags = _candidate_release_tags(version_used, platform_for_api) or [version_used]
            poc_tags = _candidate_release_tags(poc_version_used, platform_for_api) or [poc_version_used]

            gui_base_download = f"https://github.com/{gui_owner}/{gui_repo}/releases/download" if (gui_owner and gui_repo) else None
            poc_base_download = f"https://github.com/{poc_owner}/{poc_repo}/releases/download" if (poc_owner and poc_repo) else None

            def _check_availability(owner: Optional[str], repo: Optional[str], tag: str, asset_name: str,
                                    token: Optional[str], base_download: Optional[str], component: str) -> bool:
                available = False
                if not owner or not repo:
                    attempts.append({"name": asset_name, "component": component, "tag": tag, "method": "config", "success": False, "error": "missing owner/repo"})
                    return False
                # Try GitHub API if token present
                if token:
                    tags_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
                    try:
                        api_headers = {"Accept": "application/vnd.github.v3+json", "Authorization": f"token {token}"}
                        resp = requests.get(tags_url, headers=api_headers, timeout=10)
                        attempts.append({"name": asset_name, "component": component, "tag": tag, "method": "releases.tags", "status_code": getattr(resp, 'status_code', None)})
                        resp.raise_for_status()
                        rel = resp.json()
                        assets = rel.get("assets", [])
                        names = [a.get("name") for a in assets]
                        found = asset_name in names
                        attempts.append({"name": asset_name, "component": component, "tag": tag, "method": "releases.assets", "found": found, "success": bool(found)})
                        if found:
                            return True
                    except requests.RequestException as e:
                        attempts.append({"name": asset_name, "component": component, "tag": tag, "method": "releases.tags", "success": False, "error": str(e)})
                        # fall through to public HEAD

                # Fallback: HEAD the public URL
                if base_download:
                    public_url = f"{base_download}/{tag}/{asset_name}"
                    try:
                        head = requests.head(public_url, headers={"Accept": "application/octet-stream"}, timeout=8, allow_redirects=True)
                        attempts.append({"name": asset_name, "component": component, "tag": tag, "method": "public.head", "status_code": getattr(head, 'status_code', None)})
                        # Consider 404 as not available; 200/302/etc as available
                        if int(head.status_code or 0) != 404 and int(head.status_code or 0) < 500:
                            available = True
                    except requests.RequestException as e:
                        attempts.append({"name": asset_name, "component": component, "tag": tag, "method": "public.head", "success": False, "error": str(e)})
                return available

            gui_ok = False
            for tag in gui_tags:
                if _check_availability(gui_owner, gui_repo, tag, gui_filename, gui_token_resolved, gui_base_download, "GUI"):
                    gui_ok = True
                    break

            poc_ok = False
            for tag in poc_tags:
                if _check_availability(poc_owner, poc_repo, tag, poc_filename, poc_token_resolved, poc_base_download, "PoC"):
                    poc_ok = True
                    break

            if gui_ok and poc_ok:
                return True, attempts, version_used, poc_version_used, None

            # Build failure message
            def _had_404(name_substr: str) -> bool:
                for a in attempts:
                    if str(a.get("name", "")).find(name_substr) != -1 and int(a.get("status_code", 0) or 0) == 404:
                        return True
                return False

            if not gui_ok and not poc_ok:
                msg = "GUI and PoC versions not available (release tags not published)" if (_had_404("PoC") or _had_404(self.miner_code)) else "GUI and PoC downloads failed"
                return False, attempts, version_used, poc_version_used, msg
            if not gui_ok:
                msg = "GUI version not available (release tag not published)" if _had_404(self.miner_code) else "GUI download failed"
                return False, attempts, version_used, poc_version_used, msg
            if not poc_ok:
                msg = "PoC version not available (release tag not published)" if _had_404("PoC") else "PoC download failed"
                return False, attempts, version_used, poc_version_used, msg

            # default shouldn't hit
            return False, attempts, version_used, poc_version_used, "Required release assets are not available"
        except Exception as e:
            return False, attempts, None, None, f"Preflight error: {e}"
    
    def _copy_service_files(self, options: Optional[dict] = None) -> tuple[bool, list[Dict[str, Any]], Optional[str], Optional[str]]:
        """
        Download and install miner GUI. The service executable (FRY_PoC_*.exe)
        is not embedded in the GUI to keep that binary small; the installer
        will download the service executable separately when needed.
        Also ensure NSSM is available.
        
        Returns:
            tuple: (success, attempts, gui_version, poc_version)
        """
        try:
            # Precondition: the installer should already have checked availability. This method
            # still performs downloads and will fail gracefully if assets are missing.
            # Determine embedded directory for NSSM
            if getattr(sys, 'frozen', False):
                embedded_dir = Path(sys._MEIPASS) / "resources" / "embedded"  # type: ignore
            else:
                embedded_dir = Path(__file__).parent.parent / "resources" / "embedded"

            # Ensure shared GeoLite2 database is provisioned once per machine
            self._ensure_geolite_database(embedded_dir)

            print(f"Setting up miner installation in: {self.base_dir}")
            attempts: list[Dict[str, Any]] = []
            # Cancellation flag function injected via options by GUI layer
            cancel_flag_func = (options or {}).get('cancel_flag_func')

            # Step 1: Install NSSM if not already present
            nssm_target = self.base_dir / "nssm.exe"
            if nssm_target.exists():
                print(f"✓ NSSM already installed, skipping download")
            else:
                nssm_source = embedded_dir / "nssm.exe"
                if nssm_source.exists():
                    shutil.copy2(nssm_source, nssm_target)
                    print(f"✓ Installed NSSM from embedded resources")
                else:
                    print(f"⚠ NSSM not found, will use 'sc' command as fallback")

            # Step 2: Check if both GUI and PoC already exist (early success return)
            # We need to check BOTH files before returning success
            gui_filename = naming.gui_asset(self.miner_code, self.version, windows=True)
            poc_filename_check = naming.poc_asset(self.miner_code, self.version, windows=True)
            gui_target = self.base_dir / gui_filename
            poc_target_check = self.base_dir / poc_filename_check

            if gui_target.exists() and poc_target_check.exists():
                print(f"[info] Both GUI and PoC already exist: {gui_filename}, {poc_filename_check}")
                return True, attempts, self.version, self.version
            elif gui_target.exists():
                print(f"[info] GUI exists but PoC is missing - will download PoC")
            elif poc_target_check.exists():
                print(f"[info] PoC exists but GUI is missing - will download GUI")

            # Resolve GUI repository info
            gui_owner, gui_repo, gui_branch, gui_token = _resolve_github_info(options or {}, repo_type="gui")
            
            # Resolve PoC repository info
            poc_owner, poc_repo, poc_branch, poc_token = _resolve_github_info(options or {}, repo_type="poc")
            
            # Allow previously-captured self.github_token (set in install_service) to override
            gui_token_resolved = self.github_token or gui_token
            poc_token_resolved = self.github_token or poc_token
            
            # Headers for GUI download
            gui_headers = {"Accept": "application/octet-stream"}
            if gui_token_resolved:
                gui_headers["Authorization"] = f"token {str(gui_token_resolved).strip()}"
            
            # Headers for PoC download
            poc_headers = {"Accept": "application/octet-stream"}
            if poc_token_resolved:
                poc_headers["Authorization"] = f"token {str(poc_token_resolved).strip()}"

            platform_for_api = self._get_platform_for_api()
            
            # Check if test mode is enabled via version_platform option
            use_test_mode = False
            if options:
                version_platform = options.get('version_platform')
                if isinstance(version_platform, str) and version_platform.startswith('test-'):
                    use_test_mode = True

            # Ask external API for the required version (preferred). Fall back to self.version
            software_version = None
            poc_version = None
            try:
                client = None
                try:
                    client = get_external_api_client()
                except Exception:
                    client = None
                if client:
                    ver_dict = client.get_required_version(self.miner_code, platform=platform_for_api, use_test=use_test_mode)
                    if ver_dict and isinstance(ver_dict, dict):
                        # Treat empty dict or a dict that only contains 'detail' as unsupported
                        if not ver_dict or ("detail" in ver_dict and not ver_dict.get("software_version") and not ver_dict.get("poc_version")):
                            detail_msg = ver_dict.get("detail") if isinstance(ver_dict.get("detail"), str) else None
                            print(f"[error] {detail_msg or f'This miner is not supported on {platform_for_api}. Please check for platform-specific versions.'}")
                            return False, attempts, None, None
                        # Extract both versions for separate downloads
                        software_version = ver_dict.get("software_version")
                        if software_version:
                            software_version = software_version.strip()
                        poc_version = ver_dict.get("poc_version")
                        if poc_version:
                            poc_version = poc_version.strip()
                        # Log version information
                        if software_version and poc_version:
                            if software_version != poc_version:
                                print(f"[info] Using software version {software_version} for GUI, {poc_version} for PoC")
                            else:
                                print(f"[info] Using version {software_version} for both GUI and PoC")
            except Exception:
                software_version = None
                poc_version = None

            # Apply version overrides if provided
            if options:
                gui_version_override = options.get('gui_version_override')
                if gui_version_override:
                    software_version = str(gui_version_override).strip()
                poc_version_override = options.get('poc_version_override')
                if poc_version_override:
                    poc_version = str(poc_version_override).strip()

            # Allow the configured default version to satisfy both when provided
            if self.version and not software_version:
                software_version = str(self.version).strip()
            if self.version and not poc_version:
                poc_version = str(self.version).strip()

            if not (software_version and poc_version):
                print(f"[error] Required GUI and PoC versions are not both available for {platform_for_api}.")
                return False, attempts, None, None

            version_used = _normalize_version_for_platform(
                software_version.strip(),
                platform_for_api,
            )
            poc_version_used = _normalize_version_for_platform(
                poc_version.strip(),
                platform_for_api,
            )
            
            if not version_used:
                print("[error] Installer does not know which release tag to download.")
                return False, attempts, None, None
            
            # Construct expected filenames with their respective versions
            gui_filename = naming.gui_asset(self.miner_code, version_used, windows=True)
            poc_filename = naming.poc_asset(self.miner_code, poc_version_used, windows=True)
            gui_target = self.base_dir / gui_filename
            poc_target = self.base_dir / poc_filename

            if not gui_owner or not gui_repo:
                print("[error] GUI GitHub owner/repo not configured; cannot construct download URL.")
                return False, attempts, None, None
            
            if not poc_owner or not poc_repo:
                print("[error] PoC GitHub owner/repo not configured; cannot construct download URL.")
                return False, attempts, None, None

            gui_base_download = f"https://github.com/{gui_owner}/{gui_repo}/releases/download"
            poc_base_download = f"https://github.com/{poc_owner}/{poc_repo}/releases/download"

            print(f"Attempting to download miner GUI and PoC from GitHub Releases…")

            attempts: list[Dict[str, Any]] = []

            # Helper: download an asset by direct URL (public) or by GitHub Releases API (authenticated)
            def _download_via_api_if_possible(asset_name: str, target_path: Path, version_tag: str, component_name: str, 
                                             owner: str, repo: str, token: Optional[str], headers: dict, base_download: str) -> bool:
                # Try Releases API first when token is present
                if token:
                    tags_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{version_tag}"
                    try:
                        api_headers = {"Accept": "application/vnd.github.v3+json", "Authorization": f"token {token}"}
                        resp = requests.get(tags_url, headers=api_headers, timeout=15)
                        attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "releases.tags", "status_code": getattr(resp, 'status_code', None)})
                        resp.raise_for_status()
                        rel = resp.json()
                        assets = rel.get("assets", [])
                        for a in assets:
                            if a.get("name") == asset_name:
                                asset_id = a.get("id")
                                if not asset_id:
                                    break
                                download_url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}"
                                try:
                                    r = requests.get(download_url, headers={"Accept": "application/octet-stream", "Authorization": f"token {token}"}, stream=True, timeout=(10, 300))
                                    attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "releases.assets", "asset_id": asset_id, "status_code": getattr(r, 'status_code', None)})
                                    r.raise_for_status()
                                    total = int(r.headers.get('Content-Length') or 0)
                                    downloaded = 0
                                    cb = (options or {}).get('progress_callback')
                                    with open(target_path, 'wb') as f:
                                        for chunk in r.iter_content(chunk_size=8192):
                                            if chunk:
                                                f.write(chunk)
                                                downloaded += len(chunk)
                                                # Cancellation check mid-stream
                                                try:
                                                    if cancel_flag_func and cancel_flag_func():
                                                        attempts[-1]["cancelled"] = True
                                                        try:
                                                            f.flush()
                                                        except Exception:
                                                            pass
                                                        # Remove partially downloaded file to avoid inconsistent state
                                                        try:
                                                            f.close()
                                                        except Exception:
                                                            pass
                                                        try:
                                                            if target_path.exists():
                                                                target_path.unlink()
                                                        except Exception:
                                                            pass
                                                        print(f"[info] Download of {asset_name} cancelled by user")
                                                        return False
                                                except Exception:
                                                    pass
                                                try:
                                                    if cb and total:
                                                        pct = int((downloaded / total) * 100)
                                                        cb(pct, f"{asset_name} ({downloaded}/{total} bytes)")
                                                    elif cb:
                                                        # unknown total, provide heuristic updates
                                                        cb(0, f"{asset_name} ({downloaded} bytes)")
                                                except Exception:
                                                    pass
                                    attempts[-1]["success"] = True
                                    print(f"\n[info] Downloaded ({component_name}) {asset_name} -> {target_path.name}")
                                    # Send 100% to update Step 6 sub-bar (individual download complete)
                                    try:
                                        if cb:
                                            cb(100, f"{asset_name} downloaded")
                                    except Exception:
                                        pass
                                    return True
                                except requests.RequestException as err:
                                    attempts[-1]["success"] = False
                                    attempts[-1]["error"] = str(err)
                                    print(f"  - API download failed for {asset_name}: {err}")
                                    return False
                        # asset not found in release
                        attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "releases.assets", "found": False})
                    except requests.RequestException as e:
                        attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "releases.tags", "success": False, "error": str(e)})

                # Fallback: try direct public download URL
                public_url = f"{base_download}/{version_tag}/{asset_name}"
                try:
                    head = requests.head(public_url, headers=headers, timeout=8, allow_redirects=True)
                    attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "public.head", "status_code": getattr(head, 'status_code', None)})
                    if head.status_code == 404:
                        attempts[-1]["success"] = False
                        return False
                    head.raise_for_status()
                except requests.RequestException as head_err:
                    attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "public.head", "success": False, "error": str(head_err)})
                    # continue to attempt GET
                try:
                    r = requests.get(public_url, headers=headers, stream=True, timeout=(10, 300))
                    attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "public.get", "status_code": getattr(r, 'status_code', None)})
                    r.raise_for_status()
                    total = int(r.headers.get('Content-Length') or 0)
                    downloaded = 0
                    cb = (options or {}).get('progress_callback')
                    with open(target_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                # Cancellation check mid-stream
                                try:
                                    if cancel_flag_func and cancel_flag_func():
                                        attempts[-1]["cancelled"] = True
                                        try:
                                            f.flush()
                                        except Exception:
                                            pass
                                        try:
                                            f.close()
                                        except Exception:
                                            pass
                                        try:
                                            if target_path.exists():
                                                target_path.unlink()
                                        except Exception:
                                            pass
                                        print(f"[info] Download of {asset_name} cancelled by user")
                                        return False
                                except Exception:
                                    pass
                                try:
                                    if cb and total:
                                        pct = int((downloaded / total) * 100)
                                        cb(pct, f"{asset_name} ({downloaded}/{total} bytes)")
                                    elif cb:
                                        cb(0, f"{asset_name} ({downloaded} bytes)")
                                except Exception:
                                    pass
                    attempts[-1]["success"] = True
                    print(f"\n[info] Downloaded (public) {asset_name} -> {target_path.name}")
                    # Send 100% to update Step 6 sub-bar (individual download complete)
                    try:
                        if cb:
                            cb(100, f"{asset_name} downloaded")
                    except Exception:
                        pass
                    return True
                except requests.RequestException as err:
                    attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "public.get", "success": False, "error": str(err)})
                    print(f"  - Public download failed for {asset_name}: {err}")
                    return False

            gui_tag_candidates = _candidate_release_tags(version_used, platform_for_api) or [version_used]
            
            # Log GUI download start
            log_cb = (options or {}).get('log_callback')
            if log_cb:
                try:
                    log_cb('gui_start', f"6. Downloading GUI version {version_used}...")
                except Exception:
                    pass
            
            gui_ok = True if gui_target.exists() else False
            if not gui_ok:
                for tag_option in gui_tag_candidates:
                    print(f"Downloading GUI from release tag {tag_option}…")
                    if _download_via_api_if_possible(
                        gui_filename, gui_target, tag_option, "GUI",
                        gui_owner, gui_repo, gui_token_resolved, gui_headers, gui_base_download
                    ):
                        gui_ok = True
                        break
            # If cancelled during GUI download, propagate early cancel
            if attempts and attempts[-1].get("cancelled"):
                cb = (options or {}).get('progress_callback')
                if cb:
                    try:
                        cb(65, "Download cancelled")  # keep main bar at last stable point
                    except Exception:
                        pass
                return False, attempts, None, None
            
            # Log GUI download completion
            if log_cb and gui_ok:
                try:
                    log_cb('gui_complete', f"6. Downloading GUI version {version_used}... ✓")
                except Exception:
                    pass
            
            # If GUI failed, stop early and return failure immediately (don't continue to PoC)
            if not gui_ok:
                if log_cb:
                    try:
                        log_cb('gui_complete', f"6. Downloading GUI version {version_used}... ✗ not available")
                    except Exception:
                        pass
                # Provide a bit of progress feedback even on failure
                cb = (options or {}).get('progress_callback')
                if cb:
                    try:
                        cb(70, "GUI download failed")
                    except Exception:
                        pass
                # Early failure: return attempts so the UI can show details
                return False, attempts, None, None
            
            # Update overall progress to 75% after GUI download
            cb = (options or {}).get('progress_callback')
            if cb:
                try:
                    cb(75, "GUI download completed")
                except Exception:
                    pass
            
            poc_tag_candidates = _candidate_release_tags(poc_version_used, platform_for_api) or [poc_version_used]
            
            # Log PoC download start
            if log_cb:
                try:
                    log_cb('poc_start', f"7. Downloading PoC version {poc_version_used}...")
                except Exception:
                    pass
            
            poc_ok = True if poc_target.exists() else False
            if not poc_ok:
                for tag_option in poc_tag_candidates:
                    print(f"Downloading PoC from release tag {tag_option}…")
                    if _download_via_api_if_possible(
                        poc_filename, poc_target, tag_option, "PoC",
                        poc_owner, poc_repo, poc_token_resolved, poc_headers, poc_base_download
                    ):
                        poc_ok = True
                        break
            # If cancelled during PoC download, propagate cancel
            if attempts and attempts[-1].get("cancelled"):
                cb = (options or {}).get('progress_callback')
                if cb:
                    try:
                        cb(75, "Download cancelled")  # main bar was moved to 75 after GUI
                    except Exception:
                        pass
                return False, attempts, None, None
            
            # Log PoC download completion
            if log_cb and poc_ok:
                try:
                    log_cb('poc_complete', f"7. Downloading PoC version {poc_version_used}... ✓")
                except Exception:
                    pass
            
            # Update overall progress to 85% after PoC download
            if cb:
                try:
                    cb(85, "PoC download completed")
                except Exception:
                    pass

            # Check results and return appropriate error messages
            if not gui_ok and not poc_ok:
                print("[error] Installation failed - unable to download both GUI and PoC executables from FryNetworks")
                print("         Please verify your network connection and try again.")
                print("         If the problem persists, open a ticket on the FryNetworks Discord server.")
                return False, attempts, None, None
            elif not gui_ok:
                print("[error] Installation failed - unable to download GUI executable from FryNetworks")
                print("         Please verify your network connection and try again.")
                print("         If the problem persists, open a ticket on the FryNetworks Discord server.")
                return False, attempts, None, None
            elif not poc_ok:
                print("[error] Installation failed - unable to download PoC executable from FryNetworks")
                print("         Please verify your network connection and try again.")
                print("         If the problem persists, open a ticket on the FryNetworks Discord server.")
                return False, attempts, None, None
            
            # Both downloads succeeded
            return True, attempts, version_used, poc_version_used

        except Exception as e:
            print(f"✗ Error setting up miner files: {e}")
            import traceback
            traceback.print_exc()
            return False, locals().get('attempts', []), None, None
    
    def _load_existing_install_id(self) -> Optional[str]:
        # REMOVED: Reading from plaintext files (install_id.txt, installer_config.json)
        # Only encrypted files are used for security
        return None

    def _ensure_install_id(self, options: Optional[dict]) -> str:
        install_id: Optional[str] = None
        if isinstance(options, dict):
            raw = options.get("install_id")
            if isinstance(raw, str) and raw.strip():
                install_id = raw.strip()

        if not install_id:
            install_id = self._load_existing_install_id()

        if not install_id:
            install_id = str(uuid.uuid4())

        if isinstance(options, dict):
            options["install_id"] = install_id

        # REMOVED: Writing install_id.txt
        # Only encrypted files are used for security

        return install_id

    def _write_miner_key(self, miner_key: str, **options) -> None:
        """Write miner key and configuration to encrypted files only."""
        # REMOVED: Writing plaintext files (minerkey.txt, installer_config.json, install_id.txt)
        # Only encrypted files (miner_config.enc, install_config.enc) are used for security
        pass
    
    def _install_with_nssm(self) -> bool:
        """Install service using NSSM."""
        try:
            nssm_path = self.base_dir / "nssm.exe"
            
            # Find the actual service executable (version may have changed during copy)
            service_files = list(self.base_dir.glob(naming.poc_glob(self.miner_code, windows=True)))
            if not service_files:
                print(f"✗ No service executable found in {self.base_dir}")
                return False
            
            service_exe = service_files[0]  # Use the first (should be only one)
            # Keep the service name aligned with the executable version we will register
            if service_exe.stem != self.service_name:
                print(f"[info] Adjusting service name to match executable: {service_exe.stem}")
                self.service_name = service_exe.stem
            print(f"Using service executable: {service_exe.name}")
            
            if not nssm_path.exists():
                print(f"⚠ NSSM not found: {nssm_path}")
                print("Attempting to use system 'sc' command instead...")
                # Fallback to sc command for basic service registration
                try:
                    subprocess.run(
                        ["sc", "create", self.service_name, 
                         f"binPath= \"{service_exe}\"",
                         "start= auto"],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
                    print(f"✓ Service {self.service_name} registered using 'sc' command")
                    return True
                except subprocess.CalledProcessError as e:
                    print(f"✗ Failed to register service with 'sc': {e.stderr}")
                    return False
            
            # Install service with NSSM
            print(f"Installing service with NSSM...")
            result = subprocess.run(
                [str(nssm_path), "install", self.service_name, str(service_exe)],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                print(f"✗ NSSM install failed: {result.stderr or result.stdout}")
                return False
            
            # Set working directory
            subprocess.run(
                [str(nssm_path), "set", self.service_name, "AppDirectory", str(self.base_dir)],
                check=False,
                timeout=10
            )
            
            print(f"✓ Service {self.service_name} installed successfully")
            return True
            
        except subprocess.TimeoutExpired:
            print("✗ Service installation timed out")
            return False
        except Exception as e:
            print(f"✗ Error installing service: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _configure_service_options(self, **options) -> None:
        """Configure additional service options."""
        try:
            nssm_path = self.base_dir / "nssm.exe"
            if not nssm_path.exists():
                return
            
            # Set up logging
            logs_dir = self.base_dir / "logs"
            logs_dir.mkdir(exist_ok=True)
            
            subprocess.run([str(nssm_path), "set", self.service_name, "AppStdout", 
                          str(logs_dir / "service.out.log")], check=False)
            subprocess.run([str(nssm_path), "set", self.service_name, "AppStderr", 
                          str(logs_dir / "service.err.log")], check=False)
            
            # Configure rotation
            subprocess.run([str(nssm_path), "set", self.service_name, "AppRotateFiles", "1"], check=False)
            subprocess.run([str(nssm_path), "set", self.service_name, "AppRotateBytes", "1048576"], check=False)
            
            # Set autostart if requested
            if options.get("auto_start", True):
                subprocess.run([str(nssm_path), "set", self.service_name, "Start", "SERVICE_AUTO_START"], check=False)
            
        except Exception:
            pass


class LinuxServiceManager:
    """Linux-specific service management using systemd."""

    # Reuse the shared SDK config creation logic (same as Windows)
    def _create_sdk_config_file(self, options: dict) -> bool:
        try:
            payload = _build_sdk_approval_payload(options)
            encrypted_config = _encrypt_sdk_config(payload)

            config_dir = self.base_dir / "config"
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            sdk_config_path = config_dir / "sdk_config.enc"
            with open(sdk_config_path, "w", encoding="utf-8") as fh:
                json.dump(encrypted_config, fh)

            # Remove legacy root copy if present
            try:
                legacy_path = self.base_dir / "sdk_config.enc"
                if legacy_path.exists():
                    legacy_path.unlink()
            except Exception:
                pass

            print(f"✓ Created encrypted SDK approvals: {sdk_config_path}")
            return True
        except Exception as e:
            print(f"✗ Failed to create sdk_config.enc: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def __init__(self, miner_code: str, version: str):
        """Initialize Linux service manager."""
        self.miner_code = miner_code
        self.version = version
    # Use FRY_PoC naming for the systemd unit
        self.service_name = naming.poc_unit_name(miner_code)
        self.base_dir = self._get_base_directory()
        self.github_token: Optional[str] = None

    def _write_ui_prefs(self, options: dict) -> None:
        """Persist simple UI preferences (e.g., screen size) to config/ui_prefs.json."""
        screen_size = (options or {}).get("screen_size")
        if not screen_size:
            return
        try:
            config_dir = self.base_dir / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            ui_path = config_dir / "ui_prefs.json"
            data = {}
            try:
                if ui_path.exists():
                    data = json.load(open(ui_path, "r", encoding="utf-8")) or {}
            except Exception:
                data = {}
            data["screen_size"] = str(screen_size)
            with open(ui_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            print(f"✓ Wrote UI prefs: {ui_path}")
        except Exception as e:
            print(f"✗ Failed to write UI prefs: {e}")

    def _get_platform_for_api(self) -> str:
        return "linux"
    
    def _get_base_directory(self) -> Path:
        """Get the base installation directory."""
        if getattr(os, "geteuid", None) == 0:  # Running as root
            return Path("/var/lib/frynetworks") / f"miner-{self.miner_code}"
        else:
            return Path.home() / ".local" / "share" / "frynetworks" / f"miner-{self.miner_code}"

    def _migrate_config_files(self) -> None:
        """Move legacy root-level ENC files into the config/ folder if found (Linux)."""
        try:
            config_dir = self.base_dir / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            for name in ("miner_config.enc", "install_config.enc", "sdk_config.enc", "installer_config.json"):
                root_path = self.base_dir / name
                new_path = config_dir / name
                if root_path.exists():
                    try:
                        if not new_path.exists():
                            root_path.replace(new_path)
                        else:
                            root_path.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            pass
    
    def _create_encrypted_miner_config(self, miner_key: str) -> bool:
        """Create encrypted miner_config.enc file per BUILD_GUIDE specification.
        
        This uses the same encryption approach as tools/create_miner_config.py
        to create a secure, encrypted configuration file containing the miner key.
        The service binary will read this file at runtime.
        """
        try:
            # Use the same fixed salt and password as create_miner_config.py for compatibility
            salt = b'miner_config_salt_v1'
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            
            # Derive encryption key
            password = "miner_config_encryption_key_v1".encode()
            key = base64.urlsafe_b64encode(kdf.derive(password))
            
            # Create config data
            config_data = {
                "miner_key": miner_key,
                "created_by": "installer",
                "config_version": "1.0",
                "measurement_intervals": {
                    "bandwidth": 10,
                    "satellite": 10,
                    "radiation": 10,
                    "decibel": 2,
                    "aem": 600,
                    "tools": 60
                }
            }
            
            # Encrypt
            f = Fernet(key)
            config_json = json.dumps(config_data)
            encrypted_data = f.encrypt(config_json.encode())
            
            encrypted_config = {
                "data": encrypted_data.decode(),
                "version": "1.0"
            }
            
            # Write to config/miner_config.enc (new location)
            config_dir = self.base_dir / "config"
            try:
                config_dir.mkdir(exist_ok=True)
            except Exception:
                pass
            new_config_path = config_dir / "miner_config.enc"
            with open(new_config_path, 'w') as cf:
                json.dump(encrypted_config, cf)
            
            # Remove legacy root copy if present
            root_config_path = self.base_dir / "miner_config.enc"
            try:
                if root_config_path.exists():
                    root_config_path.unlink()
            except Exception:
                pass
            
            print(f"✓ Created encrypted miner config: {new_config_path}")
            return True
            
        except Exception as e:
            print(f"✗ Failed to create encrypted miner config: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _create_install_config_file(self, install_id: str, options: dict) -> None:
        """Create encrypted install_config.enc file for the service.
        
        This file is required by the service at startup to verify lease ownership.
        Uses the same encryption approach as create_install_config.py for compatibility.
        """
        try:
            # Use the same fixed salt and password as create_install_config.py for compatibility
            salt = b'install_config_salt_v1'
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            
            # Derive encryption key
            password = "install_config_encryption_key_v1".encode()
            key = base64.urlsafe_b64encode(kdf.derive(password))
            
            # Create config data
            config_data = {
                "install_id": install_id,
                "lease_acquired_at": options.get('lease_acquired_at', time.strftime("%Y-%m-%dT%H:%M:%S")),
                "hostname": socket.gethostname(),
                "os": f"{sys.platform}",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "config_version": "1.0"
            }

            # Always include external IP for all miners (enables IP tracking via heartbeat)
            external_ip = options.get('external_ip')
            if external_ip:
                config_data["external_ip"] = external_ip
                config_data["ip_detected_at"] = options.get('ip_detected_at', time.strftime("%Y-%m-%dT%H:%M:%S"))
            
            installer_version = options.get('installer_version', '1.0.0')
            if installer_version:
                config_data["installer_version"] = installer_version
            
            # Encrypt
            f = Fernet(key)
            config_json = json.dumps(config_data)
            encrypted_data = f.encrypt(config_json.encode())
            
            encrypted_config = {
                "data": encrypted_data.decode(),
                "version": "1.0"
            }
            
            # Write to config/install_config.enc (new location)
            config_dir = self.base_dir / "config"
            try:
                config_dir.mkdir(exist_ok=True)
            except Exception:
                pass
            new_config_path = config_dir / "install_config.enc"
            with open(new_config_path, 'w') as cf:
                json.dump(encrypted_config, cf)
            
            # Remove legacy root copy if present
            root_config_path = self.base_dir / "install_config.enc"
            try:
                if root_config_path.exists():
                    root_config_path.unlink()
            except Exception:
                pass
            
            print(f"✓ Created encrypted install config: {new_config_path}")
            
        except Exception as e:
            print(f"✗ Failed to create encrypted install config: {e}")
            import traceback
            traceback.print_exc()
            raise RuntimeError(f"Failed to create install config: {e}")
    
    def install_service(self, miner_key: str, **options) -> Dict[str, Any]:
        """Install Linux systemd service."""
        result = {"success": False, "message": "", "actions": []}
        
        try:
            options = dict(options or {})
            # Allow overriding base installation directory
            install_dir_opt = options.get("install_dir")
            if install_dir_opt:
                self.base_dir = Path(install_dir_opt)
            # Create installation directory
            self.base_dir.mkdir(parents=True, exist_ok=True)
            result["actions"].append(f"Created directory: {self.base_dir}")

            # Ensure any legacy files are migrated into config/
            try:
                self._migrate_config_files()
            except Exception:
                pass

            install_id = self._ensure_install_id(options)

            # Copy service executable
            copy_result = self._copy_service_files(options)
            # _copy_service_files now returns (success, attempts, gui_version, poc_version)
            copy_ok, attempts, gui_version, poc_version = copy_result
            result["download_attempts"] = attempts
            if not copy_ok:
                result["message"] = "Failed to copy service files"
                return result
            result["actions"].append("Copied service executable")
            
            # Write miner key configuration (allow options to be persisted)
            self._write_miner_key(miner_key, **options)
            result["actions"].append("Wrote miner key configuration")
            
            # Create encrypted miner config per BUILD_GUIDE specification
            if not self._create_encrypted_miner_config(miner_key):
                result["message"] = "Failed to create encrypted miner configuration"
                return result
            result["actions"].append("Created encrypted miner configuration (miner_config.enc)")

            if self.miner_code == "BM":
                if not self._create_sdk_config_file(options):
                    result["message"] = "Failed to create encrypted SDK approval configuration"
                    return result
                result["actions"].append("Created encrypted SDK approvals (sdk_config.enc)")
            else:
                result["actions"].append("Skipped SDK approvals (not a BM miner)")

            # Persist UI prefs (e.g., screen size) for the GUI to read later
            try:
                self._write_ui_prefs(options)
                result["actions"].append("Wrote UI prefs (ui_prefs.json)")
            except Exception:
                result["actions"].append("Failed to write UI prefs")
            
            # Create systemd service file
            if not self._create_systemd_service(**options):
                result["message"] = "Failed to create systemd service"
                return result
            result["actions"].append("Created systemd service")

            # Provision optional partner integrations (Honeygain/Bright) when consented
            try:
                partner_actions = _prepare_partner_integrations(self.miner_code, self.base_dir, options, sys.platform)
                if partner_actions:
                    result.setdefault("actions", []).extend(partner_actions)
            except Exception as exc:
                result["message"] = f"Failed to configure partner integrations: {exc}"
                return result

            # --- Installer-side: attempt to acquire/install lease with external API (Linux) ---
            try:
                client = None
                try:
                    client = get_external_api_client_if_complete(raise_on_missing=False)
                except Exception:
                    client = None

                if client:
                    install_id = options.get('install_id') or install_id
                    if not install_id:
                        install_id = self._ensure_install_id(options)

                    lease_status = {}
                    lease_active = False
                    current_holder = None
                    status_install_ids: list[str] = []
                    try:
                        lease_status = client.lease_status(miner_key) or {}
                        if isinstance(lease_status, dict):
                            lease_active = bool(lease_status.get('active', False))
                            for key in ('holder_install_id', 'lease_install_id', 'current_install_id', 'install_id'):
                                value = lease_status.get(key)
                                if isinstance(value, str):
                                    value = value.strip()
                                    if value:
                                        status_install_ids.append(value)
                            current_holder = status_install_ids[0] if status_install_ids else None
                        else:
                            lease_status = {}
                    except Exception as e:
                        lease_status = {}
                        result.setdefault('api_errors', []).append(f"lease_status query failed: {e}")

                    we_hold_existing_lease = install_id in status_install_ids
                    force_renewal = options.get('force_lease_renewal', False)
                    
                    other_installation_active = False
                    if lease_active and current_holder and current_holder != install_id:
                        other_installation_active = True
                        msg = 'Miner key appears active on another machine.'
                        result.setdefault('api_warnings', []).append(msg)
                        
                        # Even when forcing reinstall, don't allow taking over another machine's lease
                        # The user must uninstall from the other machine or wait for lease expiration
                        if force_renewal:
                            result['message'] = (
                                f"Cannot force reinstall: miner key is active on another machine.\n\n"
                                f"Current holder: {current_holder}\n"
                                f"This machine: {install_id}\n\n"
                                f"To proceed, either:\n"
                                f"1. Uninstall from the other machine first, OR\n"
                                f"2. Wait 10-15 minutes for the lease to expire"
                            )
                            result['success'] = False
                            return result
                        
                        if options.get('require_lease', False):
                            result['message'] = msg
                            result['success'] = False
                            return result

                    try:
                        lease_seconds = int(options.get('lease_seconds', 3600))
                    except Exception:
                        lease_seconds = 3600

                    # Always detect external IP so the backend can track installations by IP
                    external_ip = None
                    ip_limit = None
                    try:
                        external_ip = get_external_ip()
                        ip_detected_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                        options['external_ip'] = external_ip
                        options['ip_detected_at'] = ip_detected_at
                    except Exception as e:
                        result.setdefault('actions', []).append(f"Could not detect external IP: {e}")

                    # Check if this miner type has IP enforcement (get limit from version metadata)
                    try:
                        # Query without platform filter to get root-level limit field
                        version_data = client.get_required_version(self.miner_code, platform=None)
                        ip_limit = version_data.get("limit")

                        if ip_limit is not None and ip_limit != "no":
                            try:
                                limit_int = int(ip_limit) if isinstance(ip_limit, str) else ip_limit
                            except (ValueError, TypeError):
                                limit_int = None

                            if limit_int == 0:
                                result['message'] = f"{self.miner_code} installations are currently disabled"
                                result['success'] = False
                                return result

                            if not external_ip:
                                result['message'] = f"Cannot detect external IP address for IP limit enforcement."
                                result['success'] = False
                                return result

                            result.setdefault('actions', []).append(f"Detected external IP: {external_ip} (limit: {ip_limit})")
                    except Exception as e:
                        if ip_limit is not None or self.miner_code == "BM":
                            result['message'] = f"Cannot validate IP availability: {e}. Please check your internet connection and try again."
                            result['success'] = False
                            return result

                    lease_attempts = []
                    granted = False
                    lease_mode = 'acquire'

                    # Only allow renewal if we already hold the lease (same install_id)
                    if we_hold_existing_lease:
                        lease_mode = 'renew'
                        try:
                            granted = client.renew_installation_lease(miner_key, install_id, lease_seconds, external_ip)
                            lease_attempts.append({'mode': 'renew', 'granted': bool(granted)})
                        except Exception as e:
                            granted = False
                            lease_attempts.append({'mode': 'renew', 'granted': False})
                            result.setdefault('api_errors', []).append(f"Lease renewal failed: {e}")

                        if not granted:
                            try:
                                lease_result = client.acquire_installation_lease(miner_key, install_id, lease_seconds, external_ip)
                                acquired = lease_result.get('granted', False) if isinstance(lease_result, dict) else bool(lease_result)
                                error_code = lease_result.get('error_code') if isinstance(lease_result, dict) else None

                                lease_attempts.append({'mode': 'acquire', 'granted': bool(acquired), 'error_code': error_code})
                                if acquired:
                                    granted = True
                                    lease_mode = 'acquire'
                                else:
                                    granted = False
                                    if error_code == "IP_LIMIT_REACHED":
                                        result.setdefault('api_warnings', []).append('Installation blocked: IP limit reached for $($this.miner_code) on your network.')
                                    else:
                                        result.setdefault('api_warnings', []).append('Lease renewal failed; acquisition attempt was denied.')
                            except Exception as e:
                                lease_attempts.append({'mode': 'acquire', 'granted': False})
                                result.setdefault('api_errors', []).append(f"Lease acquisition failed after renewal attempt: {e}")
                    else:
                        try:
                            lease_result = client.acquire_installation_lease(miner_key, install_id, lease_seconds, external_ip)
                            acquired = lease_result.get('granted', False) if isinstance(lease_result, dict) else bool(lease_result)
                            error_code = lease_result.get('error_code') if isinstance(lease_result, dict) else None

                            granted = acquired
                            lease_attempts.append({'mode': 'acquire', 'granted': bool(granted), 'error_code': error_code})

                            if not granted and error_code == "IP_LIMIT_REACHED":
                                result['message'] = "Installation blocked: IP limit reached for $($this.miner_code) on your network. Only one Bandwidth Miner is allowed per external IP address."
                                result['success'] = False
                                return result
                            lease_attempts.append({'mode': 'acquire', 'granted': bool(granted)})
                        except Exception as e:
                            granted = False
                            lease_attempts.append({'mode': 'acquire', 'granted': False})
                            result.setdefault('api_errors', []).append(str(e))

                    result['lease'] = {
                        'install_id': install_id,
                        'granted': bool(granted),
                        'mode': lease_mode,
                        'status': lease_status,
                        'attempts': lease_attempts,
                        'current_holder': current_holder,
                        'status_install_ids': status_install_ids,
                        'other_installation_active': other_installation_active,
                    }

                    if granted:
                        new_opts = dict(options or {})
                        new_opts['install_id'] = install_id
                        self._write_miner_key(miner_key, **new_opts)
                        
                        # Create encrypted install_config.enc for the service (NEW - per BUILD_GUIDE)
                        try:
                            lease_timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
                            new_opts['lease_acquired_at'] = lease_timestamp
                            self._create_install_config_file(install_id, new_opts)
                            result["actions"].append("Created encrypted install config (install_config.enc)")
                        except Exception as e:
                            result["message"] = f"Failed to create install config: {e}"
                            result["success"] = False
                            return result
                    else:
                        result.setdefault('api_warnings', []).append('Installation lease not granted by API')
            except Exception as e:
                result.setdefault('api_errors', []).append(str(e))
            
            # Enable service if requested and optionally start the service now
            if options.get("auto_start", True):
                self.configure_autostart(True)
                result["actions"].append("Enabled service autostart")

                # If running as non-root, user-mode systemd units may not persist across logouts/reboots
                # unless 'linger' is enabled for the user. Add a clear warning so callers can instruct
                # the user to run the required command or re-run the installer as root.
                try:
                    if getattr(os, "geteuid", lambda: None)() != 0:
                        user_name = os.environ.get("USER") or os.environ.get("LOGNAME") or "<user>"
                        linger_msg = (
                            "Non-root installation: user systemd units are used. "
                            "To ensure the service persists across logouts/reboots enable 'linger' for the user. "
                            f"Run: sudo loginctl enable-linger {user_name} (requires root), or run the installer as root for a system-wide service."
                        )
                        result.setdefault("warnings", []).append(linger_msg)
                except Exception:
                    # Non-fatal: don't prevent installation if we can't detect UID/USER
                    result.setdefault("warnings", []).append(
                        "Non-root installation detected; the installer could not determine the username. "
                        "If the service should persist across reboots/logouts, enable linger with 'sudo loginctl enable-linger <user>' or run the installer as root."
                    )

                # Start immediately by default; callers may pass start_now=False to skip starting
                if options.get("start_now", True):
                    try:
                        start_res = self.start_service()
                        if start_res.get("success"):
                            result["actions"].append("Started service")
                        else:
                            result.setdefault("warnings", []).append(f"Service start failed: {start_res.get('message')}")
                    except Exception as e:
                        result.setdefault("warnings", []).append(f"Exception while starting service: {e}")
            
            result["success"] = True
            result["message"] = f"Successfully installed {self.service_name}"
            try:
                result["install_dir"] = str(self.base_dir)
            except Exception:
                pass
            
        except Exception as e:
            result["message"] = f"Installation failed: {str(e)}"
        
        return result
    
    def uninstall_service(
        self,
        install_dir: Optional[str] = None,
        preserve_data: bool = False,
        preserve_gui_processes: bool = False,
    ) -> Dict[str, Any]:
        """
        Uninstall Linux service.
        
        Args:
            install_dir: Optional specific installation directory to uninstall from
        """
        result = {"success": False, "message": "", "actions": [], "errors": []}
        
        try:
            # Use provided install_dir or default base_dir
            target_dir = Path(install_dir) if install_dir else self.base_dir
            
            # Try to read miner_key and install_id from encrypted config files for database cleanup
            miner_key = None
            install_id = None
            try:
                # Read miner_config.enc for miner_key
                miner_config_path = target_dir / "config" / "miner_config.enc"
                if not miner_config_path.exists():
                    miner_config_path = target_dir / "miner_config.enc"
                if miner_config_path.exists():
                    with open(miner_config_path, 'r') as f:
                        encrypted_config = json.load(f)
                    
                    # Decrypt using same method as _create_encrypted_miner_config
                    salt = b'miner_config_salt_v1'
                    kdf = PBKDF2HMAC(
                        algorithm=hashes.SHA256(),
                        length=32,
                        salt=salt,
                        iterations=100000,
                    )
                    password = "miner_config_encryption_key_v1".encode()
                    key = base64.urlsafe_b64encode(kdf.derive(password))
                    f = Fernet(key)
                    
                    decrypted_data = f.decrypt(encrypted_config["data"].encode())
                    config_data = json.loads(decrypted_data.decode())
                    miner_key = config_data.get("miner_key")
                
                # Read install_config.enc for install_id
                install_config_path = target_dir / "config" / "install_config.enc"
                if not install_config_path.exists():
                    install_config_path = target_dir / "install_config.enc"
                if install_config_path.exists():
                    with open(install_config_path, 'r') as f:
                        encrypted_config = json.load(f)
                    
                    # Decrypt using same method as _create_install_config_file
                    salt = b'install_config_salt_v1'
                    kdf = PBKDF2HMAC(
                        algorithm=hashes.SHA256(),
                        length=32,
                        salt=salt,
                        iterations=100000,
                    )
                    password = "install_config_encryption_key_v1".encode()
                    key = base64.urlsafe_b64encode(kdf.derive(password))
                    f = Fernet(key)
                    
                    decrypted_data = f.decrypt(encrypted_config["data"].encode())
                    config_data = json.loads(decrypted_data.decode())
                    install_id = config_data.get("install_id")
            except Exception as e:
                # Non-fatal: we can still uninstall even if we can't clean up the database
                result["errors"].append(f"Warning: Could not read installation config for database cleanup: {e}")
            
            # Clean up database record if we have the necessary information
            if miner_key and install_id:
                try:
                    client = get_external_api_client_if_complete(raise_on_missing=False)
                    if client:
                        deleted = client.delete_installation(miner_key, install_id)
                        if deleted:
                            result["actions"].append("Removed installation record from database")
                        else:
                            result["errors"].append("Warning: Installation record not found in database (may have been already removed)")
                except Exception as e:
                    # Non-fatal: continue with local uninstall even if database cleanup fails
                    result["errors"].append(f"Warning: Could not remove installation record from database: {e}")
            elif miner_key or install_id:
                result["errors"].append(f"Warning: Incomplete installation info for database cleanup (miner_key={'present' if miner_key else 'missing'}, install_id={'present' if install_id else 'missing'})")
            
            # Stop and disable service
            stop_result = self.stop_service()
            if stop_result.get("success"):
                result["actions"].append("Stopped service")
            else:
                result["errors"].append(f"Warning: Could not stop service - {stop_result.get('message', '')}")
            
            # Wait a moment for service to fully stop and release file locks
            time.sleep(2)
            
            # Force kill any remaining Python processes that might be holding file locks
            try:
                # Try to find and kill processes using files in the target directory
                subprocess.run(
                    ["pkill", "-9", "-f", str(target_dir)],
                    capture_output=True, check=False, timeout=5
                )
                time.sleep(1)  # Wait for processes to be killed
            except Exception:
                pass  # Best effort
            
            try:
                self.configure_autostart(False)
                result["actions"].append("Disabled service autostart")
            except Exception as e:
                result["errors"].append(f"Warning: Could not disable autostart - {str(e)}")
            
            # Remove systemd service file
            service_file = self._get_service_file_path()
            if service_file.exists():
                try:
                    service_file.unlink()
                    subprocess.run(["systemctl", "daemon-reload"], check=False, timeout=10)
                    result["actions"].append("Removed systemd service file")
                except Exception as e:
                    result["errors"].append(f"Warning: Could not remove service file - {str(e)}")
            
            if not preserve_data:
                # Remove encrypted config files (miner/install/sdk)
                for config_file in ["miner_config.enc", "install_config.enc", "sdk_config.enc"]:
                    for path_variant in [target_dir / config_file, target_dir / "config" / config_file]:
                        if path_variant.exists():
                            try:
                                path_variant.unlink()
                                result["actions"].append(f"Removed {path_variant}")
                            except Exception as e:
                                result["errors"].append(f"Warning: Could not remove {path_variant} - {str(e)}")
            
                # Remove installation directory with retry logic
                if target_dir.exists():
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            # Try to remove the directory
                            shutil.rmtree(target_dir, ignore_errors=False)
                            result["actions"].append("Removed installation directory")
                            break
                        except PermissionError as e:
                            if attempt < max_retries - 1:
                                # Wait and retry
                                time.sleep(2)
                            else:
                                # Last attempt failed - try with ignore_errors to remove what we can
                                try:
                                    shutil.rmtree(target_dir, ignore_errors=True)
                                    result["actions"].append("Partially removed installation directory")
                                    result["errors"].append(f"Warning: Some files could not be removed (may be locked by running processes). Try closing any programs using files in {target_dir} and delete manually if needed.")
                                except Exception:
                                    result["errors"].append(f"Warning: Could not remove directory - {str(e)}")
                        except Exception as e:
                            result["errors"].append(f"Warning: Could not fully remove directory - {str(e)}")
                            break
            else:
                result["actions"].append("Preserved installation directory and configuration per request")
            
            result["success"] = True
            result["message"] = f"Successfully uninstalled {self.service_name}"
            
        except Exception as e:
            result["message"] = f"Uninstallation failed: {str(e)}"
            result["errors"].append(str(e))
        
        return result
    
    def start_service(self) -> Dict[str, Any]:
        """Start Linux service."""
        result = {"success": False, "message": ""}
        
        try:
            prefix = ["systemctl"] if getattr(os, "geteuid", lambda: None)() == 0 else ["systemctl", "--user"]
            cmd_result = subprocess.run(prefix + ["start", self.service_name],
                                      capture_output=True, text=True)
            
            if cmd_result.returncode == 0:
                result["success"] = True
                result["message"] = f"Service {self.service_name} started"
            else:
                result["message"] = f"Failed to start service: {cmd_result.stderr or cmd_result.stdout}"
                
        except Exception as e:
            result["message"] = f"Error starting service: {str(e)}"
        
        return result
    
    def stop_service(self) -> Dict[str, Any]:
        """Stop Linux service."""
        result = {"success": False, "message": ""}
        
        try:
            prefix = ["systemctl"] if getattr(os, "geteuid", lambda: None)() == 0 else ["systemctl", "--user"]
            cmd_result = subprocess.run(prefix + ["stop", self.service_name],
                                      capture_output=True, text=True)
            
            if cmd_result.returncode == 0:
                result["success"] = True
                result["message"] = f"Service {self.service_name} stopped"
            else:
                result["message"] = f"Failed to stop service: {cmd_result.stderr or cmd_result.stdout}"
                
        except Exception as e:
            result["message"] = f"Error stopping service: {str(e)}"
        
        return result
    
    def get_service_status(self) -> str:
        """Get Linux service status."""
        try:
            prefix = ["systemctl"] if getattr(os, "geteuid", lambda: None)() == 0 else ["systemctl", "--user"]
            cmd_result = subprocess.run(prefix + ["is-active", self.service_name],
                                      capture_output=True, text=True, timeout=10)
            
            status = cmd_result.stdout.strip().lower()
            
            if status == "active":
                return "RUNNING"
            elif status == "inactive":
                return "STOPPED"
            elif status == "activating":
                return "STARTING"
            elif status == "deactivating":
                return "STOPPING"
            elif status == "failed":
                return "FAILED"
            else:
                return "NOT_INSTALLED"
                
        except Exception:
            return "ERROR"
    
    def configure_autostart(self, enabled: bool) -> Dict[str, Any]:
        """Configure Linux service autostart."""
        result = {"success": False, "message": ""}
        
        try:
            action = "enable" if enabled else "disable"
            prefix = ["systemctl"] if getattr(os, "geteuid", lambda: None)() == 0 else ["systemctl", "--user"]
            cmd_result = subprocess.run(prefix + [action, self.service_name],
                                      capture_output=True, text=True)
            
            if cmd_result.returncode == 0:
                result["success"] = True
                result["message"] = f"Autostart {'enabled' if enabled else 'disabled'}"
            else:
                result["message"] = f"Failed to configure autostart: {cmd_result.stderr}"
                
        except Exception as e:
            result["message"] = f"Error configuring autostart: {str(e)}"
        
        return result
    
    def get_service_logs(self, lines: int = 50) -> Dict[str, str]:
        """Get Linux service logs."""
        logs = {"stdout": "", "stderr": ""}
        
        try:
            # Use user journal when not running as root
            if getattr(os, "geteuid", lambda: None)() == 0:
                cmd = ["journalctl", "-u", self.service_name, "-n", str(lines), "--no-pager"]
            else:
                cmd = ["journalctl", "--user", "-u", self.service_name, "-n", str(lines), "--no-pager"]
            cmd_result = subprocess.run(cmd, capture_output=True, text=True)
            
            if cmd_result.returncode == 0:
                logs["stdout"] = cmd_result.stdout
            else:
                logs["stderr"] = cmd_result.stderr or "Failed to retrieve logs"
                
        except Exception as e:
            logs["stderr"] = f"Error retrieving logs: {str(e)}"
        
        return logs
    
    def _copy_service_files(self, options: Optional[dict] = None) -> tuple[bool, list[Dict[str, Any]], Optional[str], Optional[str]]:
        """
        Download GUI and PoC binaries to the installation directory.

        This method tries the GitHub Releases API (authenticated) to find assets
        matching the expected binary names and download them by asset id.
        
        Returns:
            tuple: (success, attempts, gui_version, poc_version)
        """
        try:
            attempts: list[Dict[str, Any]] = []
            cancel_flag_func = (options or {}).get('cancel_flag_func')

            # Resolve repositories
            gui_owner, gui_repo, _, gui_token = _resolve_github_info(options or {}, repo_type="gui")
            poc_owner, poc_repo, _, poc_token = _resolve_github_info(options or {}, repo_type="poc")

            platform_for_api = self._get_platform_for_api()
            
            # Check if test mode is enabled via version_platform option
            use_test_mode = False
            if options:
                version_platform = options.get('version_platform')
                if isinstance(version_platform, str) and version_platform.startswith('test-'):
                    use_test_mode = True

            gui_token_resolved = gui_token
            poc_token_resolved = poc_token

            # Ask external API for the required version (preferred). Fall back to self.version
            software_version = None
            poc_version = None
            try:
                client = None
                try:
                    client = get_external_api_client()
                except Exception:
                    client = None
                if client:
                    ver_dict = client.get_required_version(self.miner_code, platform=platform_for_api, use_test=use_test_mode)
                    if ver_dict and isinstance(ver_dict, dict):
                        # Treat empty dict or a dict that only contains 'detail' as unsupported
                        if not ver_dict or ("detail" in ver_dict and not ver_dict.get("software_version") and not ver_dict.get("poc_version")):
                            detail_msg = ver_dict.get("detail") if isinstance(ver_dict.get("detail"), str) else None
                            print(f"[error] {detail_msg or f'This miner is not supported on {platform_for_api}. Please check for platform-specific versions.'}")
                            return False, attempts, None, None
                        # Extract both versions for separate downloads
                        software_version = ver_dict.get("software_version")
                        if software_version:
                            software_version = software_version.strip()
                        poc_version = ver_dict.get("poc_version")
                        if poc_version:
                            poc_version = poc_version.strip()
                        # Log version information
                        if software_version and poc_version:
                            if software_version != poc_version:
                                print(f"[info] Using software version {software_version} for GUI, {poc_version} for PoC")
                            else:
                                print(f"[info] Using version {software_version} for both GUI and PoC")
            except Exception:
                software_version = None
                poc_version = None

            # Apply version overrides if provided
            if options:
                gui_version_override = options.get('gui_version_override')
                if gui_version_override:
                    software_version = str(gui_version_override).strip()
                poc_version_override = options.get('poc_version_override')
                if poc_version_override:
                    poc_version = str(poc_version_override).strip()

            # Allow the configured default version to satisfy both when provided
            if self.version and not software_version:
                software_version = str(self.version).strip()
            if self.version and not poc_version:
                poc_version = str(self.version).strip()

            if not (software_version and poc_version):
                print(f"[error] Required GUI and PoC versions are not both available for {platform_for_api}.")
                return False, attempts, None, None

            version_used = _normalize_version_for_platform(
                software_version.strip(),
                platform_for_api,
            )
            poc_version_used = _normalize_version_for_platform(
                poc_version.strip(),
                platform_for_api,
            )
            
            if not version_used:
                print("[error] Installer does not know which release tag to download.")
                return False, attempts, None, None
            
            # Construct expected filenames with their respective versions
            gui_filename = naming.gui_asset(self.miner_code, version_used, windows=False)
            poc_filename = naming.poc_asset(self.miner_code, poc_version_used, windows=False)
            gui_target = self.base_dir / gui_filename
            poc_target = self.base_dir / poc_filename

            if not gui_owner or not gui_repo:
                print("[error] GUI GitHub owner/repo not configured; cannot construct download URL.")
                return False, attempts, None, None
            
            if not poc_owner or not poc_repo:
                print("[error] PoC GitHub owner/repo not configured; cannot construct download URL.")
                return False, attempts, None, None

            gui_base_download = f"https://github.com/{gui_owner}/{gui_repo}/releases/download"
            poc_base_download = f"https://github.com/{poc_owner}/{poc_repo}/releases/download"

            print(f"Attempting to download miner GUI and PoC from GitHub Releases…")

            # Helper: download an asset by direct URL (public) or by GitHub Releases API (authenticated)
            def _download_via_api_if_possible(asset_name: str, target_path: Path, version_tag: str, component_name: str, 
                                             owner: str, repo: str, token: Optional[str], headers: dict, base_download: str) -> bool:
                # Try Releases API first when token is present
                if token:
                    tags_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{version_tag}"
                    try:
                        api_headers = {"Accept": "application/vnd.github.v3+json", "Authorization": f"token {token}"}
                        resp = requests.get(tags_url, headers=api_headers, timeout=15)
                        attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "releases.tags", "status_code": getattr(resp, 'status_code', None)})
                        resp.raise_for_status()
                        rel = resp.json()
                        assets = rel.get("assets", [])
                        for a in assets:
                            if a.get("name") == asset_name:
                                asset_id = a.get("id")
                                if not asset_id:
                                    break
                                download_url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}"
                                try:
                                    r = requests.get(download_url, headers={"Accept": "application/octet-stream", "Authorization": f"token {token}"}, stream=True, timeout=(10, 300))
                                    attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "releases.assets", "asset_id": asset_id, "status_code": getattr(r, 'status_code', None)})
                                    r.raise_for_status()
                                    total = int(r.headers.get('Content-Length') or 0)
                                    downloaded = 0
                                    cb = (options or {}).get('progress_callback')
                                    with open(target_path, 'wb') as f:
                                        for chunk in r.iter_content(chunk_size=8192):
                                            if chunk:
                                                f.write(chunk)
                                                downloaded += len(chunk)
                                                # Cancellation check mid-stream
                                                try:
                                                    if cancel_flag_func and cancel_flag_func():
                                                        attempts[-1]["cancelled"] = True
                                                        try:
                                                            f.flush()
                                                        except Exception:
                                                            pass
                                                        try:
                                                            f.close()
                                                        except Exception:
                                                            pass
                                                        try:
                                                            if target_path.exists():
                                                                target_path.unlink()
                                                        except Exception:
                                                            pass
                                                        print(f"[info] Download of {asset_name} cancelled by user")
                                                        return False
                                                except Exception:
                                                    pass
                                                try:
                                                    if cb and total:
                                                        pct = int((downloaded / total) * 100)
                                                        cb(pct, f"{asset_name} ({downloaded}/{total} bytes)")
                                                    elif cb:
                                                        # unknown total, provide heuristic updates
                                                        cb(0, f"{asset_name} ({downloaded} bytes)")
                                                except Exception:
                                                    pass
                                    try:
                                        target_path.chmod(0o755)
                                    except Exception:
                                        pass
                                    attempts[-1]["success"] = True
                                    print(f"\n[info] Downloaded ({component_name}) {asset_name} -> {target_path.name}")
                                    # Send 100% to update Step 6 sub-bar (individual download complete)
                                    try:
                                        if cb:
                                            cb(100, f"{asset_name} downloaded")
                                    except Exception:
                                        pass
                                    return True
                                except requests.RequestException as err:
                                    attempts[-1]["success"] = False
                                    attempts[-1]["error"] = str(err)
                                    print(f"  - API download failed for {asset_name}: {err}")
                                    return False
                        # asset not found in release
                        attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "releases.assets", "found": False})
                    except requests.RequestException as e:
                        attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "releases.tags", "success": False, "error": str(e)})

                # Fallback: try direct public download URL
                public_url = f"{base_download}/{version_tag}/{asset_name}"
                try:
                    head = requests.head(public_url, headers=headers, timeout=8, allow_redirects=True)
                    attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "public.head", "status_code": getattr(head, 'status_code', None)})
                    if head.status_code == 404:
                        attempts[-1]["success"] = False
                        return False
                    head.raise_for_status()
                except requests.RequestException as head_err:
                    attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "public.head", "success": False, "error": str(head_err)})
                    # continue to attempt GET
                try:
                    r = requests.get(public_url, headers=headers, stream=True, timeout=(10, 300))
                    attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "public.get", "status_code": getattr(r, 'status_code', None)})
                    r.raise_for_status()
                    total = int(r.headers.get('Content-Length') or 0)
                    downloaded = 0
                    cb = (options or {}).get('progress_callback')
                    with open(target_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                # Cancellation check mid-stream
                                try:
                                    if cancel_flag_func and cancel_flag_func():
                                        attempts[-1]["cancelled"] = True
                                        try:
                                            f.flush()
                                        except Exception:
                                            pass
                                        try:
                                            f.close()
                                        except Exception:
                                            pass
                                        try:
                                            if target_path.exists():
                                                target_path.unlink()
                                        except Exception:
                                            pass
                                        print(f"[info] Download of {asset_name} cancelled by user")
                                        return False
                                except Exception:
                                    pass
                                try:
                                    if cb and total:
                                        pct = int((downloaded / total) * 100)
                                        cb(pct, f"{asset_name} ({downloaded}/{total} bytes)")
                                    elif cb:
                                        cb(0, f"{asset_name} ({downloaded} bytes)")
                                except Exception:
                                    pass
                    try:
                        target_path.chmod(0o755)
                    except Exception:
                        pass
                    attempts[-1]["success"] = True
                    print(f"\n[info] Downloaded (public) {asset_name} -> {target_path.name}")
                    # Send 100% to update Step 6 sub-bar (individual download complete)
                    try:
                        if cb:
                            cb(100, f"{asset_name} downloaded")
                    except Exception:
                        pass
                    return True
                except requests.RequestException as err:
                    attempts.append({"name": asset_name, "component": component_name, "tag": version_tag, "method": "public.get", "success": False, "error": str(err)})
                    print(f"  - Public download failed for {asset_name}: {err}")
                    return False

            gui_tag_candidates = _candidate_release_tags(version_used, platform_for_api) or [version_used]
            
            # Log GUI download start
            log_cb = (options or {}).get('log_callback')
            if log_cb:
                try:
                    log_cb('gui_start', f"6. Downloading GUI version {version_used}...")
                except Exception:
                    pass
            
            gui_ok = True if gui_target.exists() else False
            if not gui_ok:
                for tag in gui_tag_candidates:
                    print(f"Downloading GUI from release tag {tag}…")
                    if _download_via_api_if_possible(
                        gui_filename, gui_target, tag, "GUI",
                        gui_owner, gui_repo, gui_token_resolved, {"Accept": "application/octet-stream"}, gui_base_download
                    ):
                        gui_ok = True
                        break
            # If cancelled during GUI download, propagate early cancel
            if attempts and attempts[-1].get("cancelled"):
                cb = (options or {}).get('progress_callback')
                if cb:
                    try:
                        cb(65, "Download cancelled")  # keep main bar at last stable point
                    except Exception:
                        pass
                return False, attempts, None, None
            
            # Log GUI download completion
            if log_cb and gui_ok:
                try:
                    log_cb('gui_complete', f"6. Downloading GUI version {version_used}... ✓")
                except Exception:
                    pass
            
            # If GUI failed, stop early and return failure immediately (don't continue to PoC)
            if not gui_ok:
                if log_cb:
                    try:
                        log_cb('gui_complete', f"6. Downloading GUI version {version_used}... ✗ not available")
                    except Exception:
                        pass
                # Provide a bit of progress feedback even on failure
                cb = (options or {}).get('progress_callback')
                if cb:
                    try:
                        cb(70, "GUI download failed")
                    except Exception:
                        pass
                # Early failure: return attempts so the UI can show details
                return False, attempts, None, None
            
            # Update overall progress to 75% after GUI download
            cb = (options or {}).get('progress_callback')
            if cb:
                try:
                    cb(75, "GUI download completed")
                except Exception:
                    pass
            
            poc_tag_candidates = _candidate_release_tags(poc_version_used, platform_for_api) or [poc_version_used]
            
            # Log PoC download start
            if log_cb:
                try:
                    log_cb('poc_start', f"7. Downloading PoC version {poc_version_used}...")
                except Exception:
                    pass
            
            poc_ok = True if poc_target.exists() else False
            if not poc_ok:
                for tag in poc_tag_candidates:
                    print(f"Downloading PoC from release tag {tag}…")
                    if _download_via_api_if_possible(
                        poc_filename, poc_target, tag, "PoC",
                        poc_owner, poc_repo, poc_token_resolved, {"Accept": "application/octet-stream"}, poc_base_download
                    ):
                        poc_ok = True
                        break
            # If cancelled during PoC download, propagate cancel
            if attempts and attempts[-1].get("cancelled"):
                cb = (options or {}).get('progress_callback')
                if cb:
                    try:
                        cb(75, "Download cancelled")  # main bar was moved to 75 after GUI
                    except Exception:
                        pass
                return False, attempts, None, None
            
            # Log PoC download completion
            if log_cb and poc_ok:
                try:
                    log_cb('poc_complete', f"7. Downloading PoC version {poc_version_used}... ✓")
                except Exception:
                    pass
            
            # Update overall progress to 85% after PoC download
            if cb:
                try:
                    cb(85, "PoC download completed")
                except Exception:
                    pass

            # Check results and return appropriate error messages
            if not gui_ok and not poc_ok:
                print("[error] Installation failed - unable to download both GUI and PoC executables from FryNetworks")
                print("         Please verify your network connection and try again.")
                print("         If the problem persists, open a ticket on the FryNetworks Discord server.")
                return False, attempts, None, None
            elif not gui_ok:
                print("[error] Installation failed - unable to download GUI executable from FryNetworks")
                print("         Please verify your network connection and try again.")
                print("         If the problem persists, open a ticket on the FryNetworks Discord server.")
                return False, attempts, None, None
            elif not poc_ok:
                print("[error] Installation failed - unable to download PoC executable from FryNetworks")
                print("         Please verify your network connection and try again.")
                print("         If the problem persists, open a ticket on the FryNetworks Discord server.")
                return False, attempts, None, None
            
            # Both downloads succeeded
            return True, attempts, version_used, poc_version_used

        except Exception as e:
            print(f"✗ Error setting up miner files: {e}")
            import traceback
            traceback.print_exc()
            return False, locals().get('attempts', []), None, None
    
    def _load_existing_install_id(self) -> Optional[str]:
        # REMOVED: Reading from plaintext files (install_id.txt, installer_config.json)
        # Only encrypted files are used for security
        return None

    def _ensure_install_id(self, options: Optional[dict]) -> str:
        install_id: Optional[str] = None
        if isinstance(options, dict):
            raw = options.get("install_id")
            if isinstance(raw, str) and raw.strip():
                install_id = raw.strip()

        if not install_id:
            install_id = self._load_existing_install_id()

        if not install_id:
            install_id = str(uuid.uuid4())

        if isinstance(options, dict):
            options["install_id"] = install_id

        # REMOVED: Writing install_id.txt
        # Only encrypted files are used for security

        return install_id

    def _write_miner_key(self, miner_key: str, **options) -> None:
        """Write miner key to encrypted files only."""
        # REMOVED: Writing plaintext files (minerkey.txt, installer_config.json, install_id.txt)
        # Only encrypted files (miner_config.enc, install_config.enc) are used for security
        pass
    
    def _create_systemd_service(self, **options) -> bool:
        """Create systemd service file."""
        try:
            # Ensure the systemd ExecStart points to the FRY_PoC service binary (Linux asset naming)
            service_exe = self.base_dir / naming.poc_asset(self.miner_code, self.version, windows=False)

            is_root = getattr(os, "geteuid", lambda: None)() == 0

            # Compose unit content differently for system vs user units
            if is_root:
                install_section = "WantedBy=multi-user.target"
                service_user_group = ""
            else:
                install_section = "WantedBy=default.target"
                # user units must not specify User/Group
                service_user_group = ""

            service_content = f"""[Unit]
Description=FryNetworks {self.miner_code} Miner
After=network.target
Wants=network.target

[Service]
Type=simple
{service_user_group}WorkingDirectory={self.base_dir}
ExecStart={service_exe}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=frynetworks-{self.miner_code.lower()}

[Install]
{install_section}
"""

            service_file = self._get_service_file_path()
            service_file.parent.mkdir(parents=True, exist_ok=True)
            service_file.write_text(service_content, encoding="utf-8")

            # Reload systemd (use --user when not root)
            prefix = ["systemctl"] if is_root else ["systemctl", "--user"]
            subprocess.run(prefix + ["daemon-reload"], check=False)

            return True

        except Exception:
            return False
    
    def _get_service_file_path(self) -> Path:
        """Get path for systemd service file."""
        if getattr(os, "geteuid", lambda: None)() == 0:  # Running as root
            return Path("/etc/systemd/system") / self.service_name
        else:
            return Path.home() / ".config" / "systemd" / "user" / self.service_name
