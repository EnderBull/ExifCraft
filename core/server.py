"""本地 HTTP 服务：静态资源 + JSON API。仅监听 127.0.0.1，需携带会话令牌。"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, exifio, exiftool, media, ops

if getattr(sys, "frozen", False):
    WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "web"
else:
    WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# 前端据此切分隔符、快捷键与文案（mac / win）
PLATFORM = "mac" if config.is_mac() else "win"
# 前端教程里要展示的配置目录（按平台给出习惯写法）
CONFIG_DIR_TEXT = ("~/Library/Application Support/ExifCraft" if PLATFORM == "mac"
                   else "%LOCALAPPDATA%\\ExifCraft")
TOKEN = secrets.token_urlsafe(24)
LAST_RENAME: dict = {"undo": []}

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


def reveal(target: str) -> None:
    """在系统的文件管理器里定位这个路径（Windows 资源管理器 / macOS 访达）。"""
    if config.is_mac():
        # -R 是「在访达里显示并选中」，对文件对文件夹都成立
        subprocess.Popen(["open", "-R", target])  # noqa: S603,S607
        return
    if os.path.isfile(target):
        subprocess.Popen(["explorer", "/select,", target])  # noqa: S603,S607
    else:
        os.startfile(target)  # noqa: S606


class Handler(BaseHTTPRequestHandler):
    server_version = "ExifCraft"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt, *args):
        pass

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        traceback.print_exc()

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 页面与脚本必须每次校验：WebView2 有持久化缓存，若沿用 max-age=3600，
        # 更新程序后用户一小时内仍会看到旧界面；缩略图/预览是本地生成的重资源，继续缓存。
        no_store = ctype.startswith(("application/json", "text/html")) or "javascript" in ctype
        self.send_header("Cache-Control", "no-store" if no_store else "max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400):
        self._json({"error": msg}, code)

    def _authed(self, query: dict) -> bool:
        tok = self.headers.get("X-PM-Token") or (query.get("t", [""])[0] if query else "")
        return secrets.compare_digest(tok, TOKEN)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ------------------------------------------------------------ GET
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._index()
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])

        if not path.startswith("/api/"):
            return self._err("not found", 404)
        if not self._authed(query):
            return self._err("unauthorized", 401)

        try:
            if path == "/api/roots":
                return self._json({"roots": media.roots()})
            if path == "/api/list":
                target = (query.get("path") or [""])[0]
                sort = (query.get("sort") or ["name"])[0]
                recursive = (query.get("recursive") or ["0"])[0] in ("1", "true", "yes")
                if not target:
                    return self._json({"path": "", "parent": "", "name": "", "dirs": media.roots(), "files": [], "total_photos": 0, "virtual": True})
                return self._json(media.list_dir(target, sort=sort, recursive=recursive))
            if path == "/api/thumb":
                p = (query.get("path") or [""])[0]
                if not p or not os.path.isfile(p):
                    return self._err("no file", 404)
                data = media.get_thumb(p)
                return self._send(200, data, "image/jpeg")
            if path == "/api/preview":
                p = (query.get("path") or [""])[0]
                if not p or not os.path.isfile(p):
                    return self._err("no file", 404)
                data = media.get_preview(p)
                return self._send(200, data, "image/jpeg")
            if path == "/api/meta":
                p = (query.get("path") or [""])[0]
                if not p or not os.path.isfile(p):
                    return self._err("no file", 404)
                return self._json(exifio.read_meta(p))
            if path == "/api/settings":
                return self._json(config.load())
            if path == "/api/exiftool":
                return self._json(exiftool.status())
            if path == "/api/backups":
                return self._json({"items": exifio.list_backups()})
            if path == "/api/job":
                jid = (query.get("id") or [""])[0]
                j = ops.get_job(jid)
                if not j:
                    return self._err("no job", 404)
                return self._json(j)
        except PermissionError as e:
            return self._err(f"没有访问权限：{e}", 403)
        except FileNotFoundError as e:
            return self._err(f"文件不存在：{e}", 404)
        except Exception as e:
            traceback.print_exc()
            return self._err(f"{type(e).__name__}: {e}", 500)

        return self._err("not found", 404)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authed(query):
            return self._err("unauthorized", 401)
        data = self._body()

        try:
            if path == "/api/write":
                paths = data.get("paths") or []
                changes = data.get("changes") or {}
                if not paths or not changes:
                    return self._err("缺少参数")
                return self._json(exifio.write_meta(paths, changes))

            if path == "/api/restore":
                bid = data.get("id") or ""
                return self._json(exifio.restore_backup(bid))

            if path == "/api/rename/preview":
                paths = data.get("paths") or []
                template = data.get("template") or "{name}"
                start = int(data.get("start") or 1)
                return self._json({"items": ops.rename_preview(paths, template, start)})

            if path == "/api/rename/apply":
                items = data.get("items") or []
                job = ops.rename_apply(items)
                LAST_RENAME["undo"] = (job.get("result") or {}).get("undo") or []
                return self._json(job)

            if path == "/api/rename/undo":
                pairs = LAST_RENAME.get("undo") or []
                if not pairs:
                    return self._err("没有可撤销的重命名")
                ok, failed = 0, []
                for p in reversed(pairs):
                    try:
                        if os.path.exists(p["from"]) and not os.path.exists(p["to"]):
                            os.rename(p["from"], p["to"])
                            ok += 1
                    except OSError as e:
                        failed.append({"path": p["from"], "error": str(e)})
                LAST_RENAME["undo"] = []
                return self._json({"ok": ok, "failed": failed})

            if path == "/api/export":
                paths = data.get("paths") or []
                out_dir = data.get("outDir") or ""
                if not out_dir:
                    return self._err("请选择输出目录")
                job = ops.new_job("export", len(paths))
                threading.Thread(
                    target=ops.export_images,
                    args=(paths, out_dir,
                          int(data.get("longEdge") or 3840),
                          int(data.get("quality") or 85)),
                    kwargs={"keep": data.get("keep") or {}, "job": job},
                    daemon=True,
                ).start()
                return self._json(ops.get_job(job["id"]))

            if path == "/api/settings":
                saved = config.save(data or {})
                if "exiftool_path" in (data or {}):
                    exiftool.reset()
                return self._json(saved)

            if path == "/api/job/cancel":
                return self._json({"ok": ops.cancel_job(data.get("id") or "")})

            if path == "/api/reveal":
                p = data.get("path") or ""
                if p and os.path.exists(p):
                    reveal(os.path.normpath(p))
                    return self._json({"ok": True})
                return self._err("路径不存在")

            if path == "/api/cache/clear":
                n, total = media.clear_cache()
                return self._json({"removed": n, "bytes": total})

            if path == "/api/open-external":
                url = data.get("url") or ""
                if url.startswith("http"):
                    import webbrowser
                    webbrowser.open(url)
                    return self._json({"ok": True})
                return self._err("无效链接")
        except Exception as e:
            traceback.print_exc()
            return self._err(f"{type(e).__name__}: {e}", 500)

        return self._err("not found", 404)

    # ------------------------------------------------------------ 静态
    def _index(self):
        try:
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            return self._err("index.html 缺失", 500)
        html = (html.replace("__PM_TOKEN__", TOKEN)
                    .replace("__PM_PLATFORM__", PLATFORM)
                    .replace("__PM_CONFIG_DIR__", CONFIG_DIR_TEXT))
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _static(self, rel: str):
        rel = urllib.parse.unquote(rel)
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._err("forbidden", 403)
        if not target.is_file():
            return self._err("not found", 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        traceback.print_exc()


class Server:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.httpd = _HTTPServer((host, port), Handler)
        self.host, self.port = self.httpd.server_address[:2]
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
