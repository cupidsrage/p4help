# Phantasia Advisor

A local desktop app that reads your live character off the wire and tells you
how to fight what's in front of you — with real probabilities computed from the
game's own combat code, not rules of thumb.

**Keep it to yourself.** The Phantasia calc policy explicitly allows *building*
tools like this. Distributing or advertising them is a bannable offense.
No repo, no Discord, no sharing in-realm.

---

## Run it

```
python app.py -h 178.63.136.86
```

That's it. The dashboard opens in your browser at `http://127.0.0.1:8420`.
Leave it on a second monitor while you play.

**Setup, once:** add to `C:\Windows\System32\drivers\etc\hosts` (Notepad as Admin):
```
127.0.0.1  phantasia5.com
```
If the game can't connect, run `ipconfig /flushdns`.

**In-game, once per login:** open **Info → Stats**.
Brains and Magic Level are *not* sent as packets — they only appear on that
screen. Luckout runs on brains, so without it the advisor is flying blind.

Flags: `-h <ip>` upstream · `-w <port>` dashboard · `--log raw.txt` capture traffic

---

## What's in it

**Fight** — live, per encounter. Big verdict up top (FREE KILL / risky / DON'T),
then every action ranked by win rate with expected energy and mana cost. Shows
the size scaling inline (`brains 37 → 111`) so you can see *why* a fight got hard.
Warns on Faeries, Regenerators, Stone Skin, Mana Drain, Head Hunters, and the rest.

**What-If** — pick any monster at any size, against your live stats or hypotheticals.
Answers "can I take a size-5 Troll?" before you meet one.

**Monsters** — the full table, sorted by luckout odds at your brains. Instantly
shows which monsters are free kills and which will eat you.

**Reference** — every combat formula, spell gate, and mana cost, straight from `fight.c`.

**Session** — fights, exp, energy lost, and your actual luckout record vs. predicted.

---

## The math (ported from `fight.c`, Phantasia 4.03)

```
might     = strength × (1 + √sword × 0.04)          [×2 with ring]
melee     = (0.5  + 1.3·r) × might     → degrades its STRENGTH
skirmish  = (0.33 + 1.1·r) × might     → degrades its SPEED
bolt      = mana × (0.7 + 0.6·r) × (ML^0.4 + 1)     [needs ML 5]
AoN       = 3000·ML / (exp + 10000·ML) → instant kill, 1 mana
            fail → its strength AND speed DOUBLE
luckout   = r·yourBrains ≥ r·itsBrains → instant kill, FREE, once per fight
evade     = r·(quick·1.1+1)·(brains+1) > r·(itsSpeed·1.1+1)·(itsBrains+1)
its hit   = 1 + r × itsStrength
```

Damage eats the monster's shield first, then energy.

### What this means for a low-level character

**Brains is your weapon.** Luckout is free, resolves in one round, and ignores
the monster's energy and strength entirely. A Troll's 370 energy and 112 strength
are simply irrelevant if you win the brains roll.

**Two random rolls means never a guarantee.** 84 vs 20 brains is 88%, not 100%.
A genuine 88% still loses one fight in eight. That's the game, not a bug.

**Size is the enemy of luckout.** Monster brains scale with size; yours don't.
But it's *base* brains that decide it — a size-3 Centipede (8→24) is still an
86% free kill, while a size-2 Sprite (37→74) already isn't. Watch the brainy ones:
Sprite, Naiad, Glaistig, Mermaid, Wraith, Thaumaturgist, Titan, Succubus.

**Sword is a weak knob.** Damage scales as `√sword × 0.04` — sword 100 is only
+40% might. Magic Level is the strong one, and **ML 5 unlocks Magic Bolt**, which
turns unspendable mana into hundreds of damage.

---

## Known gaps

- Version drift: server is 5.1.0-beta, source is 4.03 (2012). Skeleton matches;
  numbers may have been retuned. If predictions drift, calibrate against `--log`.
- Not modeled: Nick, Increase Might, Haste, Force Field, Paralyze, Transform,
  regeneration, and most special-monster behaviors beyond the warnings.
- Monster **shield** is assumed 0 (the server doesn't announce it).

## Files

- `app.py` — proxy + web server + packet parser (run this)
- `ui.html` — the dashboard
- `combat.py` — combat engine ported from `fight.c`
- `monsters.py` — full monster table with size scaling and specials
