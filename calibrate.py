#!/usr/bin/env python3
"""
Learn the real monster numbers from your own fights.

The advisor's monster energy/strength come from the monster table scaled
by size the way the Phantasia 4 server does it (fight.c). Phantasia 5 may
differ; if it overestimates
a size-6 monster's energy, melee looks hopeless and it reaches for Magic
Bolt when melee would have won.

For every fight won by damage (not luckout) this records, per monster size:
  energy ratio   = damage it actually took to kill / predicted energy
  strength ratio = how hard it actually hit you  / predicted
and keeps a running average in calibration.json. The tracker multiplies a
new monster's predicted energy and strength by those ratios once a size has
at least MIN_FIGHTS fights. At startup it also learns from every recording
in the recordings folder, so it starts out calibrated.

    python calibrate.py            # show what's been learned (and re-learn
                                   # from the recordings)
"""

import glob
import json
import os
import re
import threading

MIN_FIGHTS = 3          # don't correct a size until this many fights
MAX_WEIGHT = 40         # running average remembers about this many fights
CLAMP = (0.2, 3.0)      # sanity limits on a ratio

ENCOUNTER = re.compile(r"\(size:\s*(\d+)\)", re.I)
TAINTING = ("luckout", "spell")   # a fight ended by these isn't a damage kill
YOU_HIT = re.compile(r"you hit .*? for ([\d.]+) damage", re.I)
HIT_YOU = re.compile(r"hit you for ([\d.]+) damage", re.I)


