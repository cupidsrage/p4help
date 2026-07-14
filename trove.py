"""
Treasure trove navigation.

Scrolls give you a leg of a trail:

    "It says, 'To find me treasure trove, ye must move 1658 squares to the
     north-east and then look for me next map."

"look for me next map" = this is ONE HOP of a multi-hop chain. Reach the target,
find the next scroll, repeat. Lose a leg and the whole trail is dead — which is
why this is worth tracking rather than doing in your head.

Worth chasing: Do_treasure_trove() awards a BLESSING if you don't have one
(plus gems, and charms if you do). A Blessing is required for the Dark Lord.

NOTE: the trove chain is NOT in the 4.03 source in a form we can fully verify —
the scroll text is a Phantasia 5 presentation. So distances/directions here are
parsed from the message, not ported. If a scroll's wording differs, tell me.
"""

import re
import math

# Screen/compass convention in Phantasia: x = east(+)/west(-), y = north(+)/south(-)
DIRECTIONS = {
    "north":      (0, 1),
    "south":      (0, -1),
    "east":       (1, 0),
    "west":       (-1, 0),
    "north-east": (1, 1),
    "north-west": (-1, 1),
    "south-east": (1, -1),
    "south-west": (-1, -1),
    "northeast":  (1, 1),
    "northwest":  (-1, 1),
    "southeast":  (1, -1),
    "southwest":  (-1, -1),
}

# how the game names the move buttons
COMPASS = {
    (0, 1): "N", (0, -1): "S", (1, 0): "E", (-1, 0): "W",
    (1, 1): "NE", (-1, 1): "NW", (1, -1): "SE", (-1, -1): "SW",
}


class Trove:
    """One leg of the trove trail."""

    def __init__(self, distance, direction, origin=None, raw=""):
        self.distance = distance
        self.direction = direction          # e.g. "north-east"
        self.vec = DIRECTIONS.get(direction, (0, 0))
        self.origin = origin                # (x, y) where you read the scroll
        self.raw = raw
        self.leg = 1

    def target(self):
        """Where the trove should be, from where the scroll was read."""
        if not self.origin:
            return None
        dx, dy = self.vec
        # A diagonal move covers 1 square in each axis per step.
        return (self.origin[0] + dx * self.distance,
                self.origin[1] + dy * self.distance)

    def remaining(self, here):
        """How far you still have to go from your current position."""
        t = self.target()
        if not t or not here:
            return None
        rx = t[0] - here[0]
        ry = t[1] - here[1]
        # Chebyshev: diagonal moves cover both axes at once, so the number of
        # moves is the LARGER of the two gaps, not their sum.
        steps = max(abs(rx), abs(ry))
        return {
            "dx": rx,
            "dy": ry,
            "steps": steps,
            "heading": self.heading(rx, ry),
            "arrived": steps == 0,
        }

    @staticmethod
    def heading(rx, ry):
        """Which button to press to close the gap."""
        sx = (rx > 0) - (rx < 0)
        sy = (ry > 0) - (ry < 0)
        return COMPASS.get((sx, sy), "-")

    def to_dict(self, here=None):
        t = self.target()
        rem = self.remaining(here) if here else None
        return {
            "distance": self.distance,
            "direction": self.direction,
            "origin": self.origin,
            "target": t,
            "leg": self.leg,
            "remaining": rem,
            "raw": self.raw,
        }


SCROLL_RE = re.compile(
    r"move\s+([\d,]+)\s+squares?\s+to\s+the\s+"
    r"(north-?east|north-?west|south-?east|south-?west|north|south|east|west)",
    re.I,
)


def parse_scroll(text, origin=None):
    """
    Pull a trove leg out of scroll text. Returns a Trove or None.

        "ye must move 1658 squares to the north-east and then look for me next map"
    """
    m = SCROLL_RE.search(text)
    if not m:
        return None
    dist = int(m.group(1).replace(",", ""))
    direction = m.group(2).lower()
    if "-" not in direction and len(direction) > 5:
        # normalise "northeast" -> "north-east"
        for a in ("north", "south"):
            for b in ("east", "west"):
                if direction == a + b:
                    direction = f"{a}-{b}"
    return Trove(dist, direction, origin, text.strip())


# The scroll points AT THE TROVE (treasure.c:1587), not at another scroll.
# But the distance is deliberately fudged +/-12.5% (treasure.c:1584):
#     dtemp = floor(dtemp * (.875 + RND() * .25) + .01)
# The DIRECTION is exact. So: walk the bearing, read another scroll, and
# triangulate. Two readings from different spots pin it down closely.
FUDGE_LO, FUDGE_HI = 0.875, 1.125

SCROLL_MARKERS = ("to find me treasure trove", "ye must move",
                  "look for me next map")
CLOSE_MARKERS = ("you're almost there", "the booty is 1 square")
DIG_MARKERS = ("you've found the treasure", "dig matey")
FOUND_MARKERS = ("you have found a treasure trove",
                 "unearths the treasure trove")


