"""配置与路径管理。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

APP_NAME = "ExifCraft"

_LOCK = threading.Lock()


def appdata_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = appdata_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


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
