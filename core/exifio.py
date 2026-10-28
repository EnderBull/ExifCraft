"""EXIF 读取（JPG / RAW / CR3）与写入、备份与回滚。

JPG 走 piexif 做 EXIF 段无损替换。所有 RAW（含 CR3）一律**只读**：既不写文件，
也不做备份；ExifTool 若存在，只用来把 CR3 的 EXIF 读得更完整。
"""
from __future__ import annotations

import base64
import io
import json
import os
import time
import uuid
from fractions import Fraction

import exifread
import piexif

from . import config, exiftool, iso_bmff, media

# ---------------------------------------------------------------- 展示元数据

WB_MAP = {0: "自动", 1: "手动"}
PROGRAM_MAP = {
    0: "未定义", 1: "手动", 2: "程序自动", 3: "光圈优先", 4: "快门优先",
    5: "创意程序", 6: "动作程序", 7: "人像", 8: "风景",
}
METER_MAP = {
    0: "未知", 1: "平均测光", 2: "中央重点", 3: "点测光", 4: "多分区", 5: "评价测光", 6: "局部测光",
}

FIELD_DEFS = [
    # key, label, group, editable
    ("DateTimeOriginal", "拍摄时间", "shoot", True),
    ("Make", "相机厂商", "shoot", True),
    ("Model", "相机型号", "shoot", True),
    ("LensModel", "镜头", "shoot", True),
    ("FNumber", "光圈", "shoot", True),
    ("ExposureTime", "快门", "shoot", True),
    ("ISO", "ISO", "shoot", True),
    ("FocalLength", "焦距", "shoot", True),
    ("FocalLength35", "等效焦距", "shoot", True),
    ("ExposureBias", "曝光补偿", "shoot", True),
    ("WhiteBalance", "白平衡", "shoot", True),
    ("ExposureProgram", "曝光程序", "shoot", True),
    ("MeteringMode", "测光模式", "shoot", True),
    ("Artist", "作者", "author", True),
    ("Copyright", "版权", "author", True),
    ("ImageDescription", "说明", "author", True),
]


def _tag(tags: dict, *names):
    for n in names:
        t = tags.get(n)
        if t is not None:
            return t
    return None


def _first_num(tag) -> float | None:
    try:
        v = tag.values
        if isinstance(v, (list, tuple)):
            if not v:
                return None
            v = v[0]
        if isinstance(v, Fraction):
            return float(v)
        if isinstance(v, (int, float)):
            return float(v)
        return None
    except Exception:
        return None


def _fmt_shutter(v: float) -> str:
    if v <= 0:
        return ""
    if v >= 1:
        return f"{v:g} s"
    denom = round(1 / v)
    if denom <= 0:
        return f"{v:g} s"
    return f"1/{denom} s"


def _parse_dt(text: str) -> str:
    """'2024:05:01 10:20:30' -> '2024-05-01 10:20:30'"""
    if not text:
        return ""
    t = text.strip().replace(":", "-", 2)
    return t[:19]


def _dt_to_exif(text: str) -> str:
    """'2024-05-01T10:20' 或 '2024-05-01 10:20:30' -> '2024:05:01 10:20:30'"""
    s = (text or "").strip().replace("T", " ").replace("-", ":", 2)
    if len(s) == 16:
        s += ":00"
    return s[:19]


class _EtTag:
    """把 ExifTool 的 JSON 值包装成 exifread 风格的对象，让下面的取值逻辑
    能在「exifread 解析」与「ExifTool 解析」两条路上共用同一份代码。"""

    __slots__ = ("printable", "values")

    def __init__(self, value):
        if isinstance(value, (list, tuple)):
            self.values = list(value)
            self.printable = " ".join(str(x) for x in value)
        else:
            self.values = [value]
            self.printable = str(value)


