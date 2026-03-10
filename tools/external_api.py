from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Union
from pathlib import Path

import requests

_logger = logging.getLogger(__name__)
_logger.addHandler(logging.NullHandler())

# Use string forward-references for optional optimized client type to avoid
# import cycles at runtime. The optimized client is imported dynamically
# inside get_external_api_client when available.


class ApiError(RuntimeError):
    """Raised when the external backend rejects a request or returns invalid data."""


class ApiUnavailableError(ApiError):
    """Raised when the external API is temporarily unavailable (HTTP 5xx)."""


class ExternalApiClient:
    """HTTP client that mirrors the legacy MongoDB reads/writes via REST calls.

    Each public method documents the expected endpoint so the server can be
    implemented without reading the original Mongo-specific code.
    """

    def __init__(self, base_url: str, *, token: Optional[str] = None, timeout: float = 10.0):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = max(1.0, float(timeout))
        _logger.info(f"[ExternalApiClient] Initialized with base_url: {self.base_url}")

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    # Retry delays (in seconds) for 5xx errors: 1 min, 2 min, 5 min, 10 min
    _RETRY_DELAYS = [60, 120, 300, 600]

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(1 + len(self._RETRY_DELAYS)):
            if _logger:
                _logger.info(f"[HTTP REQUEST] {method} {url}" + (f" (retry {attempt})" if attempt else ""))
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                    timeout=self.timeout,
                )
                if _logger:
                    _logger.info(f"[HTTP RESPONSE] {method} {url} - Status: {response.status_code}")
            except requests.RequestException as exc:
                if _logger:
                    _logger.error(f"[HTTP ERROR] {method} {url} - {exc}")
                raise ApiError(f"{method} {url} failed: {exc}") from exc

            if response.status_code == 204 or not response.content:
                return None

            # Retry on 5xx with backoff
            if 500 <= response.status_code < 600:
                last_exc = ApiUnavailableError(f"{method} {url} returned {response.status_code}: {response.text}")
                if attempt < len(self._RETRY_DELAYS):
                    delay = self._RETRY_DELAYS[attempt]
                    if _logger:
                        _logger.warning(f"[RETRY] {method} {url} returned {response.status_code}, retrying in {delay}s (attempt {attempt + 1}/{len(self._RETRY_DELAYS)})")
                    time.sleep(delay)
                    continue
                # All retries exhausted
                raise last_exc

            if response.status_code >= 400:
                raise ApiError(f"{method} {url} returned {response.status_code}: {response.text}")
            content_type = response.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                raise ApiError(f"{method} {url} expected JSON, got {content_type}")
            try:
                return response.json()
            except ValueError as exc:
                raise ApiError(f"{method} {url} returned invalid JSON: {exc}") from exc

        # Should not reach here, but safety net
        raise last_exc or ApiError(f"{method} {url} failed after all retries")

    def get_required_version(self, miner_code: str, platform: Optional[str] = "windows", use_test: bool = False) -> Dict[str, str]:
        """GET {base}/versions/{miner_code}[?platform={platform}] -> nested version data

        Returns both software (GUI) and PoC versions for the specified platform.
        New API returns versions nested under the platform key with "_needed" suffix.

        When platform is None, no platform filter is sent and the full document
        is returned (useful for reading root-level fields like ``limit``).

        Args:
            miner_code: The miner code (BM, IDM, etc.)
            platform: Target platform ("windows" or "linux"), or None to skip filtering
            use_test: If True, queries test-windows or test-linux platforms for QA test versions
        """
        _logger.info(f"[get_required_version] Calling API with miner_code={miner_code}, platform={platform}, use_test={use_test}")

        # Build query params — omit platform entirely when caller passes None
        query_platform: Optional[str] = None
        params: Optional[Dict[str, Any]] = None
        if platform is not None:
            query_platform = f"test-{platform}" if use_test else platform
            params = {"platform": query_platform}

        try:
            data = self._request("GET", f"/versions/{miner_code}", params=params)
        except ApiError as exc:
            # Treat 404/not found as an empty result to simplify callers
            msg = str(exc).lower()
            if "404" in msg or "not found" in msg:
                return {}
            raise

        if isinstance(data, dict):
            result = {}

            # Always check for limit at root level (for IP enforcement)
            limit = data.get("limit")
            if _logger:
                _logger.info(f"[get_required_version] Raw API response: {data}")
                _logger.info(f"[get_required_version] Extracted limit from root: {limit}")
            if limit is not None:
                result["limit"] = limit
                if _logger:
                    _logger.info(f"[get_required_version] Added limit to result: {limit}")

            # Also check for limit inside platform data
            platform_data = data.get(query_platform)
            if isinstance(platform_data, dict):
                # Pick up limit from platform section if not found at root
                if "limit" not in result and platform_data.get("limit") is not None:
                    result["limit"] = platform_data["limit"]

                software_version = platform_data.get("software_version_needed")
                poc_version = platform_data.get("poc_version_needed")

                if software_version and isinstance(software_version, str):
                    result["software_version"] = software_version.strip()
                if poc_version and isinstance(poc_version, str):
                    result["poc_version"] = poc_version.strip()

                # Return result which now includes limit (from root or platform) + versions
                return result if result else {}

            # If no platform-specific data, check for root-level versions (older format)
            # Extract IP limit from version metadata (can be at root level)
            # The limit field indicates how many installations are allowed per IP address
            if limit is not None:
                result["limit"] = limit
            # If we also have version info without platform nesting, include it
            if "software_version" in data or "poc_version" in data:
                if "software_version" in data:
                    result["software_version"] = data["software_version"]
                if "poc_version" in data:
                    result["poc_version"] = data["poc_version"]
            return result

            # If no platform section, include any 'detail' message present to aid callers
            detail_msg = data.get("detail") if isinstance(data.get("detail"), str) else None
            if detail_msg:
                return {"detail": detail_msg}

        return {}

    def get_supported_installers(self, os_name: str, use_test: bool = False) -> Dict[str, Any]:
        """GET {base}/installers/{os}/supported -> {"miners": [...], "supported_devices": [...]}
        
        Args:
            os_name: Operating system ('windows' or 'linux')
            use_test: If True, queries test-windows or test-linux for QA test versions
        """
        _logger.info(f"[get_supported_installers] Called with os_name='{os_name}', use_test={use_test}")
        os_norm = (os_name or "").strip().lower() or "windows"
        # Add test- prefix if test mode is enabled
        query_os = f"test-{os_norm}" if use_test else os_norm
        _logger.info(f"[get_supported_installers] Final query_os='{query_os}'")
        data = self._request("GET", f"/installers/{query_os}/supported")
        result: Dict[str, Any] = {"miners": [], "supported_devices": [], "miner_codes": []}
        if not isinstance(data, dict):
            return result

        miners = data.get("miners")
        if isinstance(miners, list):
            result["miners"] = [m for m in miners if isinstance(m, dict)]
        elif isinstance(miners, dict):
            result["miners"] = [miners]

        devices = data.get("supported_devices") or data.get("devices")
        if isinstance(devices, list):
            result["supported_devices"] = [d for d in devices if isinstance(d, dict)]
        elif isinstance(devices, dict):
            result["supported_devices"] = [devices]

        miner_codes = data.get("miner_codes")
        if isinstance(miner_codes, list):
            result["miner_codes"] = [str(code).strip() for code in miner_codes if code]

        return result

    def get_miner_profile(self, miner_key: str) -> Dict[str, Any]:
        """GET {base}/credentials/{miner_key} -> {"exists": bool, "registered_mac": str|None, "hex_id": str|None}

        NOTE: endpoint moved from /miners/... to /credentials/...
        """
        data = self._request("GET", f"/credentials/{miner_key}")
        if isinstance(data, dict):
            return data
        return {}

    def upsert_installation(self, miner_key: str, install_id: str, payload: Dict[str, Any]) -> None:
        """POST {base}/installations/{miner_key}/installations/{install_id} with heartbeat payload.

        NOTE: endpoint moved under /installations/...
        """
        body = dict(payload)
        body.setdefault("install_id", install_id)
        self._request("POST", f"/installations/{miner_key}/installations/{install_id}", json_body=body)

    def acquire_installation_lease(self, miner_key: str, install_id: str, lease_seconds: int, external_ip: Optional[str] = None) -> Dict[str, Any]:
        """POST {base}/installations/{miner_key}/leases/{install_id} -> {"granted": bool, "expires_at": "...", "error_code": str|None}

        NOTE: endpoint moved under /installations/...

        Args:
            miner_key: The miner key (e.g., BM-ABC...)
            install_id: Installation UUID
            lease_seconds: Lease duration in seconds
            external_ip: Optional external IP address (required for BM to enforce one-per-IP rule)

        Returns:
            Dict with:
                - granted (bool): True if lease was granted
                - error_code (str|None): Error code if lease denied (e.g., "IP_ALREADY_REGISTERED")
                - expires_at (str|None): ISO timestamp when lease expires
        """
        body = {"mode": "acquire", "lease_seconds": int(lease_seconds)}
        if external_ip:
            body["external_ip"] = external_ip

        data = self._request(
            "POST",
            f"/installations/{miner_key}/leases/{install_id}",
            json_body=body,
        )
        if isinstance(data, dict):
            return {
                "granted": bool(data.get("granted")),
                "error_code": data.get("error_code"),
                "expires_at": data.get("expires_at")
            }
        return {"granted": False, "error_code": None, "expires_at": None}

    def renew_installation_lease(self, miner_key: str, install_id: str, lease_seconds: int, external_ip: Optional[str] = None) -> bool:
        """PATCH {base}/installations/{miner_key}/leases/{install_id} -> {"granted": bool}

        NOTE: endpoint moved under /installations/...

        Args:
            miner_key: The miner key
            install_id: Installation UUID
            lease_seconds: Lease duration in seconds
            external_ip: Optional external IP (for BM, triggers IP update if changed)

        Returns:
            bool: True if renewal was granted
        """
        body = {"mode": "renew", "lease_seconds": int(lease_seconds)}
        if external_ip:
            body["external_ip"] = external_ip

        data = self._request(
            "PATCH",
            f"/installations/{miner_key}/leases/{install_id}",
            json_body=body,
        )
        if isinstance(data, dict):
            return bool(data.get("granted", True))
        return True

    def lease_status(self, miner_key: str) -> Dict[str, Any]:
        """GET {base}/installations/{miner_key}/leases/current -> {"active": bool, "holder_install_id": str|None}

        NOTE: endpoint moved under /installations/...
        """
        data = self._request("GET", f"/installations/{miner_key}/leases/current")
        if isinstance(data, dict):
            return data
        return {}

    def delete_installation(self, miner_key: str, install_id: str) -> bool:
        """DELETE {base}/installations/{miner_key}/installations/{install_id} -> {"ok": true}

        Removes the installation record from the database. Returns True if successful.
        If the installation record is not found, returns False.
        """
        try:
            data = self._request("DELETE", f"/installations/{miner_key}/installations/{install_id}")
            if isinstance(data, dict):
                return bool(data.get("ok", False))
            return False
        except ApiError as e:
            # If the error message indicates "not found", return False instead of raising
            if "not found" in str(e).lower():
                return False
            raise

    def lease_history(self, miner_key: str) -> List[Dict[str, Any]]:
        # NOTE: lease history endpoint was removed from the API. This method
        # is intentionally deprecated and no longer implemented.
        raise ApiError("lease_history endpoint removed from API")

    def get_hardware_doc(self, miner_key: str) -> Dict[str, Any]:
        """GET {base}/PoC/{miner_key}/hardware -> {"document": {...}}

        NOTE: hardware/PoC endpoints moved under /PoC/...
        """
        data = self._request("GET", f"/PoC/{miner_key}/hardware")
        if isinstance(data, dict):
            if isinstance(data.get("document"), dict):
                return data["document"]
            return data
        return {}

    def put_hardware_doc(self, miner_key: str, document: Dict[str, Any]) -> None:
        """PUT {base}/PoC/{miner_key}/hardware with {"document": {...}}

        NOTE: hardware/PoC endpoints moved under /PoC/...
        """
        payload = {"document": document}
        self._request("PUT", f"/PoC/{miner_key}/hardware", json_body=payload)

    def has_other_active_installation(self, miner_key: str, install_id: str) -> bool:
        status = self.lease_status(miner_key)
        if not isinstance(status, dict):
            return False
        if not status.get("active"):
            return False
        holder = status.get("holder_install_id")
        if not isinstance(holder, str) or not holder.strip():
            return False
        return holder.strip() != install_id

    def check_ip_status(self, external_ip: str) -> Dict[str, Any]:
        """GET {base}/installations/ip/{external_ip}/status -> detailed IP usage by miner type

        Check how many installations of each miner type are using this external IP.
        Used for enforcing per-miner-type IP limits.

        Args:
            external_ip: The external IP address to check

        Returns:
            Dict with:
                - external_ip (str): The IP address checked
                - installations_by_type (dict): Grouped by miner code (BM, HG, etc.)
                    Each type has:
                        - count (int): Number of active installations
                        - limit (int|str): Limit for this type (number or "no")
                        - details (list): List of {miner_key, install_id} dicts
        """
        data = self._request("GET", f"/installations/ip/{external_ip}/status")
        if isinstance(data, dict):
            return {
                "external_ip": data.get("external_ip", external_ip),
                "installations_by_type": data.get("installations_by_type", {})
            }
        return {"external_ip": external_ip, "installations_by_type": {}}


