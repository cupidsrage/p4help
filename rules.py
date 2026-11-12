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

# data/charstats: what each level adds (quickness, strength, mana, energy,
# brains, magic level). Note Fighters, Magic-Users, ... get NO speed.
LEVEL_GAINS = {
    "magic-user":  (0.0, 1.0, 80.0, 20.0, 4.5, 3.0),
    "fighter":     (0.0, 5.0, 35.0, 25.0, 2.0, 1.5),
    "elf":         (0.0, 2.0, 65.0, 15.0, 4.0, 2.5),
    "dwarf":       (0.0, 4.0, 50.0, 35.0, 3.0, 2.0),
    "halfling":    (0.0, 3.0, 20.0, 30.0, 6.0, 1.0),
    "experimento": (0.0, 1.0, 20.0, 15.0, 2.0, 1.0),
}

N_AGE = 750            # every 750 age: permanent stat loss (Do_degenerate)
DEGENERATE = {         # what one degeneration multiplies by
    "speed": 0.975, "max energy": 0.97, "strength": 0.97, "magic level": 0.97,
    "brains": 0.95, "shield": 0.93, "sword": 0.93, "quicksilver": 0.93,
}
HUNT_SIN = 0.001       # every Hunt adds this much sin (main.c)


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


# ---------------------------------------------------------------- aging
def turns_to_degeneration(age, degenerated):
    """Turns until the next permanent stat loss. Age goes up 1 every turn
    (each move, rest, fight, treasure screen...); the server degenerates you
    when age / 750 (whole number) passes your Degenerated count."""
    if age is None or degenerated is None:
        return None
    return max(0, int(N_AGE * (degenerated + 1) - age))


# ---------------------------------------------------------------- levels
def level_of(experience):
    """stats.c: level = floor((exp / 1000) ^ 0.4748) (higher formula past
    11 million experience)."""
    if experience < 1.1e7:
        return math.floor((experience / 1000.0) ** 0.4748)
    return math.floor((experience / 1250.0) ** 0.4865)


def exp_for_level(level):
    """Experience needed to reach `level`."""
    e = 1000.0 * level ** (1 / 0.4748)
    if e >= 1.1e7:
        e = 1250.0 * level ** (1 / 0.4865)
    e = math.ceil(e)
    while level_of(e) < level:          # float rounding
        e += 1
    return e


def level_gains(cls):
    g = LEVEL_GAINS.get((cls or "").lower())
    if not g:
        return None
    return dict(zip(("speed", "strength", "mana", "energy", "brains",
                     "magic level"), g))


# ---------------------------------------------------------------- cloak
def cloak_drain(circle, level, cls=None):
    """Mana a cloak costs per turn (stats.c Do_age)."""
    max_mana = {"magic-user": 120.0, "fighter": 90.0, "elf": 110.0,
                "dwarf": 100.0, "halfling": 80.0, "experimento": 100.0
                }.get((cls or "").lower(), 100.0)
    c = circle or 1
    if c in (27, 28):
        return math.floor(.02 * (1000 + level * max_mana))
    if 24 < c < 31:
        return math.floor(.01 * (1000 + level * max_mana))
    if 19 < c < 36:
        return math.floor(.005 * (1000 + level * max_mana))
    return c


# ---------------------------------------------------------------- blessing
def blessing_rolls(sin):
    """With a blessing, the monster must win this many initiative rolls in a
    row to take the first hit away from you (fight.c). Sinless: you always
    hit first."""
    if not sin or sin <= 0:
        return None                     # always first
    return max(0, 6 - math.floor((sin + 1.0) / 1.25))
