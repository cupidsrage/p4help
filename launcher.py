#!/usr/bin/env python3
"""
Phantasia Advisor — all-in-one launcher.

One window. Open it and it:
  1. starts the proxy + dashboard in the background (no console window)
  2. launches the Phantasia game client
  3. pulls the client's window INSIDE this window, with the advisor panel
     and the auto-play button grid docked on the right

Auto-play (toolbar button) presses the advisor's recommended key in the game,
the same as buttons.py's auto-press. It only sends keys while this window is
in front with the game embedded, so it never types into other apps.

    pythonw launcher.py          (or double-click Phantasia.bat)

First run asks you to pick the game client (.exe / .jar / .bat / shortcut);
the choice is saved in launcher.json next to this file. Use the toolbar's
"Client..." button to change it later.

Embedding uses the Win32 SetParent API, so it needs Windows. On other systems
the launcher still runs the proxy, starts the client in its own window and
shows the advisor panel.

The hosts-file entry from the README is still required (127.0.0.1 phantasia5.com)
so the client talks to the proxy. The toolbar warns you if it's missing.

Local only. Building this is fine per the calc policy; sharing it is not.
"""

import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox
from tkinter import font as tkfont

import app as proxy
from buttons import ButtonOverlay
from overlay import Overlay, BG, PANEL, LINE, INK, DIM, GOLD, GREEN, AMBER, RED

IS_WIN = sys.platform == "win32"
FROZEN = getattr(sys, "frozen", False)
HOME = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HOME, "launcher.json")

DEFAULTS = {
    "client": "",                 # path to the game client
    "client_args": "",            # extra command-line args for it
    "window_title": "",           # optional: match the client window by title
    "server": "178.63.136.86",    # upstream game server (app.py -h)
    "game_host": "phantasia5.com",
    "stretch": True,              # resize the client to fill the space
    "panel": True,                # show the advisor panel
    "panel_width": 340,
    "close_client_on_exit": True,
    "autoplay": False,            # auto-press the advisor's move in the game
    "record": True,               # record screens + inputs (recorder.py)
    "record_screenshots": False,  # also save a PNG of the game per input
    "geometry": "",
}

EMBED_TIMEOUT_S = 45
BAR_BG = "#0f0d09"


# ---------------------------------------------------------------- config ---
def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# ----------------------------------------------------------------- win32 ---
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GWL_STYLE, GWL_EXSTYLE = -16, -20
    WS_CHILD = 0x40000000
    WS_POPUP = 0x80000000
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_SYSMENU = 0x00080000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    WS_VISIBLE = 0x10000000
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_WINDOWEDGE = 0x00000100
    WS_EX_DLGMODALFRAME = 0x00000001
    WS_EX_CLIENTEDGE = 0x00000200
    WS_EX_STATICEDGE = 0x00020000
    SWP_NOZORDER, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x0004, 0x0020, 0x0040
    SWP_NOACTIVATE = 0x0010
    GW_OWNER = 4
    WM_CLOSE = 0x0010
    TH32CS_SNAPPROCESS = 0x00000002

    LONG_PTR = ctypes.c_ssize_t
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND
    user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT]
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    GA_ROOT = 2
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        _GetWL, _SetWL = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
    else:
        _GetWL, _SetWL = user32.GetWindowLongW, user32.SetWindowLongW
    _GetWL.argtypes = [wintypes.HWND, ctypes.c_int]
    _GetWL.restype = LONG_PTR
    _SetWL.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
    _SetWL.restype = LONG_PTR

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def descendant_pids(root_pid):
        """root_pid plus every process it (transitively) spawned.

        Clients started via a .bat, java launcher or updater stub end up
        owning their window from a child process, so we look at the tree."""
        parent_of = {}
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return {root_pid}
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                parent_of[pe.th32ProcessID] = pe.th32ParentProcessID
                ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            kernel32.CloseHandle(snap)
        tree = {root_pid}
        grew = True
        while grew:
            grew = False
            for pid, ppid in parent_of.items():
                if ppid in tree and pid not in tree:
                    tree.add(pid)
                    grew = True
        return tree

    def window_title(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def top_windows():
        """Visible, unowned top-level windows as (hwnd, pid, title)."""
        out = []

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                out.append((hwnd, pid.value, window_title(hwnd)))
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return out

    def client_size(hwnd):
        r = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        return r.right - r.left, r.bottom - r.top

    def adopt(hwnd, host):
        """Strip the window's frame and make it a child of `host`."""
        style = _GetWL(hwnd, GWL_STYLE)
        style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU |
                   WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
        style |= WS_CHILD | WS_VISIBLE
        _SetWL(hwnd, GWL_STYLE, style)
        ex = _GetWL(hwnd, GWL_EXSTYLE)
        ex &= ~(WS_EX_APPWINDOW | WS_EX_WINDOWEDGE | WS_EX_DLGMODALFRAME |
                WS_EX_CLIENTEDGE | WS_EX_STATICEDGE)
        _SetWL(hwnd, GWL_EXSTYLE, ex)
        user32.SetParent(hwnd, host)
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                            SWP_NOZORDER | SWP_FRAMECHANGED | SWP_SHOWWINDOW)


