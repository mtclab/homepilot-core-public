# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for hp-agent standalone binary
# Usage: pyinstaller hp-agent.spec
# Output: dist/hp-agent

block_cipher = pyaes = None

a = Analysis(
    ['hp_agent/main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'hp_agent',
        'hp_agent.main',
        'hp_agent.config',
        'hp_agent.command_executor',
        'hp_agent.file_ops',
        'hp_agent.system_info',
        'hp_agent.zabbix_sender',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy', 'PIL'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='hp-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)