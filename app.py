#!/usr/bin/env python3
"""
Phantasia Advisor — desktop app.

Runs two things at once:
  1. a transparent TCP proxy between the game client and the server
  2. a local web UI at http://127.0.0.1:8420

Start it, open the browser tab, and leave it on a second monitor. It updates
live as you play — character sheet, combat advice, monster tables, what-if calc.

    python app.py -h 178.63.136.86

Setup (once):
  hosts file:  127.0.0.1  phantasia5.com
  then launch the game normally.

In-game: open Info -> Stats once. Brains and Magic Level are NOT sent as
packets — they only appear on that screen — and luckout runs on brains.

Local only. Per the Phantasia calc policy, building this is fine;
distributing or advertising it is not.
"""

import itertools
import math
import socket
import time
import threading
import select
import re
import sys
import os
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from combat import (Player, Monster, advise, aon_chance, luckout_chance,
                    bolt_plan, ML_BOLT)
import monsters as MDB
import quests as Q
import trove as TR
import rules as RU
import zones as ZN
import reroll as RR
import statsync as SS
import calibrate as CAL_MOD
from planner import plan, FightState, score_moves, explain, might_of

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 43302
SERVER_HOST = "phantasia5.com"
SERVER_PORT = 43302
WEB_PORT = 8420
RAW_LOG = None

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- packet headers ----
PLAYER_INFO = 35
CLEAR, WRITE_LINE = 10, 11
BUTTONS, FULL_BUTTONS = 20, 21
NAME, LOCATION, ENERGY, STRENGTH, SPEED = 40, 41, 42, 43, 44
SHIELD, SWORD, QUICKSILVER, MANA, LEVEL = 45, 46, 47, 48, 49
GOLD, GEMS = 50, 51
CLOAK, BLESSING, CROWN, PALANTIR, RING, VIRGIN = 52, 53, 54, 55, 56, 57
AMULETS, CHARMS, TOKENS, STAFF, EXP = 59, 60, 61, 62, 63

LINECOUNT = {
    CLEAR: 0, NAME: 1, LOCATION: 3, ENERGY: 3, STRENGTH: 2, SPEED: 2,
    SHIELD: 1, SWORD: 1, QUICKSILVER: 1, MANA: 2, LEVEL: 1, GOLD: 1, GEMS: 1,
    CLOAK: 1, BLESSING: 1, CROWN: 1, PALANTIR: 1, RING: 1, VIRGIN: 1,
    AMULETS: 1, CHARMS: 1, TOKENS: 1, STAFF: 1, EXP: 1, WRITE_LINE: 1,

    # BUTTON PACKETS CARRY 8 LABEL LINES. These were missing, so the parser
    # consumed 0 lines and then tried to read 'Yes' / 'No' / 'Melee' etc. as
    # packet HEADERS -- desyncing the whole stream every time a menu appeared.
    # Since menus appear constantly (combat, yes/no, treasure prompts), the
    # parser was losing sync all the time. This is why the trove scroll --
    # which arrives right after a button block -- was never being read.
    BUTTONS: 8,
    FULL_BUTTONS: 8,
}

# ---- shared live state (read by the web server) ----
STATE = {
    "connected": False,
    "player": {},
    "fight": None,
    "session": {"fights": 0, "exp": 0, "enlost": 0, "lk": 0, "lkwin": 0,
                "quests": 0, "quest_gold": 0},
    "quest": None,
    "quest_offer": None,
    "rerolls": None,
    "roll": None,       # character creation: the stats roll on screen (see reroll.py)
    "prompt": "",       # the question the current buttons answer
    "statsync": None,   # quiet stat-sheet fetches: sent / replies / last
    "statsync_enabled": False,
    "trove": None,
    "zone": None,
    "log": [],
    "buttons": [],
}
LOCK = threading.Lock()
# Character-creation ids must stay unique across reconnects: each connection
# gets a fresh Tracker, but the overlay's RollJudge lives on and caches by id.
_CHAR_IDS = itertools.count(1)
RECORDER = None   # recorder.Recorder, set by enable_recording()
ZONE_MIN_INTERVAL_S = 300   # zone advice recalc (~5 s CPU) at most this often
# Monster numbers learned from the player's own fights (calibrate.py)
CAL_PATH = os.path.join(HERE, "calibration.json")
CAL = None
# Quietly fetch the Info -> Stats sheet (statsync.py). The first version sent
# a malformed request (no NUL after the "5") and its reply filter could hold
# data back; both fixed using a real recording of the client's own request.
AUTO_STATS = True


def snapshot_state():
    with LOCK:
        return json.loads(json.dumps({k: STATE.get(k) for k in
                                      ("player", "fight", "buttons", "quest",
                                       "location")}, default=str))


def enable_recording(folder=None):
    """Turn on the game recorder (see recorder.py). Returns it."""
    global RECORDER
    import recorder
    if RECORDER is None:
        RECORDER = recorder.Recorder(folder or os.path.join(HERE, "recordings"),
                                     LINECOUNT, snapshot_state)
    return RECORDER


def click_index(text):
    """0-based button position of a client click, or None. Client messages
    are fields separated by NUL (or newline): a click is "1" then the
    position, either as separate fields ("1", "2") or run together ("12")."""
    toks = [t.strip() for t in re.split(r"[\x00\n]", text) if t.strip()]
    for k, tok in enumerate(toks):
        m = re.fullmatch(r"1([0-7])", tok)
        if m:
            return int(m.group(1))
        if tok == "1" and k + 1 < len(toks) and re.fullmatch(r"[0-7]", toks[k + 1]):
            return int(toks[k + 1])
    return None


def num(s):
    try:
        return float(re.sub(r"[^0-9.\-]", "", s) or 0)
    except ValueError:
        return 0.0


