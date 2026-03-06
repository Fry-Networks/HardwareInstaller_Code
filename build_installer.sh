#!/bin/bash

# FryNetworks Installer Build Script for Ubuntu
# Usage: ./build_installer.sh [VERSION]
# 
# VERSION: Version number (e.g., 5.5.5)
#          Will be automatically prefixed with "linux-v" to create "linux-v5.5.5"
#          This ensures the installer downloads Linux-specific binaries from GitHub releases

set -e  # Exit on any error

# 1Password references for retrieving secrets at build time
export OP_BEARER_TOKEN_REF="op://HardwareAPI/Hardware_API/API_BEARER_TOKEN"
export OP_GUI_GITHUB_REPO_REF="op://VSCode/hardware_exe/Github_repo_test"
export OP_GUI_GITHUB_TOKEN_REF="op://VSCode/hardware_exe/Github_token"
export OP_HW_GITHUB_REPO_REF="op://VSCode/hardware_exe/Github_repo_hardwareinstaller"
export OP_HW_GITHUB_TOKEN_REF="op://VSCode/hardware_exe/Github_token_hardwareinstaller"
export OP_POC_GITHUB_REPO_REF="op://VSCode/hardware_exe/Github_repo_poc"
export OP_POC_GITHUB_TOKEN_REF="op://VSCode/hardware_exe/Github_token_poc"
export EXTERNAL_API_BASE_URL="https://hardwareapi.frynetworks.com"
export OLOSTEP_BROWSER_URL="https://olostepbrowser.s3.us-east-1.amazonaws.com/updates/win32/x64/Olostep-Browser-1.0.1+Setup.exe"
export OP_HONEYGAIN_KEY_REF="op://Bandwidth Miners/Honeygain SDK API/credential"
export OP_BRIGHT_APP_ID_REF="op://Bandwidth Miners/Bright Data SDK Login/APP_ID"
export OP_MYSTERIUM_PAYOUT_REF="op://Bandwidth Miners/Mysterium SDK API/MYST_PAYOUT_ADDR"
export OP_MYSTERIUM_REG_TOKEN_REF="op://Bandwidth Miners/Mysterium SDK API/MYST_REG_TOKEN"
export OP_MYSTERIUM_API_KEY_REF="op://Bandwidth Miners/Mysterium SDK API/MYST_API_KEY"
# Encryption key references (create these in 1Password before first build)
export OP_ENC_HONEYGAIN_SALT_REF="op://Bandwidth Miners/Encryption Keys/HONEYGAIN_SALT"
export OP_ENC_HONEYGAIN_PASSWORD_REF="op://Bandwidth Miners/Encryption Keys/HONEYGAIN_PASSWORD"
export OP_ENC_BRIGHT_SALT_REF="op://Bandwidth Miners/Encryption Keys/BRIGHT_SALT"
export OP_ENC_BRIGHT_PASSWORD_REF="op://Bandwidth Miners/Encryption Keys/BRIGHT_PASSWORD"
export OP_ENC_SDK_SALT_REF="op://Bandwidth Miners/Encryption Keys/SDK_SALT"
export OP_ENC_SDK_PASSWORD_REF="op://Bandwidth Miners/Encryption Keys/SDK_PASSWORD"

# If no version provided, read from version.py
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    echo "No version specified, reading from version.py..."
    VERSION_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/version.py"
    if [ -f "$VERSION_FILE" ]; then
        VERSION=$(grep -oP '__version__\s*=\s*["'"'"']\K[^"'"'"']+' "$VERSION_FILE" || echo "")
        if [ -n "$VERSION" ]; then
            echo "  ✓ Version from version.py: $VERSION"
        else
            echo "  ✗ Could not parse version from version.py, using default 1.0.0"
            VERSION="1.0.0"
        fi
    else
        echo "  ✗ version.py not found, using default version 1.0.0"
        VERSION="1.0.0"
    fi
fi

# Add linux- prefix to version for Linux-specific releases
LINUX_VERSION="linux-${VERSION}"
# Ensure version starts with 'v' for GitHub releases
if [[ ! "$LINUX_VERSION" =~ ^linux-v ]]; then
    LINUX_VERSION="linux-v${VERSION}"
fi

