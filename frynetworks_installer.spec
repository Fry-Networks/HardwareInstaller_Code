# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for FryNetworks Installer
This ensures the installer is built with proper Windows settings to prevent duplicate tray icons.
"""

import sys
from pathlib import Path

# Get version from version.py
version_str = "1.0.0"
try:
    with open('version.py', 'r') as f:
        for line in f:
            if '__version__' in line:
                version_str = line.split('=')[1].strip().strip('"').strip("'")
                break
except Exception:
    pass

block_cipher = None

a = Analysis(
    ['installer_main.py'],
    pathex=['.', './core', './gui'],
    binaries=[],
    datas=[
        ('build_config.json', '.'),
        ('resources/background.png', 'resources'),
        ('resources/frynetworks_logo.ico', 'resources'),
        ('resources/embedded', 'resources/embedded'),
        ('SDK', 'SDK'),
        ('core', 'core'),
    ],
    hiddenimports=[
        'core.service_manager',
        'core.config_manager',
        'core.conflict_detector',
        'core.naming',
        'core.key_parser',
        'core.binary_downloader',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Collect all core submodules
from PyInstaller.utils.hooks import collect_submodules
a.hiddenimports += collect_submodules('core')

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=f'frynetworks_installer_v{version_str}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Critical: No console window for GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='resources/frynetworks_logo.ico',
    uac_admin=True,  # Request admin privileges
    # Windows-specific settings to prevent duplicate icons
    version='version_info.txt' if Path('version_info.txt').exists() else None,
)
