"""
The trading post — what to buy, ported from commands.c:Do_trading_post()
and stats.c:Do_book().

POST SIZE (commands.c:3979):
    size = sqrt(|x| / 100) + 1,  capped at 6
    (size 6 only in the Cracks of Doom becomes 7 -> Blessing available)

The post only offers the FIRST `size` items. So WHERE you shop decides WHAT you
can buy:

    idx  item          base cost   needs |x| >=
    0    Mana                  1        0
    1    Shield                5      100
    2    Book                100      400
    3    Amulet         250+lvl/5     900
    4    Sword               500     1600
    5    Quicksilver        2000     2500
    6    Blessing    500*lvl+12500   Cracks of Doom only

STAT EFFECTS:
    Mana        +1 mana per gold
    Shield      +1 shield per 5 gold (absorbs damage before energy)
    Book        brains, with a soft cap -- see book_gain()
    Sword       +N sword (melee scales as sqrt(sword)*0.04 -- weak!)
    Quicksilver +N quicksilver (raises quickness)
    Blessing    first hit in combat; REQUIRED for the Dark Lord

BOOK MECHANIC (stats.c:1566) -- the important one:
    soft_cap = level * max_brains          (Halfling max_brains = 18)
    ftemp    = brains - soft_cap
    if ftemp <= 0:  brains += brains_increase / 2      (full value)
    else:           brains += d2 / sqrt(ftemp + d2)    (decaying)
                    where d2 = inc*(inc-1) / ceil(ftemp/soft_cap)

So books are BEST when your brains are below level*max_brains, and decay
(but never to zero) above it.
"""

import math

ITEMS = [
    ("Mana", 1, 0),
    ("Shield", 5, 100),
    ("Book", 100, 400),
    ("Amulet", 250, 900),
    ("Sword", 500, 1600),
    ("Quicksilver", 2000, 2500),
    ("Blessing", None, None),     # Cracks of Doom only
]


def post_size(x):
    """commands.c:3979 — how many items this post offers."""
    size = int(math.sqrt(abs(x) / 100) + 1)
    return min(6, size)


def available(x):
    """Which items you can actually buy at this location."""
    n = post_size(x)
    return [ITEMS[i][0] for i in range(min(n, len(ITEMS)))]


def x_needed_for(item):
    """How far out (|x|) you must be for this item to be offered."""
    for name, _, need in ITEMS:
        if name == item and need is not None:
            return need
    return None


def blessing_cost(level):
    """commands.c:3990"""
    return 500.0 * (level + 5.0) + 10000.0


def amulet_cost(level):
    """commands.c:3991"""
    return 250.0 + math.floor(level / 5)


def book_gain(brains, level, max_brains, brains_increase, n=1):
    """
    Exactly stats.c:Do_book(). Returns the new brains after buying n books.
    """
    b = float(brains)
    for _ in range(int(n)):
        soft_cap = level * max_brains
        ftemp = b - soft_cap
        if soft_cap <= 0:
            b += brains_increase / 2
            continue
        multiple = math.ceil(ftemp / soft_cap)
        d1 = brains_increase
        if ftemp > 0:
            d2 = d1 * (d1 - 1) / multiple if multiple > 0 else d1 * (d1 - 1)
            if d2 <= 0:
                continue
            b += d2 / math.sqrt(ftemp + d2)
        else:
            b += d1 / 2
    return b


