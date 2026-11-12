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

Embedding needs Windows. By default the game stays its own window, glued over
the launcher's game area with its title bar clipped off (embed_mode "dock"), so
mouse and keyboard work exactly as normal. embed_mode "reparent" makes it a true
child window instead (Win32 SetParent), which some clients don't take clicks in. On other systems
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
import webbrowser

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
    # How the game is put inside the window:
    #   "dock"     - the game stays its own window, glued over the game area
    #                with its title bar clipped off. Clicks and keys work
    #                normally. (default)
    #   "reparent" - the game becomes a child window of the launcher. Looks
    #                the same, but many clients (Java, .NET, scaled displays)
    #                stop taking clicks or get them in the wrong place.
    "embed_mode": "dock",
    # dock mode: trim the game's title bar off. If that doesn't take on this
    # PC (the game ends up over the toolbar/panel), the launcher notices and
    # shows the game with its own title bar inside the game area instead.
    "hide_titlebar": True,
    "panel": True,                # show the advisor panel
    "panel_width": 340,
    "close_client_on_exit": True,
    "autoplay": False,            # auto-press the advisor's move in the game
    # character creation (with auto-play on): reroll until a roll this good
    # comes up -- about 1 in N rolls for the class. Higher = pickier, slower.
    "reroll_target": 100,
    "reroll_weights": {},         # e.g. {"Halfling": {"brains": 5}}
    # stats a kept roll MUST have at the class maximum (never kept otherwise)
    "reroll_require_max": ["speed"],
    # quietly keep Info -> Stats data (brains, magic level, poison...)
    # current without opening it in the game
    # keep Info -> Stats data (brains, magic level, poison) current quietly.
    # (Older keys "auto_stats"/"quiet_stats" are ignored: earlier versions
    # saved them in launcher.json while the feature was broken / off.)
    "background_stats": True,
    # monsters auto-play always Evades, whatever the odds (name contains)
    "always_evade": ["Moron"],
    # evade monsters whose experience isn't worth the age the fight costs
    "skip_low_value": True,
    # spend mana only when every way of fighting without it is at least
    # this likely to kill you (0.05 = 5%); lower = safer but more mana
    "mana_death": 0.05,
    "record": True,               # record screens + inputs (recorder.py)
    "record_screenshots": False,  # also save a PNG of the game per input
    "geometry": "",
}

EMBED_TIMEOUT_S = 45
BROWSERS = ("chrome", "firefox", "edge", "opera", "brave", "vivaldi",
            "internet explorer")
LOG_PATH = os.path.join(HOME, "launcher.log")

# pythonw / --noconsole: there is no console, so print() and thread errors
# would vanish (or crash code that writes to stderr). Send them to the log.
if sys.stdout is None or sys.stderr is None:
    try:
        _logf = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or _logf
        sys.stderr = sys.stderr or _logf
    except OSError:
        pass


