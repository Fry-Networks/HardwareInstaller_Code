import json
import sys
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def derive_key(salt: bytes, password: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password))


def main():
    if len(sys.argv) != 3:
        print("Usage: encrypt_sdk_config.py <salt> <password>")
        sys.exit(1)

    salt = sys.argv[1].encode()
    password = sys.argv[2].encode()

    payload = json.load(open("sdk_approvals.json", "r", encoding="utf-8"))
    key = derive_key(salt, password)
    token = Fernet(key).encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    json.dump({"data": token.decode("utf-8")}, open("sdk_config.enc", "w", encoding="utf-8"))


if __name__ == "__main__":
    main()
