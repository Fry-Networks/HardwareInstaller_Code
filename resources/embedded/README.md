# Embedded Resources for FryNetworks Installer

This directory contains binaries that will be embedded into the standalone installer.

## Files to include:

- `nssm.exe` - Windows Service Manager (for installing services)
- Other executables as needed

These files are automatically copied during the build process and embedded into the installer using PyInstaller's `--add-data` option.
