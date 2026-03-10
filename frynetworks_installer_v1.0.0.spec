# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['core.service_manager', 'core.config_manager', 'core.conflict_detector', 'core.naming', 'core.key_parser', 'tools.external_api']
hiddenimports += collect_submodules('core')
hiddenimports += collect_submodules('tools')


a = Analysis(
    ['installer_main.py'],
    pathex=['.', '.\\core', '.\\gui'],
    binaries=[],
    datas=[('build_config.json', '.'), ('resources\\background.png', 'resources'), ('resources\\frynetworks_logo.ico', 'resources'), ('resources\\embedded', 'resources\\embedded'), ('SDK', 'SDK'), ('core', 'core'), ('tools', 'tools')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='frynetworks_installer_v1.0.0',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    uac_admin=True,
    icon=['resources\\frynetworks_logo.ico'],
)
