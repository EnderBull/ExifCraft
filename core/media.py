"""目录扫描、缩略图生成与缓存、RAW 内嵌预览提取。"""
from __future__ import annotations

import hashlib
import io
import mmap
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps

from . import config, rawimg

Image.MAX_IMAGE_PIXELS = 400_000_000

JPG_EXT = {".jpg", ".jpeg", ".jpe", ".jfif"}
RAW_EXT = {".arw", ".srf", ".sr2", ".dng", ".raw", ".cr2", ".cr3", ".nef", ".orf", ".rw2", ".raf",
           ".pef", ".srw", ".rwl", ".3fr", ".iiq"}
# CR3 是 ISO BMFF 容器（非 TIFF），写入需重建整段盒结构，因此和其余 RAW 一样只读。
CR3_EXT = {".cr3"}
# RAF 容器 exifread 读不了，EXIF 要从文件头里的内嵌 JPEG 取。
RAF_EXT = {".raf"}
ALL_EXT = JPG_EXT | RAW_EXT

THUMB_MAX = 512
PREVIEW_MAX = 2560

_SEM = threading.Semaphore(max(4, min(12, (os.cpu_count() or 4))))


def kind_of(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in JPG_EXT:
        return "jpg"
    if ext in RAW_EXT:
        return "raw"
    return None


def is_photo(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in ALL_EXT


def is_cr3(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in CR3_EXT


def is_raf(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in RAF_EXT


# ---------------------------------------------------------------- 缩略图缓存

def _cache_key(path: str, variant: str) -> str:
    try:
        st = os.stat(path)
        sig = f"{os.path.abspath(path).lower()}|{int(st.st_mtime)}|{st.st_size}|{variant}"
    except OSError:
        sig = f"{path}|{variant}"
    return hashlib.sha1(sig.encode("utf-8", "ignore")).hexdigest()


def _cache_path(key: str) -> Path:
    d = config.cache_dir() / key[:2]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.jpg"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- RAW 预览

def extract_raw_preview(path: str) -> bytes | None:
    """取 RAW 里能解码的那段 JPEG（相机写入的最大一张），拿不到返回 None。

    各品牌容器差异见 core/rawimg.py：RAF 读头部偏移，NEF/PEF/3FR 走 TIFF IFD，
    CR3/RW2 靠 SOI/EOI 快扫；Phase One IIQ 只有未压缩 strip，没有 JPEG。
    """
    return rawimg.first_jpeg(path)


def _open_image(path: str, allow_raw_preview: bool = True, max_side: int = 2560) -> Image.Image | None:
    """打开图片。RAW 走内嵌预览，并按 max_side 让 JPEG 解码器在 DCT 域先降采样。"""
    if kind_of(path) == "raw":
        if not allow_raw_preview:
            return None
        got = rawimg.first_image(path, max_side)
        return got[0] if got else None
    try:
        img = Image.open(path)
        img.load()
        return img
    except Exception:
        return None


def _encode(img: Image.Image, max_side: int, quality: int) -> bytes:
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    longest = max(w, h)
    if longest > max_side:
        scale = max_side / longest
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def get_thumb(path: str) -> bytes:
    key = _cache_key(path, f"t{THUMB_MAX}")
    cache = _cache_path(key)
    if cache.exists():
        try:
            return cache.read_bytes()
        except OSError:
            pass
    with _SEM:
        img = _open_image(path, max_side=THUMB_MAX)
        if img is None:
            raise FileNotFoundError(path)
        data = _encode(img, THUMB_MAX, 82)
        img.close()
    try:
        _atomic_write(cache, data)
    except OSError:
        pass
    return data


def get_preview(path: str) -> bytes:
    key = _cache_key(path, f"p{PREVIEW_MAX}")
    cache = _cache_path(key)
    if cache.exists():
        try:
            return cache.read_bytes()
        except OSError:
            pass
    with _SEM:
        img = _open_image(path, max_side=PREVIEW_MAX)
        if img is None:
            raise FileNotFoundError(path)
        data = _encode(img, PREVIEW_MAX, 90)
        img.close()
    try:
        _atomic_write(cache, data)
    except OSError:
        pass
    return data


def image_size(path: str) -> tuple[int, int] | None:
    """像素尺寸。RAW 优先取容器记录的传感器尺寸，取不到才退回内嵌预览尺寸。"""
    try:
        if kind_of(path) == "raw":
            size = rawimg.full_size(path)
            if size:
                return size
            return rawimg.preview_size(path)
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def preview_only_size(path: str) -> tuple[int, int] | None:
    """RAW 内嵌预览的尺寸（用于提示「此格式内嵌预览仅 640x480」这类场景）。"""
    return rawimg.preview_size(path)


def clear_cache() -> tuple[int, int]:
    """返回 (删除文件数, 释放字节数)。"""
    n = 0
    total = 0
    root = config.cache_dir()
    for p in root.rglob("*.jpg"):
        try:
            total += p.stat().st_size
            p.unlink()
            n += 1
        except OSError:
            pass
    return n, total


# ---------------------------------------------------------------- 目录扫描

def _win_roots() -> list[dict]:
    roots = []
    try:
        for d in os.listdrives():  # Python 3.12+
            try:
                if os.path.exists(d):
                    label = d.rstrip("\\/") or d
                    roots.append({"name": label, "path": d, "kind": "drive"})
            except OSError:
                pass
    except AttributeError:
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            d = f"{letter}:\\"
            if os.path.exists(d):
                roots.append({"name": f"{letter}:", "path": d, "kind": "drive"})
    return roots


def _mac_roots() -> list[dict]:
    """macOS 没有盘符：根目录 /、挂载卷 /Volumes/*、照片图库、常用文件夹。"""
    roots = [{"name": "启动磁盘", "path": "/", "kind": "drive"}]
    try:
        for name in sorted(os.listdir("/Volumes")):
            if name.startswith("."):
                continue
            p = os.path.join("/Volumes", name)
            if os.path.ismount(p) or os.path.isdir(p):
                roots.append({"name": name, "path": p, "kind": "drive"})
    except OSError:
        pass
    home = Path.home()
    pics = home / "Pictures"
    if pics.is_dir():
        roots.append({"name": "照片图库", "path": str(pics), "kind": "quick"})
    for name, sub in (("桌面", "Desktop"), ("下载", "Downloads"), ("文档", "Documents"),
                      ("影片", "Movies"), ("音乐", "Music"), ("个人", "Public")):
        p = home / sub
        if p.is_dir():
            roots.append({"name": name, "path": str(p), "kind": "quick"})
    return roots


def _drive_roots() -> list[dict]:
    if config.is_mac():
        return _mac_roots()

    roots = _win_roots()
    home = Path.home()
    for name, sub in (("图片", "Pictures"), ("桌面", "Desktop"), ("下载", "Downloads"), ("文档", "Documents")):
        p = home / sub
        if p.is_dir():
            roots.append({"name": name, "path": str(p), "kind": "quick"})
    return roots


def roots() -> list[dict]:
    return _drive_roots()


def _count_photos(dir_path: str, limit: int = 20000) -> int:
    n = 0
    try:
        with os.scandir(dir_path) as it:
            for i, e in enumerate(it):
                if i > limit:
                    break
                try:
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in ALL_EXT:
                        n += 1
                except OSError:
                    continue
    except OSError:
        pass
    return n


def _safe_stat(p: str) -> os.stat_result | None:
    try:
        return os.stat(p)
    except OSError:
        return None


SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "node_modules", ".git", "__pycache__"}


def _walk_photos(root: str, max_files: int = 20000, max_depth: int = 8) -> list[dict]:
    """递归收集照片，返回带相对路径的记录。"""
    out: list[dict] = []
    base_len = len(root.rstrip("\\/")) + 1  # 分隔符只在根末尾丢一个
    stack = [(root, 0)]
    truncated = False
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if len(out) >= max_files:
                        truncated = True
                        break
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth < max_depth and not e.name.startswith(".") and e.name not in SKIP_DIRS:
                                stack.append((e.path, depth + 1))
                        elif e.is_file(follow_symlinks=False):
                            ext = os.path.splitext(e.name)[1].lower()
                            if ext in ALL_EXT:
                                st = _safe_stat(e.path)
                                out.append({
                                    "name": e.name,
                                    "rel": e.path[base_len:],
                                    "path": e.path,
                                    "size": st.st_size if st else 0,
                                    "mtime": st.st_mtime if st else 0,
                                    "kind": "jpg" if ext in JPG_EXT else "raw",
                                })
                    except OSError:
                        continue
        except OSError:
            continue
        if truncated:
            break
    return out


def list_dir(path: str, sort: str = "name", want_counts: bool = True, recursive: bool = False) -> dict:
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise NotADirectoryError(path)

    dirs: list[dict] = []
    files: list[dict] = []

    try:
        entries = list(os.scandir(path))
    except OSError as e:
        raise PermissionError(str(e)) from e

    subdirs = []
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                subdirs.append(e)
            elif e.is_file(follow_symlinks=False) and not recursive:
                ext = os.path.splitext(e.name)[1].lower()
                if ext in ALL_EXT:
                    st = _safe_stat(e.path)
                    files.append({
                        "name": e.name,
                        "rel": e.name,
                        "path": e.path,
                        "size": st.st_size if st else 0,
                        "mtime": st.st_mtime if st else 0,
                        "kind": "jpg" if ext in JPG_EXT else "raw",
                    })
        except OSError:
            continue

    if recursive:
        files = _walk_photos(path)

    if want_counts:
        with ThreadPoolExecutor(max_workers=8) as ex:
            counts = list(ex.map(lambda d: _count_photos(d.path), subdirs))
    else:
        counts = [0] * len(subdirs)

    for d, c in zip(subdirs, counts):
        if d.name.startswith(".") or d.name in ("$RECYCLE.BIN", "System Volume Information"):
            continue
        dirs.append({"name": d.name, "path": d.path, "count": c})

    dirs.sort(key=lambda x: x["name"].lower())

    if sort == "date":
        files.sort(key=lambda x: x["mtime"], reverse=True)
    elif sort == "size":
        files.sort(key=lambda x: x["size"], reverse=True)
    elif sort == "type":
        files.sort(key=lambda x: (x["kind"], x["name"].lower()))
    else:
        files.sort(key=lambda x: x["name"].lower())

    parent = os.path.dirname(path)
    if config.norm_key(parent) == config.norm_key(path):
        parent = ""

    return {
        "path": path,
        "parent": parent,
        "name": os.path.basename(path) or path,
        "dirs": dirs,
        "files": files,
        "total_photos": len(files),
        "recursive": recursive,
    }