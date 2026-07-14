#!/usr/bin/env python3
"""
Phantasia combat advisor - transparent TCP proxy.

Sits between the Phantasia client and the server. Forwards every byte
untouched in both directions, but reads a copy of the server->client stream
to track your live character sheet and detect monster encounters.

  Client  <-->  [ this proxy on 127.0.0.1:43302 ]  <-->  phantasia4.net:43302

Setup:
  1. Run this script.
  2. Point the Phantasia client at 127.0.0.1 instead of phantasia4.net.
     (If the client has no host setting, add a hosts-file entry mapping
      phantasia4.net to 127.0.0.1 and run the proxy on port 43302.)

Protocol (from phantcli/packet.h + handlers.c):
  Newline-delimited text. A numeric header line, then N content lines.
  Stat packets carry their value as plain text parsed with atoi().

IMPORTANT: magiclvl / brains / quickness are NOT sent as stat packets.
They only appear in the server's printed Stats screen (WRITE_LINE text),
so we scrape them from there and cache them. Check your stats in-game once
after login and the advisor is fully armed.
"""

import socket
import threading
import select
import re
import sys

from combat import Player, Monster, advise, aon_chance, luckout_chance, ML_BOLT
import monsters as MDB

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 43302
SERVER_HOST = "phantasia5.com"
SERVER_PORT = 43302
RAW_LOG = None   # set with --log <file> to capture the raw server stream

# ---- packet headers (server -> client) ----
CLEAR = 10
WRITE_LINE = 11
BUTTONS = 20
FULL_BUTTONS = 21

NAME = 40
LOCATION = 41
ENERGY = 42
STRENGTH = 43
SPEED = 44
SHIELD = 45
SWORD = 46
QUICKSILVER = 47
MANA = 48
LEVEL = 49
GOLD = 50
GEMS = 51
CLOAK = 52
BLESSING = 53
CROWN = 54
PALANTIR = 55
RING = 56
VIRGIN = 57
PLAYER_INFO = 35
AMULETS = 59
CHARMS = 60
TOKENS = 61
STAFF = 62
EXP = 63

# how many content lines follow each header
LINECOUNT = {
    CLEAR: 0,
    NAME: 1,
    LOCATION: 3,
    ENERGY: 3,      # cur, max, +
    STRENGTH: 2,
    SPEED: 2,
    SHIELD: 1,
    SWORD: 1,
    QUICKSILVER: 1,
    MANA: 2,        # PHANT5 sends 2
    LEVEL: 1,
    GOLD: 1,
    GEMS: 1,
    CLOAK: 1,
    BLESSING: 1,
    CROWN: 1,
    PALANTIR: 1,
    RING: 1,
    VIRGIN: 1,
    AMULETS: 1,
    CHARMS: 1,
    TOKENS: 1,
    STAFF: 1,
    EXP: 1,
    WRITE_LINE: 1,
}

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"


def num(s):
    try:
        return float(re.sub(r"[^0-9.\-]", "", s) or 0)
    except ValueError:
        return 0.0