class Tracker:
    def __init__(self):
        self.p = Player()
        self.name = ""
        self.gold = 0
        self.gems = 0.0
        self.amulets = None        # unknown until the server says
        self.charms = None
        self.cloaked = False
        self.cls = ""              # class, from "Name the Fighter"
        self.age = None            # stats sheet: Age / Degenerated
        self.degenerated = None
        self.next_level_at = None
        self._notes_key = None
        self._notes = []
        self._shop_key = None
        self._shop = None
        self.quicksilver = 0.0
        self.location = ""
        self.pending = None
        self.awaiting_count = False
        self.have_magic = False
        self.max_size_seen = 0
        self.quest = None
        self.offer = None
        self.qbuf = []
        self.trove = None
        self.xy = None
        self.solver = TR.TroveSolver()
        self.search = None      # TroveSearch, once we have an estimate
        self.cur = None            # current monster dict
        self.cur_mon = None        # Monster object
        self.fs = None             # live FightState
        self.await_luck = False    # a luckout is in flight
        self.prev_energy = None
        # character creation
        self.class_menu = None     # button labels while "which type?" is up
        self.class_pick = None     # classes the player's click could mean
        self.roll_char = 0         # bumps for every new character rolled
        self.roll_n = 0            # rolls for the current character
        self.experience = None
        self.poison = None          # from Info -> Stats ("Poison: 0.0000")
        self.stats_due = True       # statsync: refresh the stat sheet soon
        self.stats_at = 0.0         # when a stat sheet (Poison: line) was last read
        self.fight_end_at = 0.0     # when the last fight ended
        self.since_buttons = []     # text since the last button packet

    # ---------- packet framing ----------
    def feed(self, text):
        # Network chunks can end anywhere, including mid-line or right after
        # a packet header. Only complete lines are parsed; the unfinished
        # tail waits for the next chunk. (Before, the empty piece after a
        # chunk's final newline was parsed as a line too, and could be
        # taken as the text of a header that ended the chunk -- losing e.g.
        # "You are attacked by ...".)
        data = getattr(self, "_partial", "") + text
        lines = data.split("\n")
        self._partial = lines.pop()
        for line in lines:
            self._line(line)
        # Recompute fight advice once per batch of server data, not once per
        # damage line: a round's "You hit..." and "...hit you" usually arrive
        # together, and each recompute is a few thousand simulated fights.
        if getattr(self, "_advice_dirty", False):
            self._advice_dirty = False
            self.refresh_advice()

    def _line(self, line):
        line = line.rstrip("\r")

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

        if not line.strip():
            return
        m = re.match(r"^\s*(\d+)\s*$", line)
        if not m:
            return
        hdr = int(m.group(1))

        if hdr == PLAYER_INFO:      # variable length: next line is the count
            self.awaiting_count = True
            return

        need = LINECOUNT.get(hdr, 0)
        if need == 0:
            self._packet(hdr, [])
        else:
            self.pending = (hdr, [], need)

    def _packet(self, hdr, lines):
        p = self.p
        v = num(lines[0]) if lines else 0

        if hdr == ENERGY:
            if self.prev_energy is not None and v < self.prev_energy:
                with LOCK:
                    STATE["session"]["enlost"] += (self.prev_energy - v)
            self.prev_energy = v
            p.energy = v
            if len(lines) > 1:
                p.max_energy = num(lines[1])
        elif hdr == STRENGTH:
            p.strength = v
        elif hdr == SPEED:
            p.quickness = v
        elif hdr == MANA:
            p.mana = v
        elif hdr == SHIELD:
            p.shield = v
        elif hdr == SWORD:
            p.sword = v
        elif hdr == LEVEL:
            if p.level and v != p.level:
                self.stats_due = True      # level-up: brains/ML changed
            p.level = v
        elif hdr == GOLD:
            self.gold = v
        elif hdr == GEMS:
            self.gems = v
        elif hdr == AMULETS:
            self.amulets = v
        elif hdr == CHARMS:
            self.charms = v
        elif hdr == QUICKSILVER:
            self.quicksilver = v
        elif hdr == BLESSING:
            p.blessing = (lines[0].strip().lower() == "yes") if lines else False
        elif hdr == EXP:
            self.experience = v
        elif hdr == CLOAK:
            self.cloaked = (lines[0].strip().lower() == "yes") if lines else False
        elif hdr == NAME:
            name = lines[0].strip() if lines else ""
            if name and name != self.name:
                self.stats_due = True      # (new) character: fetch its sheet
            self.name = name
        elif hdr == LOCATION:
            if len(lines) >= 3:
                # Field order: line0 = x, line1 = y, line2 = place name.
                # (phantcli names these y,x but that's misleading. Verified from
                #  a real capture: (180,187) -> (179,188) is a single NW step,
                #  i.e. first value fell = west = x, second rose = north = y.
                #  It also matches the Stats screen's "Location: (170, 177)".)
                try:
                    x = int(float(lines[0].strip()))
                    y = int(float(lines[1].strip()))
                    self.xy = (x, y)
                except ValueError:
                    pass
                self.location = f"{lines[2].strip()} ({lines[0].strip()},{lines[1].strip()})"
                # tick off this square in the trove sweep
                if self.xy:
                    self.solver.visit(*self.xy)
                    if self.search:
                        self.search.visit(*self.xy)
                self.push_trove()
                self.push_zone()
        elif hdr == RING:
            p.ring = (lines[0].strip().lower() == "yes")
        elif hdr in (BUTTONS, FULL_BUTTONS):
            labels = [line.strip() for line in lines]
            # the question these buttons answer: last "?" line since the
            # previous set of buttons (none = a plain menu)
            qs = [t for t in self.since_buttons if t.endswith("?")]
            self.since_buttons = []
            with LOCK:
                STATE["buttons"] = labels
                STATE["prompt"] = qs[-1] if qs else ""
            self._creation_buttons(labels)
            # Back on the main menu = any fight is over, however it ended.
            # Some endings print nothing we recognise (a Kobold stealing gold
            # and running off), which left a stale fight that froze auto-play.
            low = {b.lower() for b in labels}
            if self.cur is not None:
                # when your fight prompt appeared: a timeout gives the
                # monster a 25% free hit and doubles timeout fatigue
                self.cur["prompt_at"] = time.time() if "melee" in low else None
            if self.fs is not None and "melee" in low and self._wake():
                self._advice_dirty = True   # paralysis wore off a little
            if (self.cur is not None and "melee" not in low and "rest" in low
                    and ("move to" in low or "hunt" in low)):
                self.end_fight()
        elif hdr == WRITE_LINE:
            self._text(lines[0] if lines else "")
        elif hdr == PLAYER_INFO:
            for ln in lines:
                self._text(ln)

        self.push()

    # ---------- character creation ----------
    def _creation_buttons(self, labels):
        names = [RR.class_name(b) for b in labels]
        if sum(1 for n in names if n) >= 4:
            # "Which type of character do you want?": a new character
            self.class_menu = names
            self.class_pick = None
            self.roll_char = next(_CHAR_IDS)
            self.roll_n = 0
            with LOCK:
                STATE["roll"] = None
            return
        self.class_menu = None
        low = [b.lower() for b in labels]
        if not ("reroll" in low and "keep" in low):
            with LOCK:
                STATE["roll"] = None       # left the roll screen

    def client_input(self, text):
        """What the game client sent (client -> server). Lines are a type
        digit plus data; a button click is "1" + its 0-based position, e.g.
        "12" = third button (seen in recordings). Used to learn which class
        was clicked on the class menu."""
        i = click_index(text)
        with LOCK:
            labels = list(STATE.get("buttons") or [])
        clicked = labels[i] if i is not None and i < len(labels) else None
        if clicked:
            self.last_click = clicked
        cf = getattr(self, "cal_fight", None)
        if cf is not None and clicked:
            cf["clicked"].append(clicked)
        # All or Nothing cast: it can only be tried once per fight, and the
        # server shows a failure as "You hit X for 0 damage!" (no "grows
        # stronger" message), so the tracker must remember the cast itself.
        if clicked and clicked.strip().lower() == "all or nothing" and self.fs is not None:
            self.fs.aon_spent = True
            self.aon_pending = True
        menu = self.class_menu
        if not menu:
            return
        i = click_index(text)
        if i is not None and i < len(menu) and menu[i]:
            self.class_pick = {menu[i]}

    def _new_roll(self):
        p = self.p
        self.roll_n += 1
        with LOCK:
            STATE["roll"] = {
                "char": self.roll_char, "n": self.roll_n,
                "class_options": sorted(self.class_pick) if self.class_pick else None,
                "stats": {"energy": p.energy, "strength": p.strength,
                          "speed": p.quickness, "mana": p.mana,
                          "brains": p.brains, "magiclvl": p.magiclvl,
                          "experience": self.experience, "gold": self.gold},
            }

    # ---------- server text ----------
    def _text(self, txt):
        low = txt.lower()
        if txt.strip():
            self.since_buttons = (self.since_buttons + [txt.strip()])[-30:]

        # ---- Info->Stats screen: the ONLY source of brains / magic level ----
        m = re.search(r"magic\s*level\s*:\s*([\d.]+)", low)
        if m:
            self.p.magiclvl = float(m.group(1))
            self.have_magic = True
            self.push_zone()
        m = re.search(r"brains\s*:\s*([\d.]+)", low)
        if m:
            self.p.brains = float(m.group(1))
            self.push_zone()
        m = re.search(r"quickness\s*:\s*([\d.]+)", low)
        if m:
            self.p.quickness = float(m.group(1))
        m = re.search(r"strength\s*:\s*([\d.]+)\s*\(\s*[\d.]+\s*\+\s*([\d.]+)\s*sword", low)
        if m:
            self.p.strength = float(m.group(1))
            self.p.sword = float(m.group(2))
        m = re.search(r"energy\s*:\s*([\d.]+)\s*\(\s*([\d.]+)\s*\+\s*([\d.]+)\s*shield", low)
        if m:
            self.p.energy = float(m.group(1))
            # "Energy: 1088 (477 + 836 shield)": the energy bar's max is
            # 477 + 836, so keep max_energy as the total like packet 42 does
            self.p.shield = float(m.group(3))
            self.p.max_energy = float(m.group(2)) + self.p.shield
        m = re.search(r"^\s*level\s*:\s*([\d.]+)", low)
        if m:
            self.p.level = float(m.group(1))
        m = re.search(r"^\s*mana\s*:\s*([\d.]+)", low)
        if m:
            self.p.mana = float(m.group(1))
        # game text saying we got poisoned (the Stats sheet gives the
        # exact value when opened; this just flags it in the meantime)
        if "poisoned" in low and not re.search(r"^\s*poison\s*:", low):
            if not self.poison:
                self.poison = 1.0
        m = re.search(r"^\s*poison\s*:\s*([\d.]+)", low)
        if m:
            self.poison = float(m.group(1))
            self.stats_at = time.time()
        m = re.search(r"^\s*experience\s*:\s*([\d.]+)", low)
        if m:
            self.experience = float(m.group(1))
        if "keep these stats" in low:
            self._new_roll()
        m = re.search(r"^\s*gold\s*:\s*([\d.]+)", low)
        if m:
            self.gold = float(m.group(1))
        m = re.search(r"^\s*(gems|amulets|charms)\s*:\s*([\d.]+)", low)
        if m:
            setattr(self, m.group(1), float(m.group(2)))
        m = re.search(r"quickness\s*:\s*[\d.]+\s*\(\s*[\d.]+\s*\+\s*([\d.]+)\s*quicksilver", low)
        if m:
            self.quicksilver = float(m.group(1))
        m = re.search(r"^\s*(age|degenerated)\s*:\s*([\d.]+)", low)
        if m:
            setattr(self, m.group(1), float(m.group(2)))
        m = re.search(r"^\s*next level at\s*:\s*([\d.]+)", low)
        if m:
            self.next_level_at = float(m.group(1))
        m = re.search(r"^\s*sin\s*:\s*([\d.]+)", low)
        if m:
            self.p.sin = float(m.group(1))
        m = re.search(r"^\s*blessing\s*:\s*(yes|no)", low)
        if m:
            self.p.blessing = m.group(1) == "yes"
        if "smurf berries" in low:
            self.note("Smurf berries: " + ("poison eased" if "better" in low or "cured" in low
                                           else "combat fatigue halved and some energy back"))
        m = re.search(r"^\s*cloaked\s*:\s*(yes|no)", low)
        if m:
            self.cloaked = m.group(1) == "yes"
        m = re.match(r"^\s*\S.* the (magic-user|fighter|elf|dwarf|halfling|experimento)\s*$",
                     low)
        if m:
            self.cls = m.group(1)

        # ---- quests (trading post) ----
        r = Q.parse_rerolls(txt)
        if r is not None:
            with LOCK:
                STATE["rerolls"] = r

        # Buffer the offer block. The merchant prints the greeting ONLY on the
        # first visit -- a REROLL just reprints the objective. So we start a
        # fresh buffer on the greeting OR on any new "Kill N ..." line, and we
        # clear the previous offer so a reroll can't resolve to the old quest.
        if "what i've got for you" in low or "what ive got for you" in low:
            self.qbuf = [txt]
            self.offer = None
            with LOCK:
                STATE["quest_offer"] = None
        elif Q.is_quest_line(txt):
            # a new objective (first offer or a reroll) -- reset and restart
            self.qbuf = [txt]
            self.offer = None
            with LOCK:
                STATE["quest_offer"] = None
        elif self.qbuf:
            self.qbuf.append(txt)
            if len(self.qbuf) > 8:
                self.qbuf = self.qbuf[-8:]

        if self.qbuf:
            q = Q.parse_offer(self.qbuf)
            if q:
                self.offer = q
                with LOCK:
                    STATE["quest_offer"] = q.to_dict(self.p.brains)

        # accepted
        if any(k in low for k in Q.ACCEPT_MARKERS) and self.offer:
            self.quest = self.offer
            self.quest.active = True
            self.offer = None
            self.qbuf = []
            with LOCK:
                STATE["quest"] = self.quest.to_dict(self.p.brains)
                STATE["quest_offer"] = None
            self.note(f"QUEST: {self.quest.describe()} ({self.quest.reward_gold}g)")

        # Turn-in at the post: "Well, just as promised, here is your reward..."
        if any(k in low for k in Q.COMPLETE_MARKERS) and self.quest:
            gold = self.quest.reward_gold
            was = f"{self.quest.done}/{self.quest.need}"
            self.note(f"QUEST TURNED IN ({was}) — +{gold} gold")
            self.quest = None
            self.offer = None
            self.qbuf = []
            with LOCK:
                STATE["quest"] = None
                STATE["quest_offer"] = None
                STATE["session"]["quests"] += 1
                STATE["session"]["quest_gold"] += gold

        # ---- treasure trove maps ----
        # Every map points at the same single trove from where it's read
        # (treasure.c), so each one narrows down the same spot.
        if any(k in low for k in TR.SCROLL_MARKERS):
            pos = self.xy
            one = TR.ONE_SQUARE_RE.search(txt)
            t = TR.parse_scroll(txt, origin=pos)
            if one and pos:
                t = TR.Trove(1, one.group(1).lower(), pos, txt.strip())
            if t:
                if self.trove:
                    t.leg = self.trove.leg + 1
                self.trove = t
            if not pos:
                self.note("TROVE map read but position unknown - move one "
                          "square so the game reports your location.")
            elif one:
                self.solver.add_exact(pos[0], pos[1], one.group(1).lower())
            elif t:
                dup = any(abs(rx - pos[0]) < 1 and abs(ry - pos[1]) < 1
                          for rx, ry, *_ in self.solver.readings)
                if dup:
                    self.note("TROVE map read at the SAME spot as the last one - "
                              "it adds nothing. Read the next one somewhere else.")
                else:
                    self.solver.add(pos[0], pos[1], t.distance, t.direction)
            if pos and t:
                est = self.solver.estimate()
                if est:
                    self.note(f"TROVE map {est['readings']}: {t.distance} sq "
                              f"{t.direction} -> {est['squares']:,} squares left, "
                              f"middle ({est['x']}, {est['y']})")
                    if est["squares"] <= TR.SWEEP_MAX:
                        if self.search is None:
                            self.search = TR.TroveSearch(self.solver.cands)
                            self.note(f"SWEEP: {est['squares']:,} squares to walk")
                        else:
                            self.search.update(self.solver.cands)
                elif self.solver.readings:
                    self.note("TROVE maps disagree (was the trove dug up and "
                              "re-hidden?) - starting over from this map.")
                    self.solver = TR.TroveSolver()
                    self.search = None
                    if not one:
                        self.solver.add(pos[0], pos[1], t.distance, t.direction)
            self.push_trove()

        if any(k in low for k in TR.FOUND_MARKERS):
            if self.trove:
                self.note(f"TROVE FOUND (leg {self.trove.leg})! "
                          f"Hunt begins anew - look for the next scroll.")
            self.trove = None
            self.solver = TR.TroveSolver()
            self.search = None
            with LOCK:
                STATE["trove"] = None

        # ---- shrieker summon ----
        # fight.c: "Shrieeeek!!  You scared it, and it called one of its friends."
        # It then calls Do_cancel_monster() and files a NEW monster event with
        # arg3 = ROLL(70,30), i.e. monster #70-99 -- the endgame bracket.
        # The Shrieker VANISHES; you're now facing whatever it called.
        if "shrieeeek" in low or "called one of its friends" in low:
            self.note("SHRIEKER SUMMONED SOMETHING (#70-99). Old monster is GONE.")
            # drop the stale Shrieker; the server will announce the new monster
            self.cur = None
            self.cur_mon = None
            self.fs = None
            with LOCK:
                STATE["fight"] = {
                    "name": "??? (Shrieker summon)",
                    "size": 0, "flock": False,
                    "energy": 0, "strength": 0, "brains": 0,
                    "base_brains": 0, "experience": 0, "speed": 0,
                    "luckout": -1, "aon": 0, "bolt": None,
                    "warnings": [
                        "SHRIEKER SUMMON INCOMING - it calls monster #70-99 "
                        "(Thaumaturgist / Balrog / Titan / Dragon range).",
                        "At your level this will very likely KILL YOU. "
                        "You can always flee a Shrieker fight - RUN.",
                    ],
                    "moves": [], "why": "Waiting for the server to name it...",
                    "state": {},
                }
            return

        # ---- encounters ----
        #   "You find and attack A Bogle. -  (Size: 2)"
        #   "You are attacked by A Kobold. -  (Size: 2)"
        #   "A Crebain's friend appears and attacks. -  (Size: 2)"
        # ANY line carrying a "(Size: N)" tag is an encounter, whatever the
        # wording. This is structural rather than phrase-matching, so it also
        # catches Shrieker summons, Jabberwock substitutions, Mimic reveals,
        # and any other mid-fight monster swap we haven't seen yet.
        has_size = re.search(r"\(size:\s*(\d+)\)", low)
        if (has_size or "you find and attack" in low
                or "you are attacked by" in low
                or "friend appears and attacks" in low):
            size = int(has_size.group(1)) if has_size else 1
            flock = "friend appears" in low
            name = None
            if flock:
                mf = re.match(r"\s*(.+?)'s friend appears", txt, re.I)
                if mf:
                    name = MDB.lookup(mf.group(1))
            if not name:
                name = MDB.lookup(txt)
            if name:
                self.encounter(name, size, flock)
            return

        # ---- watch the fight unfold and update live state ----
        if self.fs is not None and self.cur_mon is not None:
            changed = False

            # "You hit A Bogle for 41 damage!"
            m = re.search(r"you hit .*? for ([\d.]+) damage", low)
            if m:
                dmg = float(m.group(1))
                fs = self.fs
                fs.m_shield -= dmg
                if fs.m_shield < 0:
                    fs.m_energy += fs.m_shield
                    fs.m_shield = 0.0
                # what the hit does besides damage (fight.c Do_playerhits)
                how = (getattr(self, "last_click", "") or "").strip().lower()
                max_e = max(1.0, fs.m_max_energy)
                if how == "melee":
                    fs.melee_dmg += dmg
                    fs.m_strength = (fs.m_max_strength - (fs.melee_dmg / max_e)
                                     * (fs.m_max_strength / 3.0))
                elif how == "skirmish":
                    fs.skirm_dmg += dmg
                    fs.m_speed = (fs.m_max_speed - (fs.skirm_dmg / max_e)
                                  * (fs.m_max_speed / 3.0))
                elif how == "nick":
                    fs.m_max_speed += 2.0
                    fs.m_speed = 0.0 if fs.m_speed < 0 else fs.m_speed + 2.0
                    fs.m_experience -= fs.m_experience // 10
                changed = True

            # "A Kobold hit you for 10 damage!"
            if m and getattr(self, "cal_fight", None):
                self.cal_fight["out"].append(dmg)
            if "hit you for" in low:
                changed = True
                self._wake()           # each pass of the server loop
                mh = re.search(r"hit you for ([\d.]+) damage", low)
                cf = getattr(self, "cal_fight", None)
                if mh and cf:
                    # with the damage you'd dealt by then (melee wears it down)
                    cf["in"].append((float(mh.group(1)), sum(cf["out"])))

            # luckout resolved
            if "you blew it" in low and getattr(self, "cal_fight", None):
                self.cal_fight["blew"] += 1
            if "you blew it" in low:
                self.fs.luckout_spent = True
                self.await_luck = False
                with LOCK:
                    STATE["session"]["lk"] += 1
                self.note("luckout FAILED — falling back")
                changed = True

            # AoN failed: it doubles strength and speed. On this server the
            # only sign is "You hit X for 0 damage!" right after the cast.
            aon_zero = (getattr(self, "aon_pending", False)
                        and re.search(r"you hit .*? for 0 damage", low))
            if aon_zero or ("you blew it" not in low and (
                    "grows stronger" in low or "gets stronger" in low)):
                self.aon_pending = False
                fs = self.fs
                fs.m_max_strength *= 2.0
                fs.m_strength = fs.m_max_strength
                fs.m_max_speed *= 2.0
                fs.m_speed = fs.m_speed * 2.0 if fs.m_speed * 2.0 > 1 else fs.m_speed + 1
                fs.aon_spent = True
                self.note("AoN FAILED — it doubled str + speed")
                changed = True

            # Paralyze worked ("A Troll is held."): its speed is set to -2
            # and climbs back by 1 every pass of the fight loop.
            if low.rstrip().endswith(" is held."):
                self.fs.m_paralyzed = True
                self.fs.m_speed = -2.0
                changed = True

            if changed:
                self._advice_dirty = True

        cf = getattr(self, "cal_fight", None)
        if cf and CAL and ("you made it" in low or "you killed it" in low):
            if CAL_MOD.Calibration.fight_counts(cf["clicked"], cf["blew"]):
                CAL.record(cf["size"], cf["e"], cf["s"], cf["out"], cf["in"])
            self.cal_fight = None
        elif cf and ("you got away" in low or "you have been killed" in low
                     or "wandered off" in low):
            self.cal_fight = None

        if "you made it" in low or "you killed it" in low:
            # quest credit: only on an actual kill, and only for the right monster
            if self.quest and self.cur and not self.quest.complete:
                if self.quest.credits(self.cur["name"]):
                    self.quest.done += 1
                    left = self.quest.need - self.quest.done
                    if self.quest.complete:
                        self.note(f"QUEST: {self.quest.done}/{self.quest.need} "
                                  f"— DONE, return to a post for {self.quest.reward_gold}g")
                    else:
                        self.note(f"QUEST: {self.quest.done}/{self.quest.need} "
                                  f"({left} to go)")
                    with LOCK:
                        STATE["quest"] = self.quest.to_dict(self.p.brains)

        if "you made it" in low:
            if self.await_luck:
                self.await_luck = False
                with LOCK:
                    STATE["session"]["lk"] += 1
                    STATE["session"]["lkwin"] += 1
                self.note("luckout succeeded")
            self.end_fight()

        m = re.search(r"earned\s+([\d,]+)\s+experience", low)
        if m:
            with LOCK:
                STATE["session"]["exp"] += int(m.group(1).replace(",", ""))

        if ("you killed it" in low or "you have been killed" in low
                or "it wandered off" in low or "you got away" in low):
            self.end_fight()

    def _wake(self):
        """Start of a pass of the server's fight loop: a paralyzed monster's
        speed climbs back by 1 (fight.c)."""
        fs = self.fs
        if fs is None:
            return False
        old = fs.m_speed
        fs.m_speed = min(fs.m_speed + 1.0, fs.m_max_speed)
        fs.m_paralyzed = fs.m_speed <= 0
        return fs.m_speed != old

    # ---------- fight lifecycle ----------
    def encounter(self, name, size, flock):
        d = MDB.scaled(name, size)
        if not d:
            return
        # Correct the 4.03-table numbers with what this player's own fights
        # showed for monsters of this size (calibrate.py).
        er, sr = CAL.ratios(size) if CAL else (1.0, 1.0)
        self.cal_fight = {"size": size, "e": d["energy"], "s": d["strength"],
                          "out": [], "in": [], "clicked": [], "blew": 0}
        d = dict(d, energy=d["energy"] * er, strength=d["strength"] * sr)
        m = Monster(name=d["name"], strength=d["strength"], brains=d["brains"],
                    speed=d["speed"], energy=d["energy"], shield=0.0,
                    experience=d["experience"], specials=tuple(d["specials"]))

        self.cur_mon = m
        self.fs = FightState.opening(self.p, m)
        self.aon_pending = False

        warn = list(MDB.notes_for(d))
        if (er, sr) != (1.0, 1.0):
            warn.append(f"Size {size} numbers from your own fights: energy "
                        f"x{er:.2f}, strength x{sr:.2f}.")
        if size > self.max_size_seen and self.max_size_seen > 0:
            warn.insert(0, f"Size {size} — bigger than anything you've fought this session.")
        self.max_size_seen = max(self.max_size_seen, size)
        if not self.have_magic:
            warn.insert(0, "Brains/Magic Level unknown — open Info → Stats.")
        tr_type = MDB.MONSTERS[name][5]
        if (self.trove is not None and tr_type == RU.MAP_TREASURE_TYPE
                and RU.map_eligible(self.p.level, self.gems)):
            pm = RU.map_chance_per_kill(tr_type, size)
            warn.insert(0, f"Drops trove maps: ~{pm*100:.0f}% chance of a map "
                           "if you kill it.")

        self.cur = {
            "name": d["name"], "size": size, "flock": flock,
            "energy": d["energy"], "strength": d["strength"],
            "brains": d["brains"], "base_brains": MDB.MONSTERS[name][1],
            "experience": d["experience"], "speed": d["speed"],
            "warnings": warn,
            "treasure": RU.treasure_drop_chance(
                tr_type, size, unicorn="U" in d["specials"]),
            "luckout": luckout_chance(self.p, m),
            "aon": aon_chance(self.p, m),
            "bolt": bolt_plan(self.p, m),
        }
        # does this monster count toward the active quest?
        if self.quest and not self.quest.complete and self.quest.credits(name):
            left = self.quest.need - self.quest.done
            self.cur["quest_target"] = (
                f"QUEST TARGET — {left} more needed "
                f"({self.quest.done}/{self.quest.need})")

        self.await_luck = True
        self.refresh_advice()

        lo = self.cur["luckout"]
        tag = ("FREE KILL" if lo >= .75 else "risky" if lo >= .60
               else "DON'T" if lo >= 0 else "?")
        self.note(f"{d['name']} sz{size} — luckout {lo*100:.0f}% ({tag})")

        with LOCK:
            STATE["session"]["fights"] += 1
        self.push()

    def refresh_advice(self):
        """Recompute advice from the CURRENT fight state, not the opening one."""
        if not self.cur or not self.cur_mon or not self.fs:
            return
        m, st = self.cur_mon, self.fs

        # keep live player numbers in the fight state
        st.p_energy = self.p.energy
        st.p_mana = self.p.mana

        moves = score_moves(self.p, m, st, trials=420)
        self.cur["moves"] = [{
            "move": x["move"], "arg": x["arg"], "win": x["win"],
            "flee": x["flee"], "death": x.get("death", 0.0),
            "energy_lost": x["energy_lost"],
        } for x in moves]
        self.cur["why"] = explain(self.p, m, st, moves)

        # opening game plans (only worth computing once, at the start)
        if "plans" not in self.cur:
            self.cur["plans"] = [{
                "policy": r["policy"], "win": r["win"], "flee": r["flee"],
                "energy_lost": r["energy_lost"], "mana_spent": r["mana_spent"],
                "rounds": r["rounds"],
            } for r in plan(self.p, m, trials=700)[:5]]

        self.cur["state"] = {
            "m_energy": max(0.0, st.m_energy),
            "m_max": m.energy,
            "m_strength": st.m_strength,
            "luckout_spent": st.luckout_spent,
            "aon_spent": st.aon_spent,
            "strength_spell": st.strength_spell,
            "paralyzed": st.m_paralyzed,
            "rounds": st.rounds,
            "might": might_of(self.p, st),
        }
        with LOCK:
            STATE["fight"] = self.cur

    def end_fight(self):
        if getattr(self, "_zone_after_fight", False):
            threading.Timer(0.5, self.push_zone).start()
        self.stats_due = True
        self.fight_end_at = time.time()
        self.cur = None
        self.cur_mon = None
        self.fs = None
        self.await_luck = False
        with LOCK:
            STATE["fight"] = None

    def _zone_key(self):
        # Only the character's lasting traits. Speed and strength wobble
        # after nearly every fight (fatigue, gold weight, poison); keying on
        # them reran the ~5 s recalc after most fights, which lagged
        # auto-play more and more over a long session.
        p = self.p
        return (p.level, p.brains, p.magiclvl, p.max_energy)

    def push_zone(self):
        """Keep the zone advice current.

        The recalc is SLOW (~5 s of CPU: it simulates whole hunting
        sessions) and depends only on the character's stats, not on where
        they stand. It used to rerun on every step while moving; that kept
        the CPU busy and delayed noticing a fight the game started mid-move.
        Now: moving only re-reads the cached rows; the recalc runs when the
        stats change, and never during a fight (it waits for the fight to
        end)."""
        if self.p.brains <= 0 or self.p.strength <= 0:
            return
        rows = getattr(self, "_zone_rows", None)
        if rows is not None and self.xy:
            cur = ZN.circle_of(*self.xy)
            with LOCK:
                z = STATE.get("zone") or {}
                z = dict(z, current=cur,
                         current_row=next((r for r in rows if r["circle"] == cur), None))
                STATE["zone"] = z
        if getattr(self, "_zone_done_key", None) == self._zone_key():
            return                      # stats unchanged: cached rows are right
        if self.cur is not None:
            self._zone_after_fight = True
            return                      # don't burn CPU mid-fight
        if getattr(self, "_zone_busy", False):
            return                      # one at a time
        last = getattr(self, "_zone_ran_at", 0.0)
        if rows is not None and time.time() - last < ZONE_MIN_INTERVAL_S:
            return                      # at most once every few minutes
        self._zone_busy = True
        self._zone_ran_at = time.time()
        self._zone_after_fight = False
        key = self._zone_key()
        snap = (self.p, self.xy, key)
        threading.Thread(target=self._zone_worker, args=snap, daemon=True).start()

    def _zone_worker(self, player, xy, key=None):
        try:
            best, rows = ZN.recommend(player, here=xy, measured=True)
            self._zone_rows = rows
            self._zone_done_key = key
            cur = ZN.circle_of(*xy) if xy else None
            curr = next((r for r in rows if r["circle"] == cur), None)
            with LOCK:
                STATE["zone"] = {
                    "current": cur, "current_row": curr,
                    "best": best, "rows": rows[:24],
                }
        except Exception as e:
            print(f"  ! zone calc failed: {e}", file=sys.stderr)
        finally:
            self._zone_busy = False

    def push_trove(self):
        with LOCK:
            if not self.trove:
                STATE["trove"] = None
                return
            d = self.trove.to_dict(self.xy)
            d["map_monsters"] = self.map_monsters()
            d["maps_ok"] = RU.map_eligible(self.p.level, self.gems)
            d["cloaked"] = self.cloaked
            if self.search:
                d["search"] = self.search.to_dict(self.xy)
            est = self.solver.estimate()
            if est:
                est = d["solved"] = dict(est)
                if self.xy:
                    dx = est["x"] - self.xy[0]
                    dy = est["y"] - self.xy[1]
                    d["solved"]["steps"] = int(max(abs(dx), abs(dy)))
                    d["solved"]["heading"] = TR.Trove.heading(dx, dy)
            STATE["trove"] = d

    def note(self, s):
        with LOCK:
            STATE["log"].append(s)
            STATE["log"][:] = STATE["log"][-40:]

    def push(self):
        p = self.p
        notes = self.notes()          # takes LOCK itself: before ours
        with LOCK:
            STATE["player"] = {
                "name": self.name, "energy": p.energy, "max_energy": p.max_energy,
                "mana": p.mana, "strength": p.strength, "quickness": p.quickness,
                "brains": p.brains, "magiclvl": p.magiclvl, "level": p.level,
                "sword": p.sword, "shield": p.shield, "ring": p.ring,
                "gold": self.gold, "location": self.location,
                "poison": self.poison,
                # stat sheet read since the last fight ended (poison is current)
                "stats_fresh": self.stats_at > 0 and self.stats_at >= self.fight_end_at,
                "gems": self.gems, "amulets": self.amulets, "charms": self.charms,
                "cloaked": self.cloaked, "cls": self.cls,
                "blessing": p.blessing, "sin": p.sin,
                "quicksilver": self.quicksilver,
                "age": self.age, "degenerated": self.degenerated,
                "circle": self.circle(),
                "notes": notes,
            }

    def circle(self):
        return ZN.circle_of(*self.xy) if self.xy else None

    def notes(self):
        """Short reminders from the game's own rules, shown on the panel.
        Cached: recomputed only when something they depend on changes."""
        p = self.p
        fight = self.cur is not None
        key = (round(self.gold), self.gems, p.level, self.cls, self.cloaked,
               self.circle(), self.trove is not None, self.search is not None,
               self.age, self.degenerated, self.next_level_at,
               self.experience, round(p.sin or 0, 3), p.blessing, fight,
               self.amulets)
        if key != self._notes_key:
            self._notes_key = key
            self._notes = self._make_notes()
        return self._notes

    def _make_notes(self):
        p, out = self.p, []
        circle = self.circle()
        # aging: permanent stat loss every 750 turns
        left = RU.turns_to_degeneration(self.age, self.degenerated)
        later = []                   # routine: shown after the urgent ones
        if left is not None:
            later.append(f"Next permanent stat loss in ~{left} turns (age "
                       f"{self.age:.0f}): every move, rest and fight counts.")
        # gold weight
        loss = RU.gold_speed_loss(self.gold, self.gems, p.level, self.cls)
        limit = RU.gold_limit(self.gems, p.level, self.cls)
        if not p.level:
            pass                     # level not known yet
        elif loss > 0:
            out.append(f"Gold is slowing you: -{loss:.1f} speed. Bank or spend "
                       f"down to {limit:,.0f} gold.")
        elif self.gold and self.gold > 0.85 * limit:
            out.append(f"Gold {self.gold:,.0f}: over {limit:,.0f} starts "
                       "slowing you down.")
        # what to spend it on
        buy = self.shop_advice()
        if buy:
            b = buy[0]
            out.append(f"Best buy: {b['item']} {b['to']:,} ({b['spend']:,} gp, "
                       f"{b['effect']}) - {b['where']}.")
        # level
        if self.next_level_at and self.experience:
            need = self.next_level_at - self.experience
            with LOCK:
                s = STATE["session"]
                per = s["exp"] / s["fights"] if s.get("fights") else 0
            if need > 0:
                t = f"Level {p.level + 1:.0f} in {need:,.0f} exp"
                if per > 0:
                    t += f" (~{math.ceil(need / per)} fights at this session's rate)"
                g = RU.level_gains(self.cls)
                if g:
                    t += (f"; each level: +{g['energy']:g} energy, +{g['strength']:g} "
                          f"strength, +{g['mana']:g} mana")
                later.append(t + ".")
        # blessing and sin
        if p.blessing:
            k = RU.blessing_rolls(p.sin)
            if k is None:
                out.append("Blessed and sinless: you always hit first.")
            elif k < 6:
                out.append(f"Sin {p.sin:.2f} is weakening your blessing's first "
                           f"hit (each Hunt adds {RU.HUNT_SIN}).")
        # cloak
        if self.cloaked and not self.cur:
            out.append(f"Cloaked: -{RU.cloak_drain(circle, p.level, self.cls)} "
                       "mana a turn, resting finds no mana, and you can't find "
                       "the trove.")
        # trove maps
        if self.trove is not None:
            if not RU.map_eligible(p.level, self.gems):
                out.append(f"No more trove maps: level {p.level:.0f} is over "
                           f"100 - gems ({100 - self.gems:.0f}).")
            else:
                names = self.map_monsters()
                if names:
                    out.append("Trove maps drop from: " + ", ".join(names[:4])
                               + (" ..." if len(names) > 4 else ""))
        return out + later

    def shop_advice(self):
        """Best buys for the gold you carry. Simulating them takes a second
        or two, so it runs on a background thread; this returns the latest
        answer and starts a new calculation when gold or stats change."""
        p = self.p
        circle = self.circle()
        if not circle or not p.max_energy or self.gold < 500:
            return None
        key = (round(self.gold, -2), p.level, round(p.quickness), round(p.shield),
               round(p.sword), round(p.strength), circle, self.quicksilver)
        if key != self._shop_key and not getattr(self, "_shop_busy", False):
            self._shop_key = key
            self._shop_busy = True
            full = Player(**{**vars(p), "energy": p.max_energy})
            args = (full, self.cls, self.gold, circle, self.quicksilver,
                    self.xy[0] if self.xy else None)
            threading.Thread(target=self._shop_worker, args=args, daemon=True).start()
        return self._shop

    def _shop_worker(self, *args):
        try:
            import shop as SH
            buys = SH.best_buys(*args)
            self._shop = [b for b in buys if b["risk_cut"] > 0.001] or None
        except Exception as e:
            print(f"  ! shop advice failed: {e}", file=sys.stderr)
        finally:
            self._shop_busy = False
            self._shop_ver = getattr(self, "_shop_ver", 0) + 1
            self._notes_key = None          # show the new advice
            self.push()

    def map_monsters(self, circle=None):
        """Monsters that can spawn here and drop scrolls (trove maps)."""
        c = circle or self.circle()
        if not c:
            return []
        seen = []
        for i in ZN.monster_pool(c):
            name = ZN.IDS[i]
            if MDB.MONSTERS[name][5] == RU.MAP_TREASURE_TYPE and name not in seen:
                seen.append(name)
        return seen


