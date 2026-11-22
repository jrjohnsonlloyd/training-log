"""Table layout, upgrades and shared helpers for the build-time food database.

The Training Log app has no database of its own. This SQLite file only exists on the
computer that builds the food list:

    source files --> scripts/import_foods.py --> SQLite file --> food-db.json (what the app loads)

This module is the one place that knows what the food_items table looks like and how
to bring an older database file up to date without losing anything. Standard library only.

How versions work
-----------------
Every SQLite file carries one spare whole number, "PRAGMA user_version" (0 in a new
file). It is used here as the layout version:

    0  nothing yet
    1  the first table: name, group, four numbers per 100 g, portions, source
    2  adds brand, category, regions, barcode, thumbnail, sugars / saturates / fibre /
       sodium, other names, basis, source priority, popularity and a de-duplication key
    3  adds three facts the importer needs to choose and merge Open Food Facts rows:
       foreign_tags, completeness and alt_barcodes (see _upgrade_2_to_3)

migrate() walks a database up one version at a time until it reaches LATEST_VERSION.
Each step is all-or-nothing: if anything goes wrong the file is left as it was.

Adding version 4 later
----------------------
1. Write _upgrade_3_to_4(conn) next to the other steps.
2. Add it to _UPGRADES and set LATEST_VERSION = 4.
Never edit a step that has already been used on a real file; add a new one instead.
"""
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager

__all__ = [
    "LATEST_VERSION", "CATEGORIES", "REGIONS", "SOURCE_PRIORITY",
    "connect", "create_v1", "migrate",
    "normalize_barcode", "barcode_origin", "name_key",
    "kcal_from_kj", "sodium_mg_from_salt_g", "salt_g_from_sodium_mg",
]

LATEST_VERSION = 3
CATEGORIES = ("supermarket", "dining_out", "whole_food")
REGIONS = ("PH", "US", "UK", "JP", "KR")          # region_tags always lists them in this order
SOURCE_PRIORITY = {"curated": 1, "usda": 2, "off": 3}   # lower number wins when rows are de-duplicated

_PER_UNITS = ("100g", "serving")
_LOWEST_PRIORITY = 3                              # given to any source name not listed above


def _sql_list(values):
    """("a", "b") -> 'a', 'b'   (for the inside of an SQL "IN (...)")."""
    return ", ".join("'" + value + "'" for value in values)


# ---------------------------------------------------------------------------------------
# Table layouts
# ---------------------------------------------------------------------------------------

# Version 1: the layout before the upgrade. Kept so old files can be recognised and so the
# tests can build one.
_V1_TABLE = """
CREATE TABLE food_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    group_label TEXT,
    kcal        REAL NOT NULL,              -- kcal, protein, carbs, fat are per 100 g
    protein     REAL,
    carbs       REAL,
    fat         REAL,
    portions    TEXT NOT NULL DEFAULT '[]',
    source      TEXT NOT NULL,              -- 'usda' or 'curated'
    source_ref  TEXT
)"""

# Version 2: the layout version 3 builds on. It is built under the temporary name
# food_items_new and renamed to food_items once the old rows have been copied in (see
# _upgrade_1_to_2). Version 3 adds three columns at the end (see _V3_NEW_COLUMNS).
_V2_TABLE = f"""
CREATE TABLE food_items_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    brand           TEXT,
    group_label     TEXT,                   -- free-text display group: "Jollibee", "Home dish", "Burgers"
    category        TEXT NOT NULL DEFAULT 'whole_food' CHECK (category IN ({_sql_list(CATEGORIES)})),
    region_tags     TEXT NOT NULL DEFAULT '',   -- e.g. 'PH,US': fixed order PH,US,UK,JP,KR, no spaces
    barcode         TEXT,                   -- output of normalize_barcode(), NULL when unknown
    thumbnail_url   TEXT,
    per_unit        TEXT NOT NULL DEFAULT '100g' CHECK (per_unit IN ({_sql_list(_PER_UNITS)})),
    calories_kcal   REAL NOT NULL,          -- this and the numbers below are per 100 g, or per
    protein_g       REAL,                   -- serving when per_unit = 'serving' (curated rows whose
    carbs_g         REAL,                   -- source gives no gram weight)
    sugars_g        REAL,
    fat_g           REAL,
    saturates_g     REAL,
    fibre_g         REAL,
    sodium_mg       REAL,                   -- always milligrams of sodium; salt is never stored
    portions        TEXT NOT NULL DEFAULT '[]',   -- JSON [[label, grams-or-null], ...], default portion first
    aka             TEXT NOT NULL DEFAULT '[]',   -- JSON list of other names
    basis           TEXT,                   -- official | database | estimate
    source          TEXT NOT NULL,          -- curated | usda | off
    source_priority INTEGER NOT NULL,       -- 1 curated, 2 usda, 3 off (see SOURCE_PRIORITY)
    source_ref      TEXT,                   -- the row's identity inside its source, e.g. 'survey:2709224'
    popularity      INTEGER NOT NULL DEFAULT 0,
    name_key        TEXT NOT NULL DEFAULT ''      -- output of name_key(name, brand)
)"""

