# Phantasia Advisor

A local desktop app that reads your live character off the wire and tells you
how to fight what's in front of you — with real probabilities computed from the
game's own combat code, not rules of thumb.

**Keep it to yourself.** The Phantasia calc policy explicitly allows *building*
tools like this. Distributing or advertising them is a bannable offense.
No repo, no Discord, no sharing in-realm.

---

## Run it (one window, game inside)

Double-click **Phantasia.bat** (or `pythonw launcher.py`).

One window opens. It starts the proxy and dashboard in the background, launches
the game client, and pulls the client **inside** the window, with the advisor
panel docked on the right. Close the window and everything shuts down.

- **First run:** it asks you to pick the game client (.exe, .jar, .bat or
  shortcut). The choice is saved to `launcher.json`. Change it with **Client…**.
- **Toolbar:** Relaunch game · Client… · Dashboard (full UI in the browser) ·
  Hide/Show advisor · **Auto-play**.
- **Auto-play:** the button grid under the advisor lights the move to make. Turn on
  **Auto-play** (toolbar, or click AUTO in the grid) and it presses that key in the
  game for you: combat moves, the Spell → Magic Bolt → mana steps, and "More"
  prompts. It only types while the launcher window is in front, so alt-tabbing
  away pauses it. It's off by default and remembers your choice.
- **Auto-reroll (new characters):** pick your class, and on the "keep these
  stats?" screen auto-play rerolls until it gets a roll in about the top 1-in-100
  for that class (ranges from phantasia5.com/stats), then presses Keep. The grid
  shows each roll's score and the bar. Change how picky it is with
  `"reroll_target"` in `launcher.json` (default 100; 300 = pickier and slower), and
  what counts as good with `"reroll_weights"`, e.g. `{"Halfling": {"brains": 5}}`.
  A roll is **never** kept unless its speed is the class maximum (Halfling 35,
  Fighter 37, Elf 40, ...); set other must-max stats with `"reroll_require_max"`.
- **Recording (for teaching auto-play):** while **● Recording** is on, the launcher
  saves every screen you answer and what you pressed, plus your stats, the fight and
  the advisor's pick, to `recordings/` (one `.jsonl` dataset + readable `.txt` per
  game). Passwords are blanked. Auto-play's own presses are labelled so they're
  kept apart from yours. Run `python analyze.py` to see what you usually press on
  each screen and where you overrule the advisor. It also writes
  `recordings/policy.json` for a smarter auto-play to use. Set
  `"record_screenshots": true` in `launcher.json` to also save a picture of the
  game for every input (needs `pip install pillow`). Without the launcher:
  `python app.py -h 178.63.136.86 --record`.
- **Want a real app?** Run `build_exe.bat` once (needs `pip install pyinstaller`)
  to get `dist\Phantasia.exe`. Pin it or put it on your desktop.
- The **hosts-file entry below is still needed**. The launcher warns you if it's missing.
- Embedding the client needs Windows. Elsewhere, the client runs in its own window
  next to the advisor.

`launcher.json` options: `client`, `client_args`, `server` (upstream IP),
`window_title` (set this to part of the client's title bar text if the launcher
can't find its window), `stretch` (fill the space or keep native size),
`embed_mode` (`"dock"`, the default, keeps the game its own window glued over
the game area so clicks work normally; `"reparent"` makes it a true child window,
which some clients won't take clicks in),
`panel_width`, `close_client_on_exit`, `autoplay`, `record`,
`record_screenshots`.

## Run it (proxy only)

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

Flags: `-h <ip>` upstream · `-w <port>` dashboard · `--log raw.txt` capture traffic ·
`--record [folder]` record screens + inputs for `analyze.py`

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

- `launcher.py` — the all-in-one app window (run this, or `Phantasia.bat`)
- `app.py` — proxy + web server + packet parser
- `reroll.py` — scores character rolls and decides Reroll/Keep
- `recorder.py` — records screens + your inputs; `analyze.py` — summarises them
- `ui.html` — the dashboard
- `combat.py` — combat engine ported from `fight.c`
- `monsters.py` — full monster table with size scaling and specials
