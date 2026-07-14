"""
Combat engine, ported from Phantasia 4 fight.c (v4.03).

Key formulas, straight from the source:

  might        = strength * (1 + sqrt(sword) * 0.04) + strengthSpell
                 (doubled if ring_in_use)

  melee        = floor((0.5  + 1.3 * rnd) * might)     -> degrades monster STRENGTH
  skirmish     = floor((0.33 + 1.1 * rnd) * might)     -> degrades monster SPEED

  bolt         = floor(mana * (0.7 + 0.6*rnd) * (magiclvl**0.40 + 1))
  AoN success  = 3000*magiclvl / (monster_exp + 10000*magiclvl) > rnd
     on success: inflict = monster.energy + monster.shield + 1   (instant kill)
     on failure: monster strength *= 2 AND max_speed *= 2        (very bad)
     cost: 1 mana

  monster hit  = floor(1 + rnd * monster.strength), capped at your energy

  damage lands on monster SHIELD first, spilling into ENERGY once shield is 0.

  evade  = rnd*(quickness*1.1+1)*(brains+1) > rnd*(mon.speed*1.1+1)*(mon.brains+1)

Spell gates (magiclvl):
  AoN 0 | Bolt 5 | IncrMight 15 | Haste 25 | ForceField 35 | Transport 45
  Paralyze 60 | Transform 75
Mana costs: AoN 1 | IncrMight 30 | Haste 35 | ForceField 60 | Transport 100
  Paralyze 125 | Transform 150
"""

import random
import math
from dataclasses import dataclass, replace

SWORDPOWER = 0.04

ML_AON, MM_AON = 0.0, 1.0
ML_BOLT = 5.0
ML_INCRMIGHT, MM_INCRMIGHT = 15.0, 30.0
ML_HASTE, MM_HASTE = 25.0, 35.0
ML_FORCEFIELD, MM_FORCEFIELD = 35.0, 60.0

# specials that make a monster immune to spells entirely
SPELL_IMMUNE = {"Mo", "DL"}
# specials that resist magic
MAGIC_RESIST = {"Mr"}
# specials that resist physical
PHYS_RESIST = {"SS"}
# monsters you can always flee
ALWAYS_FLEE = {"DL", "Sh"}


@dataclass
class Player:
    energy: float = 0
    max_energy: float = 0
    strength: float = 0
    quickness: float = 0     # "Speed" bar in the client
    mana: float = 0
    magiclvl: float = 0
    brains: float = 0
    sword: float = 0
    shield: float = 0
    level: float = 0
    ring: bool = False

    def might(self, strength_spell=0.0, ring_in_use=False):
        m = self.strength * (1 + math.sqrt(max(0.0, self.sword)) * SWORDPOWER) + strength_spell
        if ring_in_use:
            m *= 2
        return m


@dataclass
class Monster:
    name: str = "?"
    strength: float = 0
    brains: float = 0
    speed: float = 0
    energy: float = 0
    shield: float = 0
    experience: float = 0
    specials: tuple = ()

    def spell_immune(self):
        return bool(set(self.specials) & SPELL_IMMUNE)


def _hit_monster(mon, inflict):
    """Damage eats shield first, then energy. (Do_hitmonster)"""
    mon.shield -= inflict
    if mon.shield < 0:
        mon.energy += mon.shield
        mon.shield = 0.0


def _monster_hits(p_energy, mon):
    """inflict = floor(1 + rnd*strength), capped at player energy."""
    inflict = math.floor(1.0 + random.random() * mon.strength)
    return min(inflict, p_energy)


