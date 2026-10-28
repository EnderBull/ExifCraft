"""RAW 内嵌预览与全尺寸提取（纯标准库 + Pillow，不引入 rawpy/libraw）。

各品牌 RAW 的容器差异很大，但能取到的图只有三种形式：
1. 一段独立的 JPEG——CR3（SOI 快扫）、RW2（快扫）、NEF/PEF/3FR（TIFF IFD 记录偏移+长度）；
2. Fujifilm RAF：文件头 84..92 字节记录 JPEG 的偏移与长度；
3. 未压缩的 RGB/灰度 strip——Phase One IIQ 只有这一种（640x480，直接 frombytes）。

因此按「RAF 头部直读 -> TIFF IFD 骨架 -> SOI/EOI 快扫 -> JPEG 标记遍历」四段
依次尝试，每段都用「能真正解码」判定成功，失败自动降级到下一段。
关键点：JPEG 候选永远排在 strip 候选之前，否则 NEF 会先命中 160x120 的裸 strip。
"""
from __future__ import annotations

import io
import mmap
import struct

from PIL import Image

# TIFF 字段类型 -> 字节数
_SZ = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_TIFF_MAGIC = (b"II*\x00", b"MM\x00*")
_RAF_MAGIC = b"FUJIFILMCCD-RAW "

_MIN_PREVIEW = 4096          # 小于此字节数的 JPEG 当缩略图，丢掉
_MAX_STRIP = 64_000_000      # 未压缩 strip 上限，防止误判把整块传感器数据读进来
_MAX_DIM = 200_000           # 尺寸合理性上限
_SOI = b"\xff\xd8\xff"
_EOI = b"\xff\xd9"


# ------------------------------------------------------------------ JPEG 结构

def jpeg_end(mm, start: int, cap: int = 80_000_000) -> int | None:
    """从 start 处的 SOI 开始遍历 JPEG 标记，返回真实结束偏移（含 EOI）。

    必须走标记遍历：只找最近的 FFD9 会在熵编码数据里撞见假结束符，且
    progressive JPEG 的 SOS 后面还有多段扫描数据。
    """
    n = len(mm)
    p = start + 2
    while p < n - 1 and p - start < cap:
        if mm[p] != 0xFF:
            p += 1
            continue
        m = mm[p + 1]
        if m in (0xD8, 0x01, 0xFF) or 0xD0 <= m <= 0xD7:
            p += 2
            continue
        if m == 0xD9:
            return p + 2
        if p + 4 > n:
            return None
        ln = struct.unpack(">H", mm[p + 2:p + 4])[0]
        if ln < 2:
            return None
        if m == 0xDA:                       # SOS 之后是熵编码数据
            q = p + 2 + ln
            while q < n - 1:
                if mm[q] == 0xFF:
                    nm = mm[q + 1]
                    if nm == 0xD9:
                        return q + 2
                    if nm == 0x00 or 0xD0 <= nm <= 0xD7:
                        q += 2
                        continue
                    if nm == 0xFF:
                        q += 1
                        continue
                    break                   # 又遇到新标记（progressive 的下一段 SOS）
                q += 1
            p = q
            continue
        p += 2 + ln
    return None


# --------------------------------------------------------------- TIFF IFD 遍历

def _ifd(mm, off: int, endian: str, base: int) -> tuple[dict, int] | None:
    """读一个 IFD，返回 ({tag: (type, count, value_offset)}, 下一个 IFD 偏移)。

    value_offset 已经加上 base（RAF 的 CFA 区里偏移是相对区首的）。
    """
    limit = len(mm)
    if off + 2 > limit:
        return None
    n = struct.unpack(endian + "H", mm[off:off + 2])[0]
    if n == 0 or n > 600 or off + 2 + n * 12 > limit:
        return None
    out = {}
    for i in range(n):
        e = off + 2 + i * 12
        tag, typ = struct.unpack(endian + "HH", mm[e:e + 4])
        cnt = struct.unpack(endian + "I", mm[e + 4:e + 8])[0]
        size = _SZ.get(typ, 1) * cnt
        voff = e + 8 if size <= 4 else base + struct.unpack(endian + "I", mm[e + 8:e + 12])[0]
        out[tag] = (typ, cnt, voff)
    try:
        nxt = struct.unpack(endian + "I", mm[off + 2 + n * 12:off + 6 + n * 12])[0]
    except struct.error:
        nxt = 0
    return out, nxt


