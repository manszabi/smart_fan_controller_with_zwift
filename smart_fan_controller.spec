# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Smart Fan Controller
# Build: pyinstaller smart_fan_controller.spec

import os

block_cipher = None

# --- Main controller exe ---
main_a = Analysis(
    ['swift_fan_controller_new_v7.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('settings.example.json', '.'),
        ('settings.example.jsonc', '.'),
    ],
    hiddenimports=[
        'tkinter',
        'tkinter.font',
        'bleak',
        'bleak.backends.winrt',
        'bleak.backends.winrt.scanner',
        'bleak.backends.winrt.client',
        'openant',
        'openant.easy.node',
        'openant.devices',
        'openant.devices.power_meter',
        'openant.devices.heart_rate',
        'requests',
        'asyncio',
        'json',
        'logging',
        'collections',
        'dataclasses',
        'enum',
        'abc',
        'math',
        'signal',
        'threading',
        'queue',
        'copy',
        'atexit',
        'subprocess',
        'urllib.request',
        'ctypes',
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
main_pyz = PYZ(main_a.pure, main_a.zipped_data, cipher=block_cipher)
main_exe = EXE(
    main_pyz,
    main_a.scripts,
    [],
    exclude_binaries=True,
    name='SmartFanController',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=None,
)

# --- Zwift API polling exe ---
zwift_a = Analysis(
    ['zwift_api_polling.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'requests',
        'urllib3',
        'charset_normalizer',
        'certifi',
        'idna',
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
zwift_pyz = PYZ(zwift_a.pure, zwift_a.zipped_data, cipher=block_cipher)
zwift_exe = EXE(
    zwift_pyz,
    zwift_a.scripts,
    [],
    exclude_binaries=True,
    name='zwift_api_polling',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=None,
)

# --- Bundle both into one dist folder ---
coll = COLLECT(
    main_exe,
    main_a.binaries,
    main_a.zipfiles,
    main_a.datas,
    zwift_exe,
    zwift_a.binaries,
    zwift_a.zipfiles,
    zwift_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SmartFanController',
)