def simulate(player, monster, action, bolt_mana=None, trials=4000, max_rounds=200):
    """
    Run `action` as the opening/repeated tactic and play the fight out.
    Returns win rate, expected energy lost, expected mana spent, mean rounds.
    """
    wins = 0
    tot_energy_lost = 0.0
    tot_mana = 0.0
    tot_rounds = 0
    fled = 0

    for _ in range(trials):
        p_energy = player.energy
        p_mana = player.mana
        mon = replace(monster)
        melee_dmg = 0.0
        skirm_dmg = 0.0
        mon_max_str = mon.strength if mon.strength else 1.0
        mon_max_spd = mon.speed if mon.speed else 1.0
        mon_max_en = mon.energy if mon.energy else 1.0
        rounds = 0
        aon_used = False
        luckout_used = False

        while rounds < max_rounds:
            rounds += 1
            act = action

            # --- player acts ---
            if act == "melee":
                might = player.might()
                inflict = math.floor((0.5 + 1.3 * random.random()) * might)
                if "SS" in mon.specials:
                    inflict *= 0.5
                melee_dmg += inflict
                mon.strength = mon_max_str - (melee_dmg / mon_max_en) * (mon_max_str / 3.0)
                mon.strength = max(0.0, mon.strength)
                _hit_monster(mon, inflict)

            elif act == "skirmish":
                might = player.might()
                inflict = math.floor((0.33 + 1.1 * random.random()) * might)
                if "SS" in mon.specials:
                    inflict *= 0.5
                skirm_dmg += inflict
                mon.speed = mon_max_spd - (skirm_dmg / mon_max_en) * (mon_max_spd / 3.0)
                mon.speed = max(0.0, mon.speed)
                _hit_monster(mon, inflict)

            elif act == "bolt":
                if mon.spell_immune():
                    break
                spend = bolt_mana if bolt_mana else p_mana
                spend = min(spend, p_mana)
                if spend <= 0:
                    # out of fuel: fall back to melee
                    might = player.might()
                    inflict = math.floor((0.5 + 1.3 * random.random()) * might)
                    _hit_monster(mon, inflict)
                else:
                    p_mana -= spend
                    inflict = math.floor(
                        spend * (0.7 + 0.6 * random.random())
                        * (player.magiclvl ** 0.40 + 1)
                    )
                    if "Mr" in mon.specials:
                        inflict *= 0.5
                    _hit_monster(mon, inflict)

            elif act == "aon":
                if mon.spell_immune():
                    break
                if not aon_used:
                    aon_used = True
                    if p_mana >= MM_AON:
                        p_mana -= MM_AON
                    chance = (3000.0 * player.magiclvl /
                              (mon.experience + 10000.0 * player.magiclvl))
                    if random.random() < chance:
                        _hit_monster(mon, mon.energy + mon.shield + 1.0)
                    else:
                        # failure: monster doubles strength and speed
                        mon.strength *= 2.0
                        mon_max_str = mon.strength
                        mon.speed = mon.speed * 2.0 if mon.speed * 2.0 > 1.0 else mon.speed + 1
                        mon_max_spd = mon.speed
                else:
                    # after the gamble resolves, finish with the best follow-up
                    if p_mana > 1 and player.magiclvl >= ML_BOLT:
                        spend = p_mana
                        p_mana = 0
                        inflict = math.floor(
                            spend * (0.7 + 0.6 * random.random())
                            * (player.magiclvl ** 0.40 + 1)
                        )
                        _hit_monster(mon, inflict)
                    else:
                        might = player.might()
                        inflict = math.floor((0.5 + 1.3 * random.random()) * might)
                        melee_dmg += inflict
                        mon.strength = max(0.0, mon_max_str -
                                           (melee_dmg / mon_max_en) * (mon_max_str / 3.0))
                        _hit_monster(mon, inflict)

            elif act == "luckout":
                # brains contest, free, ONCE per fight, instant kill on success.
                #   fail if  rnd()*player.brains < rnd()*monster.brains
                # Dark Lord always fails. Morgoth uses sin instead (ally).
                if not luckout_used:
                    luckout_used = True
                    if "DL" not in mon.specials:
                        if (random.random() * player.brains
                                >= random.random() * mon.brains):
                            mon.energy = 0.0
                            wins += 1
                            tot_energy_lost += (player.energy - p_energy)
                            tot_mana += (player.mana - p_mana)
                            tot_rounds += rounds
                            break
                    # miss: fall through, monster gets a free swing
                else:
                    # already spent it - finish with melee
                    might = player.might()
                    inflict = math.floor((0.5 + 1.3 * random.random()) * might)
                    melee_dmg += inflict
                    mon.strength = max(0.0, mon_max_str -
                                       (melee_dmg / mon_max_en) * (mon_max_str / 3.0))
                    _hit_monster(mon, inflict)

            elif act == "evade":
                if set(mon.specials) & ALWAYS_FLEE:
                    fled += 1
                    break
                mimic_block = "Mi" in mon.specials and random.random() >= 0.05
                if not mimic_block and (
                    random.random() * (player.quickness * 1.1 + 1) * (player.brains + 1)
                    > random.random() * (mon.speed * 1.1 + 1) * (mon.brains + 1)
                ):
                    fled += 1
                    break
                # failed to flee -> monster gets a free swing

            if mon.energy <= 0:
                wins += 1
                break

            # --- monster acts ---
            dmg = _monster_hits(p_energy, mon)
            p_energy -= dmg
            if p_energy <= 0:
                break

        tot_energy_lost += (player.energy - max(0.0, p_energy))
        tot_mana += (player.mana - p_mana)
        tot_rounds += rounds

    return {
        "action": action,
        "bolt_mana": bolt_mana,
        "win_rate": wins / trials,
        "flee_rate": fled / trials,
        "death_rate": 1.0 - (wins / trials) - (fled / trials),
        "avg_energy_lost": tot_energy_lost / trials,
        "avg_mana_spent": tot_mana / trials,
        "avg_rounds": tot_rounds / trials,
    }


