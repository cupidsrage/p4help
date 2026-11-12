"""
Policy-based combat planner.

The old model asked "what if I do X every round?" — which is not how you fight.
This asks "given where the fight is RIGHT NOW, what is the single best next move?"

Two layers:

  1. POLICY  — an opening game plan, simulated end-to-end with proper sequencing.
               e.g. "luckout, then melee if it misses" or "buff might x2, then melee".
               This answers: how should I approach this monster?

  2. NEXT    — given live fight state (monster hp left, luckout spent, your energy,
               buffs up), score every legal move for THIS round.
               This answers: what do I press now?

All formulas ported from Phantasia 4 fight.c. See combat.py for the raw math.
"""

import math
import random
from dataclasses import dataclass, field, replace

from combat import (Player, Monster, SWORDPOWER,
                    ML_AON, MM_AON, ML_BOLT,
                    ML_INCRMIGHT, MM_INCRMIGHT,
                    ML_HASTE, MM_HASTE,
                    ML_FORCEFIELD, MM_FORCEFIELD,
                    SPELL_IMMUNE, ALWAYS_FLEE)

ML_PARALYZE, MM_PARALYZE = 60.0, 125.0


# ---------------------------------------------------------------- fight state
FATIGUE = 50                       # N_FATIGUE: every 50 passes costs 1 speed
NO_INITIATIVE = {"DL", "Sh", "Mi"}  # never attack on initiative (fight.c)


@dataclass
class FightState:
    """Where the fight actually is, right now (at YOUR prompt)."""
    p_energy: float = 0.0
    p_mana: float = 0.0
    p_shield: float = 0.0
    force_field: float = 0.0
    strength_spell: float = 0.0    # Increase Might cast in THIS simulation
    speed_spell: float = 0.0       # Haste cast in THIS simulation
    luckout_spent: bool = False
    aon_spent: bool = False
    ring_in_use: bool = False
    m_energy: float = 0.0
    m_shield: float = 0.0
    m_strength: float = 0.0
    m_speed: float = 0.0
    m_paralyzed: bool = False
    rounds: int = 0                # your actions
    # server-side monster numbers (fight.c keeps these per fight)
    m_max_energy: float = 0.0
    m_max_strength: float = 0.0
    m_max_speed: float = 0.0
    m_experience: float = 0.0
    melee_dmg: float = 0.0         # melee total: wears its strength down
    skirm_dmg: float = 0.0         # skirmish total: wears its speed down
    ticks: int = 0                 # passes of the server's fight loop
    t_base: int = 0                # ticks already reflected in your Speed
    pre_roll: bool = False         # it may act before your first move
    # monster specials (fight.c Do_monsthits)
    p_holy: float = 0.0            # holy water vials
    p_charms: float = 0.0
    p_gold: float = 0.0
    p_gems: float = 0.0
    p_blind: bool = False
    p_blessing: bool = False
    gone: bool = False             # it left (stole, ran, transported you...)
    perm: float = 0.0              # lasting harm, in energy-equivalent points

    @staticmethod
    def opening(player, monster):
        return FightState(
            p_energy=player.energy, p_mana=player.mana, p_shield=player.shield,
            m_energy=monster.energy, m_shield=monster.shield,
            m_strength=monster.strength, m_speed=monster.speed,
            m_max_energy=monster.energy, m_max_strength=monster.strength,
            m_max_speed=monster.speed, m_experience=monster.experience,
            p_holy=getattr(player, "holywater", 0), p_charms=getattr(player, "charms", 0),
            p_gold=getattr(player, "gold", 0), p_gems=getattr(player, "gems", 0),
            p_blind=getattr(player, "blind", False),
            p_blessing=getattr(player, "blessing", False),
        )


# ---------------------------------------------------------------- primitives
def base_strength(player):
    """Strength without the sword bonus. The client's Strength already
    includes it: strength * (1 + sqrt(sword) * .04) (stats.c Do_strength)."""
    return player.strength / (1 + math.sqrt(max(0.0, player.sword)) * SWORDPOWER)


def might_of(player, st):
    """fight.c: might = strength*(1+sqrt(sword)*.04) + strengthSpell, x2 with
    a ring. That first part is exactly the Strength the client shows (and a
    Might cast is added to it live), so only add casts from the simulation."""
    m = player.strength + st.strength_spell
    if st.ring_in_use:
        m *= 2
    return m


def quickness_of(player, st):
    """Your speed now: Haste adds, and fatigue takes 1 per 50 passes
    (stats.c Do_speed). The client's Speed already holds earlier fatigue."""
    tired = st.ticks // FATIGUE - st.t_base // FATIGUE
    return max(0.0, player.quickness + st.speed_spell - tired)


def p_monster_acts(m_speed, quickness):
    """Chance it wins one initiative roll: RND*its speed > RND*your speed."""
    a, b = max(0.0, m_speed), max(0.0, quickness)
    if a <= 0:
        return 0.0
    if b <= 0:
        return 1.0
    return a / (2 * b) if a <= b else 1 - b / (2 * a)


def hits_per_turn(player, mon, st):
    """Expected monster hits between two of your moves. Each pass of the
    server loop it EITHER hits you (wins initiative) OR you act, so the
    hits come in a run that ends the first time you win: p / (1 - p)."""
    if set(mon.specials) & NO_INITIATIVE:
        return 0.0
    p = min(0.95, p_monster_acts(st.m_speed, quickness_of(player, st)))
    return p / (1 - p)


