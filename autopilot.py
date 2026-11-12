"""
Self-play: decides what to do outside of fights.

Fights, treasure, resting, fixed answers and More prompts are handled by
auto-play (buttons.py) as before. This adds the rest of the game:

    * walk to the hunting circle the zone finder picks (careful: the
      deepest circle where you survive 20 encounters >= `risk` of the time)
    * Hunt there (one monster per turn; walking only finds one on ~20% of
      turns, and every turn ages you)
    * go to a trading post and buy what makes you safest (shop.best_buys),
      never spending more in one visit than the merchant's thief limit
    * buy mana back up to a reserve, at the price the post shows
    * answer the medic, and Wayland Smith

Everything here follows the server source (main.c, misc.c, commands.c,
treasure.c); see rules.py for the formulas.

decide(d) takes the proxy's /state and returns one action:
    {"type": "button", "label": "Hunt"}           press the button with this label
    {"type": "compass", "dir": "NE"}              numeric keypad step
    {"type": "moveto", "x": 100, "y": 100}         Move To dialog, exact square
    {"type": "type", "text": "250"}               answer a typed dialog
    {"type": "wait"}                              nothing to do right now
each with a "why" for the panel. None = not my screen (fights etc.).
"""

import math
import re
import time

import rules as RU

SHOP_ITEM_DIALOG = {          # dialog text -> item it asks about
    "mana is one per": "Mana", "how strong a shield": "Shield",
    "a book costs": "Book", "an amulet costs": "Amulet",
    "how strong a sword": "Sword", "quicksilver is": "Quicksilver",
}
MIN_RISK_CUT = 0.002          # a purchase must cut session death risk this much
COMPASS_FAILS = 3             # compass presses with no move -> use Move To


def _labels(d):
    return [str(b).strip().lower() for b in (d.get("buttons") or [])]


def main_menu(labels):
    return "rest" in labels and "move to" in labels