# The two UNIQUE indexes only cover rows where the value is filled in ("WHERE ... IS NOT
# NULL"), so any number of rows may have no barcode or no source_ref.
# Note for the importer: to insert-or-update on the last one, SQLite wants the same
# condition repeated:  ... ON CONFLICT (source, source_ref) WHERE source_ref IS NOT NULL DO UPDATE SET ...
_V2_INDEXES = (
    "CREATE UNIQUE INDEX idx_food_items_barcode ON food_items (barcode) WHERE barcode IS NOT NULL",
    "CREATE INDEX idx_food_items_name_key ON food_items (name_key)",
    "CREATE INDEX idx_food_items_category ON food_items (category)",
    "CREATE INDEX idx_food_items_source_priority ON food_items (source_priority)",
    "CREATE UNIQUE INDEX idx_food_items_source_ref ON food_items (source, source_ref)"
    " WHERE source_ref IS NOT NULL",
)

# Version 3: three columns added to the end of the version 2 table. The defaults keep every
# older row valid. Only Open Food Facts rows use the first two.
#   foreign_tags   how many countries the product is tagged with that are not one of the five
#                  wanted regions (a product sold in 30 countries is rarely the local pack)
#   completeness   Open Food Facts' own "how complete is this record" figure as a whole
#                  percent, 0 to 100; NULL when the source gave none
#   alt_barcodes   the barcodes of duplicates that were merged into this row, as JSON:
#                  [[barcode, popularity, region_tags], ...] sorted by barcode. Popularity and
#                  regions are those of the merged row, so the exporter can decide which of
#                  them to pass on to the app without importing everything again.
_V3_NEW_COLUMNS = (
    "foreign_tags INTEGER NOT NULL DEFAULT 0",
    "completeness INTEGER",
    "alt_barcodes TEXT NOT NULL DEFAULT '[]'",
)


# ---------------------------------------------------------------------------------------
# Opening and upgrading a database
# ---------------------------------------------------------------------------------------

def connect(path):
    """Open the database file at path, creating the file (and its folder) when missing.

    Rows come back readable by column name: row["name"]. Call migrate() next.
    """
    path = os.fspath(path)
    folder = os.path.dirname(path)          # '' for a bare file name and for ':memory:'
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _transaction(conn):
    """Make everything inside the "with" block one all-or-nothing change, then save it.

    SAVEPOINT is SQLite's transaction that may also start while another one is open, so
    this works whether or not the caller has unsaved rows on the connection, and however
    the connection was opened. If the block fails, only the block is undone.
    Do not call conn.executescript() inside the block: it commits first, which would
    defeat the purpose.
    """
    conn.execute("SAVEPOINT food_schema_step")
    try:
        yield
        conn.execute("RELEASE food_schema_step")        # keep the changes
    except BaseException:
        conn.execute("ROLLBACK TO food_schema_step")    # undo the changes ...
        conn.execute("RELEASE food_schema_step")        # ... and close the savepoint
        raise
    conn.commit()       # write to disk, together with any rows the caller had not committed yet


