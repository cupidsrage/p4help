"""
Where should I be fighting?

In Phantasia the map IS the difficulty curve. Your CIRCLE is derived from your
distance from the origin:

    distance = sqrt(x^2 + y^2)
    circle   = floor(distance / 125) + 1        [D_CIRCLE = 125]

and circle decides which monsters spawn AND how big they are. All ported from
Do_monster() in fight.c.

MONSTER SELECTION BY CIRCLE (fight.c:80-130):

    circle 1-3    ROLL(14, 17 + circle*4)      weak stuff, low ids
    circle 4      ROLL(14, 38)
    circle 5-7    ROLL(14, 46)
    circle 8-9    ROLL(14, 18 + circle*4)
    circle 10-15  ROLL(0, -8 + circle*4) + ROLL(14, 26)   TT1-8, mid-weighted
    circle 16-19  ROLL(0, 50) + ROLL(14, 37)              all non-water, mid-weighted
    circle 20-24  ROLL(0, 17)               THE MARSHES - water monsters only
    circle 25,29,30  ROLL(50,25)+ROLL(0,26) GORGOROTH - nothing weak
    circle 26-28  ROLL(50, 50)              CRACKS OF DOOM - endgame only
    circle 31+    (see source; deep endgame)

ROLL(base, interval) = base + interval*rnd(), so it's a uniform range.

SIZE scales with circle too, which is what really kills you -- a monster's
energy/brains/experience are multiplied by size, and strength by 1+0.5*(size-1).
"""

import math
import monsters as MDB
from combat import Player, Monster
from planner import FightState, score_moves, danger_of

D_CIRCLE = 125.0

# id -> name, in table order (index == monster id from fight.c)
IDS = list(MDB.MONSTERS.keys())


def circle_of(x, y):
    """Your circle, from your coordinates."""
    dist = math.sqrt(x * x + y * y)
    return int(math.floor(dist / D_CIRCLE) + 1)


def distance_for_circle(c):
    """Minimum distance from origin to be in circle c."""
    return (c - 1) * D_CIRCLE


def monster_pool(circle):
    """
    Which monster ids can spawn in this circle, per Do_monster().
    Returns a list of ids (may repeat to reflect weighting).
    """
    c = circle
    pool = []

    def rng(base, interval):
        lo = int(base)
        hi = int(base + interval)
        return list(range(max(0, lo), min(101, hi + 1)))

    if 26 <= c <= 28:
        # cracks of doom: ROLL(50,50), anything under 52 becomes Modnar(15)
        pool = [i if i >= 52 else 15 for i in rng(50, 50)]
    elif 25 <= c <= 30:
        # gorgoroth: ROLL(50,25) + ROLL(0,26), <52 -> Modnar
        pool = [i if i >= 52 else 15 for i in rng(50, 51)]
    elif c > 19:
        # the marshes: water monsters + idiots + modnar
        pool = rng(0, 17)
    elif c > 15:
        pool = rng(14, 73)          # ROLL(0,50)+ROLL(14,37) spans ~14..101
    elif c > 9:
        hi = -8 + c * 4
        pool = rng(14, 26 + hi)
    elif c > 7:
        pool = rng(14, 18 + c * 4)
    elif c > 4:
        pool = rng(14, 46)
    elif c == 4:
        pool = rng(14, 38)
    else:
        pool = rng(14, 17 + c * 4)

    return [i for i in pool if 0 <= i < len(IDS)]


def typical_size(circle):
    """
    Monster size EQUALS circle in the normal zones. Straight from fight.c:

        c->battle.opponent->size = c->player.circle;

    (Only endgame circles with a ring override this.) My earlier round(circle*0.9)
    was a guess and it made every zone look safer than it is -- confirmed against
    a real capture at circle 3 where every monster was size 3.
    """
    return max(1, circle)