class Tracker:
    """Parses the server stream and keeps a live character sheet."""

    def __init__(self):
        self.p = Player()
        self.pending = None      # (header, [lines], needed)
        self.awaiting_count = False
        self.in_fight = False
        self.monster = None
        self.last_lines = []
        self.have_magic = False
        self.max_size_seen = 0

    # ---- packet stream ----
    def feed(self, text):
        for line in text.split("\n"):
            self._line(line)

    def _line(self, line):
        line = line.rstrip("\r")

        if getattr(self, "awaiting_count", False):
            self.awaiting_count = False
            try:
                n = int(line.strip())
            except ValueError:
                return
            self.pending = (PLAYER_INFO, [], n)
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

        # packet 35 (player info / stats screen) is variable length:
        # header, then a count line, then that many text lines
        if hdr == PLAYER_INFO:
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
        elif hdr == RING:
            p.ring = (lines[0].strip().lower() == "yes")
        elif hdr in (BUTTONS, FULL_BUTTONS):
            pass
        elif hdr == CLEAR:
            self.last_lines = []
        elif hdr == WRITE_LINE:
            self._text(lines[0] if lines else "")
        elif hdr == PLAYER_INFO:
            for ln in lines:
                self._text(ln)

    # ---- server text ----
    def _text(self, txt):
        self.last_lines.append(txt)
        low = txt.lower()

        # --- scrape the Stats screen (the ONLY source of magiclvl/brains) ---
        m = re.search(r"magic\s*level\s*:\s*([\d.]+)", low)
        if m:
            self.p.magiclvl = float(m.group(1))
            self.have_magic = True
        m = re.search(r"brains\s*:\s*([\d.]+)", low)
        if m:
            self.p.brains = float(m.group(1))
        # "Quickness: 35 (35 + 0 quicksilver)"
        m = re.search(r"quickness\s*:\s*([\d.]+)", low)
        if m:
            self.p.quickness = float(m.group(1))
        # "Strength: 36 (36 + 0 sword)"
        m = re.search(r"strength\s*:\s*([\d.]+)\s*\(\s*[\d.]+\s*\+\s*([\d.]+)\s*sword", low)
        if m:
            self.p.strength = float(m.group(1))
            self.p.sword = float(m.group(2))
        # "Energy: 128 (128 + 0 shield)"
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
        m = re.search(r"ring\s*:\s*(yes|no)", low)
        if m:
            self.p.ring = (m.group(1) == "yes")

        # --- encounter detection ---
        # Real server formats (from captured traffic):
        #   "You find and attack A Bogle. -  (Size: 2)"
        #   "You are attacked by A Kobold. -  (Size: 2)"
        #   "A Crebain's friend appears and attacks. -  (Size: 2)"
        is_enc = ("you find and attack" in low
                  or "you are attacked by" in low
                  or "friend appears and attacks" in low)

        if is_enc:
            size = 1
            ms = re.search(r"\(size:\s*(\d+)\)", low)
            if ms:
                size = int(ms.group(1))

            flock = "friend appears" in low
            name = None
            if flock:
                # "A Crebain's friend appears..." -> strip the possessive
                mf = re.match(r"\s*(.+?)'s friend appears", txt, re.I)
                if mf:
                    name = MDB.lookup(mf.group(1))
            if not name:
                name = MDB.lookup(txt)

            if name:
                self.on_encounter(name, size, txt, flock=flock)
            return

        # live damage: "A Kobold hit you for 10 damage!"
        if "hit you for" in low:
            pass

        if ("you made it" in low or "you got away" in low
                or "you killed it" in low or "you have been killed" in low
                or "it wandered off" in low):
            self.in_fight = False
            self.monster = None

    def on_encounter(self, name, size, raw, flock=False):
        mon = MDB.scaled(name, size)
        if not mon:
            return
        self.in_fight = True
        self.monster = mon
        self.report(mon, size_known=True, flock=flock)

    # ---- advice ----
    def report(self, mon, size_known=False, flock=False):
        p = self.p
        m = Monster(
            name=mon["name"],
            strength=mon["strength"],
            brains=mon["brains"],
            speed=mon["speed"],
            energy=mon["energy"],
            shield=0.0,
            experience=mon["experience"],
            specials=tuple(mon["specials"]),
        )

        print()
        print(f"{BOLD}{CYAN}{'='*66}{RESET}")
        tag = "  [FLOCK — another one]" if flock else ""
        newbig = ""
        if mon["size"] > self.max_size_seen:
            if self.max_size_seen > 0:
                newbig = f"  {YELLOW}<-- BIGGER THAN ANYTHING YOU'VE FOUGHT{RESET}"
            self.max_size_seen = mon["size"]
        print(f"{BOLD}  {mon['name']}   (size {mon['size']}){tag}{RESET}{newbig}")
        print(f"{CYAN}{'='*66}{RESET}")
        print(f"  Monster : energy {m.energy:.0f}  str {m.strength:.0f}  "
              f"speed {m.speed:.0f}  brains {m.brains:.0f}  exp {m.experience:.0f}")
        print(f"  You     : energy {p.energy:.0f}  str {p.strength:.0f}  "
              f"mana {p.mana:.0f}  ML {p.magiclvl:.0f}  sword {p.sword:.0f}  "
              f"quick {p.quickness:.0f}  brains {p.brains:.0f}")

        for n in MDB.notes_for(mon):
            print(f"  {YELLOW}! {n}{RESET}")

        if not self.have_magic:
            print(f"  {RED}! Magic level unknown — open Info->Stats once.{RESET}")
        elif p.magiclvl < ML_BOLT:
            print(f"  {RED}! Magic Level {p.magiclvl:.0f} — MAGIC BOLT LOCKED (needs 5). "
                  f"Your {p.mana:.0f} mana is dead weight until then.{RESET}")

        # expected one-shot numbers
        might = p.might()
        print(f"  {DIM}melee ≈ {might*1.15:.0f}/swing   "
              f"full bolt ({p.mana:.0f} mana) ≈ "
              f"{p.mana * 1.0 * (p.magiclvl**0.40 + 1):.0f}{RESET}")

        lo = luckout_chance(p, m)
        if lo < 0 or p.brains <= 0 or m.brains <= 0:
            why = ("your brains unknown — open Info->Stats" if p.brains <= 0
                   else f"no brains data for '{m.name}' — monster not in table?")
            print(f"  {RED}Luckout: UNKNOWN ({why}). Do NOT trust this as a free kill.{RESET}")
        else:
            base = MDB.MONSTERS[m.name][1] if m.name in MDB.MONSTERS else 0
            scale_note = ""
            if mon["size"] > 1 and base:
                scale_note = (f"  [size {mon['size']}x: {base} -> {m.brains:.0f} brains]")

            if lo >= 0.75:
                print(f"  {GREEN}{BOLD}LUCKOUT {lo*100:.0f}% — FREE INSTANT KILL, take it.{RESET}"
                      f"{DIM}  (your {p.brains:.0f} vs its {m.brains:.0f}){scale_note}{RESET}")
            elif lo >= 0.60:
                print(f"  {YELLOW}Luckout {lo*100:.0f}% — good, but fails ~1 in "
                      f"{max(2, round(1/(1-lo)))}.{RESET}"
                      f"{DIM}  (your {p.brains:.0f} vs its {m.brains:.0f}){scale_note}{RESET}")
            else:
                print(f"  {RED}{BOLD}DON'T LUCKOUT — only {lo*100:.0f}%.{RESET}"
                      f"{RED}  Too brainy: your {p.brains:.0f} vs its {m.brains:.0f}."
                      f"{scale_note}{RESET}")
                print(f"  {RED}  You get ONE try. A miss wastes the round and it swings back.{RESET}")

        if p.magiclvl >= 0 and not m.spell_immune():
            ch = aon_chance(p, m)
            verdict = (f"{GREEN}worth it{RESET}" if ch > 0.35
                       else f"{RED}bad gamble{RESET}" if ch < 0.12
                       else f"{YELLOW}coin-flip{RESET}")
            print(f"  {DIM}All-or-Nothing: {ch*100:.0f}% instant kill for 1 mana "
                  f"— {verdict}  (fail doubles its str+speed){RESET}")

        print(f"\n{BOLD}  Best actions:{RESET}")
        for r in advise(p, m, trials=2500)[:5]:
            label = r["action"]
            if r["bolt_mana"]:
                label = f"bolt {r['bolt_mana']:.0f} mana"
            wr = r["win_rate"]
            col = GREEN if wr > 0.85 else YELLOW if wr > 0.5 else RED
            extra = ""
            if r["action"] == "evade":
                extra = f"  (flee {r['flee_rate']*100:.0f}%)"
            print(f"   {col}{label:<18}{RESET} win {col}{wr*100:5.1f}%{RESET}  "
                  f"lose ~{r['avg_energy_lost']:6.1f} energy  "
                  f"mana ~{r['avg_mana_spent']:5.1f}  "
                  f"{r['avg_rounds']:.1f} rds{extra}")
        print(f"{CYAN}{'='*66}{RESET}\n")


