# Standalone Overlay

A borderless, always-on-top window that floats over the game. No browser, no
PowerToys, no Node, no Electron. Just Python — tkinter ships with it.

The Phantasia client is never modified. The overlay is a separate window that
reads from the local proxy.

## Run

Double-click **start.bat**. It starts the proxy and the overlay together.
A small dark panel appears top-left.

- **Drag** the title bar to move it — park it beside the combat buttons.
- **F8** — hide / show.
- **Right-click** — opacity (100/90/75/60%) and close.
- **Esc** — close the overlay.

The panel shows: the one move to make, why, its win % and energy cost, the live
fight state (luckout spent? buffs up? monster hp?), and your energy/mana/brains/ML.
It updates every round as the proxy watches the fight.

Full dashboard (What-If, monster table, reference) is still at
http://127.0.0.1:8420 if you want it on a second monitor.

## In-game

Open **Info -> Stats** once per login — brains and Magic Level only appear there,
and luckout runs on brains.

## Making it a one-click .exe (optional)

If you'd rather not see a console window at all:

    pip install pyinstaller
    pyinstaller --noconsole --onefile overlay.py

That produces `dist\overlay.exe`. You'd still run the proxy (app.py) alongside it;
start.bat handles both.

Local only. Building this is fine per the calc policy; sharing it is not.
