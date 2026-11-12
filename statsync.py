#!/usr/bin/env python3
"""
Keep Info -> Stats data (brains, magic level, poison, sin, age...) current
without the player opening it.

The game client asks the server for a player's stat sheet with one line:
"5" NUL "<name>" NUL (what clicking a name in the player list sends; client
messages are NUL-separated fields). The server replies
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

import re
import threading
import time

REFRESH_S = 60.0          # idle refresh interval
QUIET_S = 1.5             # don't inject within this long of the player's input
REPLY_TIMEOUT_S = 5.0     # stop waiting for a reply after this
MAX_UNANSWERED = 3        # this many unanswered requests: stop for the session


TREASURE_WORDS = ("treasure", "gold", "gem", "pick it up", "pick them up",
                  "inspect")


def is_treasure(prompt):
    p = (prompt or "").lower()
    return any(w in p for w in TREASURE_WORDS)


class ReplyFilter:
    """Server -> client filter that drops the stat-sheet reply we asked for.

    It NEVER holds data back: every chunk is forwarded at once. A reply is
    only removed when the whole sheet is inside the chunk being forwarded:
    a "35" line, a line count, "<our name> the ...", then that many lines.
    Anything else -- including a sheet split across chunks -- passes through
    untouched (worst case the stat window shows; the game can't stall).

    (An earlier version held back "35" lines while deciding. A speed of 35
    produced such lines, and the game froze waiting for the rest.)"""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = 0          # replies we asked for and haven't seen
        self.deadline = 0.0
        self.name = b""
        self.dropped = 0

    def expect(self, name):
        with self.lock:
            self.pending += 1
            self.name = name.encode("utf-8", "replace")
            self.deadline = time.time() + REPLY_TIMEOUT_S

    def filter(self, data):
        with self.lock:
            if not self.pending:
                return data
            if time.time() > self.deadline:
                self.pending = 0
                return data
            m = re.search(rb"(?:^|\n)(35\r?\n(\d+)\r?\n)" + re.escape(self.name)
                          + rb" the ", data)
            if not m:
                return data
            start = m.start(1)
            pos = m.end(1)                 # start of the first counted line
            for _ in range(int(m.group(2))):
                nl = data.find(b"\n", pos)
                if nl < 0:
                    return data            # sheet continues in a later chunk
                pos = nl + 1
            self.pending -= 1
            self.dropped += 1
            return data[:start] + data[pos:]


class StatsSync:
    def __init__(self, tracker, server_sock, send_lock, reply_filter,
                 state, state_lock, enabled=True, on_sent=None):
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
        self.on_sent = on_sent    # e.g. write the request into the recording
        self.unanswered = 0       # requests in a row with no reply

    def note_client(self, data):
        """Called with every client -> server chunk (bytes).

        Learns how the client ends a message (newline, CRLF or a NUL byte)
        so our request looks the same, notes whether the client is mid-
        message, and counts only real input (clicks, answers, name clicks:
        2+ characters) as the player being busy -- one-character messages
        are keep-alives the client sends on its own."""
        for end in (b"\r\n", b"\x00", b"\n"):
            if data.endswith(end):
                self.eol = end
                break
        self.partial = not data.endswith((b"\n", b"\x00"))
        for part in data.replace(b"\x00", b"\n").split(b"\n"):
            if len(part.strip()) >= 2:
                self.last_client = time.time()
                break

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
        with self.filter.lock:              # a reply that never came: stop waiting
            if self.filter.pending and now > self.filter.deadline:
                self.filter.pending = 0
        if not (self.enabled and name) or self.filter.pending or self.partial:
            return
        if now - self.last_client < QUIET_S:
            return
        if not (self.t.stats_due or now - self.last_sync >= REFRESH_S):
            return
        if not self._on_main_menu():
            return
        # Same format the client uses when you click a name in the player
        # list (seen in a recording): type "5", NUL, the name, NUL.
        if self.sent and self.filter.dropped < self.sent:
            self.unanswered = self.sent - self.filter.dropped
            if self.unanswered >= MAX_UNANSWERED:
                self.enabled = False       # not working on this server: stop
                with self.state_lock:
                    st = dict(self.state.get("statsync") or {})
                    st["disabled"] = f"{self.unanswered} requests got no reply"
                    self.state["statsync"] = st
                return
        req = b"5\x00" + name.encode("utf-8", "replace") + b"\x00"
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
        if self.on_sent:
            try:
                self.on_sent(req)
            except Exception:
                pass
        with self.state_lock:
            self.state["statsync"] = {"sent": self.sent,
                                      "replies": self.filter.dropped,
                                      "eol": self.eol.decode("latin-1"),
                                      "last": time.strftime("%H:%M:%S")}

    def run(self):
        while self.alive:
            try:
                self.tick()
                if self.sent:
                    with self.state_lock:
                        st = self.state.get("statsync") or {}
                        st["replies"] = self.filter.dropped
                        self.state["statsync"] = st
            except Exception:
                pass
            time.sleep(0.5)

