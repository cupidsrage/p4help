"""
Treasure trove navigation.

Scrolls give you a leg of a trail:

    "It says, 'To find me treasure trove, ye must move 1658 squares to the
     north-east and then look for me next map."

"look for me next map" just means "find another map to get closer": there is
one trove and every map points at it from where you read it.

Worth chasing: Do_treasure_trove() awards a BLESSING if you don't have one
(plus gems, and charms if you do). A Blessing is required for the Dark Lord.

Every map points at the SAME trove (treasure.c Do_treasure_map): read two
or three from different spots and the solver below narrows it to a handful
of squares. The rules are ported from the server source (see below).
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
        if dx == dy == 0:
            return None
        # the distance is straight-line; aim at the middle of the sector
        n = math.hypot(dx, dy)
        return (self.origin[0] + round(dx / n * self.distance),
                self.origin[1] + round(dy / n * self.distance))

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


# Straight from the server source:
#   * There is ONE trove in the realm. Every map points at it from where you
#     read it (treasure.c Do_treasure_map) -- "look for me next map" is not a
#     chain of hops, so every map you read narrows down the same spot.
#   * It's hidden 600..1400 squares from (0,0) (init.c Do_hide_trove).
#   * Stated distance = floor(true * (0.875 + RND*0.25) + 0.01): +/-12.5%.
#   * The direction is one of 8 compass SECTORS, each 45 degrees wide
#     (misc.c Do_direction) -- "north-east" means anywhere from 22.5 to 67.5
#     degrees, not exactly diagonal.
#   * You find it by standing on its exact square, NOT while cloaked, and not
#     on a square another player is standing on (misc.c Do_move).
#   * Maps only come from scrolls (treasure type 4 monsters), and only while
#     your level <= 100 - gems (treasure.c).
FUDGE_LO, FUDGE_HI = 0.875, 1.125
SWEEP_MAX = 20000          # start the on-foot sweep below this many squares
TROVE_MIN, TROVE_MAX = 600.0, 1401.0   # 1400 plus rounding

SCROLL_MARKERS = ("to find me treasure trove", "ye must move",
                  "look for me next map", "you're almost there",
                  "the booty is 1 square")
CLOSE_MARKERS = ("you're almost there", "the booty is 1 square")
DIG_MARKERS = ("you've found the treasure", "dig matey")
FOUND_MARKERS = ("you have found a treasure trove",
                 "unearths the treasure trove")

ONE_SQUARE_RE = re.compile(r"booty is 1 square\s+(north|south|east|west)", re.I)


def server_direction(dx, dy):
    """misc.c Do_direction, ported exactly: which word the game uses for a
    target dx east / dy north of you."""
    if dx == 0:
        if dy > 0:
            rad = 1.5708
        elif dy < 0:
            rad = 4.7124
        else:
            return "down"
    else:
        rad = math.atan(dy / dx)
        if dx < 0:
            rad += 3.1416
    if rad > 4.3197:
        return "south"
    if rad > 3.5343:
        return "south-west"
    if rad > 2.7489:
        return "west"
    if rad > 1.9635:
        return "north-west"
    if rad > 1.1781:
        return "north"
    if rad > .3927:
        return "north-east"
    if rad > -.3927:
        return "east"
    if rad > -1.1781:
        return "south-east"
    return "south"


def distance_band(stated):
    """True distances that can produce this stated number."""
    return ((stated - 0.01) / FUDGE_HI, (stated + 0.99) / FUDGE_LO)


class TroveSolver:
    """
    Every square the trove could still be on, given every map read so far.

    Each map allows a 45-degree wedge of a ring around where it was read
    (the distance is +/-12.5%, the direction a compass sector). The trove is
    on a square inside EVERY wedge and inside the 600..1400 ring around
    (0,0). The first map leaves tens of thousands of squares; each extra map
    read from a different spot cuts that down hard.
    """

    def __init__(self):
        self.readings = []      # (x, y, stated_distance, direction)
        self.cands = None       # set of (x, y) still possible
        self._est = None        # cached estimate (recomputed on change)

    def visit(self, x, y):
        """You stood here and found nothing: not the trove."""
        if self.cands and (int(x), int(y)) in self.cands:
            self.cands.discard((int(x), int(y)))
            if len(self.cands) < 5000:
                self._est = None

    def add(self, x, y, distance, direction):
        x, y, distance = int(x), int(y), float(distance)
        self.readings.append((x, y, distance, direction))
        self._est = None
        if self.cands is None:
            self.cands = self._wedge(x, y, distance, direction)
        else:
            self.cands = {c for c in self.cands
                          if self._fits(c, x, y, distance, direction)}

    def add_exact(self, x, y, direction):
        """'Arr, you're almost there. The booty is 1 square <dir>.'"""
        dx, dy = DIRECTIONS.get(direction, (0, 0))
        spot = (int(x) + dx, int(y) + dy)
        self.readings.append((int(x), int(y), 1.0, direction))
        self.cands = {spot}
        self._est = None

    @staticmethod
    def _fits(c, x, y, distance, direction):
        dx, dy = c[0] - x, c[1] - y
        lo, hi = distance_band(distance)
        if not lo <= math.hypot(dx, dy) <= hi:
            return False
        return server_direction(dx, dy) == direction

    def _wedge(self, x, y, distance, direction):
        lo, hi = distance_band(distance)
        out = set()
        lo2, hi2 = lo * lo, hi * hi
        tmin2, tmax2 = TROVE_MIN ** 2, TROVE_MAX ** 2
        for dx in range(-int(hi) - 1, int(hi) + 2):
            rem_hi = hi2 - dx * dx
            if rem_hi < 0:
                continue
            ymax = int(math.sqrt(rem_hi))
            rem_lo = lo2 - dx * dx
            ymin = int(math.ceil(math.sqrt(rem_lo))) if rem_lo > 0 else 0
            for sgn in (1, -1):
                for ady in range(ymin, ymax + 1):
                    dy = sgn * ady
                    if sgn < 0 and ady == 0:
                        continue
                    gx, gy = x + dx, y + dy
                    r2 = gx * gx + gy * gy
                    if r2 < tmin2 or r2 > tmax2:
                        continue
                    if server_direction(dx, dy) != direction:
                        continue
                    if lo2 <= dx * dx + dy * dy <= hi2:
                        out.add((gx, gy))
        return out

    def estimate(self, step=None):
        """Middle of what's left, how spread out it is, and how many squares.
        Returns None before the first map, or if the maps contradict."""
        if not self.cands:
            return None
        if self._est is not None:
            return self._est
        n = len(self.cands)
        cx = sum(c[0] for c in self.cands) / n
        cy = sum(c[1] for c in self.cands) / n
        # the real square nearest the middle (the middle of a wedge can be
        # outside it)
        bx, by = min(self.cands, key=lambda c: (c[0] - cx) ** 2 + (c[1] - cy) ** 2)
        unc = max(math.hypot(c[0] - bx, c[1] - by) for c in self.cands)
        self._est = {"x": bx, "y": by, "uncertainty": unc, "squares": n,
                     "readings": len(self.readings)}
        return self._est