class Calibration:
    def __init__(self, path=None):
        self.path = path
        self.lock = threading.Lock()
        self.sizes = {}          # "6" -> {"energy": r, "strength": r, "n": k, "ns": k}
        self._load()

    # ---- persistence ----
    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.sizes = json.load(f).get("sizes", {})
        except (OSError, ValueError):
            self.sizes = {}

    def save(self):
        if not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"sizes": self.sizes}, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---- learning ----
    @staticmethod
    def _avg(old, n, new):
        w = min(n, MAX_WEIGHT - 1)
        return (old * w + new) / (w + 1)

    def record(self, size, pred_energy, pred_strength, dmg_out, dmg_in,
               save=True):
        """One finished fight. dmg_out: your hits on it, in order; dmg_in:
        its hits on you, each a number or (damage, your total damage dealt
        before it). Only call for fights won purely by those hits (no
        luckout or spell finished it -- see fight_counts)."""
        if not dmg_out or pred_energy <= 0:
            return
        total = sum(dmg_out)
        # it died somewhere inside the last hit
        actual = max(1.0, total - dmg_out[-1] / 2.0)
        if actual < 0.25 * pred_energy and len(dmg_out) < 2:
            return                       # looks like a luckout/spell kill
        er = min(max(actual / pred_energy, CLAMP[0]), CLAMP[1])
        with self.lock:
            s = self.sizes.setdefault(str(size), {"energy": 1.0, "strength": 1.0,
                                                  "n": 0, "ns": 0})
            s["energy"] = er if s["n"] == 0 else self._avg(s["energy"], s["n"], er)
            s["n"] += 1
            if len(dmg_in) >= 3 and pred_strength > 0:
                # A hit is 1 + r * strength, r uniform: mean 1 + strength/2.
                # Melee wears its strength down by up to a third as its
                # energy runs out (fight.c), so compare each hit with the
                # strength it had at that moment.
                got = want = 0.0
                for h in dmg_in:
                    dmg, before = h if isinstance(h, (tuple, list)) else (h, 0.0)
                    wear = min(before, actual) / (3.0 * actual)
                    got += dmg - 1.0
                    want += pred_strength * (1.0 - wear) / 2.0
                sr = min(max(got / want, CLAMP[0]), CLAMP[1])
                s["strength"] = sr if s["ns"] == 0 else self._avg(s["strength"], s["ns"], sr)
                s["ns"] += 1
        if save:
            self.save()

    def ratios(self, size):
        """(energy ratio, strength ratio) to apply to a new monster."""
        with self.lock:
            s = self.sizes.get(str(size))
            if not s or s["n"] < MIN_FIGHTS:
                return 1.0, 1.0
            sr = s["strength"] if s.get("ns", 0) >= MIN_FIGHTS else 1.0
            return s["energy"], sr

    def summary(self):
        with self.lock:
            return {k: dict(v) for k, v in sorted(self.sizes.items(),
                                                  key=lambda kv: int(kv[0]))}

    # ---- learn from recordings ----
    @staticmethod
    def fight_counts(clicked, blew_luckout):
        """Was this fight won by plain damage? `clicked` = labels of the
        buttons pressed during it. A Luckout that didn't fail ("You blew
        it") or any Spell may have done the killing."""
        low = [c.lower() for c in clicked]
        if any(c.startswith("spell") for c in low):
            return False
        luck = sum(1 for c in low if c.startswith("luckout"))
        return luck <= blew_luckout

    def learn_recordings(self, folder, scaled):
        """Replay every recordings/*.txt transcript. `scaled(name_line,
        size)` returns (energy, strength) predicted for the monster named in
        an encounter line, or None."""
        fights = 0
        for path in sorted(glob.glob(os.path.join(folder, "*.txt"))):
            cur = None
            labels, want = [], 0          # last button set from the server
            try:
                f = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with f:
                for ln in f:
                    if " > " in ln and cur is not None:
                        i = click_index(ln.split(" > ", 1)[1])
                        if i is not None and i < len(labels):
                            cur["clicked"].append(labels[i])
                        continue
                    parts = ln.split(" < ", 1)
                    if len(parts) != 2:
                        continue
                    text = parts[1].strip()
                    if want:
                        labels.append(text)
                        want -= 1
                        continue
                    if text in ("20", "21"):
                        labels, want = [], 8
                        continue
                    m = ENCOUNTER.search(text)
                    if m:
                        pred = scaled(text, int(m.group(1)))
                        cur = None if not pred else {
                            "size": int(m.group(1)), "e": pred[0], "s": pred[1],
                            "out": [], "in": [], "clicked": [], "blew": 0}
                        continue
                    if cur is None:
                        continue
                    m = YOU_HIT.search(text)
                    if m:
                        cur["out"].append(float(m.group(1)))
                        continue
                    m = HIT_YOU.search(text)
                    if m:
                        cur["in"].append((float(m.group(1)), sum(cur["out"])))
                        continue
                    low = text.lower()
                    if "you blew it" in low:
                        cur["blew"] += 1
                        continue
                    if "you made it" in low or "you killed it" in low:
                        if self.fight_counts(cur["clicked"], cur["blew"]):
                            self.record(cur["size"], cur["e"], cur["s"],
                                        cur["out"], cur["in"], save=False)
                            fights += 1
                        cur = None
                    elif ("you got away" in low or "you have been killed" in low
                          or "wandered off" in low):
                        cur = None
        self.save()
        return fights


def click_index(text):
    """0-based button position of a client click ("1" NUL "5" NUL or "15")."""
    toks = [t.strip() for t in re.split(r"[\x00\n]|<NUL>", text) if t.strip()]
    for k, tok in enumerate(toks):
        m = re.fullmatch(r"1([0-7])", tok)
        if m:
            return int(m.group(1))
        if tok == "1" and k + 1 < len(toks) and re.fullmatch(r"[0-7]", toks[k + 1]):
            return int(toks[k + 1])
    return None


def _scaled_from_line(text, size):
    import monsters as MDB
    name = MDB.lookup(text)
    if not name:
        return None
    d = MDB.scaled(name, size)
    return (d["energy"], d["strength"]) if d else None


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    cal = Calibration(os.path.join(here, "calibration.json"))
    cal.sizes = {}
    n = cal.learn_recordings(os.path.join(here, "recordings"), _scaled_from_line)
    print(f"learned from {n} fights in recordings/")
    for size, s in cal.summary().items():
        used = "used" if s["n"] >= MIN_FIGHTS else "not enough fights yet"
        print(f"  size {size:>2}: {s['n']:3} fights  energy x{s['energy']:.2f}  "
              f"strength x{s['strength']:.2f} ({s.get('ns', 0)} with hits)  [{used}]")
