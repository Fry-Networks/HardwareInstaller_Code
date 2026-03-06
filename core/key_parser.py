"""
Automatic miner key parsing and validation.

This module handles:
- Miner type detection from key format 
- Key format validation
- Miner information lookup
"""

import re
from typing import Dict, Any, Optional


class MinerKeyParser:
    """Automatic miner type detection from key format."""
    
    # Standard miner codes and their display names (updated naming from plan)
    MINER_TYPES = {
        "BM": {"name": "Bandwidth Miner", "group": "BM", "exclusive": None},
        "IDM": {"name": "Indoor Decibel Miner", "group": "Decibel", "exclusive": "ODM"},
        "ODM": {"name": "Outdoor Decibel Miner", "group": "Decibel", "exclusive": "IDM"},
        "ISM": {"name": "Indoor Satellite Miner", "group": "Satellite", "exclusive": "OSM"},
        "OSM": {"name": "Outdoor Satellite Miner", "group": "Satellite", "exclusive": "ISM"},
        "RDN": {"name": "Rewards Decentralization Node", "group": "RDN", "exclusive": None},
        "SVN": {"name": "Storage Validator Node", "group": "SVN", "exclusive": None},
        "SDN": {"name": "Storage Decentralization Node", "group": "SDN", "exclusive": None},
        "AEM": {"name": "AI Edge Miner", "group": "AEM", "exclusive": None},
        "IRM": {"name": "Indoor Radiation Miner", "group": "Radiation", "exclusive": None}
    }
    
    def __init__(self):
        """Initialize the parser."""
        self._patterns = {}
        for code in self.MINER_TYPES:
            self._patterns[code] = re.compile(rf"^{code}-[A-Z0-9]{{32}}$")
    
    def parse_miner_key(self, key: str) -> Dict[str, Any]:
        """
        Extract miner type and validate format.
        
        Args:
            key: The miner key to parse
            
        Returns:
            Dictionary with validation results and miner information
        """
        if not key or not isinstance(key, str):
            return {"valid": False, "error": "Key is required"}
        
        key = key.strip().upper()
        
        if len(key) < 35:
            return {"valid": False, "error": "Key too short"}
        
        # Extract miner code (everything before first hyphen)
        if '-' not in key:
            return {"valid": False, "error": "Invalid key format - missing hyphen"}
        
        miner_code = key.split('-')[0].upper()
        
        if miner_code not in self.MINER_TYPES:
            return {
                "valid": False, 
                "error": f"Unknown miner type: {miner_code}",
                "available_types": list(self.MINER_TYPES.keys())
            }
        
        # Validate full format using pre-compiled pattern
        if not self._patterns[miner_code].match(key):
            return {
                "valid": False, 
                "error": "Invalid key format - must be {CODE}-{32 alphanumeric chars}",
                "expected_pattern": f"{miner_code}-[A-Z0-9]{{32}}"
            }
        
        # Return successful validation with miner info
        miner_info = self.MINER_TYPES[miner_code].copy()
        miner_info.update({
            "valid": True,
            "code": miner_code,
            "key": key,
            "pattern": self._patterns[miner_code].pattern
        })
        
        return miner_info
    
    def get_miner_types(self) -> Dict[str, Dict[str, Any]]:
        """Get all available miner types."""
        return self.MINER_TYPES.copy()
    
    def is_exclusive_pair(self, code1: str, code2: str) -> bool:
        """Check if two miner codes are mutually exclusive."""
        if code1 not in self.MINER_TYPES or code2 not in self.MINER_TYPES:
            return False
        
        exclusive1 = self.MINER_TYPES[code1].get("exclusive")
        exclusive2 = self.MINER_TYPES[code2].get("exclusive")
        
        return exclusive1 == code2 or exclusive2 == code1
    
    def validate_key_format_only(self, key: str) -> bool:
        """
        Quick validation of key format without full parsing.
        
        Args:
            key: The key to validate
            
        Returns:
            True if format is valid, False otherwise
        """
        if not key or not isinstance(key, str):
            return False
        
        key = key.strip().upper()
        
        # Basic format check
        if len(key) != 35 or '-' not in key:
            return False
        
        miner_code = key.split('-')[0]
        if miner_code not in self.MINER_TYPES:
            return False
        
        return bool(self._patterns[miner_code].match(key))


def validate_miner_key(key: str) -> Dict[str, Any]:
    """
    Convenience function for key validation.
    
    Args:
        key: The miner key to validate
        
    Returns:
        Validation results dictionary
    """
    parser = MinerKeyParser()
    return parser.parse_miner_key(key)


def extract_miner_code(key: str) -> Optional[str]:
    """
    Extract just the miner code from a key.
    
    Args:
        key: The miner key
        
    Returns:
        Miner code or None if invalid
    """
    if not key or '-' not in key:
        return None
    
    code = key.split('-')[0].upper()
    parser = MinerKeyParser()
    
    return code if code in parser.MINER_TYPES else None