def _stamp(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _current_version(conn):
    """The layout version of this database. Falls back to looking at the table itself when
    the file was never stamped (a table created by hand)."""
    version = _stamp(conn)
    if version == 0:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(food_items)")}
        if "alt_barcodes" in columns:
            return 3
        if "calories_kcal" in columns:
            return 2
        if "kcal" in columns:
            return 1
    return version


def _upgrade_0_to_1(conn):
    conn.execute(_V1_TABLE)


def _upgrade_1_to_2(conn):
    """Rebuild food_items in the version 2 layout and copy every old row across, same id.

    A rebuild (rather than a series of ALTER TABLE statements) means a fresh database and
    an upgraded one end up with exactly the same table, columns in the documented order.
    """
    conn.execute(_V2_TABLE)

    # Let the copy statement below call two Python helpers by name.
    conn.create_function("food_name_key", 1, name_key)
    conn.create_function("food_source_priority", 1,
                         lambda source: SOURCE_PRIORITY.get(source, _LOWEST_PRIORITY))

    # Old values are carried over unchanged (kcal -> calories_kcal and so on). The columns
    # an old row cannot know are filled in: priority from its source, 'whole_food', region
    # 'US' for USDA rows, numbers per 100 g, no other names, and its de-duplication key.
    # Everything not listed (brand, barcode, sugars, ...) starts empty.
    conn.execute("""
        INSERT INTO food_items_new
              (id, name, group_label, calories_kcal, protein_g, carbs_g, fat_g, portions, source, source_ref,
               source_priority, category, region_tags, per_unit, aka, name_key)
        SELECT id, name, group_label, kcal,          protein,   carbs,   fat,   portions, source, source_ref,
               food_source_priority(source), 'whole_food',
               CASE WHEN source = 'usda' THEN 'US' ELSE '' END, '100g', '[]', food_name_key(name)
        FROM food_items ORDER BY id""")

    # Safety net: only remove the old table once every row is known to be in the new one.
    old_count = conn.execute("SELECT COUNT(*) FROM food_items").fetchone()[0]
    new_count = conn.execute("SELECT COUNT(*) FROM food_items_new").fetchone()[0]
    if new_count != old_count:
        raise RuntimeError(f"upgrade to version 2 copied {new_count} of {old_count} rows; nothing was changed")

    # AUTOINCREMENT promises an id is never handed out twice, even after its row is deleted.
    # SQLite remembers the highest id ever used in its own table sqlite_sequence; take the
    # old table's figure along so the promise survives the rebuild.
    highest_id = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'food_items'").fetchone()
    conn.execute("DROP TABLE food_items")
    conn.execute("ALTER TABLE food_items_new RENAME TO food_items")
    if highest_id is not None:
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'food_items'")
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('food_items', ?)", (highest_id[0],))

    # Version 1 had no rule against two rows carrying the same (source, source_ref). Version 2
    # does, and rows are never deleted here, so a later copy gets '#dup-<its id>' added to its
    # source_ref. The first row (lowest id) keeps the plain value.
    conn.execute("""
        UPDATE food_items SET source_ref = source_ref || '#dup-' || id
        WHERE source_ref IS NOT NULL
          AND id NOT IN (SELECT MIN(id) FROM food_items WHERE source_ref IS NOT NULL GROUP BY source, source_ref)""")

    for statement in _V2_INDEXES:
        conn.execute(statement)


def _upgrade_2_to_3(conn):
    """Add the three version 3 columns. Every existing row keeps its id and all its values
    and gets the defaults: no foreign tags, no completeness figure, no merged barcodes.

    New columns go at the end of the table, so a fresh database (which walks through the
    same steps) and an upgraded one still end up with exactly the same layout.
    """
    for column in _V3_NEW_COLUMNS:
        conn.execute("ALTER TABLE food_items ADD COLUMN " + column)


# version reached -> the step that gets there from the version before
_UPGRADES = {1: _upgrade_0_to_1, 2: _upgrade_1_to_2, 3: _upgrade_2_to_3}


def create_v1(conn):
    """Create the version 1 table in an empty database and stamp it as version 1.

    Only for tests and as a record of the old layout; real code calls migrate().
    """
    with _transaction(conn):
        _upgrade_0_to_1(conn)
        conn.execute("PRAGMA user_version = 1")


def migrate(conn):
    """Bring the database up to LATEST_VERSION and return the version it now has.

    Works on a brand-new file, a version 1 or 2 file and an up-to-date file. Each step is saved
    as a whole or not at all, no row is ever deleted, and running it again changes nothing.
    When an upgrade is needed, rows the caller had inserted but not yet committed are
    committed along with it.
    """
    version = _current_version(conn)
    if version > LATEST_VERSION:
        raise RuntimeError(f"this database is version {version}, but scripts/food_schema.py only knows "
                           f"up to version {LATEST_VERSION}: use a newer copy of the scripts")
    while version < LATEST_VERSION:
        with _transaction(conn):
            _UPGRADES[version + 1](conn)
            version += 1
            conn.execute(f"PRAGMA user_version = {version}")
    if _stamp(conn) != version:             # an up-to-date table that was never stamped
        with _transaction(conn):
            conn.execute(f"PRAGMA user_version = {version}")
    return version


# ---------------------------------------------------------------------------------------
# Helpers shared with the importer
# ---------------------------------------------------------------------------------------

def normalize_barcode(raw):
    """Return the one canonical spelling of a barcode, or None when it cannot be used.

    The same product is printed as a 12-digit UPC-A in the US and with extra leading zeros
    elsewhere, so "016000275287", "0016000275287" and "00016000275287" must all compare
    equal: each becomes "0016000275287" (13 digits, zero-padded on the left).
    """
    if raw is None:
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)                      # 16000275287.0 from a spreadsheet, not "...2870"
    digits = re.sub(r"[^0-9]", "", str(raw))
    if not 8 <= len(digits) <= 14:          # shorter than EAN-8 or longer than GTIN-14
        return None
    digits = digits.lstrip("0")
    if not digits:                          # all zeros
        return None
    canonical = digits.zfill(13)            # a full 14-digit code is left as it is
    if canonical.startswith(("2", "02")):
        return None                         # in-store / price-embedded codes: each shop reuses them
    return canonical


