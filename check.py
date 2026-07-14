"""Diagnostic — run this in the same folder as app.py to see what's wrong."""
import os, sys, json, urllib.request

print("=" * 60)
print("PHANTASIA ADVISOR — DIAGNOSTIC")
print("=" * 60)

# 1. files present?
need = ["app.py", "combat.py", "planner.py", "monsters.py",
        "quests.py", "trove.py", "zones.py", "overlay.py", "ui.html"]
print("\n1. FILES:")
missing = []
for f in need:
    ok = os.path.exists(f)
    print(f"   {'OK ' if ok else 'MISSING'}  {f}")
    if not ok:
        missing.append(f)

# 2. is app.py the new one?
print("\n2. app.py VERSION:")
try:
    src = open("app.py", encoding="utf-8").read()
    checks = {
        "push_zone (Where to Hunt)": "def push_zone" in src,
        "import zones":              "import zones" in src,
        "BUTTONS fix":               "BUTTONS: 8" in src,
        "shrieker summon":           "shrieeeek" in src.lower(),
        "trove":                     "import trove" in src,
    }
    for k, v in checks.items():
        print(f"   {'OK ' if v else 'OLD/MISSING'}  {k}")
except OSError as e:
    print("   cannot read app.py:", e)

# 3. is ui.html the new one?
print("\n3. ui.html VERSION:")
try:
    h = open("ui.html", encoding="utf-8").read()
    print(f"   {'OK ' if 'drawHunt' in h else 'OLD'}  Where-to-Hunt tab")
    print(f"   {'OK ' if 'drawTrove' in h else 'OLD'}  Trove panel")
except OSError as e:
    print("   cannot read ui.html:", e)

# 4. what is the running proxy actually serving?
print("\n4. LIVE PROXY (http://127.0.0.1:8420/state):")
try:
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:8420/state",
                                          timeout=2).read())
    p = d.get("player") or {}
    print(f"   connected : {d.get('connected')}")
    print(f"   name      : {p.get('name') or '(none)'}")
    print(f"   brains    : {p.get('brains') or 'MISSING -> open Info->Stats'}")
    print(f"   strength  : {p.get('strength') or 'MISSING'}")
    print(f"   location  : {p.get('location') or '(none)'}")
    z = d.get("zone")
    if z:
        print(f"   ZONE      : OK — you're in circle {z.get('current')}, "
              f"best is {z['best']['circle'] if z.get('best') else '?'}")
    else:
        print("   ZONE      : NOT SET  <-- this is why the tab is empty")
        if not p.get("brains"):
            print("               cause: brains is 0. Open Info -> Stats in-game.")
        elif "def push_zone" not in open("app.py", encoding="utf-8").read():
            print("               cause: app.py is OLD. Replace it.")
        else:
            print("               cause: unclear — restart the proxy.")
except Exception as e:
    print(f"   proxy not responding ({e})")
    print("   -> is app.py running?")

if missing:
    print(f"\n!! MISSING FILES: {', '.join(missing)}")
print("\n" + "=" * 60)