def _land(st, mon, dmg):
    """Damage eats monster shield first, then energy (Do_hitmonster)."""
    st.m_shield -= dmg
    if st.m_shield < 0:
        st.m_energy += st.m_shield
        st.m_shield = 0.0


def special_of(mon):
    """The server special ('TROLL', 'WRAITH', ...) or None. Cached on the
    monster: this runs on every simulated hit."""
    d = mon.__dict__
    if "_sm" not in d:
        d["_sm"] = next((s[3:] for s in mon.specials if s.startswith("SM:")), None)
    return d["_sm"]


# Lasting harm from specials, priced in energy-equivalent points so it can
# sit next to energy lost in true_cost(). Fractions are of max energy.
PERM = {
    "poison": 0.25,       # per unit of poison: drains every turn, costs a medic
    "blind": 0.5,         # names hidden, no maps, until cured
    "brains": 0.5,        # Succubus: brains x0.8 for good
    "speed": 0.15,        # per point of speed lost for good
    "saruman": 0.3,
    "transport": 0.1,     # carried far away: the walk back
    "jabber_friend": 0.4,  # left facing a Jubjub Bird or Bandersnatch
    "blessing": 0.3,
    "maxenergy": 0.01,    # Nazgul curse: max energy x0.99
    "charm": 0.02,        # per charm spent blocking a special
    "exp": 0.0,           # Balrog experience theft (counted as a weaker fight)
}
GOLD_PER_ENERGY = 5.0     # a shield point costs 5 gold: gold -> energy points
GEM_GOLD = 1000.0         # N_GEMVALUE


def _perm(st, player, kind, amount=1.0):
    st.perm += PERM[kind] * amount * max(1.0, getattr(player, "max_energy", 0) or 0)


def _charms_block(player, mon, st, sm):
    """Charms soak a special (fight.c): dtemp = treasure type -
    floor((class + 1) * 1.5), at least 1; it costs between dtemp and
    sqrt(size) * dtemp charms."""
    if sm in ("MODNAR", "MORON", "SMURF", "TROLL", "SMEAGOL", "MIMIC"):
        return False
    if sm == "SARUMAN" and st.p_charms > getattr(player, "amulets", 0):
        return False
    dtemp = max(1.0, (getattr(mon, "treasure", 0) or 0)
                - math.floor((getattr(player, "ptype", 1) + 1) * 1.5))
    need = max(dtemp, math.ceil(random.random()
                                * math.floor(math.sqrt(max(1, mon.size)) * dtemp)))
    if st.p_charms >= need:
        st.p_charms -= need
        _perm(st, player, "charm", need)
        return True
    return False


def _special(player, mon, st, sm):
    """One of its 20% specials. Returns True if it replaced the hit."""
    size = max(1.0, getattr(mon, "size", 1) or 1)
    mx = max(1.0, player.max_energy)
    if sm == "LEANAN":
        lost = max(0.02 * player.strength,
                   min(player.level or 1, 1 + random.random() * (size * 4 - 1)))
        st.strength_spell -= lost
        st.perm += lost / max(1.0, player.strength) * mx
        return True
    if sm == "THAUMATURG":
        st.gone = True
        _perm(st, player, "transport")
        return True
    if sm == "SARUMAN":
        _perm(st, player, "saruman")
        return True
    if sm == "VORTEX":
        st.p_mana -= min(st.p_mana, math.floor(random.random() * 50 * size))
        return True
    if sm == "SUCCUBUS":
        _perm(st, player, "brains")
        return True
    if sm == "TIAMAT":
        st.perm += (st.p_gold / 2 + st.p_gems / 2 * GEM_GOLD) / GOLD_PER_ENERGY
        st.gone = True
        return True
    if sm == "KOBOLD":
        if st.p_gold > 0:
            st.p_gold -= 1
            st.gone = True
            return True
        return False
    if sm == "SHELOB":
        _perm(st, player, "poison", 1.0)
        return True
    if sm == "LAMPREY":
        if random.random() * 10 < size / 2 - 1:
            _perm(st, player, "poison", 0.25)
        return True
    if sm == "BONNACON":
        st.p_energy /= 2.0
        st.gone = True
        return True
    if sm == "CERBERUS":
        shield = getattr(player, "shield", 0) or 0
        st.perm += shield + st.p_gold / GOLD_PER_ENERGY
        st.p_energy = min(st.p_energy, mx - shield)
        st.gone = True
        return True
    if sm == "UNGOLIANT":
        _perm(st, player, "poison", 5.0)
        _perm(st, player, "speed", 1.0)
        st.speed_spell -= 1.0
        return True
    if sm == "JABBERWOCK":
        qs = getattr(player, "quicksilver", 0) or 0
        if random.random() > .75 or qs == 0:
            st.gone = True
            _perm(st, player, "jabber_friend")
        else:
            lost = math.sqrt(qs) - math.sqrt(math.floor(qs * .5))
            st.speed_spell -= lost
            _perm(st, player, "speed", lost)
        return True
    if sm == "TROLL":
        st.m_energy += math.floor((st.m_max_energy - st.m_energy) / 2.0)
        st.m_strength, st.m_speed = st.m_max_strength, st.m_max_speed
        st.melee_dmg = st.skirm_dmg = 0.0
        return True
    if sm == "WRAITH":
        if not st.p_blind:
            st.p_blind = True
            _perm(st, player, "blind")
            return True
        return False
    if sm == "NAZGUL":
        if st.p_blessing:
            st.p_blessing = False
            _perm(st, player, "blessing")
            return False                     # then a normal hit
        st.p_energy = (st.p_energy + st.force_field) / 2.0
        st.force_field = 0.0
        _perm(st, player, "poison", 0.5)
        _perm(st, player, "maxenergy")
        return True
    return False                             # Smeagol without a ring, etc.


