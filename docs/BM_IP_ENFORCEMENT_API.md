# IP Enforcement Per Miner Type: Backend API Contract

## Overview

Each miner type can have a configurable limit on the number of installations allowed per external IP address. The installer detects the external IP and sends it to the backend during lease operations. The backend is responsible for persisting the IP and enforcing the per-miner-type limits.

Limits are configured per miner type in the version metadata and can be:
- A number (1, 2, 5, etc.) for a specific limit
- `"no"` for no limit (unlimited installations allowed)

---

## Endpoints

### 1. Get Miner Version & IP Limit — `GET /versions/{miner_code}`

**No request body.**

**Backend behavior:**

- Return version metadata for the specified miner type, including the IP enforcement limit

**Response:**

```json
{
  "miner_code": "BM",
  "version": "3.10.3",
  "limit": 1,
  "download_url": "https://...",
  "checksum": "..."
}
```

**Limit values:**
- `1`, `2`, `5`, etc. — numeric limit of installations per IP
- `"no"` — no limit enforced

**Example responses:**

```json
// BM: only 1 per IP
{"miner_code": "BM", "version": "3.10.3", "limit": 1}

// HG: up to 2 per IP
{"miner_code": "HG", "version": "2.5.1", "limit": 2}

// MYST: no limit
{"miner_code": "MYST", "version": "1.8.0", "limit": "no"}
```

---

### 2. Lease Acquire — `POST /installations/{miner_key}/leases/{install_id}`

**Request body:**

```json
{
  "mode": "acquire",
  "lease_seconds": 3600,
  "external_ip": "203.0.113.42"
}
```

> `external_ip` is present when the miner type has IP enforcement enabled (limit is not `"no"`).

**Backend behavior:**

1. If `external_ip` is present:
   - Extract miner type from `miner_key` (e.g., `BM`, `HG`, `MYST`)
   - Get the IP limit for this miner type from version metadata
   - Count existing **active** leases of the same miner type with this `external_ip`
   - If count >= limit → deny the lease with `IP_LIMIT_REACHED`
   - If count < limit → grant the lease and **persist `external_ip` on the lease record**
2. If `external_ip` is absent: behave as before, no IP enforcement

**Response — success:**

```json
{
  "granted": true,
  "expires_at": "2026-02-01T15:30:00Z",
  "error_code": null
}
```

**Response — IP limit reached:**

```json
{
  "granted": false,
  "expires_at": null,
  "error_code": "IP_LIMIT_REACHED"
}
```

---

### 3. Lease Renew — `PATCH /installations/{miner_key}/leases/{install_id}`

**Request body:**

```json
{
  "mode": "renew",
  "lease_seconds": 3600,
  "external_ip": "203.0.113.42"
}
```

> `external_ip` is present when the miner type has IP enforcement enabled (limit is not `"no"`).

**Backend behavior:**

1. If `external_ip` is present: **update** the stored IP on the lease record
   - If the IP changed from the previously stored value, release the old IP mapping and store the new one
   - Do **not** block renewal due to IP change — just update the mapping
2. If `external_ip` is absent: behave as before

**Response (unchanged):**

```json
{
  "granted": true
}
```

---

### 3.5. Heartbeat/Status Update — `POST /installations/{miner_key}/installations/{install_id}`

**Request body (example with heartbeat data):**

```json
{
  "install_id": "a7cb3f40-2126-466e-92a9-42e97fdb01c4",
  "hostname": "PLEX",
  "is_installed": true,
  "is_uptodate": true,
  "external_ip": "203.0.113.42",
  "ip_detected_at": "2026-02-12T11:20:08Z",
  "os": "Windows-10-10.0.19045-SP0",
  "poc_version_installed": "1.9.1",
  "software_version_installed": "6.4.6"
}
```

> `external_ip` is **always included** for all miners to enable continuous IP tracking across all miner types.

**Backend behavior:**

1. Update the installation record with provided fields
2. If `external_ip` is present: update the stored IP address (used for device distribution monitoring)
3. Set `last_seen_at` timestamp to current time

---

