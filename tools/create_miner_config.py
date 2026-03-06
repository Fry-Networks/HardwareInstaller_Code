#!/usr/bin/env python3
"""
Create encrypted miner configuration file.
This tool is used by the installer to create a secure, encrypted config file
containing the miner key for a specific deployment.
"""

import os
import sys
import json
import re
import argparse
from pathlib import Path

# Add tools directory to path
tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools')
if os.path.exists(tools_dir):
    sys.path.insert(0, tools_dir)

# Import encryption functions
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

def validate_miner_key(miner_key: str) -> bool:
    """Validate miner key format: {MINER_CODE}-[A-Z0-9]{32}
    
    Supported miner codes:
    BM, IDM, ODM, ISM, OSM, RDN, SDN, SVN, AEM, IRM
    """
    valid_codes = {'BM', 'IDM', 'ODM', 'ISM', 'OSM', 'RDN', 'SDN', 'SVN', 'AEM', 'IRM'}
    
    # Check format: CODE-[A-Z0-9]{32}
    match = re.match(r'^([A-Z]{2,3})-[A-Z0-9]{32}$', miner_key)
    if not match:
        return False
    
    # Check if the miner code is valid
    miner_code = match.group(1)
    return miner_code in valid_codes

def encrypt_miner_config(config_data: dict) -> dict:
    """Encrypt miner configuration data using Fernet."""
    # Use a fixed salt for miner config (this is acceptable since it's just obfuscation)
    salt = b'miner_config_salt_v1'
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    
    # Derive key from a known string (this is obfuscation, not true security)
    password = "miner_config_encryption_key_v1".encode()
    key = base64.urlsafe_b64encode(kdf.derive(password))
    
    # Encrypt
    f = Fernet(key)
    config_json = json.dumps(config_data)
    encrypted_data = f.encrypt(config_json.encode())
    
    return {
        "data": encrypted_data.decode(),
        "version": "1.0"
    }

def create_miner_config(miner_key: str, output_path: str = None) -> str:
    """Create encrypted miner configuration file."""
    
    # Validate miner key
    if not validate_miner_key(miner_key):
        raise ValueError(f"Invalid miner key format: {miner_key}. Expected: {{MINER_CODE}}-[A-Z0-9]{{32}} where MINER_CODE is one of: BM, IDM, ODM, ISM, OSM, RDN, SDN, SVN, AEM, IRM")
    
    # Create config data
    config_data = {
        "miner_key": miner_key,
        "created_by": "installer",
        "config_version": "1.0"
    }
    
    # Determine output path
    if output_path is None:
        output_path = "miner_config.enc"
    
    # Ensure output directory exists
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    
    # Encrypt the config
    encrypted_data = encrypt_miner_config(config_data)
    
    # Save encrypted config
    with open(output_path, 'w') as f:
        json.dump(encrypted_data, f)
    
    return output_path

def decrypt_miner_config(encrypted_data: dict) -> dict:
    """Decrypt miner configuration data."""
    # Use the same parameters as encryption
    salt = b'miner_config_salt_v1'
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    
    password = "miner_config_encryption_key_v1".encode()
    key = base64.urlsafe_b64encode(kdf.derive(password))
    
    # Decrypt
    f = Fernet(key)
    decrypted_data = f.decrypt(encrypted_data['data'].encode())
    config_data = json.loads(decrypted_data)
    
    return config_data

def read_miner_config(config_path: str) -> str:
    """Read and decrypt miner configuration file."""
    try:
        with open(config_path, 'r') as f:
            encrypted_data = json.load(f)
        
        # Decrypt the config
        config_data = decrypt_miner_config(encrypted_data)
        
        return config_data.get('miner_key')
    
    except FileNotFoundError:
        raise RuntimeError(f"Miner config file not found: {config_path}")
    except Exception as e:
        raise RuntimeError(f"Failed to read miner config: {e}")

def main():
    parser = argparse.ArgumentParser(description="Manage encrypted miner configuration")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Create command
    create_parser = subparsers.add_parser('create', help='Create encrypted miner config')
    create_parser.add_argument('miner_key', help='Miner key ({MINER_CODE}-[A-Z0-9]{32})')
    create_parser.add_argument('--output', '-o', help='Output file path (default: miner_config.enc)')
    
    # Read command
    read_parser = subparsers.add_parser('read', help='Read encrypted miner config')
    read_parser.add_argument('config_file', help='Path to encrypted config file')
    
    # Validate command
    validate_parser = subparsers.add_parser('validate', help='Validate miner key format')
    validate_parser.add_argument('miner_key', help='Miner key to validate (format: {MINER_CODE}-[A-Z0-9]{32})')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == 'create':
            output_path = create_miner_config(args.miner_key, args.output)
            print(f"✅ Created encrypted miner config: {output_path}")
            print(f"📋 Miner Key: {args.miner_key}")
            print(f"🔒 Config is encrypted and ready for deployment")
            
        elif args.command == 'read':
            miner_key = read_miner_config(args.config_file)
            if miner_key:
                print(f"📋 Miner Key: {miner_key}")
            else:
                print("❌ No miner key found in config")
                
        elif args.command == 'validate':
            if validate_miner_key(args.miner_key):
                print(f"✅ Valid miner key: {args.miner_key}")
            else:
                print(f"❌ Invalid miner key: {args.miner_key}")
                print("Expected format: {MINER_CODE}-[A-Z0-9]{32}")
                print("Valid miner codes: BM, IDM, ODM, ISM, OSM, RDN, SDN, SVN, AEM, IRM")
                sys.exit(1)
                
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()