def _monster_hit(st, player=None, mon=None):
    """Do_monsthits: floor(1 + RND * strength), unless a special takes the
    hit. A force field is a pool of hit points that soaks damage first."""
    sm = special_of(mon) if mon is not None else None
    if sm is None:                           # plain monster: the common case
        dmg = math.floor(1.0 + random.random() * max(0.0, st.m_strength))
        if st.force_field < dmg:
            st.p_energy -= dmg - st.force_field
            st.force_field = 0.0
        else:
            st.force_field -= dmg
        return dmg
    if sm == "DARKLORD":
        st.p_energy = 0.0                     # hits just enough to kill you
        return 0
    if sm == "BALROG" and random.random() > .33 and getattr(player, "experience", 1):
        # whips away experience instead of energy, and grows stronger
        st.m_strength += min(.05 * (getattr(player, "shield", 0) or 0),
                             math.floor(math.sqrt((getattr(player, "experience", 0) or 0)
                                                  / 1800.0)))
        return 0
    if sm == "FAERIES" and st.p_holy >= math.floor(math.sqrt(max(1, mon.size))):
        st.p_holy -= math.floor(math.sqrt(max(1, mon.size)))
        st.m_energy = 0.0                     # "Your holy water killed it!"
        return 0
    if sm == "TITAN":
        st.perm += 0.01 * (getattr(player, "shield", 0) or 0)
        dmg = math.floor(1.0 + random.random() * max(0.0, st.m_strength))
        st.force_field = 0.0                  # punches straight through
        st.p_energy -= dmg
        return dmg
    if (sm and sm not in ("SHRIEKER", "MIMIC", "MODNAR", "MORON", "SMURF",
                          "IDIOT", "UNICORN") and random.random() < 0.2):
        if not (getattr(player, "ptype", 1) == 4 and random.random() < 0.25):
            if _charms_block(player, mon, st, sm):
                return 0
            if _special(player, mon, st, sm):
                return 0
    dmg = math.floor(1.0 + random.random() * max(0.0, st.m_strength))
    if st.force_field < dmg:
        st.p_energy -= dmg - st.force_field
        st.force_field = 0.0
    else:
        st.force_field -= dmg
    return dmg


def _wake(st):
    """Start of every pass: a paralyzed monster's speed climbs back by 1."""
    st.m_speed = min(st.m_speed + 1.0, st.m_max_speed)
    st.m_paralyzed = st.m_speed <= 0


def _monster_turns(player, mon, st, after_action=True):
    """The server's fight loop, from the end of your action until your next
    prompt. Each pass, paralysis wears off a little, then it rolls
    initiative: if it wins it hits you and the loop goes round again; if
    you win, it's your move. So a slow monster often gets no hit at all
    between your swings, and a fast one can hit several times in a row."""
    if after_action:
        st.ticks += 1                       # the pass you just acted in
    no_init = bool(set(mon.specials) & NO_INITIATIVE)
    # A blessing gives you the first hit. With any sin the monster can take
    # it back by winning several initiative rolls in a row (fight.c).
    firsthit = not after_action and getattr(player, "blessing", False)
    sin = getattr(player, "sin", 0.0) or 0.0
    for _ in range(400):
        if st.p_energy <= 0 or st.gone or st.m_energy <= 0:
            return
        _wake(st)
        q = quickness_of(player, st)
        if firsthit and sin > 0:
            k = 6 - math.floor((sin + 1.0) / 1.25)
            if (all(random.random() * st.m_speed > random.random() * q
                    for _ in range(max(0, k))) and q > 0):
                firsthit = False
        if firsthit:
            return                          # blessed: you go first
        if q <= 0 and st.m_speed > 0 and not no_init:
            st.p_energy = 0.0               # speed 0: it never stops hitting
            return
        if no_init or not (random.random() * st.m_speed > random.random() * q):
            return                          # your turn
        _monster_hit(st, player, mon)
        st.ticks += 1
    st.p_energy = min(st.p_energy, 0.0)


def legal_moves(player, mon, st):
    """Every button you could actually press this round."""
    if "Sh" in mon.specials:
        # Shrieker: never attack it. A hit that doesn't kill it calls a
        # monster #70-99 (Balrog, Titan, Dragon...), and running never fails.
        return ["evade"]
    out = ["melee", "skirmish", "evade"]
    immune = bool(set(mon.specials) & SPELL_IMMUNE)

    if not st.luckout_spent and "Mo" not in mon.specials:
        out.append("luckout")
    if player.sword > 0:
        out.append("nick")
    if not immune:
        # AoN costs 1 mana if you have it, but works without; the advisor
        # still only tries it once (a miss doubles the monster)
        if not st.aon_spent and player.magiclvl >= ML_AON:
            out.append("aon")
        if player.magiclvl >= ML_BOLT and st.p_mana >= 1:
            out.append("bolt")
        if (player.magiclvl >= ML_INCRMIGHT
                and st.p_mana >= MM_INCRMIGHT + player.magiclvl / 2):
            out.append("might")
        if (player.magiclvl >= ML_HASTE
                and st.p_mana >= MM_HASTE + player.magiclvl / 2):
            out.append("haste")
        if (player.magiclvl >= ML_FORCEFIELD and st.p_mana > MM_FORCEFIELD
                and st.force_field <= 0):
            out.append("forcefield")
        if (player.magiclvl >= ML_PARALYZE and st.p_mana >= MM_PARALYZE
                and st.m_speed > 0):
            out.append("paralyze")
    if player.ring and not st.ring_in_use:
        out.append("ring")
    return out


