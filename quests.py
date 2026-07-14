"""
Quest tracking.

NOTE: the quest system does NOT exist in the Phantasia 4.03 source — it was
added server-side in Phantasia 5. So unlike combat, none of this is ported;
it's all built from observed server text. The parser is deliberately generous
about wording, and unknown quest types are stored raw rather than dropped.

Observed so far:
    "Well, lets see what I've got for you..."
    Rerolls left: 10
    Kill 4 monsters of Treasure Type 2.
    Recieve 93 gold pieces.
    "It's settled then!  Good luck!"          <- accepted

Quests are picked up at trading posts.
"""

import re
import monsters as MDB


def monsters_of_treasure_type(tt):
    """Every monster with a given treasure type, easiest luckout first."""
    out = []
    for name, v in MDB.MONSTERS.items():
        if v[5] == tt:
            out.append({
                "name": name,
                "brains": v[1],      # low brains = easy luckout
                "energy": v[3],
                "strength": v[0],
                "experience": v[4],
                "flock": v[6],
                "specials": [s for s in v[7].split(",") if s],
            })
    out.sort(key=lambda m: m["brains"])
    return out


class Quest:
    def __init__(self, kind, need, target=None, reward_gold=0, raw=""):
        self.kind = kind            # 'treasure_type' | 'monster' | 'unknown'
        self.need = need
        self.done = 0
        self.target = target        # treasure type number, or monster name
        self.reward_gold = reward_gold
        self.raw = raw
        self.active = False

    @property
    def complete(self):
        return self.done >= self.need

    def credits(self, monster_name):
        """Does killing this monster count toward the quest?"""
        if self.kind == "treasure_type":
            v = MDB.MONSTERS.get(monster_name)
            return bool(v) and v[5] == self.target
        if self.kind == "monster":
            return monster_name.lower() == str(self.target).lower()
        return False

    def candidates(self):
        if self.kind == "treasure_type":
            return monsters_of_treasure_type(self.target)
        if self.kind == "monster":
            v = MDB.MONSTERS.get(self.target)
            if v:
                return [{"name": self.target, "brains": v[1], "energy": v[3],
                         "strength": v[0], "experience": v[4], "flock": v[6],
                         "specials": [s for s in v[7].split(",") if s]}]
        return []

    def describe(self):
        if self.kind == "treasure_type":
            return f"Kill {self.need} monsters of Treasure Type {self.target}"
        if self.kind == "monster":
            return f"Kill {self.need} \u00d7 {self.target}"
        return self.raw or "Unknown quest"

    def to_dict(self, player_brains=0):
        cands = self.candidates()
        for c in cands:
            b = c["brains"]
            if player_brains > 0 and b > 0:
                c["luckout"] = (1 - b / (2 * player_brains)
                                if player_brains >= b
                                else player_brains / (2 * b))
            else:
                c["luckout"] = -1
        return {
            "kind": self.kind,
            "text": self.describe(),
            "need": self.need,
            "done": self.done,
            "target": self.target,
            "reward_gold": self.reward_gold,
            "complete": self.complete,
            "active": self.active,
            "candidates": cands[:8],
        }


def is_quest_line(txt):
    """True if this single line states a quest objective."""
    low = txt.lower()
    return bool(re.search(r"kill\s+\d+\s+", low))


def parse_offer(lines):
    """
    Pull a quest out of a block of server text. Returns a Quest or None.

    IMPORTANT: on a REROLL the merchant reprints only the new objective — there
    is no fresh greeting. So we always parse the LAST quest line in the buffer,
    never the first, or a reroll would keep resolving to the original quest.

    Tolerant of wording variants; falls back to 'unknown' rather than dropping.
    """
    # find the most recent objective line
    idx = None
    for i in range(len(lines) - 1, -1, -1):
        if is_quest_line(lines[i]):
            idx = i
            break
    if idx is None:
        return None

    quest_line = lines[idx]
    # reward is stated after the objective
    tail = " ".join(lines[idx:])
    low_tail = tail.lower()

    reward = 0
    m = re.search(r"rec(?:ei|ie)ve\s+([\d,]+)\s+gold", low_tail)
    if m:
        reward = int(m.group(1).replace(",", ""))

    low = quest_line.lower()

    # "Kill 4 monsters of Treasure Type 2."
    m = re.search(r"kill\s+(\d+)\s+monsters?\s+of\s+treasure\s+type\s+(\d+)", low)
    if m:
        return Quest("treasure_type", int(m.group(1)), int(m.group(2)),
                     reward, quest_line)

    # "Kill 3 A Trolls." / "Kill 3 Trolls."
    m = re.search(r"kill\s+(\d+)\s+(.+?)[.\n]", quest_line, re.I)
    if m:
        need = int(m.group(1))
        who = m.group(2).strip()
        name = MDB.lookup(who)
        if name:
            return Quest("monster", need, name, reward, quest_line)
        return Quest("unknown", need, who, reward, quest_line)

    return None


def parse_rerolls(text):
    m = re.search(r"rerolls?\s+left\s*:?\s*(\d+)", text, re.I)
    return int(m.group(1)) if m else None


ACCEPT_MARKERS = ("it's settled then", "its settled then", "good luck")
COMPLETE_MARKERS = ("quest complete", "you have completed", "well done",
                    "you did it", "quest fulfilled")
