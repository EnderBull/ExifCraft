"""ISO BMFF（佳能 CR3）盒结构解析：定位 CMT1/CMT2/CMT4 元数据盒并解析 GPS。"""
from __future__ import annotations

import mmap
import os
import struct

CMT_NAMES = (b"CMT1", b"CMT2", b"CMT3", b"CMT4")

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _iter_boxes(mm, start: int, end: int):
    """逐个产出 (类型, 负载起点, 盒结束)。遇到越界立即停止，绝不抛异常。"""
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(mm[pos:pos + 4], "big")
        typ = bytes(mm[pos + 4:pos + 8])
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = int.from_bytes(mm[pos + 8:pos + 16], "big")
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        pos += size


def read_cmt(path: str) -> dict:
    """返回 {"CMT1": bytes, ...}；CR3 里这些盒的负载就是一段完整 TIFF 头 + IFD。"""
    out: dict = {}
    try:
        size = os.path.getsize(path)
        if size < 64:
            return out
        with open(path, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                for typ, ps, pe in _iter_boxes(mm, 0, size):
                    if typ != b"moov":
                        continue
                    for t2, ps2, pe2 in _iter_boxes(mm, ps, pe):
                        if t2 != b"uuid":
                            continue
                        # uuid 盒在 8 字节头之后还有 16 字节类型标识
                        for t3, ps3, pe3 in _iter_boxes(mm, ps2 + 16, pe2):
                            if t3 in CMT_NAMES:
                                out[t3.decode("ascii")] = bytes(mm[ps3:pe3])
    except (OSError, ValueError):
        return out
    return out


def _ifd_entries(blob: bytes):
    """解析一段 TIFF 负载的 IFD0，产出 (tag, type, count, 值)。"""
    if len(blob) < 8 or blob[:2] not in (b"II", b"MM"):
        return
    bo = "<" if blob[:2] == b"II" else ">"
    try:
        off = struct.unpack(bo + "I", blob[4:8])[0]
        if off + 2 > len(blob):
            return
        n = struct.unpack(bo + "H", blob[off:off + 2])[0]
    except struct.error:
        return
    for i in range(min(n, 256)):
        e = off + 2 + i * 12
        if e + 12 > len(blob):
            return
        tag, typ, cnt = struct.unpack(bo + "HHI", blob[e:e + 8])
        unit = _TYPE_SIZE.get(typ, 1)
        total = unit * cnt
        if total <= 0:
            continue
        if total <= 4:
            raw = blob[e + 8:e + 8 + total]
        else:
            try:
                vp = struct.unpack(bo + "I", blob[e + 8:e + 12])[0]
            except struct.error:
                continue
            raw = blob[vp:vp + total]
        if len(raw) < total:
            continue
        if typ == 2:
            yield tag, typ, cnt, raw.rstrip(b"\x00").decode("ascii", "ignore")
        elif typ == 5:
            vals = []
            for j in range(cnt):
                num, den = struct.unpack(bo + "II", raw[j * 8:j * 8 + 8])
                vals.append(num / den if den else 0.0)
            yield tag, typ, cnt, vals
        elif typ in (3, 4):
            fmt = bo + ("H" if typ == 3 else "I")
            yield tag, typ, cnt, [struct.unpack(fmt, raw[j * unit:(j + 1) * unit])[0] for j in range(cnt)]
        elif typ in (1, 6, 7):
            yield tag, typ, cnt, list(raw)


def parse_gps(blob: bytes | None) -> dict:
    """CMT4 里的 GPS IFD 不被 exifread 识别，这里手工解析 1~6 号标签。"""
    out = {"lat": None, "lng": None, "alt": None}
    if not blob:
        return out
    vals: dict = {}
    for tag, typ, cnt, v in _ifd_entries(blob):
        vals[tag] = v

    def dms(v):
        if not isinstance(v, list) or len(v) < 3:
            return None
        try:
            return float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
        except (TypeError, ValueError):
            return None

    lat = dms(vals.get(2))
    lng = dms(vals.get(4))
    if lat is not None and lng is not None:
        if str(vals.get(1, "N")).strip().upper().startswith("S"):
            lat = -lat
        if str(vals.get(3, "E")).strip().upper().startswith("W"):
            lng = -lng
        out["lat"] = round(lat, 7)
        out["lng"] = round(lng, 7)
    alt = vals.get(6)
    if isinstance(alt, list) and alt:
        a = float(alt[0])
        ref = vals.get(5)
        if isinstance(ref, list) and ref and int(ref[0]) == 1:
            a = -a
        out["alt"] = round(a, 1)
    return out
