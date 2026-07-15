"""
data/materials.py

Static definitions for every material, recipe, and drill in the game. This
is plain Python data (dicts), not database rows, because it never changes
at runtime - it's balance data you'll tune by editing this file and
restarting the bot, not something users modify.
"""

# Raw materials and their drop chance when a drill/mining event pulls one
# item from a "mining block". Chances are expressed as fractions of 1.0 and
# should sum to 1.0. market_ceiling_price is the most the server market will
# ever pay to acquire one unit (see cogs/economy.py and docs/market.md) -
# denominated in the buying server's own currency, not DragonCoin.
RAW_MATERIALS = {
    "iron_ore":    {"name": "Iron Ore",    "emoji": "<:IronOre:1523432328028885034>",    "drop_chance": 0.60,     "market_ceiling_price": 0.01},
    "copper_ore":  {"name": "Copper Ore",  "emoji": "<:CopperOre:1523432342813933699>",  "drop_chance": 0.30,     "market_ceiling_price": 0.0167},
    "coal":        {"name": "Coal",        "emoji": "<:Coal:1523432352318099456>",       "drop_chance": 0.0999,   "market_ceiling_price": 0.05},
    "ruby":        {"name": "Ruby",        "emoji": "<:Ruby:1523433101840089208>",       "drop_chance": 0.00009,  "market_ceiling_price": 5500.00},
    "obsidian":    {"name": "Obsidian",    "emoji": "<:Obsidian:1523433115450736690>",   "drop_chance": 0.000009, "market_ceiling_price": 52500.00},
    "diamond":     {"name": "Diamond",     "emoji": "<:Diamond:1523433355708858612>",    "drop_chance": 0.000001, "market_ceiling_price": 500000.00},
}

# Smelted materials: produced by the furnace from raw materials.
# "inputs" maps material_id -> quantity required to produce ONE output unit.
SMELTED_MATERIALS = { # Smelting a material increases its value of all its raw materials by 50%
    "iron":  {"name": "Iron",  "emoji": "<:Iron:1523433412805918820>",  "inputs": {"iron_ore": 10}, "market_ceiling_price": 0.15}, #raw: 0.1
    "copper": {"name": "Copper", "emoji": "<:Copper:1523433425220927498>", "inputs": {"copper_ore": 10}, "market_ceiling_price": 0.25}, #raw: 0.167
    "steel": {"name": "Steel", "emoji": "<:Steel:1523433463150149692>", "inputs": {"iron_ore": 20, "coal": 4}, "market_ceiling_price": 0.60}, #raw: 0.4
}

# Component materials: produced by the factory from smelted materials.
COMPONENT_MATERIALS = {
    "wiring":              {"name": "Wiring",              "emoji": "<:Wiring:1523433594004049971>",        "inputs": {"copper": 12}},
    "drill_chassis":       {"name": "Drill Chassis",        "emoji": "<:DrillChassis:1523433620566446150>",  "inputs": {"iron": 20, "copper": 12}},
    "iron_drill_bit":      {"name": "Iron Drill Bit",       "emoji": "<:IronDrillBit:1523433731799519403>",  "inputs": {"iron": 20}},
    "steel_drill_bit":     {"name": "Steel Drill Bit",      "emoji": "<:SteelDrillBit:1523433738950807592>", "inputs": {"steel": 20}},
    "ruby_drill_bit":      {"name": "Ruby Drill Bit",       "emoji": "<:RubyDrillBit:1523433749893742752>",  "inputs": {"steel": 10, "ruby": 3}},
    "obsidian_drill_bit":  {"name": "Obsidian Drill Bit",   "emoji": "<:ObsidianDrillBit:1523433758139748372>", "inputs": {"steel": 10, "obsidian": 3}},
    "diamond_drill_bit":   {"name": "Diamond Drill Bit",    "emoji": "<:DiamondDrillBit:1523433768076050551>", "inputs": {"steel": 10, "diamond": 3}},
}