class TroveSearch:
    """
    The trove is ONE exact square (misc.c: object x,y == player x,y -- no
    radius, no dig command), so once the maps have narrowed it down you walk
    the squares still possible. This ticks off every square you stand on and
    always points at the nearest one left.
    """

    def __init__(self, candidates):
        self.cands = set(candidates)
        self.ruled_out = set()

    def update(self, candidates):
        """A new map came in: keep only squares it still allows."""
        self.cands = set(candidates)
        self.ruled_out &= self.cands

    def visit(self, x, y):
        p = (int(x), int(y))
        if p in self.cands:
            self.ruled_out.add(p)

    def left(self):
        return self.cands - self.ruled_out

    def bounds(self):
        xs = [c[0] for c in self.cands] or [0]
        ys = [c[1] for c in self.cands] or [0]
        return (min(xs), min(ys), max(xs), max(ys))

    def total_squares(self):
        return len(self.cands)

    def covered(self):
        return len(self.ruled_out)

    def progress(self):
        t = self.total_squares()
        return self.covered() / t if t else 0.0

    def next_target(self, here):
        """Nearest square not stood on yet (moves count diagonals as 1)."""
        if not here:
            return None
        hx, hy = int(here[0]), int(here[1])
        left = self.left()
        if not left:
            return None
        x, y = min(left, key=lambda c: (max(abs(c[0] - hx), abs(c[1] - hy)),
                                        c[1], c[0]))
        return {"x": x, "y": y, "steps": max(abs(x - hx), abs(y - hy)),
                "heading": Trove.heading(x - hx, y - hy)}

    def grid(self, here=None, cells=21):
        """Coarse map for display. Each tile: walked (all its possible
        squares stood on), possible, or not part of the search."""
        if not self.cands:
            return []
        x0, y0, x1, y1 = self.bounds()
        step = max(1, (max(x1 - x0, y1 - y0) + cells) // cells)
        tiles = {}
        for c in self.cands:
            k = ((c[0] - x0) // step, (c[1] - y0) // step)
            t = tiles.setdefault(k, [0, 0])
            t[0] += 1
            if c in self.ruled_out:
                t[1] += 1
        hx = hy = None
        if here:
            hx, hy = int(here[0]), int(here[1])
        rows = []
        for ty in range((y1 - y0) // step, -1, -1):        # north at top
            row = []
            for tx in range((x1 - x0) // step + 1):
                gx, gy = x0 + tx * step, y0 + ty * step
                t = tiles.get((tx, ty))
                row.append({"x": gx, "y": gy,
                            "possible": bool(t),
                            "walked": bool(t) and t[1] >= t[0],
                            "here": hx is not None and gx <= hx < gx + step
                            and gy <= hy < gy + step})
            rows.append(row)
        return rows

    def to_dict(self, here=None):
        x0, y0, x1, y1 = self.bounds()
        return {
            "center": [(x0 + x1) // 2, (y0 + y1) // 2],
            "radius": max(x1 - x0, y1 - y0) // 2,
            "bounds": [x0, y0, x1, y1],
            "total": self.total_squares(),
            "covered": self.covered(),
            "progress": self.progress(),
            "next": self.next_target(here),
            "grid": self.grid(here),
        }
