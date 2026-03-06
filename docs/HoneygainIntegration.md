# Honeygain Integration Guide

This UI release ships with a lightweight Honeygain status panel so Bandwidth Miner (`BM`) operators can monitor sharing activity without re-requesting user consent. The installer is responsible for deploying the native SDK binaries, provisioning configuration, and gathering the user's approval up-front. The GUI simply consumes that data, starts the SDK when allowed, and offers an opt-out switch.

## Folder Layout

```
<install-root>/
├── SDK/
│   ├── linux-honeygain-sdk/
│   │   └── <arch>/lib/libhgsdk.so
│   └── windows-honeygain-sdk/
│       └── <arch>/bin/hgsdk.dll
└── config/
    ├── miner_config.enc
    ├── honeygain.json   ← non-sensitive settings
    └── honeygain.enc    ← encrypted API key
```

* `SDK/` contains the official Honeygain binaries for every supported architecture. The GUI auto-detects host architecture and prefers `windows-honeygain-sdk/<arch>/bin/hgsdk.dll` on Windows and `<arch>/lib/libhgsdk.so` on Linux (glibc builds first, musl as fallback).
* `config/honeygain.json` carries opt-in flags, log preferences, and (optionally) explicit library paths.
* `config/honeygain.enc` stores the Honeygain API key encrypted with the helper script below.

## `config/honeygain.json` schema

```jsonc
{
  "enabled": true,                 // installer sets to true once consent is granted
  "sdk_root": "SDK",               // optional; defaults to <install-root>/SDK
  "library_path": null,            // optional absolute override for libhgsdk
  "log_dir": "/var/log/honeygain", // optional SDK log location
  "poll_interval_seconds": 20      // optional UI refresh cadence (>=5s)
}
```

Notes:

* The GUI searches for the config in:
  1. `$HONEYGAIN_CONFIG_PATH` (exact file)
  2. `$HONEYGAIN_CONFIG_DIR/honeygain.json`
  3. `<ProgramData>/FryNetworks/miner-<code>/config/honeygain.json`
  4. `<install-root>/config/honeygain.json`
  5. `./config/honeygain.json` (developer fallback)
* If the installer stores state elsewhere, set `HONEYGAIN_CONFIG_PATH` or `HONEYGAIN_CONFIG_DIR` during launch.

## Encrypted Honeygain Secret (`honeygain.enc`)

Generate the encrypted secret file that contains the Honeygain API key using the helper script:

```bash
python tools/create_honeygain_config.py create HG-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX --output config/honeygain.enc
```

* The script uses the same PBKDF2 + Fernet scheme as `miner_config.enc`, so only FryNetworks tooling can decrypt it.
* To verify or debug a secret, run `python tools/create_honeygain_config.py read config/honeygain.enc`.
* At runtime the GUI decrypts `honeygain.enc` (or the path specified via `HONEYGAIN_SECRET_PATH`) before calling `hgsdk_start()`.

## Runtime Behaviour

* On BM builds the Live Data tab now includes a **Honeygain Sharing** panel that shows:
  * Whether SDK binaries are present
  * Installer config path and API key status (present/absent)
  * Consent + runtime status (`running`, `opted in`, errors)
  * Device/session identifiers and raw payload from `hgsdk_identify`
  * Reported traffic totals when available
* The GUI automatically calls `hgsdk_start()` on launch (only when `enabled` and an API key exist) and refreshes telemetry every `poll_interval_seconds`.
* The Honeygain panel now exposes a toggle that flips the `enabled` flag inside `config/honeygain.json`, calling `hgsdk_start()`/`hgsdk_opt_out()` as needed. Consent is still obtained by the installer; the toggle only works after an initial approval and when a valid `honeygain.enc` is available.
* No consent prompts ever appear inside the GUI; the installer must handle any UX/legal touchpoints.

## Environment Overrides

* `HONEYGAIN_SDK_LIB` – absolute path to `libhgsdk.so`/`hgsdk.dll`.
* `HONEYGAIN_CONFIG_PATH` – absolute path to `honeygain.json`.
* `HONEYGAIN_CONFIG_DIR` – directory containing `honeygain.json`.
* `HONEYGAIN_SECRET_PATH` – absolute path to `honeygain.enc`.
* `HONEYGAIN_SECRET_DIR` – directory containing `honeygain.enc`.

These are primarily for development or custom packaging scenarios.

## Installer workflow

1. During the build phase configure `OP_HONEYGAIN_KEY_REF` in `.env`. `build_installer.ps1` uses `op read` to fetch the Honeygain API key from 1Password and embeds it into `build_config.json` so the packaged installer can provision `honeygain.enc` automatically.
2. During a BM install the GUI exposes a **Honeygain Sharing** toggle inside the “Bandwidth Sharing Programs” section. Turning it on simply records consent; the API key is already embedded so the user is never asked to type it.
3. The CLI exposes the same feature via `--enable-honeygain`. When the flag is present the installer copies the Windows/Linux SDK folders into `SDK/`, writes `config/honeygain.json`, and generates `config/honeygain.enc` from the embedded secret.
4. Both flows assume consent was already obtained outside the miner UI; the GUI will not ask the end user.

## Troubleshooting

| Symptom | Resolution |
|---------|------------|
| Panel says "Honeygain SDK not available" | Confirm the correct architecture folder exists under `SDK/` and that dependent system libraries (glibc, pthread, etc.) are installed. |
| Panel says "Honeygain installer data missing" | Ensure `config/honeygain.json` and `config/honeygain.enc` both exist and decrypt successfully. |
| Opt-out button disabled | The installer set `"enabled": false` (already opted out) or the SDK library is missing. |
| No traffic shown | The SDK only surfaces what `hgsdk_identify` returns. Verify that the Honeygain account reflects active traffic and the API key is valid. |

The GUI logs rich error context to `miner_gui.log` via `log_step(...)` calls under the `honeygain_*` namespace for support teams.
