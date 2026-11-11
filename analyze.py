#!/usr/bin/env python3
"""
Turn game recordings (recorder.py) into something auto-play can learn from.

    python analyze.py                    # all sessions in ./recordings
    python analyze.py recordings/session-20261111-120000.jsonl
    python analyze.py --raw              # also list raw client inputs

Prints:
  - how much was recorded (sessions, inputs, human vs auto-play)
  - the screens you answer most often, and which button you pick on each
  - in fights: how often YOUR move matched the advisor's top move, and where
    you disagreed (those disagreements are where the advisor can learn)
  - samples of raw client input, to confirm the input format

Writes recordings/policy.json:
    { "<prompt> | <buttons>": {"Melee": 12, "Luckout": 3, ...}, ... }
i.e. for each kind of screen, what a human pressed and how often. A smarter
auto-play can look up the current screen there before falling back to the
combat advisor.
"""

import glob
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REC = os.path.join(HERE, "recordings")

# advisor move -> combat button label it presses (mirrors buttons.py)
MOVE_LABEL = {"melee": "Melee", "skirmish": "Skirmish", "nick": "Nick",
              "luckout": "Luckout", "evade": "Evade", "ring": "Use Ring",
              "bolt": "Spell", "aon": "Spell", "might": "Spell",
              "paralyze": "Spell", "forcefield": "Spell"}


def screen_key(ev):
    sc = ev.get("screen") or {}
    prompt = (sc.get("prompt") or "").strip()
    btns = [b for b in (sc.get("buttons") or []) if b]
    return f"{prompt} | {', '.join(btns)}"


def load(paths):
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_raw = "--raw" in sys.argv
    paths = args or sorted(glob.glob(os.path.join(REC, "*.jsonl")))
    if not paths:
        print("No recordings yet. Turn on Record in the launcher (or run "
              "app.py --record) and play a while.")
        return

    sessions = len(paths)
    inputs = [e for e in load(paths) if e.get("kind") == "input"]
    by = Counter(e.get("by", "human") for e in inputs)
    human = [e for e in inputs if e.get("by", "human") == "human"]

    print(f"\n  {sessions} session(s), {len(inputs)} inputs "
          f"({by['human']} human, {by['autoplay']} auto-play)\n")

    # ---- what you press on each screen ----
    policy = defaultdict(Counter)
    unknown = 0
    for e in human:
        b = e.get("button")
        if b:
            policy[screen_key(e)][b["label"]] += 1
        else:
            unknown += 1

    print("  MOST COMMON SCREENS  (what you pressed)")
    ranked = sorted(policy.items(), key=lambda kv: -sum(kv[1].values()))
    for key, c in ranked[:20]:
        total = sum(c.values())
        picks = ", ".join(f"{lbl} {n}" for lbl, n in c.most_common(4))
        print(f"  {total:5d}  {key[:70]}")
        print(f"         -> {picks}")
    if unknown:
        print(f"\n  {unknown} human inputs weren't a recognisable button "
              "(typed text, or a different input format; see --raw)")

    # ---- fights: you vs the advisor ----
    agree = disagree = 0
    diffs = Counter()
    for e in human:
        f = e.get("fight") or {}
        adv = (f.get("advisor") or {}).get("move")
        b = e.get("button")
        if not adv or not b:
            continue
        sc = e.get("screen") or {}
        if "Melee" not in (sc.get("buttons") or []):
            continue   # not on the main combat menu
        want = MOVE_LABEL.get(adv)
        if want == b["label"]:
            agree += 1
        else:
            disagree += 1
            diffs[(f.get("name"), adv, b["label"])] += 1
    if agree + disagree:
        pct = 100 * agree / (agree + disagree)
        print(f"\n  FIGHTS: you matched the advisor {agree}/{agree + disagree} "
              f"times ({pct:.0f}%)")
        for (mon, adv, you), n in diffs.most_common(10):
            print(f"  {n:4d}x  vs {mon}: advisor said {adv}, you pressed {you}")

    if show_raw:
        print("\n  RAW CLIENT INPUT SAMPLES")
        seen = set()
        for e in inputs:
            r = tuple(e.get("raw") or [])
            if r in seen:
                continue
            seen.add(r)
            print(f"  {list(r)!r:40}  on: {screen_key(e)[:60]}")
            if len(seen) >= 25:
                break

    out = os.path.join(os.path.dirname(paths[0]) or REC, "policy.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({k: dict(v) for k, v in ranked}, f, indent=1)
    print(f"\n  wrote {out}\n")


if __name__ == "__main__":
    main()
