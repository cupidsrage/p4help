"""
Monster table for Phantasia.

Base stats as listed. In-game a monster's actual stats are scaled by its SIZE
(the circle it was encountered in):
    energy     *= size
    brains     *= size
    experience *= size
    strength   *= 1 + 0.5*(size-1)   [+50% per size over one]
    speed      rises very slowly with size
"""

# name -> (strength, brains, speed, energy, experience, treasure, flock%, special)
MONSTERS = {
    "A Water Leaper":      (12,   14,   16, 24,    59,    0,  62, ""),
    "A Leech":             (4,    19,   29, 30,    66,    0,  73, ""),
    "An Urisk":            (13,   30,   15, 46,    127,   1,  3,  ""),
    "Shellycoat":          (28,   21,   18, 63,    226,   2,  0,  ""),
    "A Naiad":             (21,   62,   27, 58,    378,   2,  11, ""),
    "A Nixie":             (22,   58,   28, 108,   604,   3,  6,  ""),
    "A Glaistig":          (21,   106,  25, 127,   1002,  3,  0,  ""),
    "A Mermaid":           (18,   116,  22, 108,   809,   3,  0,  ""),
    "A Merman":            (24,   115,  23, 109,   808,   4,  0,  ""),
    "A Siren":             (22,   128,  31, 89,    915,   4,  24, ""),
    "A Lamprey":           (14,   67,   33, 156,   1562,  4,  37, "P"),
    "A Kopoacinth":        (26,   36,   26, 206,   2006,  5,  20, ""),
    "A Kelpie":            (61,   25,   24, 223,   4025,  5,  0,  ""),
    "An Aspidchelone":     (114,  104,  19, 898,   10041, 7,  2,  ""),
    "An Idiot":            (13,   14,   16, 28,    49,    0,  0,  "Id"),
    "Modnar":              (15,   23,   20, 40,    101,   5,  12, "Mo"),
    "A Moron":             (3,    1,    10, 10,    28,    0,  100,"Ch"),
    "Some Green Slime":    (1,    5,    45, 100,   57,    0,  26, ""),
    "A Pixie":             (11,   29,   23, 26,    64,    0,  32, ""),
    "A Serpent":           (10,   18,   25, 25,    79,    0,  10, ""),
    "A Cluricaun":         (12,   27,   20, 30,    81,    0,  5,  ""),
    "An Imp":              (22,   30,   14, 40,    92,    0,  1,  ""),
    "A Centipede":         (3,    8,    18, 15,    33,    0,  61, ""),
    "A Beetle":            (2,    11,   21, 26,    44,    0,  48, ""),
    "A Fir Darrig":        (18,   22,   17, 35,    107,   0,  1,  "F"),
    "A Zombie":            (7,    45,   26, 23,    111,   0,  21, ""),
    "A Sprite":            (9,    37,   25, 31,    132,   1,  43, ""),
    "A Mimic":             (11,   55,   20, 47,    213,   1,  0,  "Mi"),
    "A Kobold":            (13,   10,   14, 21,    121,   1,  68, "G"),
    "A Spider":            (6,    11,   28, 28,    154,   1,  57, "P"),
    "An Uldra":            (14,   37,   21, 32,    93,    1,  6,  ""),
    "A Crebain":           (5,    11,   31, 31,    112,   1,  81, ""),
    "A Bogle":             (19,   15,   16, 35,    157,   1,  15, "F"),
    "A Fachan":            (9,    40,   15, 45,    139,   1,  10, ""),
    "A Stirge":            (2,    6,    35, 25,    101,   1,  95, ""),
    "A Ghillie Dhu":       (12,   16,   13, 28,    104,   2,  2,  "F"),
    "A Shrieker":          (2,    62,   27, 9,     213,   2,  0,  "Sh"),
    "A Carrion Crawler":   (12,   20,   20, 65,    142,   2,  42, ""),
    "A Trow":              (15,   17,   23, 51,    136,   2,  36, ""),
    "A Gnoll":             (20,   25,   15, 40,    166,   2,  61, ""),
    "A Smurf":             (23,   28,   19, 57,    189,   2,  57, "Ch"),
    "A Warg":              (20,   10,   17, 45,    152,   2,  88, ""),
    "An Orc":              (25,   13,   16, 26,    141,   2,  92, ""),
    "A Killmoulis":        (30,   19,   8,  75,    175,   3,  22, ""),
    "A Hob-goblin":        (35,   20,   15, 72,    246,   3,  18, ""),
    "A Wichtlein":         (13,   40,   25, 61,    300,   3,  8,  ""),
    "A Fenoderee":         (16,   6,    21, 65,    222,   3,  42, "F"),
    "A Bwca":              (21,   17,   19, 55,    387,   3,  1,  "F"),
    "An Ogre":             (42,   14,   16, 115,   409,   3,  19, ""),
    "A Dodo":              (62,   12,   11, 76,    563,   3,  3,  ""),
    "A Hydra":             (14,   27,   33, 99,    599,   3,  27, ""),
    "A Hamadryad":         (23,   47,   26, 62,    426,   3,  12, ""),
    "A Unicorn":           (27,   57,   99, 57,    669,   4,  0,  "U"),
    "An Owlbear":          (35,   16,   18, 100,   623,   4,  22, ""),
    "Black Annis":         (37,   52,   15, 65,    786,   4,  2,  ""),
    "A Jubjub Bird":       (45,   23,   12, 114,   1191,  4,  0,  ""),
    "A Peridexion":        (26,   32,   24, 98,    1300,  5,  2,  ""),
    "A Chaladrius":        (8,    49,   37, 172,   1929,  5,  20, ""),
    "A Gwyllion":          (27,   73,   20, 65,    1888,  5,  4,  ""),
    "A Cinomulgus":        (23,   2,    10, 199,   263,   5,  18, ""),
    "A Jello Blob":        (100,  25,   7,  264,   1257,  4,  13, ""),
    "A Cocodrill":         (39,   28,   24, 206,   1438,  4,  38, ""),
    "A Troll":             (75,   12,   20, 185,   1013,  4,  29, "R"),
    "A Bonnacon":          (89,   26,   9,  255,   1661,  4,  14, "Bo"),
    "A Gargoyle":          (22,   21,   29, 200,   1753,  5,  7,  "SS"),
    "Smeagol":             (41,   33,   27, 373,   2487,  5,  0,  "Sm,Ch"),
    "A Wraith":            (52,   102,  22, 200,   3112,  5,  13, "W"),
    "A Phooka":            (42,   63,   21, 300,   4125,  5,  12, ""),
    "A Vortex":            (101,  30,   31, 500,   6992,  6,  4,  "Md"),
    "A Snotgurgle":        (143,  19,   26, 525,   5752,  6,  3,  ""),
    "A Thaumaturgist":     (35,   200,  23, 400,   7628,  6,  0,  "Tra,Mr"),
    "A Bandersnatch":      (105,  98,   22, 450,   7981,  6,  3,  ""),
    "A Harpy":             (103,  49,   24, 263,   7582,  6,  2,  ""),
    "A Tigris":            (182,  38,   17, 809,   7777,  6,  3,  ""),
    "A Coblynau":          (205,  46,   18, 585,   8333,  6,  2,  ""),
    "Shelob":              (147,  64,   28, 628,   9003,  7,  0,  "P+"),
    "A Gryphon":           (201,  45,   19, 813,   8888,  7,  1,  ""),
    "A Chimaera":          (173,  109,  28, 947,   10006, 7,  0,  ""),
    "A Jack-in-Irons":     (222,  36,   12, 1000,  8119,  7,  0,  ""),
    "Smaug":               (251,  76,   26, 1022,  10077, 7,  0,  ""),
    "A Balrog":            (500,  100,  25, 705,   10103, 7,  0,  "Ba"),
    "Argus":               (201,  87,   14, 1500,  9510,  7,  0,  ""),
    "Cacus":               (256,  43,   19, 1750,  11012, 7,  0,  ""),
    "A Wyvern":            (301,  102,  24, 1222,  10888, 8,  0,  ""),
    "Begion":              (403,  154,  10, 1875,  12013, 8,  0,  ""),
    "A Xorn":              (342,  141,  23, 1299,  13649, 8,  0,  ""),
    "Grendel":             (197,  262,  23, 2000,  14014, 8,  0,  ""),
    "Red Cap":             (143,  50,   35, 1965,  15015, 8,  0,  ""),
    "A Nuckelavee":        (300,  75,   20, 2185,  15555, 8,  0,  ""),
    "A Titan":             (302,  1483, 12, 1625,  17999, 8,  0,  "Tit"),
    "Saruman":             (55,   373,  17, 1500,  17101, 11, 0,  "Sa"),
    "A Nazgul":            (250,  251,  26, 1011,  12988, 10, 9,  "N"),
    "Scatha the Worm":     (406,  208,  20, 1790,  17999, 9,  0,  ""),
    "Tiamat":              (506,  381,  29, 2000,  19001, 9,  0,  "Tia"),
    "A Jabberwock":        (185,  136,  25, 2265,  19984, 9,  0,  "J,HH"),
    "A Succubus/Incubus":  (186,  1049, 27, 2007,  23256, 9,  0,  "In,HH"),
    "Cerberus":            (236,  96,   29, 2600,  25862, 9,  0,  "Ce,HH"),
    "Ungoliant":           (399,  2398, 37, 2784,  27849, 12, 0,  "P++"),
    "Leanan-Sidhe":        (486,  5432, 46, 3000,  30004, 13, 0,  "L"),
    "Dragons":             (400,  150,  45, 3000,  35003, 14, 0,  "DR"),
    "The Dark Lord":       (9999, 9999, 31, 19999, 40005, 15, 0,  "DL"),
}

