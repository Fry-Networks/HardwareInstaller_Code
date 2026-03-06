"""
Conflict detection and resolution for miner installations.

This module handles:
- Device installation limits (one miner per device)  
- Exclusive pair conflicts (ISM/OSM, IDM/ODM)
- Global conflict detection via External API
- Hardware resource conflicts
"""

import os
import sys
import json
import logging
import psutil
import platform
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Any

from .key_parser import MinerKeyParser
from . import naming

# Import external API client from the tools package
from tools.external_api import ExternalApiClient, ApiError

# Setup debug logger that writes to install_debug.log
_cd_logger = logging.getLogger("conflict_detector")
_cd_logger.setLevel(logging.WARNING)
try:
    _local_app = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    if _local_app:
        _log_dir = Path(_local_app) / "FryNetworks"
    else:
        _log_dir = Path(tempfile.gettempdir()) / "FryNetworks"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_path = _log_dir / "install_debug.log"
    _fh = logging.FileHandler(str(_log_path), mode='a', encoding='utf-8')
    _fh.setFormatter(logging.Formatter('%(asctime)s [ConflictDetector] %(message)s'))
    _cd_logger.addHandler(_fh)
except Exception:
    pass


class ConflictDetector:
    """Detect and resolve miner installation conflicts."""
    
    def __init__(self, api_client: ExternalApiClient, use_test: bool = False):
        """Initialize the conflict detector.

        Args:
            api_client: External API client for global conflict detection (required)
            use_test: If True, query test-windows/test-linux platforms for version data
        """
        self.parser = MinerKeyParser()
        self.platform = sys.platform
        self.api_client = api_client
        self.use_test = use_test
    
    def _get_install_id(self) -> str:
        """Get or create install_id for this installation."""
        # REMOVED: Reading/writing plaintext install_id.txt
        # Only encrypted files are used for security
        import uuid
        return str(uuid.uuid4())
    
    def check_device_conflicts(self, new_key: str) -> Dict[str, Any]:
        """
        Check for conflicts on this device.
        
        Args:
            new_key: The miner key to check for conflicts
            
        Returns:
            Dictionary with conflict information
        """
        conflicts = {
            "has_conflicts": False,
            "device_limit": False,
            "exclusive_pair": False,
            "active_instance": False,
            "hardware_conflict": False,
            "vm_environment": False,
            "details": []
        }
        
        # Parse the new miner key
        new_miner = self.parser.parse_miner_key(new_key)
        if not new_miner["valid"]:
            return {"error": new_miner["error"]}
        
        # Validate miner key exists in the system via External API
        try:
            miner_profile = self.api_client.get_miner_profile(new_key)
            if not miner_profile.get("exists", False):
                detail_msg = miner_profile.get("detail") or "Miner key not found in the backend."
                return {
                    "error": detail_msg,
                    "has_conflicts": True,
                    "details": [{
                        "type": "invalid_key",
                        "severity": "error", 
                        "message": detail_msg,
                        "resolution": "Verify the key or contact support to register it."
                    }]
                }
        except ApiError as e:
            msg = str(e)
            detail_msg = msg
            try:
                brace = msg.find("{")
                if brace != -1:
                    body = msg[brace:]
                    parsed = json.loads(body)
                    if isinstance(parsed, dict) and parsed.get("detail"):
                        detail_msg = str(parsed.get("detail"))
            except Exception:
                detail_msg = msg
            if "404" in msg or "not found" in msg.lower():
                return {
                    "error": detail_msg,
                    "has_conflicts": True,
                    "details": [{
                        "type": "invalid_key",
                        "severity": "error",
                        "message": detail_msg,
                        "resolution": "Verify the key or contact support to register it."
                    }]
                }
            return {
                "error": "Could not validate miner key (API error).",
                "has_conflicts": True,
                "details": [{
                    "type": "validation_error",
                    "severity": "error",
                    "message": f"API validation failed: {msg}",
                    "resolution": "Check network connection and API availability"
                }]
            }
        except Exception as e:
            return {
                "error": "Could not validate miner key.",
                "has_conflicts": True,
                "details": [{
                    "type": "validation_error",
                    "severity": "error",
                    "message": f"Unexpected validation failure: {e}",
                    "resolution": "Retry or contact support."
                }]
            }
        
        try:
            # --- Virtual Machine Environment Check (Hard Block) ---
            vm_info = self._detect_virtual_machine()
            if vm_info.get("vm") is True:
                conflicts["vm_environment"] = True
                conflicts["has_conflicts"] = True
                conflicts["details"].append({
                    "type": "vm_environment",
                    "severity": "error",
                    "message": "Installation forbidden: Virtual Machine environment detected",
                    "evidence": vm_info.get("evidence", []),
                    "resolution": "Install on physical hardware. VMs are not supported for FryNetworks miners."
                })
                # Early return: VM usage is a hard stop regardless of other checks
                return conflicts

            # Check existing installations
            existing_miners = self._get_installed_miners()

            # Certain miners share a logical group but are allowed to coexist.
            group_exempt_codes = {"BM", "RDN", "SVN", "SDN"}
            
            # Device limit check: only treat as a hard conflict if an existing installation
            # is of the same miner group (or same exact miner type). This allows different
            # groups (e.g., Bandwidth vs Satellite) to coexist unless other checks block them.
            if existing_miners:
                for existing in existing_miners:
                    try:
                        existing_group = existing.get('group') or existing.get('config', {}).get('group')
                    except Exception:
                        existing_group = None
                    existing_code = existing.get('code')
                    new_code = new_miner.get('code')
                    same_code = existing_code == new_code
                    same_group = bool(existing_group and existing_group == new_miner.get('group'))

                    # Skip group conflict for exempt codes (Bandwidth family coexistence)
                    if same_group and (existing_code in group_exempt_codes or new_code in group_exempt_codes):
                        same_group = False

                    # Conflict if same group (e.g., two satellite miners) or exact same code
                    if same_code or same_group:
                        conflicts["device_limit"] = True
                        conflicts["has_conflicts"] = True
                        conflicts["details"].append({
                            "type": "device_limit",
                            "severity": "error",
                            "message": f"Device already has {existing.get('name')} installed",
                            "existing": existing,
                            "resolution": "Remove existing miner or choose different device"
                        })
                        # Stop after reporting the first relevant device-limit conflict
                        break
            
            # Exclusive pair check
            if new_miner["exclusive"]:
                for existing in existing_miners:
                    if existing["code"] == new_miner["exclusive"]:
                        conflicts["exclusive_pair"] = True
                        conflicts["has_conflicts"] = True
                        conflicts["details"].append({
                            "type": "exclusive_pair",
                            "severity": "error",
                            "message": f"{new_miner['name']} conflicts with {existing['name']}",
                            "existing": existing,
                            "resolution": f"Uninstall {existing['name']} before installing {new_miner['name']}"
                        })
            
            # Active instance check (same miner key already running)
            # First check local processes
            active_processes = self._get_active_miner_processes()
            for process in active_processes:
                if process.get("key") == new_key:
                    conflicts["active_instance"] = True
                    conflicts["has_conflicts"] = True
                    conflicts["details"].append({
                        "type": "active_instance",
                        "severity": "warning",
                        "message": f"Miner key {new_key} is already running locally",
                        "process": process,
                        "resolution": "Stop existing local process or use different key"
                    })
            
            # Global instance check via External API
            install_id = self._get_install_id()
            has_other_active = self.api_client.has_other_active_installation(new_key, install_id)
            if has_other_active:
                conflicts["active_instance"] = True
                conflicts["has_conflicts"] = True
                conflicts["details"].append({
                    "type": "active_instance",
                    "severity": "error",
                    "message": f"Miner key {new_key} is already active on another machine",
                    "global_conflict": True,
                    "resolution": "Use a different miner key or stop the other installation"
                })

            # IP-based conflict check for miners with IP limits
            # Check version metadata to see if this miner type has IP enforcement
            ip_limit = None
            try:
                from core.service_manager import get_external_ip

                miner_code = new_miner.get("code", "")
                _cd_logger.info(f"Checking device limits for miner: {miner_code}")
                # Query without platform filter — we only need the root-level "limit" field
                version_data = self.api_client.get_required_version(miner_code, platform=None)
                _cd_logger.info(f"get_required_version({miner_code}, platform=None) returned: {version_data}")
                ip_limit = version_data.get("limit")
                _cd_logger.info(f"Extracted limit for {miner_code}: {ip_limit} (type: {type(ip_limit).__name__})")

                # Only check IP conflicts if miner type has a limit set
                if ip_limit is not None and ip_limit != "no":
                    # Special case: limit=0 means miner type is disabled
                    # Handle both string and int types
                    try:
                        limit_int = int(ip_limit) if isinstance(ip_limit, str) else ip_limit
                        _cd_logger.info(f"Converted limit to int: {limit_int}")
                    except (ValueError, TypeError) as e:
                        limit_int = None
                        _cd_logger.warning(f"Failed to convert limit {ip_limit} to int: {e}")

                    if limit_int == 0:
                        miner_name = new_miner.get("name", new_miner.get("code"))
                        _cd_logger.warning(f"BLOCKING: {miner_name} has limit=0 (disabled)")
                        conflicts["has_conflicts"] = True
                        conflicts["details"].append({
                            "type": "miner_disabled",
                            "severity": "error",
                            "message": f"{miner_name} installations are currently disabled",
                            "limit": 0,
                            "resolution": "This miner type has been temporarily disabled. Please contact support or check for updates."
                        })
                        return conflicts

                    external_ip = get_external_ip()
                    _cd_logger.info(f"External IP: {external_ip}, checking IP status...")
                    ip_status = self.api_client.check_ip_status(external_ip)
                    _cd_logger.info(f"check_ip_status response: {ip_status}")

                    installations_by_type = ip_status.get("installations_by_type", {})
                    current_usage = installations_by_type.get(new_miner.get("code"), {})

                    current_count = current_usage.get("count", 0)
                    limit_value = current_usage.get("limit", ip_limit)
                    _cd_logger.info(f"IP usage for {miner_code}: count={current_count}, limit={limit_value}")

                    # Check if limit is reached
                    if isinstance(limit_value, int) and current_count >= limit_value:
                        details = current_usage.get("details", [])
                        conflicting_keys = [d.get("miner_key", "Unknown") for d in details]

                        # Show last 8 chars of first conflicting key
                        first_key = conflicting_keys[0] if conflicting_keys else "Unknown"
                        key_suffix = first_key[-8:] if len(first_key) > 8 else first_key

                        miner_name = new_miner.get("name", new_miner.get("code"))
                        _cd_logger.warning(f"BLOCKING: IP limit reached for {miner_name} ({current_count}/{limit_value})")

                        conflicts["has_conflicts"] = True
                        conflicts["details"].append({
                            "type": "ip_limit_reached",
                            "severity": "error",
                            "message": f"IP limit reached: {current_count}/{limit_value} {miner_name} installation(s) already active on your network (IP: {external_ip})",
                            "conflicting_keys": conflicting_keys,
                            "conflicting_key_suffix": key_suffix,
                            "external_ip": external_ip,
                            "current_count": current_count,
                            "limit": limit_value,
                            "resolution": f"Only {limit_value} {miner_name} installation(s) allowed per IP address. "
                                         f"Conflicting: ...{key_suffix}. "
                                         f"If this is your miner, uninstall it first. "
                                         f"If someone else on your network is running it, contact support."
                        })
                    else:
                        _cd_logger.info(f"IP limit OK for {miner_code}: {current_count}/{limit_value}")
                else:
                    _cd_logger.info(f"No IP limit for {miner_code} (limit={ip_limit}), skipping IP check")
            except Exception as e:
                # IP detection or validation failed - log to debug file
                import traceback
                error_msg = str(e)
                _cd_logger.error(f"ERROR checking limits for {new_miner.get('code')}: {error_msg}")
                _cd_logger.error(f"Traceback: {traceback.format_exc()}")

                # Block if this miner type has an IP limit configured (not just BM)
                # If we know a limit was set but failed to check, block to be safe
                should_block = ip_limit is not None or new_miner.get("code") == "BM"
                if should_block:
                    _cd_logger.warning(f"Blocking {new_miner.get('code')} due to IP check failure (ip_limit={ip_limit})")
                    conflicts["has_conflicts"] = True
                    conflicts["details"].append({
                        "type": "ip_detection_error",
                        "severity": "error",
                        "message": f"Cannot validate miner availability: {error_msg}",
                        "resolution": "Please check your internet connection and try again. "
                                     "If the problem persists, check firewall settings."
                    })
            
            # Hardware conflict check (shared resources)
            hardware_conflicts = self._check_hardware_resources(new_miner, existing_miners)
            if hardware_conflicts:
                conflicts["hardware_conflict"] = True
                conflicts["has_conflicts"] = True
                conflicts["details"].extend(hardware_conflicts)
                
        except Exception as e:
            conflicts["details"].append({
                "type": "detection_error",
                "severity": "warning",
                "message": f"Error during conflict detection: {str(e)}",
                "resolution": "Manual verification recommended"
            })
        
        return conflicts

    # -------------------- Virtual Machine Detection --------------------
    def _detect_virtual_machine(self) -> Dict[str, Any]:
        """Detect whether the current host is a virtual machine.

        Returns:
            dict: {
                'vm': bool|None,    # True if VM detected, False if confidently physical, None if unknown
                'evidence': list[str],  # Indicators found
                'method': str            # Primary detection method used
            }
        Notes:
            This function uses a best-effort, multi-layer heuristic approach:
            1. Platform-specific commands (systemd-detect-virt, WMI, sysctl)
            2. DMI / SMBIOS product/vendor strings
            3. MAC OUI prefixes common to hypervisor virtual NICs
            4. Process / device artifact hints
        """
        info: Dict[str, Any] = {"vm": None, "evidence": [], "method": "heuristic"}
        try:
            sys_name = platform.system()
            sys_lower = sys_name.lower()

            vm_markers = [
                "virtual", "vmware", "hyper-v", "xen", "qemu", "kvm",
                "parallels", "virtualbox", "vbox", "bochs"
            ]

            # --- Windows ---
            if sys_lower == "windows":
                try:
                    wmi_cmd = ["powershell", "-NoLogo", "-WindowStyle", "Hidden", "-NoProfile", "-Command",
                               "Get-CimInstance Win32_ComputerSystem | Select Manufacturer,Model"]
                    r = subprocess.run(wmi_cmd, capture_output=True, text=True, timeout=5)
                    out = (r.stdout + r.stderr).lower()
                    if any(m in out for m in vm_markers):
                        info["vm"] = True
                        info["evidence"].append(f"WMI Manufacturer/Model: {out.strip()[:120]}")
                        info["method"] = "wmi"
                        return info
                except Exception:
                    pass
                # BIOS vendor / product via wmic (fallback)
                try:
                    r = subprocess.run(["wmic", "computersystem", "get", "manufacturer,model"],
                                        capture_output=True, text=True, timeout=5)
                    out = (r.stdout + r.stderr).lower()
                    if any(m in out for m in vm_markers):
                        info["vm"] = True
                        info["evidence"].append(f"WMIC manufacturer/model: {out.strip()[:120]}")
                        info["method"] = "wmic"
                        return info
                except Exception:
                    pass
                # MAC OUI heuristic
                try:
                    import uuid
                    mac = uuid.getnode()
                    oui = f"{(mac >> 40) & 0xff:02x}:{(mac >> 32) & 0xff:02x}:{(mac >> 24) & 0xff:02x}"
                    vm_ouis = {"00:05:69", "00:0c:29", "00:1c:14", "00:50:56"}  # VMware ranges
                    if oui.lower() in vm_ouis:
                        info["vm"] = True
                        info["evidence"].append(f"VM OUI prefix detected: {oui}")
                        info["method"] = "mac_oui"
                except Exception:
                    pass
                if info["vm"] is None:
                    info["vm"] = False  # Default to physical if no evidence
                return info

            # --- Linux ---
            if sys_lower == "linux":
                # systemd-detect-virt
                try:
                    r = subprocess.run(["systemd-detect-virt"], capture_output=True, text=True, timeout=5)
                    if r.returncode == 0 and r.stdout.strip() and r.stdout.strip() != "none":
                        info["vm"] = True
                        info["evidence"].append(f"systemd-detect-virt: {r.stdout.strip()}")
                        info["method"] = "systemd-detect-virt"
                        return info
                except Exception:
                    pass
                # DMI product name / sys vendor
                dmi_paths = [
                    "/sys/class/dmi/id/product_name",
                    "/sys/class/dmi/id/sys_vendor",
                    "/sys/class/dmi/id/bios_vendor"
                ]
                for p in dmi_paths:
                    try:
                        if os.path.exists(p):
                            content = Path(p).read_text(encoding="utf-8", errors="ignore").lower()
                            if any(m in content for m in vm_markers):
                                info["vm"] = True
                                info["evidence"].append(f"DMI {os.path.basename(p)}: {content.strip()[:80]}")
                                info["method"] = "dmi"
                                return info
                    except Exception:
                        pass
                # /proc/cpuinfo hypervisor flag
                try:
                    cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore").lower()
                    if "hypervisor" in cpuinfo:
                        info["vm"] = True
                        info["evidence"].append("CPU hypervisor flag present")
                        info["method"] = "cpuinfo"
                        return info
                except Exception:
                    pass
                if info["vm"] is None:
                    info["vm"] = False
                return info

            # --- macOS ---
            if sys_lower == "darwin":
                try:
                    r = subprocess.run(["sysctl", "-a"], capture_output=True, text=True, timeout=8)
                    out = r.stdout.lower()
                    if any(m in out for m in vm_markers):
                        info["vm"] = True
                        info["evidence"].append("sysctl hypervisor indicators present")
                        info["method"] = "sysctl"
                        return info
                except Exception:
                    pass
                # Apple Silicon / hardware model seldom virtualized publicly; default False if no evidence
                info["vm"] = False if info["vm"] is None else info["vm"]
                return info

            # Other/unknown OS
            info["vm"] = None
            return info
        except Exception as e:
            info["vm"] = None
            info["evidence"].append(f"vm-detect-error: {e}")
            return info
    
    def resolve_conflicts(self, conflicts: Dict[str, Any], resolution_strategy: str) -> Dict[str, Any]:
        """
        Resolve detected conflicts based on strategy.
        
        Args:
            conflicts: Conflict information from check_device_conflicts
            resolution_strategy: How to resolve conflicts ("replace", "abort", "force")
            
        Returns:
            Resolution results
        """
        result = {
            "success": False,
            "actions_taken": [],
            "errors": []
        }
        
        if not conflicts.get("has_conflicts"):
            result["success"] = True
            result["message"] = "No conflicts to resolve"
            return result
        
        try:
            if resolution_strategy == "replace":
                # Stop and remove existing miners
                if self._remove_existing_miners():
                    result["actions_taken"].append("Removed existing miner installations")
                    result["success"] = True
                else:
                    result["errors"].append("Failed to remove existing miners")
                    
            elif resolution_strategy == "abort":
                result["message"] = "Installation aborted due to conflicts"
                return result
                
            elif resolution_strategy == "force":
                # Force installation despite conflicts
                result["actions_taken"].append("Forced installation ignoring conflicts")
                result["success"] = True
                result["warnings"] = ["Installation forced - conflicts may cause issues"]
                
            else:
                result["errors"].append(f"Unknown resolution strategy: {resolution_strategy}")
                
        except Exception as e:
            result["errors"].append(f"Error during conflict resolution: {str(e)}")
        
        return result
    
    def _get_installed_miners(self) -> List[Dict[str, Any]]:
        """Get list of installed miners on this device."""
        installed = []
        
        try:
            # Check for installation directory and config files
            if self.platform.startswith('win'):
                base_dirs = [
                    Path(os.environ.get("PROGRAMDATA", r"C:\\ProgramData")) / "FryNetworks",
                    Path.home() / "AppData" / "Local" / "FryNetworks"
                ]
            else:
                base_dirs = [
                    Path("/var/lib/frynetworks"),
                    Path.home() / ".local" / "share" / "frynetworks"
                ]
            
            for base_dir in base_dirs:
                if base_dir.exists():
                    for miner_dir in base_dir.iterdir():
                        if miner_dir.is_dir() and miner_dir.name.startswith("miner-"):
                            miner_code = miner_dir.name.split("-")[1].upper()
                            if miner_code in self.parser.MINER_TYPES:
                                # Check for config file in config/ subdirectory first (new location)
                                config_file = miner_dir / "config" / "installer_config.json"
                                if not config_file.exists():
                                    # Fall back to root directory (legacy location)
                                    config_file = miner_dir / "installer_config.json"
                                
                                if config_file.exists():
                                    try:
                                        with open(config_file) as f:
                                            config = json.load(f)
                                        
                                        miner_info = self.parser.MINER_TYPES[miner_code].copy()
                                        miner_info.update({
                                            "code": miner_code,
                                            "install_dir": str(miner_dir),
                                            "config": config
                                        })
                                        installed.append(miner_info)
                                    except Exception:
                                        pass
                                        
        except Exception:
            pass
        
        return installed
    
    def _get_active_miner_processes(self) -> List[Dict[str, Any]]:
        """Get list of active miner processes."""
        active = []
        
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    proc_info = proc.info
                    name = proc_info.get('name', '').lower()
                    cmdline = proc_info.get('cmdline', [])
                    
                    # Look for miner executables
                    if any(keyword in name for keyword in ['miner', 'frm_', 'fry_']):
                        # Try to extract miner key from command line
                        miner_key = None
                        for arg in cmdline:
                            if isinstance(arg, str) and '-' in arg and len(arg) >= 30:
                                # Might be a miner key
                                if self.parser.validate_key_format_only(arg):
                                    miner_key = arg
                                    break
                        
                        active.append({
                            "pid": proc_info.get('pid'),
                            "name": name,
                            "key": miner_key,
                            "cmdline": cmdline
                        })
                        
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                    
        except Exception:
            pass
        
        return active
    
    def _check_hardware_resources(self, new_miner: Dict[str, Any], existing_miners: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Check for hardware resource conflicts."""
        conflicts = []
        
        # Define exclusive hardware resources
        exclusive_resources = {
            "audio": ["IDM", "ODM"],  # Audio miners share audio hardware
            "gnss": ["ISM", "OSM"],   # Satellite miners share GNSS hardware
        }
        
        new_code = new_miner["code"]
        
        for resource, codes in exclusive_resources.items():
            if new_code in codes:
                # Check if any existing miner uses the same resource
                for existing in existing_miners:
                    if existing["code"] in codes and existing["code"] != new_code:
                        conflicts.append({
                            "type": "hardware_conflict",
                            "severity": "error",
                            "message": f"Hardware conflict: {resource} resource already in use",
                            "existing": existing,
                            "resource": resource,
                            "resolution": f"Stop {existing['name']} or use different hardware"
                        })
        
        return conflicts
    
    def _remove_existing_miners(self) -> bool:
        """Remove existing miner installations."""
        try:
            installed = self._get_installed_miners()
            
            for miner in installed:
                install_dir = Path(miner.get("install_dir", ""))
                if install_dir.exists():
                    # Stop any running services first
                    self._stop_miner_service(miner["code"])
                    
                    # Remove installation directory
                    import shutil
                    shutil.rmtree(install_dir, ignore_errors=True)
            
            return True
            
        except Exception:
            return False
    
    def _stop_miner_service(self, miner_code: str) -> bool:
        """Stop a miner service if running."""
        try:
            # Import naming here since it's used in both Windows and Linux branches
            from . import naming
            
            if self.platform.startswith('win'):
                service_name = f"{naming.poc_prefix(miner_code)}*"
                subprocess.run(["sc", "stop", service_name], 
                             capture_output=True, check=False)
            else:
                # Attempt to stop any registered FRY_PoC systemd unit if present
                service_unit = naming.poc_unit_name(miner_code)
                subprocess.run(["systemctl", "stop", service_unit],
                               capture_output=True, check=False)

                # Also ensure any FRY_* processes for this miner are terminated
                # (some distributions may not register the binary as a systemd unit)
                try:
                    # Patterns to match FRY processes/binaries for this miner
                    patterns = [f"FRY_PoC_{miner_code}", f"FRY_{miner_code}"]
                    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                        try:
                            pname = (proc.info.get('name') or '')
                            cmdline = ' '.join(proc.info.get('cmdline') or [])
                            if any(pat in pname or pat in cmdline for pat in patterns):
                                try:
                                    proc.terminate()
                                except (psutil.NoSuchProcess, psutil.AccessDenied):
                                    pass
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                except Exception:
                    # Non-fatal; continue
                    pass
            return True
        except Exception:
            return False
    
    def get_conflict_summary(self, conflicts: Dict[str, Any]) -> str:
        """Get a human-readable summary of conflicts."""
        if not conflicts.get("has_conflicts"):
            return "No conflicts detected - ready for installation"
        
        summary_parts = []
        
        if conflicts.get("device_limit"):
            summary_parts.append("• Device already has a miner installed")
        
        if conflicts.get("exclusive_pair"):
            summary_parts.append("• Conflicting miner type detected")
        
        if conflicts.get("active_instance"):
            summary_parts.append("• Miner key already in use")
        
        if conflicts.get("hardware_conflict"):
            summary_parts.append("• Hardware resource conflicts detected")
        
        return "\\n".join(summary_parts)