# ============================================================================
# Configuration Management & Factory Functions
# ============================================================================

def _get_1password_secret(reference: str) -> Optional[str]:
    """Retrieve a secret from 1Password using the CLI.
    
    Args:
        reference: 1Password reference like "op://VPS/Hardware_API/API_BEARER_TOKEN"
        
    Returns:
        The secret value, or None if retrieval fails
    """
    try:
        # Run 'op read' command to get the secret
        result = subprocess.run(
            ['op', 'read', reference],
            capture_output=True,
            text=True,
            timeout=10,
            check=True
        )
        
        # Strip whitespace and return the value
        value = result.stdout.strip()
        return value if value else None
        
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, FileNotFoundError):
        # 1Password CLI not available, not authenticated, or reference not found
        return None
    except Exception:
        # Any other error
        return None


def _load_build_config() -> Dict[str, Any]:
    """Load build configuration from embedded file or defaults."""
    import os
    debug = os.getenv('DEBUG_BUILD_CONFIG') == '1'
    
    try:
        # Try to find build config file (embedded during build)
        if getattr(sys, 'frozen', False):
            # Running from PyInstaller bundle
            bundle_dir = Path(getattr(sys, '_MEIPASS', '.'))
            config_file = bundle_dir / 'build_config.json'
            if debug:
                print(f"[DEBUG] Running from frozen bundle, looking for: {config_file}")
        else:
            # Running from source
            config_file = Path(__file__).parent / 'build_config.json'
            if debug:
                print(f"[DEBUG] Running from source, looking for: {config_file}")
        
        if config_file.exists():
            if debug:
                print(f"[DEBUG] Config file found, loading...")
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                
                # IMPORTANT: For packaged builds, bearer_token MUST be embedded in build_config.json
                # We no longer support 1Password fallback at runtime for security and portability
                if 'external_api' in config and 'bearer_token' in config['external_api']:
                    if debug:
                        token_preview = config['external_api']['bearer_token'][:20] + '...' if len(config['external_api']['bearer_token']) > 20 else config['external_api']['bearer_token']
                        print(f"[DEBUG] Bearer token found: {token_preview}")
                    return config
                elif 'external_api' in config:
                    if debug:
                        print(f"[DEBUG] Config exists but no bearer token in external_api")
                    # Config exists but no bearer token - fall back to 1Password for development
                    if not getattr(sys, 'frozen', False):
                        bearer_token = _get_1password_secret('op://VPS/Hardware_API/API_BEARER_TOKEN')
                        if bearer_token:
                            config['external_api']['bearer_token'] = bearer_token
                            return config
        else:
            if debug:
                print(f"[DEBUG] Config file NOT found at: {config_file}")
        
    except Exception as e:
        if debug:
            print(f"[DEBUG] Exception loading config: {e}")
        pass  # Silently fall through to fallbacks
    
    # For development (running from source), allow 1Password fallback
    if not getattr(sys, 'frozen', False):
        bearer_token = _get_1password_secret('op://VPS/Hardware_API/API_BEARER_TOKEN')
        if bearer_token:
            api_url = os.getenv('EXTERNAL_API_BASE_URL', 'https://hardwareapi.frynetworks.com')
            if _logger:
                _logger.info(f"[DEV 1PASSWORD] EXTERNAL_API_BASE_URL env var: {os.getenv('EXTERNAL_API_BASE_URL')}")
                _logger.info(f"[DEV 1PASSWORD] Using API URL: {api_url}")
            return {
                'external_api': {
                    'base_url': api_url,
                    'bearer_token': bearer_token,
                    'timeout': 10.0
                },
                'status': 'development',
                'source': '1password'
            }
    
    # Final fallback - return config without bearer token (will fail on API calls)
    # This is acceptable for packaged builds that should have embedded token
    api_url = os.getenv('EXTERNAL_API_BASE_URL', 'https://hardwareapi.frynetworks.com')
    _logger.info(f"[FALLBACK] EXTERNAL_API_BASE_URL env var: {os.getenv('EXTERNAL_API_BASE_URL')}")
    _logger.info(f"[FALLBACK] Using API URL: {api_url}")
    return {
        'external_api': {
            'base_url': api_url,
            'timeout': 10.0
        },
        'status': 'fallback',
        'source': 'defaults'
    }


