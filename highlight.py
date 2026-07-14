#!/usr/bin/env python3
"""
Button highlighter — a transparent, CLICK-THROUGH strip that sits directly on
top of Phantasia's 8 action buttons and lights up the one you should press.

Unlike buttons.py (a separate grid you glance at), this draws the green
highlight ON the real game buttons. Your clicks pass straight through to the
button underneath -- the overlay only paints, it never intercepts.

    python highlight.py

It reads the advisor feed at 127.0.0.1:8420/state and never touches the game.

FIRST RUN — LINE IT UP:
    The 8 highlight boxes start at coordinates read from your screenshot. If
    they don't sit exactly over your buttons (different window size/position),
    nudge them:
        arrow keys       move all boxes 2px
        Shift+arrows     move 10px
        Ctrl+Left/Right  make boxes narrower / wider
        Ctrl+Up/Down     make boxes shorter / taller
        [  ]             tighten / loosen the gap between boxes
        S                save the alignment (so it's remembered next time)
        F9               hide / show
        Esc              quit
    A faint outline shows all 8 boxes while aligning; it fades once locked.

BUTTON MEANINGS (verified fight.c:826 / :2155) -- same 8 slots switch between
combat and the spell submenu:
    combat:  1 Melee  2 Skirmish  3 Nick  4 Spell  5 Rest  6 Luckout  7 Evade  8 Ring
    spell :  1 AllOrNothing  2 MagicBolt  5 ForceField  8 Transform
"""

import tkinter as tk
from tkinter import font as tkfont
import json
import os
import queue
import sys
import threading
import urllib.request
import urllib.error

POLL_URL = "http://127.0.0.1:8420/state"
POLL_MS = 350
PAINT_MS = 90
CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "highlight.cfg")

GREEN = "#2ee66b"
GREEN2 = "#7bffb0"
AMBER = "#ffb43a"

# Default geometry, read off the provided screenshot (1187px-wide window):
#   8 buttons, left edge ~15px, each ~100px wide with ~8px gaps, top ~448, h ~40
DEFAULT = dict(x=17, y=449, w=98, h=40, gap=10)

# advisor move -> button slot (1-8). Spells are a two-step: Spell(4) then sub.
MOVE_SLOT = {
    "melee": 1, "skirmish": 2, "nick": 3, "luckout": 6, "evade": 7, "ring": 8,
}
SPELL_SUB = {          # after Spell(4) is pressed, the sub-button slot
    "bolt": 2, "aon": 1, "forcefield": 5, "transform": 8,
}


