#!/usr/bin/env python3
"""
Button overlay — lights up and presses the button you should press.

A small always-on-top grid that mirrors the game's 8 action buttons. The
advisor's recommended move is lit GREEN so you can't miss it, and the matching
number key is tapped once when the recommendation changes. For spells it walks
you through the sub-steps (Spell -> Magic Bolt -> type amount), and for bolt it
shows the exact mana to one-shot the monster.

    python buttons.py

Reads the same proxy feed as the main overlay (127.0.0.1:8420/state).

BUTTON LAYOUT (verified from fight.c:826 and :2155):

    combat menu                    spell submenu (after pressing Spell)
    1 Melee      5 Rest            1 All or Nothing
    2 Skirmish   6 Luckout/Ally    2 Magic Bolt
    3 Nick       7 Evade           5 Force Field
    4 Spell      8 Use Ring        8 Transform

Controls: drag to move, right-click to close, F9 hide/show, F8 auto-press on/off.
"""

import tkinter as tk
from tkinter import font as tkfont
import json
import ctypes
from ctypes import wintypes
import os
import queue
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.error

import reroll
from statsync import is_treasure

POLL_URL = "http://127.0.0.1:8420/state"
POLL_MS = 150     # how often to read the proxy's state (local, cheap)
PAINT_MS = 100
AUTO_PRESS = (
    os.environ.get("P4HELP_BUTTONS_AUTO_PRESS", "1").lower()
    not in {"0", "false", "no", "off"}
)
KEY_DEBOUNCE_S = 0.2
KEY_REPEAT_S = 1.25
BOLT_TYPE_DELAY_S = 0.35
# A reroll repeats only if the same roll is STILL on screen this long after
# pressing (the key got lost). Shorter would risk rerolling the next roll
# before it's been judged.
ROLL_REPEAT_S = 3.0
# Resting: press Rest again as soon as energy changes, or after this long if
# it didn't. REST_STALLS rests in a row that don't raise energy = stop (it
# can't fill: poisoned, or something else is draining it). 1 if poisoned.
REST_REPEAT_S = 2.0
REST_STALLS = 3
# Treasure: wait this long for a fresh stat sheet (is the last fight's poison
# known?) before answering on the last known poison value.
TREASURE_WAIT_S = 8.0
# Monsters to Evade every round no matter what the advisor's odds say.
# Matched case-insensitively against the monster's name ("A Moron").
ALWAYS_EVADE = ["Moron"]
# Survival rules on top of the advisor's pick (auto-play only):
SAFE_MAX_DEATH = 0.02     # advisor's move kills you more often than this -> safest move
GROUP_RESERVE = 0.50      # in a group, don't let a fight leave you below this of max energy
LOW_ENERGY = 0.30         # below this of max energy, take the safest move ...
SURE_WIN = 0.97           # ... unless the fight is this close to certain

BG    = "#16130e"
PANEL = "#211c14"
LINE  = "#4a3f2a"
INK   = "#e8dcc0"
DIM   = "#8a7d63"
GREEN = "#4caf50"
GREEN_HI = "#66d96b"
AMBER = "#e0982e"
RED   = "#cc5038"
BLUE  = "#5a94c0"

# --- the game's combat button grid (1-8), as the player sees it ---
COMBAT_LAYOUT = [
    ("Melee", 1), ("Skirmish", 2), ("Nick", 3), ("Spell", 4),
    ("Rest", 5), ("Luckout", 6), ("Evade", 7), ("Use Ring", 8),
]
SPELL_LAYOUT = [
    ("All or Nothing", 1), ("Magic Bolt", 2), ("(3)", 3), ("(4)", 4),
    ("Force Field", 5), ("(6)", 6), ("(7)", 7), ("Transform", 8),
]

# advisor move name -> which combat button (and sub-button for spells)
MOVE_TO_BUTTON = {
    "melee":    ("Melee", 1, None),
    "skirmish": ("Skirmish", 2, None),
    "nick":     ("Nick", 3, None),
    "luckout":  ("Luckout", 6, None),
    "evade":    ("Evade", 7, None),
    "ring":     ("Use Ring", 8, None),
    # spells: press Spell (4), then the sub-button
    "bolt":     ("Spell", 4, ("Magic Bolt", 2)),
    "aon":      ("Spell", 4, ("All or Nothing", 1)),
    "might":    ("Spell", 4, ("Increase Might", None)),
    "paralyze": ("Spell", 4, ("Paralyze", None)),
    "forcefield": ("Spell", 4, ("Force Field", 5)),
}


