"""
Character progression — leveling and buying power.

All of this comes from the game's own data files and source, NOT guesses:

  data/charstats  -> per-class stat growth per level
  data/shopitems  -> what the trading post sells and base costs
  info.c:68       -> next level needs 1800 * (level+1)^2 experience
  stats.c:343     -> on level-up, each stat += increase * levels_gained
  commands.c:3990 -> blessing costs 500*(level+5) + 10000

CHARSTATS FORMAT (from init.c:511 fscanf):
    abbrev, max_brains, max_mana, weakness, gold_tote, ring_duration,
    then base/interval/increase for:
        quickness, strength, mana, energy, brains, magiclvl

So the "increase" column is what you gain PER LEVEL.
"""

import math

# ---------------------------------------------------------------- charstats
# name -> (max_brains, max_mana, weakness, gold_tote, ring_dur,
#          quick(b,i,inc), str(b,i,inc), mana(b,i,inc),
#          energy(b,i,inc), brains(b,i,inc), magic(b,i,inc))
CLASSES = {
    "Magic-User": dict(
        max_brains=12.0, max_mana=120.0, weakness=14.0, gold_tote=150.0,
        ring_dur=30,
        quickness=(29, 5, 0.0), strength=(10, 5, 1.0), mana=(50, 51, 80.0),
        energy=(40, 21, 20.0), brains=(40, 24, 4.5), magiclvl=(5, 6, 3.0)),
    "Fighter": dict(
        max_brains=4.0, max_mana=90.0, weakness=14.0, gold_tote=225.0,
        ring_dur=15,
        quickness=(33, 5, 0.0), strength=(24, 25, 5.0), mana=(30, 16, 35.0),
        energy=(50, 26, 25.0), brains=(10, 11, 2.0), magiclvl=(2, 3, 1.5)),
    "Elf": dict(
        max_brains=10.0, max_mana=110.0, weakness=14.0, gold_tote=125.0,
        ring_dur=25,
        quickness=(36, 5, 0.0), strength=(12, 10, 2.0), mana=(40, 36, 65.0),
        energy=(30, 21, 15.0), brains=(30, 21, 4.0), magiclvl=(4, 5, 2.5)),
    "Dwarf": dict(
        max_brains=8.0, max_mana=100.0, weakness=14.0, gold_tote=200.0,
        ring_dur=20,
        quickness=(26, 5, 0.0), strength=(20, 20, 4.0), mana=(35, 26, 50.0),
        energy=(70, 31, 35.0), brains=(20, 16, 3.0), magiclvl=(3, 4, 2.0)),
    "Halfling": dict(
        max_brains=18.0, max_mana=80.0, weakness=14.0, gold_tote=175.0,
        ring_dur=10,
        quickness=(31, 5, 0.0), strength=(16, 15, 3.0), mana=(25, 6, 20.0),
        energy=(60, 26, 30.0), brains=(50, 31, 6.0), magiclvl=(1, 2, 1.0)),
    "Experimento": dict(
        max_brains=6.0, max_mana=100.0, weakness=14.0, gold_tote=175.0,
        ring_dur=25,
        quickness=(33, 0, 0.0), strength=(25, 0, 1.0), mana=(40, 0, 20.0),
        energy=(60, 0, 15.0), brains=(40, 0, 2.0), magiclvl=(4, 0, 1.0)),
}

# ---------------------------------------------------------------- shopitems
# name -> base cost. (Actual cost also scales; blessing/amulet have formulas.)
SHOP = {
    "Mana": 1,
    "Shield": 5,
    "Book": 100,          # raises brains
    "Amulet": 250,
    "Sword": 500,
    "Quicksilver": 2000,
    "Blessing": 1000,
}


def exp_for_level(level):
    """Experience needed to reach the NEXT level (info.c:68)."""
    return 1800.0 * (level + 1) ** 2


def blessing_cost(level):
    """commands.c:3990"""
    return 500.0 * (level + 5.0) + 10000.0


def level_gains(cls, levels=1):
    """What you gain per level, straight from charstats."""
    c = CLASSES[cls]
    return {
        "quickness": c["quickness"][2] * levels,
        "strength": c["strength"][2] * levels,
        "mana": c["mana"][2] * levels,
        "energy": c["energy"][2] * levels,
        "brains": c["brains"][2] * levels,
        "magiclvl": c["magiclvl"][2] * levels,
    }


def project(player, cls, levels_ahead):
    """
    What will this character look like after gaining N levels?
    stats.c:343 -- each stat += increase * levels_gained.
    """
    from combat import Player
    g = level_gains(cls, levels_ahead)
    return Player(
        energy=player.max_energy + g["energy"],
        max_energy=player.max_energy + g["energy"],
        strength=player.strength + g["strength"],
        quickness=player.quickness + g["quickness"],
        mana=player.mana + g["mana"],
        magiclvl=player.magiclvl + g["magiclvl"],
        brains=player.brains + g["brains"],
        sword=player.sword,
        shield=player.shield,
        level=player.level + levels_ahead,
        ring=player.ring,
    )


def spell_unlocks(cls, current_ml, current_level):
    """When does this character unlock each spell?"""
    from combat import ML_BOLT, ML_INCRMIGHT, ML_HASTE, ML_FORCEFIELD
    ML_PARALYZE = 60.0
    gates = [
        ("Magic Bolt", ML_BOLT), ("Increase Might", ML_INCRMIGHT),
        ("Haste", ML_HASTE), ("Force Field", ML_FORCEFIELD),
        ("Paralyze", ML_PARALYZE),
    ]
    per = CLASSES[cls]["magiclvl"][2]
    out = []
    for name, gate in gates:
        if current_ml >= gate:
            out.append({"spell": name, "gate": gate, "have": True,
                        "levels_away": 0})
        elif per > 0:
            need = math.ceil((gate - current_ml) / per)
            out.append({"spell": name, "gate": gate, "have": False,
                        "levels_away": need,
                        "at_level": current_level + need})
    return out


def roadmap(player, cls, here=None, ahead=12):
    """
    Where should this character hunt now, and where will it be able to hunt
    as it levels? Uses the REAL growth numbers from charstats.
    """
    import zones as ZN

    rows = []
    for n in range(0, ahead + 1):
        p = project(player, cls, n) if n else player
        best, _ = ZN.recommend(p, measured=False)
        rows.append({
            "level": int(player.level + n),
            "levels_ahead": n,
            "energy": p.max_energy,
            "strength": p.strength,
            "brains": p.brains,
            "magiclvl": p.magiclvl,
            "mana": p.mana,
            "circle": best["circle"] if best else None,
            "exp_per_fight": best["exp_per_fight"] if best else 0,
            "survive": best["p_survive_session"] if best else 0,
            "exp_to_next": exp_for_level(player.level + n),
        })
    return rows
