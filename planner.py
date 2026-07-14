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
@dataclass
class FightState:
    """Where the fight actually is, right now."""
    p_energy: float = 0.0
    p_mana: float = 0.0
    p_shield: float = 0.0
    force_field: float = 0.0
    strength_spell: float = 0.0    # accumulated Increase Might
    speed_spell: float = 0.0       # accumulated Haste
    luckout_spent: bool = False
    aon_spent: bool = False
    ring_in_use: bool = False
    m_energy: float = 0.0
    m_shield: float = 0.0
    m_strength: float = 0.0
    m_speed: float = 0.0
    m_paralyzed: bool = False
    rounds: int = 0

    @staticmethod
    def opening(player, monster):
        return FightState(
            p_energy=player.energy, p_mana=player.mana, p_shield=player.shield,
            m_energy=monster.energy, m_shield=monster.shield,
            m_strength=monster.strength, m_speed=monster.speed,
        )


# ---------------------------------------------------------------- primitives
def might_of(player, st):
    m = (player.strength * (1 + math.sqrt(max(0.0, player.sword)) * SWORDPOWER)
         + st.strength_spell)
    if st.ring_in_use:
        m *= 2
    return m


def _land(st, mon, dmg):
    """Damage eats monster shield first, then energy."""
    st.m_shield -= dmg
    if st.m_shield < 0:
        st.m_energy += st.m_shield
        st.m_shield = 0.0


def _monster_swing(st, mon):
    """inflict = 1 + r*strength, capped at your energy. Force field eats ONE hit."""
    if st.m_paralyzed or st.m_speed < 0:
        return 0.0
    dmg = math.floor(1.0 + random.random() * max(0.0, st.m_strength))
    dmg = min(dmg, st.p_energy)
    if st.force_field > 0:
        # NB: any hit zeroes the whole field - it is one free hit, not a pool
        st.force_field = 0.0
    st.p_energy -= dmg
    return dmg


def legal_moves(player, mon, st):
    """Every button you could actually press this round."""
    out = ["melee", "skirmish", "evade"]
    immune = bool(set(mon.specials) & SPELL_IMMUNE)

    if not st.luckout_spent and "Mo" not in mon.specials:
        out.append("luckout")
    if player.sword > 0:
        out.append("nick")
    if not immune:
        if not st.aon_spent and player.magiclvl >= ML_AON and st.p_mana >= MM_AON:
            out.append("aon")
        if player.magiclvl >= ML_BOLT and st.p_mana > 0:
            out.append("bolt")
        if (player.magiclvl >= ML_INCRMIGHT
                and st.p_mana >= MM_INCRMIGHT + player.magiclvl / 2):
            out.append("might")
        if (player.magiclvl >= ML_PARALYZE and st.p_mana >= MM_PARALYZE
                and not st.m_paralyzed):
            out.append("paralyze")
    if player.ring and not st.ring_in_use:
        out.append("ring")
    return out


def apply_move(player, mon, st, move, bolt_mana=None):
    """Run one player action. Mutates st. Returns True if the monster died."""
    st.rounds += 1

    if move == "melee":
        dmg = math.floor((0.5 + 1.3 * random.random()) * might_of(player, st))
        if "SS" in mon.specials:
            dmg *= 0.5
        _land(st, mon, dmg)

    elif move == "skirmish":
        dmg = math.floor((0.33 + 1.1 * random.random()) * might_of(player, st))
        if "SS" in mon.specials:
            dmg *= 0.5
        _land(st, mon, dmg)
        # degrades its SPEED (helps you flee; does NOT reduce its damage)
        st.m_speed = max(0.0, st.m_speed * 0.97)

    elif move == "nick":
        dmg = math.floor((0.4 + 1.0 * random.random()) * might_of(player, st))
        _land(st, mon, dmg)

    elif move == "luckout":
        st.luckout_spent = True
        if "DL" not in mon.specials:
            if random.random() * player.brains >= random.random() * mon.brains:
                st.m_energy = 0.0
                return True

    elif move == "aon":
        st.aon_spent = True
        st.p_mana = max(0.0, st.p_mana - MM_AON)
        chance = (3000.0 * player.magiclvl
                  / (mon.experience + 10000.0 * player.magiclvl))
        if random.random() < chance:
            st.m_energy = 0.0
            return True
        # failure: it doubles strength AND speed
        st.m_strength *= 2.0
        st.m_speed = st.m_speed * 2.0 if st.m_speed * 2.0 > 1 else st.m_speed + 1

    elif move == "bolt":
        spend = min(bolt_mana or st.p_mana, st.p_mana)
        st.p_mana -= spend
        scale = player.magiclvl ** 0.40 + 1.0
        if "Mr" in mon.specials:
            scale *= 0.5
        dmg = math.floor(spend * (0.7 + 0.6 * random.random()) * scale)
        _land(st, mon, dmg)

    elif move == "might":
        cost = MM_INCRMIGHT + player.magiclvl / 2
        st.p_mana -= cost
        # stacks, additive, lasts the fight
        st.strength_spell += player.strength * (
            1 - 10.0 / (math.sqrt(player.magiclvl) + 10.0))

    elif move == "paralyze":
        st.p_mana -= MM_PARALYZE
        chance = (4000.0 * player.magiclvl
                  / (mon.experience + 6000.0 * player.magiclvl))
        if chance > random.random() - 0.1:
            st.m_paralyzed = True
            st.m_speed = -2.0

    elif move == "ring":
        st.ring_in_use = True

    elif move == "evade":
        pass   # handled by caller

    return st.m_energy <= 0