def _num(mm, ent, endian: str) -> int | None:
    """取单个无符号整数字段。"""
    if not ent:
        return None
    typ, cnt, voff = ent
    try:
        if typ == 3:
            return struct.unpack(endian + "H", mm[voff:voff + 2])[0]
        if typ in (4, 13):
            return struct.unpack(endian + "I", mm[voff:voff + 4])[0]
    except struct.error:
        return None
    return None


def _offsets(mm, ent, endian: str) -> list[int]:
    """SubIFDs 字段可能是单个偏移，也可能是偏移数组。"""
    typ, cnt, voff = ent
    step = 4 if typ in (4, 13) else 8
    vals = []
    for i in range(min(cnt, 16)):
        try:
            raw = mm[voff + i * step: voff + i * step + 4]
            v = struct.unpack(endian + "I", raw)[0]
        except struct.error:
            break
        if v:
            vals.append(v)
    return vals


def _images(mm, endian: str, base: int, first: int) -> list[dict]:
    """递归遍历 IFD 链 + SubIFDs(0x014A) + ExifIFD(0x8769) + GPS(0xA005)。"""
    out: list[dict] = []
    seen: set[int] = set()

    def walk(off: int, depth: int) -> None:
        if depth > 3 or off in seen:
            return
        seen.add(off)
        got = _ifd(mm, off, endian, base)
        if not got:
            return
        d, nxt = got

        w, h = _num(mm, d.get(0x100), endian), _num(mm, d.get(0x101), endian)
        if not (w and h and 0 < w <= _MAX_DIM and 0 < h <= _MAX_DIM):
            w = h = 0          # 有些 SubIFD 只记 JPEGInterchangeFormat，没有宽高
        if True:
            out.append({
                "w": w, "h": h,
                "comp": _num(mm, d.get(0x103), endian),
                "spp": _num(mm, d.get(0x115), endian) or 1,
                "joff": _num(mm, d.get(0x201), endian),
                "jlen": _num(mm, d.get(0x202), endian),
                "soff": _num(mm, d.get(0x111), endian),
                "slen": _num(mm, d.get(0x117), endian),
            })

        for tag in (0x14A, 0x8769, 0xA005):
            sub = d.get(tag)
            if sub and sub[0] in (4, 13):
                for s in _offsets(mm, sub, endian):
                    walk(s, depth + 1)
        if nxt:
            walk(nxt, depth)

    walk(first, 0)
    return out


def _tiff(mm, head: bytes) -> tuple[str, int] | None:
    if head[:4] not in _TIFF_MAGIC:
        return None
    endian = "<" if head[:2] == b"II" else ">"
    return endian, struct.unpack(endian + "I", head[4:8])[0]


def _raf_parts(head: bytes) -> tuple[int, int, int, int, int] | None:
    """RAF 头部：84..88 JPEG 偏移、88..92 JPEG 长度、92..96 meta 块偏移、
    96..100 meta 块长度、100..104 CFA TIFF 偏移。"""
    if head[:8] != _RAF_MAGIC[:8]:
        return None
    try:
        return (struct.unpack(">I", head[84:88])[0],
                struct.unpack(">I", head[88:92])[0],
                struct.unpack(">I", head[92:96])[0],
                struct.unpack(">I", head[96:100])[0],
                struct.unpack(">I", head[100:104])[0])
    except struct.error:
        return None