def aon_chance(player, monster):
    """Raw All-or-Nothing success probability."""
    if monster.experience <= 0 or player.magiclvl <= 0:
        return 0.0
    return min(1.0, 3000.0 * player.magiclvl /
               (monster.experience + 10000.0 * player.magiclvl))


def advise(player, monster, trials=3000):
    """Evaluate every legal action and rank them."""
    results = []
    immune = monster.spell_immune()

    results.append(simulate(player, monster, "melee", trials=trials))
    results.append(simulate(player, monster, "skirmish", trials=trials))
    results.append(simulate(player, monster, "evade", trials=trials))

    # luckout: free brains contest, once per fight, instant kill.
    # Not offered vs Morgoth (becomes "Ally", a sin check instead).
    if "Mo" not in monster.specials:
        results.append(simulate(player, monster, "luckout", trials=trials))

    if not immune and player.magiclvl >= ML_AON and player.mana >= 1:
        results.append(simulate(player, monster, "aon", trials=trials))

    if not immune and player.magiclvl >= ML_BOLT and player.mana > 0:
        # Solve for the EXACT mana needed rather than guessing at fractions.
        plan = bolt_plan(player, monster)
        if plan:
            cands = []
            if plan["guaranteed_ok"]:
                cands.append(plan["guaranteed"])      # always kills
            if plan["coinflip_ok"] and plan["coinflip"] != plan["guaranteed"]:
                cands.append(plan["coinflip"])        # ~50%, cheaper
            if not cands:
                cands.append(int(player.mana))        # can't afford: dump it all
            for spend in cands:
                if spend > 0:
                    results.append(
                        simulate(player, monster, "bolt", bolt_mana=spend,
                                 trials=trials)
                    )

    # rank: survival first, then cheapness
    results.sort(key=lambda r: (-r["win_rate"], r["avg_energy_lost"], r["avg_mana_spent"]))
    return results


def luckout_chance(player, monster):
    """
    P(rnd()*brains_p >= rnd()*brains_m) for independent uniforms.
    Let a = player.brains, b = monster.brains.
      If a >= b:  P = 1 - b/(2a)
      If a <  b:  P = a/(2b)
    Dark Lord always fails.
    """
    if "DL" in monster.specials:
        return 0.0
    a = max(0.0, player.brains)
    b = max(0.0, monster.brains)
    if b <= 0 or a <= 0:
        # Bad data (unmatched monster, or brains never scraped).
        # Every real monster has >0 brains, so this is NOT a free kill.
        return -1.0        # sentinel: "unknown", caller must not claim 100%
    if a >= b:
        return 1.0 - b / (2.0 * a)
    return a / (2.0 * b)


def bolt_mana_for_kill(player, monster, confidence=1.0):
    """
    Exact mana needed to one-shot a monster with Magic Bolt.

        damage = mana * (0.7 + 0.6*r) * (ML**0.4 + 1)      r ~ U(0,1)

    The roll multiplier spans 0.7 (worst) to 1.3 (best), so:

      confidence=1.0  -> guaranteed kill even on the worst roll (mult 0.7)
      confidence=0.5  -> kills on an average roll (mult 1.0); fails ~half the time
      confidence=0.9  -> kills on all but the worst 10% of rolls (mult 0.76)

    In general the multiplier at a given confidence c is:
        mult = 0.7 + 0.6 * (1 - c)

    Returns None if the player can't cast bolt or lacks the mana.
    """
    if player.magiclvl < ML_BOLT:
        return None
    if monster.spell_immune() or "DL" in monster.specials:
        return None

    hp = monster.energy + monster.shield
    if hp <= 0:
        return None

    scale = player.magiclvl ** 0.40 + 1.0
    if "Mr" in monster.specials:      # magic resistant: halve it
        scale *= 0.5

    mult = 0.7 + 0.6 * (1.0 - max(0.0, min(1.0, confidence)))
    need = math.ceil(hp / (mult * scale))

    return {
        "mana": need,
        "affordable": need <= player.mana,
        "shortfall": max(0, need - int(player.mana)),
        "confidence": confidence,
        # what your full mana bar would do, worst-case and average
        "max_worst": math.floor(player.mana * 0.7 * scale),
        "max_avg": math.floor(player.mana * 1.0 * scale),
    }


def bolt_plan(player, monster):
    """
    Practical bolt advice: the guaranteed number, the coin-flip number,
    and whether you can afford either.
    """
    sure = bolt_mana_for_kill(player, monster, 1.0)
    if sure is None:
        return None
    avg = bolt_mana_for_kill(player, monster, 0.5)
    return {
        "guaranteed": sure["mana"],
        "guaranteed_ok": sure["affordable"],
        "coinflip": avg["mana"],
        "coinflip_ok": avg["affordable"],
        "shortfall": sure["shortfall"],
        "max_worst": sure["max_worst"],
        "overkill": max(0, int(player.mana) - sure["mana"]),
    }
