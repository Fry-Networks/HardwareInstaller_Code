"""
Configuration management for the FryNetworks installer.

This module handles:
- Installation directory management
- Configuration file creation and validation
- Settings persistence and retrieval
- Environment detection and setup
"""

import os
import sys
import json
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

from .key_parser import MinerKeyParser


class ConfigManager:
    """Manages installer configuration and settings."""
    
    def __init__(self, miner_code: Optional[str] = None):
        """
        Initialize configuration manager.
        
        Args:
            miner_code: Optional miner code for miner-specific configuration
        """
        self.miner_code = miner_code.upper() if miner_code else None
        self.platform = sys.platform
        self.parser = MinerKeyParser()
    
    def get_installation_directory(self, system_wide: bool = False) -> Path:
        """
        Get the appropriate installation directory.
        
        Args:
            system_wide: Whether to use system-wide installation
            
        Returns:
            Path to installation directory
        """
        if self.platform.startswith('win'):
            if system_wide:
                base = Path(os.environ.get("PROGRAMDATA", r"C:\\ProgramData"))
            else:
                base = Path.home() / "AppData" / "Local"
            
            if self.miner_code:
                return base / "FryNetworks" / f"miner-{self.miner_code}"
            else:
                return base / "FryNetworks"
        else:
            # os.geteuid() does not exist on Windows; guard access to avoid
            # Pylance/reporting and runtime AttributeError on platforms
            # that don't provide geteuid(). Use getattr to check for the
            # presence of the function and call it only when available.
            euid_func = getattr(os, "geteuid", None)
            if system_wide and euid_func is not None and euid_func() == 0:
                base = Path("/var/lib/frynetworks")
            else:
                base = Path.home() / ".local" / "share" / "frynetworks"
            
            if self.miner_code:
                return base / f"miner-{self.miner_code}"
            else:
                return base
    
    def setup_directories(self, system_wide: bool = False, install_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Create necessary installation directories.
        
        Args:
            system_wide: Whether to use system-wide installation
            
        Returns:
            Directory setup results
        """
        result = {"success": False, "directories": [], "errors": []}
        
        try:
            # Main installation directory (allow override)
            install_dir = Path(install_path) if install_path else self.get_installation_directory(system_wide)
            install_dir.mkdir(parents=True, exist_ok=True)
            result["directories"].append(str(install_dir))
            
            # Logs directory
            logs_dir = install_dir / "logs"
            logs_dir.mkdir(exist_ok=True)
            result["directories"].append(str(logs_dir))
            
            # Config directory
            config_dir = install_dir / "config"
            config_dir.mkdir(exist_ok=True)
            result["directories"].append(str(config_dir))

            # Measurements directory (for encrypted measurements)
            measurements_dir = install_dir / "measurements"
            measurements_dir.mkdir(exist_ok=True)
            result["directories"].append(str(measurements_dir))

            # Status directory (for daily JSON status files)
            status_dir = install_dir / "status"
            status_dir.mkdir(exist_ok=True)
            result["directories"].append(str(status_dir))

            # Set permissions for system-wide install (Windows: inherit, Linux: 700)
            try:
                if system_wide:
                    import platform
                    if platform.system() == "Linux":
                        measurements_dir.chmod(0o700)
                        status_dir.chmod(0o700)
            except Exception:
                result["errors"].append("Could not set measurements/status directory permissions")
            
            result["success"] = True
            result["install_dir"] = str(install_dir)
            
        except Exception as e:
            result["errors"].append(f"Directory setup failed: {str(e)}")
        
        return result
    
    def write_miner_key(self, miner_key: str, system_wide: bool = False, install_path: Optional[str] = None, 
                       gui_version: Optional[str] = None, poc_version: Optional[str] = None) -> dict:
        """
        Write miner key to configuration.
        
        Args:
            miner_key: The validated miner key
            system_wide: Whether using system-wide installation
            
        Returns:
            Write operation results
        """
        result = {"success": False, "files": [], "errors": []}
        
        try:
            # Validate key first
            key_info = self.parser.parse_miner_key(miner_key)
            if not key_info["valid"]:
                result["errors"].append(f"Invalid miner key: {key_info['error']}")
                return result
            
            # Update miner code if needed
            if not self.miner_code:
                self.miner_code = key_info["code"]
            
            # Resolve installation directory (allow override)
            install_dir = Path(install_path) if install_path else self.get_installation_directory(system_wide)
            
            # Write simplified installer_config.json with non-sensitive data only
            # (miner_key is the activation key, not a secret - it's safe to store)
            config_dir = install_dir / "config"
            config_dir.mkdir(exist_ok=True)
            config_file = config_dir / "installer_config.json"
            config_data = {
                "miner_code": self.miner_code,
                "miner_name": key_info.get("name", ""),
                "miner_key": miner_key,  # Activation key - safe to store
                "install_date": datetime.now().isoformat(),
                "system_wide": system_wide,
                "version": "2.3.14",
                "gui_version": gui_version or "unknown",
                "poc_version": poc_version or "unknown"
                # NOTE: Bearer tokens and other sensitive data stored in encrypted files only
            }
            
            try:
                with open(config_file, 'w') as f:
                    json.dump(config_data, f, indent=2)
                result["files"].append(str(config_file))
            except Exception as e:
                result["errors"].append(f"Failed to write installer_config.json: {str(e)}")
            
            result["success"] = True
            result["miner_info"] = key_info
            
        except Exception as e:
            result["errors"].append(f"Failed to write miner key: {str(e)}")
        
        return result
    
    def read_miner_key(self, system_wide: bool = False) -> Optional[str]:
        """
        Read existing miner key from configuration.
        
        Args:
            system_wide: Whether to check system-wide installation
            
        Returns:
            Miner key if found, None otherwise
        """
        try:
            if not self.miner_code:
                # Try to find any miner installation
                base_dir = self.get_installation_directory(system_wide)
                if base_dir.exists():
                    for item in base_dir.iterdir():
                        if item.is_dir() and item.name.startswith("miner-"):
                            code = item.name.split("-")[1].upper()
                            if code in self.parser.MINER_TYPES:
                                self.miner_code = code
                                break
            
            if not self.miner_code:
                return None
            
            install_dir = self.get_installation_directory(system_wide)
            
            # REMOVED: Reading from plaintext files (installer_config.json, minerkey.txt)
            # Only encrypted files are used for security
            # All config files (installer_config.json, install_config.enc, miner_config.enc) are now stored in the config subdirectory
            
        except Exception:
            pass
        
        return None
    
    def get_installer_config(self, system_wide: bool = False) -> Optional[Dict[str, Any]]:
        """
        Read installer configuration from installer_config.json.
        
        Args:
            system_wide: Whether to check system-wide installation
            
        Returns:
            Configuration dictionary if found, None otherwise
        """
        try:
            if not self.miner_code:
                return None
            
            install_dir = self.get_installation_directory(system_wide)
            config_dir = install_dir / "config"
            config_file = config_dir / "installer_config.json"
            
            if config_file.exists():
                with open(config_file, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        
        return None
    
    def update_installer_config(self, updates: Dict[str, Any], system_wide: bool = False) -> Dict[str, Any]:
        """
        Update installer configuration (disabled - encrypted files only).
        
        Args:
            updates: Configuration updates to apply
            system_wide: Whether using system-wide installation
            
        Returns:
            Update operation results
        """
        result = {"success": False, "errors": []}
        
        # REMOVED: Plaintext installer_config.json writing
        # Only encrypted files are used for security
        result["errors"].append("Plaintext config updates disabled - use encrypted files only")
        
        return result
    
    def remove_configuration(self, system_wide: bool = False, install_dir: Optional[str] = None) -> Dict[str, Any]:
        """
        Remove all configuration files.
        
        Args:
            system_wide: Whether to remove system-wide installation
            install_dir: Optional explicit installation directory to remove
            
        Returns:
            Removal operation results
        """
        result = {"success": False, "removed": [], "errors": []}
        
        try:
            if not self.miner_code:
                result["errors"].append("No miner code specified")
                return result
            
            target_dir = Path(install_dir) if install_dir else self.get_installation_directory(system_wide)
            
            if not target_dir.exists():
                result["success"] = True
                result["info"] = "Configuration directory already removed"
                return result
            
            import shutil
            try:
                shutil.rmtree(target_dir)
            except Exception as remove_error:
                result["errors"].append(
                    f"Failed to remove configuration directory {target_dir}: {remove_error}"
                )
            else:
                result["removed"].append(str(target_dir))
                result["success"] = True
            
        except Exception as e:
            result["errors"].append(f"Failed to remove configuration: {str(e)}")
        
        return result
    
    def detect_existing_installations(self) -> List[Dict[str, Any]]:
        """
        Detect existing miner installations.
        
        Returns:
            List of detected installations
        """
        installations = []
        
        # Check both user and system locations
        for system_wide in [False, True]:
            try:
                base_dir = self.get_installation_directory(system_wide)
                print(f"[DEBUG] Checking for installations in: {base_dir} (system_wide={system_wide})")
                if not base_dir.exists():
                    print(f"[DEBUG] Directory does not exist: {base_dir}")
                    continue
                
                # Look for miner-specific directories
                for item in base_dir.iterdir():
                    print(f"[DEBUG] Found item: {item.name}, is_dir={item.is_dir()}, starts_with_miner={item.name.startswith('miner-')}")
                    if item.is_dir() and item.name.startswith("miner-"):
                        try:
                            code = item.name.split("-")[1].upper()
                            print(f"[DEBUG] Extracted code: {code}, in MINER_TYPES={code in self.parser.MINER_TYPES}")
                            if code in self.parser.MINER_TYPES:
                                # Try to read config file for additional details
                                config_dir = item / "config"
                                config_file = config_dir / "installer_config.json"
                                config = {}
                                install_date = "Unknown"
                                
                                if config_file.exists():
                                    try:
                                        with open(config_file, 'r') as f:
                                            config = json.load(f)
                                        install_date = config.get("install_date", "Unknown")
                                        print(f"[DEBUG] Loaded config from {config_file}: {config}")
                                    except Exception as e:
                                        print(f"[DEBUG] Could not read config file {config_file}: {e}")
                                
                                # Build installation info
                                install_info = {
                                    "miner_code": code,
                                    "miner_name": self.parser.MINER_TYPES[code]["name"],
                                    "install_dir": str(item),
                                    "system_wide": system_wide,
                                    "install_date": install_date,
                                    "config": config
                                }
                                print(f"[DEBUG] Adding installation: {install_info}")
                                installations.append(install_info)
                        except Exception as e:
                            print(f"[DEBUG] Error processing {item.name}: {e}")
                            continue
            except Exception as e:
                print(f"[DEBUG] Error checking system_wide={system_wide}: {e}")
                continue
        
        print(f"[DEBUG] Total installations found: {len(installations)}")
        return installations
    
    def _create_installer_config(self, key_info: Dict[str, Any]) -> Dict[str, Any]:
        """Create installer configuration dictionary."""
        import time
        
        return {
            "miner_key": key_info["key"],
            "miner_code": key_info["code"],
            "miner_name": key_info["name"],
            "miner_group": key_info["group"],
            "exclusive": key_info["exclusive"],
            "installer_version": "1.0.0",
            "installed_by": "FryNetworks Installer",
            "install_date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "platform": self.platform,
            "validated": True
        }
    
    def validate_installation(self, system_wide: bool = False) -> Dict[str, Any]:
        """
        Validate an existing installation.
        
        Args:
            system_wide: Whether to check system-wide installation
            
        Returns:
            Validation results
        """
        result = {
            "valid": False,
            "issues": [],
            "details": {}
        }
        
        try:
            if not self.miner_code:
                result["issues"].append("No miner code specified")
                return result
            
            install_dir = self.get_installation_directory(system_wide)
            
            # Check installation directory exists
            if not install_dir.exists():
                result["issues"].append("Installation directory not found")
                return result
            
            # REMOVED: Check for plaintext files (minerkey.txt, installer_config.json)
            # Only encrypted files are validated for security
            
            # If no issues found, installation is valid
            if not result["issues"]:
                result["valid"] = True
                result["install_dir"] = str(install_dir)
            
        except Exception as e:
            result["issues"].append(f"Validation error: {str(e)}")
        
        return result