def apply_move(player, mon, st, move, bolt_mana=None):
    """Run one player action exactly as fight.c does. Mutates st. Returns
    True if the monster died (or the fight ended in your favour)."""
    st.rounds += 1
    max_e = max(1.0, st.m_max_energy or mon.energy)

    if move == "melee":
        dmg = math.floor((0.5 + 1.3 * random.random()) * might_of(player, st))
        st.melee_dmg += dmg
        # melee wears its strength down: up to -1/3 as its energy runs out
        st.m_strength = (st.m_max_strength
                         - (st.melee_dmg / max_e) * (st.m_max_strength / 3.0))
        _land(st, mon, dmg)

    elif move == "skirmish":
        dmg = math.floor((0.33 + 1.1 * random.random()) * might_of(player, st))
        st.skirm_dmg += dmg
        # skirmish wears its speed down instead (and resets a paralysis)
        st.m_speed = (st.m_max_speed
                      - (st.skirm_dmg / max_e) * (st.m_max_speed / 3.0))
        _land(st, mon, dmg)

    elif move == "nick":
        # 1 + sword damage, steals 10% of its experience, makes it faster
        st.m_experience -= math.floor(st.m_experience / 10.0)
        st.m_max_speed += 2.0
        st.m_speed = 0.0 if st.m_speed < 0 else st.m_speed + 2.0
        _land(st, mon, 1.0 + player.sword)

    elif move == "luckout":
        st.luckout_spent = True
        if "DL" not in mon.specials:
            if not (random.random() * player.brains < random.random() * mon.brains):
                st.m_energy = 0.0
                return True

    elif move == "aon":
        st.aon_spent = True
        if st.p_mana >= MM_AON:
            st.p_mana -= MM_AON
        chance = (3000.0 * player.magiclvl
                  / (st.m_experience + 10000.0 * player.magiclvl))
        if chance > random.random():
            st.m_energy = 0.0
            return True
        # miss: strength AND max strength double (undoing melee wear), speed too
        st.m_max_strength *= 2.0
        st.m_strength = st.m_max_strength
        st.m_max_speed *= 2.0
        st.m_speed = st.m_speed * 2.0 if st.m_speed * 2.0 > 1 else st.m_speed + 1

    elif move == "bolt":
        spend = min(bolt_mana or st.p_mana, st.p_mana)
        st.p_mana -= spend
        dmg = math.floor(spend * (0.7 + 0.6 * random.random())
                         * (player.magiclvl ** 0.40 + 1.0))
        if player.level > 10 and (random.random() * (player.level + player.magiclvl)
                                  < math.sqrt(player.level)):
            if random.random() > 0.5:
                dmg *= random.random() / 2 + 0.5          # fumbled
            else:
                dmg += dmg * random.random()              # surge...
                st.p_mana += spend * (random.random() / 2 + 0.25)   # ...and refund
        if "DL" in mon.specials:
            dmg = 0.0
        _land(st, mon, dmg)

    elif move == "might":
        st.p_mana -= MM_INCRMIGHT + player.magiclvl / 2
        # stacks, lasts the fight; based on strength without the sword
        st.strength_spell += base_strength(player) * (
            1 - 10.0 / (math.sqrt(player.magiclvl) + 10.0))

    elif move == "haste":
        st.p_mana -= MM_HASTE + player.magiclvl / 2
        st.speed_spell += player.quickness / 2 * (
            1 - 10.0 / (math.sqrt(player.magiclvl) + 10.0))

    elif move == "forcefield":
        full = MM_FORCEFIELD + player.magiclvl / 2
        if st.p_mana < full:
            st.force_field += 40 * (st.p_mana - MM_FORCEFIELD)
            st.p_mana = 0.0
        else:
            st.p_mana -= full
            st.force_field += 20 * player.magiclvl

    elif move == "paralyze":
        st.p_mana -= MM_PARALYZE
        chance = (4000.0 * player.magiclvl
                  / (st.m_experience + 6000.0 * player.magiclvl))
        if chance > random.random() - 0.1:
            st.m_paralyzed = True
            st.m_speed = -2.0

    elif move == "ring":
        st.ring_in_use = True

    elif move == "evade":
        pass   # handled by caller

    # Shrieker: any hit that doesn't kill it makes it shriek and call
    # monster #70-99 (Balrog, Titan, Dragon...) in its place (fight.c
    # Do_hitmonster -> Do_monsthits). Count that as losing the fight.
    if ("Sh" in mon.specials and move in ("melee", "skirmish", "nick", "bolt")
            and 0 < st.m_energy):
        st.p_energy = 0.0
    return st.m_energy <= 0


