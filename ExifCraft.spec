# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

IS_MAC = sys.platform == "darwin"

# web 是前端静态资源。ExifTool 不内置（约 27 MB，与轻量化目标冲突），
# 用户需要时自己放到程序目录，或用设置页指定路径。
datas = [('web', 'web')]
datas += collect_data_files('webview')
hiddenimports = collect_submodules('webview')

if IS_MAC:
    # macOS 走 pyobjc（pywebview 的 Cocoa 后端），没有 pythonnet / clr_loader
    hiddenimports += [
        'objc', 'AppKit', 'Foundation', 'WebKit', 'Quartz',
        'CoreFoundation', 'UniformTypeIdentifiers',
    ]
else:
    hiddenimports += ['clr_loader', 'pythonnet']

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'numpy', 'matplotlib'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if IS_MAC:
    # macOS：生成 ExifCraft.app（arm64，未签名），不做 UPX 压缩
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='ExifCraft',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch='arm64',
        codesign_identity=None,
        entitlements_file=None,
        icon=['build/icon.icns'],
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name='ExifCraft',
    )
    app = BUNDLE(
        coll,
        name='ExifCraft.app',
        icon='build/icon.icns',
        bundle_identifier='com.enderbull.exifcraft',
        version='2.2.1',
        info_plist={
            'CFBundleDisplayName': 'ExifCraft',
            'CFBundleShortVersionString': '2.2.1',
            'CFBundleVersion': '2.2.1',
            'LSMinimumSystemVersion': '11.0',
            'NSHighResolutionCapable': 'True',
            'NSRequiresAquaSystemAppearance': 'False',
            # 没有这几条说明，访问桌面/文档/下载/外置盘时系统会直接静默拒绝
            'NSDesktopFolderUsageDescription': 'ExifCraft 需要读取你选择的照片文件夹，才能显示缩略图与 EXIF。',
            'NSDocumentsFolderUsageDescription': 'ExifCraft 需要读取你选择的照片文件夹，才能显示缩略图与 EXIF。',
            'NSDownloadsFolderUsageDescription': 'ExifCraft 需要读取你选择的照片文件夹，才能显示缩略图与 EXIF。',
            'NSRemovableVolumesUsageDescription': 'ExifCraft 需要读取存储卡、移动硬盘里的照片。',
            'NSNetworkVolumesUsageDescription': 'ExifCraft 需要读取网络卷里的照片。',
            'NSHumanReadableCopyright': '© 2026 Plane Li',
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='ExifCraft',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=['build/icon.ico'],
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='ExifCraft',
    )