def try_evade(player, mon, st):
    if set(mon.specials) & ALWAYS_FLEE:
        return True
    if "Mi" in mon.specials and random.random() >= 0.05:
        return False
    return (random.random() * (player.quickness * 1.1 + 1) * (player.brains + 1)
            > random.random() * (st.m_speed * 1.1 + 1) * (mon.brains + 1))


# ---------------------------------------------------------------- policies
def _bolt_to_kill(player, st, mon, safe=True):
    """Exact mana to one-shot what's LEFT of the monster."""
    hp = st.m_energy + st.m_shield
    if hp <= 0 or player.magiclvl < ML_BOLT:
        return None
    scale = player.magiclvl ** 0.40 + 1.0
    if "Mr" in mon.specials:
        scale *= 0.5
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

    for _ in range(trials):
        st = FightState.opening(player, mon)
        while st.rounds < max_rounds:
            move, arg = fn(player, mon, st)

            if move == "evade":
                st.rounds += 1
                if try_evade(player, mon, st):
                    fled += 1
                    break
                _monster_swing(st, mon)
                if st.p_energy <= 0:
                    died += 1
                    break
                continue

            if apply_move(player, mon, st, move, arg):
                wins += 1
                break
            _monster_swing(st, mon)
            if st.p_energy <= 0:
                died += 1
                break
        else:
            died += 1

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
    the monster hits you (see _monster_swing). So `energy_lost` is a prediction
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
    mana_price = 6.0 - 5.5 * danger

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

    return (energy * energy_price + mana * mana_price
            + death_penalty + aon_penalty)


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
    incoming = 1.0 + st.m_strength / 2.0            # its average hit
    might = max(1.0, might_of(player, st) * 1.15)   # your average swing
    hp = max(1.0, st.m_energy + st.m_shield)
    rounds = hp / might
    expected_damage = incoming * rounds

    ratio = expected_damage / st.p_energy
    # soft saturation: ratio 0.5 -> ~0.33,  1.0 -> ~0.5,  3.0 -> ~0.75
    return ratio / (1.0 + ratio)


# ---------------------------------------------------------------- next move
def score_moves(player, mon, st, trials=500):
    """
    Given the CURRENT fight state, score each legal move by playing the rest
    of the fight out sensibly after it. This is the 'what do I press now?'
    answer, and it accounts for what's already happened.
    """
    results = []

    for mv in legal_moves(player, mon, st):
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
        for _ in range(trials):
            s = replace(st)

            if mv == "evade":
                s.rounds += 1
                if try_evade(player, mon, s):
                    fled += 1
                    en += player.energy - s.p_energy
                    mana += st.p_mana - s.p_mana
                    continue
                _monster_swing(s, mon)
            else:
                dead = apply_move(player, mon, s, mv, arg)
                if dead:
                    wins += 1
                    en += player.energy - s.p_energy
                    mana += st.p_mana - s.p_mana
                    continue
                _monster_swing(s, mon)

            # ...then finish the fight with a sane default.
            #
            # IMPORTANT: the fallback must not reflexively bolt, or every move's
            # cost gets polluted with follow-up mana (a FREE luckout was showing
            # up as "33 mana" purely because of what came after it). Only reach
            # for mana when melee is actually losing the race.
            while s.p_energy > 0 and s.m_energy > 0 and s.rounds < 120:
                if not s.luckout_spent and "Mo" not in mon.specials:
                    nxt, narg = "luckout", None
                else:
                    # would melee kill it before it kills me?
                    swing = max(1.0, might_of(player, s) * 1.15)
                    rounds_to_kill = (s.m_energy + s.m_shield) / swing
                    incoming = 1.0 + s.m_strength / 2.0
                    dmg_taken = rounds_to_kill * incoming
                    losing = dmg_taken > s.p_energy * 0.6

                    n = _bolt_to_kill(player, s, mon) if losing else None
                    if n and n <= s.p_mana:
                        nxt, narg = "bolt", n
                    else:
                        nxt, narg = "melee", None
                if apply_move(player, mon, s, nxt, narg):
                    wins += 1
                    break
                _monster_swing(s, mon)
            en += player.energy - max(0.0, s.p_energy)
            mana += st.p_mana - s.p_mana

        results.append({
            "move": mv,
            "arg": arg,
            "win": wins / trials,
            "flee": fled / trials,
            "death": 1.0 - (wins / trials) - (fled / trials),
            "energy_lost": en / trials,
            "mana_spent": mana / trials,
        })

    # Rank by actual KILL probability first. Escape only matters when the fight
    # is unwinnable - otherwise a flee-ish move (skirmish/evade) can outrank a
    # genuine free kill, which is nonsense.
    danger = danger_of(player, mon, st)
    for r in results:
        r["danger"] = danger
        r["cost"] = true_cost(player, st, r, danger)

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
    incoming = 1 + st.m_strength / 2

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
        return ("Paralyze sets its speed to -2 — it effectively stops acting. "
                "Best mana you'll ever spend on something this dangerous.")
    if mv == "aon":
        ch = 3000 * player.magiclvl / (mon.experience + 10000 * player.magiclvl)
        return (f"All-or-Nothing: {ch*100:.0f}% instant kill for 1 mana. "
                f"A miss doubles its strength and speed — but nothing else here wins.")
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
        return ("Skirmish only to slow it down for an escape — it does less "
                "damage than melee and its speed isn't what's hurting you.")
    return ""
