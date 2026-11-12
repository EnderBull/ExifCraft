"""ExifCraft 启动入口（Windows / macOS）。"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config  # noqa: E402
from core.server import Server  # noqa: E402

IS_MAC = sys.platform == "darwin"


def _fatal(msg: str) -> None:
    """启动失败时用系统对话框报错（Windows 用 MessageBox，macOS 用 osascript）。"""
    try:
        if IS_MAC:
            import subprocess
            text = msg.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e",
                 f'display dialog "{text}" with title "ExifCraft 启动失败" buttons {{"好"}} default button 1 with icon stop'],
                check=False,
            )
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "ExifCraft 启动失败", 0x10)
    except Exception:
        print(msg)


def _limit_maximized_bounds(win, tries: int = 25) -> None:
    """把最大化范围限制在显示器工作区（不含任务栏）。

    WinForms 的无边框窗口直接最大化会连任务栏一起盖住，这是它的已知行为。
    设置 MaximizedBounds 后和普通窗口一样，只铺满任务栏以外的区域；
    Win+↑、把窗口拖到屏幕上沿这类系统最大化也会一起生效。

    启动时窗口句柄可能还没建好，Invoke 会失败，所以这里等句柄就绪再设。
    """
    err = None
    for _ in range(tries):
        try:
            form = win.native
            if form is not None and form.IsHandleCreated:
                from System import Action
                from System.Windows.Forms import Screen

                def _set():
                    form.MaximizedBounds = Screen.FromControl(form).WorkingArea

                # WinForms 控件只能在 UI 线程上改，这里转过去执行
                form.Invoke(Action(_set))
                return
        except Exception as ex:
            err = ex
        time.sleep(0.1)
    if err is not None:
        try:
            log = config.appdata_dir() / "window-error.log"
            log.write_text(f"设置最大化边界失败：{type(err).__name__}: {err}", encoding="utf-8")
        except Exception:
            pass


class WindowApi:
    """暴露给前端的最小窗口控制接口（Windows 无边框窗口自绘标题栏用）。

    macOS 版一律使用系统原生标题栏，红黄绿交通灯由系统绘制，
    因此这里的位置/尺寸接口只用于前端查询，不会影响窗口行为。
    """
    def __init__(self):
        self._maxed = False
        self._restore_rect = None

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

    def _dpi_scale(self, hwnd) -> float:
        try:
            import ctypes
            return ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0 or 1.0
        except Exception:
            return 1.0

    def _rect_css(self, w) -> dict:
        """当前窗口矩形，换算成前端用的 CSS 像素。"""
        import ctypes
        from ctypes import wintypes
        hwnd = w.native.Handle.ToInt32()
        s = self._dpi_scale(hwnd)
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return {"x": r.left / s, "y": r.top / s,
                "w": (r.right - r.left) / s, "h": (r.bottom - r.top) / s}

    def get_drag_state(self) -> dict:
        """前端拖拽用：当前矩形、是否最大化、最大化之前的矩形。"""
        w = self._win()
        if not w:
            return {}
        try:
            return {"rect": self._rect_css(w), "maxed": self._is_maxed(w),
                    "restore": self._restore_rect}
        except Exception:
            return {}

    def begin_drag(self, x, y, width, height) -> bool:
        """开始拖拽：如果当前是最大化，先还原并摆到指定位置。"""
        w = self._win()
        if not w:
            return False
        try:
            if self._is_maxed(w):
                w.restore()
                self._maxed = False
            self._restore_rect = None
            return self.set_bounds(x, y, width, height)
        except Exception:
            return False

    def _is_maxed(self, w) -> bool:
        try:
            return int(w.native.WindowState) == 2  # FormWindowState.Maximized
        except Exception:
            return self._maxed

    def toggle_maximize(self) -> bool:
        w = self._win()
        if not w:
            return False
        if IS_MAC:
            # macOS 是「缩放（zoom）」，由系统交通灯负责；这里只做全屏切换的兜底
            try:
                w.toggle_fullscreen()
                return not self._maxed
            except Exception:
                return False
        if self._is_maxed(w):
            w.restore()
            self._maxed = False
        else:
            try:
                self._restore_rect = self._rect_css(w)
            except Exception:
                self._restore_rect = None
            _limit_maximized_bounds(w)
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
        """弹出系统文件对话框选一个文件（用于指定 ExifTool）。取消返回空串。

        macOS 上的 ExifTool 是没有扩展名的可执行文件，因此不设文件类型过滤。
        """
        w = self._win()
        if not w:
            return ""
        try:
            import webview
            if IS_MAC:
                res = w.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False)
            else:
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
        if not w or self._is_maxed(w):
            return False
        w.resize(int(width), int(height))
        return True

    def set_bounds(self, x: int, y: int, width: int, height: int) -> bool:
        """一次调用同时设置位置和尺寸，避免 move+resize 两帧重绘造成闪烁。"""
        w = self._win()
        if not w or self._is_maxed(w):
            return False
        if IS_MAC:
            try:
                import AppKit
                native = w.native
                screen = native.screen().frame()
                # 前端传的是「左上角为原点」的坐标；Cocoa 用左下角原点
                fx = screen.origin.x + int(x)
                fy = screen.origin.y + screen.size.height - int(y) - int(height)
                rect = AppKit.NSMakeRect(fx, fy, int(width), int(height))
                native.setFrame_display_(rect, True)
                return True
            except Exception:
                try:
                    w.move(int(x), int(y))
                    w.resize(int(width), int(height))
                    return True
                except Exception:
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


_LAST_MAX_STATE = {"value": None}


def _sync_max_state(win, maxed: bool) -> None:
    """把真实的最大化状态推给自绘标题栏（贴边最大化时按钮图标也要变）。

    事件是在 UI 线程上触发的，而 evaluate_js 会阻塞等 JS 返回，
    直接在 UI 线程调用会卡死，所以另起线程。
    """
    if _LAST_MAX_STATE["value"] == maxed:
        return
    _LAST_MAX_STATE["value"] = maxed

    def run():
        try:
            win.evaluate_js(f"setMaxState({'true' if maxed else 'false'})")
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def _mac_window_size() -> tuple[int, int]:
    """macOS：按屏幕可用区域给一个合适的初始尺寸，避免小屏笔记本上窗口超出屏幕。"""
    try:
        import AppKit
        f = AppKit.NSScreen.mainScreen().visibleFrame()
        sw, sh = int(f.size.width), int(f.size.height)
        return max(1040, min(1460, sw - 80)), max(640, min(920, sh - 80))
    except Exception:
        return 1460, 920


def run(browser_only: bool = False, port: int = 0) -> None:
    srv = Server(port=port)
    srv.start()
    url = srv.url

    # macOS 固定用系统原生标题栏（交通灯 + 系统拖拽/缩放），不给无边框选项
    use_native_frame = IS_MAC or bool(config.load().get("native_frame")) or "--framed" in sys.argv
    win_w, win_h = _mac_window_size() if IS_MAC else (1460, 920)

    if not browser_only:
        try:
            import webview
            target = url + ("?framed=1" if use_native_frame else "?chrome=1")
            win = webview.create_window(
                "ExifCraft · 照片元数据工具",
                target,
                width=win_w,
                height=win_h,
                min_size=(1040, 640),
                background_color="#0b0c0f",
                frameless=not use_native_frame,
                easy_drag=False,
                shadow=use_native_frame,
                js_api=WindowApi(),
            )
            # 系统最大化（贴边、Win+↑）后同步按钮图标
            win.events.maximized += lambda w: _sync_max_state(w, True)
            win.events.restored += lambda w: _sync_max_state(w, False)

            if use_native_frame:
                webview.start()
            else:
                # 启动时先设好最大化边界，Win+↑ / 拖到屏幕上沿也不会盖住任务栏
                webview.start(lambda: _limit_maximized_bounds(win))
            srv.stop()
            return
        except Exception:
            log = config.appdata_dir() / "startup-error.log"
            log.write_text(traceback.format_exc(), encoding="utf-8")
            if IS_MAC:
                hint = "无法启动应用窗口。macOS 自带 WKWebView，通常不会缺运行时；请查看下面的错误详情。"
            else:
                hint = "无法启动应用窗口（通常是缺少 WebView2 运行时）。"
            _fatal(
                hint
                + "\n\n已改为在浏览器中打开，如浏览器没有自动打开，请手动访问：\n"
                + f"{url}\n\n错误详情见：{log}"
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