# Drills: also crafted in the factory, from a chassis + wiring + a bit type.
# mines_per_hour / storage_capacity match the design doc's stated rates.
DRILLS = {
    "iron_drill": {
        "name": "Iron Drill", "emoji": "<:IronMiningDrill:1523433637398450347>",
        "inputs": {"wiring": 1, "drill_chassis": 1, "iron_drill_bit": 1},
        "mines_per_hour": 5, "storage_capacity": 100,
    },
    "steel_drill": {
        "name": "Steel Drill", "emoji": "<:SteelMiningDrill:1523433646613069824>",
        "inputs": {"wiring": 1, "drill_chassis": 1, "steel_drill_bit": 1},
        "mines_per_hour": 7.5, "storage_capacity": 200,
    },
    "ruby_drill": {
        "name": "Ruby Drill", "emoji": "<:RubyMiningDrill:1523433666800517262>",
        "inputs": {"wiring": 1, "drill_chassis": 1, "ruby_drill_bit": 1},
        "mines_per_hour": 10, "storage_capacity": 350,
    },
    "obsidian_drill": {
        "name": "Obsidian Drill", "emoji": "<:ObsidianMiningDrill:1523433678825459893>",
        "inputs": {"wiring": 1, "drill_chassis": 1, "obsidian_drill_bit": 1},
        "mines_per_hour": 12.5, "storage_capacity": 500,
    },
    "diamond_drill": {
        "name": "Diamond Drill", "emoji": "<:DiamondMiningDrill:1523433688656908408>",
        "inputs": {"wiring": 1, "drill_chassis": 1, "diamond_drill_bit": 1},
        "mines_per_hour": 15, "storage_capacity": 750,
    },
}

# Furnace/factory throughput per level, in items-per-hour, straight from the doc.
FURNACE_RATES = {1: 5, 2: 10, 3: 15}
FACTORY_RATES = {1: 1, 2: 2, 3: 3}

# Fee totals (in server currency) required to level up infrastructure.
FURNACE_FACTORY_UPGRADE_THRESHOLDS = {2: 5.00, 3: 50.00}

FURNACE_COAL_COST_PER_UNIT = 1  # extra coal burned per item smelted, on top of the recipe's own inputs
MAX_DRILLS_PER_USER_PER_SERVER = 3

# The server-wide raw material pool: how much is added per member each day,
# and how many days' worth (at that daily rate) it can bank up to before the
# top-up stops growing it further.
MINING_POOL_DAILY_PER_MEMBER = 200
MINING_POOL_CAP_MULTIPLIER = 3

FURNACE_MAX_QUEUE_ITEMS = 25
FACTORY_MAX_QUEUE_ITEMS = 5

# The server market's per-material "target stock" - the equilibrium point its
# buy-price curve is built around - scales with server size: target_stock =
# member_count * MARKET_TARGET_STOCK_PER_MEMBER. Below target, the server pays
# closer to a material's market_ceiling_price to acquire more of it; at or
# above target, it pays progressively less. See cogs/economy.py.
MARKET_TARGET_STOCK_PER_MEMBER = 100
# The server always sells back to users at this multiple of market_ceiling_price.
MARKET_SELL_MARKUP = 2


def target_stock(member_count: int) -> int:
    """The equilibrium stock level (docs/market.md section 3) a server's
    market pricing curve is built around for ANY material - same formula
    regardless of which material, since it only scales with server size."""
    return max(1, member_count * MARKET_TARGET_STOCK_PER_MEMBER)


def get_material_info(material_id: str) -> dict | None:
    """Looks up a material regardless of which tier (raw/smelted/component)
    it belongs to. Returns None if the ID doesn't exist."""
    for table in (RAW_MATERIALS, SMELTED_MATERIALS, COMPONENT_MATERIALS, DRILLS):
        if material_id in table:
            return table[material_id]
    return None