# ---------- web server ----------
class Web(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 5

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/state"):
            with LOCK:
                body = json.dumps({
                    **STATE,
                    "monsters": {
                        n: {"strength": v[0], "brains": v[1], "speed": v[2],
                            "energy": v[3], "experience": v[4],
                            "specials": [s for s in v[7].split(",") if s]}
                        for n, v in MDB.MONSTERS.items()
                    },
                    "notes": MDB.SPECIAL_NOTES,
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        fname = "overlay.html" if self.path.startswith("/overlay") else "ui.html"
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            path = os.path.join(HERE, "overlay", fname)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404, f"{fname} missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------- tcp proxy ----------
def pump(src, dst, tracker=None, tap=None, send_lock=None, out_filter=None,
         on_bytes=None):
    try:
        while True:
            data = src.recv(8192)
            if not data:
                break
            if on_bytes is not None:
                on_bytes(data)
            out = out_filter.filter(data) if out_filter is not None else data
            if out:
                if send_lock is not None:
                    with send_lock:
                        dst.sendall(out)
                else:
                    dst.sendall(out)
            if tap is not None:
                try:
                    tap(data.decode("utf-8", errors="replace"))
                except Exception as e:
                    print(f"[record] {e}", file=sys.stderr)
            if tracker is not None:
                if RAW_LOG:
                    try:
                        with open(RAW_LOG, "a", encoding="utf-8") as f:
                            f.write(data.decode("utf-8", errors="replace"))
                    except OSError:
                        pass
                try:
                    tracker.feed(data.decode("utf-8", errors="replace"))
                except Exception as e:
                    print(f"[parse] {e}", file=sys.stderr)
    except (OSError, ConnectionError):
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client):
    try:
        server = socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=15)
    except OSError as e:
        print(f"  ! cannot reach {SERVER_HOST}:{SERVER_PORT} — {e}")
        client.close()
        return
    server.settimeout(None)

    with LOCK:
        STATE["connected"] = True
    print("  · client connected")

    t = Tracker()
    rec = RECORDER
    if rec is not None:
        rec.open()

    def client_tap(text):
        t.client_input(text)
        if rec is not None:
            rec.client(text)

    # statsync: quietly fetch our own stat sheet; the reply is kept from the
    # game window by `replies` but still read by the tracker.
    send_lock = threading.Lock()
    replies = SS.ReplyFilter()
    def note_sent(req):
        if rec is not None:
            rec.note("[auto stats] > " + req.decode("utf-8", "replace")
                     .replace("\x00", "<NUL>"))
    sync = SS.StatsSync(t, server, send_lock, replies, STATE, LOCK,
                        enabled=AUTO_STATS, on_sent=note_sent)
    with LOCK:
        STATE["statsync_enabled"] = AUTO_STATS
    threading.Thread(target=sync.run, daemon=True).start()

    a = threading.Thread(target=pump, daemon=True,
                         args=(server, client, t, rec.server if rec else None),
                         kwargs={"out_filter": replies})
    b = threading.Thread(target=pump, daemon=True,
                         args=(client, server, None, client_tap),
                         kwargs={"send_lock": send_lock,
                                 "on_bytes": sync.note_client})
    a.start(); b.start(); a.join(); b.join()
    sync.alive = False
    if rec is not None:
        rec.close()

    client.close(); server.close()
    with LOCK:
        STATE["connected"] = False
        STATE["fight"] = None
    print("  · client disconnected")


def parse_args(a):
    global SERVER_HOST, SERVER_PORT, LISTEN_PORT, WEB_PORT, RAW_LOG
    if "-h" in a: SERVER_HOST = a[a.index("-h") + 1]
    if "-p" in a: SERVER_PORT = int(a[a.index("-p") + 1])
    if "-l" in a: LISTEN_PORT = int(a[a.index("-l") + 1])
    if "-w" in a: WEB_PORT = int(a[a.index("-w") + 1])
    if "--log" in a: RAW_LOG = a[a.index("--log") + 1]
    if "--no-auto-stats" in a:
        global AUTO_STATS
        AUTO_STATS = False
    if "--record" in a:
        i = a.index("--record")
        nxt = a[i + 1] if i + 1 < len(a) and not a[i + 1].startswith("-") else None
        enable_recording(nxt)


def init_calibration():
    """Load calibration.json and (in the background) learn from every
    recording, so advice is calibrated from the start."""
    global CAL
    if CAL is not None:
        return CAL
    CAL = CAL_MOD.Calibration(CAL_PATH)
    folder = RECORDER.folder if RECORDER else os.path.join(HERE, "recordings")

    def learn():
        try:
            CAL.sizes = {}
            n = CAL.learn_recordings(folder, CAL_MOD._scaled_from_line)
            print(f"  calibration: learned from {n} recorded fights")
        except Exception as e:
            print(f"  ! calibration failed: {e}", file=sys.stderr)
    threading.Thread(target=learn, daemon=True).start()
    return CAL


def start(open_browser=True, background=False):
    """Start the dashboard + proxy.

    background=False blocks forever in the accept loop (CLI use).
    background=True runs everything on daemon threads and returns, so the
    launcher (launcher.py) can host the proxy inside its own window.
    Raises OSError if the proxy port is already taken.
    """
    # ThreadingHTTPServer, NOT HTTPServer: the plain one handles a single
    # request at a time. With the dashboard polling AND the overlay polling
    # (plus keep-alive holding connections open), requests queue up behind
    # each other and eventually just time out. This was why the UI went dead.
    init_calibration()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((LISTEN_HOST, LISTEN_PORT))
    s.listen(4)

    web = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), Web)
    web.daemon_threads = True
    threading.Thread(target=web.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{WEB_PORT}"
    print("\n  PHANTASIA ADVISOR")
    print(f"  dashboard   {url}")
    print(f"  (if the page hangs, check this window for errors)")
    print(f"  proxy       {LISTEN_HOST}:{LISTEN_PORT} -> {SERVER_HOST}:{SERVER_PORT}")
    print("\n  1. leave this running")
    print("  2. launch Phantasia")
    print("  3. in-game: Info -> Stats (once) so brains + magic level load\n")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def accept_loop():
        while True:
            c, _ = s.accept()
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    if background:
        threading.Thread(target=accept_loop, daemon=True).start()
    else:
        accept_loop()


def main():
    parse_args(sys.argv[1:])
    start(open_browser=True, background=False)


if __name__ == "__main__":
    main()
