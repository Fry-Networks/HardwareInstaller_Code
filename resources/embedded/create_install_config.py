#!/usr/bin/env python3
"""
Create encrypted installation configuration file.
This tool is used by the installer to create a secure, encrypted config file
containing the installation ID and lease metadata for a specific deployment.

The installer must acquire the global lease BEFORE creating this file.
"""

import os
import sys
import json
import argparse
import uuid
import platform
from pathlib import Path
from datetime import datetime, timezone

# Add tools directory to path
tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools')
if os.path.exists(tools_dir):
    sys.path.insert(0, tools_dir)

# Import encryption functions
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

def encrypt_install_config(config_data: dict) -> dict:
    """Encrypt installation configuration data using Fernet."""
    # Use a fixed salt for install config (same approach as miner_config)
    salt = b'install_config_salt_v1'
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    
    # Derive key from a known string (obfuscation, not cryptographic security)
    password = "install_config_encryption_key_v1".encode()
    key = base64.urlsafe_b64encode(kdf.derive(password))
    
    # Encrypt
    f = Fernet(key)
    config_json = json.dumps(config_data)
    encrypted_data = f.encrypt(config_json.encode())
    
    return {
        "data": encrypted_data.decode(),
        "version": "1.0"
    }

def decrypt_install_config(encrypted_data: dict) -> dict:
    """Decrypt installation configuration data."""
    # Use the same parameters as encryption
    salt = b'install_config_salt_v1'
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    
    password = "install_config_encryption_key_v1".encode()
    key = base64.urlsafe_b64encode(kdf.derive(password))
    
    # Decrypt
    f = Fernet(key)
    decrypted_data = f.decrypt(encrypted_data['data'].encode())
    config_data = json.loads(decrypted_data)
    
    return config_data

def create_install_config(
    install_id: str = None,
    output_path: str = None,
    installer_version: str = None,
    lease_acquired_at: str = None
) -> str:
    """Create encrypted installation configuration file.
    
    Args:
        install_id: UUID for this installation (generated if not provided)
        output_path: Output file path (default: install_config.enc)
        installer_version: Version of the installer that created this (optional)
        lease_acquired_at: ISO timestamp when lease was acquired (optional)
    """
    
    # Generate install_id if not provided
    if not install_id:
        install_id = str(uuid.uuid4())
    
    # Validate install_id format (must be valid UUID)
    try:
        uuid.UUID(install_id)
    except ValueError:
        raise ValueError(f"Invalid install_id format: {install_id}. Must be a valid UUID.")
    
    # Get current timestamp if lease_acquired_at not provided
    if not lease_acquired_at:
        lease_acquired_at = datetime.now(timezone.utc).isoformat()
    
    # Create config data
    config_data = {
        "install_id": install_id,
        "lease_acquired_at": lease_acquired_at,
        "hostname": platform.node(),
        "os": platform.platform(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config_version": "1.0"
    }
    
    if installer_version:
        config_data["installer_version"] = installer_version
    
    # Determine output path
    if output_path is None:
        output_path = "install_config.enc"
    
    # Ensure output directory exists
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    
    # Encrypt the config
    encrypted_data = encrypt_install_config(config_data)
    
    # Save encrypted config
    with open(output_path, 'w') as f:
        json.dump(encrypted_data, f)
    
    return output_path

def read_install_config(config_path: str) -> dict:
    """Read and decrypt installation configuration file."""
    try:
        with open(config_path, 'r') as f:
            encrypted_data = json.load(f)
        
        # Decrypt the config
        config_data = decrypt_install_config(encrypted_data)
        
        return config_data
    
    except FileNotFoundError:
        raise RuntimeError(f"Install config file not found: {config_path}")
    except Exception as e:
        raise RuntimeError(f"Failed to read install config: {e}")

def main():
    parser = argparse.ArgumentParser(
        description="Manage encrypted installation configuration for miner deployments"
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Create command
    create_parser = subparsers.add_parser('create', help='Create encrypted install config')
    create_parser.add_argument('--install-id', help='Installation UUID (generated if not provided)')
    create_parser.add_argument('--output', '-o', help='Output file path (default: install_config.enc)')
    create_parser.add_argument('--installer-version', help='Installer version string')
    create_parser.add_argument('--lease-acquired-at', help='ISO timestamp when lease was acquired')
    
    # Read command
    read_parser = subparsers.add_parser('read', help='Read encrypted install config')
    read_parser.add_argument('config_file', help='Path to encrypted config file')
    
    # Validate command
    validate_parser = subparsers.add_parser('validate', help='Validate install ID format')
    validate_parser.add_argument('install_id', help='Installation UUID to validate')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == 'create':
            output_path = create_install_config(
                install_id=args.install_id,
                output_path=args.output,
                installer_version=args.installer_version,
                lease_acquired_at=args.lease_acquired_at
            )
            config = read_install_config(output_path)
            print(f"✅ Created encrypted install config: {output_path}")
            print(f"📋 Install ID: {config['install_id']}")
            print(f"🖥️  Hostname: {config['hostname']}")
            print(f"⏰ Created: {config['created_at']}")
            if config.get('lease_acquired_at'):
                print(f"🔒 Lease Acquired: {config['lease_acquired_at']}")
            print(f"🔒 Config is encrypted and ready for deployment")
            
        elif args.command == 'read':
            config = read_install_config(args.config_file)
            print(f"📋 Install ID: {config['install_id']}")
            print(f"🖥️  Hostname: {config['hostname']}")
            print(f"💻 OS: {config['os']}")
            print(f"⏰ Created: {config['created_at']}")
            if config.get('lease_acquired_at'):
                print(f"🔒 Lease Acquired: {config['lease_acquired_at']}")
            if config.get('installer_version'):
                print(f"📦 Installer Version: {config['installer_version']}")
                
        elif args.command == 'validate':
            try:
                uuid.UUID(args.install_id)
                print(f"✅ Valid install ID (UUID): {args.install_id}")
            except ValueError:
                print(f"❌ Invalid install ID: {args.install_id}")
                print("Expected format: valid UUID (e.g., 550e8400-e29b-41d4-a716-446655440000)")
                sys.exit(1)
                
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
