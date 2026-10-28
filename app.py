"""ExifCraft 启动入口。"""
from __future__ import annotations

import os
import sys
import time
import traceback
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config  # noqa: E402
from core.server import Server  # noqa: E402


def _fatal(msg: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "ExifCraft 启动失败", 0x10)
    except Exception:
        print(msg)


class WindowApi:
    """暴露给前端的最小窗口控制接口（无边框窗口自绘标题栏用）。"""

    def __init__(self):
        self._maxed = False

    @staticmethod
    def _win():
        import webview
        return webview.windows[0] if webview.windows else None

    def minimize(self) -> bool:
        w = self._win()
        if not w:
            return False
        w.minimize()
        return True

    def toggle_maximize(self) -> bool:
        w = self._win()
        if not w:
            return False
        if self._maxed:
            w.restore()
            self._maxed = False
        else:
            w.maximize()
            self._maxed = True
        return self._maxed

    def close(self) -> bool:
        w = self._win()
        if not w:
            return False
        w.destroy()
        return True

    def pick_file(self) -> str:
        """弹出系统文件对话框选一个文件（用于指定 exiftool.exe）。取消返回空串。"""
        w = self._win()
        if not w:
            return ""
        try:
            import webview
            res = w.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("可执行文件 (*.exe)", "所有文件 (*.*)"),
            )
        except Exception:
            return ""
        if not res:
            return ""
        return res[0] if isinstance(res, (list, tuple)) else str(res)

    def get_bounds(self) -> dict:
        w = self._win()
        if not w:
            return {"x": 0, "y": 0, "w": 0, "h": 0, "maxed": self._maxed}
        return {"x": w.x, "y": w.y, "w": w.width, "h": w.height, "maxed": self._maxed}

    def move_to(self, x: int, y: int) -> bool:
        w = self._win()
        if not w:
            return False
        w.move(int(x), int(y))
        return True

    def resize_to(self, width: int, height: int) -> bool:
        w = self._win()
        if not w or self._maxed:
            return False
        w.resize(int(width), int(height))
        return True

    def set_bounds(self, x: int, y: int, width: int, height: int) -> bool:
        """一次调用同时设置位置和尺寸，避免 move+resize 两帧重绘造成闪烁。"""
        w = self._win()
        if not w or self._maxed:
            return False
        try:
            import ctypes
            hwnd = w.native.Handle.ToInt32()
            scale = ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0 or 1.0
            pw = max(200, int(width * scale))
            ph = max(200, int(height * scale))
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, int(x), int(y), pw, ph, SWP_NOZORDER | SWP_NOACTIVATE
            )
            return True
        except Exception:
            try:
                w.move(int(x), int(y))
                w.resize(int(width), int(height))
                return True
            except Exception:
                return False


def run(browser_only: bool = False, port: int = 0) -> None:
    srv = Server(port=port)
    srv.start()
    url = srv.url

    use_native_frame = bool(config.load().get("native_frame")) or "--framed" in sys.argv

    if not browser_only:
        try:
            import webview
            target = url + ("?framed=1" if use_native_frame else "?chrome=1")
            webview.create_window(
                "ExifCraft · 照片元数据工具",
                target,
                width=1460,
                height=920,
                min_size=(1040, 640),
                background_color="#0b0c0f",
                frameless=not use_native_frame,
                easy_drag=False,
                shadow=use_native_frame,
                js_api=WindowApi(),
            )
            webview.start()
            srv.stop()
            return
        except Exception:
            log = config.appdata_dir() / "startup-error.log"
            log.write_text(traceback.format_exc(), encoding="utf-8")
            _fatal(
                "无法启动应用窗口（通常是缺少 WebView2 运行时）。\n\n"
                "已改为在浏览器中打开，如浏览器没有自动打开，请手动访问：\n"
                f"{url}\n\n错误详情见：{log}"
            )

    print(f"ExifCraft 已启动：{url}")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    port = 0
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            try:
                port = int(sys.argv[i + 1])
            except ValueError:
                port = 0
    try:
        run(browser_only="--browser" in sys.argv, port=port)
    except Exception:
        _fatal("启动失败：\n\n" + traceback.format_exc())
