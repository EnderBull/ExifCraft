"""操作历史：把撤销所需的信息写到硬盘，重启程序后仍然可用。

目前记录两类操作：
- rename：新旧文件名映射，撤销时反向改名；
- exif：只记录备份 id，撤销走 backups/*.json 的回滚逻辑。

文件位置与配置同目录（Windows %LOCALAPPDATA%\\ExifCraft\\history.json，
macOS ~/Library/Application Support/ExifCraft/history.json），只保留最近
HISTORY_KEEP 条，避免无限增长。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

from . import config

HISTORY_KEEP = 50
_LOCK = threading.Lock()


def _path():
    return config.appdata_dir() / "history.json"


def _read() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except Exception:
        pass
    return {"items": []}


def _write(data: dict) -> None:
    fp = _path()
    tmp = fp.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, fp)


def _prune(items: list[dict], keep: int = HISTORY_KEEP) -> list[dict]:
    """按时间倒序保留最近 keep 条。"""
    items.sort(key=lambda x: x.get("time", 0), reverse=True)
    return items[:keep]


def add(kind: str, summary: str, undo: list[dict] | None = None,
        backup_id: str = "", extra: dict | None = None) -> dict:
    """追加一条操作记录，返回这条记录（含 id）。"""
    item = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "time": time.time(),
        "summary": summary,
        "undo": undo or [],
        "backupId": backup_id,
        "undone": False,
    }
    if extra:
        item.update(extra)
    with _LOCK:
        data = _read()
        data["items"] = _prune([item] + data["items"])
        _write(data)
    return item


def list_items(limit: int = HISTORY_KEEP) -> list[dict]:
    items = _read().get("items", [])
    items.sort(key=lambda x: x.get("time", 0), reverse=True)
    return items[:limit]


def get(item_id: str) -> dict | None:
    if not item_id:
        return None
    for it in _read().get("items", []):
        if it.get("id") == item_id:
            return it
    return None


def latest(kind: str | None = None) -> dict | None:
    for it in list_items():
        if kind and it.get("kind") != kind:
            continue
        if not it.get("undone"):
            return it
    return None


def mark_undone(item_id: str) -> None:
    if not item_id:
        return
    with _LOCK:
        data = _read()
        for it in data.get("items", []):
            if it.get("id") == item_id:
                it["undone"] = True
                break
        _write(data)