class ButtonOverlay:
    def __init__(self, parent=None, autopress=None, before_press=None,
                 on_autopress_change=None, reroll_opts=None,
                 always_evade=None):
        """parent=None: standalone floating window.

        parent=<Frame>: render inside that frame (launcher.py docks it beside
        the embedded game). before_press() is called before every key tap and
        must return True for the key to be sent; the launcher uses it to make
        sure keys only ever go to the game window.
        """
        self.embedded = parent is not None
        self.before_press = before_press
        self.on_autopress_change = on_autopress_change
        if self.embedded:
            self.root = parent
        else:
            self.root = tk.Tk()
            self.root.title("Phantasia Buttons")
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            self.root.attributes("-alpha", 0.95)
            self.root.geometry("300x250+900+560")
        self.root.configure(bg=BG)

        self.f_lbl = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.f_num = tkfont.Font(family="Segoe UI", size=8)
        self.f_tiny = tkfont.Font(family="Segoe UI", size=8)
        self.f_hint = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.f_big = tkfont.Font(family="Segoe UI", size=13, weight="bold")

        self.q = queue.Queue()
        self.data = {}
        self.alive = True
        self._hidden = False
        self._sig = None
        self._last_press_sig = None
        self._last_press_at = 0.0
        self._last_bolt_type_sig = None
        self._autopress = AUTO_PRESS if autopress is None else bool(autopress)
        self._flash = 0
        # character creation: Reroll until the stats are top-tier for the class
        self.judge = reroll.RollJudge(**(reroll_opts or {}))
        # monsters to always flee, whatever the odds (matched by name)
        self.always_evade = [str(n).strip().lower() for n in
                             (ALWAYS_EVADE if always_evade is None else always_evade)
                             if str(n).strip()]
        # resting after fights
        self._rest_last_e = None     # best energy reached while resting
        self._rest_stalls = 0        # rests in a row that didn't raise energy
        self._rest_stopped = None    # energy we gave up at (stays until it moves)
        self._treasure_seen = None   # (prompt, labels) and when it appeared

        self._build()
        if not self.embedded:
            self._bind()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._paint()

    def _build(self):
        outer = tk.Frame(self.root, bg=LINE)
        outer.pack(fill="both", expand=True)
        shell = tk.Frame(outer, bg=BG)
        shell.pack(fill="both", expand=True, padx=1, pady=1)

        self.bar = tk.Frame(shell, bg="#0f0d09", height=20)
        self.bar.pack(fill="x")
        self.bar.pack_propagate(False)
        tk.Label(self.bar, text="PRESS THIS", bg="#0f0d09", fg=GREEN,
                 font=self.f_tiny).pack(side="left", padx=8)
        self.auto_lbl = tk.Label(
            self.bar,
            text="AUTO ON" if self._autopress else "AUTO OFF",
            bg="#0f0d09",
            fg=GREEN if self._autopress else RED,
            font=self.f_tiny,
        )
        self.auto_lbl.pack(side="right", padx=8)
        if self.embedded:
            self.auto_lbl.configure(cursor="hand2")
            self.auto_lbl.bind("<Button-1>", lambda e: self._toggle_autopress())
        else:
            tk.Label(self.bar, text="F8 auto  F9 hide", bg="#0f0d09", fg=DIM,
                     font=self.f_tiny).pack(side="right", padx=8)

        # top hint line: the move + bolt amount / sub-steps
        self.hint = tk.Label(shell, text="waiting…", bg=BG, fg=DIM,
                             font=self.f_hint, wraplength=280, justify="center")
        self.hint.pack(fill="x", pady=(6, 2))
        self.sub = tk.Label(shell, text="", bg=BG, fg=AMBER,
                            font=self.f_tiny, wraplength=280, justify="center")
        self.sub.pack(fill="x")

        # 4x2 button grid mirroring the game
        grid = tk.Frame(shell, bg=BG)
        grid.pack(fill="both", expand=True, padx=8, pady=8)
        self.cells = {}
        for i, (label, num) in enumerate(COMBAT_LAYOUT):
            r, c = divmod(i, 2)
            cell = tk.Frame(grid, bg=PANEL, highlightbackground=LINE,
                            highlightthickness=1, width=130, height=38)
            cell.grid(row=r, column=c, padx=3, pady=3, sticky="nsew")
            cell.grid_propagate(False)
            grid.columnconfigure(c, weight=1)
            grid.rowconfigure(r, weight=1)
            n = tk.Label(cell, text=str(num), bg=PANEL, fg=DIM, font=self.f_num)
            n.place(x=5, y=3)
            lbl = tk.Label(cell, text=label, bg=PANEL, fg=INK, font=self.f_lbl)
            lbl.place(relx=0.5, rely=0.5, anchor="center")
            self.cells[num] = {"frame": cell, "label": lbl, "num": n,
                               "base": label}

    def _bind(self):
        for w in (self.bar,) + tuple(self.bar.winfo_children()):
            w.bind("<Button-1>", self._grab)
            w.bind("<B1-Motion>", self._drag)
        self.root.bind("<F9>", lambda e: self._toggle())
        self.root.bind("<F8>", lambda e: self._toggle_autopress())
        self.root.bind("<Escape>", lambda e: self._quit())
        self.root.bind("<Button-3>", self._menu)

    def _grab(self, e):
        self._ox, self._oy = e.x_root, e.y_root
        self._wx, self._wy = self.root.winfo_x(), self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{self._wx + e.x_root - self._ox}"
                           f"+{self._wy + e.y_root - self._oy}")

    def _toggle(self):
        self.root.withdraw() if not self._hidden else self.root.deiconify()
        self._hidden = not self._hidden

    def _toggle_autopress(self):
        self.set_autopress(not self._autopress)

    def set_autopress(self, on):
        on = bool(on)
        changed = on != self._autopress
        self._autopress = on
        self.auto_lbl.configure(
            text="AUTO ON" if self._autopress else "AUTO OFF",
            fg=GREEN if self._autopress else RED,
        )
        # forget what was pressed so turning it back on acts immediately
        self._last_press_sig = None
        if changed and self.on_autopress_change:
            self.on_autopress_change(on)

    def _allowed(self):
        return self.before_press is None or bool(self.before_press())

    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Close", command=self._quit)
        m.tk_popup(e.x_root, e.y_root)

    def _quit(self):
        self.alive = False
        self.root.destroy()

    def _poll_loop(self):
        ev = threading.Event()
        while self.alive:
            try:
                with urllib.request.urlopen(POLL_URL, timeout=2) as r:
                    self.q.put(json.loads(r.read()))
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                self.q.put(None)
            ev.wait(POLL_MS / 1000)

    def _layout(self, spell_mode):
        """Swap the grid labels between combat and spell submenu."""
        layout = SPELL_LAYOUT if spell_mode else COMBAT_LAYOUT
        for label, num in layout:
            c = self.cells[num]
            if c["base"] != label:
                c["label"].configure(text=label)
                c["base"] = label

    def _paint(self):
        if not self.alive:
            return
        got = False
        while True:
            try:
                self.data = self.q.get_nowait() or {}
                got = True
            except queue.Empty:
                break
        try:
            self._flash = (self._flash + 1) % 2
            self._update()
        except tk.TclError:
            return
        self.root.after(PAINT_MS, self._paint)

    def _reset_cells(self):
        for c in self.cells.values():
            c["frame"].configure(bg=PANEL, highlightbackground=LINE,
                                  highlightthickness=1)
            c["label"].configure(bg=PANEL, fg=INK)
            c["num"].configure(bg=PANEL, fg=DIM)

    def _light(self, num, color, hi):
        c = self.cells.get(num)
        if not c:
            return
        c["frame"].configure(bg=color, highlightbackground=hi,
                             highlightthickness=3)
        c["label"].configure(bg=color, fg="#0c0a07")
        c["num"].configure(bg=color, fg="#0c0a07")


    def _press_key(self, num, sig, *, repeat_same=False, repeat_s=KEY_REPEAT_S):
        """Tap the number key matching the lit button.

        Returns True only when a key was actually sent.

        Normal combat actions are sent when the recommendation changes, then
        retried at a conservative interval while the same prompt remains visible.
        Combat rounds often reuse the same labels/recommendation, so treating an
        identical signature as permanently handled can leave the overlay lit but
        no longer pressing. Repeating prompts like More still use only the short
        debounce because the visible button text can stay identical across pages.
        """
        if not self._autopress or num is None:
            return False
        now = time.monotonic()
        if now - self._last_press_at < KEY_DEBOUNCE_S:
            return False
        if not repeat_same and sig == self._last_press_sig:
            if now - self._last_press_at < repeat_s:
                return False
        if not 1 <= int(num) <= 8:
            return False
        if not self._allowed():
            return False

        key = str(num)
        sent = False
        if sys.platform.startswith("win"):
            sent = self._send_windows_key(key)
        if not sent:
            sent = self._send_external_key(key)

        if sent:
            self._last_press_sig = sig
            self._last_press_at = now
        return sent

    def _type_text_then_enter(self, text):
        """Type a short value and press Enter after a submenu key opens a prompt."""
        if not self._autopress:
            return

        def worker():
            time.sleep(BOLT_TYPE_DELAY_S)
            if not self._autopress or not self._allowed():
                return
            if sys.platform.startswith("win"):
                for ch in str(text):
                    self._send_windows_key(ch)
                    time.sleep(0.02)
                self._send_windows_enter()
            else:
                try:
                    subprocess.run(
                        ["xdotool", "type", "--clearmodifiers", str(text)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                    subprocess.run(
                        ["xdotool", "key", "--clearmodifiers", "Return"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                except OSError:
                    return

        threading.Thread(target=worker, daemon=True).start()

    def _send_external_key(self, key):
        """Send a real number-key tap on non-Windows systems when xdotool exists."""
        try:
            return subprocess.run(
                ["xdotool", "key", "--clearmodifiers", key],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
        except OSError:
            return False

    def _send_windows_key(self, key):
        """Send a real number-key tap on Windows without external packages."""
        try:
            INPUT_KEYBOARD = 1
            KEYEVENTF_KEYUP = 0x0002
            vk = ord(key)

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
                ]

            class INPUTUNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]

            extra = ctypes.c_ulong(0)
            inputs = (INPUT * 2)(
                INPUT(
                    INPUT_KEYBOARD,
                    INPUTUNION(KEYBDINPUT(vk, 0, 0, 0, ctypes.pointer(extra))),
                ),
                INPUT(
                    INPUT_KEYBOARD,
                    INPUTUNION(
                        KEYBDINPUT(
                            vk, 0, KEYEVENTF_KEYUP, 0, ctypes.pointer(extra)
                        )
                    ),
                ),
            )
            sent = ctypes.windll.user32.SendInput(2, inputs, ctypes.sizeof(INPUT)) == 2
            if sent:
                return True
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            return True
        except Exception:
            return False

    def _send_windows_enter(self):
        try:
            vk_return = 0x0D
            ctypes.windll.user32.keybd_event(vk_return, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk_return, 0, 0x0002, 0)
            return True
        except Exception:
            return False

    def _button_texts(self):
        """Return the currently visible game button labels, normalized."""
        return [str(b).strip().lower() for b in (self.data.get("buttons") or [])]

    def _spell_menu_visible(self):
        """True when the actual game buttons are showing the spell submenu."""
        btns = self._button_texts()
        return any(
            label in btns
            for label in ("magic bolt", "all or nothing", "force field", "transform")
        )

    def _more_prompt_visible(self):
        """True when the first game button is the More prompt."""
        btns = self._button_texts()
        return bool(btns and btns[0] == "more")

    @staticmethod
    def _safe_move(f, p):
        """The advisor's top move, unless a survival rule says otherwise.

        The advisor scores each fight on its own; a group of monsters (each
        killed one calls a friend) can still grind you down, and its top
        pick can carry real death risk. Returns a move dict; "safety" says
        which rule changed it."""
        moves = f.get("moves") or []
        top = moves[0]
        death = lambda m: m.get("death", max(0.0, 1 - m.get("win", 0) - m.get("flee", 0)))
        safest = min(moves, key=death)
        evade = next((m for m in moves if m.get("move") == "evade"), None)
        e, mx = p.get("energy") or 0, p.get("max_energy") or 0

        def pick(m, why):
            return dict(m, safety=why) if m is not top else top

        if death(top) > SAFE_MAX_DEATH and death(safest) < death(top):
            return pick(safest, f"{death(top)*100:.0f}% death risk")
        if mx:
            after = e - top.get("energy_lost", 0)
            if (f.get("flock") and evade and after < GROUP_RESERVE * mx
                    and death(evade) <= death(top) + 0.05):
                return pick(evade, f"group: would drop to {after:.0f}/{mx:g} energy")
            if (e < LOW_ENERGY * mx and top.get("win", 0) < SURE_WIN
                    and death(safest) < death(top)):
                return pick(safest, f"low energy {e:g}/{mx:g}")
        return top

    def _forced_evade(self, name):
        """The always-evade entry this monster matches, or None."""
        low = (name or "").lower()
        return next((n for n in self.always_evade if n in low), None)

    @staticmethod
    def _round(f):
        """The fight's round number. Part of every fight key's signature, so
        each new round is a new decision and is pressed at once, instead of
        waiting out the repeat timer when the same move wins round after
        round (Melee, Melee, Melee...)."""
        st = f.get("state") or {}
        return (st.get("rounds"), round(st.get("m_energy", -1), 1))

    def _show_labels(self, labels):
        """Show the game's actual button labels in the grid."""
        for num in range(1, 9):
            text = labels[num - 1] if num - 1 < len(labels) else ""
            c = self.cells[num]
            if c["base"] != text:
                c["label"].configure(text=text)
                c["base"] = text

    def _roll_screen(self, roll, labels):
        dec = self.judge.decide(roll)
        self._show_labels(labels)
        self._reset_cells()
        n, cls = roll.get("n"), dec.get("class") or "?"
        if dec["action"] is None:
            self.hint.configure(text=f"Roll {n}: can't judge", fg=AMBER)
            self.sub.configure(text=dec["why"])
            return
        want = dec["action"]                       # "reroll" or "keep"
        low = [b.strip().lower() for b in labels]
        num = low.index(want) + 1 if want in low else None
        col = GREEN if self._flash else GREEN_HI
        if num:
            self._light(num, col, GREEN_HI)
            self._press_key(num, ("roll", roll.get("char"), n, want),
                            repeat_s=ROLL_REPEAT_S)
        verb = "KEEP" if want == "keep" else "Reroll"
        self.hint.configure(text=f"Roll {n} \u00b7 {cls} \u2192 {verb}"
                                 + (f" ({num})" if num else ""),
                            fg=GREEN_HI if want == "keep" else INK)
        if dec["score"] is not None and dec["need"] is not None:
            self.sub.configure(text=f"score {dec['score']:.2f} / keep at "
                                    f"{dec['need']:.2f} \u00b7 {dec['why']}")
        else:
            self.sub.configure(text=dec["why"])

    def _treasure_step(self, d):
        """'You have found some treasure. Do you want to inspect it?' and the
        like: Yes when not poisoned, No when poisoned. Waits briefly for a
        stat sheet read after the fight, since that fight may have poisoned
        us. Returns True when it handled the panel."""
        labels = d.get("buttons") or []
        low = [str(b).strip().lower() for b in labels]
        prompt = d.get("prompt") or ""
        if not ("yes" in low and "no" in low and is_treasure(prompt)):
            self._treasure_seen = None
            return False
        key = (prompt, tuple(low))
        if not self._treasure_seen or self._treasure_seen[0] != key:
            self._treasure_seen = (key, time.monotonic())
        waited = time.monotonic() - self._treasure_seen[1]

        p = d.get("player") or {}
        poison, fresh = p.get("poison"), p.get("stats_fresh")
        self._show_labels(labels)
        self._reset_cells()
        if not fresh and d.get("statsync_enabled") and waited < TREASURE_WAIT_S:
            self.hint.configure(text="Treasure: checking for poison\u2026", fg=INK)
            self.sub.configure(text="reading your stats after that fight")
            return True
        if poison is None:
            want, why = "yes", "poison unknown, no sign of it"
        elif poison > 0:
            want, why = "no", f"poisoned ({poison:g})"
        else:
            want, why = "yes", "not poisoned" + ("" if fresh else " (last check)")
        num = low.index(want) + 1
        col = GREEN if self._flash else GREEN_HI
        self._light(num, col, GREEN_HI)
        self._press_key(num, ("treasure", key, want))
        self.hint.configure(text=f"Treasure \u2192 {want.title()} ({num})",
                            fg=GREEN_HI if want == "yes" else AMBER)
        self.sub.configure(text=why)
        return True

    def _rest_step(self, d):
        """Out of combat on the main menu with energy below max: Rest until
        full. Stops when resting stops raising energy (poisoned, etc.).
        Returns True when it handled the panel."""
        p = d.get("player") or {}
        e, mx = p.get("energy"), p.get("max_energy")
        labels = [str(b).strip().lower() for b in (d.get("buttons") or [])]
        on_main_menu = "rest" in labels and ("move to" in labels or "hunt" in labels)
        if not e or not mx or e >= mx:
            self._rest_last_e, self._rest_stalls, self._rest_stopped = None, 0, None
            return False
        if not on_main_menu:
            return False
        poison = p.get("poison") or 0
        num = labels.index("rest") + 1
        self._show_labels(d.get("buttons") or [])
        self._reset_cells()

        if self._rest_stopped is not None:
            # Stay stopped while energy is at or below the best resting
            # reached (holding, wobbling or draining from poison). Resume only
            # if it goes higher than that (cured/healed); a new fight also
            # clears the stop (see _update).
            if e <= self._rest_stopped:
                why = (f"poisoned ({poison:g})" if poison > 0
                       else "resting isn't raising it (poisoned?)")
                self.hint.configure(text=f"Energy {e:g}/{mx:g}: stopped resting",
                                    fg=AMBER)
                self.sub.configure(text=f"{why}; best was {self._rest_stopped:g}, "
                                        "resumes if energy goes above it")
                return True
            self._rest_stopped = None           # energy went up: try again
            self._rest_last_e, self._rest_stalls = None, 0

        col = GREEN if self._flash else GREEN_HI
        self._light(num, col, GREEN_HI)
        if self._press_key(num, ("rest", e), repeat_s=REST_REPEAT_S):
            # Progress = a NEW HIGH. Poison can make energy wobble (up one
            # rest, down the next); small rises that never beat the best so
            # far don't count, so it can't rest forever.
            if self._rest_last_e is not None and e <= self._rest_last_e:
                self._rest_stalls += 1
            else:
                self._rest_stalls = 0
            self._rest_last_e = e if self._rest_last_e is None else max(self._rest_last_e, e)
            if self._rest_stalls >= (1 if poison > 0 else REST_STALLS):
                self._rest_stopped = self._rest_last_e
        self.hint.configure(text=f"Rest \u2192 button {num}", fg=GREEN_HI)
        self.sub.configure(text=f"energy {e:g}/{mx:g}"
                                + (f" \u00b7 poisoned {poison:g}" if poison > 0 else ""))
        return True

    def _update(self):
        d = self.data
        f = d.get("fight")

        # HIGHEST PRIORITY: if the game is showing a "More" prompt, nothing else
        # works until you clear it. Button 1 == "More". Tell the player to press
        # it, overriding any stale combat recommendation.
        if self._more_prompt_visible():
            self._layout(False)
            self._reset_cells()
            col = GREEN if self._flash else GREEN_HI
            self._light(1, col, GREEN_HI)
            self._press_key(1, ("more", tuple(self._button_texts())), repeat_same=True)
            self.hint.configure(text="Press 1 — More", fg=GREEN_HI)
            self.sub.configure(text="clear the text to continue")
            return

        # Treasure / gold: Yes unless poisoned, then No
        if self._treasure_step(d):
            return

        # Character creation: "Do you wish to keep these stats?"
        roll = d.get("roll")
        btns = self._button_texts()
        if roll and "reroll" in btns and "keep" in btns:
            self._roll_screen(roll, d.get("buttons") or [])
            return

        if not f or not f.get("name") or not f.get("moves"):
            if self._rest_step(d):
                return
            self._layout(False)
            self._reset_cells()
            self.hint.configure(text="No monster", fg=DIM)
            self.sub.configure(text="")
            return

        # in a fight: whatever stopped resting before no longer applies
        self._rest_last_e, self._rest_stalls, self._rest_stopped = None, 0, None

        mv = self._safe_move(f, d.get("player") or {})
        forced = self._forced_evade(f.get("name"))
        if forced:
            ev = next((m for m in f["moves"] if m.get("move") == "evade"), {})
            mv = {"move": "evade", "arg": None, "win": ev.get("win", 0),
                  "flee": ev.get("flee", 0), "forced": forced}
        move = mv["move"]
        info = MOVE_TO_BUTTON.get(move)

        self._reset_cells()

        # Only press fight keys while the game is actually showing the combat
        # menu (or the spell submenu). Fight advice can outlive the fight; on
        # a treasure question button 7 is "No", not "Evade".
        labels = self._button_texts()
        if labels and not ({"melee", "skirmish", "luckout", "evade"} & set(labels)
                           or self._spell_menu_visible()):
            self._layout(False)
            self.hint.configure(text="Waiting for the combat menu", fg=DIM)
            self.sub.configure(text="(not pressing fight keys on this screen)")
            return

        if not info:
            self._layout(False)
            self.hint.configure(text=f"{move}?", fg=AMBER)
            self.sub.configure(text="(unmapped move)")
            return

        label, num, sub = info

        # spell moves: two-step. When the game is still on the combat menu,
        # flash Spell (4). Once the spell submenu is actually visible, switch
        # the grid and flash the submenu key instead. This keeps the highlighted
        # key matched to the button the player can press right now, including
        # after clearing an intervening More prompt.
        if sub is not None:
            spell_mode = self._spell_menu_visible()
            sub_label, sub_num = sub
            current_num = sub_num if spell_mode and sub_num is not None else 4
            self._layout(spell_mode)
            col = GREEN if self._flash else GREEN_HI
            self._light(current_num, col, GREEN_HI)
            press_sig = (
                "fight", f.get("name"), self._round(f), move, mv.get("arg"),
                spell_mode, current_num,
            )
            pressed_now = self._press_key(current_num, press_sig)

            if move == "bolt" and mv.get("arg"):
                amt = int(mv["arg"])
                self.hint.configure(
                    text=f"Press {current_num} — BOLT ({amt} mana)",
                    fg=GREEN_HI)
                self.sub.configure(
                    text=f"① Spell (4)   ② Magic Bolt (2)   "
                         f"③ type {amt}")
                bolt_type_sig = (
                    f.get("name"), mv.get("arg"), tuple(self._button_texts())
                )
                if (
                    spell_mode
                    and current_num == 2
                    and pressed_now
                    and self._last_bolt_type_sig != bolt_type_sig
                ):
                    self._last_bolt_type_sig = bolt_type_sig
                    self._type_text_then_enter(amt)
            else:
                self.hint.configure(text=f"Press {current_num} — {sub_label}",
                                    fg=GREEN_HI)
                s = f"① Spell (4)   ② {sub_label}"
                if sub_num:
                    s += f" ({sub_num})"
                self.sub.configure(text=s)
        else:
            self._layout(False)
            col = GREEN if self._flash else GREEN_HI
            self._light(num, col, GREEN_HI)
            self._press_key(
                num, ("fight", f.get("name"), self._round(f), move, mv.get("arg"),
                      False, num)
            )
            title = self.cells[num]["base"]
            self.hint.configure(text=f"{title}  →  button {num}", fg=GREEN_HI)
            odds = f"{mv['win']*100:.0f}% win"
            if mv.get("flee", 0) > 0:
                odds = f"{mv['flee']*100:.0f}% escape"
            if mv.get("forced"):
                odds = f"always evade \"{mv['forced']}\" \u00b7 " + odds
            elif mv.get("safety"):
                odds = f"safety: {mv['safety']} \u00b7 " + odds
            self.sub.configure(text=odds)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ButtonOverlay().run()
