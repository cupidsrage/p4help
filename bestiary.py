#!/usr/bin/env python3
"""
Bestiary: simulated fights against every monster, with YOUR stats.

For each monster at sizes 1..MAX_SIZE it runs the advisor's own fight
simulation (the same one auto-play uses, including the corrections learned
from your fights in calibration.json) at full energy and mana, and records
the best opening move, win / death chance, energy and mana cost, and a
verdict. Then it re-runs everything with each stat raised a little to show
which stat makes you safest -- "what stats do what".

Writes bestiary.html (open in a browser) and bestiary.json.

    python bestiary.py                  # stats from the running launcher
    python bestiary.py --sizes 1-6      # only these sizes
    python bestiary.py --stats '{"strength":142,"brains":49,...}'

Runs as its own process so a long run doesn't slow the game down.
"""

import argparse
import html
import json
import os
import random
import sys
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                       else __file__))
sys.path.insert(0, HERE)

import calibrate as CAL_MOD                       # noqa: E402
import rules as RU                                # noqa: E402
import monsters as MDB                            # noqa: E402
from combat import Player, Monster                # noqa: E402
from planner import score_moves, FightState       # noqa: E402

STATE_URL = "http://127.0.0.1:8420/state"
TRIALS = 300
MAX_SIZE = 10

# "what stats do what": each stat raised by this much
BUMPS = {
    "strength": ("Strength +10%", lambda p: setattr(p, "strength", p.strength * 1.10)),
    "brains":   ("Brains +10%",   lambda p: setattr(p, "brains", p.brains * 1.10)),
    "quickness": ("Speed +3",     lambda p: setattr(p, "quickness", p.quickness + 3)),
    "magiclvl": ("Magic Level +3", lambda p: setattr(p, "magiclvl", p.magiclvl + 3)),
    "energy":   ("Max energy +10%", lambda p: (setattr(p, "max_energy", p.max_energy * 1.10),
                                               setattr(p, "energy", p.max_energy))),
    "sword":    ("Sword +5",      lambda p: setattr(p, "sword", p.sword + 5)),
}

VERDICTS = [  # (test, label, colour)
    (lambda r: r["death"] <= 0.005 and r["win"] >= 0.99 and r["energy_frac"] < 0.15
     and r["mana"] <= 10, "FREE KILL", "#2e7d32"),
    (lambda r: r["death"] <= 0.02, "SAFE", "#558b2f"),
    (lambda r: r["death"] <= 0.10, "RISKY", "#f9a825"),
    (lambda r: r["move"] == "evade", "FLEE", "#ef6c00"),
    (lambda r: True, "DEADLY", "#c62828"),
]


def player_from(stats):
    p = Player()
    for k in ("strength", "quickness", "brains", "magiclvl", "level", "sword",
              "shield", "max_energy", "mana"):
        if stats.get(k) is not None:
            setattr(p, k, float(stats[k]))
    p.ring = bool(stats.get("ring"))
    p.max_energy = float(stats.get("max_energy") or stats.get("energy") or 100)
    p.energy = p.max_energy                       # rested
    return p


def live_stats():
    with urllib.request.urlopen(STATE_URL, timeout=3) as r:
        return (json.loads(r.read()).get("player") or {})


def monster(name, size, cal):
    d = MDB.scaled(name, size)
    if not d:
        return None
    er, sr = cal.ratios(size) if cal else (1.0, 1.0)
    return Monster(name=d["name"], strength=d["strength"] * sr, brains=d["brains"],
                   speed=d["speed"], energy=d["energy"] * er, shield=0.0,
                   experience=d["experience"], specials=tuple(d["specials"]))


def fight(p, m):
    st = FightState.opening(p, m)
    st.pre_roll = True               # it may hit before your first move
    res = score_moves(p, m, st, trials=TRIALS)
    if not res:
        return None
    best = res[0]
    out = {"move": best["move"], "arg": best.get("arg"),
           "win": round(best["win"], 3), "flee": round(best["flee"], 3),
           "death": round(best.get("death", 0.0), 3),
           "energy": round(best["energy_lost"]), "mana": round(best.get("mana_spent", 0)),
           "energy_frac": best["energy_lost"] / max(1.0, p.max_energy)}
    for test, label, colour in VERDICTS:
        if test(out):
            out["verdict"], out["colour"] = label, colour
            break
    return out


def _run_one(args):
    """Worker: (stats, cal sizes, name, size, bump) -> result."""
    stats, cal_sizes, name, size, bump = args
    cal = CAL_MOD.Calibration(None)
    cal.sizes = cal_sizes
    p = player_from(stats)
    if bump:
        BUMPS[bump][1](p)
        p.energy = p.max_energy
    m = monster(name, size, cal)
    # Same dice for every stat variant of this monster/size, so a difference
    # comes from the stat, not from luck ("common random numbers").
    random.seed(f"{name}|{size}")
    r = fight(p, m) if m else None
    if r is not None:
        tt = MDB.MONSTERS[name][5]
        r["treasure"] = round(RU.treasure_drop_chance(tt, size, "U" in m.specials), 2)
        r["maps"] = round(RU.map_chance_per_kill(tt, size), 3)
    return (name, size, bump, r)