def _raf_cropped_size(mm, meta_off: int, meta_len: int) -> tuple[int, int] | None:
    """RAF meta 块是 [u32 条目数] + n*(u16 tag, u16 count, 4 字节值)，
    其中 0x0112 就是裁掉边缘暗角后的实际像素尺寸（11648x8736）。"""
    try:
        if not (0 < meta_off < len(mm) - 8) or meta_len < 32:
            return None
        n = struct.unpack(">I", mm[meta_off:meta_off + 4])[0]
        if not (4 <= n <= 64):
            return None
        for i in range(n):
            e = meta_off + 4 + i * 8
            tag = struct.unpack(">H", mm[e:e + 2])[0]
            if tag != 0x0112:
                continue
            w, h = struct.unpack(">HH", mm[e + 4:e + 8])
            if w and h and 0 < w <= _MAX_DIM and 0 < h <= _MAX_DIM:
                return (w, h)
            break
    except struct.error:
        pass
    return None


def _fuji_private(mm, cfa: int) -> tuple[int, int] | None:
    """Fuji CFA 区是独立小端 TIFF，IFD0 只有 0xF000（子 IFD 指针），
    子 IFD 里 0xF001/0xF002 才是传感器全宽全高。"""
    try:
        endian = "<" if mm[cfa:cfa + 2] == b"II" else ">"
        first = struct.unpack(endian + "I", mm[cfa + 4:cfa + 8])[0]
        got = _ifd(mm, cfa + first, endian, cfa)
        if not got:
            return None
        d, _ = got
        rel = _num(mm, d.get(0xF000), endian)
        if not rel:
            return None
        got = _ifd(mm, cfa + rel, endian, cfa)
        if not got:
            return None
        d2, _ = got
        w = _num(mm, d2.get(0xF001), endian)
        h = _num(mm, d2.get(0xF002), endian)
        if w and h and 0 < w <= _MAX_DIM and 0 < h <= _MAX_DIM:
            return (w, h)
    except (struct.error, IndexError):
        pass
    return None


# ------------------------------------------------------------- 候选图（内部）

def _candidates(mm):
    """按优先级惰性产出候选：("jpeg", blob, how) 或 ("strip", w, h, spp, data, how)。

    必须是生成器：调用方拿到能解码的候选就停，后面的重扫（40 段 SOI 扫描、
    8 次标记遍历）根本不该发生，否则每个文件都白扫上百 MB。
    """
    head = bytes(mm[:192])
    n = len(mm)

    # ① RAF 头部直读
    raf = _raf_parts(head)
    if raf:
        joff, jlen = raf[0], raf[1]
        if 0 < joff < n - 4:
            end = jpeg_end(mm, joff) or (joff + jlen)
            if end and end <= n and end - joff >= _MIN_PREVIEW:
                yield ("jpeg", bytes(mm[joff:end]), "raf")

    # ② TIFF IFD 骨架：JPEG 候选按面积从大到小（NEF/PEF 的 IFD 链里大图不在最前，
    #    不排序会先命中 160x120 的缩略图），未压缩 strip 永远排在所有 JPEG 之后。
    tiff = _tiff(mm, head)
    if tiff:
        endian, first = tiff
        jpegs: list[tuple[int, bytes]] = []
        strips: list[tuple[int, int, int, bytes]] = []
        for img in _images(mm, endian, 0, first):
            joff, jlen = img["joff"], img["jlen"]
            if joff and jlen and jlen >= _MIN_PREVIEW and joff + jlen <= n:
                jpegs.append((img["w"] * img["h"], bytes(mm[joff:joff + jlen])))
                continue
            soff, slen = img["soff"], img["slen"]
            if not (soff and slen and soff + slen <= n and slen <= _MAX_STRIP):
                continue
            w, h, spp = img["w"], img["h"], img["spp"]
            if img["comp"] == 7 and slen >= _MIN_PREVIEW:
                jpegs.append((w * h, bytes(mm[soff:soff + slen])))
            elif img["comp"] == 1 and spp in (1, 3) and slen == w * h * spp:
                strips.append((w, h, spp, bytes(mm[soff:soff + slen])))
        for _, blob in sorted(jpegs, key=lambda c: -len(c[1])):
            yield ("jpeg", blob, "ifd-jpeg")
        for w, h, spp, data in sorted(strips, key=lambda c: -c[0] * c[1]):
            yield ("strip", w, h, spp, data, "ifd-strip")

    # ③ SOI/EOI 快扫：CR3、RW2 以及多数相机写入的「最大那张」都在这一层
    try:
        segs = []
        pos = 0
        while len(segs) < 40:
            s = mm.find(_SOI, pos)
            if s < 0:
                break
            e = mm.find(_EOI, s + 3)
            if e < 0:
                break
            e += 2
            segs.append((e - s, s, e))
            pos = e
        segs.sort(reverse=True)
        for ln, s, e in segs[:4]:
            if ln < _MIN_PREVIEW:
                break
            yield ("jpeg", bytes(mm[s:e]), "scan")
    except ValueError:
        pass

    # ④ JPEG 标记遍历：快扫被截断时（RAF 等）靠它求真实结尾
    try:
        pos = 0
        tried = 0
        while tried < 8:
            s = mm.find(_SOI, pos)
            if s < 0:
                break
            e = jpeg_end(mm, s)
            if e and e - s >= _MIN_PREVIEW:
                yield ("jpeg", bytes(mm[s:e]), "walk")
            pos = s + 3
            tried += 1
    except ValueError:
        pass