# A crash inside Windows API code kills Python outright, with no exception
# to catch. faulthandler still writes where it happened to the log.
try:
    import faulthandler
    _faultf = open(LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(file=_faultf)
except Exception:
    pass


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass
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

    # Be DPI aware BEFORE any window exists. Otherwise, at 125%/150% display
    # scaling Windows fakes our coordinates, and the game (often DPI aware)
    # gets placed and clipped in the wrong spot -- e.g. over the toolbar.
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)   # system aware
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
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
    GWLP_HWNDPARENT = -8          # on a top-level window: its OWNER
    SW_RESTORE = 9
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
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.CreateRectRgn.argtypes = [ctypes.c_int] * 4
    gdi32.CreateRectRgn.restype = wintypes.HANDLE
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

    def window_rect(hwnd):
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def client_screen_rect(hwnd):
        """The window's client area (no title bar/borders) in screen coords."""
        w, h = client_size(hwnd)
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return pt.x, pt.y, pt.x + w, pt.y + h

    def dock(hwnd, owner):
        """Make `owner` own the window: it then stays above the launcher,
        minimizes with it and leaves the taskbar. Its own styles and input
        handling are untouched, unlike SetParent."""
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        _SetWL(hwnd, GWLP_HWNDPARENT, owner or 0)

    def _target(hwnd, area, stretch, size):
        """Where a rect of `size` goes inside `area`: filled or centred."""
        ax, ay, ar, ab = area
        resizable = bool(_GetWL(hwnd, GWL_STYLE) & WS_THICKFRAME)
        if stretch and resizable:
            return ax, ay, ar - ax, ab - ay
        w, h = size
        return (ax + max(0, ((ar - ax) - w) // 2),
                ay + max(0, ((ab - ay) - h) // 2), w, h)

    def place_docked(hwnd, area, stretch, clip):
        """Fit the game into `area` (screen rect).

        clip=True: put the game's CLIENT area on `area` and cut off its title
        bar/borders with a window region, so it looks built in.
        clip=False: put the WHOLE window (title bar included) inside `area`.
        Less seamless, but nothing can stick out over the toolbar."""
        if user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW
        if not clip:
            wl, wt, wr, wb = window_rect(hwnd)
            x, y, w, h = _target(hwnd, area, stretch, (wr - wl, wb - wt))
            user32.SetWindowPos(hwnd, None, x, y, w, h, flags)
            # A fixed-size window bigger than the area would spill over the
            # panel/off the launcher: clip it to the area. If it fits, no clip.
            ax, ay, ar, ab = area
            wl, wt, wr, wb = window_rect(hwnd)
            if wl >= ax and wt >= ay and wr <= ar and wb <= ab:
                user32.SetWindowRgn(hwnd, None, True)
            else:
                rgn = gdi32.CreateRectRgn(max(wl, ax) - wl, max(wt, ay) - wt,
                                          min(wr, ar) - wl, min(wb, ab) - wt)
                user32.SetWindowRgn(hwnd, rgn, True)
            return
        cl, ct, cr, cb = client_screen_rect(hwnd)
        x, y, cw, ch = _target(hwnd, area, stretch, (cr - cl, cb - ct))
        for _ in range(3):      # measure, move, re-measure: frame sizes vary
            wl, wt, wr, wb = window_rect(hwnd)
            cl, ct, cr, cb = client_screen_rect(hwnd)
            if (cl, ct, cr - cl, cb - ct) == (x, y, cw, ch):
                break
            left, top = cl - wl, ct - wt
            right, bottom = wr - cr, wb - cb
            user32.SetWindowPos(hwnd, None, x - left, y - top,
                                cw + left + right, ch + top + bottom, flags)
        # visible part = client area, and never anything outside `area`
        cl, ct, cr, cb = client_screen_rect(hwnd)
        wl, wt, _, _ = window_rect(hwnd)
        ax, ay, ar, ab = area
        vl, vt = max(cl, ax), max(ct, ay)
        vr, vb = min(cr, ar), min(cb, ab)
        rgn = gdi32.CreateRectRgn(vl - wl, vt - wt, vr - wl, vb - wt)
        user32.SetWindowRgn(hwnd, rgn, True)     # system owns rgn now

    def sticks_out(hwnd, area, panel_side):
        """True if the game covers the launcher outside `area`.

        Hit-testing honours window regions, so this checks what the user
        actually sees and clicks: points on the toolbar just above the game
        area, and on the advisor panel just right of it."""
        ax, ay, ar, ab = area
        pts = [(ax + 12, ay - 4), ((ax + ar) // 2, ay - 4), (ar - 12, ay - 4)]
        if panel_side:
            pts += [(ar + 6, ay + 40), (ar + 6, (ay + ab) // 2)]
        for x, y in pts:
            h = user32.WindowFromPoint(wintypes.POINT(x, y))
            if h and user32.GetAncestor(h, GA_ROOT) == hwnd:
                return True
        return False

    def describe(hwnd):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return (f"title={window_title(hwnd)!r} class={buf.value!r} "
                f"style={_GetWL(hwnd, GWL_STYLE) & 0xFFFFFFFF:#010x} "
                f"exstyle={_GetWL(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF:#010x} "
                f"window={window_rect(hwnd)} client={client_screen_rect(hwnd)}")

    def undock(hwnd):
        user32.SetWindowRgn(hwnd, None, True)
        _SetWL(hwnd, GWLP_HWNDPARENT, 0)

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


def resolve_shortcut(path):
    """(target, args, workdir) of a Windows .lnk, or None."""
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:P4LNK);"
          "$s.TargetPath;$s.Arguments;$s.WorkingDirectory")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "P4LNK": path}, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (out.stdout or "").splitlines() + ["", "", ""]
    target, args, cwd = (x.strip() for x in lines[:3])
    if not target or not os.path.exists(target):
        return None
    return (target, shlex.split(args, posix=False) if args else [],
            cwd or os.path.dirname(target) or None)


# ------------------------------------------------------------ crash report ---
def report_crash(exc_type, exc, tb, where="startup"):
    """pythonw has no console, so errors would vanish. Log them and say so."""
    import traceback
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ({where}) ===\n")
            f.write(text)
    except OSError:
        pass
    msg = (f"Phantasia Advisor hit an error ({where}):\n\n"
           f"{exc_type.__name__}: {exc}\n\nFull details saved to:\n{LOG_PATH}")
    try:
        if IS_WIN:
            ctypes.windll.user32.MessageBoxW(None, msg, "Phantasia Advisor", 0x10)
        else:
            sys.stderr.write(text)
    except Exception:
        sys.stderr.write(text)


# Imported here, after report_crash exists, so a missing module (no tkinter,
# a file left out of the folder) shows a message instead of silently exiting.
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from tkinter import font as tkfont

    import app as proxy
    from buttons import ButtonOverlay
    from overlay import (Overlay, BG, PANEL, LINE, INK, DIM, GOLD, GREEN,
                         AMBER, RED)
except Exception:
    report_crash(*sys.exc_info(), where="loading")
    sys.exit(1)


# ---------------------------------------------------------------- the app ---
class Launcher:
    def __init__(self):
        self.cfg = load_config()
        self.proc = None
        self.hwnd = None
        self.native_size = None
        self.searching_since = None
        self.proxy_note = ""
        self._placed = None
        self._clip = True
        self._known_windows = set()

        self.root = tk.Tk()
        # pixel sizes below are for 100% display scaling; scale them up
        self.ui = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.root.title("Phantasia Advisor")
        self.root.configure(bg=BG)
        self.root.geometry(self.cfg.get("geometry") or
                           f"{self.px(1560)}x{self.px(860)}+40+40")
        self.root.minsize(self.px(640), self.px(420))
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self._errors_shown = 0

        def on_tk_error(et, e, tb):
            # errors inside the running app: log every one, pop up the first few
            if self._errors_shown < 3:
                self._errors_shown += 1
                report_crash(et, e, tb, "while running")
            else:
                import traceback
                try:
                    with open(LOG_PATH, "a", encoding="utf-8") as f:
                        f.write("".join(traceback.format_exception(et, e, tb)))
                except OSError:
                    pass
        self.root.report_callback_exception = on_tk_error

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
        bar = tk.Frame(self.root, bg=BAR_BG, height=self.px(30))
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
        btn("Bestiary", self.open_bestiary)

        self.status = tk.Label(bar, text="", bg=BAR_BG, fg=DIM, font=self.f_sm,
                               anchor="e")
        self.status.pack(side="right", padx=10, fill="x", expand=True)

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(side="top", fill="both", expand=True)

        self.panel_frame = tk.Frame(self.body, bg=BG,
                                    width=self.px(self.cfg["panel_width"]))
        self.panel_frame.pack_propagate(False)
        self.divider = tk.Frame(self.body, bg=LINE, width=1)
        self.game = tk.Frame(self.body, bg="#000000", takefocus=1)
        self._layout_panel()

        self.placeholder = tk.Label(self.game, text="", bg="#000000", fg=DIM,
                                    font=self.f_md, justify="center")
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")

        self.game.bind("<Configure>", lambda e: self._fit())
        self.root.bind("<Configure>", lambda e: self._fit(), add="+")
        self.game.bind("<Button-1>", lambda e: self._focus_game())
        self.root.bind("<FocusIn>", self._on_focus)

        # right column: advisor on top, the auto-play button grid underneath
        self.buttons_frame = tk.Frame(self.panel_frame, bg=BG,
                                      height=self.px(250))
        self.buttons_frame.pack(side="bottom", fill="x")
        self.buttons_frame.pack_propagate(False)
        tk.Frame(self.panel_frame, bg=LINE, height=1).pack(side="bottom", fill="x")
        self.advice_frame = tk.Frame(self.panel_frame, bg=BG)
        self.advice_frame.pack(side="top", fill="both", expand=True)

        self.panel = Overlay(parent=self.advice_frame)
        self.buttons = ButtonOverlay(parent=self.buttons_frame,
                                     autopress=self.cfg["autoplay"],
                                     before_press=self._game_ready_for_keys,
                                     on_autopress_change=self._autoplay_changed,
                                     reroll_opts={
                                         "target_rolls": self.cfg.get("reroll_target", 100),
                                         "weights": self.cfg.get("reroll_weights") or {},
                                         "require_max": self.cfg.get("reroll_require_max", ["speed"])},
                                     always_evade=self.cfg.get("always_evade", ["Moron"]),
                                     skip_low_value=self.cfg.get("skip_low_value", True),
                                     mana_death=self.cfg.get("mana_death", 0.05))
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
        ImageGrab.grab(bbox=client_screen_rect(self.hwnd),
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

    def open_bestiary(self):
        """Simulate fights against every monster with the live stats
        (bestiary.py) in a separate process, so it doesn't lag the game;
        it opens bestiary.html in the browser when done (~30 s)."""
        script = os.path.join(HOME, "bestiary.py")
        if FROZEN or not os.path.exists(script):
            messagebox.showinfo("Bestiary", "Run  python bestiary.py  from the "
                                "p4help folder while the launcher is running.")
            return
        p = proxy.STATE.get("player") or {}
        if not p.get("strength"):
            messagebox.showinfo("Bestiary", "Log in first, so your stats are known.")
            return
        flags = 0x08000000 if IS_WIN else 0     # no console window
        subprocess.Popen([sys.executable, script], cwd=HOME, creationflags=flags)
        self.set_status("bestiary: simulating every monster (about 30 s)...", AMBER)

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
        top = self._top_hwnd()
        fg = user32.GetForegroundWindow()
        if fg not in (top, self.hwnd):
            return False
        if self._docked():
            if fg != self.hwnd:
                user32.SetForegroundWindow(self.hwnd)
        elif threading.current_thread() is threading.main_thread():
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
        # "auto_stats" (old key, saved as true in existing launcher.json files)
        # is deliberately ignored: the quiet fetch locked up the live game.
        proxy.AUTO_STATS = bool(self.cfg.get("background_stats", True))
        proxy.CAL_PATH = os.path.join(HOME, "calibration.json")
        try:                      # the advisor's own mana threshold too
            import planner
            planner.MANA_DEATH = float(self.cfg.get("mana_death", 0.05))
        except (ImportError, TypeError, ValueError):
            pass
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

    def _command(self, path, extra=()):
        args = list(extra) + shlex.split(self.cfg.get("client_args") or "",
                                         posix=not IS_WIN)
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
            target, extra, cwd = path, [], os.path.dirname(path) or None
            if IS_WIN and os.path.splitext(path)[1].lower() == ".lnk":
                lnk = resolve_shortcut(path)
                if lnk:
                    target, extra, cwd = lnk
            if IS_WIN and os.path.splitext(target)[1].lower() == ".lnk":
                # couldn't read the shortcut: let the shell open it and find
                # the window by title instead of process id.
                os.startfile(target)
                self.proc = None
            else:
                flags = 0x08000000 if IS_WIN else 0   # CREATE_NO_WINDOW
                self.proc = subprocess.Popen(self._command(target, extra),
                                             cwd=cwd, creationflags=flags)
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
        me = os.getpid()
        pids = descendant_pids(self.proc.pid) if self.proc else set()
        best = None
        for hwnd, pid, text in top_windows():
            if pid == me:
                continue   # never swallow our own window ("Phantasia Advisor")
            if not text and not title:
                continue
            low = text.lower()
            if any(b in low for b in BROWSERS):
                continue   # a phantasia5.com browser tab is not the game
            if title and title in low:
                return hwnd
            if pid in pids:
                best = best or hwnd
            elif not pids and not title and hwnd not in self._known_windows \
                    and "phantasia" in low:
                best = best or hwnd    # shell launch: new window named Phantasia
        return best

    def px(self, n):
        return int(round(n * self.ui))

    def _docked(self):
        return self.cfg.get("embed_mode", "dock") != "reparent"

    def _top_hwnd(self):
        return user32.GetAncestor(self.game.winfo_id(), GA_ROOT)

    def _embed(self, hwnd):
        self.hwnd = hwnd
        self.native_size = client_size(hwnd)
        self._placed = None
        self._clip = bool(self.cfg.get("hide_titlebar", True))
        log(f"game window found (pid tree {sorted(descendant_pids(self.proc.pid)) if self.proc else 'shell'}): "
            f"{describe(hwnd)}")
        if self._docked():
            dock(hwnd, self._top_hwnd())
            self.show_placeholder("")       # black backdrop behind the game
        else:
            self.placeholder.place_forget()
            adopt(hwnd, self.game.winfo_id())
        self._fit()
        self._focus_game()
        self.set_status("game running", GREEN)

        # First time: size the launcher so the client fits at its own size.
        if not self.cfg.get("geometry"):
            w, h = self.native_size
            if w > 200 and h > 150:
                pw = self.px(self.cfg["panel_width"]) + 1 if self.cfg["panel"] else 0
                self.root.geometry(f"{w + pw}x{h + self.px(30)}")

    def _fit(self):
        if not (IS_WIN and self.hwnd):
            return
        if self._docked():
            if not user32.IsWindow(self.hwnd):
                return
            if user32.IsIconic(self._top_hwnd()):
                return                      # launcher minimized: leave it
            area = window_rect(self.game.winfo_id())
            if area[2] - area[0] < 20 or area[3] - area[1] < 20:
                return
            # re-place when the launcher moved/resized OR the game got moved
            # (e.g. Alt+Space > Move); cheap enough to check every tick
            key = (area, window_rect(self.hwnd), self._clip)
            if key != self._placed:
                first = self._placed is None
                place_docked(self.hwnd, area, self.cfg["stretch"], self._clip)
                self._placed = (area, window_rect(self.hwnd), self._clip)
                if first:
                    log(f"placed (clip={self._clip}) area={area} "
                        f"{describe(self.hwnd)}")
            # The guarantee: the game must never cover the toolbar or panel.
            # If trimming the title bar didn't take, show it inside instead.
            if self._clip and sticks_out(self.hwnd, area, self.cfg["panel"]):
                log("game covers the toolbar/panel; showing its title bar "
                    "inside the game area instead")
                self._clip = False
                self._placed = None
                self._fit()
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
            if self._docked():
                # only when the launcher is in front, so we never pull the
                # game up over some other app the player switched to
                if user32.GetForegroundWindow() == self._top_hwnd():
                    user32.SetForegroundWindow(self.hwnd)
            else:
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
            else:
                self._fit()     # docked: follow the launcher if it moved
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
            if self._docked():
                undock(self.hwnd)               # hand the window back
            else:
                user32.SetParent(self.hwnd, None)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    try:
        Launcher().run()
    except Exception:
        report_crash(*sys.exc_info())
        sys.exit(1)