echo "========================================"
echo "FryNetworks Installer Build Script (Ubuntu)"
echo "Version: $VERSION"
echo "Linux Version Tag: $LINUX_VERSION"
echo "========================================"

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$INSTALLER_DIR"

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[0;37m'
NC='\033[0m' # No Color

echo -e "\n${YELLOW}[1/6] Checking system dependencies...${NC}"

# Check if running on Ubuntu/Debian
if ! command -v apt &> /dev/null; then
    echo -e "${RED}  ✗ This script requires Ubuntu/Debian with apt package manager${NC}"
    exit 1
fi

# Check for required system packages
REQUIRED_PACKAGES=(
    "python3"
    "python3-pip"
    "python3-venv"
    "python3-dev"
    "build-essential"
    "libgl1-mesa-dev"
    "libglib2.0-0"
    "libxcb-xinerama0"
    "libxcb-cursor0"
    "libxkbcommon-x11-0"
    "libxcb-icccm4"
    "libxcb-image0"
    "libxcb-keysyms1"
    "libxcb-randr0"
    "libxcb-render-util0"
    "libxcb-shape0"
    "libfontconfig1"
    "libfreetype6"
    "bc"
)

MISSING_PACKAGES=()
for pkg in "${REQUIRED_PACKAGES[@]}"; do
    if ! dpkg -l "$pkg" &> /dev/null; then
        MISSING_PACKAGES+=("$pkg")
    fi
done

if [ ${#MISSING_PACKAGES[@]} -ne 0 ]; then
    echo -e "${YELLOW}  Installing missing system packages: ${MISSING_PACKAGES[*]}${NC}"
    sudo apt update
    sudo apt install -y "${MISSING_PACKAGES[@]}"
fi
echo -e "${GREEN}  ✓ System dependencies verified${NC}"

echo -e "\n${YELLOW}[2/6] Retrieving bearer token from 1Password...${NC}"
if ! command -v op &> /dev/null; then
    echo -e "${RED}  ✗ 1Password CLI (op) not found. Please install it first:${NC}"
    echo -e "${RED}     curl -sS https://downloads.1password.com/linux/keys/1password.asc | sudo gpg --dearmor --output /usr/share/keyrings/1password-archive-keyring.gpg${NC}"
    echo -e "${RED}     echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/amd64 stable main' | sudo tee /etc/apt/sources.list.d/1password.list${NC}"
    echo -e "${RED}     sudo apt update && sudo apt install 1password-cli${NC}"
    exit 1
fi

# Check if signed in to 1Password
if ! op account list &> /dev/null; then
    echo -e "${RED}  ✗ Not signed in to 1Password. Please sign in first:${NC}"
    echo -e "${RED}     op signin${NC}"
    exit 1
fi

BEARER_TOKEN=$(op read "$OP_BEARER_TOKEN_REF" 2>/dev/null || true)
if [ -z "$BEARER_TOKEN" ]; then
    echo -e "${RED}  ✗ Failed to retrieve bearer token from 1Password${NC}"
    echo -e "${RED}  Make sure the item exists and you have access to it${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ Bearer token retrieved successfully${NC}"

echo -e "\n${YELLOW}[2b/6] Retrieving GUI GitHub repo path from 1Password...${NC}"
GUI_GITHUB_PATH=$(op read "$OP_GUI_GITHUB_REPO_REF" 2>/dev/null || true)
if [ -z "$GUI_GITHUB_PATH" ]; then
    echo -e "${RED}  ✗ Failed to retrieve GUI GitHub path from 1Password${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ GUI GitHub path retrieved: $GUI_GITHUB_PATH${NC}"

echo -e "\n${YELLOW}[2c/6] Retrieving PoC GitHub repo path from 1Password...${NC}"
POC_GITHUB_PATH=$(op read "$OP_POC_GITHUB_REPO_REF" 2>/dev/null || true)
if [ -z "$POC_GITHUB_PATH" ]; then
    echo -e "${RED}  ✗ Failed to retrieve PoC GitHub path from 1Password${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ PoC GitHub path retrieved: $POC_GITHUB_PATH${NC}"

echo -e "\n${YELLOW}[2d/6] Retrieving GUI GitHub PAT from 1Password for build-time asset download...${NC}"
GUI_GITHUB_PAT=$(op read "$OP_GUI_GITHUB_TOKEN_REF" 2>/dev/null || true)
if [ -z "$GUI_GITHUB_PAT" ]; then
    echo -e "${RED}  ✗ Failed to retrieve GUI GitHub PAT from 1Password (needed to download release assets)${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ GUI GitHub PAT retrieved for build (hidden)${NC}"

echo -e "\n${YELLOW}[2e/6] Retrieving PoC GitHub PAT from 1Password for build-time asset download...${NC}"
POC_GITHUB_PAT=$(op read "$OP_POC_GITHUB_TOKEN_REF" 2>/dev/null || true)
if [ -z "$POC_GITHUB_PAT" ]; then
    echo -e "${RED}  ✗ Failed to retrieve PoC GitHub PAT from 1Password (needed to download release assets)${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ PoC GitHub PAT retrieved for build (hidden)${NC}"

# Honeygain integration: required to be provided via 1Password for builds
if [ -z "$OP_HONEYGAIN_KEY_REF" ]; then
    echo -e "\n${RED}  ✗ OP_HONEYGAIN_KEY_REF not set - Honeygain API key must be provided via 1Password${NC}"
    exit 1
fi
echo -e "\n${YELLOW}[2f/6] Retrieving Honeygain API key from 1Password...${NC}"
HONEYGAIN_KEY=$(op read "$OP_HONEYGAIN_KEY_REF" 2>/dev/null || true)
if [ -z "$HONEYGAIN_KEY" ]; then
    echo -e "${RED}  ✗ Failed to retrieve Honeygain API key from 1Password${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ Honeygain API key embedded for BM installs${NC}"

# Bright integration: required to be provided via 1Password for builds
if [ -z "$OP_BRIGHT_APP_ID_REF" ]; then
    echo -e "\n${RED}  ✗ OP_BRIGHT_APP_ID_REF not set - Bright app ID must be provided via 1Password${NC}"
    exit 1
fi
echo -e "\n${YELLOW}[2g/6] Retrieving Bright app ID from 1Password...${NC}"
BRIGHT_APP_ID=$(op read "$OP_BRIGHT_APP_ID_REF" 2>/dev/null || true)
if [ -z "$BRIGHT_APP_ID" ]; then
    echo -e "${RED}  ✗ Failed to retrieve Bright app ID from 1Password${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ Bright app ID embedded for BM installs${NC}"

# Mysterium credentials: required to be provided via 1Password for builds
if [ -z "$OP_MYSTERIUM_PAYOUT_REF" ] || [ -z "$OP_MYSTERIUM_REG_TOKEN_REF" ] || [ -z "$OP_MYSTERIUM_API_KEY_REF" ]; then
    echo -e "\n${RED}  ✗ One or more OP_MYSTERIUM_* refs not set - Mysterium credentials must be provided via 1Password${NC}"
    exit 1
fi
echo -e "\n${YELLOW}[2h/6] Retrieving Mysterium credentials from 1Password...${NC}"
MYSTERIUM_PAYOUT=$(op read "$OP_MYSTERIUM_PAYOUT_REF" 2>/dev/null || true)
MYSTERIUM_REG=$(op read "$OP_MYSTERIUM_REG_TOKEN_REF" 2>/dev/null || true)
MYSTERIUM_API_KEY=$(op read "$OP_MYSTERIUM_API_KEY_REF" 2>/dev/null || true)
if [ -z "$MYSTERIUM_PAYOUT" ] || [ -z "$MYSTERIUM_REG" ] || [ -z "$MYSTERIUM_API_KEY" ]; then
    echo -e "${RED}  ✗ Failed to retrieve one or more Mysterium credentials from 1Password${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ Mysterium credentials embedded for BM installs${NC}"

echo -e "\n${YELLOW}[2i/6] Retrieving encryption keys from 1Password...${NC}"
ENC_HONEYGAIN_SALT=$(op read "$OP_ENC_HONEYGAIN_SALT_REF" 2>/dev/null || true)
ENC_HONEYGAIN_PASSWORD=$(op read "$OP_ENC_HONEYGAIN_PASSWORD_REF" 2>/dev/null || true)
ENC_BRIGHT_SALT=$(op read "$OP_ENC_BRIGHT_SALT_REF" 2>/dev/null || true)
ENC_BRIGHT_PASSWORD=$(op read "$OP_ENC_BRIGHT_PASSWORD_REF" 2>/dev/null || true)
ENC_SDK_SALT=$(op read "$OP_ENC_SDK_SALT_REF" 2>/dev/null || true)
ENC_SDK_PASSWORD=$(op read "$OP_ENC_SDK_PASSWORD_REF" 2>/dev/null || true)
if [ -z "$ENC_HONEYGAIN_SALT" ] || [ -z "$ENC_HONEYGAIN_PASSWORD" ] || \
   [ -z "$ENC_BRIGHT_SALT" ] || [ -z "$ENC_BRIGHT_PASSWORD" ] || \
   [ -z "$ENC_SDK_SALT" ] || [ -z "$ENC_SDK_PASSWORD" ]; then
    echo -e "${RED}  ✗ Failed to retrieve encryption keys from 1Password${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ Encryption keys retrieved${NC}"

echo -e "\n${YELLOW}[3/6] Setting up Python virtual environment...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo -e "${GREEN}  ✓ Created virtual environment${NC}"
else
    echo -e "${GREEN}  ✓ Virtual environment already exists${NC}"
fi

source venv/bin/activate
echo -e "${GREEN}  ✓ Activated virtual environment${NC}"

# Upgrade pip
pip install --upgrade pip setuptools wheel

# Install Python dependencies
echo -e "\n${YELLOW}[4/6] Installing Python dependencies...${NC}"
pip install -r requirements.txt
pip install pyinstaller requests
echo -e "${GREEN}  ✓ Python dependencies installed${NC}"

echo -e "\n${YELLOW}[5/6] Creating build_config.json...${NC}"
BUILD_DATE=$(date '+%Y-%m-%d %H:%M:%S')
cat > build_config.json << EOF
{
    "external_api": {
        "base_url": "$EXTERNAL_API_BASE_URL",
        "bearer_token": "$BEARER_TOKEN",
        "timeout": 10.0
    },
    "github": {
        "gui": {
            "path": "$GUI_GITHUB_PATH",
            "token": "$GUI_GITHUB_PAT"
        },
        "poc": {
            "path": "$POC_GITHUB_PATH",
            "token": "$POC_GITHUB_PAT"
        }
    },
    "encryption": {
        "honeygain": { "salt": "$ENC_HONEYGAIN_SALT", "password": "$ENC_HONEYGAIN_PASSWORD" },
        "bright": { "salt": "$ENC_BRIGHT_SALT", "password": "$ENC_BRIGHT_PASSWORD" },
        "sdk": { "salt": "$ENC_SDK_SALT", "password": "$ENC_SDK_PASSWORD" }
    },
    "partner_integrations": {
        "honeygain": {
            "enabled": $([ -n "$HONEYGAIN_KEY" ] && echo "true" || echo "false"),
            "api_key": "$HONEYGAIN_KEY"
        },
        "bright": {
            "enabled": $([ -n "$BRIGHT_APP_ID" ] && echo "true" || echo "false"),
            "app_id": "$BRIGHT_APP_ID"
        }
        ,
        "mysterium": {
            "enabled": true,
            "payout_addr": "$MYSTERIUM_PAYOUT",
            "reg_token": "$MYSTERIUM_REG",
            "api_key": "$MYSTERIUM_API_KEY"
        }
    },
    "status": "embedded",
    "source": "1password",
    "version": "$LINUX_VERSION",
    "base_version": "$VERSION",
    "platform": "linux",
    "build_date": "$BUILD_DATE"
}
EOF
echo -e "${GREEN}  ✓ build_config.json created${NC}"

echo -e "\n${YELLOW}[5b/6] Preparing embedded resources...${NC}"

# Create resources/embedded directory if it doesn't exist
mkdir -p resources/embedded

# Note: For Linux, we don't need NSSM (Windows-specific)
# The Linux version uses systemd directly
echo -e "${GREEN}  ✓ Embedded resources prepared (Linux uses systemd, no additional tools needed)${NC}"

echo -e "\n${YELLOW}[6/6] Cleaning previous builds...${NC}"
rm -rf build dist *.spec
echo -e "${GREEN}  ✓ Build directories cleaned${NC}"

echo -e "\n${YELLOW}Building installer with PyInstaller...${NC}"
echo -e "${GRAY}  This may take 30-60 seconds...${NC}"

# Add the current directory to Python path for PyInstaller
export PYTHONPATH="${PWD}:${PYTHONPATH}"

EXE_NAME="frynetworks_installer_v${VERSION}"

pyinstaller \
    --onefile \
    --noconsole \
    --icon "resources/frynetworks_logo_256.png" \
    --paths "${PWD}" \
    --add-data "build_config.json:." \
    --add-data "resources/frynetworks_logo_256.png:resources" \
    --add-data "resources/frynetworks_logo.png:resources" \
    --add-data "resources/frynetworks_logo.ico:resources" \
    --add-data "resources/background.png:resources" \
    --add-data "resources/embedded:resources/embedded" \
    --add-data "core:core" \
    --add-data "gui:gui" \
    --add-data "tools:tools" \
    --add-data "dependencies:dependencies" \
    --add-data "SDK:SDK" \
    --name "$EXE_NAME" \
    installer_main.py

if [ -f "dist/$EXE_NAME" ]; then
    echo -e "\n${GREEN}✓ Build completed successfully!${NC}"
    echo -e "\n${CYAN}Installer location:${NC}"
    echo -e "${NC}  $(realpath "dist/$EXE_NAME")${NC}"
    
    FILE_SIZE=$(stat -f%z "dist/$EXE_NAME" 2>/dev/null || stat -c%s "dist/$EXE_NAME")
    if command -v bc &> /dev/null; then
        FILE_SIZE_MB=$(echo "scale=2; $FILE_SIZE / 1048576" | bc)
    else
        FILE_SIZE_MB=$(python3 -c "print(f'{ $FILE_SIZE / 1048576 :.2f}')")
    fi
    echo -e "\n${GRAY}File size: ${FILE_SIZE_MB} MB${NC}"
    
    # Make executable
    chmod +x "dist/$EXE_NAME"
    
    echo -e "\n${CYAN}To test the installer:${NC}"
    echo -e "${NC}  cd dist${NC}"
    echo -e "${NC}  ./$EXE_NAME --gui${NC}"

    echo -e "\n${YELLOW}Packaging Debian .deb (amd64)...${NC}"
    PKG_NAME="frynetworks-installer"
    PKG_VERSION="${VERSION}"
    PKG_ROOT="dist/${PKG_NAME}_${PKG_VERSION}_amd64"

    rm -rf "$PKG_ROOT"
    mkdir -p \
        "$PKG_ROOT/DEBIAN" \
        "$PKG_ROOT/usr/bin" \
        "$PKG_ROOT/opt/frynetworks-installer" \
        "$PKG_ROOT/usr/share/applications" \
        "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps"

    cp "dist/$EXE_NAME" "$PKG_ROOT/opt/frynetworks-installer/"
    chmod 755 "$PKG_ROOT/opt/frynetworks-installer/$EXE_NAME"
    ln -sf "/opt/frynetworks-installer/$EXE_NAME" "$PKG_ROOT/usr/bin/frynetworks-installer"

    cp "resources/frynetworks_logo_256.png" "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps/frynetworks-installer.png"

    cat > "$PKG_ROOT/usr/share/applications/frynetworks-installer.desktop" << EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=FryNetworks Installer
Comment=Install and manage FryNetworks hardware components
Exec=/usr/bin/frynetworks-installer --gui
TryExec=/usr/bin/frynetworks-installer
Icon=frynetworks-installer
Categories=Utility;System;
Terminal=false
EOF

    cat > "$PKG_ROOT/DEBIAN/control" << EOF
Package: $PKG_NAME
Version: $PKG_VERSION
Section: utils
Priority: optional
Architecture: amd64
Maintainer: FryNetworks <support@frynetworks.com>
Description: FryNetworks hardware installer
 A packaged installer for FryNetworks hardware setup on Debian/Ubuntu systems.
EOF

    dpkg-deb --build "$PKG_ROOT" >/dev/null
    if [ -f "${PKG_ROOT}.deb" ]; then
        echo -e "${GREEN}  ✓ Debian package created: ${PKG_ROOT}.deb${NC}"
    else
        echo -e "${RED}  ✗ Failed to create Debian package${NC}"
        exit 1
    fi
else
    echo -e "\n${RED}✗ Build completed but executable not found${NC}"
    exit 1
fi

# Cleanup
echo -e "\n${GRAY}Cleaning up build artifacts...${NC}"
if [ -f "build_config.json" ]; then
    rm -f build_config.json
    echo -e "${GRAY}  Cleaned up build_config.json${NC}"
fi

# Deactivate virtual environment
deactivate

echo -e "\n========================================"
echo -e "${CYAN}Build process complete!${NC}"
echo -e "========================================"