class TroveSolver:
    """
    Triangulate the trove from multiple scroll readings.

    Each scroll gives an EXACT direction but a distance that's randomly scrambled
    by +/-12.5%. One reading defines an annulus (a fuzzy ring). Two readings from
    different positions intersect to pin the trove down.

    Add readings as you find scrolls; estimate() gets steadily sharper.
    """

    def __init__(self):
        self.readings = []      # (x, y, stated_distance, direction_vec)

    def add(self, x, y, distance, direction):
        vec = DIRECTIONS.get(direction, (0, 0))
        self.readings.append((x, y, float(distance), vec))

    def true_range(self, stated):
        """The real distance is within this band."""
        return (stated / FUDGE_HI, stated / FUDGE_LO)

    def estimate(self, step=8, span=1600):
        """
        Grid-search the position that best fits every reading.
        Returns (x, y, error_estimate) or None.
        """
        if not self.readings:
            return None

        best = None
        for gx in range(-span, span + 1, step):
            for gy in range(-span, span + 1, step):
                # trove is always >= 600 from origin (init.c:Do_hide_trove)
                if math.hypot(gx, gy) < 600 or math.hypot(gx, gy) > 1400:
                    continue
                err = 0.0
                for (px, py, sd, vec) in self.readings:
                    d = math.hypot(gx - px, gy - py)
                    # distance error, allowing for the fudge band
                    lo, hi = self.true_range(sd)
                    if d < lo:
                        err += (lo - d) ** 2
                    elif d > hi:
                        err += (d - hi) ** 2
                    # direction must roughly agree (it's exact in-game)
                    if vec != (0, 0):
                        want = math.atan2(vec[1], vec[0])
                        got = math.atan2(gy - py, gx - px)
                        da = abs((want - got + math.pi) % (2 * math.pi) - math.pi)
                        err += (da * 200.0) ** 2
                if best is None or err < best[0]:
                    best = (err, gx, gy)

        if not best:
            return None
        _, bx, by = best
        # rough uncertainty: half the width of the tightest fudge band
        unc = min((self.true_range(sd)[1] - self.true_range(sd)[0]) / 2
                  for *_, sd, _ in [(0, 0, r[2], 0) for r in self.readings])
        return {"x": bx, "y": by, "uncertainty": unc,
                "readings": len(self.readings)}


class TroveSearch:
    """
    The endgame: triangulation gets you within ~50 squares, but the trove is ONE
    exact square (misc.c: object->x == player.x && object->y == player.y -- no
    proximity radius, no dig command). So you have to WALK the search box.

    This tracks every square you've stood on, so you can sweep systematically
    instead of wandering and re-covering ground.
    """

    def __init__(self, cx, cy, radius):
        self.cx = int(cx)
        self.cy = int(cy)
        self.radius = int(radius)
        self.visited = set()          # (x, y) squares you've stood on
        self.ruled_out = set()        # squares walked with no trove

    def recenter(self, cx, cy, radius):
        """New triangulation came in -- move the box, keep the visited squares."""
        self.cx, self.cy, self.radius = int(cx), int(cy), int(radius)

    def visit(self, x, y):
        p = (int(x), int(y))
        self.visited.add(p)
        if self.in_box(*p):
            self.ruled_out.add(p)

    def in_box(self, x, y):
        return (abs(x - self.cx) <= self.radius
                and abs(y - self.cy) <= self.radius)

    def bounds(self):
        return (self.cx - self.radius, self.cy - self.radius,
                self.cx + self.radius, self.cy + self.radius)

    def total_squares(self):
        n = 2 * self.radius + 1
        return n * n

    def covered(self):
        return len(self.ruled_out)

    def progress(self):
        t = self.total_squares()
        return self.covered() / t if t else 0.0

    def next_target(self, here):
        """
        Where to walk next: the nearest unsearched square, preferring a
        boustrophedon (snake) sweep so you don't zigzag pointlessly.
        """
        if not here:
            return None
        hx, hy = int(here[0]), int(here[1])
        x0, y0, x1, y1 = self.bounds()

        best = None
        for y in range(y0, y1 + 1):
            # snake: alternate direction each row so you don't walk back
            rng = range(x0, x1 + 1) if (y - y0) % 2 == 0 else range(x1, x0 - 1, -1)
            for x in rng:
                if (x, y) in self.ruled_out:
                    continue
                d = max(abs(x - hx), abs(y - hy))     # chebyshev = moves needed
                if best is None or d < best[0]:
                    best = (d, x, y)
        if not best:
            return None
        d, x, y = best
        dx, dy = x - hx, y - hy
        return {"x": x, "y": y, "steps": d,
                "heading": Trove.heading(dx, dy)}

    def grid(self, here=None, cells=21):
        """
        A coarse map of the search box for display: `cells` x `cells` tiles,
        each marked searched / unsearched / you-are-here.
        """
        x0, y0, x1, y1 = self.bounds()
        span = max(1, x1 - x0)
        step = max(1, span // cells)

        rows = []
        for gy in range(y1, y0 - 1, -step):          # north at top
            row = []
            for gx in range(x0, x1 + 1, step):
                # is this tile fully walked?
                walked = any((x, y) in self.ruled_out
                             for y in range(gy, min(gy + step, y1 + 1))
                             for x in range(gx, min(gx + step, x1 + 1)))
                is_here = False
                if here:
                    hx, hy = int(here[0]), int(here[1])
                    is_here = (gx <= hx < gx + step and gy <= hy < gy + step)
                row.append({"x": gx, "y": gy,
                            "walked": walked, "here": is_here})
            rows.append(row)
        return rows

    def to_dict(self, here=None):
        x0, y0, x1, y1 = self.bounds()
        return {
            "center": [self.cx, self.cy],
            "radius": self.radius,
            "bounds": [x0, y0, x1, y1],
            "total": self.total_squares(),
            "covered": self.covered(),
            "progress": self.progress(),
            "next": self.next_target(here),
            "grid": self.grid(here),
        }