def shopping_advice(player, cls_name, x, gold, measured=False):
    """
    What should this character spend gold on, right here, right now?

    NOTE ON SCORING: the weights below are heuristic. For ground truth, pass
    measured=True -- it runs the autobattler and reports what each purchase
    ACTUALLY does to your survival, which is the only number that matters.
    (Measured runs showed sword beating books at low level, which the heuristic
    got wrong -- melee damage compounds because shorter fights mean fewer hits
    taken. Trust the measurement over the heuristic.)
    """
    if measured:
        return _measure_purchases(player, cls_name, x, gold)
    from progress import CLASSES
    c = CLASSES[cls_name]
    max_brains = c["max_brains"]
    binc = c["brains"][2]

    can = available(x)
    out = []

    # --- Book: brains. The big one for luckout builds. ---
    if "Book" in can and gold >= 100:
        n = int(gold // 100)
        after = book_gain(player.brains, player.level, max_brains, binc, n)
        gain = after - player.brains
        soft_cap = player.level * max_brains
        out.append({
            "item": "Book", "cost": 100, "buy": n, "spend": n * 100,
            "effect": f"brains {player.brains:.0f} -> {after:.0f} (+{gain:.0f})",
            "per_gold": gain / (n * 100) if n else 0,
            "note": ("below soft cap - FULL value" if player.brains < soft_cap
                     else f"above soft cap ({soft_cap:.0f}) - decaying but still good"),
            "score": gain * 3.0,      # brains drive luckout: weight heavily
        })

    # --- Mana ---
    if "Mana" in can and gold >= 1:
        n = int(gold)
        out.append({
            "item": "Mana", "cost": 1, "buy": n, "spend": n,
            "effect": f"mana {player.mana:.0f} -> {player.mana + n:.0f}",
            "per_gold": 1.0,
            "note": ("useless until ML 5 unlocks Magic Bolt"
                     if player.magiclvl < 5 else "bolt fuel"),
            "score": n * (0.05 if player.magiclvl < 5 else 0.4),
        })

    # --- Shield: straight damage soak ---
    if "Shield" in can and gold >= 5:
        n = int(gold // 5)
        out.append({
            "item": "Shield", "cost": 5, "buy": n, "spend": n * 5,
            "effect": f"shield +{n} (absorbs {n} damage before energy)",
            "per_gold": 0.2,
            "note": "eats damage before your health does",
            "score": n * 0.5,
        })

    # --- Sword: weak, sqrt scaling ---
    if "Sword" in can and gold >= 500:
        n = int(gold // 500)
        cur = player.sword
        new = cur + n
        old_mult = 1 + math.sqrt(max(0, cur)) * 0.04
        new_mult = 1 + math.sqrt(max(0, new)) * 0.04
        dmg_gain = (new_mult / old_mult - 1) * 100
        out.append({
            "item": "Sword", "cost": 500, "buy": n, "spend": n * 500,
            "effect": f"sword {cur:.0f} -> {new:.0f} (+{dmg_gain:.0f}% melee damage)",
            "per_gold": dmg_gain / (n * 500) if n else 0,
            "note": "scales as sqrt(sword)*0.04 - sharply diminishing",
            "score": dmg_gain * 0.3,
        })

    # --- Blessing ---
    if "Blessing" in can:
        bc = blessing_cost(player.level)
        if gold >= bc:
            out.append({
                "item": "Blessing", "cost": bc, "buy": 1, "spend": bc,
                "effect": "first hit in every fight; REQUIRED for the Dark Lord",
                "per_gold": 0,
                "note": "endgame requirement",
                "score": 500,
            })

    out.sort(key=lambda r: -r["score"])
    return out


def _measure_purchases(player, cls_name, x, gold, circle=None, sessions=20):
    """
    Ground truth: buy the thing, then actually play sessions and count deaths.
    Slow, but honest -- and it caught the heuristic being wrong about swords.
    """
    from autobattle import run
    from progress import CLASSES
    import zones as ZN
    from combat import Player as P

    c = CLASSES[cls_name]
    if circle is None:
        best, _ = ZN.recommend(player, measured=False)
        circle = best["circle"] if best else 1

    def survive(p):
        ok = 0
        for _ in range(sessions):
            r = run(p, circle, 20, decide_trials=70)
            if not any(o["result"] == "death" for o in r["outcomes"]):
                ok += 1
        return ok / sessions

    def clone(**kw):
        base = dict(
            energy=player.max_energy, max_energy=player.max_energy,
            strength=player.strength, quickness=player.quickness,
            mana=player.mana, magiclvl=player.magiclvl, brains=player.brains,
            sword=player.sword, shield=player.shield, level=player.level,
            ring=player.ring)
        base.update(kw)
        return P(**base)

    can = available(x)
    base_sv = survive(clone())
    out = []

    if "Book" in can and gold >= 100:
        n = int(gold // 100)
        nb = book_gain(player.brains, player.level, c["max_brains"],
                       c["brains"][2], n)
        out.append({"item": "Book", "buy": n, "spend": n * 100,
                    "effect": f"brains {player.brains:.0f} -> {nb:.0f}",
                    "survive": survive(clone(brains=nb))})

    if "Sword" in can and gold >= 500:
        n = int(gold // 500)
        out.append({"item": "Sword", "buy": n, "spend": n * 500,
                    "effect": f"sword {player.sword:.0f} -> {player.sword + n:.0f}",
                    "survive": survive(clone(sword=player.sword + n))})

    if "Shield" in can and gold >= 5:
        n = int(gold // 5)
        out.append({"item": "Shield", "buy": n, "spend": n * 5,
                    "effect": f"shield +{n}",
                    "survive": survive(clone(shield=n))})

    if "Mana" in can and gold >= 1:
        n = int(gold)
        out.append({"item": "Mana", "buy": n, "spend": n,
                    "effect": f"mana +{n}",
                    "survive": survive(clone(mana=player.mana + n))})

    for r in out:
        r["baseline"] = base_sv
        r["delta"] = r["survive"] - base_sv
    out.sort(key=lambda r: -r["delta"])
    return out