def _decode(cand: tuple) -> Image.Image | None:
    try:
        if cand[0] == "jpeg":
            im = Image.open(io.BytesIO(cand[1]))
            im.load()
            return im
        _, w, h, spp, data, _ = cand
        return Image.frombytes("RGB" if spp == 3 else "L", (w, h), data)
    except Exception:
        return None


# ------------------------------------------------------------------ 公开接口

def first_image(path: str, side: int = 512) -> tuple[Image.Image, bool] | None:
    """返回 (预览图, 是否已按 side 降采样)，失败返回 None。

    JPEG 走 draft()，让解码器在 DCT 域直接降采样——这是最大的提速点，
    8192x5464 的大图不必先解成 RGB 位图再缩。
    """
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for cand in _candidates(mm):
                if cand[0] == "jpeg":
                    try:
                        im = Image.open(io.BytesIO(cand[1]))
                        if im.format == "JPEG":
                            im.draft("RGB", (side, side))
                        im.load()
                    except Exception:
                        continue
                    return im, True
                img = _decode(cand)
                if img is not None:
                    return img, False
    except (OSError, ValueError):
        return None
    return None


def first_jpeg(path: str) -> bytes | None:
    """返回第一段能解码的 JPEG 字节。"""
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for cand in _candidates(mm):
                if cand[0] == "jpeg" and _decode(cand) is not None:
                    return cand[1]
    except (OSError, ValueError):
        pass
    return None


def preview_size(path: str) -> tuple[int, int] | None:
    """内嵌预览的像素尺寸（只读图像头，不解像素）。"""
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for cand in _candidates(mm):
                if cand[0] == "strip":
                    return (cand[1], cand[2])
                try:
                    im = Image.open(io.BytesIO(cand[1]))
                    size = im.size
                    im.close()
                except Exception:
                    continue
                if max(size) >= 320:
                    return size
    except (OSError, ValueError):
        pass
    return None


def full_size(path: str) -> tuple[int, int] | None:
    """实际像素尺寸（裁掉暗角后的那张，与 ExifTool 的 ImageSize 同口径）。

    - RAF：meta 块 tag 0x0112（11648x8736）；取不到才退回 Fuji 私有 IFD 的未裁切尺寸；
    - 3FR / NEF / PEF：TIFF IFD 里最大的一对 ImageWidth/ImageLength；
    - CR3 / IIQ / RW2：容器里没有可比字段，返回 None，由调用方按 EXIF / 预览尺寸兜底。
    """
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            head = bytes(mm[:192])
            n = len(mm)

            raf = _raf_parts(head)
            if raf:
                size = _raf_cropped_size(mm, raf[2], raf[3])
                if size:
                    return size
                if 0 < raf[4] < n - 8:
                    return _fuji_private(mm, raf[4])
                return None

            best: tuple[int, int] | None = None
            tiff = _tiff(mm, head)
            if tiff:
                endian, first = tiff
                for img in _images(mm, endian, 0, first):
                    area = img["w"] * img["h"]
                    if area and (best is None or area > best[0] * best[1]):
                        best = (img["w"], img["h"])
            return best
    except (OSError, ValueError):
        return None