# Load configuration once at module import
_BUILD_CONFIG = _load_build_config()


def get_external_api_client(base_url: Optional[str] = None, token: Optional[str] = None, 
                          timeout: float = 10.0, use_optimized: bool = True) -> Union[Any, ExternalApiClient]:
    """Get an API client instance (optimized or basic).
    
    Args:
        base_url: API base URL (uses build config if None)
        token: Optional auth token (uses build config bearer token if None)
        timeout: Request timeout in seconds
        use_optimized: If True, returns OptimizedExternalApiClient (requires miner_GUI)
        
    Returns:
        OptimizedExternalApiClient if use_optimized=True, else ExternalApiClient
    """
    _logger.info(f"[get_external_api_client CALLED] base_url={base_url}, use_optimized={use_optimized}")
    
    # Use build configuration if not provided
    if base_url is None:
        base_url = _BUILD_CONFIG['external_api']['base_url']
        _logger.info(f"[get_external_api_client] Using base_url from BUILD_CONFIG: {base_url}")
    
    if token is None:
        token = _BUILD_CONFIG['external_api'].get('bearer_token')
    
    if timeout == 10.0:  # Default timeout
        timeout = _BUILD_CONFIG['external_api'].get('timeout', 10.0)
    
    # Ensure base_url is not None
    if not base_url:
        # Use environment variable or fallback to canonical API base URL
        base_url = os.getenv('EXTERNAL_API_BASE_URL', 'https://hardwareapi.frynetworks.com')
        _logger.info(f"[CLIENT INIT] EXTERNAL_API_BASE_URL env var: {os.getenv('EXTERNAL_API_BASE_URL')}")
        _logger.info(f"[CLIENT INIT] Using API URL: {base_url}")
    
    if use_optimized:
        try:
            import importlib, importlib.util
            spec = importlib.util.find_spec('miner_GUI.utils.optimized_api')
            if spec is not None:
                mod = importlib.import_module('miner_GUI.utils.optimized_api')
                OptimizedExternalApiClient = getattr(mod, 'OptimizedExternalApiClient', None)
                if OptimizedExternalApiClient:
                    if _logger:
                        _logger.info(f"[get_external_api_client] Returning OptimizedExternalApiClient with URL: {base_url}")
                    return OptimizedExternalApiClient(base_url, token=token, timeout=timeout, client_class=ExternalApiClient)
        except Exception:
            # Fall back to basic client if optimized version not available or import fails
            pass
    
    _logger.info(f"[get_external_api_client] Returning basic ExternalApiClient with URL: {base_url}")
    return ExternalApiClient(base_url, token=token, timeout=timeout)