def try_evade(player, mon, st):
    if set(mon.specials) & ALWAYS_FLEE:
        return True
    if "Mi" in mon.specials and random.random() >= 0.05:
        return False
    return (random.random() * (quickness_of(player, st) * 1.1 + 1) * (player.brains + 1)
            > random.random() * (st.m_speed * 1.1 + 1) * (mon.brains + 1))


def _outcome(s):
    """None while the fight goes on; else 'win', 'death' or 'gone' (it left
    with no experience: stole, ran off, transported you...)."""
    if s.p_energy <= 0:
        return "death"
    if s.m_energy <= 0:
        return "win"
    if s.gone:
        return "gone"
    if s.rounds >= 120:
        return "death"
    return None


def start_trial(player, mon, st):
    """Copy of st for one simulated fight; lets the monster have its
    opening hits if the fight hasn't reached your first prompt yet."""
    s = replace(st)
    s.t_base = s.ticks
    if special_of(mon) == "UNICORN":
        # never fights: a Virgin subdues it, otherwise it runs off (fight.c)
        if getattr(player, "virgin", False):
            s.m_energy = 0.0
        else:
            s.gone = True
        return s
    if s.pre_roll:
        s.pre_roll = False
        _monster_turns(player, mon, s, after_action=False)
    return s


# ---------------------------------------------------------------- policies
def _bolt_to_kill(player, st, mon, safe=True):
    """Exact mana to one-shot what's LEFT of the monster."""
    hp = st.m_energy + st.m_shield
    if hp <= 0 or player.magiclvl < ML_BOLT:
        return None
    scale = player.magiclvl ** 0.40 + 1.0
    mult = 0.7 if safe else 1.0
    return math.ceil(hp / (mult * scale))


POLICIES = {}


def policy(name):
    def deco(fn):
        POLICIES[name] = fn
        return fn
    return deco


@policy("luckout, then melee")
def _p_luck_melee(player, mon, st):
    if not st.luckout_spent and "Mo" not in mon.specials:
        return "luckout", None
    return "melee", None


@policy("melee only")
def _p_melee(player, mon, st):
    return "melee", None


@policy("buff might, then melee")
def _p_buff_melee(player, mon, st):
    cost = MM_INCRMIGHT + player.magiclvl / 2
    # buff while it pays for itself: keep ~2 casts, then swing
    casts = st.strength_spell / max(1e-9, player.strength * (
        1 - 10.0 / (math.sqrt(max(1.0, player.magiclvl)) + 10.0)))
    if (player.magiclvl >= ML_INCRMIGHT and st.p_mana >= cost
            and casts < 2 and st.m_energy > might_of(player, st) * 2):
        return "might", None
    return "melee", None


@policy("luckout, then bolt")
def _p_luck_bolt(player, mon, st):
    if not st.luckout_spent and "Mo" not in mon.specials:
        return "luckout", None
    n = _bolt_to_kill(player, st, mon)
    if n and n <= st.p_mana:
        return "bolt", n
    return "melee", None


@policy("bolt to kill")
def _p_bolt(player, mon, st):
    n = _bolt_to_kill(player, st, mon)
    if n and n <= st.p_mana:
        return "bolt", n
    if st.p_mana > 0 and player.magiclvl >= ML_BOLT:
        return "bolt", st.p_mana
    return "melee", None


@policy("paralyze, then melee")
def _p_para(player, mon, st):
    if (player.magiclvl >= ML_PARALYZE and st.p_mana >= MM_PARALYZE
            and not st.m_paralyzed):
        return "paralyze", None
    return "melee", None


@policy("all-or-nothing gamble")
def _p_aon(player, mon, st):
    if not st.aon_spent and st.p_mana >= MM_AON:
        return "aon", None
    if not st.luckout_spent and "Mo" not in mon.specials:
        return "luckout", None
    return "melee", None


@policy("flee")
def _p_flee(player, mon, st):
    return "evade", None


def run_policy(player, mon, pol, trials=1200, max_rounds=120):
    """Play a full fight under a policy. Returns outcome stats."""
    fn = POLICIES[pol]
    wins = fled = died = 0
    en_lost = mana = rnds = 0.0

    perm = 0.0
    for _ in range(trials):
        st = start_trial(player, mon, FightState.opening(player, mon))
        out = _outcome(st)
        while out is None:
            move, arg = fn(player, mon, st)
            if move == "evade":
                st.rounds += 1
                if try_evade(player, mon, st):
                    out = "flee"
                    break
            else:
                apply_move(player, mon, st, move, arg)
            out = _outcome(st)
            if out is None:
                _monster_turns(player, mon, st)
                out = _outcome(st)
            if st.rounds >= max_rounds and out is None:
                out = "death"
        if out == "win":
            wins += 1
        elif out in ("flee", "gone"):
            fled += 1
        else:
            died += 1
        perm += st.perm

        en_lost += player.energy - max(0.0, st.p_energy)
        mana += player.mana - st.p_mana
        rnds += st.rounds

    return {
        "policy": pol,
        "win": wins / trials,
        "flee": fled / trials,
        "death": died / trials,
        "energy_lost": en_lost / trials,
        "mana_spent": mana / trials,
        "rounds": rnds / trials,
        "perm": perm / trials,
    }


