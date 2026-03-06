# FryNetworks Installer Build System

This directory contains the build system for the FryNetworks Hardware Installer.

## Version Management

The installer uses semantic versioning (MAJOR.MINOR.PATCH):
- **MAJOR**: Breaking changes or major feature releases
- **MINOR**: New features, backwards compatible
- **PATCH**: Bug fixes, backwards compatible

Current version is managed in `version.py`.

## Building the Installer

### Option 1: Using the CLI Tool (Recommended)

```bash
# Build with current version
python build_cli.py

# Build with a specific version
python build_cli.py --version 1.2.3

# Bump patch version and build (e.g., 1.0.0 → 1.0.1)
python build_cli.py --bump patch

# Bump minor version and build (e.g., 1.0.1 → 1.1.0)
python build_cli.py --bump minor

# Bump major version and build (e.g., 1.1.0 → 2.0.0)
python build_cli.py --bump major

# Show current version information
python build_cli.py --show

# Update version without building
python build_cli.py --bump patch --no-build
```

### Option 2: Using PowerShell Script Directly

```powershell
# Build with specific version
.\build_installer.ps1 -Version "1.0.1"

# Build with version from version.py
.\build_installer.ps1
```

## Build Requirements

1. **1Password CLI** (`op`) - For retrieving secrets
2. **Python 3.8+** with the following packages:
   - PySide6
   - cryptography
   - requests
   - PyInstaller
3. **NSSM** (Non-Sucking Service Manager) - Place `nssm.exe` in the `tools/` directory
4. **1Password references for partner integrations** - set `OP_HONEYGAIN_KEY_REF` and `OP_BRIGHT_APP_ID_REF` in `.env` so `build_installer.ps1` can pull the Honeygain API key (HG-…) and Bright app ID during the build. These credentials are embedded into the installer so end users are never prompted for them.

## Build Output

The built installer will be located at:
```
dist/frynetworks_installer.exe
```

## Version History

Track version changes in your commit messages or maintain a CHANGELOG.md file.

## Examples

### Release Workflow

```bash
# Bug fix release
python build_cli.py --bump patch

# New feature release
python build_cli.py --bump minor

# Major release
python build_cli.py --bump major
```

### Development Testing

```bash
# Build without changing version
python build_cli.py

# Test the installer
cd dist
.\frynetworks_installer.exe
```