def build(stats, sizes, workers=None):
    cal = CAL_MOD.Calibration(os.path.join(HERE, "calibration.json"))
    jobs = [(stats, cal.sizes, n, s, None) for n in MDB.MONSTERS for s in sizes]
    jobs += [(stats, cal.sizes, n, s, b) for b in BUMPS for n in MDB.MONSTERS
             for s in sizes]
    table, bumped = {}, {b: {} for b in BUMPS}
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, (n, s, b, r) in enumerate(ex.map(_run_one, jobs, chunksize=8)):
            if r is None:
                continue
            (bumped[b] if b else table).setdefault(n, {})[s] = r
            if (i + 1) % 500 == 0:
                print(f"  {i + 1}/{len(jobs)} fights simulated "
                      f"({time.time() - t0:.0f} s)", flush=True)

    # what stats do what: how much safer each bump makes you overall
    def total_death(t):
        return sum(r["death"] for sz in t.values() for r in sz.values())
    def n_safe(t):
        return sum(1 for sz in t.values() for r in sz.values() if r["death"] <= 0.02)
    base_d, base_s = total_death(table), n_safe(table)
    effects = []
    for b, t in bumped.items():
        effects.append({"stat": BUMPS[b][0],
                        "fights_made_safe": n_safe(t) - base_s,
                        "death_cut": round(base_d - total_death(t), 2)})
    effects.sort(key=lambda e: (-e["fights_made_safe"], -e["death_cut"]))
    return {"stats": stats, "sizes": list(sizes), "table": table,
            "effects": effects, "made": time.strftime("%Y-%m-%d %H:%M"),
            "calibration": cal.summary()}


def write_html(data, path):
    sizes = data["sizes"]
    st = data["stats"]
    rows = []
    for name in sorted(data["table"]):
        cells = []
        for s in sizes:
            r = data["table"][name].get(s)
            if not r:
                cells.append("<td></td>")
                continue
            mv = r["move"] + (f" {r['arg']}" if r.get("arg") else "")
            tip = (f"win {r['win']*100:.0f}% · death {r['death']*100:.1f}% · "
                   f"-{r['energy']} en · {r['mana']} mana · "
                   f"treasure {r.get('treasure', 0)*100:.0f}%")
            if r.get("maps"):
                tip += f" · trove map {r['maps']*100:.0f}%"
            cells.append(f'<td style="background:{r["colour"]}" title="{html.escape(tip)}">'
                         f'<b>{html.escape(r["verdict"])}</b><br>{html.escape(mv)}'
                         f'<br><small>{r["death"]*100:.0f}% death</small></td>')
        mark = " 🗺" if MDB.MONSTERS[name][5] == RU.MAP_TREASURE_TYPE else ""
        rows.append(f"<tr><th>{html.escape(name)}{mark}</th>{''.join(cells)}</tr>")
    eff = "".join(f"<tr><td>{html.escape(e['stat'])}</td><td>{e['fights_made_safe']:+d}</td>"
                  f"<td>{e['death_cut']:+.2f}</td></tr>" for e in data["effects"])
    stat_line = " · ".join(f"{k} {st.get(k)}" for k in
                           ("level", "strength", "quickness", "brains", "magiclvl",
                            "max_energy", "mana", "sword", "shield") if st.get(k) is not None)
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Bestiary</title><style>
body{{background:#16130e;color:#e8dcc0;font:13px Segoe UI,sans-serif;margin:16px}}
table{{border-collapse:collapse}} td,th{{border:1px solid #4a3f2a;padding:4px 6px;text-align:center}}
th{{text-align:left;background:#211c14;position:sticky;left:0}}
thead th{{position:sticky;top:0;text-align:center}} td{{color:#fff;min-width:78px}}
small{{opacity:.85}} h2{{color:#d4b03a}} input{{margin:6px 0;padding:4px;width:240px}}
</style></head><body>
<h2>Bestiary</h2>
<p>Built {data['made']} from your stats: {html.escape(stat_line)} (full energy and mana).
Hover a cell for win / death / energy / mana / treasure chance. Monsters
marked 🗺 drop trove maps. Corrections from your own fights are
included where a size has enough of them.</p>
<h3>What stats do what</h3>
<p>Every fight re-simulated with one stat raised. "Fights made safe" = how many
monster/size combinations drop to 2% death risk or less.</p>
<table><tr><th>Stat raised</th><th>Fights made safe</th><th>Total death risk cut</th></tr>{eff}</table>
<h3>Every monster</h3>
<input id=q placeholder="filter monsters..." oninput="for(const r of document.querySelectorAll('#b tr'))r.style.display=r.textContent.toLowerCase().includes(this.value.toLowerCase())?'':'none'">
<table><thead><tr><th>Monster</th>{''.join(f'<th>size {s}</th>' for s in sizes)}</tr></thead>
<tbody id=b>{''.join(rows)}</tbody></table></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default=f"1-{MAX_SIZE}")
    ap.add_argument("--stats", help="JSON of player stats (default: running launcher)")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    lo, _, hi = a.sizes.partition("-")
    sizes = list(range(int(lo), int(hi or lo) + 1))
    stats = json.loads(a.stats) if a.stats else live_stats()
    if not stats.get("strength"):
        sys.exit("No character stats yet: log in (and open Info -> Stats once).")
    print(f"Simulating {len(MDB.MONSTERS)} monsters x {len(sizes)} sizes "
          f"x {1 + len(BUMPS)} stat variants...")
    data = build(stats, sizes)
    jpath = os.path.join(HERE, "bestiary.json")
    hpath = os.path.join(HERE, "bestiary.html")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in data.items()}, f, indent=0)
    write_html(data, hpath)
    print(f"wrote {hpath}")
    print("What stats do what:")
    for e in data["effects"]:
        print(f"  {e['stat']:18} {e['fights_made_safe']:+4d} fights made safe, "
              f"death risk cut {e['death_cut']:+.2f}")
    if not a.no_open:
        import webbrowser
        webbrowser.open("file://" + hpath)


if __name__ == "__main__":
    main()
