#!/usr/bin/env python3
"""
Character-creation reroller: decides Reroll vs Keep on the
"Do you wish to keep these stats?" screen.

Roll ranges come from https://www.phantasia5.com/stats#characters
("Each character class starts off with base statistics ... rolled randomly
from the following list"). Copied here as (low, high) per stat.

Class: inferred from the roll itself (the only class whose ranges contain
every rolled value), or given explicitly.

Score of a roll = weighted average of where each stat sits in its class
range (0 = lowest possible, 1 = highest possible). Weights per class follow
what the site says each class is good at, plus brains everywhere because
luckout runs on it. Override with launcher.json "reroll_weights".

Keep rule: a score threshold chosen so that, on average, about TARGET_ROLLS
rolls are needed to hit it (worked out by simulating the class's rolls).
Higher TARGET_ROLLS = pickier = better stats, slower. HARD_STOP rolls and it
keeps whatever is on screen. Experimento never varies, so it keeps at once.
"""

import random

# stat keys: the launcher's names for the roll screen values
#   strength, speed (Quick), mana, energy, brains, magiclvl (Magic)
RANGES = {
    "Magic User":  {"strength": (10, 14), "speed": (29, 33), "mana": (50, 100),
                    "energy": (40, 60), "brains": (40, 63), "magiclvl": (5, 10)},
    "Fighter":     {"strength": (25, 50), "speed": (33, 37), "mana": (30, 45),
                    "energy": (50, 75), "brains": (10, 20), "magiclvl": (2, 4)},
    "Elf":         {"strength": (12, 21), "speed": (36, 40), "mana": (40, 75),
                    "energy": (30, 50), "brains": (30, 50), "magiclvl": (4, 8)},
    "Dwarf":       {"strength": (20, 40), "speed": (26, 30), "mana": (35, 60),
                    "energy": (70, 100), "brains": (20, 35), "magiclvl": (3, 6)},
    "Halfling":    {"strength": (16, 34), "speed": (31, 35), "mana": (25, 30),
                    "energy": (60, 85), "brains": (50, 80), "magiclvl": (1, 2)},
    "Gnome":       {"strength": (20, 34), "speed": (32, 38), "mana": (30, 59),
                    "energy": (40, 79), "brains": (25, 45), "magiclvl": (3, 5)},
    "Ent":         {"strength": (44, 54), "speed": (23, 28), "mana": (80, 98),
                    "energy": (78, 90), "brains": (16, 30), "magiclvl": (2, 4)},
    "Experimento": {"strength": (25, 25), "speed": (33, 33), "mana": (40, 40),
                    "energy": (60, 60), "brains": (40, 40), "magiclvl": (4, 4)},
}
STATS = ("strength", "speed", "mana", "energy", "brains", "magiclvl")

WEIGHTS = {
    # "strong in magic level and brains"
    "Magic User":  {"magiclvl": 3, "brains": 3, "mana": 2, "energy": 1.5,
                    "speed": 1, "strength": 0.5},
    # "good in strength and speed"
    "Fighter":     {"strength": 3, "energy": 2.5, "speed": 2, "brains": 1.5,
                    "mana": 0.5, "magiclvl": 0.5},
    # "very high quickness and above average magic level"
    "Elf":         {"speed": 3, "magiclvl": 2.5, "brains": 2, "energy": 1.5,
                    "mana": 1.5, "strength": 1},
    # "very high energy level and strength"
    "Dwarf":       {"energy": 3, "strength": 3, "brains": 1.5, "speed": 1,
                    "mana": 0.5, "magiclvl": 0.5},
    # "very smart ... excels through the use of luckout"
    "Halfling":    {"brains": 4, "energy": 2, "strength": 1.5, "speed": 1.5,
                    "mana": 0.5, "magiclvl": 0.5},
    # "fair in all respects other than mana"
    "Gnome":       {"strength": 2, "energy": 2, "brains": 2, "speed": 1.5,
                    "magiclvl": 1.5, "mana": 0.5},
    "Ent":         {"strength": 3, "energy": 2.5, "brains": 1.5, "mana": 1,
                    "speed": 1, "magiclvl": 1},
    "Experimento": {s: 1 for s in STATS},
}

TARGET_ROLLS = 100   # pickiness: ~this many rolls on average to hit the bar
HARD_STOP = 500      # never roll more than this for one character


def class_name(label):
    """Our class name for a game button label ("Magic-User" -> "Magic User"),
    or None if the label isn't a class."""
    norm = label.replace("-", " ").strip().lower()
    for c in RANGES:
        if c.lower() == norm:
            return c
    return None