def plan(player, mon, trials=1200):
    """Rank every viable opening game plan."""
    immune = bool(set(mon.specials) & SPELL_IMMUNE)
    viable = ["melee only", "flee"]

    if "Mo" not in mon.specials:
        viable.append("luckout, then melee")
    if not immune:
        if player.magiclvl >= ML_INCRMIGHT:
            viable.append("buff might, then melee")
        if player.magiclvl >= ML_BOLT and player.mana > 0:
            viable.append("bolt to kill")
            if "Mo" not in mon.specials:
                viable.append("luckout, then bolt")
        if player.magiclvl >= ML_PARALYZE and player.mana >= MM_PARALYZE:
            viable.append("paralyze, then melee")
        if player.magiclvl >= ML_AON and player.mana >= MM_AON:
            viable.append("all-or-nothing gamble")

    out = [run_policy(player, mon, p, trials) for p in viable]
    st0 = FightState.opening(player, mon)
    danger = danger_of(player, mon, st0)
    for r in out:
        r["cost"] = true_cost(player, st0, r, danger)

    best_win = max((r["win"] for r in out), default=0.0)
    if best_win >= 0.15:
        TIE = 0.02
        top = best_win
        tied = [r for r in out if r["win"] >= top - TIE]
        rest = [r for r in out if r["win"] < top - TIE]
        tied.sort(key=lambda r: r["cost"])
        rest.sort(key=lambda r: (-r["win"], r["cost"]))
        return tied + rest
    out.sort(key=lambda r: (-r["flee"], r["cost"]))
    return out


# ---------------------------------------------------------------- cost model
def true_cost(player, st, r, danger):
    """
    What a move ACTUALLY costs you.

    ENERGY IS YOUR HEALTH. Nothing you do spends it -- it only goes down when
    the monster hits you (see _monster_turns). So `energy_lost` is a prediction
    of how much LIFE this fight will cost, not an action cost.

    But health is not free either, for two reasons:

      1. You have to REST it back, and resting takes turns during which you get
         attacked again. Energy has a real recovery cost.

      2. Health near zero is worth far more than health near full. Losing 30hp
         at 128/128 is an inconvenience; losing 30hp at 40/128 can kill you.
         So we price energy by how much of your REMAINING health it represents.

    Mana, meanwhile, is finite and slow to recover -- so it's precious against
    trivial monsters and nearly free against lethal ones.
    """
    energy = r["energy_lost"]
    mana = r.get("mana_spent", 0.0)

    # --- price of health, scaled by how close to death it puts you ---
    # fraction of your CURRENT health this fight will consume
    frac = energy / max(1.0, st.p_energy)
    # convex: cheap when you're topped up, brutally expensive when you're low
    energy_price = 1.0 + 4.0 * (frac ** 2)
    # and health is worth more the less of it you have left
    if player.max_energy > 0:
        low = 1.0 - (st.p_energy / player.max_energy)   # 0 = full, 1 = nearly dead
        energy_price *= 1.0 + 2.0 * (low ** 2)

    # --- price of mana, scaled by danger ---
    # trivial monster: 1 mana ~ 6 energy (hoard it)
    # lethal monster:  1 mana ~ 0.5 energy (spend it, just live)
    mana_price = max(3.0, 6.0 - 5.5 * danger)   # never cheap: it's hard to refill

    # --- dying dwarfs everything ---
    death_penalty = r.get("death", 0.0) * 800.0

    # --- All-or-Nothing is a GAMBLE, not a tool ---
    # On failure it doubles the monster's strength AND speed -- catastrophic.
    # The sim may still "win" via the follow-up, hiding that risk. So charge a
    # heavy penalty for choosing AoN UNLESS the fight is genuinely lethal
    # (danger high) and nothing safe wins. Against easy monsters this makes AoN
    # rank far below luckout/melee, where it belongs.
    aon_penalty = 0.0
    if r.get("move") == "aon":
        # only "cheap" when you're desperate: penalty fades as danger -> 1
        aon_penalty = 120.0 * (1.0 - danger)

    # --- lasting harm from specials (blindness, poison, lost stats...) ---
    perm = r.get("perm", 0.0)

    return (energy * energy_price + mana * mana_price
            + death_penalty + aon_penalty + perm)


def danger_of(player, mon, st):
    """
    How dangerous is this monster to you RIGHT NOW? [0,1]

    Expected damage taken (its hit x rounds to kill it) as a share of the health
    you actually have. Uses a soft curve rather than a hard clamp, so it doesn't
    saturate at 1.0 the moment a fight gets moderately risky -- a Bogle at low
    health and a Balrog should not read the same.
    """
    if st.p_energy <= 0:
        return 1.0
    # its average hit, times how many hits it gets per move of yours
    incoming = (1.0 + st.m_strength / 2.0) * hits_per_turn(player, mon, st)
    might = max(1.0, might_of(player, st) * 1.15)   # your average swing
    hp = max(1.0, st.m_energy + st.m_shield)
    rounds = hp / might
    expected_damage = incoming * rounds

    ratio = expected_damage / st.p_energy
    # soft saturation: ratio 0.5 -> ~0.33,  1.0 -> ~0.5,  3.0 -> ~0.75
    return ratio / (1.0 + ratio)


# ---------------------------------------------------------------- next move
MANA_MOVES = {"bolt", "aon", "might", "paralyze", "haste", "forcefield"}
# Mana is hard to come by and dear at the posts: spend it only when every
# way of fighting without it is at least this likely to get you killed.
# (launcher.json "mana_death" sets it.)
MANA_DEATH = 0.05
NO_MANA_WIN = 0.90     # a no-mana plan this sure to win and no deadlier than
                       # MANA_DEATH beats any plan that spends mana


