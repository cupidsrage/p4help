#!/usr/bin/env python3
"""
Keep Info -> Stats data (brains, magic level, poison, sin, age...) current
without the player opening it.

The game client asks the server for a player's stat sheet with one line:
"5<name>" (what clicking a name in the player list sends). The server replies
with a PLAYER_INFO packet: "35", a line count, then "<name> the <class>",
"Location: ...", ..., "Brains: 84", "Magic Level: 3", "Poison: 0.0000", ...

StatsSync sends that request for the player's own name now and then, and
ReplyFilter keeps the reply from reaching the game window (so no popup),
while the proxy's Tracker still reads it.

Only while: connected, name known, on the main menu (not fighting, not
rolling a character), and the player hasn't sent anything for a moment.
When: at login, after each fight, after a level change, and every
REFRESH_S while idle.
"""

import threading
import time

REFRESH_S = 60.0          # idle refresh interval
QUIET_S = 1.5             # don't inject within this long of the player's input
REPLY_TIMEOUT_S = 5.0     # stop waiting for a reply after this


TREASURE_WORDS = ("treasure", "gold", "gem", "pick it up", "pick them up",
                  "inspect")


def is_treasure(prompt):
    p = (prompt or "").lower()
    return any(w in p for w in TREASURE_WORDS)


class ReplyFilter:
    """Server -> client byte filter that drops the stat-sheet replies we
    asked for. Everything else passes through unchanged.

    A reply is only dropped when it looks exactly like one: a "35" line,
    then a number, then a line starting with "<our name> the ". Anything
    else is forwarded as-is."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = 0          # replies we asked for and haven't seen
        self.deadline = 0.0
        self.name = b""
        self.buf = b""            # partial line (only while filtering)
        self.hold = []            # lines held while deciding
        self.skip = 0             # lines of a matched reply still to drop
        self.dropped = 0

    def expect(self, name):
        with self.lock:
            self.pending += 1
            self.name = name.encode("utf-8", "replace")
            self.deadline = time.time() + REPLY_TIMEOUT_S

    def filter(self, data):
        with self.lock:
            if (not self.pending and not self.hold and not self.skip
                    and not self.buf):
                return data                       # fast path: not filtering
            if self.pending and time.time() > self.deadline:
                self.pending = 0                  # gave up waiting
            data = self.buf + data
            lines = data.split(b"\n")
            self.buf = lines.pop()                # last piece: maybe partial
            out = []
            for ln in lines:
                self._line(ln, out)
            res = b"".join(x + b"\n" for x in out)
            if not self.pending and not self.hold and not self.skip and self.buf:
                res += self.buf                   # done filtering: flush partial
                self.buf = b""
            return res

    def _line(self, ln, out):
        bare = ln.rstrip(b"\r").strip()
        if self.skip:
            self.skip -= 1
            return
        if self.hold:
            self.hold.append(ln)
            if len(self.hold) == 2:
                if not bare.isdigit():
                    out.extend(self.hold)
                    self.hold = []
                return
            # third line: is it our sheet?
            count = int(self.hold[1].rstrip(b"\r").strip())
            if bare.startswith(self.name + b" the ") or bare == self.name:
                self.skip = max(0, count - 1)
                self.pending = max(0, self.pending - 1)
                self.dropped += 1
            else:
                out.extend(self.hold)
            self.hold = []
            return
        if self.pending and bare == b"35":
            self.hold = [ln]
            return
        out.append(ln)


class StatsSync:
    def __init__(self, tracker, server_sock, send_lock, reply_filter,
                 state, state_lock, enabled=True):
        self.t = tracker
        self.sock = server_sock
        self.send_lock = send_lock
        self.filter = reply_filter
        self.state = state
        self.state_lock = state_lock
        self.enabled = enabled
        self.last_client = time.time()
        self.partial = False      # client is mid-line: don't inject now
        self.eol = b"\n"
        self.last_sync = 0.0
        self.alive = True
        self.sent = 0

    def note_client(self, data):
        """Called with every client -> server chunk (bytes)."""
        self.last_client = time.time()
        self.partial = not data.endswith(b"\n")
        if data.endswith(b"\r\n"):
            self.eol = b"\r\n"

    def _on_main_menu(self):
        with self.state_lock:
            labels = [str(b).strip().lower() for b in (self.state.get("buttons") or [])]
            busy = self.state.get("fight") or self.state.get("roll")
            prompt = (self.state.get("prompt") or "").lower()
        main_menu = "rest" in labels and ("move to" in labels or "hunt" in labels)
        # a treasure/gold question after a fight: auto-play needs to know if
        # that fight poisoned us before answering it
        treasure = "yes" in labels and "no" in labels and is_treasure(prompt)
        return not busy and (main_menu or treasure)

    def tick(self):
        now = time.time()
        name = (self.t.name or "").strip()
        if not (self.enabled and name) or self.filter.pending or self.partial:
            return
        if now - self.last_client < QUIET_S:
            return
        if not (self.t.stats_due or now - self.last_sync >= REFRESH_S):
            return
        if not self._on_main_menu():
            return
        req = b"5" + name.encode("utf-8", "replace") + self.eol
        self.filter.expect(name)
        try:
            with self.send_lock:
                self.sock.sendall(req)
        except OSError:
            self.alive = False
            return
        self.t.stats_due = False
        self.last_sync = now
        self.sent += 1

    def run(self):
        while self.alive:
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(0.5)

