#!/usr/bin/env python3
"""Manage encrypted Bright SDK credentials (Windows only)."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except ImportError as exc:  # pragma: no cover
    raise SystemExit("cryptography package is required to manage Bright secrets") from exc


def _derive_key(salt: bytes, password: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def encrypt_bright_secret(secret: dict, salt: bytes, password: str) -> dict:
    key = _derive_key(salt, password)
    fernet = Fernet(key)
    payload = json.dumps(secret).encode("utf-8")
    encrypted = fernet.encrypt(payload)
    return {"data": encrypted.decode("utf-8"), "version": "1.0"}


def decrypt_bright_secret(blob: dict, salt: bytes, password: str) -> dict:
    key = _derive_key(salt, password)
    payload = blob.get("data")
    if not isinstance(payload, str):
        raise ValueError("Secret payload missing 'data' field")
    fernet = Fernet(key)
    decrypted = fernet.decrypt(payload.encode("utf-8"))
    return json.loads(decrypted.decode("utf-8"))


def create_bright_secret(app_id: str, salt: bytes, password: str, output: str | None) -> Path:
    if not app_id or not isinstance(app_id, str):
        raise ValueError("app_id must be a non-empty string")
    target = Path(output or "bright.enc").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = encrypt_bright_secret(
        {"app_id": app_id.strip(), "created_by": "installer", "config_version": "1.0"},
        salt,
        password,
    )
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def create_bright_consent_config(
    app_id: str,
    app_name: str,
    logo_link: str,
    output: str | None,
    benefit: str | None = None,
    agree_btn: str | None = None,
    disagree_btn: str | None = None,
    opt_out_txt: str | None = None,
) -> Path:
    """Create brd_config.json used by lum_sdk.dll / net_updater32.exe."""
    if not app_id:
        raise ValueError("app_id is required")
    if not app_name:
        raise ValueError("app_name is required")
    if not logo_link:
        raise ValueError("logo_link is required")

    target = Path(output or "brd_config.json").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "app_id": app_id.strip(),
        "app_name": app_name.strip(),
        "logo_link": logo_link.strip(),
    }
    if benefit:
        cfg["benefit"] = benefit
    if agree_btn:
        cfg["agree_btn"] = agree_btn
    if disagree_btn:
        cfg["disagree_btn"] = disagree_btn
    if opt_out_txt:
        cfg["opt_out_txt"] = opt_out_txt

    target.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage encrypted Bright SDK config")
    parser.add_argument("--salt", help="Encryption salt (required for create/read)")
    parser.add_argument("--password", help="Encryption password (required for create/read)")
    subparsers = parser.add_subparsers(dest="command")

    create_parser = subparsers.add_parser("create", help="Create encrypted bright.enc")
    create_parser.add_argument("app_id", help="Bright app id (provided by Bright SDK)")
    create_parser.add_argument("--output", "-o", help="Output file path (default: bright.enc)")

    config_parser = subparsers.add_parser("config", help="Create Bright consent brd_config.json")
    config_parser.add_argument("app_id", help="Bright app id (required)")
    config_parser.add_argument("app_name", help="Friendly app name for consent screen")
    config_parser.add_argument("logo_link", help="URL to app logo for consent screen")
    config_parser.add_argument("--output", "-o", help="Output file path (default: brd_config.json)")
    config_parser.add_argument("--benefit", help="Optional benefit text")
    config_parser.add_argument("--agree-btn", help="Optional consent agree button label")
    config_parser.add_argument("--disagree-btn", help="Optional consent disagree button label")
    config_parser.add_argument("--opt-out-txt", help="Optional opt-out instruction text")

    read_parser = subparsers.add_parser("read", help="Decrypt bright.enc for verification")
    read_parser.add_argument("secret_file", help="Path to bright.enc")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == "create" or args.command == "read":
        if not args.salt or not args.password:
            parser.error("--salt and --password are required for create/read commands")

    if args.command == "create":
        salt = args.salt.encode()
        target = create_bright_secret(args.app_id, salt, args.password, args.output)
        print(f"Created Bright secret at {target}")
    elif args.command == "config":
        target = create_bright_consent_config(
            app_id=args.app_id,
            app_name=args.app_name,
            logo_link=args.logo_link,
            output=args.output,
            benefit=args.benefit,
            agree_btn=args.agree_btn,
            disagree_btn=args.disagree_btn,
            opt_out_txt=args.opt_out_txt,
        )
        print(f"Created Bright consent config at {target}")
    elif args.command == "read":
        salt = args.salt.encode()
        path = Path(args.secret_file).resolve()
        if not path.exists():
            raise SystemExit(f"Secret file not found: {path}")
        blob = json.loads(path.read_text(encoding="utf-8"))
        data = decrypt_bright_secret(blob, salt, args.password)
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