# Global optimized client instance
_global_client: Optional[Any] = None


def get_global_api_client() -> Union[Any, ExternalApiClient]:
    """Get the global optimized API client instance (singleton pattern).
    
    Returns:
        OptimizedExternalApiClient singleton, or basic client as fallback
    """
    global _global_client
    
    if _global_client is None:
        base_url = _BUILD_CONFIG['external_api']['base_url']
        bearer_token = _BUILD_CONFIG['external_api'].get('bearer_token')
        timeout = _BUILD_CONFIG['external_api'].get('timeout', 10.0)
        
        try:
            import importlib, importlib.util
            spec = importlib.util.find_spec('miner_GUI.utils.optimized_api')
            if spec is not None:
                mod = importlib.import_module('miner_GUI.utils.optimized_api')
                OptimizedExternalApiClient = getattr(mod, 'OptimizedExternalApiClient', None)
                if OptimizedExternalApiClient:
                    _global_client = OptimizedExternalApiClient(base_url, token=bearer_token, timeout=timeout, client_class=ExternalApiClient)
                else:
                    return ExternalApiClient(base_url, token=bearer_token, timeout=timeout)
            else:
                return ExternalApiClient(base_url, token=bearer_token, timeout=timeout)
        except Exception:
            # Return basic client if optimized not available or import fails
            return ExternalApiClient(base_url, token=bearer_token, timeout=timeout)
    
    return _global_client


