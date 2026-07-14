#!/usr/bin/env python3
"""
Phantasia Advisor — standalone overlay.

A borderless, always-on-top window that floats over the game. Reads live state
from the proxy (app.py) at 127.0.0.1:8420. The Phantasia client is never touched.

  python overlay.py

Uses tkinter, which ships with Python. No Node, no npm, no Electron, no install.

Controls:
  drag the title bar   move it
  right-click          menu (opacity, pin, close)
  F8                   hide / show
  Esc                  close
"""

import tkinter as tk
from tkinter import font as tkfont
import json
import threading
import queue
import urllib.request
import urllib.error

POLL_URL = "http://127.0.0.1:8420/state"
POLL_MS = 500      # network poll
PAINT_MS = 120     # ui refresh

BG      = "#16130e"
PANEL   = "#1e1a12"
LINE    = "#4a3f2a"
INK     = "#e8dcc0"
DIM     = "#9a8d72"
GOLD    = "#d4b03a"
GREEN   = "#6bb050"
AMBER   = "#e0982e"
RED     = "#cc5038"
BLUE    = "#5a94c0"

NAMES = {
    "melee": "Melee", "skirmish": "Skirmish", "luckout": "Luckout",
    "evade": "Evade / Flee", "bolt": "Magic Bolt", "aon": "All or Nothing",
    "might": "Increase Might", "paralyze": "Paralyze", "nick": "Nick",
    "ring": "Use Ring",
}


