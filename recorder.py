#!/usr/bin/env python3
"""
Game recorder: copies what's on screen and what you do about it.

The proxy (app.py) already sees every byte between the game and the server.
The recorder taps both directions and writes, for every input you send:

    what the game was showing   recent text lines, the prompt, the 8 buttons
    your character              energy, mana, strength, brains, level, ...
    the fight                   monster, size, and what the ADVISOR would press
    what you did                the raw input the client sent, plus a guess at
                                which button that was
    whether auto-play did it    so human moves and bot moves can be told apart

That's a (situation -> action) pair. Play normally with recording on and you
build a dataset of how the game is actually played. analyze.py turns it into
a summary and a policy table that a smarter auto-play can learn from.

Files (in ./recordings, one set per game connection):
    session-YYYYmmdd-HHMMSS.jsonl   one JSON object per event (the dataset)
    session-YYYYmmdd-HHMMSS.txt     readable transcript of both directions
    session-YYYYmmdd-HHMMSS/        screenshots, if turned on (launcher only)

Anything you type after a password prompt is replaced with "***".

Recordings contain your account and character names. Keep them local.
"""

import json
import os
import re
import threading
import time

SCREEN_LINES = 40       # how much recent text to keep as "the screen"
CONTEXT_LINES = 12      # how much of it goes into each event

# packet headers that matter for "what's on screen" (see app.py)
CLEAR, WRITE_LINE = 10, 11
BUTTONS, FULL_BUTTONS = 20, 21
PLAYER_INFO = 35


class ScreenModel:
    """Rebuilds what the game window is showing from the server stream."""

    def __init__(self, linecount):
        self.linecount = linecount
        self.lines = []          # recent text, oldest first
        self.buttons = []
        self.prompt = ""         # the question the current buttons answer
        self.since_buttons = []  # text since the last button packet
        self.pending = None
        self.awaiting_count = False
        self.loose = False       # inside an unknown packet: keep its text

    def feed(self, text):
        for line in text.split("\n"):
            self._line(line.rstrip("\r"))

    def _add_text(self, txt, loose=False):
        txt = txt.rstrip()
        if not txt.strip():
            return
        self.lines.append(txt)
        del self.lines[:-SCREEN_LINES]
        self.since_buttons.append(txt.strip())
        if loose and txt.endswith("?"):
            # name / password / amount questions come without buttons
            self.prompt = txt.strip()

    def _line(self, line):
        if self.awaiting_count:
            self.awaiting_count = False
            try:
                self.pending = (PLAYER_INFO, [], int(line.strip()))
            except ValueError:
                pass
            return
        if self.pending is not None:
            hdr, buf, need = self.pending
            buf.append(line)
            if len(buf) >= need:
                self.pending = None
                self._packet(hdr, buf)
            return

        m = re.match(r"^\s*(\d+)\s*$", line)
        if not m:
            # text outside a known packet: prompts for name/password etc.
            if self.loose:
                self._add_text(line, loose=True)
            return
        hdr = int(m.group(1))
        self.loose = False
        if hdr == PLAYER_INFO:
            self.awaiting_count = True
            return
        if hdr == CLEAR:
            self.lines.append("----")
            del self.lines[:-SCREEN_LINES]
            return
        need = self.linecount.get(hdr, 0)
        if need:
            self.pending = (hdr, [], need)
        else:
            self.loose = True

    def _packet(self, hdr, lines):
        if hdr in (BUTTONS, FULL_BUTTONS):
            self.buttons = [ln.strip() for ln in lines]
            # the question these buttons answer: last "?" line since the
            # previous set of buttons (none = plain menu, e.g. combat/More)
            qs = [t for t in self.since_buttons if t.endswith("?")]
            self.prompt = qs[-1] if qs else ""
            self.since_buttons = []
        elif hdr == WRITE_LINE:
            self._add_text(lines[0] if lines else "")
        elif hdr == PLAYER_INFO:
            for ln in lines:
                self._add_text(ln)

    def snapshot(self):
        return {"text": self.lines[-CONTEXT_LINES:], "prompt": self.prompt,
                "buttons": list(self.buttons)}