# ExifTool 的裸标签名 -> exifread 的 key（只列 read_meta 会用到的）
_ET_TO_EXIFREAD = {
    "Make": "Image Make",
    "Model": "Image Model",
    "Artist": "Image Artist",
    "Copyright": "Image Copyright",
    "ImageDescription": "Image ImageDescription",
    "ImageWidth": "Image ImageWidth",
    "ImageHeight": "Image ImageLength",
    "DateTimeOriginal": "EXIF DateTimeOriginal",
    "DateTimeDigitized": "EXIF DateTimeDigitized",
    "LensModel": "EXIF LensModel",
    "LensSpecification": "EXIF LensSpecification",
    "FNumber": "EXIF FNumber",
    "ExposureTime": "EXIF ExposureTime",
    "ISO": "EXIF ISOSpeedRatings",
    "PhotographicSensitivity": "EXIF PhotographicSensitivity",
    "FocalLength": "EXIF FocalLength",
    "FocalLengthIn35mmFormat": "EXIF FocalLengthIn35mmFilm",
    "ExposureCompensation": "EXIF ExposureBiasValue",
    "WhiteBalance": "EXIF WhiteBalance",
    "ExposureProgram": "EXIF ExposureProgram",
    "MeteringMode": "EXIF MeteringMode",
    "ExifImageWidth": "EXIF ExifImageWidth",
    "ExifImageHeight": "EXIF ExifImageLength",
}


def _read_cr3_tags_via_exiftool(path: str) -> tuple[dict, dict]:
    """ExifTool 对 CR3 的覆盖比内置解析器全（CMT1/2/3/4 都会合并），
    有 ExifTool 时优先走这条路。"""
    raw = exiftool.read_tags(path, ["EXIF:all", "GPS:all"])
    tags: dict = {}
    for name, key in _ET_TO_EXIFREAD.items():
        if name in raw:
            tags[key] = _EtTag(raw[name])
    gps = {"lat": None, "lng": None, "alt": None}
    try:
        if raw.get("GPSLatitude") is not None and raw.get("GPSLongitude") is not None:
            lat = float(raw["GPSLatitude"])
            lng = float(raw["GPSLongitude"])
            if raw.get("GPSLatitudeRef") in ("S", "s"):
                lat = -abs(lat)
            if raw.get("GPSLongitudeRef") in ("W", "w"):
                lng = -abs(lng)
            if not (lat == 0 and lng == 0):
                gps["lat"] = round(lat, 7)
                gps["lng"] = round(lng, 7)
        if raw.get("GPSAltitude") is not None:
            alt = float(raw["GPSAltitude"])
            if str(raw.get("GPSAltitudeRef")) in ("1", "-1"):
                alt = -abs(alt)
            gps["alt"] = round(alt, 1)
    except (TypeError, ValueError):
        pass
    return tags, gps


def _read_cr3_tags(path: str) -> tuple[dict, dict]:
    """CR3 的 EXIF 分散在 CMT1（IFD0）与 CMT2（ExifIFD）两个盒里，各自是一段
    独立 TIFF，必须分别交给 exifread 解析再合并。CMT4 的 GPS IFD 用 exifread
    解析不出来，交给 iso_bmff.parse_gps 手工解。

    返回 (tags, gps)。tags 同时补一份 "EXIF xxx" 别名，让下面的取值逻辑
    与 JPG 共用同一套 key。
    """
    cmts = iso_bmff.read_cmt(path)
    merged: dict = {}
    for name in ("CMT1", "CMT2"):
        blob = cmts.get(name)
        if not blob:
            continue
        try:
            part = exifread.process_file(io.BytesIO(blob), details=False, strict=False)
        except Exception:
            continue
        for k, v in part.items():
            merged.setdefault(k, v)
            if k.startswith("Image "):
                merged.setdefault("EXIF " + k[6:], v)
    return merged, iso_bmff.parse_gps(cmts.get("CMT4"))


