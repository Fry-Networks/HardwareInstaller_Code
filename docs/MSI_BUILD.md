MSI packaging (WiX 3.11)
=========================

This repo ships the installer as a PyInstaller EXE. The files below add a minimal WiX authoring to wrap that EXE into an MSI so Windows registers it under Apps & Features with an uninstaller entry.

What you need
- WiX Toolset 3.x (`candle.exe`, `light.exe`) on PATH. For example, `C:\Program Files (x86)\WiX Toolset v3.14\bin` if you installed 3.14.
- A built PyInstaller EXE (e.g., `dist/frynetworks_installer.exe`).

Files
- `packaging/wix/installer.wxs` - WiX authoring. Uses preprocessor variables for the EXE path/name, version, and manufacturer.

Build steps (example)
1) Build the EXE with PyInstaller so you have `dist\frynetworks_installer.exe`.
2) From repo root, run:
   ```powershell
   $exe = Resolve-Path dist/frynetworks_installer.exe
   $exeName = Split-Path $exe -Leaf
   $version = (Get-Content version.py -Raw) -match "__version__\\s*=\\s*['\\\"]([^'\\\"]+)" | Out-Null; $version = $matches[1]
   candle -dInstallerExe="$exe" -dInstallerExeName="$exeName" -dProductVersion="$version" -dManufacturer="FryNetworks" packaging/wix/installer.wxs
   light -o out/frynetworks_installer.msi installer.wixobj
   ```
   Adjust `-dManufacturer` as needed. The `installer.wixobj` file is produced by `candle` in the current directory; you can place outputs in `out/` or `build/` as you prefer.
   With the current authoring, the CAB is embedded into the MSI (`EmbedCab="yes"`), so the MSI is a single file containing the payload.

Notes
- The MSI installs the EXE under `Program Files\FryInstaller` and creates a Start Menu shortcut "Fry Installer."
- UpgradeCode is fixed in the `.wxs`; keep it stable to allow upgrades.
- If you rename the EXE, pass the new filename via `-dInstallerExeName`. If you move the EXE, update `-dInstallerExe`.
- To add more files (icons, config), duplicate the `<Component>` pattern in the `.wxs` and reference them in the `Feature`.
- Dialog assets: small top-right logo uses `resources/frynetworks_logo_55.bmp` (generated from logo PNG); no license dialog (Welcome -> InstallDir -> VerifyReady -> Progress -> Exit). Finish checkbox can launch the installer.

CLI shortcut
- On Windows you can reuse `build_cli.py` to avoid manual candle/light commands:
  - Build EXE + MSI: `python build_cli.py --msi`
  - Build MSI only from an existing EXE: `python build_cli.py --msi-only --msi-exe dist\frynetworks_installer_v3.6.0.exe`
