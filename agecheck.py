#!/usr/bin/env python3
"""
What actually costs age? Measured from your own recordings.

Between every two stats sheets (Info -> Stats, or the background stats the
launcher fetches) it counts what you pressed and what happened, then fits
how much Age each one adds:

    python agecheck.py                 # every recording in ./recordings
    python agecheck.py some.txt ...    # particular transcripts

The Phantasia 4 server ages you once per menu turn (Move, Rest, Hunt...)
and once per event (a monster appearing, treasure, ...); a fight costs the
same however many rounds it lasts. If your server ages you per attack, the
"attack" row below comes out near 1 instead of near 0.
"""

import glob
import os
import re
import sys

from calibrate import click_index

ATTACKS = {"melee", "skirmish", "nick", "luckout", "evade", "all or nothing",
           "magic bolt", "increase might", "haste", "force field", "paralyze",
           "transport", "transform", "use ring"}
MOVES = {"move to", "n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south",
         "east", "west"}
ROWS = ["attack", "fight rest", "rest", "hunt", "move", "monster", "friend",
        "treasure"]


def intervals(path):
    """Yield (age gained, counts) between consecutive stats sheets."""
    labels, want, in_fight = [], 0, False
    counts = dict.fromkeys(ROWS, 0)
    last_age = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            if " > " in ln:
                i = click_index(ln.split(" > ", 1)[1])
                if i is None or i >= len(labels):
                    continue
                b = labels[i].strip().lower()
                if b in ATTACKS:
                    counts["attack"] += 1
                elif b == "rest":
                    counts["fight rest" if in_fight else "rest"] += 1
                elif b == "hunt":
                    counts["hunt"] += 1
                elif b in MOVES:
                    counts["move"] += 1
                continue
            parts = ln.split(" < ", 1)
            if len(parts) != 2:
                continue
            text = parts[1].strip()
            if want:
                labels.append(text)
                want -= 1
                if not want:
                    low = {x.lower() for x in labels}
                    in_fight = "melee" in low
                continue
            if text in ("20", "21"):
                labels, want = [], 8
                continue
            if "(size:" in text.lower():
                counts["friend" if "friend appears" in text.lower() else "monster"] += 1
            elif "found some treasure" in text.lower() or "you have found" in text.lower():
                counts["treasure"] += 1
            m = re.match(r"Age:\s*(\d+)", text)
            if m:
                age = int(m.group(1))
                if last_age is not None and age != last_age:
                    yield age - last_age, dict(counts)
                if last_age is None or age != last_age:
                    counts = dict.fromkeys(ROWS, 0)
                last_age = age


def fit(data):
    """Least squares: age gained = sum(weight x count). Plain Python."""
    cols = [r for r in ROWS if any(c[r] for _, c in data)]
    n = len(cols)
    a = [[0.0] * n for _ in range(n)]
    b = [0.0] * n
    for y, c in data:
        x = [c[k] for k in cols]
        for i in range(n):
            b[i] += x[i] * y
            for j in range(n):
                a[i][j] += x[i] * x[j]
    for i in range(n):                       # Gaussian elimination
        a[i][i] += 1e-9
        p = max(range(i, n), key=lambda r: abs(a[r][i]))
        a[i], a[p], b[i], b[p] = a[p], a[i], b[p], b[i]
        for r in range(n):
            if r != i and a[i][i]:
                f = a[r][i] / a[i][i]
                a[r] = [u - f * v for u, v in zip(a[r], a[i])]
                b[r] -= f * b[i]
    return {k: b[i] / a[i][i] for i, k in enumerate(cols)}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(here, "recordings", "*.txt")))
    data = [d for p in paths for d in intervals(p)]
    if len(data) < 3:
        sys.exit("Not enough stats sheets with age changes in the recordings yet. "
                 "Play a while with Record and background stats on.")
    print(f"{len(data)} gaps between stats sheets in {len(paths)} recording(s)\n")
    print("  age   " + "  ".join(f"{r:>10}" for r in ROWS))
    for y, c in data[:15]:
        print(f"  +{y:<4} " + "  ".join(f"{c[r]:>10}" for r in ROWS))
    if len(data) > 15:
        print(f"  ... {len(data) - 15} more")
    print("\nAge each one costs (fitted):")
    for k, v in fit(data).items():
        print(f"  {k:>10}: {v:+.2f}")


if __name__ == "__main__":
    main()