def evaluate_circle(player, circle, sample=None):
    """
    Score a circle for this player: how dangerous, how rewarding, how survivable.
    """
    pool = monster_pool(circle)
    if not pool:
        return None
    size = typical_size(circle)

    seen = {}
    for mid in pool:
        nm = IDS[mid]
        if nm in seen:
            continue
        seen[nm] = True

    dangers, exps, wins, deadly = [], [], [], []

    for nm in seen:
        d = MDB.scaled(nm, size)
        if not d:
            continue
        m = Monster(name=d["name"], strength=d["strength"], brains=d["brains"],
                    speed=d["speed"], energy=d["energy"], shield=0.0,
                    experience=d["experience"], specials=tuple(d["specials"]))
        st = FightState.opening(player, m)
        dg = danger_of(player, m, st)
        dangers.append(dg)
        exps.append(d["experience"])
        if dg > 0.75:
            deadly.append((nm, dg))

    if not dangers:
        return None

    avg_danger = sum(dangers) / len(dangers)
    # "lethal" now means genuinely threatening (>0.45), not just near-certain
    # death (>0.75). A monster at danger 0.5 still kills you often enough to end
    # a session -- the autobattler proved this by dying to a Killmoulis the old
    # threshold rated safe.
    lethal_frac = len([d for d in dangers if d > 0.45]) / len(dangers)
    worst_danger = max(dangers)

    # ---- EXPECTED experience, not average ----
    # The naive average is dragged UP by the very monsters that kill you (Ogres
    # and Jubjub Birds have huge exp). You don't bank exp from a fight you die
    # in. So weight each monster's exp by the chance you actually survive it.
    exp_earned = 0.0
    for dg, xp in zip(dangers, exps):
        p_survive = max(0.0, 1.0 - dg)
        exp_earned += xp * p_survive
    exp_per_fight = exp_earned / len(exps)

    # raw average, kept for display only
    avg_exp = sum(exps) / len(exps)

    # ---- SURVIVAL OVER A SESSION ----
    # This is the thing the old formula missed entirely. You don't fight once,
    # you fight many times, and death is ABSORBING -- one bad roll ends the run.
    # At 13% lethal, surviving 20 fights is 0.87^20 = 6%. That is not a
    # "survivable" zone, it is a coin-flip you lose.
    SESSION = 20
    # Per-fight death risk. danger_of() reports risk at CURRENT health; a fresh
    # fight from full is much safer than that number suggests, because you rest
    # between fights. So the real per-fight death chance is only a fraction of
    # raw danger -- but the tail matters, so we keep a worst-case term small.
    mean_danger = sum(dangers) / len(dangers)
    # empirically (from autobattle) a monster at danger d kills you from full
    # roughly d^2 of the time -- squaring reflects that you start each fight rested
    p_death_per_fight = sum(d * d for d in dangers) / len(dangers)
    p_death_per_fight = 0.8 * p_death_per_fight + 0.2 * (worst_danger ** 2)
    p_survive_session = (1.0 - p_death_per_fight) ** SESSION

    # ---- score ----
    # Expected exp banked over a session, given you might not finish it.
    # Dying is catastrophic: it costs a life and everything you were carrying.
    score = exp_per_fight * SESSION * p_survive_session

    deadly.sort(key=lambda t: -t[1])

    return {
        "circle": circle,
        "distance": distance_for_circle(circle),
        "size": size,
        "monsters": len(seen),
        "avg_danger": avg_danger,
        "avg_exp": avg_exp,
        "exp_per_fight": exp_per_fight,
        "lethal_frac": lethal_frac,
        "p_survive_session": p_survive_session,
        "score": score,
        "deadly": [n for n, _ in deadly[:3]],
        "zone": zone_name(circle),
    }


def zone_name(c):
    if 26 <= c <= 28:
        return "The Cracks of Doom"
    if 25 <= c <= 30:
        return "Gorgoroth"
    if c > 19:
        return "The Marshes"
    if c > 15:
        return "Outer wilds"
    if c > 9:
        return "Mid wilds"
    if c > 4:
        return "Near wilds"
    return "Home circles"


def measure_survival(player, circle, sessions=30, session_len=20):
    """
    Ground-truth survival: actually play sessions with the planner and count
    how many finish alive. Slower than the analytic estimate but honest.
    Imported lazily to avoid a circular import with the planner.
    """
    from autobattle import run
    survived = 0
    for _ in range(sessions):
        res = run(player, circle, session_len, decide_trials=80)
        if not any(o["result"] == "death" for o in res["outcomes"]):
            survived += 1
    return survived / sessions


def recommend(player, here=None, max_circle=32, measured=False):
    """
    Rank every circle for this player. Returns (best, all_rows).
    """
    rows = []
    for c in range(1, max_circle + 1):
        r = evaluate_circle(player, c)
        if r:
            # analytic estimate is a rough prior; replace with measured survival
            # for the circles that matter (cheap: only near the likely answer)
            rows.append(r)

    if measured:
        # The analytic p_survive_session is unreliable, so MEASURE it. Walk
        # outward from circle 1 measuring real survival, and stop once a circle
        # drops below ~15% (everything past it is worse). This anchors the
        # recommendation in simulated reality instead of a bad formula.
        cur_c = circle_of(*here) if here else 1
        start = 1
        stop_at = None
        for r in rows:
            c = r["circle"]
            # always measure through at least the current circle + a few beyond
            if c < start:
                continue
            sv = measure_survival(player, c, sessions=24)
            r["p_survive_session"] = sv
            r["measured"] = True
            if sv < 0.15 and c > max(cur_c, 2):
                stop_at = c
                break
        # circles we didn't measure and are past the cliff: mark unsafe
        if stop_at is not None:
            for r in rows:
                if r["circle"] > stop_at:
                    r["p_survive_session"] = 0.0

    if here:
        cur = circle_of(*here)
        for r in rows:
            r["current"] = (r["circle"] == cur)

    # Best = highest expected exp among circles you will actually SURVIVE.
    # The old gate allowed lethal_frac < 0.35, which is insane: a 35% chance per
    # fight of meeting something that kills you means you die within a few
    # fights, guaranteed. Require a real chance of finishing a session intact.
    # Best = the DEEPEST circle you can still reliably survive (>=85%), because
    # deeper = more exp. Not the highest "score" (that formula was unreliable);
    # just push out as far as survival allows.
    safe = [r for r in rows if r["p_survive_session"] >= 0.85]
    if not safe:
        safe = [r for r in rows if r["p_survive_session"] >= 0.60]
    best = max(safe, key=lambda r: r["circle"]) if safe else None

    # never recommend moving OUT into somewhere you're likely to die
    if best and here:
        cur = circle_of(*here)
        curr = next((r for r in rows if r["circle"] == cur), None)
        if (curr and best["circle"] > cur
                and best["p_survive_session"] < 0.85
                and curr["p_survive_session"] > best["p_survive_session"]):
            best = curr     # stay put

    return best, rows
