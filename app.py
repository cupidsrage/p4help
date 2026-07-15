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

import socket
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
import zones as ZN
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
    "trove": None,
    "zone": None,
    "log": [],
    "buttons": [],
}
LOCK = threading.Lock()


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
        self.search = None      # TroveSearch, once we have an estimate
        self.cur = None            # current monster dict
        self.cur_mon = None        # Monster object
        self.fs = None             # live FightState
        self.await_luck = False    # a luckout is in flight
        self.prev_energy = None

    # ---------- packet framing ----------
    def feed(self, text):
        for line in text.split("\n"):
            self._line(line)

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
            p.level = v
        elif hdr == GOLD:
            self.gold = v
        elif hdr == NAME:
            self.name = lines[0].strip() if lines else ""
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
                if self.search and self.xy:
                    self.search.visit(*self.xy)
                self.push_trove()
                self.push_zone()
        elif hdr == RING:
            p.ring = (lines[0].strip().lower() == "yes")
        elif hdr in (BUTTONS, FULL_BUTTONS):
            with LOCK:
                STATE["buttons"] = [line.strip() for line in lines]
        elif hdr == WRITE_LINE:
            self._text(lines[0] if lines else "")
        elif hdr == PLAYER_INFO:
            for ln in lines:
                self._text(ln)

        self.push()

    # ---------- server text ----------
    def _text(self, txt):
        low = txt.lower()

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
            self.p.max_energy = float(m.group(2))
            self.p.shield = float(m.group(3))
        m = re.search(r"^\s*level\s*:\s*([\d.]+)", low)
        if m:
            self.p.level = float(m.group(1))
        m = re.search(r"^\s*mana\s*:\s*([\d.]+)", low)
        if m:
            self.p.mana = float(m.group(1))
        m = re.search(r"^\s*gold\s*:\s*([\d.]+)", low)
        if m:
            self.gold = float(m.group(1))

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

        # ---- treasure trove scrolls ----
        if any(k in low for k in TR.SCROLL_MARKERS):
            t = TR.parse_scroll(txt, origin=self.xy)
            if t:
                if self.trove:
                    t.leg = self.trove.leg + 1
                self.trove = t
                # Feed the reading to the triangulator. The scroll's DIRECTION is
                # exact but its DISTANCE is fudged +/-12.5% (treasure.c:1584), so
                # a single scroll can't pinpoint it -- but several can.
                pos = self.xy or t.origin
                if pos:
                    # avoid logging two readings at the exact same coordinates --
                    # that adds no triangulation info. Only skip if identical.
                    dup = any(abs(rx - pos[0]) < 1 and abs(ry - pos[1]) < 1
                              for rx, ry, *_ in self.solver.readings)
                    if not dup:
                        self.solver.add(pos[0], pos[1], t.distance, t.direction)
                    else:
                        self.note("TROVE scroll read at the SAME spot as the last "
                                  "one - move before reading to triangulate.")
                    est = self.solver.estimate(step=12)
                    if est:
                        self.note(f"TROVE scroll {est['readings']}: "
                                  f"{t.distance} sq {t.direction} -> best guess "
                                  f"({est['x']}, {est['y']}) +/-{est['uncertainty']:.0f}")
                        # start (or recenter) the on-foot sweep box
                        rad = max(6, int(est["uncertainty"]))
                        if self.search is None:
                            self.search = TR.TroveSearch(est["x"], est["y"], rad)
                            self.note(f"SEARCH BOX: {self.search.total_squares()} "
                                      f"squares to sweep around "
                                      f"({est['x']}, {est['y']})")
                        else:
                            self.search.recenter(est["x"], est["y"], rad)
                else:
                    self.note("TROVE scroll read but position unknown - "
                              "move one square so the game reports your location, "
                              "then it can triangulate.")
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
                self.fs.m_shield -= dmg
                if self.fs.m_shield < 0:
                    self.fs.m_energy += self.fs.m_shield
                    self.fs.m_shield = 0.0
                changed = True

            # "A Kobold hit you for 10 damage!"
            if "hit you for" in low:
                changed = True

            # luckout resolved
            if "you blew it" in low:
                self.fs.luckout_spent = True
                self.await_luck = False
                with LOCK:
                    STATE["session"]["lk"] += 1
                self.note("luckout FAILED — falling back")
                changed = True

            # AoN failed: it doubles strength and speed
            if "you blew it" not in low and (
                    "grows stronger" in low or "gets stronger" in low):
                self.fs.m_strength *= 2.0
                self.fs.m_speed *= 2.0
                self.fs.aon_spent = True
                self.note("AoN FAILED — it doubled str + speed")
                changed = True

            if "paralyzed" in low or "cannot move" in low:
                self.fs.m_paralyzed = True
                self.fs.m_speed = -2.0
                changed = True

            if changed:
                self.refresh_advice()

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

    # ---------- fight lifecycle ----------
    def encounter(self, name, size, flock):
        d = MDB.scaled(name, size)
        if not d:
            return
        m = Monster(name=d["name"], strength=d["strength"], brains=d["brains"],
                    speed=d["speed"], energy=d["energy"], shield=0.0,
                    experience=d["experience"], specials=tuple(d["specials"]))

        self.cur_mon = m
        self.fs = FightState.opening(self.p, m)

        warn = list(MDB.notes_for(d))
        if size > self.max_size_seen and self.max_size_seen > 0:
            warn.insert(0, f"Size {size} — bigger than anything you've fought this session.")
        self.max_size_seen = max(self.max_size_seen, size)
        if not self.have_magic:
            warn.insert(0, "Brains/Magic Level unknown — open Info → Stats.")

        self.cur = {
            "name": d["name"], "size": size, "flock": flock,
            "energy": d["energy"], "strength": d["strength"],
            "brains": d["brains"], "base_brains": MDB.MONSTERS[name][1],
            "experience": d["experience"], "speed": d["speed"],
            "warnings": warn,
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
            "flee": x["flee"], "energy_lost": x["energy_lost"],
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
        self.cur = None
        self.cur_mon = None
        self.fs = None
        self.await_luck = False
        with LOCK:
            STATE["fight"] = None

    def push_zone(self):
        """Kick off a zone recalc on a background thread (it's slow: it
        actually simulates sessions). Never runs on the packet thread."""
        if self.p.brains <= 0 or self.p.strength <= 0:
            return
        if getattr(self, "_zone_busy", False):
            return                      # one at a time
        self._zone_busy = True
        snap = (self.p, self.xy)
        threading.Thread(target=self._zone_worker, args=snap, daemon=True).start()

    def _zone_worker(self, player, xy):
        try:
            best, rows = ZN.recommend(player, here=xy, measured=True)
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
            if self.search:
                d["search"] = self.search.to_dict(self.xy)
            est = self.solver.estimate(step=12)
            if est:
                d["solved"] = est
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
        with LOCK:
            STATE["player"] = {
                "name": self.name, "energy": p.energy, "max_energy": p.max_energy,
                "mana": p.mana, "strength": p.strength, "quickness": p.quickness,
                "brains": p.brains, "magiclvl": p.magiclvl, "level": p.level,
                "sword": p.sword, "shield": p.shield, "ring": p.ring,
                "gold": self.gold, "location": self.location,
            }


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
def pump(src, dst, tracker=None):
    try:
        while True:
            data = src.recv(8192)
            if not data:
                break
            dst.sendall(data)
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
    a = threading.Thread(target=pump, args=(server, client, t), daemon=True)
    b = threading.Thread(target=pump, args=(client, server, None), daemon=True)
    a.start(); b.start(); a.join(); b.join()

    client.close(); server.close()
    with LOCK:
        STATE["connected"] = False
        STATE["fight"] = None
    print("  · client disconnected")


def main():
    global SERVER_HOST, SERVER_PORT, LISTEN_PORT, WEB_PORT, RAW_LOG
    a = sys.argv[1:]
    if "-h" in a: SERVER_HOST = a[a.index("-h") + 1]
    if "-p" in a: SERVER_PORT = int(a[a.index("-p") + 1])
    if "-l" in a: LISTEN_PORT = int(a[a.index("-l") + 1])
    if "-w" in a: WEB_PORT = int(a[a.index("-w") + 1])
    if "--log" in a: RAW_LOG = a[a.index("--log") + 1]

    # ThreadingHTTPServer, NOT HTTPServer: the plain one handles a single
    # request at a time. With the dashboard polling AND the overlay polling
    # (plus keep-alive holding connections open), requests queue up behind
    # each other and eventually just time out. This was why the UI went dead.
    web = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), Web)
    web.daemon_threads = True
    threading.Thread(target=web.serve_forever, daemon=True).start()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((LISTEN_HOST, LISTEN_PORT))
    s.listen(4)

    url = f"http://127.0.0.1:{WEB_PORT}"
    print("\n  PHANTASIA ADVISOR")
    print(f"  dashboard   {url}")
    print(f"  (if the page hangs, check this window for errors)")
    print(f"  proxy       {LISTEN_HOST}:{LISTEN_PORT} -> {SERVER_HOST}:{SERVER_PORT}")
    print("\n  1. leave this running")
    print("  2. launch Phantasia")
    print("  3. in-game: Info -> Stats (once) so brains + magic level load\n")

    try:
        webbrowser.open(url)
    except Exception:
        pass

    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