class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Phantasia Advisor")
        self.root.overrideredirect(True)          # borderless
        self.root.attributes("-topmost", True)    # always on top
        self.root.attributes("-alpha", 0.94)
        self.root.configure(bg=BG)
        self.root.geometry("340x600+40+40")

        self.f_tiny = tkfont.Font(family="Segoe UI", size=7)
        self.f_sm   = tkfont.Font(family="Segoe UI", size=8)
        self.f_md   = tkfont.Font(family="Segoe UI", size=9)
        self.f_lg   = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        self.f_nm   = tkfont.Font(family="Segoe UI", size=11, weight="bold")

        self._build()
        self._bind()

        self.data = {}
        self.q = queue.Queue()
        self._sig = None
        self.alive = True
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._render()

    # ---------------- chrome ----------------
    def _build(self):
        outer = tk.Frame(self.root, bg=LINE, bd=0)
        outer.pack(fill="both", expand=True)
        shell = tk.Frame(outer, bg=BG)
        shell.pack(fill="both", expand=True, padx=1, pady=1)

        # title bar (draggable)
        self.bar = tk.Frame(shell, bg="#0f0d09", height=24)
        self.bar.pack(fill="x")
        self.bar.pack_propagate(False)
        tk.Label(self.bar, text="ADVISOR", bg="#0f0d09", fg=GOLD,
                 font=self.f_tiny).pack(side="left", padx=8)
        self.dot = tk.Label(self.bar, text="\u25cf", bg="#0f0d09", fg=RED,
                            font=self.f_sm)
        self.dot.pack(side="right", padx=8)
        self.hint = tk.Label(self.bar, text="F8 hide", bg="#0f0d09", fg=DIM,
                             font=self.f_tiny)
        self.hint.pack(side="right")

        # scrollable body
        self.body = tk.Frame(shell, bg=BG)
        self.body.pack(fill="both", expand=True, padx=9, pady=8)

    def _bind(self):
        for w in (self.bar,) + tuple(self.bar.winfo_children()):
            w.bind("<Button-1>", self._grab)
            w.bind("<B1-Motion>", self._drag)
        self.root.bind("<Escape>", lambda e: self._quit())
        self.root.bind("<F8>", lambda e: self._toggle())
        self.root.bind("<Button-3>", self._menu)
        self._hidden = False

    def _grab(self, e):
        self._ox, self._oy = e.x_root, e.y_root
        self._wx = self.root.winfo_x()
        self._wy = self.root.winfo_y()

    def _drag(self, e):
        dx = e.x_root - self._ox
        dy = e.y_root - self._oy
        self.root.geometry(f"+{self._wx + dx}+{self._wy + dy}")

    def _toggle(self):
        if self._hidden:
            self.root.deiconify()
        else:
            self.root.withdraw()
        self._hidden = not self._hidden

    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=INK,
                    activebackground=LINE, activeforeground=INK, bd=0)
        for pct in (1.0, 0.9, 0.75, 0.6):
            m.add_command(label=f"Opacity {int(pct*100)}%",
                          command=lambda p=pct: self.root.attributes("-alpha", p))
        m.add_separator()
        m.add_command(label="Close", command=self._quit)
        m.tk_popup(e.x_root, e.y_root)

    def _quit(self):
        self.alive = False
        self.root.destroy()

    # ---------------- data ----------------
    def _poll_loop(self):
        ev = threading.Event()
        while self.alive:
            try:
                with urllib.request.urlopen(POLL_URL, timeout=2) as r:
                    self.q.put(json.loads(r.read()))
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                self.q.put(None)
            ev.wait(POLL_MS / 1000)

    # ---------------- render ----------------
    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _row(self, parent, text, fg=INK, font=None, pady=1, anchor="w"):
        lb = tk.Label(parent, text=text, bg=parent["bg"], fg=fg,
                      font=font or self.f_sm, anchor=anchor,
                      justify="left", wraplength=300)
        lb.pack(fill="x", pady=pady)
        return lb

    def _signature(self, d):
        """What the panel LOOKS like. If this is unchanged, don't rebuild."""
        f = d.get("fight") or {}
        st = f.get("state") or {}
        q = d.get("quest") or d.get("quest_offer") or {}
        p = d.get("player") or {}
        moves = f.get("moves") or []
        return (
            bool(d.get("connected")),
            f.get("name"), f.get("size"),
            round(st.get("m_energy", -1)), st.get("rounds"),
            st.get("luckout_spent"), st.get("aon_spent"),
            round(st.get("strength_spell", 0)), st.get("paralyzed"),
            tuple((m["move"], round(m["win"], 3)) for m in moves[:5]),
            f.get("quest_target"),
            q.get("text"), q.get("done"), q.get("need"), d.get("rerolls"),
            round(p.get("energy", 0)), round(p.get("mana", 0)),
            p.get("brains"), p.get("magiclvl"),
        )

    def _render(self):
        if not self.alive:
            return

        # drain the queue to the newest payload (never block the UI thread)
        got = False
        while True:
            try:
                self.data = self.q.get_nowait() or {}
                got = True
            except queue.Empty:
                break

        d = self.data or {}
        self.dot.configure(fg=GREEN if d.get("connected") else RED)

        # ONLY rebuild when something visible actually changed. Rebuilding every
        # cycle destroys/recreates every widget, which is what caused the flicker
        # and the sluggishness.
        sig = self._signature(d)
        if sig == getattr(self, "_sig", None):
            self.root.after(PAINT_MS, self._render)
            return
        self._sig = sig

        self._clear()
        f = d.get("fight")
        p = d.get("player") or {}
        self._quest(d)

        if not f or not f.get("name"):
            box = tk.Frame(self.body, bg=BG)
            box.pack(expand=True)
            self._row(box, "\u2694", fg=LINE,
                      font=tkfont.Font(family="Segoe UI", size=26), anchor="center")
            self._row(box, "No monster.", fg=DIM, anchor="center")
            if not p.get("brains") or not p.get("magiclvl"):
                self._row(box, "Open Info \u2192 Stats\nso brains load.",
                          fg=AMBER, font=self.f_tiny, anchor="center")
            self._meter(p)
            self.root.after(PAINT_MS, self._render)
            return

        st = f.get("state") or {}
        hp = st.get("m_energy", f.get("energy", 0))
        mx = f.get("energy") or 1

        # --- monster header ---
        hdr = tk.Frame(self.body, bg=BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f["name"], bg=BG, fg=INK, font=self.f_nm,
                 anchor="w").pack(side="left")
        rd = f" \u00b7 rd {st['rounds']}" if st.get("rounds") else ""
        tk.Label(hdr, text=f"sz {f['size']}{rd}", bg=BG, fg=DIM,
                 font=self.f_tiny).pack(side="right")

        # hp bar
        cv = tk.Canvas(self.body, height=4, bg="#0c0a07", highlightthickness=0)
        cv.pack(fill="x", pady=(3, 7))
        self.root.update_idletasks()
        w = max(1, cv.winfo_width())
        cv.create_rectangle(0, 0, w * max(0, hp) / mx, 4, fill=RED, width=0)

        # --- THE RECOMMENDATION ---
        moves = f.get("moves") or []
        if moves:
            mv = moves[0]
            win = mv["win"]
            col = (RED if mv["move"] == "evade"
                   else GREEN if win >= .85
                   else AMBER if win >= .5 else RED)
            card = tk.Frame(self.body, bg=PANEL, highlightthickness=0)
            card.pack(fill="x", pady=(0, 7))
            tk.Frame(card, bg=col, width=3).pack(side="left", fill="y")
            inner = tk.Frame(card, bg=PANEL)
            inner.pack(side="left", fill="both", expand=True, padx=8, pady=7)

            title = NAMES.get(mv["move"], mv["move"])
            if mv["move"] == "bolt" and mv.get("arg"):
                title = f"Bolt \u2014 {int(mv['arg'])} mana"

            tk.Label(inner, text="DO THIS", bg=PANEL, fg=DIM,
                     font=self.f_tiny, anchor="w").pack(fill="x")
            tk.Label(inner, text=title, bg=PANEL, fg=INK,
                     font=self.f_lg, anchor="w").pack(fill="x")
            if f.get("why"):
                tk.Label(inner, text=f["why"], bg=PANEL, fg=DIM,
                         font=self.f_tiny, anchor="w", justify="left",
                         wraplength=280).pack(fill="x", pady=(3, 0))

            line = f"{win*100:.0f}% win"
            if mv.get("flee", 0) > 0:
                line += f"  \u00b7  {mv['flee']*100:.0f}% escape"
            line += f"  \u00b7  ~{mv['energy_lost']:.0f} en"
            tk.Label(inner, text=line, bg=PANEL, fg=col,
                     font=self.f_sm, anchor="w").pack(fill="x", pady=(4, 0))

        # --- state chips ---
        chips = []
        if st.get("luckout_spent"):
            chips.append(("luckout spent", RED))
        elif f.get("luckout", -1) >= 0:
            chips.append((f"luckout {f['luckout']*100:.0f}%", GREEN))
        if st.get("aon_spent"):
            chips.append(("AoN spent", RED))
        if st.get("strength_spell", 0) > 0:
            chips.append((f"+{st['strength_spell']:.0f} might", GREEN))
        if st.get("paralyzed"):
            chips.append(("PARALYZED", GREEN))
        if chips:
            cf = tk.Frame(self.body, bg=BG)
            cf.pack(fill="x", pady=(0, 5))
            for i, (txt, c) in enumerate(chips):
                tk.Label(cf, text=("  \u00b7  " if i else "") + txt, bg=BG,
                         fg=c, font=self.f_tiny).pack(side="left")

        # --- quest target flag ---
        if f.get("quest_target"):
            self._row(self.body, "\u2605 " + f["quest_target"],
                      fg=GOLD, font=self.f_sm)

        # --- warnings ---
        for wmsg in (f.get("warnings") or [])[:2]:
            self._row(self.body, "! " + wmsg, fg=AMBER, font=self.f_tiny)

        # --- all moves ---
        if len(moves) > 1:
            tbl = tk.Frame(self.body, bg=BG)
            tbl.pack(fill="x", pady=(6, 0))
            for i, a in enumerate(moves[:5]):
                r = tk.Frame(tbl, bg=BG)
                r.pack(fill="x")
                lbl = NAMES.get(a["move"], a["move"])
                if a["move"] == "bolt" and a.get("arg"):
                    lbl += f" {int(a['arg'])}m"
                c = (GREEN if a["win"] >= .85
                     else AMBER if a["win"] >= .5 else RED)
                fg = DIM if i == 0 else INK
                tk.Label(r, text=lbl, bg=BG, fg=fg, font=self.f_tiny,
                         anchor="w", width=15).pack(side="left")
                tk.Label(r, text=f"{a['win']*100:.0f}%", bg=BG, fg=c,
                         font=self.f_tiny, width=5, anchor="e").pack(side="left")
                tk.Label(r, text=f"{a['energy_lost']:.0f} en", bg=BG, fg=DIM,
                         font=self.f_tiny, width=7, anchor="e").pack(side="left")

        self._meter(p)
        self.root.after(PAINT_MS, self._render)

    def _quest(self, d):
        q = d.get("quest")
        offer = d.get("quest_offer")

        # a pending offer at the post
        if offer and not q:
            box = tk.Frame(self.body, bg=PANEL)
            box.pack(fill="x", pady=(0, 7))
            tk.Frame(box, bg=BLUE, width=3).pack(side="left", fill="y")
            inn = tk.Frame(box, bg=PANEL)
            inn.pack(side="left", fill="both", expand=True, padx=7, pady=6)
            tk.Label(inn, text="QUEST OFFERED", bg=PANEL, fg=DIM,
                     font=self.f_tiny, anchor="w").pack(fill="x")
            tk.Label(inn, text=offer["text"], bg=PANEL, fg=INK,
                     font=self.f_md, anchor="w", wraplength=270,
                     justify="left").pack(fill="x")
            rr = d.get("rerolls")
            sub = f"{offer['reward_gold']}g"
            if rr is not None:
                sub += f"  \u00b7  {rr} rerolls left"
            tk.Label(inn, text=sub, bg=PANEL, fg=DIM,
                     font=self.f_tiny, anchor="w").pack(fill="x")
            cands = offer.get("candidates") or []
            if cands:
                best = cands[0]
                lo = best.get("luckout", -1)
                t = f"easiest: {best['name']}"
                if lo >= 0:
                    t += f" ({lo*100:.0f}% luckout)"
                tk.Label(inn, text=t, bg=PANEL, fg=GREEN,
                         font=self.f_tiny, anchor="w").pack(fill="x", pady=(3, 0))
            return

        if not q:
            return

        box = tk.Frame(self.body, bg=PANEL)
        box.pack(fill="x", pady=(0, 7))
        col = GREEN if q["complete"] else GOLD
        tk.Frame(box, bg=col, width=3).pack(side="left", fill="y")
        inn = tk.Frame(box, bg=PANEL)
        inn.pack(side="left", fill="both", expand=True, padx=7, pady=6)

        head = tk.Frame(inn, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text="QUEST", bg=PANEL, fg=DIM,
                 font=self.f_tiny).pack(side="left")
        tk.Label(head, text=f"{q['done']}/{q['need']}", bg=PANEL, fg=col,
                 font=self.f_sm).pack(side="right")

        tk.Label(inn, text=q["text"], bg=PANEL, fg=INK, font=self.f_sm,
                 anchor="w", wraplength=270, justify="left").pack(fill="x")

        # progress bar
        cv = tk.Canvas(inn, height=3, bg="#0c0a07", highlightthickness=0)
        cv.pack(fill="x", pady=(4, 3))
        self.root.update_idletasks()
        w = max(1, cv.winfo_width())
        frac = q["done"] / max(1, q["need"])
        cv.create_rectangle(0, 0, w * min(1, frac), 3, fill=col, width=0)

        if q["complete"]:
            tk.Label(inn, text=f"DONE \u2014 collect {q['reward_gold']}g at a post",
                     bg=PANEL, fg=GREEN, font=self.f_tiny,
                     anchor="w").pack(fill="x")
        else:
            cands = q.get("candidates") or []
            if cands:
                names = []
                for c in cands[:3]:
                    lo = c.get("luckout", -1)
                    names.append(f"{c['name'].replace('A ','').replace('An ','')}"
                                 + (f" {lo*100:.0f}%" if lo >= 0 else ""))
                tk.Label(inn, text="hunt: " + ", ".join(names), bg=PANEL,
                         fg=GREEN, font=self.f_tiny, anchor="w",
                         wraplength=270, justify="left").pack(fill="x")

    def _meter(self, p):
        if not p.get("name"):
            return
        sep = tk.Frame(self.body, bg=LINE, height=1)
        sep.pack(fill="x", pady=(8, 5))
        m = tk.Frame(self.body, bg=BG)
        m.pack(fill="x")
        cells = [
            ("en", int(p.get("energy", 0))),
            ("mana", int(p.get("mana", 0))),
            ("br", int(p["brains"]) if p.get("brains") else "?"),
            ("ML", int(p["magiclvl"]) if p.get("magiclvl") else "?"),
        ]
        for k, v in cells:
            c = tk.Frame(m, bg=BG)
            c.pack(side="left", expand=True)
            tk.Label(c, text=str(v), bg=BG, fg=INK,
                     font=self.f_md).pack()
            tk.Label(c, text=k, bg=BG, fg=DIM, font=self.f_tiny).pack()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Overlay().run()
