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
energy/brains/experience are multiplied by size, and strength by 1+size/2.

Each circle is scored by simulating encounters with every monster that
spawns there, with the advisor's fight engine (planner.py), including
friends joining after a kill (flock %).
"""

import math
import time
import monsters as MDB
from combat import Player, Monster
from planner import FightState, score_moves, danger_of   # noqa: F401

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


SIM_TRIALS = 100        # simulated encounters per monster per circle
SESSION = 20            # "survive 20": encounters in a hunting session
SAFE_DEATH = 0.02       # above this, auto-play starts spending mana
MAX_CHAIN = 8           # most friends that can join one encounter here
_CACHE = {}


def _auto_policy(use_mana):
    """Roughly what auto-play does: luckout first, then melee; when melee is
    losing the race, bolt it dead (if allowed and affordable) or run."""
    from planner import might_of, hits_per_turn, _bolt_to_kill

    def pick(player, mon, st):
        if not st.luckout_spent and "Mo" not in mon.specials:
            return "luckout", None
        swings = (st.m_energy + st.m_shield) / max(1.0, might_of(player, st) * 1.15)
        incoming = (1.0 + st.m_strength / 2.0) * hits_per_turn(player, mon, st)
        if incoming * swings > st.p_energy * 0.8:
            if use_mana:
                n = _bolt_to_kill(player, st, mon)
                if n and n <= st.p_mana:
                    return "bolt", n
            return "evade", None
        return "melee", None
    return pick


def _fight(player, mon, st, pick):
    """Play one fight out. Returns 'win', 'flee', 'gone' or 'death'."""
    from planner import apply_move, try_evade, _monster_turns, _outcome
    out = _outcome(st)
    while out is None:
        mv, arg = pick(player, mon, st)
        if mv == "evade":
            st.rounds += 1
            if try_evade(player, mon, st):
                return "flee"
        else:
            apply_move(player, mon, st, mv, arg)
        out = _outcome(st)
        if out is None:
            _monster_turns(player, mon, st)
            out = _outcome(st)
    return out


def simulate_encounter(player, mon, flock, use_mana, trials=SIM_TRIALS):
    """Simulated encounters with one monster, from full energy, the way the
    server runs them: it may hit before your first move, and after every
    kill another one joins with its flock chance (fight.c), fought on the
    energy you have left. Returns death / flee chance, exp, energy lost and
    fights per encounter."""
    import random
    from planner import FightState, start_trial
    pick = _auto_policy(use_mana)
    deaths = fled = 0
    xp = lost = fights = 0.0
    for _ in range(trials):
        energy, mana, chain = player.energy, player.mana, 0
        while True:
            st = FightState.opening(player, mon)
            st.p_energy, st.p_mana, st.pre_roll = energy, mana, True
            st = start_trial(player, mon, st)
            res = "death" if st.p_energy <= 0 else _fight(player, mon, st, pick)
            energy, mana = st.p_energy, st.p_mana
            fights += 1
            if res == "death":
                deaths += 1
                break
            if res in ("flee", "gone"):  # it's gone: no experience, no friends
                fled += 1
                break
            xp += mon.experience
            chain += 1
            if chain >= MAX_CHAIN or random.random() >= flock / 100.0:
                break
        lost += player.energy - max(0.0, energy)
    return {"death": deaths / trials, "flee": fled / trials, "exp": xp / trials,
            "lost": lost / trials, "fights": fights / trials}


def evaluate_circle(player, circle, sample=None):
    """
    Score a circle for this player by SIMULATING encounters with every
    monster that spawns there (weighted by how often each spawns), using the
    same fight engine as the advisor. A monster that's risky without mana is
    re-simulated with Magic Bolt allowed, like auto-play would play it.
    """
    import random
    import rules as RU
    from collections import Counter

    pool = monster_pool(circle)
    if not pool:
        return None
    size = typical_size(circle)
    key = (round(player.max_energy, -1), round(player.strength), round(player.quickness, 1),
           round(player.brains), round(player.magiclvl), round(player.mana, -1),
           player.sword, round(player.level), getattr(player, "blessing", False), circle)
    if key in _CACHE:
        return dict(_CACHE[key])

    full = Player(**{**vars(player), "energy": player.max_energy})
    state = random.getstate()
    random.seed(f"zone|{circle}")          # same dice every time: stable advice
    weights = Counter(pool)
    tot_w = sum(weights.values())
    p_death = p_flee = exp = lost = fights = 0.0
    deadly, monsters, lethal_w = [], 0, 0.0
    for mid, w in weights.items():
        nm = IDS[mid]
        d = MDB.scaled(nm, size)
        if not d:
            continue
        m = Monster(name=d["name"], strength=d["strength"], brains=d["brains"],
                    speed=d["speed"], energy=d["energy"], shield=0.0,
                    experience=d["experience"], specials=tuple(d["specials"]),
                    size=d["size"], treasure=d["treasure"])
        time.sleep(0.001)      # runs on a background thread: let the proxy in
        r = simulate_encounter(full, m, d["flock"], use_mana=False)
        if r["death"] > SAFE_DEATH and full.magiclvl >= 5 and full.mana > 0:
            r2 = simulate_encounter(full, m, d["flock"], use_mana=True)
            if r2["death"] < r["death"]:
                r = r2
        f = w / tot_w
        monsters += 1
        p_death += f * r["death"]
        p_flee += f * r["flee"]
        exp += f * r["exp"]
        lost += f * r["lost"]
        fights += f * r["fights"]
        if r["death"] > 0.05:
            lethal_w += f
            deadly.append((nm, r["death"]))
    random.setstate(state)
    if not monsters:
        return None

    # turns an encounter costs: the Hunt, each fight, and the rests to heal
    gain = max(1.0, RU.rest_gain(full.max_energy, full.level))
    turns = 1.0 + fights + lost / gain
    p_survive_session = (1.0 - p_death) ** SESSION
    exp_per_turn = exp / turns
    deadly.sort(key=lambda t: -t[1])
    out = {
        "circle": circle,
        "distance": distance_for_circle(circle),
        "size": size,
        "monsters": monsters,
        "avg_danger": p_death,
        "p_death_per_encounter": p_death,
        "p_flee": p_flee,
        "avg_exp": exp,
        "exp_per_fight": exp / max(1e-9, fights),
        "exp_per_turn": exp_per_turn,
        "turns_per_fight": turns,
        "lethal_frac": lethal_w,
        "p_survive_session": p_survive_session,
        "score": exp_per_turn * SESSION * p_survive_session,
        "deadly": [n for n, _ in deadly[:3]],
        "zone": zone_name(circle),
    }
    if len(_CACHE) > 400:
        _CACHE.clear()
    _CACHE[key] = out
    return dict(out)


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
    dead_run = 0
    for c in range(1, max_circle + 1):
        if dead_run >= 2 and c <= 19:
            continue        # two circles in a row you can't survive: deeper
                            # normal circles only get worse (the marshes and
                            # beyond have their own monsters, so check those)
        r = evaluate_circle(player, c)
        if r:
            rows.append(r)
            dead_run = dead_run + 1 if r["p_survive_session"] < 0.01 else 0

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