# ---------------------------------------------------------------- the app ---
class Launcher:
    def __init__(self):
        self.cfg = load_config()
        self.proc = None
        self.hwnd = None
        self.native_size = None
        self.searching_since = None
        self.proxy_note = ""
        self._known_windows = set()

        self.root = tk.Tk()
        self.root.title("Phantasia Advisor")
        self.root.configure(bg=BG)
        self.root.geometry(self.cfg.get("geometry") or "1560x860+40+40")
        self.root.minsize(640, 420)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.f_sm = tkfont.Font(family="Segoe UI", size=9)
        self.f_md = tkfont.Font(family="Segoe UI", size=11)
        self.f_bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        self._start_proxy()
        self._setup_recorder()
        self._build()
        self._check_hosts()
        self.root.after(200, self.launch_client)
        self.root.after(1000, self._watch)

    # ------------------------------------------------------------ layout ---
    def _build(self):
        bar = tk.Frame(self.root, bg=BAR_BG, height=30)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text="PHANTASIA ADVISOR", bg=BAR_BG, fg=GOLD,
                 font=self.f_bold).pack(side="left", padx=10)

        def btn(text, cmd):
            def run():
                cmd()
                self._focus_game()   # keep keystrokes going to the game
            b = tk.Button(bar, text=text, command=run, bg=PANEL, fg=INK,
                          activebackground=LINE, activeforeground=INK,
                          relief="flat", bd=0, padx=10, font=self.f_sm,
                          cursor="hand2")
            b.pack(side="left", padx=2, pady=4)
            return b

        btn("Relaunch game", self.relaunch)
        btn("Client\u2026", self.choose_client)
        btn("Dashboard", self.open_dashboard)
        self.panel_btn = btn("Hide advisor" if self.cfg["panel"] else "Show advisor",
                             self.toggle_panel)
        self.auto_btn = btn("", self.toggle_autoplay)
        self.auto_btn.configure(font=self.f_bold)
        self.rec_btn = btn("", self.toggle_record)
        btn("Recordings", self.open_recordings)

        self.status = tk.Label(bar, text="", bg=BAR_BG, fg=DIM, font=self.f_sm,
                               anchor="e")
        self.status.pack(side="right", padx=10, fill="x", expand=True)

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(side="top", fill="both", expand=True)

        self.panel_frame = tk.Frame(self.body, bg=BG,
                                    width=self.cfg["panel_width"])
        self.panel_frame.pack_propagate(False)
        self.divider = tk.Frame(self.body, bg=LINE, width=1)
        self.game = tk.Frame(self.body, bg="#000000", takefocus=1)
        self._layout_panel()

        self.placeholder = tk.Label(self.game, text="", bg="#000000", fg=DIM,
                                    font=self.f_md, justify="center")
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")

        self.game.bind("<Configure>", lambda e: self._fit())
        self.game.bind("<Button-1>", lambda e: self._focus_game())
        self.root.bind("<FocusIn>", self._on_focus)

        # right column: advisor on top, the auto-play button grid underneath
        self.buttons_frame = tk.Frame(self.panel_frame, bg=BG, height=250)
        self.buttons_frame.pack(side="bottom", fill="x")
        self.buttons_frame.pack_propagate(False)
        tk.Frame(self.panel_frame, bg=LINE, height=1).pack(side="bottom", fill="x")
        self.advice_frame = tk.Frame(self.panel_frame, bg=BG)
        self.advice_frame.pack(side="top", fill="both", expand=True)

        self.panel = Overlay(parent=self.advice_frame)
        self.buttons = ButtonOverlay(parent=self.buttons_frame,
                                     autopress=self.cfg["autoplay"],
                                     before_press=self._game_ready_for_keys,
                                     on_autopress_change=self._autoplay_changed)
        self._paint_auto_btn()
        self._paint_rec_btn()

    def _layout_panel(self):
        for w in (self.panel_frame, self.divider, self.game):
            w.pack_forget()
        if self.cfg["panel"]:
            self.panel_frame.pack(side="right", fill="y")
            self.divider.pack(side="right", fill="y")
        self.game.pack(side="left", fill="both", expand=True)

    def toggle_panel(self):
        self.cfg["panel"] = not self.cfg["panel"]
        self.panel_btn.configure(text="Hide advisor" if self.cfg["panel"]
                                 else "Show advisor")
        self._layout_panel()
        save_config(self.cfg)

    # --------------------------------------------------------- auto-play ---
    def toggle_autoplay(self):
        self.buttons.set_autopress(not self.cfg["autoplay"])

    def _autoplay_changed(self, on):
        self.cfg["autoplay"] = on
        if self.recorder:
            self.recorder.autoplay = on
        save_config(self.cfg)
        self._paint_auto_btn()

    def _paint_auto_btn(self):
        on = self.cfg["autoplay"]
        self.auto_btn.configure(text="Auto-play: ON" if on else "Auto-play: OFF",
                                fg="#0c0a07" if on else INK,
                                bg=GREEN if on else PANEL,
                                activebackground=GREEN if on else LINE)

    # --------------------------------------------------------- recording ---
    def _setup_recorder(self):
        self.recorder = None
        if self.proxy_note:
            return   # proxy runs in another process; it can't be tapped here
        self.recorder = proxy.enable_recording(os.path.join(HOME, "recordings"))
        self.recorder.enabled = self.cfg["record"]
        self.recorder.autoplay = self.cfg["autoplay"]
        if self.cfg.get("record_screenshots"):
            self.recorder.screenshot_fn = self._screenshot

    def _screenshot(self, path):
        """Save the embedded game window to `path` (needs Pillow, Windows)."""
        if not (IS_WIN and self.hwnd and user32.IsWindow(self.hwnd)):
            return False
        try:
            from PIL import ImageGrab
        except ImportError:
            return False
        r = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom),
                       all_screens=True).save(path)
        return True

    def toggle_record(self):
        if not self.recorder:
            messagebox.showinfo(
                "Recording",
                "The proxy is already running in another window (app.py / "
                "start.bat), so this window can't record.\n\nClose that one and "
                "restart the launcher, or run:  python app.py --record")
            return
        on = not self.cfg["record"]
        self.cfg["record"] = on
        self.recorder.enabled = on
        if on and proxy.STATE.get("connected"):
            self.recorder.open()     # start recording the current game now
        elif not on:
            self.recorder.close()
        save_config(self.cfg)
        self._paint_rec_btn()

    def _paint_rec_btn(self):
        on = bool(self.recorder and self.cfg["record"])
        self.rec_btn.configure(text="\u25cf Recording" if on else "Record: OFF",
                               fg=RED if on else INK)

    def open_recordings(self):
        folder = os.path.join(HOME, "recordings")
        os.makedirs(folder, exist_ok=True)
        if IS_WIN:
            os.startfile(folder)
        else:
            subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

    def _game_ready_for_keys(self):
        """Gate for every auto-play key tap.

        Keys are real keystrokes, so they go to whatever has focus. Only send
        them while THIS window is in front and the game is embedded in it, and
        put keyboard focus on the game first. Alt-tab away and auto-play just
        waits instead of typing into your other apps."""
        if not IS_WIN:
            return True   # no embedding; buttons.py's xdotool path as before
        if not (self.hwnd and user32.IsWindow(self.hwnd)):
            return False
        top = user32.GetAncestor(self.game.winfo_id(), GA_ROOT)
        if user32.GetForegroundWindow() != top:
            return False
        if threading.current_thread() is threading.main_thread():
            user32.SetFocus(self.hwnd)
        return True

    def set_status(self, text, color=DIM):
        parts = [x for x in (self.proxy_note, text) if x]
        self.status.configure(text="   \u00b7   ".join(parts), fg=color)

    def show_placeholder(self, text):
        self.placeholder.configure(text=text)
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        self.placeholder.lift()

    # ------------------------------------------------------------- proxy ---
    def _start_proxy(self):
        proxy.SERVER_HOST = self.cfg["server"] or proxy.SERVER_HOST
        try:
            proxy.start(open_browser=False, background=True)
        except OSError:
            # Port already in use: most likely app.py / start.bat is already
            # running. The panel reads from 127.0.0.1:8420 either way.
            self.proxy_note = "using already-running proxy"

    def _check_hosts(self):
        host = self.cfg.get("game_host")
        if not host:
            return

        def check():
            try:
                ip = socket.gethostbyname(host)
            except OSError:
                ip = None
            if ip != "127.0.0.1":
                self.root.after(0, lambda: messagebox.showwarning(
                    "Hosts file",
                    f"{host} doesn't point at this PC (got {ip}).\n\n"
                    "The game will connect straight to the server and the "
                    "advisor won't see anything.\n\nAdd this line to "
                    "C:\\Windows\\System32\\drivers\\etc\\hosts "
                    "(Notepad as Administrator):\n\n"
                    f"    127.0.0.1  {host}\n\nthen run: ipconfig /flushdns"))

        threading.Thread(target=check, daemon=True).start()

    def open_dashboard(self):
        webbrowser.open(f"http://127.0.0.1:{proxy.WEB_PORT}")

    # ------------------------------------------------------------ client ---
    def choose_client(self):
        path = filedialog.askopenfilename(
            title="Pick the Phantasia game client",
            initialdir=os.path.dirname(self.cfg["client"]) if self.cfg["client"] else HOME,
            filetypes=[("Programs", "*.exe *.jar *.bat *.cmd *.lnk"),
                       ("All files", "*.*")])
        if not path:
            return False
        self.cfg["client"] = os.path.normpath(path)
        save_config(self.cfg)
        self.relaunch()
        return True

    def _command(self):
        path = self.cfg["client"]
        args = shlex.split(self.cfg.get("client_args") or "", posix=not IS_WIN)
        ext = os.path.splitext(path)[1].lower()
        if ext == ".jar":
            java = "javaw" if IS_WIN else "java"
            return [java, "-jar", path] + args
        if ext in (".bat", ".cmd") and IS_WIN:
            return ["cmd", "/c", path] + args
        return [path] + args

    def launch_client(self):
        path = self.cfg["client"]
        if not path or not os.path.exists(path):
            self.show_placeholder("Pick your Phantasia game client to get started.\n\n"
                                  "Use the  Client\u2026  button above.")
            self.set_status("no game client chosen", AMBER)
            self.choose_client()   # relaunches on success
            return

        self.hwnd = None
        self.native_size = None
        self.show_placeholder("Starting the game\u2026")
        self.set_status("launching " + os.path.basename(path))
        try:
            if IS_WIN and os.path.splitext(path)[1].lower() == ".lnk":
                # shortcuts can't be exec'd directly; let the shell open it and
                # find the window by title instead of process id.
                os.startfile(path)
                self.proc = None
            else:
                flags = 0x08000000 if IS_WIN else 0   # CREATE_NO_WINDOW
                self.proc = subprocess.Popen(self._command(),
                                             cwd=os.path.dirname(path) or None,
                                             creationflags=flags)
        except OSError as e:
            self.show_placeholder(f"Couldn't start the game:\n{e}\n\n"
                                  "Use  Client\u2026  to pick it again.")
            self.set_status("launch failed", RED)
            return
        self.searching_since = time.time()
        self._known_windows = set(h for h, _, _ in top_windows()) if IS_WIN else set()

    def relaunch(self):
        self._close_client()
        self.root.after(400, self.launch_client)

    def _close_client(self):
        if IS_WIN and self.hwnd and user32.IsWindow(self.hwnd):
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
        self.proc = None
        self.hwnd = None
        self.searching_since = None

    # ------------------------------------------------------------ embed ----
    def _find_window(self):
        title = (self.cfg.get("window_title") or "").lower()
        own = self.root.winfo_id()
        pids = descendant_pids(self.proc.pid) if self.proc else set()
        best = None
        for hwnd, pid, text in top_windows():
            if not text and not title:
                continue
            if title and title in text.lower():
                return hwnd
            if pid in pids:
                best = best or hwnd
            elif not pids and not title and hwnd not in self._known_windows \
                    and "phantasia" in text.lower() and hwnd != own:
                best = best or hwnd    # .lnk launch: new window named Phantasia
        return best

    def _embed(self, hwnd):
        self.hwnd = hwnd
        self.native_size = client_size(hwnd)
        self.placeholder.place_forget()
        adopt(hwnd, self.game.winfo_id())
        self._fit()
        self._focus_game()
        self.set_status("game running", GREEN)

        # First time: size the launcher so the client fits at its own size.
        if not self.cfg.get("geometry"):
            w, h = self.native_size
            if w > 200 and h > 150:
                pw = self.cfg["panel_width"] + 1 if self.cfg["panel"] else 0
                self.root.geometry(f"{w + pw}x{h + 30}")

    def _fit(self):
        if not (IS_WIN and self.hwnd):
            return
        gw, gh = self.game.winfo_width(), self.game.winfo_height()
        if self.cfg["stretch"] or not self.native_size:
            x, y, w, h = 0, 0, gw, gh
        else:
            w, h = self.native_size
            x, y = max(0, (gw - w) // 2), max(0, (gh - h) // 2)
        user32.MoveWindow(self.hwnd, x, y, w, h, True)

    def _focus_game(self):
        if IS_WIN and self.hwnd and user32.IsWindow(self.hwnd):
            user32.SetFocus(self.hwnd)

    def _on_focus(self, e):
        # Clicking the launcher's frame/title should send keys to the game,
        # but don't steal focus from the toolbar buttons.
        if e.widget in (self.root, self.game):
            self._focus_game()

    def _watch(self):
        try:
            self._tick()
        finally:
            self.root.after(300, self._watch)

    def _tick(self):
        if self.proc is not None and self.proc.poll() is not None and \
                not (self.hwnd and IS_WIN and user32.IsWindow(self.hwnd)):
            # Launcher stubs exit after starting the real client; keep looking
            # by title for a while before declaring the game closed.
            if self.searching_since is None:
                self._game_closed()
                return

        if self.hwnd:
            if IS_WIN and not user32.IsWindow(self.hwnd):
                self._game_closed()
            return

        if self.searching_since is None:
            return
        if not IS_WIN:
            # can't embed here; the client just runs in its own window
            self.show_placeholder("Game running in its own window.\n"
                                  "(Embedding the client needs Windows.)")
            self.set_status("game running", GREEN)
            self.searching_since = None
            return

        hwnd = self._find_window()
        if hwnd:
            # give the client a moment to finish creating its window
            self.root.after(250, lambda h=hwnd: self._embed(h)
                            if user32.IsWindow(h) and not self.hwnd else None)
            self.searching_since = None
            return
        if time.time() - self.searching_since > EMBED_TIMEOUT_S:
            self.searching_since = None
            self.show_placeholder(
                "Couldn't find the game window to pull in.\n\n"
                "If it opened on its own, set \"window_title\" in launcher.json\n"
                "to part of its title bar text and press  Relaunch game.")
            self.set_status("game window not found", AMBER)

    def _game_closed(self):
        self.hwnd = None
        self.proc = None
        self.searching_since = None
        self.show_placeholder("The game was closed.\n\nPress  Relaunch game  to start it again.")
        self.set_status("game closed", AMBER)

    # -------------------------------------------------------------- exit ---
    def quit(self):
        if self.root.state() == "normal":
            self.cfg["geometry"] = self.root.geometry()
        save_config(self.cfg)
        self.panel.alive = False
        self.buttons.alive = False
        if self.recorder:
            self.recorder.close()
        if self.cfg.get("close_client_on_exit", True):
            self._close_client()
        elif IS_WIN and self.hwnd and user32.IsWindow(self.hwnd):
            user32.SetParent(self.hwnd, None)   # hand the window back
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Launcher().run()