def reset_global_api_client():
    """Reset the global API client (useful for testing or configuration changes)."""
    global _global_client
    _global_client = None


def get_api_client():
    """Convenience function that matches the existing interface."""
    return get_global_api_client()


def get_build_config_info() -> Dict[str, Any]:
    """Get information about the current build configuration."""
    return {
        'source': _BUILD_CONFIG.get('source', 'unknown'),
        'status': _BUILD_CONFIG.get('status', 'unknown'),
        'api_url_configured': bool(_BUILD_CONFIG.get('external_api', {}).get('base_url')),
        'bearer_token_configured': bool(_BUILD_CONFIG.get('external_api', {}).get('bearer_token')),
        'timeout': _BUILD_CONFIG.get('external_api', {}).get('timeout', 10.0)
    }


# ============================================================================
# Safety & Validation Helpers
# ============================================================================

REQUIRED_API_METHODS = [
    'get_required_version',
    'get_miner_profile',
    'upsert_installation',
    'acquire_installation_lease',
    'renew_installation_lease',
    'lease_status',
    'delete_installation',
    'get_hardware_doc',
    'put_hardware_doc',
]


def factory_has_all_endpoints(use_optimized: bool = True, base_url: Optional[str] = None, 
                             token: Optional[str] = None, timeout: float = 10.0) -> bool:
    """Return True if the client has all required API methods.

    This performs a shallow, non-network check: it simply verifies the methods exist and are callable.
    It does not make real HTTP calls.
    
    Args:
        use_optimized: Whether to check optimized client
        base_url: API base URL
        token: Optional auth token
        timeout: Request timeout
        
    Returns:
        True if all required methods are present and callable
    """
    try:
        client = get_external_api_client(base_url=base_url, token=token, timeout=timeout, use_optimized=use_optimized)
    except Exception:
        return False

    for name in REQUIRED_API_METHODS:
        if not hasattr(client, name) or not callable(getattr(client, name)):
            return False
    return True


def get_external_api_client_if_complete(*, base_url: Optional[str] = None, token: Optional[str] = None,
                                       timeout: float = 10.0, use_optimized: bool = True, 
                                       raise_on_missing: bool = True):
    """Return a client only if it exposes the complete set of required endpoints.

    If the client is missing methods, this returns None (or raises RuntimeError when raise_on_missing=True).
    
    Args:
        base_url: API base URL
        token: Optional auth token
        timeout: Request timeout
        use_optimized: Whether to use optimized client
        raise_on_missing: If True, raise error on missing methods; if False, return None
        
    Returns:
        API client instance if complete, None if incomplete (when raise_on_missing=False)
        
    Raises:
        RuntimeError: If client is missing methods and raise_on_missing=True
    """
    ok = factory_has_all_endpoints(use_optimized=use_optimized, base_url=base_url, token=token, timeout=timeout)
    if not ok:
        msg = "API client is missing required endpoints"
        if raise_on_missing:
            raise RuntimeError(msg)
        return None
    return get_external_api_client(base_url=base_url, token=token, timeout=timeout, use_optimized=use_optimized)
