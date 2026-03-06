# Encrypted Measurements for Service Upload

## Overview

The GUI worker writes encrypted measurement data every 10 minutes to a shared location in ProgramData. The service reads, decrypts, and uploads this data via external API calls.

**Key Design**: The encryption key is derived from the miner_key (stored in `miner_config.enc`) using PBKDF2. No separate key file is required.

## Architecture

```
GUI Worker (worker.py)
  ↓ every 10 minutes
  ↓ derives Fernet key from miner_key
  ↓ writes encrypted JSON
%PROGRAMDATA%\FryNetworks\miner-{CODE}\measurements\measurements-{GROUP}-latest.json.enc
  ↑ reads & decrypts
  ↑ derives same Fernet key from miner_key
  ↑ uploads via API
Service (external)
```

## Encryption

- **Algorithm**: Fernet (symmetric encryption, AES-128-CBC + HMAC-SHA256)
- **Key Derivation**: Per-device unique key derived from miner_key using PBKDF2-HMAC-SHA256
  - Salt: `b'measurements_key_v1'`
  - Iterations: 100,000
  - Length: 32 bytes (base64 urlsafe-encoded)
- **Key Source**: The miner_key is read from `miner_config.enc` (same as GUI configuration)
- **No Separate Key File**: The installer no longer needs to provision a separate encryption key file
- **Key Sharing**: GUI and service derive the same Fernet key from the miner_key stored in `miner_config.enc`

## Responsibilities

### Installer

The installer must provision the following structure and files:

**1. Create Directory Structure**
```
%PROGRAMDATA%\FryNetworks\miner-{CODE}\
%PROGRAMDATA%\FryNetworks\miner-{CODE}\measurements\
```

Examples:
- `C:\ProgramData\FryNetworks\miner-BM\measurements\`
- `C:\ProgramData\FryNetworks\miner-IRM\measurements\`
- `C:\ProgramData\FryNetworks\miner-ISM\measurements\`
- `C:\ProgramData\FryNetworks\miner-DM\measurements\`
- `C:\ProgramData\FryNetworks\miner-SM\measurements\`

**2. Provision miner_config.enc**

The installer must create the encrypted `miner_config.enc` file containing the miner_key. This file is already used by the GUI for configuration and will now also provide the basis for measurement encryption.

Location: `%PROGRAMDATA%\FryNetworks\miner-{CODE}\miner_config.enc`

**3. Set Permissions**

Ensure appropriate ACLs:
- GUI application user: read/write access to `measurements\` directory, read access to `miner_config.enc`
- Service: read access to both `measurements\` directory and `miner_config.enc`
- Recommended: Restrict to SYSTEM + Administrators + specific service account

**What the Installer Does NOT Need:**
- ❌ No separate `miner_encryption.key` file
- ❌ No environment variables for key paths
- ❌ No key generation logic

### GUI (worker.py)

The GUI worker automatically handles encryption and writes:

**1. Key Derivation**

On each 10-minute interval:
- Reads `miner_key` from `miner_config.enc` (via `read_miner_key()`)
- Derives a Fernet key using PBKDF2-HMAC-SHA256 with salt `b'measurements_key_v1'`

**2. Measurement Collection & Encryption**

- Collects current sensor data (download, upload, radiation, satellites, etc.)
- Formats packet: `{timestamp, miner_key, group, measurement}`
- Encrypts the JSON packet using derived Fernet key
- Writes to: `%PROGRAMDATA%\FryNetworks\miner-{CODE}\measurements\measurements-{GROUP}-latest.json.enc`

**3. Atomic Write**

- Uses temp file + rename for atomicity
- Overwrites previous measurement (latest only)
- Silent fail if `miner_config.enc` missing or decryption fails

**4. Error Handling**

- If `miner_key` unavailable: skip encryption, no crash
- If cryptography library missing: skip encryption, no crash
- All errors logged via `log_step()` for diagnostics

### Service (external)

The service is responsible for reading, decrypting, and uploading measurements:

**1. Key Derivation**

The service must implement the same key derivation logic:

```python
import base64
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.fernet import Fernet

def derive_measurement_key(miner_key: str) -> bytes:
    """Derive Fernet key from miner_key (same logic as GUI)."""
    salt = b'measurements_key_v1'
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    derived = kdf.derive(miner_key.encode('utf-8'))
    return base64.urlsafe_b64encode(derived)
```

**2. Read miner_key**

The service must read the miner_key from `miner_config.enc` using the existing decryption logic from `miner_GUI.utils.data.decrypt_miner_config()` or re-implement it.

**3. Read & Decrypt Measurements**

```python
from pathlib import Path
import json
from cryptography.fernet import Fernet