def read_meta(path: str) -> dict:
    """返回 {fields: [...], edit: {...}, gps: {...}, file: {...}}"""
    kind = media.kind_of(path) or "jpg"
    tags: dict = {}
    cr3_gps: dict = {"lat": None, "lng": None, "alt": None}
    if media.is_cr3(path):
        if exiftool.find():
            try:
                tags, cr3_gps = _read_cr3_tags_via_exiftool(path)
            except Exception:
                tags, cr3_gps = {}, {"lat": None, "lng": None, "alt": None}
        if not tags:
            try:
                tags, cr3_gps = _read_cr3_tags(path)
            except Exception:
                tags = {}
    elif media.is_raf(path):
        # exifread 不认识 RAF 容器（会静默返回空字典），但文件头里的内嵌 JPEG
        # 自带完整 EXIF：取那段 JPEG 再解析，1 ms 就能拿到 71 个标签。
        try:
            blob = media.extract_raw_preview(path)
            if blob:
                tags = exifread.process_file(io.BytesIO(blob), details=False, strict=False)
        except Exception:
            tags = {}
    else:
        try:
            with open(path, "rb") as f:
                tags = exifread.process_file(f, details=False, strict=False)
        except Exception:
            tags = {}

    edit: dict = {}
    fields: list[dict] = []

    def put(key: str, label: str, group: str, display: str, raw=None, editable=True):
        edit[key] = raw
        fields.append({
            "key": key, "label": label, "group": group,
            "value": display, "editable": editable,
        })

    dt_tag = _tag(tags, "EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime")
    dt_disp = _parse_dt(dt_tag.printable if dt_tag else "")
    put("DateTimeOriginal", "拍摄时间", "shoot", dt_disp or "—", dt_disp)

    make = (_tag(tags, "Image Make").printable.strip() if _tag(tags, "Image Make") else "")
    model = (_tag(tags, "Image Model").printable.strip() if _tag(tags, "Image Model") else "")
    lens = (_tag(tags, "EXIF LensModel", "EXIF LensSpecification").printable.strip()
            if _tag(tags, "EXIF LensModel", "EXIF LensSpecification") else "")
    put("Make", "相机厂商", "shoot", make or "—", make)
    put("Model", "相机型号", "shoot", model or "—", model)
    put("LensModel", "镜头", "shoot", lens or "—", lens)

    fnum = _first_num(_tag(tags, "EXIF FNumber"))
    put("FNumber", "光圈", "shoot", f"f/{fnum:g}" if fnum else "—", fnum)

    shut = _first_num(_tag(tags, "EXIF ExposureTime"))
    put("ExposureTime", "快门", "shoot", _fmt_shutter(shut) if shut else "—", shut)

    iso = _first_num(_tag(tags, "EXIF ISOSpeedRatings", "EXIF PhotographicSensitivity"))
    put("ISO", "ISO", "shoot", f"{int(iso)}" if iso else "—", int(iso) if iso else None)

    focal = _first_num(_tag(tags, "EXIF FocalLength"))
    put("FocalLength", "焦距", "shoot", f"{focal:g} mm" if focal else "—", focal)

    f35 = _first_num(_tag(tags, "EXIF FocalLengthIn35mmFilm"))
    put("FocalLength35", "等效焦距", "shoot", f"{int(f35)} mm" if f35 else "—", int(f35) if f35 else None)

    bias = _first_num(_tag(tags, "EXIF ExposureBiasValue"))
    put("ExposureBias", "曝光补偿", "shoot",
        f"{bias:+.1f} EV" if bias is not None else "—", bias)

    wb = _first_num(_tag(tags, "EXIF WhiteBalance"))
    put("WhiteBalance", "白平衡", "shoot", WB_MAP.get(int(wb), "—") if wb is not None else "—",
        int(wb) if wb is not None else None)

    prog = _first_num(_tag(tags, "EXIF ExposureProgram"))
    put("ExposureProgram", "曝光程序", "shoot",
        PROGRAM_MAP.get(int(prog), str(int(prog))) if prog is not None else "—",
        int(prog) if prog is not None else None)

    meter = _first_num(_tag(tags, "EXIF MeteringMode"))
    put("MeteringMode", "测光模式", "shoot",
        METER_MAP.get(int(meter), str(int(meter))) if meter is not None else "—",
        int(meter) if meter is not None else None)

    artist = (_tag(tags, "Image Artist").printable if _tag(tags, "Image Artist") else "")
    copyright_ = (_tag(tags, "Image Copyright").printable if _tag(tags, "Image Copyright") else "")
    desc = (_tag(tags, "Image ImageDescription").printable if _tag(tags, "Image ImageDescription") else "")
    put("Artist", "作者", "author", artist.strip() or "—", artist.strip())
    put("Copyright", "版权", "author", copyright_.strip() or "—", copyright_.strip())
    put("ImageDescription", "说明", "author", desc.strip() or "—", desc.strip())

    gps = {"lat": None, "lng": None, "alt": None}
    if cr3_gps["lat"] is not None:
        gps = dict(cr3_gps)
    lat_tag = _tag(tags, "GPS GPSLatitude")
    lng_tag = _tag(tags, "GPS GPSLongitude")
    if gps["lat"] is None and lat_tag and lng_tag:
        try:
            def dms(t):
                vals = [float(x) for x in t.values]
                while len(vals) < 3:
                    vals.append(0.0)
                return vals[0] + vals[1] / 60 + vals[2] / 3600

            lat = dms(lat_tag)
            lng = dms(lng_tag)
            lat_ref = _tag(tags, "GPS GPSLatitudeRef")
            lng_ref = _tag(tags, "GPS GPSLongitudeRef")
            if lat_ref and lat_ref.printable.strip().upper().startswith("S"):
                lat = -lat
            if lng_ref and lng_ref.printable.strip().upper().startswith("W"):
                lng = -lng
            gps["lat"] = round(lat, 7)
            gps["lng"] = round(lng, 7)
            alt_tag = _tag(tags, "GPS GPSAltitude")
            if alt_tag:
                alt = _first_num(alt_tag)
                alt_ref = _tag(tags, "GPS GPSAltitudeRef")
                if alt is not None:
                    if alt_ref and int(alt_ref.values[0]) == 1:
                        alt = -alt
                    gps["alt"] = round(alt, 1)
        except Exception:
            pass

    # 尺寸取「实际像素」：容器记录的传感器尺寸，与 EXIF 里的大图尺寸取较大者。
    # 各品牌这信息放的位置不一样（CR3 在 CMT1、IIQ 在 ExifIFD、3FR/NEF/PEF 在 IFD），
    # 单独信任何一处都会得到预览尺寸或反了的宽高。
    size = media.image_size(path)
    # 同一个文件里往往同时有「缩略图尺寸」和「大图尺寸」，按面积取最大：
    # IIQ 的 Image ImageWidth 是 640（缩略图），EXIF ExifImageWidth 才是 14204。
    best_area = size[0] * size[1] if size else 0
    for wk, hk in (("Image ImageWidth", "Image ImageLength"),
                   ("EXIF ExifImageWidth", "EXIF ExifImageLength")):
        wv, hv = _first_num(_tag(tags, wk)), _first_num(_tag(tags, hk))
        if not (wv and hv) or wv <= 0 or hv <= 0:
            continue
        if wv * hv > best_area:
            size, best_area = (int(wv), int(hv)), wv * hv
    try:
        st = os.stat(path)
        fsize = st.st_size
        fmtime = st.st_mtime
    except OSError:
        fsize, fmtime = 0, 0

    # RAW 一律只读（含 CR3）；ExifTool 只用于读取，不参与写入
    writable = kind == "jpg"
    file_info = {
        "name": os.path.basename(path),
        "path": path,
        "size": fsize,
        "mtime": fmtime,
        "kind": kind,
        "ext": os.path.splitext(path)[1].lower(),
        "width": size[0] if size else 0,
        "height": size[1] if size else 0,
        "writable": writable,
        "writer": "piexif" if kind == "jpg" else "",
        "et_read": bool(media.is_cr3(path) and exiftool.find()),
    }
    if kind == "raw":
        # IIQ 这类容器里最大的内嵌图只有 640x480，前端据此提示一句
        pv = media.preview_only_size(path)
        if pv:
            file_info["preview_width"] = pv[0]
            file_info["preview_height"] = pv[1]

    return {"fields": fields, "edit": edit, "gps": gps, "file": file_info}