def guess_button(raw_lines, buttons):
    """Which button an input was. Client lines are a type digit plus data;
    a button click is "1" + its 0-based position ("12" = 3rd button), as
    seen in real recordings. Returns {"num": 1-8, "label"} or None."""
    for ln in reversed(raw_lines):
        m = re.match(r"^\s*1([0-7])\s*$", ln)
        if m:
            i = int(m.group(1))
            if i < len(buttons):
                return {"num": i + 1, "label": buttons[i] or "(blank)"}
    return None


class Recorder:
    def __init__(self, folder, linecount, state_fn=None):
        """state_fn() returns the proxy's live STATE (player, fight, ...)."""
        self.folder = folder
        self.linecount = linecount
        self.state_fn = state_fn
        self.enabled = True
        self.autoplay = False          # set by the launcher
        self.screenshot_fn = None      # set by the launcher: path -> bool
        self.lock = threading.Lock()
        self.session = None
        self.events = 0

    # ---- connection lifecycle (called from app.handle) ----
    def open(self):
        with self.lock:
            self._close()
            if not self.enabled:
                return
            os.makedirs(self.folder, exist_ok=True)
            stamp = time.strftime("session-%Y%m%d-%H%M%S")
            base = os.path.join(self.folder, stamp)
            self.session = {
                "base": base,
                "jsonl": open(base + ".jsonl", "a", encoding="utf-8"),
                "txt": open(base + ".txt", "a", encoding="utf-8"),
                "screen": ScreenModel(self.linecount),
                "t0": time.time(),
                "n": 0,
            }
            self._event("connect", {})

    def close(self):
        with self.lock:
            self._close()

    def _close(self):
        s = self.session
        if not s:
            return
        try:
            self._event("disconnect", {})
            s["jsonl"].close()
            s["txt"].close()
        except (OSError, ValueError):
            pass
        self.session = None

    # ---- taps (called from app.pump threads) ----
    def server(self, text):
        with self.lock:
            s = self.session
            if not s:
                return
            s["screen"].feed(text)
            self._raw("<", text)

    def client(self, text):
        with self.lock:
            s = self.session
            if not s:
                return
            screen = s["screen"].snapshot()
            # client messages may end in a newline or a NUL byte
            lines = [ln.rstrip("\r") for ln in text.replace("\x00", "\n").split("\n")
                     if ln.strip()]
            if "password" in (screen["prompt"] or "").lower():
                lines = ["***" for _ in lines]
                self._raw(">", "***\n")
            else:
                self._raw(">", text)
            if not lines:
                return
            self._event("input", {
                "screen": screen,
                "raw": lines,
                "button": guess_button(lines, screen["buttons"]),
                "by": "autoplay" if self.autoplay else "human",
            }, shot=True)

    # ---- writers (lock held) ----
    def _raw(self, arrow, text):
        s = self.session
        try:
            t = time.time() - s["t0"]
            for ln in text.rstrip("\n").split("\n"):
                s["txt"].write(f"{t:9.2f} {arrow} {ln.rstrip(chr(13))}\n")
            s["txt"].flush()
        except (OSError, ValueError):
            pass

    def _event(self, kind, data, shot=False):
        s = self.session
        ev = {"t": round(time.time(), 3), "kind": kind, **data}
        if self.state_fn:
            try:
                st = self.state_fn()
                p = st.get("player") or {}
                f = st.get("fight") or None
                ev["player"] = p
                if f:
                    moves = f.get("moves") or []
                    ev["fight"] = {
                        "name": f.get("name"), "size": f.get("size"),
                        "state": f.get("state"),
                        "advisor": moves[0] if moves else None,
                        "moves": moves[:5],
                    }
                ev["location"] = st.get("location") or p.get("location")
            except Exception:
                pass
        if shot and self.screenshot_fn:
            s["n"] += 1
            folder = s["base"]
            path = os.path.join(folder, f"{s['n']:05d}.png")
            try:
                os.makedirs(folder, exist_ok=True)
                if self.screenshot_fn(path):
                    ev["screenshot"] = os.path.relpath(path, self.folder)
            except Exception:
                pass
        try:
            s["jsonl"].write(json.dumps(ev, default=str) + "\n")
            s["jsonl"].flush()
            self.events += 1
        except (OSError, ValueError):
            pass
