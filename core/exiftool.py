"""可选的 ExifTool 支持：只用于**读取** CR3 等 RAW 的完整 EXIF，不参与写入。

程序不内置 ExifTool（内置要让体积多出约 27 MB，与轻量化目标冲突）。用户把
exiftool.exe 放到程序目录（或程序目录下的 exiftool* 子目录），或在设置里
指定路径后，CR3 的元数据就交给 ExifTool 读；找不到时一切照常，改由内置的
iso_bmff + exifread 解析器读取。

所有 RAW（含 CR3）始终只读，ExifTool 只读不写。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from . import config

CREATE_NO_WINDOW = 0x08000000

EXE_NAMES = ("exiftool.exe", "exiftool")

_cache: dict = {"path": None, "ver": None, "checked": False}


def _roots() -> list[str]:
    """程序可能安放 exiftool 的目录（打包后就是程序自身所在的几个位置）。"""
    here = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, "frozen", False) else __file__))
    roots = [here, os.path.dirname(here)]
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        roots.insert(0, exe_dir)
        if config.is_mac():
            # .app 内部：Contents/MacOS（可执行文件）→ Contents → Contents/Resources
            contents = os.path.dirname(exe_dir)
            roots.append(os.path.join(contents, "Resources"))
            # 以及 .app 外面那层，方便用户把 exiftool 放在 ExifCraft.app 旁边
            roots.append(os.path.dirname(contents))
    return roots


def _candidates():
    """按优先级列出可能的 exiftool 路径：设置里指定的 > 程序目录（含其子文件夹）。

    刻意不查系统 PATH：只有当用户把 exiftool.exe 放到程序旁边（或在设置里
    明确指定）时才会调用，避免机器上别的工具链带的 exiftool 被莫名用上。
    """
    cfg = (config.load().get("exiftool_path") or "").strip()
    if cfg:
        yield cfg
    for root in _roots():
        for name in EXE_NAMES:
            yield os.path.join(root, name)
        # 也认官方压缩包的原名文件夹：程序目录\\exiftool-13.59_64\\exiftool.exe
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            if not n.lower().startswith("exiftool"):
                continue
            sub = os.path.join(root, n)
            if not os.path.isdir(sub):
                continue
            for name in EXE_NAMES:
                yield os.path.join(sub, name)


def source_of(path: str | None) -> str:
    """说明这个 exiftool.exe 是从哪儿找到的（设置页会显示）。"""
    if not path:
        return ""
    cfg = (config.load().get("exiftool_path") or "").strip()
    if cfg and config.norm_key(cfg) == config.norm_key(path):
        return "设置中指定的路径"
    for root in _roots():
        if config.norm_key(path).startswith(config.norm_key(root) + os.sep):
            return "程序目录"
    return "自动查找"


def find() -> str | None:
    if _cache["checked"]:
        return _cache["path"]
    _cache["checked"] = True
    for p in _candidates():
        try:
            if p and os.path.isfile(p):
                _cache["path"] = p
                break
        except OSError:
            continue
    return _cache["path"]


def reset() -> None:
    _cache.update({"path": None, "ver": None, "checked": False})


def _run(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    exe = find()
    if not exe:
        return -1, "", "未找到 ExifTool"
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    try:
        r = subprocess.run([exe] + args, capture_output=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return -1, "", "ExifTool 超时"
    except OSError as e:
        return -1, "", str(e)
    return (r.returncode,
            r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def version() -> str | None:
    if _cache["ver"]:
        return _cache["ver"]
    if not find():
        return None
    rc, out, _ = _run(["-ver"], timeout=20)
    if rc == 0 and out.strip():
        _cache["ver"] = out.strip()
        return _cache["ver"]
    return None


def status() -> dict:
    """设置页用它显示「是否已调用」以及来源。"""
    path = find()
    return {
        "available": bool(path),
        "path": path or "",
        "version": (version() or "") if path else "",
        "source": source_of(path),
        # ExifTool 在本程序里只负责读取，任何 RAW 都不会被写入
        "readonly": True,
    }


# ---------------------------------------------------------------- 只读

def read_tags(path: str, tags: list[str] | None = None) -> dict:
    """用 ExifTool 读指定标签，返回 {标签: 值}（已转成 JSON 友好类型）。"""
    args = ["-j", "-q", "-m", "-n", "-charset", "filename=UTF8", "-charset", "exif=UTF8"]
    for t in (tags or ["EXIF:all", "GPS:all"]):
        args.append("-" + t)
    args.append(path)
    rc, out, err = _run(args)
    if rc != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    if not data:
        return {}
    return data[0]