def read_measurement(miner_code: str, group: str) -> dict:
    """Read and decrypt measurement file."""
    # Derive key
    miner_key = get_miner_key_from_config(miner_code)  # Implement this
    fernet_key = derive_measurement_key(miner_key)
    
    # Read encrypted file
    measurements_dir = Path(os.environ.get('PROGRAMDATA', r'C:\ProgramData')) / \
                       'FryNetworks' / f'miner-{miner_code}' / 'measurements'
    file_path = measurements_dir / f'measurements-{group}-latest.json.enc'
    
    if not file_path.exists():
        return None
    
    with open(file_path, 'rb') as f:
        encrypted = f.read()
    
    # Decrypt
    fernet = Fernet(fernet_key)
    decrypted = fernet.decrypt(encrypted)
    return json.loads(decrypted)
```

**4. Upload to Backend**

Poll measurement files periodically (e.g., every 10 minutes) and upload via HTTPS POST to your backend API.

**5. Error Handling**

- If `miner_config.enc` missing: log error, skip upload
- If measurement file missing: wait for next interval
- If decryption fails: log error, skip (corrupted file or key mismatch)
- Retry failed uploads on next interval

## File Format

### Encrypted File Path
```
%PROGRAMDATA%\FryNetworks\miner-{CODE}\measurements\measurements-{GROUP}-latest.json.enc
```

Examples:
```
C:\ProgramData\FryNetworks\miner-BM\measurements\measurements-Bandwidth-latest.json.enc
C:\ProgramData\FryNetworks\miner-IRM\measurements\measurements-Radiation-latest.json.enc
C:\ProgramData\FryNetworks\miner-ISM\measurements\measurements-Satellite-latest.json.enc
```

Where `{GROUP}` is one of:
- `Bandwidth`
- `AIEdge`
- `Radiation`
- `Satellite`
- `Decibel`

### Decrypted JSON Structure

```json
{
  "timestamp": "2025-11-07T14:32:05.123456",
  "miner_key": "BM-ABC123...",
  "group": "Bandwidth",
  "measurement": {
    "dl": 125.42,
    "ul": 23.15,
    "iface": "Ethernet"
  }
}
```

### Measurement Fields by Group

**Bandwidth / AIEdge**
```json
{
  "dl": 125.42,        // Download Mbps
  "ul": 23.15,         // Upload Mbps
  "iface": "Ethernet"  // Interface name
}
```

**Radiation**
```json
{
  "cpm": 28.0,         // Counts per minute
  "usv": 0.182,        // Microsieverts
  "usv_hour": 0.182,   // μSv/h
  "mr": 0.0182,        // Milliroentgen
  "cps": 1.2           // Counts per second (optional)
}
```

**Satellite**
```json
{
  "sats": 8,           // Satellites in view
  "fix": "GPS",        // Fix type: NONE, GPS, DGPS, etc.
  "lat": 37.7749,      // Latitude (optional)
  "lon": -122.4194,    // Longitude (optional)
  "alt": 45.2,         // Altitude meters (optional)
  "hdop": 1.2          // Horizontal dilution (optional)
}
```

**Decibel**
```json
{
  "dbfs": -42.5        // Decibels full scale
}
```

## Service Integration

### Option 1: Use GUI Helper (Recommended)

If the service can import from the GUI codebase:

```python
from miner_GUI.utils.encryption import read_encrypted_measurement

# Read latest Bandwidth measurement
data = read_encrypted_measurement("Bandwidth")

if data:
    print(f"Timestamp: {data['timestamp']}")
    print(f"Miner Key: {data['miner_key']}")
    print(f"Measurement: {data['measurement']}")
else:
    print("No data available or decryption failed")
```

### Option 2: Standalone Service Implementation

If the service is standalone, implement key derivation and decryption:

```python
import os
import json
import base64
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

def derive_measurement_key(miner_key: str) -> bytes:
    """Derive Fernet key from miner_key (matches GUI logic)."""
    salt = b'measurements_key_v1'
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    derived = kdf.derive(miner_key.encode('utf-8'))
    return base64.urlsafe_b64encode(derived)

def read_miner_key_from_config(miner_code: str) -> str:
    """
    Read miner_key from miner_config.enc.
    
    Implement decryption logic from miner_GUI.utils.data.decrypt_miner_config()
    or reuse that module if available.
    """
    # TODO: Implement config decryption (see miner_GUI/utils/data.py)
    pass

