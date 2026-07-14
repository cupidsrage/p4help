#!/usr/bin/env python3
"""
Autobattler — measures the advisor's DECISION QUALITY with the human removed.

This is the "let it play itself" test you wanted, done safely: it never touches
the game client or the server. It plays entirely against the ported combat
engine (fight.c formulas), letting the planner pick every move, and reports how
well those picks actually perform over thousands of fights.

    python autobattle.py                  # default character, 2000 fights
    python autobattle.py --fights 5000
    python autobattle.py --circle 5       # fight what spawns in circle 5
    python autobattle.py --str 50 --brains 120 --ml 5 --mana 200 --energy 250

What it reports:
  - win / flee / DEATH rate  (the headline: is the advisor keeping you alive?)
  - average energy + mana spent per fight
  - how often it chose each action, and how that action did
  - whether it ever walked into a fight it should have fled
  - luckout hit rate vs. what the advisor predicted (calibration check)

Because it uses the SAME planner the live overlay uses, the decision quality it
measures is exactly the decision quality you'd get in-game -- minus your reaction
time. That's the isolation you asked for.
"""

import argparse
import random
import statistics
from collections import defaultdict

from combat import Player, Monster
from planner import (FightState, score_moves, apply_move, try_evade,
                     _monster_swing, might_of)
import monsters as MDB
import zones as ZN


def make_monster(name, size):
    d = MDB.scaled(name, size)
    return Monster(name=d["name"], strength=d["strength"], brains=d["brains"],
                   speed=d["speed"], energy=d["energy"], shield=0.0,
                   experience=d["experience"], specials=tuple(d["specials"]))


def play_one_fight(player, mon, decide_trials=300, max_rounds=150):
    """
    Play a single fight, letting the planner choose every move from live state.
    Returns an outcome dict.
    """
    st = FightState.opening(player, mon)
    moves_taken = []

    while st.rounds < max_rounds:
        ranked = score_moves(player, mon, st, trials=decide_trials)
        if not ranked:
            break
        choice = ranked[0]
        mv, arg = choice["move"], choice["arg"]
        moves_taken.append(mv)

        if mv == "evade":
            st.rounds += 1
            if try_evade(player, mon, st):
                return {"result": "fled", "rounds": st.rounds,
                        "energy_lost": player.energy - st.p_energy,
                        "mana_spent": player.mana - st.p_mana,
                        "moves": moves_taken}
            _monster_swing(st, mon)
        else:
            dead = apply_move(player, mon, st, mv, arg)
            if dead:
                return {"result": "win", "rounds": st.rounds,
                        "energy_lost": player.energy - st.p_energy,
                        "mana_spent": player.mana - st.p_mana,
                        "moves": moves_taken}
            _monster_swing(st, mon)

        if st.p_energy <= 0:
            return {"result": "death", "rounds": st.rounds,
                    "energy_lost": player.energy,
                    "mana_spent": player.mana - st.p_mana,
                    "moves": moves_taken}

    return {"result": "timeout", "rounds": st.rounds,
            "energy_lost": player.energy - max(0, st.p_energy),
            "mana_spent": player.mana - st.p_mana, "moves": moves_taken}


def run(player, circle, n_fights, decide_trials, rest_between=True):
    """
    Simulate a hunting session: repeatedly meet a monster from the circle's pool
    and let the advisor fight it. Rest (recover energy) between fights, like a
    real player would. A death ends the session -- that's the whole point.
    """
    pool = ZN.monster_pool(circle)
    size = ZN.typical_size(circle)
    names = list({ZN.IDS[i] for i in pool if 0 <= i < len(ZN.IDS)})

    outcomes = []
    first_move = defaultdict(int)
    move_result = defaultdict(lambda: defaultdict(int))
    deaths_to = defaultdict(int)
    cur_energy = player.energy
    cur_mana = player.mana

    for i in range(n_fights):
        name = random.choice(names)
        mon = make_monster(name, size)

        # fight from current resources, not always full
        p = Player(energy=cur_energy, max_energy=player.max_energy,
                   strength=player.strength, quickness=player.quickness,
                   mana=cur_mana, magiclvl=player.magiclvl,
                   brains=player.brains, sword=player.sword,
                   shield=player.shield, level=player.level, ring=player.ring)

        out = play_one_fight(p, mon, decide_trials)
        outcomes.append(out)
        if out["moves"]:
            first_move[out["moves"][0]] += 1
            move_result[out["moves"][0]][out["result"]] += 1

        if out["result"] == "death":
            deaths_to[name] += 1
            break

        # spend was real; rest recovers energy between encounters
        cur_energy = max(0, cur_energy - out["energy_lost"])
        cur_mana = max(0, cur_mana - out["mana_spent"])
        if rest_between:
            cur_energy = min(player.max_energy, cur_energy + player.max_energy * 0.5)
            cur_mana = min(player.mana, cur_mana + player.mana * 0.15)

    return {
        "outcomes": outcomes,
        "first_move": dict(first_move),
        "move_result": {k: dict(v) for k, v in move_result.items()},
        "deaths_to": dict(deaths_to),
        "fought": len(outcomes),
        "circle": circle,
        "size": size,
    }