SPECIAL_NOTES = {
    "Ba":  "BALROG: steals experience instead of energy, grows stronger with each hit.",
    "Bo":  "Bonnacon: can fart and flee combat.",
    "Ch":  "Chatty: will talk to you in combat.",
    "Ce":  "CERBERUS: can run off with all your metal treasures.",
    "DL":  "THE DARK LORD: immune to most spells and physical damage. Magic bolts do ZERO. "
           "Does not pursue - you can always flee. Needs Blessing + Charms.",
    "DR":  "DRAGONS: vary by color and age; stats differ from the printed table and scale with your level.",
    "F":   "Faerie: can be defeated with Holy Water.",
    "G":   "Greedy: can steal money.",
    "HH":  "HEAD HUNTER: will stalk you if you look like a good target.",
    "Id":  "Idiot: may drool.",
    "In":  "INCUBUS/SUCCUBUS: may disable part of your brains or magic level.",
    "J":   "JABBERWOCK: burbles away quicksilver; may summon a Jubjub Bird or Bandersnatch.",
    "L":   "LEANAN-SIDHE: permanently saps strength and can suck out your soul.",
    "Md":  "MANA DRAIN: can drain your mana - bolt fuel at risk.",
    "Mo":  "MODNAR/MORGOTH: below level 3000 randomized stats. Above 3000, Morgoth mirrors YOUR stats "
           "and is IMMUNE TO SPELLS and nicking. No spell menu, no luckout - melee/skirmish only.",
    "Mi":  "MIMIC: disguises itself as other monsters. You can rarely flee (5%).",
    "Mr":  "Magic Resistant: resists magical damage.",
    "N":   "NAZGUL: can demand your ring or destroy your blessing with an Eldritch Curse.",
    "P":   "Minor poison.",
    "P+":  "Poison.",
    "P++": "MAJOR POISON: large poison damage and can drain speed.",
    "R":   "Regenerates in combat - burst it down, don't grind.",
    "Sa":  "SARUMAN: Wormtongue can steal a Palantir; can turn gems to gold, charms to amulets, "
           "or scramble your stats.",
    "Sh":  "SHRIEKER: DO NOT ATTACK. It summons monster #70-99 (Thaumaturgist through Dragon range) and then VANISHES, leaving you facing it. You can always flee - do that.",
    "Sm":  "SMEAGOL: can try to steal a ring.",
    "SS":  "STONE SKIN: resists physical damage - prefer spells.",
    "Tia": "TIAMAT: can steal half your gold and gems and escape.",
    "Tit": "TITAN: shatters force fields and damages shields directly.",
    "Tra": "Transporter: can teleport you far away.",
    "U":   "UNICORN: can only be subdued with a Virgin.",
    "W":   "WRAITH: can blind you.",
}


def scaled(name, size=1):
    """Return a monster's stats scaled to the given size."""
    if name not in MONSTERS:
        return None
    st, br, sp, en, xp, tr, flock, spec = MONSTERS[name]
    size = max(1, size)
    return {
        "name": name,
        "size": size,
        "strength": st * (1 + 0.5 * (size - 1)),
        "brains": br * size,
        "speed": sp,          # "very slowly" increases; treat as flat
        "energy": en * size,
        "experience": xp * size,
        "treasure": tr,
        "flock": flock,
        "specials": [s for s in spec.split(",") if s],
    }


def notes_for(mon):
    return [SPECIAL_NOTES[s] for s in mon["specials"] if s in SPECIAL_NOTES]


def lookup(text):
    """Find a monster name inside a line of server text. Longest match wins."""
    best = None
    for name in MONSTERS:
        if name.lower() in text.lower():
            if best is None or len(name) > len(best):
                best = name
    return best