class Highlighter:
    def __init__(self):
        self.g = dict(DEFAULT)
        self._load()

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        # transparent: paint on a colorkey background that becomes see-through
        self.transparent = "#010203"
        self.root.configure(bg=self.transparent)
        # -transparentcolor is Windows-only; it's what makes the window's
        # background vanish so only the highlight boxes show. On other platforms
        # fall back to a low overall alpha.
        try:
            self.root.attributes("-transparentcolor", self.transparent)
            self.root.attributes("-alpha", 0.92)
        except tk.TclError:
            self.root.attributes("-alpha", 0.55)
        self.root.geometry(self._winspec())

        self.canvas = tk.Canvas(self.root, bg=self.transparent,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.font = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        self.q = queue.Queue()
        self.data = {}
        self.alive = True
        self._hidden = False
        self._flash = 0
        self._aligning = not os.path.exists(CFG)   # skip if already aligned
        self._click_through = False

        self._bind()
        # NOTE: click-through is only enabled AFTER you save the alignment.
        # While aligning, the window must accept focus or the keys do nothing.
        if not self._aligning:
            self.root.after(200, self._make_click_through)
        else:
            self.root.after(200, self._focus_for_align)
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._paint()

    # ---- geometry ----
    def _winspec(self):
        g = self.g
        total_w = g["x"] + (g["w"] + g["gap"]) * 8 + 20
        total_h = g["y"] + g["h"] + 20
        # full-screen-width strip anchored at 0,0 so button coords are absolute
        return f"{total_w}x{total_h}+0+0"

    def _slot_rect(self, slot):
        """Pixel rect of button `slot` (1-8) in this window."""
        g = self.g
        i = slot - 1
        x0 = g["x"] + i * (g["w"] + g["gap"])
        return x0, g["y"], x0 + g["w"], g["y"] + g["h"]

    def _load(self):
        try:
            with open(CFG) as f:
                self.g.update(json.load(f))
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            with open(CFG, "w") as f:
                json.dump(self.g, f)
            self._aligning = False
            self._toast("aligned - clicks now pass through")
            self.root.attributes("-alpha", 0.92)
            self.root.after(300, self._make_click_through)
        except OSError:
            pass

    def _realign(self):
        """Re-enter alignment mode (F10) -- turns click-through back off."""
        self._aligning = True
        self._unmake_click_through()
        self._focus_for_align()
        self._toast("alignment mode - arrows to move, S to save")

    def _focus_for_align(self):
        """While aligning, grab focus so the arrow keys actually work."""
        self.root.attributes("-alpha", 0.85)
        self.root.lift()
        self.root.focus_force()
        self.canvas.focus_set()

    def _unmake_click_through(self):
        """Turn click-through OFF so the window can take keys again."""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x20
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style & ~WS_EX_TRANSPARENT)
            self._click_through = False
        except Exception:
            pass

    # ---- click-through (Windows) ----
    def _make_click_through(self):
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x80000
            WS_EX_TRANSPARENT = 0x20
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
            self._click_through = True
        except Exception:
            self._click_through = False

    def _bind(self):
        r = self.root
        r.bind("<Escape>", lambda e: self._quit())
        r.bind("<F9>", lambda e: self._toggle())
        r.bind("<s>", lambda e: self._save())
        r.bind("<S>", lambda e: self._save())
        # nudge
        for key, dx, dy in (("Left", -2, 0), ("Right", 2, 0),
                            ("Up", 0, -2), ("Down", 0, 2)):
            r.bind(f"<{key}>", lambda e, dx=dx, dy=dy: self._nudge(dx, dy))
            r.bind(f"<Shift-{key}>",
                   lambda e, dx=dx, dy=dy: self._nudge(dx * 5, dy * 5))
        r.bind("<Control-Left>", lambda e: self._resize(-2, 0))
        r.bind("<Control-Right>", lambda e: self._resize(2, 0))
        r.bind("<Control-Up>", lambda e: self._resize(0, -2))
        r.bind("<Control-Down>", lambda e: self._resize(0, 2))
        r.bind("<bracketleft>", lambda e: self._gap(-1))
        r.bind("<bracketright>", lambda e: self._gap(1))
        r.bind("<F10>", lambda e: self._realign())
        # drag the boxes with the mouse while aligning
        self.canvas.bind("<Button-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, e):
        if not self._aligning:
            return
        self._dx0, self._dy0 = e.x, e.y
        self._gx0, self._gy0 = self.g["x"], self.g["y"]

    def _drag_move(self, e):
        if not self._aligning:
            return
        self.g["x"] = self._gx0 + (e.x - self._dx0)
        self.g["y"] = self._gy0 + (e.y - self._dy0)
        self.root.geometry(self._winspec())

    def _nudge(self, dx, dy):
        self.g["x"] += dx
        self.g["y"] += dy
        self._aligning = True
        self.root.geometry(self._winspec())

    def _resize(self, dw, dh):
        self.g["w"] = max(20, self.g["w"] + dw)
        self.g["h"] = max(16, self.g["h"] + dh)
        self._aligning = True
        self.root.geometry(self._winspec())

    def _gap(self, dg):
        self.g["gap"] = max(0, self.g["gap"] + dg)
        self._aligning = True
        self.root.geometry(self._winspec())

    def _toggle(self):
        self.root.withdraw() if not self._hidden else self.root.deiconify()
        self._hidden = not self._hidden

    def _quit(self):
        self.alive = False
        self.root.destroy()

    def _toast(self, msg):
        self._toast_msg = msg
        self._toast_ttl = 20

    # ---- polling ----
    def _poll_loop(self):
        ev = threading.Event()
        while self.alive:
            try:
                with urllib.request.urlopen(POLL_URL, timeout=2) as r:
                    self.q.put(json.loads(r.read()))
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                self.q.put(None)
            ev.wait(POLL_MS / 1000)

    # ---- paint ----
    def _paint(self):
        if not self.alive:
            return
        while True:
            try:
                self.data = self.q.get_nowait() or {}
            except queue.Empty:
                break
        self._flash = (self._flash + 1) % 10
        try:
            self._draw()
        except tk.TclError:
            return
        self.root.after(PAINT_MS, self._paint)

    def _draw(self):
        c = self.canvas
        c.delete("all")

        # alignment outlines
        if self._aligning:
            for slot in range(1, 9):
                x0, y0, x1, y1 = self._slot_rect(slot)
                c.create_rectangle(x0, y0, x1, y1, outline="#3a5cff", width=2)
                c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=str(slot),
                              fill="#3a5cff", font=self.font)
            # instructions, drawn ABOVE the boxes so they're readable
            y = max(4, self.g["y"] - 58)
            c.create_rectangle(self.g["x"] - 4, y - 4,
                               self.g["x"] + 560, y + 50,
                               fill="#0d1020", outline="#3a5cff")
            for i, line in enumerate([
                "ALIGN THE BOXES OVER YOUR 8 GAME BUTTONS",
                "drag with mouse  ·  arrows = nudge  ·  Ctrl+arrows = resize  ·  [ ] = gap",
                "press  S  to save  (clicks then pass through to the game)",
            ]):
                c.create_text(self.g["x"], y + 2 + i * 16, anchor="nw",
                              text=line,
                              fill="#7bffb0" if i == 2 else "#9fb4ff",
                              font=self.font)

        # toast
        if getattr(self, "_toast_ttl", 0) > 0:
            self._toast_ttl -= 1
            c.create_text(self.g["x"], self.g["y"] + self.g["h"] + 12,
                          anchor="w", text=self._toast_msg, fill=GREEN2,
                          font=self.font)

        d = self.data
        f = d.get("fight")
        if not f or not f.get("name") or not f.get("moves"):
            return

        mv = f["moves"][0]
        move = mv["move"]

        # which slot to light?
        slot = MOVE_SLOT.get(move)
        sub = SPELL_SUB.get(move)
        pulse = GREEN if self._flash < 5 else GREEN2

        if sub is not None:
            # spell: light Spell(4), and hint the sub-button + bolt amount
            self._ring(4, pulse, "SPELL")
            amt = f" {int(mv['arg'])} mana" if move == "bolt" and mv.get("arg") else ""
            label = {"bolt": "then Magic Bolt", "aon": "then All-or-Nothing",
                     "forcefield": "then Force Field",
                     "transform": "then Transform"}.get(move, "")
            x0, y0, x1, y1 = self._slot_rect(4)
            self.canvas.create_text((x0 + x1) / 2, y1 + 12, anchor="n",
                                    text=f"{label}{amt}", fill=GREEN2,
                                    font=self.font)
        elif slot is not None:
            self._ring(slot, pulse, None)

    def _ring(self, slot, color, tag):
        x0, y0, x1, y1 = self._slot_rect(slot)
        # bright rounded border + soft fill; fill uses near-transparent alpha
        for i, w in ((6, 1), (3, 1)):
            self.canvas.create_rectangle(x0 - i, y0 - i, x1 + i, y1 + i,
                                         outline=color, width=2)
        if tag:
            self.canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2,
                                    text=tag, fill=color, font=self.font)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Highlighter().run()
