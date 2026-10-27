"""批量重命名、缩放导出、任务进度。"""
from __future__ import annotations

import io
import os
import re
import threading
import time
import uuid
from pathlib import Path

import exifread
import piexif
from PIL import Image, ImageOps

from . import exifio, media

# ---------------------------------------------------------------- 任务系统

_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}


def new_job(kind: str, total: int) -> dict:
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "kind": kind, "total": total, "done": 0,
        "status": "running", "message": "", "result": None,
        "errors": [], "started": time.time(), "cancel": False,
    }
    with _LOCK:
        JOBS[jid] = job
    return job


def get_job(jid: str) -> dict | None:
    with _LOCK:
        j = JOBS.get(jid)
        if not j:
            return None
        return {k: v for k, v in j.items() if k != "cancel"}


def cancel_job(jid: str) -> bool:
    with _LOCK:
        j = JOBS.get(jid)
        if j:
            j["cancel"] = True
            return True
    return False


def _gc_jobs() -> None:
    now = time.time()
    with _LOCK:
        for k in [k for k, v in JOBS.items() if now - v["started"] > 3600]:
            JOBS.pop(k, None)


# ---------------------------------------------------------------- 轻量 EXIF

def quick_meta(path: str) -> dict:
    out = {"dt": None, "make": "", "model": "", "lens": "", "iso": None, "focal": None, "fnum": None, "shutter": None}
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False, strict=False)
    except Exception:
        return out

    def g(*names):
        for n in names:
            if n in tags:
                return tags[n]
        return None

    t = g("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime")
    if t:
        s = t.printable.strip()
        out["dt"] = s
    for key, names in (("make", ("Image Make",)), ("model", ("Image Model",)), ("lens", ("EXIF LensModel",))):
        t = g(*names)
        if t:
            out[key] = t.printable.strip()
    t = g("EXIF ISOSpeedRatings", "EXIF PhotographicSensitivity")
    if t:
        try:
            out["iso"] = int(float(t.values[0]))
        except Exception:
            pass
    t = g("EXIF FocalLength")
    if t:
        try:
            out["focal"] = float(t.values[0])
        except Exception:
            pass
    t = g("EXIF FNumber")
    if t:
        try:
            out["fnum"] = float(t.values[0])
        except Exception:
            pass
    t = g("EXIF ExposureTime")
    if t:
        try:
            out["shutter"] = float(t.values[0])
        except Exception:
            pass
    return out


def _dt_parts(raw: str | None, fallback_ts: float) -> dict:
    if raw:
        m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", raw)
        if m:
            y, mo, d, h, mi, s = m.groups()
            return {"yyyy": y, "mm": mo, "dd": d, "HH": h, "MM": mi, "SS": s}
    t = time.localtime(fallback_ts)
    return {
        "yyyy": f"{t.tm_year:04d}", "mm": f"{t.tm_mon:02d}", "dd": f"{t.tm_mday:02d}",
        "HH": f"{t.tm_hour:02d}", "MM": f"{t.tm_min:02d}", "SS": f"{t.tm_sec:02d}",
    }


VAR_RE = re.compile(r"\{(\w+)(?::(\d+))?\}")


def _render(template: str, ctx: dict) -> str:
    def rep(m):
        name = m.group(1)
        width = m.group(2)
        val = ctx.get(name, "")
        if name == "seq":
            val = str(ctx.get("_seq", 1))
            if width:
                val = val.zfill(int(width))
        return str(val)

    return VAR_RE.sub(rep, template)


