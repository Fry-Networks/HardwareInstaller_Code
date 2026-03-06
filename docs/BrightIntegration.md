# Bright SDK Integration (Windows BM)

Bright SDK sharing is integrated in the BM GUI similarly to Honeygain, but it is enabled only on Windows builds. The installer is responsible for packaging the native DLLs, encrypting the Bright app ID, and ensuring the user has already provided consent. The GUI simply consumes the installer-provided data, initializes the SDK, and surfaces status with enable/disable controls.

## Folder Layout

```
<install-root>/
├── SDK/
│   └── windows-bright-sdk/
│       ├── lum_sdk64.dll
│       ├── lum_sdk32.dll
│       ├── lum_sdk.dll         (fallback)
│       └── lum_sdk.h / samples /
└── config/
    ├── bright.json   ← non-sensitive toggles/log settings
    └── bright.enc    ← encrypted Bright app id
```

* The GUI searches for the DLL under both `windows-bright-sdk` and automatically picks the 64-bit binary on modern systems.
* `bright.json` contains only safe fields such as:

```jsonc
{
  "enabled": true,
  "app_name": "Fry Bandwidth Miner",
  "lang": "en",
  "info_text": "When enabled, your device contributes to the fVPN network, which can boost the value of your fVPN earnings",
  "poll_interval_seconds": 20
}
```

* `info_text` controls the user-facing subtitle next to the Web Indexing toggle (defaults to "When enabled, your device contributes to the fVPN network, which can boost the value of your fVPN earnings").
* `bright.enc` stores the Bright `app_id` encrypted with the helper script described below.

## Creating the Encrypted Secret

Use the provided tool to generate `bright.enc` during installation:

```bash
python tools/create_bright_config.py create <BRIGHT_APP_ID> --output config/bright.enc
```

* The script uses PBKDF2 + Fernet (same as `miner_config.enc`) so the plaintext app id never ships with the installer.
* To verify or debug a secret, run `python tools/create_bright_config.py read config/bright.enc`.

## Runtime Behaviour

* The Windows BM Live Data tab now includes a **Web Indexing** toggle (Bright) along with a diagnostics grid showing SDK availability, consent choice, and whether the background service is running.
* The GUI calls `brd_sdk_init()`/`brd_sdk_start_service()` using the decrypted app id. Consent dialogs are skipped (the installer must handle user approval separately).
* Moving the Web Indexing toggle flips the `"enabled"` flag in `bright.json`, calls `brd_sdk_opt_out()`/`brd_sdk_start_service()`, and stops/starts the SDK accordingly.
* Honeygain and Bright panels can coexist; each has its own refresh cadence and encrypted secret file.

## Environment Overrides (Development)

* `BRIGHT_SDK_LIB` – absolute path to `lum_sdk64.dll` (overrides autodetect).
* `BRIGHT_CONFIG_PATH` / `BRIGHT_CONFIG_DIR` - override location of `bright.json`.
* `BRIGHT_SECRET_PATH` / `BRIGHT_SECRET_DIR` - override location of `bright.enc`.

## Installer workflow

1. Configure `OP_BRIGHT_APP_ID_REF` in `.env` before running `build_installer.ps1`. The build pulls the Bright app id from 1Password and stores it inside the packaged `build_config.json`.
2. On BM Windows installs the GUI’s “Bandwidth Sharing Programs” section exposes a **Bright Web Indexing** toggle with no text box. When turned on, the installer copies the Bright SDK into `SDK/windows-bright-sdk/`, writes `config/bright.json`, and encrypts the embedded app id into `config/bright.enc`.
3. The CLI mirrors this via `--enable-bright` (Windows-only). Consent is captured without exposing the underlying secret.
4. Honeygain and Bright can both be enabled as long as the installer was built with the corresponding 1Password references configured.

## Troubleshooting

| Symptom | Resolution |
|---------|------------|
| Panel says "Bright SDK not available" | Confirm the Windows DLLs exist under `SDK/windows-bright-sdk/` and match the OS architecture. |
| Panel says "Bright installer data missing" | Ensure `bright.json` and `bright.enc` both exist and decrypt successfully. |
| Toggle disabled | The Bright SDK is missing, the config is absent, or the encrypted secret failed to load. |
| Consent choice is `None` | This value comes directly from `brd_sdk_get_consent_choice()`; verify the installer recorded consent properly. |
