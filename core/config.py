"""配置与路径管理。"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

APP_NAME = "ExifCraft"

_LOCK = threading.Lock()


def is_mac() -> bool:
    return sys.platform == "darwin"


def is_win() -> bool:
    return os.name == "nt"


def appdata_dir() -> Path:
    """配置与备份目录。Windows 用 %LOCALAPPDATA%，macOS 用 ~/Library/Application Support。"""
    if is_mac():
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    p = base / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    """缩略图缓存。macOS 按惯例放 ~/Library/Caches，可被系统自动清理。"""
    p = (Path.home() / "Library" / "Caches" / APP_NAME) if is_mac() else (appdata_dir() / "cache")
    p.mkdir(parents=True, exist_ok=True)
    return p


def norm_key(path: str) -> str:
    """路径比较键。Windows 与 macOS 默认卷都不区分大小写，Linux 区分。

    os.path.normcase 在 POSIX 上是空操作，直接用会让 macOS 上
    IMG_1.JPG / img_1.jpg 被当成两个不同文件（可能互相覆盖）。
    """
    p = os.path.abspath(path)
    if is_win():
        return os.path.normcase(p)
    return p.lower() if is_mac() else p


def backup_dir() -> Path:
    p = appdata_dir() / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return appdata_dir() / "config.json"


DEFAULTS = {
    "amap_key": "",
    "amap_secret": "",
    "theme": "dark",
    "thumb_size": 168,
    "sort": "name",
    "recursive": False,
    "native_frame": False,
    "last_dir": "",
    "sidebar_width": 248,
    "inspector_width": 320,
    "exiftool_path": "",
}


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            for k, v in stored.items():
                if k in DEFAULTS:
                    data[k] = v
    except Exception:
        pass
    return data


def save(patch: dict) -> dict:
    with _LOCK:
        data = load()
        for k, v in patch.items():
            if k in DEFAULTS:
                data[k] = v
        tmp = config_path().with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path())
        return data