def rename_preview(paths: list[str], template: str, start: int = 1) -> list[dict]:
    out = []
    for i, p in enumerate(paths):
        base = os.path.basename(p)
        stem, ext = os.path.splitext(base)
        meta = quick_meta(p)
        parts = _dt_parts(meta["dt"], os.path.getmtime(p) if os.path.exists(p) else time.time())
        ctx = {
            "name": stem,
            "ext": ext.lstrip("."),
            "date": parts["yyyy"] + parts["mm"] + parts["dd"],
            "time": parts["HH"] + parts["MM"] + parts["SS"],
            "camera": meta["model"] or meta["make"] or "Unknown",
            "model": meta["model"] or "",
            "make": meta["make"] or "",
            "lens": meta["lens"] or "",
            "iso": meta["iso"] if meta["iso"] is not None else "",
            "focal": f"{meta['focal']:g}" if meta["focal"] else "",
            "fnum": f"{meta['fnum']:g}" if meta["fnum"] else "",
            "shutter": f"{meta['shutter']:g}" if meta["shutter"] else "",
            "_seq": start + i,
            **parts,
        }
        new_stem = _render(template, ctx).strip().strip(".")
        new_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", new_stem) or stem
        new_name = f"{new_stem}{ext}"
        out.append({
            "src": p,
            "dir": os.path.dirname(p),
            "newName": new_name,
            "oldName": base,
            "changed": new_name != base,
        })

    # 冲突检测
    seen: dict[str, int] = {}
    for item in out:
        key = os.path.normcase(os.path.join(item["dir"], item["newName"]))
        seen[key] = seen.get(key, 0) + 1

    for item in out:
        target = os.path.join(item["dir"], item["newName"])
        key = os.path.normcase(target)
        conflict = ""
        if seen[key] > 1:
            conflict = "重名"
        elif os.path.exists(target) and os.path.normcase(target) != os.path.normcase(item["src"]):
            conflict = "已存在"
        item["conflict"] = conflict
    return out


def rename_apply(items: list[dict]) -> dict:
    """两阶段改名，避免循环冲突。"""
    job = new_job("rename", len(items))
    undo = []
    errors = []

    stage = []
    for it in items:
        src = it["src"]
        dst = os.path.join(it["dir"], it["newName"])
        if os.path.normcase(src) == os.path.normcase(dst):
            job["done"] += 1
            continue
        stage.append((src, dst))

    tmp_pairs = []
    for idx, (src, dst) in enumerate(stage):
        if not os.path.exists(src):
            errors.append({"path": src, "error": "文件不存在"})
            continue
        tmp = os.path.join(os.path.dirname(src), f".pm_rename_{uuid.uuid4().hex[:8]}_{idx}")
        try:
            os.rename(src, tmp)
            tmp_pairs.append((tmp, dst, src))
        except OSError as e:
            errors.append({"path": src, "error": str(e)})

    for tmp, dst, src in tmp_pairs:
        if job["cancel"]:
            try:
                os.rename(tmp, src)
            except OSError:
                pass
            continue
        try:
            if os.path.exists(dst):
                raise FileExistsError(f"目标已存在：{os.path.basename(dst)}")
            os.rename(tmp, dst)
            undo.append({"from": dst, "to": src})
        except OSError as e:
            errors.append({"path": src, "error": str(e)})
            try:
                os.rename(tmp, src)
            except OSError:
                pass
        job["done"] += 1

    job["status"] = "done"
    job["errors"] = errors
    job["result"] = {"renamed": len(undo), "undo": undo}
    return job


# ---------------------------------------------------------------- 导出

def _tags(*pairs) -> list[int]:
    out = []
    for ifd, name in pairs:
        v = getattr(ifd, name, None)
        if v is not None:
            out.append(v)
    return out