def pump(src, dst, tracker=None):
    """Forward bytes, optionally sniffing them."""
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
                    print(f"{RED}[parse error] {e}{RESET}", file=sys.stderr)
    except (OSError, ConnectionError):
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client, addr):
    print(f"{DIM}[client connected from {addr[0]}]{RESET}")
    try:
        server = socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=15)
    except OSError as e:
        print(f"{RED}[cannot reach {SERVER_HOST}:{SERVER_PORT}: {e}]{RESET}")
        client.close()
        return

    server.settimeout(None)
    tracker = Tracker()

    t1 = threading.Thread(target=pump, args=(server, client, tracker), daemon=True)
    t2 = threading.Thread(target=pump, args=(client, server, None), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    client.close()
    server.close()
    print(f"{DIM}[client disconnected]{RESET}")


def main():
    global SERVER_HOST, SERVER_PORT, LISTEN_PORT, RAW_LOG
    args = sys.argv[1:]
    if "-h" in args:
        SERVER_HOST = args[args.index("-h") + 1]
    if "-p" in args:
        SERVER_PORT = int(args[args.index("-p") + 1])
    if "-l" in args:
        LISTEN_PORT = int(args[args.index("-l") + 1])
    if "--log" in args:
        RAW_LOG = args[args.index("--log") + 1]

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((LISTEN_HOST, LISTEN_PORT))
    s.listen(4)

    print(f"{BOLD}Phantasia advisor{RESET}")
    print(f"  listening  {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  forwarding {SERVER_HOST}:{SERVER_PORT}")
    print(f"  {DIM}point your client at {LISTEN_HOST}:{LISTEN_PORT}{RESET}\n")

    while True:
        c, a = s.accept()
        threading.Thread(target=handle, args=(c, a), daemon=True).start()


if __name__ == "__main__":
    main()