def read_measurement(miner_code: str, group: str) -> dict:
    """Read and decrypt a measurement file."""
    try:
        # Get miner key
        miner_key = read_miner_key_from_config(miner_code)
        if not miner_key:
            return None
        
        # Derive encryption key
        fernet_key = derive_measurement_key(miner_key)
        
        # Build file path
        data_dir = Path(os.environ.get('PROGRAMDATA', r'C:\ProgramData')) / \
                   'FryNetworks' / f'miner-{miner_code}' / 'measurements'
        file_path = data_dir / f'measurements-{group}-latest.json.enc'
        
        if not file_path.exists():
            return None
        
        # Read and decrypt
        with open(file_path, 'rb') as f:
            encrypted = f.read()
        
        fernet = Fernet(fernet_key)
        decrypted = fernet.decrypt(encrypted)
        return json.loads(decrypted)
    except Exception as e:
        print(f"Error reading measurement: {e}")
        return None
```

### Example Service Upload Flow

```python
import time
from miner_GUI.utils.encryption import read_encrypted_measurement
import requests

def upload_measurements():
    """Service worker to upload encrypted measurements."""
    groups = ["Bandwidth", "AIEdge", "Radiation", "Satellite", "Decibel"]
    
    for group in groups:
        try:
            # Read encrypted measurement
            data = read_encrypted_measurement(group)
            if not data:
                continue
            
            # Upload to API
            response = requests.post(
                "https://api.frynetworks.com/v1/measurements",
                json=data,
                headers={"Authorization": f"Bearer {API_TOKEN}"},
                timeout=10
            )
            
            if response.status_code == 200:
                print(f"✓ Uploaded {group} measurement")
            else:
                print(f"✗ Upload failed for {group}: {response.status_code}")
                
        except Exception as e:
            print(f"✗ Error uploading {group}: {e}")

# Run every 10 minutes
while True:
    upload_measurements()
    time.sleep(600)  # 10 minutes
```

## Timing

- **GUI Write Interval**: Every 10 minutes (600 seconds)
- **Service Read Interval**: Configurable (recommended: every 10–15 minutes)
- **Atomicity**: Temp file + atomic rename
- **Overwrite Behavior**: Latest measurement overwrites previous file (service should upload promptly)

## Error Handling

### GUI Worker
- Silent fail if encryption unavailable
- Silent fail if miner_key not found
- Silent fail if write fails
- Service retry handles missing/stale data

### Service
- Skip if file doesn't exist (wait for next write)
- Skip if decryption fails (corrupted key or file)
- Retry failed uploads on next interval
- Log errors for monitoring

## Security Considerations

1. **Key Derivation**: Each device has a unique encryption key derived from its miner_key
2. **Key Protection**: The `miner_config.enc` file should have restricted ACLs (Administrators + SYSTEM + service account)
3. **Transit Security**: Service should use HTTPS + server-side validation when uploading
4. **No Global Key**: Unlike the config file encryption, measurements use per-device keys (derived from miner_key)
5. **Fail-Safe**: If miner_key missing or unreadable, GUI silently skips encrypted writes

## Testing

### Validate Key Derivation (Development)

```python
from miner_GUI.utils.encryption import get_encryption_key
from miner_GUI.utils.data import read_miner_key

# Check if key can be derived
miner_key = read_miner_key()
print(f"Miner Key: {miner_key[:15]}...")

fernet_key = get_encryption_key()
if fernet_key:
    print(f"✓ Fernet key derived successfully: {fernet_key[:20]}...")
else:
    print("✗ Failed to derive Fernet key")
```

### Validate Encryption (Development)

```python
from miner_GUI.utils.encryption import encrypt_measurement_data, decrypt_measurement_data

test_data = {
    "timestamp": "2025-11-10T14:00:00",
    "miner_key": "BM-TEST-KEY",
    "group": "Bandwidth",
    "measurement": {"dl": 125.42, "ul": 23.15, "iface": "Ethernet"}
}

encrypted = encrypt_measurement_data(test_data)
if encrypted:
    print(f"✓ Encrypted: {encrypted[:50]}...")
    
    decrypted = decrypt_measurement_data(encrypted)
    if decrypted == test_data:
        print("✓ Encryption round-trip test passed")
    else:
        print("✗ Decryption mismatch")
else:
    print("✗ Encryption failed")
```

### Check File Output

```powershell
# List measurement files for Bandwidth Miner
Get-ChildItem "$env:PROGRAMDATA\FryNetworks\miner-BM\measurements"

# Check miner_config.enc
Get-ChildItem "$env:PROGRAMDATA\FryNetworks\miner-BM\miner_config.enc"
```

## Dependencies

- `cryptography>=41.0.0` (already in requirements.txt)
- Shared ProgramData access between GUI and service

## Future Enhancements

- [ ] Key rotation support (coordinated miner_key updates)
- [ ] Measurement queuing (retain last N measurements for resilience)
- [ ] Compression for large payloads
- [ ] Batch uploads for efficiency
- [ ] Integrity hash (SHA-256) alongside ciphertext
- [ ] Configurable upload intervals