PARAM_EXIF = _tags(
    (piexif.ExifIFD, "DateTimeOriginal"), (piexif.ExifIFD, "DateTimeDigitized"),
    (piexif.ExifIFD, "FNumber"), (piexif.ExifIFD, "ApertureValue"), (piexif.ExifIFD, "MaxApertureValue"),
    (piexif.ExifIFD, "ExposureTime"), (piexif.ExifIFD, "ShutterSpeedValue"),
    (piexif.ExifIFD, "ISOSpeedRatings"), (piexif.ExifIFD, "PhotographicSensitivity"),
    (piexif.ExifIFD, "FocalLength"), (piexif.ExifIFD, "FocalLengthIn35mmFilm"),
    (piexif.ExifIFD, "ExposureBiasValue"), (piexif.ExifIFD, "WhiteBalance"),
    (piexif.ExifIFD, "ExposureProgram"), (piexif.ExifIFD, "MeteringMode"),
    (piexif.ExifIFD, "LensModel"), (piexif.ExifIFD, "LensMake"), (piexif.ExifIFD, "LensSpecification"),
)
PARAM_0TH = _tags(
    (piexif.ImageIFD, "Make"), (piexif.ImageIFD, "Model"),
    (piexif.ImageIFD, "DateTime"), (piexif.ImageIFD, "Software"),
)
AUTHOR_0TH = _tags(
    (piexif.ImageIFD, "Artist"), (piexif.ImageIFD, "Copyright"),
    (piexif.ImageIFD, "ImageDescription"),
    (piexif.ImageIFD, "XPAuthor"), (piexif.ImageIFD, "XPComment"),
    (piexif.ImageIFD, "XPKeywords"), (piexif.ImageIFD, "XPSubject"), (piexif.ImageIFD, "XPTitle"),
)


def _filter_exif(exif_bytes: bytes, params: bool, author: bool, gps: bool) -> bytes | None:
    """按分组裁剪 EXIF；Orientation 归一为 1（像素已摆正）。"""
    try:
        d = piexif.load(exif_bytes)
    except Exception:
        return exif_bytes

    for k in ("0th", "Exif", "GPS", "1st", "Interop"):
        d.setdefault(k, {})

    if not params:
        for t in PARAM_EXIF:
            d["Exif"].pop(t, None)
        for t in PARAM_0TH:
            d["0th"].pop(t, None)
    if not author:
        for t in AUTHOR_0TH:
            d["0th"].pop(t, None)
    if not gps:
        d["GPS"] = {}

    d["0th"][piexif.ImageIFD.Orientation] = 1

    if not d["0th"] and not d["Exif"] and not d["GPS"]:
        return None
    try:
        return piexif.dump(d)
    except Exception:
        return exif_bytes


def export_images(paths: list[str], out_dir: str, long_edge: int = 3840,
                  quality: int = 85, keep: dict | None = None, job: dict | None = None) -> dict:
    job = job or new_job("export", len(paths))
    keep = keep or {}
    keep_params = bool(keep.get("params", True))
    keep_author = bool(keep.get("author", True))
    keep_gps = bool(keep.get("gps", True))
    keep_icc = bool(keep.get("icc", True))
    os.makedirs(out_dir, exist_ok=True)
    written = []

    for p in paths:
        if job["cancel"]:
            break
        if media.kind_of(p) != "jpg":
            job["errors"].append({"path": p, "error": "仅支持 JPG 导出"})
            job["done"] += 1
            continue
        try:
            with Image.open(p) as im:
                icc = im.info.get("icc_profile")
                exif_bytes = im.info.get("exif")
                img = ImageOps.exif_transpose(im)
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                w, h = img.size
                longest = max(w, h)
                if longest > long_edge:
                    scale = long_edge / longest
                    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

                stem, ext = os.path.splitext(os.path.basename(p))
                target = os.path.join(out_dir, f"{stem}{ext.lower()}")
                n = 1
                while os.path.exists(target):
                    target = os.path.join(out_dir, f"{stem}_{n}{ext.lower()}")
                    n += 1

                save_kw = {"quality": quality, "optimize": True, "progressive": True}
                if keep_icc and icc:
                    save_kw["icc_profile"] = icc
                if exif_bytes and (keep_params or keep_author or keep_gps):
                    filtered = _filter_exif(exif_bytes, keep_params, keep_author, keep_gps)
                    if filtered:
                        save_kw["exif"] = filtered

                img.save(target, "JPEG", **save_kw)
                written.append(target)
        except Exception as e:
            job["errors"].append({"path": p, "error": f"{type(e).__name__}: {e}"})
        job["done"] += 1

    job["status"] = "cancelled" if job["cancel"] else "done"
    job["result"] = {"written": len(written), "outDir": out_dir, "files": written[:200]}
    return job
