# FryNetworks Installer Updater (GitHub release + scheduled task)

This ships a tiny updater that checks the latest GitHub release, downloads the MSI, and runs `msiexec` to update. A per-user scheduled task can run it at logon and daily.

## Files
- `tools/updater.py` – Fetches `https://api.github.com/repos/<repo>/releases/latest`, downloads the `.msi` asset (and optional `.sha256`), verifies checksum, and launches `msiexec`. Logs to `%LOCALAPPDATA%\FryNetworks\Updater\updater.log`.
- `tools/register_updater_task.ps1` – Registers/unregisters a per-user scheduled task to run the updater silently.

## Build the updater EXE (example)
```powershell
pip install pillow pyinstaller
pyinstaller --onefile tools/updater.py --name updater
# Copy dist\updater.exe into your installer payload (e.g., INSTALLFOLDER)
```

## Register the scheduled task (per-user)
```powershell
powershell -ExecutionPolicy Bypass -File tools/register_updater_task.ps1 `
  -UpdaterPath "C:\Program Files (x86)\FryNetworks Installer\updater.exe"
```
Options: `-RunNow` to trigger immediately, `-Remove` to delete the task.

Private repo token:
```powershell
powershell -ExecutionPolicy Bypass -File tools/register_updater_task.ps1 `
  -UpdaterPath "C:\Program Files (x86)\FryNetworks Installer\frynetworks_updater.exe" `
  -GitHubToken "<your PAT with repo read>"
```
The token is injected into the task as `GITHUB_TOKEN` (not baked into the binary).

## Configuration
- Repo default: `FryDevsTestingLab/HardwareInstaller` (change in `tools/updater.py` or pass `--repo owner/name`).
- Version: pass `--current-version vX.Y.Z` or let it infer from `frynetworks_installer_v*.exe` sitting next to `updater.exe`.
- Auth: set `--token`, `GITHUB_TOKEN`, or bake at build time via `EMBEDDED_GITHUB_TOKEN` env var when running PyInstaller; if releases are private, one of these must be provided.
- Quiet install: use `--quiet` (default in the scheduled task).

## How it works
1. Get latest release JSON.
2. If `tag_name` newer than current, find `.msi` asset (and `.sha256` if present).
3. Download to temp, verify checksum (if provided).
4. Run `msiexec /i <msi> /qn` (quiet by default).
5. Log to `%LOCALAPPDATA%\FryNetworks\Updater\updater.log`.

## Uninstall/cleanup
- Remove scheduled task: `-Remove`.
- Delete `%LOCALAPPDATA%\FryNetworks\Updater` if you want to purge logs.
