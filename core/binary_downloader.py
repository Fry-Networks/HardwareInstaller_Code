"""
FryNetworks Binary Downloader

This module handles:
- Downloading miner binaries from GitHub repository
- Platform-specific binary selection (Linux/Windows)
- Version management and caching
- Binary verification and installation
"""

import os
import hashlib
import requests
import platform
from pathlib import Path
from typing import Dict, Any, Optional

from .key_parser import MinerKeyParser


class BinaryDownloader:
    """Download and manage miner binaries from GitHub repository."""
    
    def __init__(self):
        """Initialize binary downloader with environment configuration."""
        self.github_repo_url = os.environ.get('GITHUB_REPO_URL', 'https://github.com/FryDevsTestingLab/HardwareExe')
        self.github_branch = os.environ.get('GITHUB_BINARIES_BRANCH', 'main')
        self.github_path = os.environ.get('GITHUB_BINARIES_PATH', 'dist')
        
        # Platform detection
        self.platform = self._detect_platform()
        
        # Local cache directory
        self.cache_dir = self._get_cache_directory()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Supported binary types
        self.parser = MinerKeyParser()
        
    def _detect_platform(self) -> str:
        """Detect current platform for binary selection."""
        system = platform.system().lower()
        if system == "windows":
            return "windows"
        elif system == "linux":
            return "linux"
        elif system == "darwin":
            return "macos"  # Future support
        else:
            return "linux"  # Default fallback
    
    def _get_cache_directory(self) -> Path:
        """Get local cache directory for downloaded binaries."""
        if self.platform == "windows":
            cache_base = Path(os.environ.get("LOCALAPPDATA", "~/AppData/Local")).expanduser()
        else:
            cache_base = Path.home() / ".cache"
        
        return cache_base / "frynetworks" / "binaries"
    
    def get_available_versions(self, miner_code: str) -> Dict[str, Any]:
        """
        Get available versions for a miner type from GitHub.
        
        Args:
            miner_code: Miner code (BM, IDM, etc.)
            
        Returns:
            Dictionary with available versions and metadata
        """
        result = {"success": False, "versions": [], "error": None}
        
        try:
            # Construct GitHub API URL for directory listing
            api_url = self._build_github_api_url(miner_code)
            
            print(f"🔍 Checking available versions for {miner_code}...")
            print(f"   URL: {api_url}")
            
            response = requests.get(api_url, timeout=30)
            
            if response.status_code == 200:
                files = response.json()
                versions = []
                
                for file_info in files:
                    if file_info.get("type") == "file":
                        filename = file_info["name"]
                        # Parse version from filename
                        if self._is_valid_binary_name(filename, miner_code):
                            version_info = self._parse_binary_info(filename, miner_code)
                            if version_info:
                                version_info["download_url"] = file_info["download_url"]
                                version_info["size"] = file_info["size"]
                                version_info["sha"] = file_info["sha"]
                                versions.append(version_info)
                
                result["success"] = True
                result["versions"] = sorted(versions, key=lambda x: x["version"], reverse=True)
                
            elif response.status_code == 404:
                result["error"] = f"Miner type {miner_code} not found in repository"
            else:
                result["error"] = f"GitHub API error: {response.status_code}"
                
        except requests.exceptions.RequestException as e:
            result["error"] = f"Network error: {str(e)}"
        except Exception as e:
            result["error"] = f"Unexpected error: {str(e)}"
        
        return result
    
    def download_binary(self, miner_code: str, version: str = "latest") -> Dict[str, Any]:
        """
        Download miner binary for specified version.
        
        Args:
            miner_code: Miner code (BM, IDM, etc.)
            version: Version to download ("latest" for newest)
            
        Returns:
            Download result with local path
        """
        result = {"success": False, "binary_path": None, "error": None, "version_info": None}
        
        try:
            # Get available versions
            versions_result = self.get_available_versions(miner_code)
            
            if not versions_result["success"]:
                result["error"] = f"Failed to get versions: {versions_result['error']}"
                return result
            
            if not versions_result["versions"]:
                result["error"] = f"No binaries found for {miner_code} on {self.platform}"
                return result
            
            # Select version
            if version == "latest":
                version_info = versions_result["versions"][0]  # Already sorted by version desc
            else:
                version_info = next(
                    (v for v in versions_result["versions"] if v["version"] == version),
                    None
                )
                
                if not version_info:
                    result["error"] = f"Version {version} not found for {miner_code}"
                    return result
            
            # Check if already cached
            cached_path = self._get_cached_binary_path(miner_code, version_info["version"])
            if cached_path.exists() and self._verify_binary(cached_path, version_info.get("sha")):
                print(f"✓ Using cached binary: {cached_path}")
                result["success"] = True
                result["binary_path"] = str(cached_path)
                result["version_info"] = version_info
                return result
            
            # Download binary
            print(f"📥 Downloading {miner_code} v{version_info['version']} ({version_info['size']} bytes)...")
            
            download_result = self._download_file(
                version_info["download_url"],
                cached_path,
                version_info.get("sha")
            )
            
            if download_result["success"]:
                # Make executable on Unix systems
                if self.platform != "windows":
                    cached_path.chmod(0o755)
                
                result["success"] = True
                result["binary_path"] = str(cached_path)
                result["version_info"] = version_info
                print(f"✓ Downloaded successfully: {cached_path}")
            else:
                result["error"] = download_result["error"]
                
        except Exception as e:
            result["error"] = f"Download failed: {str(e)}"
        
        return result
    
    def _build_github_api_url(self, miner_code: str) -> str:
        """Build GitHub API URL for miner binaries directory."""
        # Extract owner and repo from GitHub URL
        # https://github.com/FryDevsTestingLab/HardwareExe -> FryDevsTestingLab/HardwareExe
        repo_parts = self.github_repo_url.replace("https://github.com/", "").strip("/")
        
        # Determine binary type based on miner code
        if miner_code in ["BM", "RDN", "SVN", "SDN"]:
            binary_type = "svc"  # Service binaries
        else:
            binary_type = "svc"  # Default to service for now
        
        # Build API URL
        path = f"{self.github_path}/{self.platform}/{binary_type}/{miner_code}"
        api_url = f"https://api.github.com/repos/{repo_parts}/contents/{path}"
        
        if self.github_branch != "main":
            api_url += f"?ref={self.github_branch}"
        
        return api_url
    
    def _is_valid_binary_name(self, filename: str, miner_code: str) -> bool:
        """Check if filename matches expected binary naming pattern."""
        # Updated naming: GUI is "FRY_<MINER>_v<ver>" and service/PoC is "FRY_PoC_<MINER>_v<ver>"
        from . import naming
        if self.platform == "windows":
            # Windows executables end with .exe; accept FRY_* naming only
            prefixes = [naming.poc_prefix(miner_code), naming.gui_prefix(miner_code)]
            return any(prefix in filename for prefix in prefixes) and filename.lower().endswith(".exe")
        else:
            # Non-Windows: accept the FRY_* naming (extension optional)
            prefixes = [naming.poc_prefix(miner_code), naming.gui_prefix(miner_code)]
            return any(prefix in filename for prefix in prefixes)
    
    def _parse_binary_info(self, filename: str, miner_code: str) -> Optional[Dict[str, str]]:
        """Parse version and type information from binary filename."""
        try:
            # Extract version from filename
            # Examples: FRY_PoC_IDM_v5.5.0.exe, FRY_BM_v1.0.3
            
            if "_v" in filename:
                version_part = filename.split("_v")[-1]
                if self.platform == "windows" and version_part.endswith(".exe"):
                    version_part = version_part[:-4]  # Remove .exe
                
                # Extract just the version number (e.g., "1.0.3" from "1.0.3")
                version = version_part.split("_")[0]  # Take first part if more underscores
                
                # Determine binary type using central naming helpers
                from . import naming
                if naming.is_poc_filename(filename):
                    binary_type = "poc"
                elif naming.is_gui_filename(filename):
                    binary_type = "gui"
                else:
                    binary_type = "unknown"
                
                return {
                    "filename": filename,
                    "version": version,
                    "type": binary_type,
                    "miner_code": miner_code,
                    "platform": self.platform
                }
            
        except Exception:
            pass
        
        return None
    
    def _get_cached_binary_path(self, miner_code: str, version: str) -> Path:
        """Get local cache path for binary."""
        # Default to the FRY_PoC naming for cached service binaries. On Windows
        # keep the .exe extension. This aligns cached filenames with release
        # asset names: FRY_PoC_<MINER>_v<version>.exe and FRY_<MINER>_v<version>.exe
        from . import naming
        windows = self.platform == "windows"
        filename = naming.poc_asset(miner_code, version, windows=windows)
        return self.cache_dir / miner_code / filename
    
    def _verify_binary(self, binary_path: Path, expected_sha: Optional[str] = None) -> bool:
        """Verify downloaded binary integrity."""
        if not binary_path.exists():
            return False
        
        # Basic existence and size check
        if binary_path.stat().st_size == 0:
            return False
        
        # SHA verification if provided
        if expected_sha:
            try:
                sha1_hash = hashlib.sha1()
                with open(binary_path, 'rb') as f:
                    for chunk in iter(lambda: f.read(4096), b""):
                        sha1_hash.update(chunk)
                
                calculated_sha = sha1_hash.hexdigest()
                return calculated_sha == expected_sha
            except Exception:
                return False
        
        return True
    
    def _download_file(self, download_url: str, target_path: Path, expected_sha: Optional[str] = None) -> Dict[str, Any]:
        """Download file from URL to target path."""
        result = {"success": False, "error": None}
        
        try:
            # Create target directory
            target_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Download with progress tracking
            response = requests.get(download_url, stream=True, timeout=60)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0
            
            with open(target_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        
                        # Simple progress indication
                        if total_size > 0:
                            progress = (downloaded_size / total_size) * 100
                            if downloaded_size % (1024 * 1024) == 0:  # Every MB
                                print(f"   Progress: {progress:.1f}% ({downloaded_size // 1024 // 1024}MB)")
            
            # Verify download
            if self._verify_binary(target_path, expected_sha):
                result["success"] = True
            else:
                result["error"] = "Binary verification failed"
                target_path.unlink(missing_ok=True)  # Remove corrupted file
                
        except requests.exceptions.RequestException as e:
            result["error"] = f"Download error: {str(e)}"
        except Exception as e:
            result["error"] = f"File operation error: {str(e)}"
        
        return result
    
    def get_binary_info(self, miner_code: str) -> Dict[str, Any]:
        """Get information about available binaries for miner type."""
        miner_info = self.parser.MINER_TYPES.get(miner_code, {})
        
        return {
            "miner_code": miner_code,
            "miner_name": miner_info.get("name", f"Unknown Miner ({miner_code})"),
            "platform": self.platform,
            "cache_directory": str(self.cache_dir),
            "github_repo": self.github_repo_url,
            "supported": miner_code in self.parser.MINER_TYPES
        }
    
    def clear_cache(self, miner_code: Optional[str] = None) -> Dict[str, Any]:
        """Clear downloaded binary cache."""
        result = {"success": False, "message": "", "cleared_files": []}
        
        try:
            if miner_code:
                # Clear specific miner cache
                miner_cache = self.cache_dir / miner_code
                if miner_cache.exists():
                    import shutil
                    shutil.rmtree(miner_cache)
                    result["cleared_files"].append(str(miner_cache))
            else:
                # Clear entire cache
                if self.cache_dir.exists():
                    import shutil
                    shutil.rmtree(self.cache_dir)
                    result["cleared_files"].append(str(self.cache_dir))
            
            result["success"] = True
            result["message"] = f"Cache cleared for {miner_code or 'all miners'}"
            
        except Exception as e:
            result["error"] = f"Failed to clear cache: {str(e)}"
        
        return result