### 4. IP Status Check — `GET /installations/ip/{external_ip}/status`

**No request body.**

**Backend behavior:**

- Query all **active** leases that have this `external_ip` stored
- Group results by miner type
- For each miner type, count installations and collect miner keys
- Return counts and details grouped by miner type

**Response:**

```json
{
  "external_ip": "203.0.113.42",
  "installations_by_type": {
    "BM": {
      "count": 1,
      "limit": 1,
      "details": [
        {"miner_key": "BM-a1b2c3d4e5f6", "install_id": "inst-001"}
      ]
    },
    "ISM": {
      "count": 2,
      "limit": 5,
      "details": [
        {"miner_key": "ISM-x9y8z7w6v5u4", "install_id": "inst-002"},
        {"miner_key": "ISM-m3n2o1p0q9r8", "install_id": "inst-003"}
      ]
    },
    "RDN": {
      "count": 5,
      "limit": "no",
      "details": [
        {"miner_key": "RDN-abc123", "install_id": "inst-004"},
        {"miner_key": "RDN-def456", "install_id": "inst-005"}
        // ... 3 more
      ]
    }
  }
}
```

**Response — no installations:**

```json
{
  "external_ip": "203.0.113.42",
  "installations_by_type": {}
}
```

**Usage:**

The installer can:
1. Call `GET /versions/{miner_code}` to get the limit for the miner being installed
2. Call this endpoint to see current usage for the IP
3. Check if `installations_by_type[miner_code].count < limit` before attempting installation

---

## Database Changes

### Lease/Installation Table

Add an `external_ip` field (string, nullable) to the lease/installation record:

| Field         | Type          | Description                                                      |
|---------------|---------------|------------------------------------------------------------------|
| `external_ip` | `VARCHAR(45)` | IPv4 or IPv6 address. Nullable. Set for miners with IP limits.  |

### Version Metadata Table

Add a `limit` field to the version/miner metadata:

| Field         | Type          | Description                                                      |
|---------------|---------------|------------------------------------------------------------------|
| `limit`       | `VARCHAR(10)` | IP limit: numeric value ("1", "2", "5") or "no" for unlimited. |

**`external_ip` Lifecycle:**

- **Set** on lease acquire (from `external_ip` in request body)
- **Updated** on lease renew (if `external_ip` differs from stored value)
- **Updated** on heartbeat POST (from `external_ip` in heartbeat payload for continuous IP tracking)
- **Cleared/released** when lease expires or is deleted

**`limit` Configuration:**

- Set per miner type in version metadata
- Used by backend to enforce IP-based installation limits
- Retrieved by installer via `GET /versions/{miner_code}` endpoint

---

## Flow Summary

```
Installer                          Backend
   |                                  |
   |-- GET /versions/{code} -------->|  Get miner version & IP limit
   |<-- {limit: 1, ...} -------------|  e.g., BM has limit=1
   |                                  |
   |-- GET .../ip/{ip}/status ------>|  Check current IP usage
   |<-- {BM: {count:0, limit:1}} ----|  IP is available for BM
   |                                  |
   |-- POST .../leases/{id} -------->|  Acquire lease with external_ip
   |   {mode: acquire,               |  Backend checks IP uniqueness,
   |    lease_seconds: 3600,          |  persists external_ip on record
   |    external_ip: "203.0.113.42"}  |
   |<-- {granted: true} -------------|
   |                                  |
   |   ... service running (heartbeats every 5-15 min) ...|
   |                                  |
   |-- POST .../installations/{id} ->|  Heartbeat with current IP
   |   {external_ip: "203.0.113.42",  |  Backend updates external_ip
   |    hostname: "PLEX", ...}        |  and last_seen_at
   |<-- (ok) -------------------------|
   |                                  |
   |   ... later (service renewal) ...|
   |                                  |
   |-- PATCH .../leases/{id} ------->|  Renew lease with current IP
   |   {mode: renew,                  |  Backend updates external_ip
   |    lease_seconds: 3600,          |  if it changed
   |    external_ip: "203.0.113.42"}  |
   |<-- {granted: true} -------------|
```