# ---------------------------------------------------------------- 写入

def _ratio(v: float, den: int = 100) -> tuple[int, int]:
    return (int(round(v * den)), den)


def _sratio(v: float, den: int = 100) -> tuple[int, int]:
    return (int(round(v * den)), den)


def _dms(v: float) -> tuple:
    v = abs(v)
    d = int(v)
    m_f = (v - d) * 60
    m = int(m_f)
    s = (m_f - m) * 60
    return ((d, 1), (m, 1), (int(round(s * 10000)), 10000))


def _load_exif(path: str) -> dict:
    try:
        d = piexif.load(path)
    except Exception:
        d = {}
    for k in ("0th", "Exif", "GPS", "1st", "Interop"):
        d.setdefault(k, {})
    d.setdefault("thumbnail", None)
    return d


def _apply(d: dict, changes: dict) -> None:
    z, e, g = d["0th"], d["Exif"], d["GPS"]

    def has(k):
        return k in changes and changes[k] is not None

    if has("DateTimeOriginal"):
        s = _dt_to_exif(changes["DateTimeOriginal"]).encode("ascii", "ignore")
        e[piexif.ExifIFD.DateTimeOriginal] = s
        e[piexif.ExifIFD.DateTimeDigitized] = s
        z[piexif.ImageIFD.DateTime] = s
    if has("Make"):
        z[piexif.ImageIFD.Make] = str(changes["Make"]).encode("utf-8")
    if has("Model"):
        z[piexif.ImageIFD.Model] = str(changes["Model"]).encode("utf-8")
    if has("LensModel"):
        e[piexif.ExifIFD.LensModel] = str(changes["LensModel"]).encode("utf-8")
    if has("Artist"):
        z[piexif.ImageIFD.Artist] = str(changes["Artist"]).encode("utf-8")
    if has("Copyright"):
        z[piexif.ImageIFD.Copyright] = str(changes["Copyright"]).encode("utf-8")
    if has("ImageDescription"):
        z[piexif.ImageIFD.ImageDescription] = str(changes["ImageDescription"]).encode("utf-8")
    if has("FNumber"):
        e[piexif.ExifIFD.FNumber] = _ratio(float(changes["FNumber"]))
        e[piexif.ExifIFD.ApertureValue] = _ratio(float(changes["FNumber"]))
    if has("ExposureTime"):
        v = float(changes["ExposureTime"])
        if v >= 1:
            e[piexif.ExifIFD.ExposureTime] = (int(round(v * 10)), 10)
        else:
            e[piexif.ExifIFD.ExposureTime] = (1, max(1, int(round(1 / v))))
    if has("ISO"):
        e[piexif.ExifIFD.ISOSpeedRatings] = int(changes["ISO"])
    if has("FocalLength"):
        e[piexif.ExifIFD.FocalLength] = _ratio(float(changes["FocalLength"]))
    if has("FocalLength35"):
        e[piexif.ExifIFD.FocalLengthIn35mmFilm] = int(changes["FocalLength35"])
    if has("ExposureBias"):
        e[piexif.ExifIFD.ExposureBiasValue] = _sratio(float(changes["ExposureBias"]))
    if has("WhiteBalance"):
        e[piexif.ExifIFD.WhiteBalance] = int(changes["WhiteBalance"])
    if has("ExposureProgram"):
        e[piexif.ExifIFD.ExposureProgram] = int(changes["ExposureProgram"])
    if has("MeteringMode"):
        e[piexif.ExifIFD.MeteringMode] = int(changes["MeteringMode"])

    if has("GPSLat") and has("GPSLng"):
        lat = float(changes["GPSLat"])
        lng = float(changes["GPSLng"])
        if -90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0):
            g[piexif.GPSIFD.GPSVersionID] = (2, 3, 0, 0)
            g[piexif.GPSIFD.GPSLatitudeRef] = b"S" if lat < 0 else b"N"
            g[piexif.GPSIFD.GPSLatitude] = _dms(lat)
            g[piexif.GPSIFD.GPSLongitudeRef] = b"W" if lng < 0 else b"E"
            g[piexif.GPSIFD.GPSLongitude] = _dms(lng)
            alt = changes.get("GPSAlt")
            if alt is not None:
                alt = float(alt)
                g[piexif.GPSIFD.GPSAltitudeRef] = 1 if alt < 0 else 0
                g[piexif.GPSIFD.GPSAltitude] = (int(round(abs(alt) * 100)), 100)


