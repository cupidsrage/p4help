"""
Game rules ported from the Phantasia server source (cupidsrage/phantasia,
phantasia/src), outside of combat. Combat lives in planner.py.

    rest            commands.c Do_rest
    poison drain    stats.c Do_age (every rest, move, ...)
    gold weight     stats.c Do_speed
    cursed treasure treasure.c Do_cursed_treasure / Do_treasure
    treasure drops  fight.c (after a kill)
    trove maps      treasure.c (scroll 1 of 6, treasure type 4 only)
"""

import math

# data/charstats: class -> (weakness, gold tote)
CLASSES = {
    "magic-user":  (14.0, 150.0),
    "fighter":     (14.0, 225.0),
    "elf":         (14.0, 125.0),
    "dwarf":       (14.0, 200.0),
    "halfling":    (14.0, 175.0),
    "experimento": (14.0, 175.0),
}
DEFAULT_CLASS = (14.0, 150.0)          # unknown class: assume the lowest tote

MAP_TREASURE_TYPE = 4                  # scrolls (and so trove maps) come from these


def class_stats(cls):
    return CLASSES.get((cls or "").strip().lower(), DEFAULT_CLASS)


# ---------------------------------------------------------------- resting
def rest_gain(max_total, level, cls=None):
    """Energy one Rest gives: (max energy + shield)/15 + level/3 + 2
    (dwarves /12). `max_total` is max energy INCLUDING shield, which is what
    the client's energy bar shows."""
    div = 12.0 if (cls or "").lower() == "dwarf" else 15.0
    return max_total / div + level / 3.0 + 2.0


def poison_drain(poison, max_total, shield, cls=None):
    """Energy poison takes every turn (each Rest included): poison *
    weakness * max energy (without shield) / 600. It never takes you below
    10% of max energy + shield."""
    if not poison or poison <= 0:
        return 0.0
    weakness = class_stats(cls)[0]
    base = max(0.0, max_total - (shield or 0.0))
    return poison * weakness * base / 600.0


def rest_target(energy, max_total, shield, level, poison, cls=None):
    """Where resting tops out and how many rests to get there.
    Returns (target, net gain per rest, rests needed or None if resting
    can't raise energy). A rest adds its gain (capped at max) and then
    poison drains, so while poisoned the top is max - drain."""
    gain = rest_gain(max_total, level, cls)
    drain = poison_drain(poison, max_total, shield, cls)
    floor10 = max_total / 10.0
    if energy <= floor10:
        drain = 0.0                       # poison can't take you below 10%
    target = max_total - drain
    net = gain - drain
    if energy >= target - 0.5:
        return target, net, 0
    if net <= 0:
        return target, net, None
    return target, net, int(math.ceil((target - energy) / net))


def rest_mana(circle, level, cloaked=False):
    """Mana one Rest finds: floor((circle + level) / 5), at least 1; none
    while cloaked."""
    if cloaked:
        return 0
    return max(1, math.floor((circle + level) / 5.0))


# ---------------------------------------------------------------- gold weight
def gold_speed_loss(gold, gems, level, cls=None):
    """Speed lost to carrying gold: ((gold + gems/2) - 1000) / tote - level,
    never below 0. Rounded fatigue isn't involved, so this is exact."""
    tote = class_stats(cls)[1]
    return max(0.0, ((gold or 0) + (gems or 0) / 2.0 - 1000.0) / tote - (level or 0))


def gold_limit(gems, level, cls=None):
    """Most gold you can carry before it starts slowing you down."""
    tote = class_stats(cls)[1]
    return 1000.0 + tote * (level or 0) - (gems or 0) / 2.0


# ---------------------------------------------------------------- treasure
def curse_cost(circle):
    """Amulets a curse uses up: floor(sqrt(circle))."""
    return int(math.floor(math.sqrt(max(1, circle or 1))))


def curse_covered(amulets, charms, circle):
    """Would a curse be harmless? Amulets (floor(sqrt(circle)) of them) save
    you first, then one charm. Otherwise a curse costs up to a third of your
    energy and adds 0.25 poison."""
    return (amulets or 0) >= curse_cost(circle) or (charms or 0) > 0


def treasure_drop_chance(treasure_type, size, unicorn=False):
    """Chance a killed monster leaves treasure: RND > 0.2 + 0.4^(size/3)."""
    if not treasure_type or treasure_type <= 0:
        return 0.0
    if unicorn:
        return 1.0
    return max(0.0, 1.0 - (0.2 + 0.4 ** (max(1, size) / 3.0)))


def map_eligible(level, gems):
    """Scrolls only give trove maps while level <= 100 - gems."""
    return (level or 0) <= 100 - (gems or 0)


def map_chance_per_kill(treasure_type, size, circle=1):
    """Chance one kill gives a trove map: it drops treasure, the treasure
    isn't gold/gems (35% of the time in normal circles it is), and the
    scroll is a map (1 in 6). Type-4 treasure is never cursed on inspect."""
    if treasure_type != MAP_TREASURE_TYPE:
        return 0.0
    return treasure_drop_chance(treasure_type, size) * 0.65 / 6.0