def fitting_classes(stats):
    """Every class whose ranges contain all of the rolled values."""
    return {c for c, rng in RANGES.items()
            if all(stats.get(s) is not None and lo <= stats[s] <= hi
                   for s, (lo, hi) in rng.items())}


def infer_class(stats):
    """The one class this roll could come from, or None if several/none."""
    fits = fitting_classes(stats)
    return next(iter(fits)) if len(fits) == 1 else None


class RollJudge:
    def __init__(self, weights=None, target_rolls=TARGET_ROLLS,
                 hard_stop=HARD_STOP):
        self.weights = {c: dict(w) for c, w in WEIGHTS.items()}
        for c, w in (weights or {}).items():
            if c in self.weights:
                self.weights[c].update(w)
        self.target_rolls = max(1, int(target_rolls))
        self.hard_stop = hard_stop
        self._bar = {}           # class -> threshold score
        self.rolls = 0           # rolls seen for the current character
        self.cls = None
        self.cands = set(RANGES)  # classes consistent with every roll so far
        self.decided = {}        # roll n -> decision (decide once per roll)
        self.char = None         # which character's rolls we're judging
        self.recent = []         # last few rolls, to spot fixed-stat classes

    def score(self, stats, cls):
        rng, w = RANGES[cls], self.weights[cls]
        num = den = 0.0
        for s, (lo, hi) in rng.items():
            if hi <= lo or not w.get(s):
                continue
            v = min(max(stats[s], lo), hi)
            num += w[s] * (v - lo) / (hi - lo)
            den += w[s]
        return num / den if den else 1.0

    def bar(self, cls, samples=20000, seed=1):
        """Score that a random roll beats with chance 1/target_rolls."""
        if cls not in self._bar:
            r = random.Random(seed)
            rng = RANGES[cls]
            scores = sorted(
                self.score({s: r.randint(lo, hi) for s, (lo, hi) in rng.items()},
                           cls)
                for _ in range(samples))
            i = int(len(scores) * (1 - 1 / self.target_rolls))
            self._bar[cls] = scores[min(i, len(scores) - 1)]
        return self._bar[cls]

    def decide(self, roll):
        """roll = {"char": id, "n": int, "stats": {...}, "class": str|None,
        "class_options": [classes the player's click could mean] | None}.
        "char" changes for each new character being rolled; "n" counts that
        character's rolls from 1.
        Returns {"action": "reroll"|"keep", "class", "score", "need",
        "rolls", "why"}. Decides once per roll number."""
        n = roll.get("n")
        char = roll.get("char")
        if char != self.char:
            self.char = char
            self.rolls, self.cls, self.cands = 0, None, set(RANGES)
            self.decided, self.recent = {}, []
        if n in self.decided:
            return self.decided[n]
        stats = roll.get("stats") or {}
        self.rolls += 1
        self.recent = (self.recent + [tuple(stats.get(k) for k in STATS)])[-3:]
        # Ranges overlap between classes, but every roll of one character
        # comes from the same class: narrow the candidates roll by roll.
        fits = fitting_classes(stats)
        picked = {class_name(c) or c for c in (roll.get("class_options") or [])}
        if picked and (picked & fits):
            fits = picked & fits        # what the player clicked, if it fits
        self.cands = (self.cands & fits) or fits
        if (len(self.recent) == 3 and len(set(self.recent)) == 1
                and "Experimento" in self.cands):
            self.cands = {"Experimento"}   # same roll 3 times: fixed stats
        cls = roll.get("class") or (next(iter(self.cands))
                                    if len(self.cands) == 1 else None)
        self.cls = cls

        if cls is None and self.cands and self.rolls < self.hard_stop:
            out = {"action": "reroll", "class": None, "score": None,
                   "need": None, "rolls": self.rolls,
                   "why": "working out the class: could be "
                          + " / ".join(sorted(self.cands))}
        elif cls is None:
            out = {"action": None, "class": None, "score": None, "need": None,
                   "rolls": self.rolls,
                   "why": "these stats don't match any class on the stats page"}
        elif all(lo == hi for lo, hi in RANGES[cls].values()):
            out = {"action": "keep", "class": cls, "score": 1.0, "need": 1.0,
                   "rolls": self.rolls, "why": f"{cls} stats never change"}
        else:
            sc, need = self.score(stats, cls), self.bar(cls)
            if sc >= need:
                why = f"top 1-in-{self.target_rolls} roll for a {cls}"
                act = "keep"
            elif self.rolls >= self.hard_stop:
                why, act = f"{self.rolls} rolls: taking this one", "keep"
            else:
                why, act = f"below the bar for a {cls}", "reroll"
            out = {"action": act, "class": cls, "score": round(sc, 3),
                   "need": round(need, 3), "rolls": self.rolls, "why": why}
        self.decided[n] = out
        return out