class Autopilot:
    def __init__(self, risk=0.95, mana_reserve=None, shop=True):
        self.risk = risk                  # min chance to survive 20 encounters
        self.mana_reserve = mana_reserve  # None: 100 + 20 x level
        self.shop = shop
        self.plan = None                  # shopping plan for the next post visit
        self.spent = 0.0                  # gold spent this post visit
        self.prices = {}                  # item -> price, read off the post screen
        self.moveto = None                # (x, y) waiting for the coords dialog
        self.compass_fails = 0
        self.use_moveto = False           # compass doesn't work in this client
        self._pending = None              # (kind, xy before, time): a move in flight
        self.status = ""

    # ------------------------------------------------------------ helpers
    def target_circle(self, d):
        """Deepest circle you survive 20 encounters in at least `risk` of
        the time (zone finder rows), or None before the zone calc ran."""
        rows = ((d.get("zone") or {}).get("rows")) or []
        ok = [r["circle"] for r in rows if r.get("p_survive_session", 0) >= self.risk
              and r["circle"] < 20]          # marshes and beyond: never by default
        return max(ok) if ok else (1 if rows else None)

    def hunt_spot(self, x, y, circle):
        """A square in the middle of `circle`, on the line from the origin
        through where you are (so going in or out is a straight walk)."""
        r = (circle - 0.5) * RU.D_CIRCLE
        a = math.atan2(y, x) if (x or y) else math.pi / 4
        return int(round(r * math.cos(a))), int(round(r * math.sin(a)))

    def price(self, item, level):
        if item in self.prices:
            return self.prices[item]
        return {"Mana": 1, "Shield": 5, "Book": 100, "Sword": 500,
                "Quicksilver": 2000, "Amulet": 250 + math.floor(level / 5)}.get(item, 1e9)

    def mana_target(self, level):
        return self.mana_reserve if self.mana_reserve is not None else 100 + 20 * level

    # ------------------------------------------------------------ shopping
    def make_plan(self, p, x, y, hunt_circle):
        """What to buy next and where, or None. Gear first (shop advice:
        the biggest cut in death risk per visit), then mana back to the
        reserve with what's left under the thief limit."""
        gold, level = p.get("gold") or 0, p.get("level") or 0
        cap = min(gold, RU.thief_cap(level) - self.spent - 1)
        if cap <= 0:
            return None
        safe = max(2, (hunt_circle or 1))
        buys = []
        for b in (p.get("shop") or []):
            if b.get("risk_cut", 0) < MIN_RISK_CUT or b["item"] not in RU.SHOP_ITEMS:
                continue
            post = RU.nearest_post(x, y, b["item"], max_circle=safe)
            if not post:
                continue
            price = self.price(b["item"], level)
            # what this visit can afford under the cap (replacement items: the
            # new value must beat what you have)
            n = math.floor(cap / price)
            have = {"Shield": p.get("shield") or 0, "Sword": p.get("sword") or 0,
                    "Quicksilver": p.get("quicksilver") or 0}.get(b["item"])
            if have is not None and n <= have:
                continue
            if n <= 0:
                continue
            buys.append({"item": b["item"], "n": n, "post": post, "why": b.get("effect", ""),
                         "risk_cut": b["risk_cut"]})
        mana, want = p.get("mana") or 0, self.mana_target(level)
        if not buys and mana < 0.5 * want:
            post = RU.nearest_post(x, y, "Mana", max_circle=safe)
            n = int(min(want - mana, math.floor(cap / self.price("Mana", level))))
            if post and n >= 20:
                buys.append({"item": "Mana", "n": n, "post": post,
                             "why": f"mana {mana:.0f} -> {mana + n:.0f}", "risk_cut": 0})
        if not buys:
            return None
        return max(buys, key=lambda b: b["risk_cut"])

    # ------------------------------------------------------------ decide
    def decide(self, d):
        p = d.get("player") or {}
        labels = _labels(d)
        dialog = d.get("dialog") or None
        prompt = (d.get("prompt") or "").lower()
        x, y = p.get("x"), p.get("y")
        level = p.get("level") or 0
        self.prices.update(d.get("post_prices") or {})
        if d.get("fight") and (d["fight"] or {}).get("name"):
            return None                               # fights: auto-play

        # ---- typed answers ----
        if dialog:
            text = (dialog.get("text") or "").lower()
            kind = dialog.get("kind")
            if kind == "coords" and self.moveto:
                mx, my = self.moveto
                self.moveto = None
                self._pending = ("moveto", (x, y), time.monotonic())
                return {"type": "coords", "x": mx, "y": my,
                        "why": f"Move To ({mx}, {my})"}
            for key, item in SHOP_ITEM_DIALOG.items():
                if key in text:
                    n = self.plan["n"] if self.plan and self.plan["item"] == item else 0
                    if n:
                        self.spent += n * self.price(item, level)
                    self.plan = None if n else self.plan
                    return {"type": "type", "text": str(int(n)),
                            "why": f"buy {item} {n}" if n else f"not buying {item}"}
            if "offer to be cured" in text or "heal your blindness" in text:
                offer = RU.medic_offer(p.get("gold") or 0, level,
                                       p.get("poison") or 0, "blindness" in text)
                return {"type": "type", "text": str(int(offer or 0)),
                        "why": f"medic: offering {int(offer or 0)} gold"}
            if "how many do you want to sell" in text:
                return {"type": "type", "text": "0", "why": "keeping gems"}
            return None                               # bolt mana etc.: auto-play

        # ---- trading post screens ----
        if "purchase" in labels and "leave" in labels:
            if self.plan:
                return {"type": "button", "label": "Purchase",
                        "why": f"buying {self.plan['item']} ({self.plan['why']})"}
            self.spent = 0.0                           # the thief limit is per visit
            return {"type": "button", "label": "Leave", "why": "done shopping"}
        if "what would you like to buy" in prompt or ("cancel" in labels and "mana" in labels):
            if self.plan and self.plan["item"].lower() in labels:
                return {"type": "button", "label": self.plan["item"],
                        "why": f"{self.plan['item']} x{self.plan['n']}"}
            return {"type": "button", "label": "Cancel", "why": "nothing to buy"}
        if "wayland smith" in prompt or ("cancel" in labels and "shield" in labels
                                         and "mana" not in labels):
            for item in ("shield", "sword", "quicksilver"):
                if item in labels:
                    return {"type": "button", "label": item.title(),
                            "why": f"Wayland Smith: improve {item}"}

        if not main_menu(labels) or x is None or y is None:
            return None

        # ---- a move in flight: wait for the new location ----
        if self._pending:
            kind, before, at = self._pending
            if (x, y) != before:
                if kind == "compass":
                    self.compass_fails = 0
                self._pending = None
            elif time.monotonic() - at > 2.5:
                self._pending = None
                if kind == "compass":
                    self.compass_fails += 1
                    if self.compass_fails >= COMPASS_FAILS:
                        self.use_moveto = True   # keypad not working: use Move To
            else:
                return {"type": "wait", "why": "moving..."}

        if p.get("blind"):
            return {"type": "button", "label": "Rest",
                    "why": "blind: resting and waiting for a medic (or sight to return)"}

        hunt = self.target_circle(d)
        here = RU.circle_of(x, y)

        # ---- shopping ----
        if self.shop and self.plan is None:
            self.plan = self.make_plan(p, x, y, hunt)
        if self.plan:
            post = self.plan["post"]
            if (x, y) == (post["x"], post["y"]):
                if "enter post" in labels:
                    return {"type": "button", "label": "Enter Post",
                            "why": f"shopping: {self.plan['item']}"}
                self.plan = None                       # no post here after all
            else:
                return self._go(x, y, post["x"], post["y"], level,
                                f"to the post at ({post['x']}, {post['y']}) for "
                                f"{self.plan['item']} ({self.plan['why']})")

        # ---- hunting circle ----
        if hunt is None:
            return {"type": "wait", "why": "waiting for the zone calculation"}
        if here != hunt:
            tx, ty = self.hunt_spot(x, y, hunt)
            return self._go(x, y, tx, ty, level, f"to circle {hunt} (now {here})")
        if "hunt" in labels:
            return {"type": "button", "label": "Hunt", "why": f"hunting in circle {here}"}
        if "enter post" in labels:                     # standing on a post: step off
            tx, ty = self.hunt_spot(x + 3, y, hunt)
            return self._go(x, y, tx, ty, level, "stepping off the post to hunt")
        return {"type": "wait", "why": "no Hunt button (cloaked?)"}

    def _go(self, x, y, tx, ty, level, why):
        step = RU.step_toward(x, y, tx, ty, level)
        if step is None:
            return {"type": "wait", "why": why}
        if step[0] == "moveto" or self.use_moveto:
            if step[0] != "moveto":
                _, tx, ty = step                       # compass broken: one step at a time
            self.moveto = (tx, ty)
            return {"type": "moveto", "x": tx, "y": ty, "why": why}
        return {"type": "compass", "dir": step[0], "why": why}

    def pressed(self, act, xy):
        """The buttons layer pressed `act`: remember the move in flight."""
        if act and act.get("type") == "compass":
            self._pending = ("compass", xy, time.monotonic())