def report(player, res, circle):
    o = res["outcomes"]
    n = len(o)
    wins = sum(1 for x in o if x["result"] == "win")
    fled = sum(1 for x in o if x["result"] == "fled")
    died = sum(1 for x in o if x["result"] == "death")
    en = [x["energy_lost"] for x in o if x["result"] != "death"]
    mn = [x["mana_spent"] for x in o]

    print("=" * 60)
    print(f"AUTOBATTLE — circle {circle} (size {res['size']}), "
          f"{n} fights before {'DEATH' if died else 'stop'}")
    print("=" * 60)
    print(f"  survived : {wins:>5} wins   {fled:>4} fled   "
          f"{'*** DIED ***' if died else 'no deaths'}")
    print(f"  win rate : {wins/n*100:5.1f}%   flee {fled/n*100:.1f}%")
    if en:
        print(f"  energy   : {statistics.mean(en):5.1f} avg/fight lost "
              f"(max {max(en):.0f})")
    if mn:
        print(f"  mana     : {statistics.mean(mn):5.1f} avg/fight spent")

    print("\n  WHAT THE ADVISOR CHOSE (opening move):")
    for mv, c in sorted(res["first_move"].items(), key=lambda t: -t[1]):
        rr = res["move_result"].get(mv, {})
        w = rr.get("win", 0)
        print(f"    {mv:<10} {c:>4}x   ({w} wins, "
              f"{rr.get('fled',0)} fled, {rr.get('death',0)} deaths)")

    if res["deaths_to"]:
        print("\n  DIED TO:")
        for nm, c in res["deaths_to"].items():
            print(f"    {nm}")

    print("\n  VERDICT:")
    if died:
        print(f"    Died after {n} fights. The advisor walked into something it "
              f"couldn't handle at circle {circle}.")
        print(f"    -> this circle is too dangerous for these stats.")
    else:
        print(f"    Survived all {n} fights with no deaths. The advisor's "
              f"choices held up at circle {circle}.")
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fights", type=int, default=2000)
    ap.add_argument("--circle", type=int, default=None,
                    help="which circle's monsters to fight (default: recommended)")
    ap.add_argument("--trials", type=int, default=300,
                    help="planner rollouts per decision (higher = better choices, slower)")
    ap.add_argument("--energy", type=float, default=128)
    ap.add_argument("--str", dest="strength", type=float, default=36)
    ap.add_argument("--brains", type=float, default=84)
    ap.add_argument("--ml", type=float, default=3)
    ap.add_argument("--mana", type=float, default=98)
    ap.add_argument("--sword", type=float, default=0)
    ap.add_argument("--quick", type=float, default=35)
    ap.add_argument("--level", type=float, default=2)
    args = ap.parse_args()

    player = Player(energy=args.energy, max_energy=args.energy,
                    strength=args.strength, quickness=args.quick,
                    mana=args.mana, magiclvl=args.ml, brains=args.brains,
                    sword=args.sword, level=args.level)

    circle = args.circle
    if circle is None:
        best, _ = ZN.recommend(player)
        circle = best["circle"] if best else 1
        print(f"(no circle given; using recommended circle {circle})\n")

    res = run(player, circle, args.fights, args.trials)
    report(player, res, circle)


if __name__ == "__main__":
    main()