# The first three digits of a barcode say which national office handed the number out, which
# is nearly always where the brand owner is based. Only the ranges the food list cares about:
#   (first prefix, last prefix, origin)
_BARCODE_PREFIXES = (
    (480, 480, "PH"),                                       # Philippines
    (450, 459, "JP"), (490, 499, "JP"),                     # Japan
    (880, 880, "KR"),                                       # South Korea
    (500, 509, "UK"),                                       # United Kingdom
    (0, 19, "US"), (30, 39, "US"), (60, 139, "US"),         # United States and Canada
    # "AS": the neighbouring markets whose products fill Philippine import shelves
    (885, 885, "AS"), (955, 955, "AS"), (888, 888, "AS"),   # Thailand, Malaysia, Singapore
    (899, 899, "AS"), (893, 893, "AS"), (489, 489, "AS"),   # Indonesia, Vietnam, Hong Kong
    (471, 471, "AS"), (690, 699, "AS"),                     # Taiwan, China
)


def _prefix_origin(prefix):
    number = int(prefix)
    for first, last, origin in _BARCODE_PREFIXES:
        if first <= number <= last:
            return origin
    return None


def barcode_origin(barcode):
    """Where a barcode was registered: 'PH', 'JP', 'KR', 'UK', 'US', 'AS' or None.

    'AS' stands for Thailand, Malaysia, Singapore, Indonesia, Vietnam, Hong Kong, Taiwan
    and China together. None means anywhere else, or no usable barcode at all.

        barcode_origin("4800016644504") == "PH"      barcode_origin("8801043014830") == "KR"
        barcode_origin("0016000275287") == "US"      barcode_origin("3017620425035") is None

    The answer is read from the canonical spelling (see normalize_barcode); any other
    spelling of the same barcode gives the same answer. A 14-digit code starts with a
    packaging digit, which is skipped. A short 8-digit code (stored with five leading
    zeros) carries its prefix in digits 6 to 8; those are only trusted for the Philippines,
    Japan and Korea, because the short American and British codes are numbered differently.
    """
    code = normalize_barcode(barcode)
    if code is None:
        return None
    if len(code) == 14:
        code = code[1:]
    if code.startswith("00000"):
        origin = _prefix_origin(code[5:8])
        return origin if origin in ("PH", "JP", "KR") else None
    return _prefix_origin(code[:3])


# ' ` and the curly, modifier, acute-accent and full-width characters people type as an apostrophe
_APOSTROPHES = dict.fromkeys(map(ord, "'`‘’ʼ´＇"))


def _key_part(text):
    """"Lucky Me!" -> "lucky me",  "Café  M&M's" -> "cafe m and ms"."""
    if text is None:
        return ""
    text = str(text).translate(_APOSTROPHES)        # delete apostrophes: McDonald's -> McDonalds
    text = unicodedata.normalize("NFKD", text)      # split "é" into "e" + accent mark; full-width -> plain
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("M"))   # drop the marks
    text = text.casefold().replace("&", " and ").replace(".", "")
    # Anything left that is not a letter or digit (in any alphabet) becomes a single space.
    return re.sub(r"[\W_]+", " ", text).strip()


def name_key(name, brand=None):
    """The text two rows must share to count as the same food: "<brand>|<name>".

    Spelling noise (case, accents, punctuation, "&" versus "and") is ignored, and a brand
    repeated at the start of the name is dropped, so
        name_key("Lucky Me! Pancit Canton", "Lucky Me!") == name_key("Pancit Canton", "Lucky Me")
                                                         == "lucky me|pancit canton"
    Without a brand the key is "|<name>".
    """
    name_part, brand_part = _key_part(name), _key_part(brand)
    if brand_part:
        if name_part == brand_part:
            name_part = ""
        elif name_part.startswith(brand_part + " "):
            name_part = name_part[len(brand_part) + 1:]
    return brand_part + "|" + name_part


def kcal_from_kj(kj):
    """Kilojoules -> kilocalories."""
    return float(kj) / 4.184


def sodium_mg_from_salt_g(salt_g):
    """Grams of salt -> milligrams of sodium (salt is about 40 % sodium)."""
    return float(salt_g) * 400


def salt_g_from_sodium_mg(sodium_mg):
    """Milligrams of sodium -> grams of salt."""
    return float(sodium_mg) / 400