AON_ONLY_IF_DEATH = 0.50
MIN_EVADE = 0.25           # an Evade working less often than this isn't ranked first   # rank AoN first only if everything else is this deadly


def score_moves(player, mon, st, trials=500, mana_fallback=True, only=None):
    """Score and rank every legal move (see _score_moves). All or Nothing is
    never ranked first while another move gives better than even odds of
    surviving: a failed cast doubles the monster's strength and speed (a
    size-9 Cocodrill hit for 1551 after three failures and killed a player)."""
    res = _score_moves(player, mon, st, trials, mana_fallback, only)
    # An Evade that rarely works isn't an escape: each failure gives the
    # monster free hits. Don't rank it first while something else wins.
    if (only is None and res and res[0]["move"] == "evade"
            and res[0]["flee"] < MIN_EVADE):
        alt = next((r for r in res[1:] if r["win"] >= 0.5), None)
        if alt is not None:
            res = [alt] + [r for r in res if r is not alt]
    # Nick does 1 + sword damage and makes the monster 2 faster each time;
    # it only "ties" because the rest of the fight is the same either way.
    if only is None and res and res[0]["move"] == "nick":
        alt = next((r for r in res[1:] if r["move"] != "nick"
                    and r["win"] >= res[0]["win"] - 0.05), None)
        if alt is not None:
            res = [alt] + [r for r in res if r is not alt]
    if only is None and res and res[0]["move"] == "aon":
        # next move in the advisor's own ranking that's reasonably safe
        best = next((r for r in res[1:]
                     if r.get("death", 1.0) < AON_ONLY_IF_DEATH), None)
        if best is not None:
            res = [best] + [r for r in res if r is not best]
    return res