def _extract_app1(path: str) -> bytes | None:
    """取出 JPEG 中完整的 EXIF APP1 负载（以 b'Exif\\x00\\x00' 开头）。"""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                m = marker[1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    continue
                if m == 0xD9:
                    return None
                ln = f.read(2)
                if len(ln) < 2:
                    return None
                length = int.from_bytes(ln, "big")
                payload = f.read(max(0, length - 2))
                if m == 0xE1 and payload[:6] == b"Exif\x00\x00":
                    return payload
                if m == 0xDA:
                    return None
    except Exception:
        return None


def _strip_app1(path: str) -> None:
    """删除 JPEG 中的 EXIF APP1 段（用于回滚到「原本没有 EXIF」的状态）。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"\xff\xd8":
        return
    out = bytearray(b"\xff\xd8")
    i = 2
    n = len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            out.extend(data[i:])
            break
        m = data[i + 1]
        if m == 0xD8:
            i += 2
            continue
        if m == 0xDA:
            out.extend(data[i:])
            break
        if i + 4 > n:
            out.extend(data[i:])
            break
        length = int.from_bytes(data[i + 2:i + 4], "big")
        seg = data[i:i + 2 + length]
        if m == 0xE1 and seg[4:10] == b"Exif\x00\x00":
            i += 2 + length
            continue
        out.extend(seg)
        i += 2 + length
    with open(path, "wb") as f:
        f.write(out)


def backup_files(paths: list[str]) -> dict:
    entries = []
    for p in paths:
        # 只有 JPG 会被写入，也只备份 JPG
        if media.kind_of(p) != "jpg":
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        seg = _extract_app1(p)
        entries.append({
            "path": p,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "exif": base64.b64encode(seg).decode("ascii") if seg else None,
        })
    bid = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    record = {"id": bid, "time": time.time(), "entries": entries}
    fp = config.backup_dir() / f"{bid}.json"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(record, f)
    return {"id": bid, "count": len(entries)}


def list_backups() -> list[dict]:
    out = []
    for p in sorted(config.backup_dir().glob("*.json"), reverse=True):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            out.append({
                "id": d.get("id", p.stem),
                "time": d.get("time", 0),
                "count": len(d.get("entries", [])),
            })
        except Exception:
            continue
    return out[:50]


def restore_backup(bid: str) -> dict:
    fp = config.backup_dir() / f"{bid}.json"
    if not fp.exists():
        raise FileNotFoundError(bid)
    with open(fp, "r", encoding="utf-8") as f:
        d = json.load(f)
    ok, failed = 0, []
    for e in d.get("entries", []):
        p = e["path"]
        if not os.path.exists(p):
            failed.append({"path": p, "error": "文件不存在"})
            continue
        try:
            seg = base64.b64decode(e["exif"]) if e.get("exif") else None
            if seg:
                piexif.insert(seg, p)
            else:
                _strip_app1(p)
            try:
                os.utime(p, (e.get("mtime", time.time()), e.get("mtime", time.time())))
            except OSError:
                pass
            ok += 1
        except Exception as ex:
            failed.append({"path": p, "error": str(ex)})
    return {"ok": ok, "failed": failed}


def write_meta(paths: list[str], changes: dict, backup: bool = True) -> dict:
    if backup:
        try:
            backup_files(paths)
        except Exception:
            pass

    results = []
    for p in paths:
        # 所有 RAW（含 CR3）都只读，跳过而不报错，前端也不会把它们列为可写
        if media.kind_of(p) != "jpg":
            results.append({"path": p, "ok": False, "error": "RAW 文件只读，已跳过"})
            continue
        try:
            st = os.stat(p)
            d = _load_exif(p)
            _apply(d, changes)
            blob = piexif.dump(d)
            piexif.insert(blob, p)
            try:
                os.utime(p, (st.st_atime, st.st_mtime))
            except OSError:
                pass
            results.append({"path": p, "ok": True})
        except Exception as ex:
            results.append({"path": p, "ok": False, "error": f"{type(ex).__name__}: {ex}"})

    ok = sum(1 for r in results if r["ok"])
    return {"ok": ok, "failed": [r for r in results if not r["ok"]]}