def _score_moves(player, mon, st, trials=500, mana_fallback=True, only=None):
    """
    Given the CURRENT fight state, score each legal move by playing the rest
    of the fight out sensibly after it. This is the 'what do I press now?'
    answer, and it accounts for what's already happened.
    """
    results = []

    for mv in legal_moves(player, mon, st):
        if only is not None and mv not in only:
            continue
        arg = None
        if mv == "bolt":
            arg = _bolt_to_kill(player, st, mon)
            if not arg or arg > st.p_mana:
                arg = int(st.p_mana)
            if arg <= 0:
                continue

        wins = fled = 0
        en = 0.0
        mana = 0.0
        perm = 0.0
        for _ in range(trials):
            s = start_trial(player, mon, st)
            out = _outcome(s)
            if out is None:
                if mv == "evade":
                    s.rounds += 1
                    if try_evade(player, mon, s):
                        out = "flee"
                    else:
                        _monster_turns(player, mon, s)
                else:
                    apply_move(player, mon, s, mv, arg)
                    out = _outcome(s)
                    if out is None:
                        _monster_turns(player, mon, s)
            # ...then finish the fight with a sane default.
            #
            # IMPORTANT: the fallback must not reflexively bolt, or every move's
            # cost gets polluted with follow-up mana (a FREE luckout was showing
            # up as "33 mana" purely because of what came after it). Only reach
            # for mana when melee is actually losing the race.
            while out is None:
                out = _outcome(s)
                if out is not None:
                    break
                if not s.luckout_spent and "Mo" not in mon.specials:
                    nxt, narg = "luckout", None
                else:
                    # would melee kill it before it kills me?
                    swing = max(1.0, might_of(player, s) * 1.15)
                    rounds_to_kill = (s.m_energy + s.m_shield) / swing
                    incoming = ((1.0 + s.m_strength / 2.0)
                                * hits_per_turn(player, mon, s))
                    dmg_taken = rounds_to_kill * incoming
                    losing = dmg_taken > s.p_energy * 0.6

                    n = (_bolt_to_kill(player, s, mon)
                         if losing and mana_fallback else None)
                    if n and n <= s.p_mana:
                        nxt, narg = "bolt", n
                    elif (losing and mana_fallback and s.m_speed > 0
                          and player.magiclvl >= ML_PARALYZE
                          and s.p_mana >= MM_PARALYZE
                          and not set(mon.specials) & SPELL_IMMUNE):
                        # can't bolt it dead: hold it still for a few rounds
                        nxt, narg = "paralyze", None
                    else:
                        nxt, narg = "melee", None
                apply_move(player, mon, s, nxt, narg)
                if _outcome(s) is None:
                    _monster_turns(player, mon, s)
            if out == "win":
                wins += 1
            elif out in ("flee", "gone"):
                fled += 1
            en += player.energy - max(0.0, s.p_energy)
            mana += st.p_mana - s.p_mana
            perm += s.perm - st.perm

        results.append({
            "move": mv,
            "arg": arg,
            "win": wins / trials,
            "flee": fled / trials,
            "death": 1.0 - (wins / trials) - (fled / trials),
            "energy_lost": en / trials,
            "mana_spent": mana / trials,
            "perm": perm / trials,
        })

    # Rank by actual KILL probability first. Escape only matters when the fight
    # is unwinnable - otherwise a flee-ish move (skirmish/evade) can outrank a
    # genuine free kill, which is nonsense.
    danger = danger_of(player, mon, st)
    for r in results:
        r["danger"] = danger
        r["cost"] = true_cost(player, st, r, danger)

    if only is not None:
        return results          # internal re-score: no ranking needed

    # Mana is only worth spending when you can't win safely without it.
    # The playout above bolts whenever melee is "losing the race", so even a
    # Melee line carries mana -- which made an immediate Bolt look no dearer
    # and got it picked against big monsters melee alone beats. Re-score the
    # moves that cost no mana with a playout that never bolts; if one is a
    # near-sure, safe win, recommend it (with its no-mana numbers) first.
    plain = [r["move"] for r in results if r["move"] not in MANA_MOVES]
    if plain and any(r["move"] in MANA_MOVES or r.get("mana_spent", 0) > 0
                     for r in results):
        nm = _score_moves(player, mon, st, trials=max(150, trials // 2),
                          mana_fallback=False, only=set(plain))
        good = [r for r in nm if r["win"] >= NO_MANA_WIN
                and r["death"] <= MANA_DEATH]
        if good:
            for r in good:
                r["danger"] = danger_of(player, mon, st)
                r["cost"] = true_cost(player, st, r, r["danger"])
                r["no_mana"] = True
            good.sort(key=lambda r: (-round(r["win"], 2), r["cost"]))
            chosen = {r["move"] for r in good}
            others = [r for r in results if r["move"] not in chosen]
            others.sort(key=lambda r: (-r["win"], r["cost"]))
            return good + others

    best_win = max((r["win"] for r in results), default=0.0)

    if best_win < 0.15:
        # hopeless: just get out alive
        results.sort(key=lambda r: (-r["flee"], r["cost"]))
        return results

    # Winnable. Moves that lead to the same fallback line converge on win rate
    # and differ only by noise, so treat anything within ~2pts of the best as
    # TIED -- then break the tie on TRUE COST, which prices mana by scarcity.
    # This is what stops it from bolting trivial monsters: against a Bogle,
    # 35 mana is far more expensive than the 18 energy melee would cost.
    TIE = 0.02
    top = max(r["win"] for r in results)
    tied = [r for r in results if r["win"] >= top - TIE]
    rest = [r for r in results if r["win"] < top - TIE]

    tied.sort(key=lambda r: r["cost"])
    rest.sort(key=lambda r: (-r["win"], r["cost"]))
    return tied + rest


def explain(player, mon, st, moves):
    """One honest sentence about why the top move is the top move."""
    if not moves:
        return "No legal moves."
    top = moves[0]
    mv = top["move"]
    hp = st.m_energy + st.m_shield
    swings = math.ceil(hp / max(1.0, might_of(player, st) * 1.15))
    incoming = (1 + st.m_strength / 2) * hits_per_turn(player, mon, st)

    if mv == "luckout":
        pl = (1 - mon.brains / (2 * player.brains)) if player.brains >= mon.brains \
             else player.brains / (2 * mon.brains)
        return (f"Luckout is {pl*100:.0f}% to end this instantly, free, in one "
                f"round. Expect to lose ~{top['energy_lost']:.0f} health overall.")
    if mv == "bolt":
        return (f"{int(top['arg'])} mana one-shots it "
                f"({hp:.0f} hp left). Melee would take ~{swings} more rounds "
                f"and ~{swings*incoming:.0f} damage.")
    if mv == "might":
        return ("Buffing might now pays for itself — melee damage is linear in "
                "might, and the bonus stacks and lasts the whole fight.")
    if mv == "paralyze":
        ch = min(1.0, 4000 * player.magiclvl / (st.m_experience + 6000 * player.magiclvl) + 0.1)
        return (f"Paralyze ({ch*100:.0f}% to hold it): its speed drops to -2 and "
                "climbs back 1 a round, so you get about 3 free rounds before it "
                "can hit again. Skirmish wakes it up, so melee after.")
    if mv == "aon":
        ch = 3000 * player.magiclvl / (mon.experience + 10000 * player.magiclvl)
        return (f"All-or-Nothing: {ch*100:.0f}% instant kill for 1 mana. "
                f"A miss doubles its strength and speed — but nothing else here wins.")
    if mv == "haste":
        return ("Haste raises your speed for the whole fight, so it wins "
                "initiative less often and gets fewer hits on you.")
    if mv == "forcefield":
        return (f"Force Field soaks {20 * player.magiclvl:.0f} damage before "
                "your energy takes any.")
    if mv == "evade" and "Sh" in mon.specials:
        return ("Shrieker: RUN. Hitting it without killing it calls a Balrog, "
                "Titan or Dragon; running from a Shrieker always works.")
    if mv == "evade":
        return (f"You lose this fight. Get out — {top['flee']*100:.0f}% to escape.")
    if mv == "melee":
        hp_after = st.p_energy - top["energy_lost"]
        if st.luckout_spent:
            return (f"Luckout's spent. Melee: ~{swings} rounds, expect to lose "
                    f"~{top['energy_lost']:.0f} health "
                    f"({st.p_energy:.0f} \u2192 ~{max(0, hp_after):.0f}).")
        return (f"Melee: ~{swings} rounds, expect to lose "
                f"~{top['energy_lost']:.0f} health "
                f"({st.p_energy:.0f} \u2192 ~{max(0, hp_after):.0f}). "
                f"Degrades its strength as you go.")
    if mv == "skirmish":
        return ("Skirmish wears its speed down, so it wins initiative less "
                "often and gets fewer hits on you; it does less damage than melee.")
    return ""
