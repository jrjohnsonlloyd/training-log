"""Fill the build-time food database and write the food list the app loads.

    curated lists (JSON) --+
    USDA downloads --------+--> SQLite file (table food_items) --> food-db.json
    Open Food Facts file --+

Usage
-----
    python scripts/import_foods.py --db data/foods.sqlite
        [--curated-json FILE]...   hand-checked lists (see tests/fixtures/local_ph_curated.json for the shape)
        [--usda-path PATH]...      an unzipped FoodData Central CSV folder, or one flat CSV file
        [--off-path FILE]          the Open Food Facts "CSV" export (tab-separated, may be .gz)
        [--export-app OUT.json]    also write the app's food list ("version 2" of food-db.json)
        [--off-quota POOLS]        how many Open Food Facts rows to export per market, e.g.
                                   "US=12000,UK=2500,JP=3500,KR=2500,AS=1500" (these are the defaults;
                                   a pool left out keeps its default, 0 switches a pool off;
                                   Philippine rows are always exported in full)
        [--built YYYY-MM-DD]       date written into the exported file
        [--quiet]                  print only the final summary line

Nothing is downloaded: every input is a file already on this computer. Standard library only.

What one run does
-----------------
1. Every row that is read is cleaned and checked, then parked in a temporary table
   ("cand", short for candidates). Input files are read one line at a time and written
   in small batches, so even the 10 GB Open Food Facts export needs little memory.
   Open Food Facts is typed in by volunteers, so its rows pass extra checks first (see
   "Open Food Facts: checks and repairs" below): unusable names, sodium that disagrees
   with salt, calories that contradict the row's own protein, carbs and fat.
2. Rows already in the database join the candidates. A row read again (same source and
   same source_ref) replaces its older self and keeps its id.
3. Duplicates collapse: first rows sharing a barcode, then rows sharing the same
   name + brand key. A curated row always beats a USDA row, which always beats an Open
   Food Facts row. The winner keeps its own numbers.
   - Same barcode: always one product. The winner takes the loser's picture and regions.
   - Same name + brand: an Open Food Facts row is only merged when its calories agree
     with the row it would disappear into (within 10 %, or 5 kcal); otherwise it is a
     different food that happens to share a name and it stays. The barcode of a merged
     row is kept in the winner's alt_barcodes, so typing it still finds the food.
     Curated and USDA rows never take a pack photo or barcode from such a merge.
4. food_items is brought in line with the surviving candidates in one all-or-nothing step.

Running the same command twice leaves the same rows. The last line printed is a JSON
summary of the run.

The rules for merging and choosing Open Food Facts rows changed with database version 3.
A database filled by an older version of this script still carries the old merges (for
example pack photos on USDA rows), so build a new one from an empty file; the export
refuses a database whose Open Food Facts rows were all imported the old way.

Version 4 added the "doubt" mark: an Open Food Facts row from outside the Philippines whose
numbers look wrong (see pool_doubt) is kept in the database but never chosen for the app.
The marks are set while the export file is read, so a version 3 database that was merely
upgraded has none; the export refuses to fill a market pool from such a database.
"""
import argparse
import csv
import datetime
import gzip
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
import zlib

# "python -I" (isolated mode) does not put this folder on the import path, so add it by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import food_schema  # noqa: E402  (must come after the line above)
from food_schema import (CATEGORIES, REGIONS, SOURCE_PRIORITY, barcode_origin,  # noqa: E402
                         kcal_from_kj, name_key, normalize_barcode, sodium_mg_from_salt_g)

BATCH = 2000                    # rows written to SQLite at a time
MAX_KCAL_PER_100G = 950         # pure fat is about 900; anything above is a data error
MAX_SODIUM_MG = 40000           # pure salt is about 39 000 mg of sodium per 100 g
GRAM_COLUMNS = ("protein_g", "carbs_g", "sugars_g", "fat_g", "saturates_g", "fibre_g")
BASES = ("official", "database", "estimate")

# The food_items columns this script writes, in one fixed order (id is left to SQLite).
ITEM_COLUMNS = (
    "name", "brand", "group_label", "category", "region_tags", "barcode", "thumbnail_url",
    "per_unit", "calories_kcal", "protein_g", "carbs_g", "sugars_g", "fat_g", "saturates_g",
    "fibre_g", "sodium_mg", "portions", "aka", "basis", "source", "source_priority",
    "source_ref", "popularity", "name_key", "foreign_tags", "completeness", "alt_barcodes", "doubt",
)
_COLUMN_LIST = ", ".join(ITEM_COLUMNS)

# How many of the seven optional numbers a row has. Used to pick the fuller of two duplicates.
FILLED_SQL = "(" + " + ".join(f"({column} IS NOT NULL)" for column in GRAM_COLUMNS + ("sodium_mg",)) + ")"

# 1 when a row's calories fit its own protein, carbs and fat (4, 4 and 9 kcal per gram) to
# within 20 % or 15 kcal. Used to prefer the believable one of two Open Food Facts duplicates.
_MACRO_KCAL_SQL = "(4 * protein_g + 4 * carbs_g + 9 * fat_g)"
MACRO_OK_SQL = ("(protein_g IS NOT NULL AND carbs_g IS NOT NULL AND fat_g IS NOT NULL"
                f" AND ABS(calories_kcal - {_MACRO_KCAL_SQL})"
                f" <= MAX(0.20 * MAX(calories_kcal, {_MACRO_KCAL_SQL}), 15.0))")

# When are two same-name rows the same food? When their calories are this close.
MERGE_KCAL_SHARE = 0.10         # 10 % of the larger figure ...
MERGE_KCAL_FLOOR = 5.0          # ... or 5 kcal, whichever is more
# Doubts (see pool_doubt) and same-name merges. A doubt read from a row's own protein, carbs
# and fat says nothing about a twin with the same calories. One read from the serving size
# says nothing about a twin that states a serving of its own and passed the same test with
# it, but it does hold for a twin with no serving at all (same name, same calories: the same
# label typed the same way). The others are read from the name and the calories, which the
# twin shares. See _absorb and _better_twin.
OWN_DOUBTS = ("macro_mismatch", "dense_low", "drink_high", "mix_low", "zero_food", "per_serving_as", "per_bottle_as")
SERVING_DOUBTS = ("per_serving", "per_bottle")

# Open Food Facts: the only columns read out of roughly 200, and the countries that are kept.
OFF_COLUMNS = (
    "code", "product_name", "product_name_en", "brands", "countries_tags", "image_front_small_url",
    "serving_size", "serving_quantity", "unique_scans_n", "energy-kcal_100g", "energy-kj_100g",
    "energy_100g", "proteins_100g", "carbohydrates_100g", "sugars_100g", "fat_100g",
    "saturated-fat_100g", "fiber_100g", "sodium_100g", "salt_100g",
    "completeness", "carbohydrates-total_100g", "alcohol_100g", "categories_en", "generic_name", "quantity",
    "image_small_url",      # keep last: the bulk export's name for the small front photo
)
OFF_COUNTRIES = {"en:philippines": "PH", "en:united-states": "US", "en:united-kingdom": "UK",
                 "en:japan": "JP", "en:south-korea": "KR",
                 # "AS": seven neighbouring markets under one code (trips, and the imported goods
                 # on Philippine shelves). Counts in the November 2026 export: Singapore 12 686,
                 # Thailand 11 464, Indonesia 9 358, Malaysia 7 701, Hong Kong 6 000, Taiwan
                 # 3 558, Vietnam 1 723; the last four spellings are local-script tags (38 rows).
                 "en:thailand": "AS", "en:malaysia": "AS", "en:singapore": "AS", "en:indonesia": "AS",
                 "en:vietnam": "AS", "en:hong-kong": "AS", "en:taiwan": "AS",
                 "th:ไทย": "AS", "en:ประเทศไทย-thai": "AS", "en:ประเทศไทย (thai)": "AS", "zh:台灣": "AS"}
# Country tags that say nothing about where a product is sold: catch-all tags and parts of
# the United Kingdom or the United States. They are not counted as "also sold abroad".
OFF_NEUTRAL_TAGS = frozenset((
    "en:world", "en:europe", "en:european-union", "en:scotland", "en:england", "en:wales",
    "en:northern-ireland", "en:puerto-rico", "en:guam", "en:american-samoa",
    "fr:angleterre", "fr:angle", "fr:international",
))

# The export takes every Philippine Open Food Facts row and then fills one pool per market.
# A pool is filled in this order (so a Korean product also tagged "US" lands in the Korean
# pool) up to these row counts; --off-quota changes the counts.
OFF_POOLS = ("KR", "JP", "AS", "US", "UK")
DEFAULT_OFF_QUOTA = {"US": 12000, "UK": 2500, "JP": 3500, "KR": 2500, "AS": 1500}
OFF_WRITE_ORDER = ("US", "UK", "JP", "KR", "AS")    # the order the pools are written in, after PH
ASIA_POOLS = ("JP", "KR", "AS")                     # pools whose rows the export tidies (see asia_brand)
# A merged barcode is passed on to the app when the pack it came from was scanned at least
# once, or is tagged with one of these regions (home, and the two 2027 trips).
ALIAS_REGIONS = ("PH", "JP", "KR")

# USDA: the flat one-file layout, and how a data type shows up in source_ref.
USDA_FLAT_COLUMNS = (
    "fdc_id", "data_type", "description", "brand_owner", "gtin_upc", "food_category",
    "serving_size_g", "household_serving", "energy_kcal", "energy_kj", "protein_g",
    "carbohydrate_g", "sugars_g", "fat_g", "saturated_fat_g", "fiber_g", "sodium_mg",
)
USDA_REF_PREFIX = {"survey_fndds_food": "survey", "sr_legacy_food": "sr",
                   "branded_food": "branded", "foundation_food": "fdc"}

# FoodData Central folders name a nutrient either by id (1008) or by its older number (208).
# kcal_alt is the stand-in used when a food has no plain energy figure.
NUTRIENT_COLUMN = {
    "1008": "kcal", "208": "kcal",
    "2047": "kcal_alt", "2048": "kcal_alt", "957": "kcal_alt", "958": "kcal_alt",
    "1003": "protein", "203": "protein",
    "1004": "fat", "204": "fat",
    "1005": "carbs", "205": "carbs",
    "2000": "sugars", "269": "sugars",
    "1258": "saturates", "606": "saturates",
    "1079": "fibre", "291": "fibre",
    "1093": "sodium", "307": "sodium",          # milligrams
}

# A USDA food naming one of these chains is filed under "dining out".
RESTAURANT_CHAINS = (
    "McDonalds", "Burger King", "Wendys", "Taco Bell", "KFC", "Subway", "Pizza Hut", "Dominos",
    "Popeyes", "Chick-fil-A", "Starbucks", "Dunkin", "Arbys", "Jack in the Box", "Dairy Queen",
    "Papa Johns", "Little Caesars", "Panda Express", "Chipotle", "Dennys", "IHOP", "Applebees",
    "Olive Garden", "Cracker Barrel", "TGI Fridays", "Carls Jr", "Hardees", "White Castle",
    "Five Guys", "Panera", "Long John Silvers", "Whataburger", "Outback", "Chilis", "Red Lobster",
    "On the Border", "Carrabbas",
)

# The exported app file (section 6 of the data contract).
EXPORT_FIELDS = ["name", "brand", "group", "category", "regions", "barcode", "thumb", "per", "kcal",
                 "protein", "carbs", "sugars", "fat", "saturates", "fibre", "sodium", "portions",
                 "source", "aka", "basis"]
THUMB_BASE = "https://images.openfoodfacts.org/images/products/"
EXPORT_NOTE = ("Curated Philippine and restaurant items, USDA FoodData Central (public domain) and "
               "Open Food Facts (Open Database Licence); numbers are per 100 g unless per is 1 (per serving).")


class InputError(Exception):
    """A file or argument that cannot be used. Shown to the user as one line, exit code 1."""


# ---------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------

def to_number(value):
    """Text or number -> float. Blank, not a number, or not finite -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def clean_text(value):
    """Trim and collapse inner whitespace: "  Pancit   Canton " -> "Pancit Canton". None -> ""."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def tidy_number(value):
    """2.0 -> 2 and 2.5 -> 2.5, which keeps the stored JSON short."""
    return int(value) if float(value).is_integer() else value


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def http_url(value):
    """A trimmed web address, or None when the text is not one."""
    text = (value or "").strip() if isinstance(value, str) else ""
    return text if text.lower().startswith(("http://", "https://")) else None


def region_text(codes):
    """A set of region codes -> the region_tags text, in the fixed order of REGIONS.

    A Philippine row is never also marked "AS": the Philippines is home, its rows are always
    exported as they were, and the neighbouring markets add nothing to them.
        region_text({"US", "AS"}) == "US,AS"      region_text({"PH", "AS"}) == "PH"
    """
    if "PH" in codes:
        codes = set(codes) - {"AS"}
    return ",".join(code for code in REGIONS if code in codes)


def regions_union(first, second):
    """"PH" and "US,KR" -> "PH,US,KR", always in the fixed order of REGIONS (see region_text)."""
    return region_text(set((first or "").split(",")) | set((second or "").split(",")))


def only_as(regions):
    """Is the row tagged only for the neighbouring markets ("AS"), with none of the five others?"""
    return (regions or "") == "AS"


def plain_words(text):
    """Lower-case words of plain letters and digits: "Crème  Brûlée (2x)" -> "creme brulee 2x".

    This is how the app compares what the user types with a food's name, with accents
    folded as well. Text in other alphabets gives "".
    """
    text = unicodedata.normalize("NFKD", str(text or ""))       # "é" -> "e" + accent mark
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"['’`.]", "", text).replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def aliases_add(aliases, code, popularity, regions):
    """Note one merged barcode in aliases, a dictionary {barcode: [popularity, region_tags]}.

    A barcode noted twice keeps the higher popularity and all regions, so the result does
    not depend on the order things are added in.
    """
    known = aliases.get(code)
    if known is None:
        aliases[code] = [int(popularity or 0), regions or ""]
    else:
        known[0] = max(known[0], int(popularity or 0))
        known[1] = regions_union(known[1], regions)


def aliases_read(text):
    """The alt_barcodes column -> {barcode: [popularity, region_tags]}. Unreadable text -> {}.

    A bare barcode (a list written by another tool) counts as never scanned, no region.
    """
    aliases = {}
    try:
        entries = json.loads(text or "[]")
    except ValueError:
        return aliases
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str):
            entry = [entry, 0, ""]
        if isinstance(entry, list) and len(entry) == 3 and isinstance(entry[0], str) and entry[0]:
            aliases_add(aliases, entry[0], to_number(entry[1]) or 0, entry[2] if isinstance(entry[2], str) else "")
    return aliases


def aliases_text(aliases):
    """{barcode: [popularity, region_tags]} -> the JSON stored in alt_barcodes, sorted by barcode."""
    return compact_json([[code, aliases[code][0], aliases[code][1]] for code in sorted(aliases)])


def aliases_union(first, second):
    """Two alt_barcodes texts -> one holding the barcodes of both."""
    if (second or "[]") == "[]":
        return first or "[]"
    aliases = aliases_read(first)
    for code, (popularity, regions) in aliases_read(second).items():
        aliases_add(aliases, code, popularity, regions)
    return aliases_text(aliases)


def blank_row(source):
    """A food_items row with nothing filled in yet, as a dictionary keyed by column name."""
    return {
        "name": "", "brand": None, "group_label": None, "category": "whole_food", "region_tags": "",
        "barcode": None, "thumbnail_url": None, "per_unit": "100g", "calories_kcal": None,
        "protein_g": None, "carbs_g": None, "sugars_g": None, "fat_g": None, "saturates_g": None,
        "fibre_g": None, "sodium_mg": None, "portions": [], "aka": [], "basis": "database",
        "source": source, "source_priority": SOURCE_PRIORITY[source], "source_ref": None,
        "popularity": 0, "foreign_tags": 0, "completeness": None, "alt_barcodes": [], "doubt": None,
    }


def numbers_possible(row):
    """False when a row's numbers cannot be true: no calories, a negative figure, or more
    than 100 g of something in 100 g of food.

    The upper limits only make sense for numbers per 100 g, so a row stored per serving is
    only checked for missing or negative values.
    """
    per_100g = row["per_unit"] == "100g"
    kcal = row["calories_kcal"]
    if kcal is None or kcal < 0 or (per_100g and kcal > MAX_KCAL_PER_100G):
        return False
    for column in GRAM_COLUMNS:
        value = row[column]
        if value is not None and (value < 0 or (per_100g and value > 100)):
            return False
    # 100 g of food cannot hold more than 100 g of protein + carbs + fat; 105 allows for rounding.
    if per_100g and (row["protein_g"] or 0) + (row["carbs_g"] or 0) + (row["fat_g"] or 0) > 105 + 1e-9:
        return False
    return True


def apply_rules(row):
    """Check and round the numbers of one row (rules b to e of the data contract).

    Returns False when the row must be thrown away (see numbers_possible).
    """
    if not numbers_possible(row):
        return False
    kcal = row["calories_kcal"]
    sodium = row["sodium_mg"]
    if sodium is not None and not 0 <= sodium <= MAX_SODIUM_MG:
        sodium = None                               # a wrong sodium figure is dropped, the row is kept
    row["calories_kcal"] = round(kcal, 1)
    for column in GRAM_COLUMNS:
        if row[column] is not None:
            row[column] = round(row[column], 2)
    row["sodium_mg"] = None if sodium is None else round(sodium, 1)
    return True


def raise_csv_field_limit():
    """Let the csv module accept very long fields (Open Food Facts has some)."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:                       # Windows only takes a 32-bit number here
            limit //= 2


def open_text(path):
    """Open a text file for reading line by line; a gzip file is unpacked on the fly."""
    with open(path, "rb") as probe:
        gzipped = probe.read(2) == b"\x1f\x8b"
    opener = gzip.open if gzipped else open
    return opener(path, "rt", encoding="utf-8-sig", errors="replace", newline="")


def csv_records(path, columns, found=None):
    """Read a delimited text file one line at a time.

    Yields, for every data row, a list holding just the wanted columns in the order asked
    ("" where the file has no such column or the row is short). A row the csv module cannot
    read is yielded as None so the caller can count it. The delimiter is a tab when the
    header line contains one (the Open Food Facts layout, which uses no quoting), otherwise
    a comma. Pass a set as "found" to learn which of the wanted columns the file has.
    """
    with open_text(path) as handle:
        header_line = handle.readline()
        if not header_line.strip():
            return
        if "\t" in header_line:
            options = {"delimiter": "\t", "quoting": csv.QUOTE_NONE}
        else:
            options = {"delimiter": ","}
        header = [title.strip().casefold() for title in next(csv.reader([header_line], **options))]
        position = [header.index(column.casefold()) if column.casefold() in header else -1
                    for column in columns]
        if found is not None:
            found.update(column for column, index in zip(columns, position) if index >= 0)
        reader = csv.reader(handle, **options)
        while True:
            try:
                row = next(reader)
            except StopIteration:
                return
            except csv.Error:
                yield None
                continue
            if not row:                             # an empty line
                continue
            size = len(row)
            yield [row[index] if 0 <= index < size else "" for index in position]


# ---------------------------------------------------------------------------------------
# The importer: running totals, the candidates table, de-duplication, saving
# ---------------------------------------------------------------------------------------

class _Kept:
    """One surviving row while a group of duplicates is walked (see Importer.collapse).

    The fields that a merge can change are plain attributes; "before" remembers what they
    were, so only rows that really changed are written back.
    """
    __slots__ = ("rid", "source", "per_unit", "kcal", "thumbnail", "regions", "barcode",
                 "foreign_tags", "popularity", "portions", "aliases", "doubt", "before")

    def __init__(self, record):
        (self.rid, _, self.source, self.per_unit, self.kcal, self.thumbnail, self.regions, self.barcode,
         self.foreign_tags, self.popularity, self.portions, self.aliases, self.doubt) = record
        self.before = self.changeable()

    def changeable(self):
        return (self.thumbnail, self.regions, self.barcode, self.foreign_tags, self.popularity,
                self.portions, self.aliases, self.doubt)


class Importer:
    def __init__(self, conn, quiet=False):
        self.conn = conn
        self.quiet = quiet
        self.read = {"curated": 0, "usda": 0, "off": 0}     # input rows seen in this run
        self.filtered_region = 0
        self.invalid = 0
        self.duplicates = 0
        self.repaired = 0                                   # Open Food Facts rows kept after a repair
        self.filled = 0                                     # ... and rows given one missing macro
        self.off_dropped = {"name": 0, "numbers": 0, "calories": 0}    # why Open Food Facts rows were invalid
        # Philippine rows whose label was typed per serving (see serving_typed_fix): re-based to
        # 100 g with the serving first, stored per serving, or dropped (counted in invalid too).
        self.serving_typed = {"rebased": 0, "per_serving": 0, "drop_zero": 0, "drop_high": 0}
        # Japanese, Korean and neighbouring-market snack rows re-based on their serving (see asia_serving_fix).
        self.asia_rebased = 0
        # Rows from other markets kept but marked as not to be chosen for the app (see pool_doubt), by reason.
        self.doubts = {}
        self._waiting = []                                  # cleaned rows not yet written
        self._order = 0                                     # 1, 2, 3 ... in the order rows are read
        conn.create_function("regions_union", 2, regions_union)
        conn.create_function("aliases_union", 2, aliases_union)
        conn.create_function("food_name_key", 2, name_key)
        # The candidates table lives in SQLite's temporary area (a scratch file that
        # disappears when the script ends), so the real database is untouched until save().
        #   item_id  the id of the food_items row this candidate is or replaces; NULL = new
        #   ord      import order: a lower number was imported earlier
        #   changed  1 when the row has to be written to food_items
        conn.execute(f"""
            CREATE TEMP TABLE cand (
                rid INTEGER PRIMARY KEY,
                item_id INTEGER,
                ord INTEGER NOT NULL,
                changed INTEGER NOT NULL DEFAULT 1,
                {_COLUMN_LIST}
            )""")

    def say(self, message):
        if not self.quiet:
            print(message, flush=True)

    def warn(self, message):
        print("warning: " + message, file=sys.stderr)

    def stage(self, row):
        """Park one cleaned row (a dictionary from blank_row) among the candidates."""
        self._order += 1
        row["name_key"] = name_key(row["name"], row["brand"])
        row["portions"] = compact_json(row["portions"])
        row["aka"] = compact_json(row["aka"])
        row["alt_barcodes"] = compact_json(row["alt_barcodes"])
        self._waiting.append((self._order,) + tuple(row[column] for column in ITEM_COLUMNS))
        if len(self._waiting) >= BATCH:
            self.flush()

    def flush(self):
        if self._waiting:
            marks = ", ".join("?" * (len(ITEM_COLUMNS) + 1))
            self.conn.executemany(f"INSERT INTO cand (ord, {_COLUMN_LIST}) VALUES ({marks})", self._waiting)
            self._waiting.clear()
        self.conn.commit()

    # -- de-duplication ------------------------------------------------------------------

    def collapse(self, key_sql, by_name=False):
        """Candidates sharing the same value of key_sql become one row (section 4 of the contract).

        The rows of a group are walked best first: the lowest source_priority number; then
        the one with more of the optional numbers filled in; then the more popular one; then
        the one imported first. The first row is kept. It keeps its own numbers and takes a
        few things from the rows merged into it (see _absorb).

        by_name=False (same source row, same barcode): the rows are one product, so every
        later row is merged into the first.

        by_name=True (same name + brand): the name alone does not prove it is the same food
        ("Butter Coconut" exists at 68 and at 500 kcal), so an Open Food Facts row is merged
        only into a kept row with matching calories (see _same_food) and otherwise stays as
        a row of its own. Among Open Food Facts rows, one whose calories fit its own protein,
        carbs and fat is walked before one whose numbers contradict each other. Curated and
        USDA rows are merged into the first row regardless, as they always were. When the
        kept row is marked doubtful and the row merged into it is not, the two change places
        (see _better_twin): the believable record of the product is the one that stays.

        Rows whose key is NULL are left alone. Returns how many rows were removed.
        """
        conn = self.conn
        # A row tagged only for the neighbouring markets ("AS") is walked after every row of
        # the same source that carries one of the five other markets, so it never replaces one
        # (see _absorb). Rows imported before "AS" existed never have region_tags 'AS', so
        # their order among themselves is what it always was.
        order = "source_priority, (region_tags = 'AS'), "
        if by_name:
            order += f"(source <> 'off' OR {MACRO_OK_SQL}) DESC, "
        else:
            # Two records of one barcode: one marked doubtful (pool_doubt) is walked after an
            # unmarked one of the same source, so the believable record is the one kept.
            order += "(doubt IS NULL) DESC, "
        order += f"{FILLED_SQL} DESC, popularity DESC, ord"
        facts = ("source, per_unit, calories_kcal, thumbnail_url, region_tags, barcode,"
                 " foreign_tags, popularity, portions, alt_barcodes, doubt")   # what _Kept reads, in its order
        # List the rows of every group of two or more, best row first, in a table of their own
        # so that it can be read while the candidates table is being changed.
        conn.execute("DROP TABLE IF EXISTS temp.dup")
        conn.execute(f"""
            CREATE TEMP TABLE dup AS
            SELECT rid, ({key_sql}) AS k, {facts}
            FROM cand
            WHERE ({key_sql}) IN (SELECT ({key_sql}) FROM cand WHERE ({key_sql}) IS NOT NULL
                                  GROUP BY 1 HAVING COUNT(*) > 1)
            ORDER BY 2, {order}""")

        updates, deletes, removed = [], [], 0

        def write():
            conn.executemany("UPDATE cand SET thumbnail_url = ?, region_tags = ?, barcode = ?, foreign_tags = ?,"
                             " popularity = ?, portions = ?, alt_barcodes = ?, doubt = ?, changed = 1"
                             " WHERE rid = ?", updates)
            conn.executemany("DELETE FROM cand WHERE rid = ?", deletes)
            updates.clear()
            deletes.clear()

        def close_group(kept):
            for keeper in kept:
                if keeper.changeable() != keeper.before:
                    updates.append(keeper.changeable() + (keeper.rid,))
            if len(updates) + len(deletes) >= BATCH:
                write()

        kept, group = [], None                              # the rows kept so far in the current group
        for record in conn.execute(f"SELECT rid, k, {facts} FROM dup ORDER BY rowid"):
            row = _Kept(record)
            if not kept or record[1] != group:              # first row of a group: always kept
                close_group(kept)
                kept, group = [row], record[1]
                continue
            keeper = _same_food(kept, row, group) if by_name else kept[0]
            if keeper is None:
                kept.append(row)                            # same name, different food: it stays
                continue
            if by_name and _better_twin(keeper, row):
                kept[kept.index(keeper)] = row              # row stays and takes in the marked keeper
                keeper, row = row, keeper
            _absorb(keeper, row, by_name)
            deletes.append((row.rid,))
            removed += 1
        close_group(kept)
        write()
        conn.execute("DROP TABLE temp.dup")
        return removed

    def save(self):
        """De-duplicate everything and bring food_items in line with the result."""
        conn = self.conn
        self.flush()

        # The same source row read twice in this run (for example one barcode on two lines).
        self.duplicates += self.collapse("source || ':' || source_ref")
        conn.execute("CREATE INDEX temp.cand_barcode ON cand (barcode)")

        # A row read again replaces the row saved earlier and keeps its id. What the saved
        # row had picked up from duplicates on an earlier run (picture, regions, merged
        # barcodes) is carried over, so re-importing one source alone does not lose it.
        # Popularity is a scan count that only Open Food Facts supplies: an Open Food Facts
        # row brings its own, a curated or USDA row keeps what merged rows had given it.
        conn.execute("""
            UPDATE cand SET item_id = f.id,
                            thumbnail_url = COALESCE(cand.thumbnail_url, f.thumbnail_url),
                            region_tags = regions_union(cand.region_tags, f.region_tags),
                            alt_barcodes = aliases_union(cand.alt_barcodes, f.alt_barcodes),
                            popularity = CASE WHEN cand.source = 'off' THEN cand.popularity
                                              ELSE MAX(cand.popularity, f.popularity) END
            FROM food_items AS f
            WHERE f.source = cand.source AND f.source_ref = cand.source_ref AND f.source_ref IS NOT NULL""")
        conn.execute("CREATE INDEX temp.cand_item_id ON cand (item_id)")
        # The same goes for a barcode the saved row had taken from a duplicate, unless the row
        # that barcode came from was read again in this run: then the steps below decide
        # afresh (and reach the same result when nothing changed), instead of two candidates
        # carrying one barcode.
        conn.execute("""
            UPDATE cand SET barcode = f.barcode
            FROM food_items AS f
            WHERE f.id = cand.item_id AND cand.barcode IS NULL AND f.barcode IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM cand AS other WHERE other.barcode = f.barcode)""")

        # Every other saved row joins the candidates unchanged. They were imported before
        # this run, so they get the lowest order numbers (all negative, oldest first).
        above = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM food_items").fetchone()[0]
        conn.execute(f"""
            INSERT INTO cand (item_id, ord, changed, {_COLUMN_LIST})
            SELECT f.id, f.id - ?, 0, {", ".join("f." + column for column in ITEM_COLUMNS)}
            FROM food_items AS f
            WHERE NOT EXISTS (SELECT 1 FROM cand WHERE cand.item_id = f.id)""", (above,))
        # A saved row without a key (added by another tool) gets one, or all such rows would look alike.
        conn.execute("UPDATE cand SET name_key = food_name_key(name, brand), changed = 1 WHERE name_key = ''")
        conn.execute("CREATE INDEX temp.cand_name_key ON cand (name_key)")

        self.duplicates += self.collapse("barcode")                     # step 1: same barcode
        self.duplicates += self.collapse("name_key", by_name=True)      # step 2: same name + brand

        # Write the result. Everything below is one transaction: all of it or none of it.
        try:
            # 1. saved rows that lost to a duplicate
            conn.execute("DELETE FROM food_items WHERE id NOT IN (SELECT item_id FROM cand WHERE item_id IS NOT NULL)")
            # 2. a barcode may move from one row to another and two rows may never share one,
            #    so clear the barcodes of the rows about to be rewritten first
            conn.execute("""UPDATE food_items SET barcode = NULL
                            WHERE barcode IS NOT NULL
                              AND id IN (SELECT item_id FROM cand WHERE changed = 1 AND item_id IS NOT NULL)""")
            # 3. saved rows that were read again or took something from a duplicate
            conn.execute(f"""
                UPDATE food_items SET {", ".join(f"{column} = c.{column}" for column in ITEM_COLUMNS)}
                FROM cand AS c
                WHERE c.item_id = food_items.id AND c.changed = 1""")
            # 4. new rows, in the order they were read
            conn.execute(f"""
                INSERT INTO food_items ({_COLUMN_LIST})
                SELECT {_COLUMN_LIST} FROM cand WHERE item_id IS NULL ORDER BY ord""")
            conn.execute("DROP TABLE temp.cand")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def summary(self):
        """The numbers for the final JSON line."""
        kept = {"curated": 0, "usda": 0, "off": 0}
        for source, count in self.conn.execute("SELECT source, COUNT(*) FROM food_items GROUP BY source"):
            if source in kept:
                kept[source] = count
        total = self.conn.execute("SELECT COUNT(*) FROM food_items").fetchone()[0]
        aliases = sum(len(aliases_read(text)) for (text,) in self.conn.execute(
            "SELECT alt_barcodes FROM food_items WHERE alt_barcodes <> '[]'"))
        return {"read": dict(self.read), "kept": kept, "filtered_region": self.filtered_region,
                "invalid": self.invalid, "duplicates": self.duplicates, "total_rows": total,
                "repaired": self.repaired, "filled": self.filled, "off_dropped": dict(self.off_dropped),
                "aliases": aliases, "serving_typed": dict(self.serving_typed), "asia_rebased": self.asia_rebased,
                "doubts": dict(sorted(self.doubts.items()))}


def _same_food(kept, row, key):
    """Which of the kept rows of a same-name group is the same food as row? None when none is.

    kept is in winner order, so the first match is the best one.
    """
    if row.source != "off":
        return kept[0]              # curated and USDA duplicates were always merged by name alone
    for keeper in kept:
        if keeper.per_unit != "100g" and keeper.source != "off":
            # A curated row stored per serving cannot be compared in calories. It takes the
            # row when the key names a brand ("mcdonalds|big mac"), not for a bare food name.
            if not key.startswith("|"):
                return keeper
        elif keeper.per_unit != row.per_unit:
            continue                # an Open Food Facts row per serving and one per 100 g: not comparable
        elif abs(keeper.kcal - row.kcal) <= max(MERGE_KCAL_SHARE * max(keeper.kcal, row.kcal), MERGE_KCAL_FLOOR):
            return keeper
    return None


def _philippine_kept(row):
    """Is this row of a group of duplicates a Philippine row (tagged PH first, or a Philippine barcode)?"""
    return (row.regions or "").startswith("PH") or barcode_origin(row.barcode) == "PH"


def _better_twin(keeper, row):
    """Should row, and not the kept row it matches, be the one that stays (same name, brand and calories)?

    Yes when the kept row is marked doubtful (see pool_doubt) for a reason that row was
    tested for and passed: its own macros, or the serving size when row states one of its
    own. Typical: "Cut green beans" at 20 kcal, 1 / 3 / 0 g (the label's half cup typed per
    100 g) next to the same can at 16.5 kcal, 0.83 / 3.31 / 0 g (worked out from it). Never
    for Philippine rows (they are not tested) and never for curated or USDA rows.
    """
    if keeper.source != "off" or row.source != "off" or not keeper.doubt or row.doubt:
        return False
    if only_as(row.regions) and not only_as(keeper.regions):
        return False                # a neighbouring-market row never replaces a row of the five markets
    if _philippine_kept(keeper) or _philippine_kept(row):
        return False
    return keeper.doubt in OWN_DOUBTS or (keeper.doubt in SERVING_DOUBTS and row.portions != "[]")


def _absorb(keeper, loser, by_name):
    """Merge loser into keeper: keeper keeps its own name and numbers and takes the following.

    Both kinds of merge
      - an empty picture or barcode is taken from the first merged row that has one
      - its regions become both rows' regions together
      - it counts as "sold abroad" only as much as the least foreign of the rows
      - a barcode it does not take, and the barcodes the loser had collected itself, are
        noted in alt_barcodes
    Exceptions for curated and USDA keepers
      - a curated keeper's regions are never changed, in either kind of merge: they are
        what the owner wrote in the curated file
      - in a merge by name + brand (by_name) a curated or USDA keeper takes no picture and
        no barcode: the merged row is some pack that shares its name, not a picture of
        "Banana". The barcode becomes an alt_barcode instead. (A USDA keeper still takes
        the regions, as it always did: the same food is sold there too.)
    Merges by name + brand only (by_name)
      - popularity becomes the higher of the two, so a product is not ranked by its least
        scanned pack
      - an Open Food Facts keeper without a serving size takes the merged row's
      - an Open Food Facts keeper from another market takes the merged row's doubt (see
        pool_doubt) when it has none and the doubt holds for it too (see OWN_DOUBTS and
        SERVING_DOUBTS): the two rows have the same name, brand and calories, so a
        dry-coffee figure or a shampoo is the same mistake in both, and so is a label typed
        per serving when the keeper has no serving size of its own to be tested with.
        (Without this the wrong figures would reach the app through the twin that merely
        lacks the serving size the pattern is read from.) Philippine rows are never marked.
    """
    if only_as(loser.regions) and not only_as(keeper.regions):
        # A row tagged only for the neighbouring markets ("AS") that matches a curated, USDA
        # or five-market row adds nothing to it but its barcodes (as alt_barcodes, so typing
        # the Thai or Malaysian pack's code still finds the row). Every other fact of the
        # kept row stays exactly as it was before "AS" was imported.
        extra = aliases_read(loser.aliases) if loser.aliases != "[]" else {}
        if loser.barcode and loser.barcode != keeper.barcode:
            aliases_add(extra, loser.barcode, loser.popularity, loser.regions)
        if extra:
            aliases = aliases_read(keeper.aliases)
            for code, (popularity, regions) in extra.items():
                aliases_add(aliases, code, popularity, regions)
            aliases.pop(keeper.barcode, None)
            keeper.aliases = aliases_text(aliases)
        return
    protected = by_name and keeper.source != "off"
    if keeper.thumbnail is None and loser.thumbnail and not protected:
        keeper.thumbnail = loser.thumbnail
    if keeper.source == "usda":
        # A USDA food is an American reference food: it takes no "AS" from a pack it matches.
        keeper.regions = region_text(set(regions_union(keeper.regions, loser.regions).split(",")) - {"AS"})
    elif keeper.source != "curated":
        keeper.regions = regions_union(keeper.regions, loser.regions)
    keeper.foreign_tags = min(keeper.foreign_tags or 0, loser.foreign_tags or 0)

    # Barcodes. The alt_barcodes text is only decoded when there is something to add.
    extra = aliases_read(loser.aliases) if loser.aliases != "[]" else {}
    if loser.barcode and loser.barcode != keeper.barcode:
        if keeper.barcode is None and not protected:
            keeper.barcode = loser.barcode
        else:
            aliases_add(extra, loser.barcode, loser.popularity, loser.regions)
    if extra:
        aliases = aliases_read(keeper.aliases)
        for code, (popularity, regions) in extra.items():
            aliases_add(aliases, code, popularity, regions)
        aliases.pop(keeper.barcode, None)                   # never its own barcode
        keeper.aliases = aliases_text(aliases)

    if by_name:
        keeper.popularity = max(keeper.popularity or 0, loser.popularity or 0)
        no_serving = keeper.portions == "[]"
        if keeper.source == "off" and no_serving and loser.portions != "[]":
            keeper.portions = loser.portions
        if (keeper.source == "off" and loser.doubt and not keeper.doubt and loser.doubt not in OWN_DOUBTS
                and (no_serving or loser.doubt not in SERVING_DOUBTS) and not _philippine_kept(keeper)):
            keeper.doubt = loser.doubt
    if "AS" in (keeper.regions or "").split(",") and _philippine_kept(keeper):
        # A row with a Philippine barcode is never also marked "AS" (see region_text).
        keeper.regions = region_text(set(keeper.regions.split(",")) - {"AS"})


# ---------------------------------------------------------------------------------------
# Curated lists (JSON)
# ---------------------------------------------------------------------------------------

def curated_entries(path):
    """Yield the entries of the file's "items" list one at a time.

    json.load would read the whole file first. Instead the file is read in blocks and each
    entry is decoded as soon as all of it has arrived, so only one entry is held at a time.
    """
    decoder = json.JSONDecoder()
    with open(path, "r", encoding="utf-8-sig") as handle:
        buffer, at_end = "", False

        def read_more():
            nonlocal buffer, at_end
            block = handle.read(65536)
            if block:
                buffer += block
            else:
                at_end = True

        def next_char(also_skip=""):
            """Drop white space (and the given punctuation) and return the next character, "" at the end."""
            nonlocal buffer
            while True:
                buffer = buffer.lstrip(" \t\r\n" + also_skip)
                if buffer or at_end:
                    return buffer[:1]
                read_more()

        def take_value():
            """Decode one complete JSON value from the front of the buffer."""
            nonlocal buffer
            while True:
                try:
                    value, end = decoder.raw_decode(buffer)
                    if end < len(buffer) or at_end:     # otherwise it may continue in the next block
                        buffer = buffer[end:]
                        return value
                except json.JSONDecodeError:
                    if at_end:
                        raise
                read_more()

        if next_char() != "{":
            raise InputError(f'{path}: expected a JSON object with an "items" list')
        buffer = buffer[1:]
        found_items = False
        while True:
            if next_char(",") in ("}", ""):
                break
            key = take_value()
            if next_char() != ":":
                raise InputError(f"{path}: not valid JSON")
            buffer = buffer[1:]
            if key != "items":
                next_char()
                take_value()                            # "version", "updated", "how_to_use": not needed
                continue
            if next_char() != "[":
                raise InputError(f'{path}: "items" must be a list')
            buffer = buffer[1:]
            found_items = True
            while True:
                char = next_char(",")
                if char == "]":
                    buffer = buffer[1:]
                    break
                if char == "":
                    raise InputError(f'{path}: the "items" list is not closed')
                yield take_value()
        if not found_items:
            raise InputError(f'{path}: no "items" list found')


def curated_slug(name, brand):
    """source_ref for a curated row: "lucky-me-pancit-canton-kalamansi"."""
    slug = "-".join(name_key(name, brand).replace("|", " ").split())
    return slug or "item-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def curated_row(entry):
    """Turn one curated entry into a row. Returns (row, None) or (None, why it was refused)."""
    if not isinstance(entry, dict):
        return None, "not an object"
    row = blank_row("curated")
    row["name"] = clean_text(entry.get("name"))
    if not row["name"]:
        return None, "no name"
    row["brand"] = clean_text(entry.get("brand")) or None
    row["group_label"] = clean_text(entry.get("group")) or row["brand"]
    row["category"] = entry.get("category")
    if row["category"] not in CATEGORIES:
        return None, f"category must be one of {', '.join(CATEGORIES)}"

    regions = entry.get("region_tags") or []
    if isinstance(regions, str):
        regions = regions.split(",")
    regions = [str(code).strip() for code in regions if str(code).strip()]
    unknown = [code for code in regions if code not in REGIONS]
    if unknown:
        return None, f"unknown region {unknown[0]!r} (use {', '.join(REGIONS)})"
    row["region_tags"] = region_text(set(regions))

    row["barcode"] = normalize_barcode(entry.get("barcode"))
    row["thumbnail_url"] = http_url(entry.get("thumbnail_url"))
    aka = entry.get("aka") if isinstance(entry.get("aka"), list) else []
    row["aka"] = [clean_text(other) for other in aka if isinstance(other, str) and clean_text(other)]
    basis = clean_text(entry.get("basis")).lower()
    row["basis"] = basis if basis in BASES else None

    for column in ("calories_kcal",) + GRAM_COLUMNS + ("sodium_mg",):
        row[column] = to_number(entry.get(column))
    if row["calories_kcal"] is None:
        return None, "no calories"

    serving = entry.get("serving") if isinstance(entry.get("serving"), dict) else {}
    label = clean_text(serving.get("label")) or "1 serving"
    grams = to_number(serving.get("grams"))
    if grams is not None and grams <= 0:
        grams = None
    extra = []
    for portion in entry.get("extra_portions") or []:
        if isinstance(portion, dict):
            extra_label, extra_grams = clean_text(portion.get("label")), to_number(portion.get("grams"))
            if extra_label and extra_grams and extra_grams > 0 and extra_label.lower() != label.lower():
                extra.append([extra_label, tidy_number(round(extra_grams, 2))])

    per = entry.get("nutrition_per")
    if per == "serving" and grams:
        # Numbers for a serving of known weight: convert them to per 100 g.
        for column in ("calories_kcal",) + GRAM_COLUMNS + ("sodium_mg",):
            if row[column] is not None:
                row[column] = row[column] * 100 / grams
        row["portions"] = [[label, tidy_number(round(grams, 2))]] + extra
    elif per == "serving":
        # The source gives no weight: keep the numbers per serving.
        row["per_unit"] = "serving"
        row["portions"] = [[label, None]]
    elif per == "100g":
        row["portions"] = ([[label, tidy_number(round(grams, 2))]] if grams else []) + extra
    else:
        return None, 'nutrition_per must be "serving" or "100g"'

    # Optional "default_portion": "100g" puts 100 g first, "serving" pins the stated serving first
    # (the export rule in default_portions() would otherwise decide). Stored in the portions
    # list, no schema change: "100g" is simply the stored order, "serving" adds a third item
    # PIN_MARK to the first portion, which the exporter strips again.
    mode = clean_text(entry.get("default_portion")).lower()
    if mode not in ("", "serving", "100g"):
        return None, 'default_portion must be "serving" or "100g"'
    if mode and row["portions"] and row["portions"][0][1]:
        if mode == "100g":
            row["portions"] = [["100 g", 100]] + [p for p in row["portions"] if p[1] != 100]
        else:
            row["portions"][0] = row["portions"][0] + [PIN_MARK]

    if not apply_rules(row):
        return None, "numbers out of range (negative, or impossible for 100 g)"
    row["source_ref"] = curated_slug(row["name"], row["brand"])
    return row, None


def import_curated(importer, path):
    count = kept = 0
    try:
        for entry in curated_entries(path):
            count += 1
            importer.read["curated"] += 1
            row, problem = curated_row(entry)
            if row is None:
                importer.invalid += 1
                shown = entry.get("name") if isinstance(entry, dict) else None
                importer.warn(f"{path}: item {count} ({shown!r}) skipped: {problem}")
                continue
            importer.stage(row)
            kept += 1
    except json.JSONDecodeError as problem:
        raise InputError(f"{path}: not valid JSON after item {count} ({problem})") from None
    importer.flush()
    importer.say(f"curated  {path}: read {count}, accepted {kept}")


# ---------------------------------------------------------------------------------------
# USDA FoodData Central
# ---------------------------------------------------------------------------------------

def _plain(text):
    """Lower case without apostrophes and full stops: "McDONALD'S" -> "mcdonalds"."""
    return re.sub(r"['’`.]", "", text.casefold())


_CHAIN_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(_plain(chain)) for chain in RESTAURANT_CHAINS) + r")(?![a-z0-9])")
# Chain names that are also everyday food words ("chipotle dip", "chilis"): they only count as the
# restaurant when written the way USDA names a chain: "(Chipotle)" or "Chipotle, burrito bowl".
_FOOD_WORD_CHAINS = ("chipotle", "chilis")


def _names_chain(text):
    for match in _CHAIN_PATTERN.finditer(text):
        if match.group(0) not in _FOOD_WORD_CHAINS:
            return True
        before, after = text[:match.start()], text[match.end():].lstrip()
        in_brackets = before.rfind("(") > before.rfind(")") and ")" in after
        leads_name = not before.strip() and (not after or after[0] == ",")
        if in_brackets or leads_name:
            return True
    return False


def usda_category(data_type, gtin, food_category, description):
    """supermarket (packaged, branded), dining_out (fast food and restaurant items) or whole_food."""
    if data_type == "branded_food" or gtin:
        return "supermarket"
    text = _plain(description)
    if food_category.casefold() in ("fast foods", "restaurant foods"):
        # USDA files frozen supermarket pizzas and school-lunch items under "Fast Foods" too.
        if "school lunch" in text:
            return "whole_food"
        if "frozen" in text:
            return "supermarket"
        return "dining_out"
    if "fast food" in text or "restaurant" in text or _names_chain(text):
        return "dining_out"
    return "whole_food"


def usda_row(fdc_id, data_type, name, description, food_category, brand_owner, gtin, fallback_prefix):
    """The parts of a USDA row that do not depend on the file layout (numbers are added by the caller)."""
    row = blank_row("usda")
    row["name"] = name
    row["region_tags"] = "US"
    row["group_label"] = food_category or None
    row["category"] = usda_category(data_type, gtin, food_category, description)
    if row["category"] == "supermarket":
        row["brand"] = clean_text(brand_owner) or None
        row["barcode"] = normalize_barcode(gtin)
    prefix = USDA_REF_PREFIX.get(data_type, fallback_prefix)
    # A row without an id is told apart by its name, so reading it again still replaces it.
    row["source_ref"] = f"{prefix}:{fdc_id}" if fdc_id else f"{prefix}:name:{name_key(name, row['brand'])}"
    return row


def import_usda_flat(importer, path):
    """One CSV file, one food per line, numbers per 100 g (layout: USDA_FLAT_COLUMNS)."""
    count = kept = 0
    found = set()
    for record in csv_records(path, USDA_FLAT_COLUMNS, found):
        count += 1
        importer.read["usda"] += 1
        if record is None:
            importer.invalid += 1
            continue
        (fdc_id, data_type, description, brand_owner, gtin, food_category, serving_g, household,
         kcal, kj, protein, carbs, sugars, fat, saturates, fibre, sodium) = record
        name = clean_text(description)
        if not name:
            importer.invalid += 1
            continue
        row = usda_row(fdc_id.strip(), data_type.strip(), name, description, clean_text(food_category),
                       brand_owner, gtin.strip(), "fdc")
        row["calories_kcal"] = to_number(kcal)
        if row["calories_kcal"] is None and to_number(kj) is not None:
            row["calories_kcal"] = kcal_from_kj(to_number(kj))
        row["protein_g"], row["carbs_g"], row["sugars_g"] = to_number(protein), to_number(carbs), to_number(sugars)
        row["fat_g"], row["saturates_g"], row["fibre_g"] = to_number(fat), to_number(saturates), to_number(fibre)
        row["sodium_mg"] = to_number(sodium)                # already milligrams
        label, grams = clean_text(household), to_number(serving_g)
        if label and grams and grams > 0:
            row["portions"] = [[label[:60], tidy_number(round(grams, 2))]]
        if not apply_rules(row):
            importer.invalid += 1
            continue
        importer.stage(row)
        kept += 1
    if count and not {"description", "energy_kcal"} <= found:
        importer.warn(f"{path}: no 'description' / 'energy_kcal' columns; is this the flat USDA layout?")
    importer.flush()
    importer.say(f"usda     {path}: read {count}, accepted {kept}")


# --- FoodData Central folders. This part repeats what the first builder, tools/build_food_db.py, did (the script
# --- that built version 1 of food-db.json): same tidied names, same skipped foods, same
# --- portions in the same order, same calorie fallback.

KEEP_CAPS = {"KFC", "USDA", "BBQ", "UHT", "IHOP", "DHA", "ARA", "USA", "WIC", "NFS", "MRE", "BLT", "TGI",
             "PBJ", "GMO", "CLA", "MCT", "RTD", "RTE", "RTF", "RTB", "RTS", "STK", "NBC", "CVS", "HFCS", "A&W"}
SKIP_CATEGORIES = {"Baby Foods", "Quality Control Materials"}
SKIP_NAME = re.compile(r"^(baby ?food|infant formula|milk, human|formula,|human milk)"
                       r"|\binfant formula\b|\bbaby food\b|babyfood|toddler formula", re.I)
_FRACTIONS = {0.5: "1/2", 0.25: "1/4", 0.75: "3/4", 0.333: "1/3", 0.667: "2/3", 0.125: "1/8"}


def tidy_name(text):
    """USDA descriptions -> readable names: drops "NFS", stops SR Legacy shouting brand names."""
    text = text.strip()
    text = re.sub(r",\s*NFS\b", "", text)
    text = re.sub(r"\bNS as to\b", "unspecified", text)
    text = re.sub(r"\bNFS\b", "unspecified", text)
    text = re.sub(r"\s+", " ", text).strip(" ,")

    def lower_word(match):
        word = match.group(0)
        return word if word in KEEP_CAPS else word[0] + word[1:].lower()

    text = text.replace("McDONALD'S", "McDonald's")
    text = re.sub(r"Mc([A-Z])([A-Z]{2,})", lambda m: "Mc" + m.group(1) + m.group(2).lower(), text)
    text = re.sub(r"\s*\(Includes foods for USDA's Food Distribution Program\)", "", text)
    text = re.sub(r"(?<![A-Za-z'])[A-Z][A-Z']{2,}(?![A-Za-z])", lower_word, text)
    return text


def fraction_text(amount):
    """0.5 -> "1/2", 2.0 -> "2", 1.5 -> "1.5"."""
    amount = tidy_number(round(amount, 3))
    return _FRACTIONS.get(amount, str(amount))


def order_portions(found):
    """found = [(label, grams), ...] sorted as in the USDA file -> [[label, grams], ...], default first.

    At most five household portions are kept. The default is the portion matching the
    survey's "typical amount" when there is one, otherwise the first that is not a bare
    ounce measure.
    """
    portions, seen = [], set()
    for label, grams in found:
        if label.lower() not in seen:
            seen.add(label.lower())
            portions.append([label, tidy_number(grams)])
    typical = [p for p in portions if p[0].startswith("1 serving (typical")]
    portions = [p for p in portions if not p[0].startswith("1 serving (typical")][:5]
    first = None
    if typical:
        same = [p for p in portions if p[1] == typical[0][1]]
        if same:
            first = same[0]
        else:
            first = ["1 serving", typical[0][1]]
            portions.append(first)
    if first is None:
        plain = [p for p in portions if not re.match(r"^[\d./]+ (fl )?oz\b", p[0])]
        first = plain[0] if plain else None
    if first is not None:
        portions.remove(first)
        portions.insert(0, first)
    return portions


def import_usda_folder(importer, folder):
    """An unzipped FoodData Central CSV download (Survey / FNDDS, SR Legacy, Foundation or Branded).

    food_nutrient.csv has one line per food per nutrient and can be very large, so the few
    nutrients needed are gathered into a temporary table, one row per food, and food.csv
    is then walked line by line.
    """
    conn = importer.conn

    def inside(file_name):
        return os.path.join(folder, file_name)

    for needed in ("food.csv", "food_nutrient.csv"):
        if not os.path.isfile(inside(needed)):
            raise InputError(f"{folder}: {needed} not found (expected a FoodData Central CSV folder)")
    # Survey folders describe portions and food groups differently from the others.
    survey_style = os.path.isfile(inside("wweia_food_category.csv"))

    # Food group names: a short list, kept in memory.
    categories = {}
    if survey_style:
        source = ("wweia_food_category.csv", ("wweia_food_category", "wweia_food_category_description"))
    else:
        source = ("food_category.csv", ("id", "description"))
    if os.path.isfile(inside(source[0])):
        for record in csv_records(inside(source[0]), source[1]):
            if record is not None:
                categories[record[0]] = record[1]

    # Nutrients: one row per food. A repeated figure overwrites the earlier one, except the
    # stand-in energy figure, where the first one seen is kept (as the old script did).
    conn.execute("DROP TABLE IF EXISTS temp.fdc_nutrients")
    conn.execute("""CREATE TEMP TABLE fdc_nutrients (
                        fdc_id TEXT PRIMARY KEY, kcal REAL, kcal_alt REAL, protein REAL, fat REAL,
                        carbs REAL, sugars REAL, saturates REAL, fibre REAL, sodium REAL)""")
    waiting = {column: [] for column in sorted(set(NUTRIENT_COLUMN.values()))}

    def write_nutrients():
        for column, values in waiting.items():
            if values:
                new = (f"COALESCE(fdc_nutrients.{column}, excluded.{column})" if column == "kcal_alt"
                       else f"excluded.{column}")
                conn.executemany(f"""INSERT INTO fdc_nutrients (fdc_id, {column}) VALUES (?, ?)
                                     ON CONFLICT (fdc_id) DO UPDATE SET {column} = {new}""", values)
                values.clear()
        conn.commit()

    held = 0
    for record in csv_records(inside("food_nutrient.csv"), ("fdc_id", "nutrient_id", "amount")):
        if record is None:
            continue
        column = NUTRIENT_COLUMN.get(record[1])
        amount = to_number(record[2]) if column else None
        if amount is None:
            continue
        waiting[column].append((record[0], amount))
        held += 1
        if held >= 10 * BATCH:
            write_nutrients()
            held = 0
    write_nutrients()

    # Household portions: (food, position in the file's own order, label, grams).
    conn.execute("DROP TABLE IF EXISTS temp.fdc_portions")
    conn.execute("CREATE TEMP TABLE fdc_portions (fdc_id TEXT, seq INTEGER, label TEXT, grams REAL)")
    if os.path.isfile(inside("food_portion.csv")):
        batch = []
        for record in csv_records(inside("food_portion.csv"), ("fdc_id", "seq_num", "amount",
                                                               "portion_description", "modifier", "gram_weight")):
            if record is None:
                continue
            fdc_id, seq, amount, description, modifier, gram_weight = record
            grams = to_number(gram_weight)
            if not grams or grams <= 0:
                continue
            if survey_style:
                label = description.strip()
                if not label:
                    continue
                if label.lower() == "quantity not specified":
                    label = "1 serving (typical amount)"
            else:
                modifier = modifier.strip()
                if not modifier:
                    continue
                label = fraction_text(to_number(amount) or 1) + " " + modifier
            label = re.sub(r"\s+", " ", re.sub(r",? \(?NFS\)?", "", label)).strip()[:60]
            batch.append((fdc_id, int(to_number(seq) or 99), label, round(grams, 1)))
            if len(batch) >= BATCH:
                conn.executemany("INSERT INTO fdc_portions VALUES (?, ?, ?, ?)", batch)
                batch.clear()
        conn.executemany("INSERT INTO fdc_portions VALUES (?, ?, ?, ?)", batch)
    conn.execute("CREATE INDEX temp.fdc_portions_food ON fdc_portions (fdc_id)")

    # Branded downloads carry brand, barcode and serving in a file of their own.
    branded = os.path.isfile(inside("branded_food.csv"))
    conn.execute("DROP TABLE IF EXISTS temp.fdc_branded")
    if branded:
        conn.execute("""CREATE TEMP TABLE fdc_branded (
                            fdc_id TEXT PRIMARY KEY, brand_owner TEXT, gtin TEXT, category TEXT,
                            serving TEXT, unit TEXT, household TEXT)""")
        batch = []
        for record in csv_records(inside("branded_food.csv"), (
                "fdc_id", "brand_owner", "gtin_upc", "branded_food_category", "serving_size",
                "serving_size_unit", "household_serving_fulltext")):
            if record is None:
                continue
            batch.append(record)
            if len(batch) >= BATCH:
                conn.executemany("INSERT OR REPLACE INTO fdc_branded VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
                batch.clear()
        conn.executemany("INSERT OR REPLACE INTO fdc_branded VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
    conn.commit()

    # Now the foods themselves, in the file's order.
    count = kept = skipped = 0
    for record in csv_records(inside("food.csv"), ("fdc_id", "data_type", "description", "food_category_id")):
        count += 1
        importer.read["usda"] += 1
        if record is None:
            importer.invalid += 1
            continue
        fdc_id, data_type, description, category_id = record
        data_type = data_type.strip()
        numbers = conn.execute("SELECT kcal, kcal_alt, protein, fat, carbs, sugars, saturates, fibre, sodium"
                               " FROM fdc_nutrients WHERE fdc_id = ?", (fdc_id,)).fetchone()
        kcal = None if numbers is None else (numbers[0] if numbers[0] is not None else numbers[1])
        if kcal is None:
            importer.invalid += 1                           # no calories: nothing to log with
            continue
        brand_owner = gtin = ""
        extra_portion = None
        category = categories.get(category_id, "")
        if branded:
            pack = conn.execute("SELECT brand_owner, gtin, category, serving, unit, household"
                                " FROM fdc_branded WHERE fdc_id = ?", (fdc_id,)).fetchone()
            if pack is not None:
                brand_owner, gtin = pack[0], pack[1].strip()
                category = category or clean_text(pack[2])
                label, grams = clean_text(pack[5]), to_number(pack[3])
                if label and grams and grams > 0 and pack[4].strip().lower() in ("g", "grm", "ml", "mlt"):
                    extra_portion = [label[:60], tidy_number(round(grams, 2))]
        if category.lower().startswith("not included"):
            category = "Ingredient"
        if (category in SKIP_CATEGORIES or SKIP_NAME.search(description)
                or category.lower().startswith(("baby", "human milk", "formula"))):
            skipped += 1                                    # baby food, infant formula and the like
            continue
        name = tidy_name(description)
        if not name:
            importer.invalid += 1
            continue
        row = usda_row(fdc_id.strip(), data_type, name, description, category, brand_owner, gtin,
                       "survey" if survey_style else "fdc")
        row["calories_kcal"] = kcal
        (row["protein_g"], row["fat_g"], row["carbs_g"], row["sugars_g"], row["saturates_g"],
         row["fibre_g"], row["sodium_mg"]) = numbers[2:]
        found = conn.execute("SELECT label, grams FROM fdc_portions WHERE fdc_id = ?"
                             " ORDER BY seq, label, grams", (fdc_id,)).fetchall()
        row["portions"] = order_portions(found)
        if extra_portion and not row["portions"]:
            row["portions"] = [extra_portion]
        if not apply_rules(row):
            importer.invalid += 1
            continue
        importer.stage(row)
        kept += 1
    importer.flush()
    for table in ("fdc_nutrients", "fdc_portions", "fdc_branded"):
        conn.execute(f"DROP TABLE IF EXISTS temp.{table}")
    importer.say(f"usda     {folder}: read {count}, accepted {kept}, "
                 f"left out on purpose (baby food, infant formula) {skipped}")


# ---------------------------------------------------------------------------------------
# Open Food Facts
# ---------------------------------------------------------------------------------------

# --- Open Food Facts: checks and repairs ------------------------------------------------
# Open Food Facts is typed in from the pack by volunteers, and a few rows in every hundred
# are wrong in ways the row itself gives away. The helpers below are used by import_off
# only, in the order they appear there; curated and USDA rows never pass through them.
# The thresholds were measured on the real export (1.04 million usable rows).

# --- names ---

_CJK = re.compile("[぀-ヿ㐀-鿿가-힯豈-﫿ｦ-ﾟ]")   # kana, Chinese characters, hangul
_PLACEHOLDER_NAME = re.compile(r"unknown|unnamed|untitled|test|n/a|none|null|no name|product|food|item"
                               r"|undefined|x+|a{3,}|abc", re.I)
_PLACEHOLDER_BRAND = re.compile(r"unknown|undefined|none|null|n/a|na|no brand|nobrand|no name|generic"
                                r"|sans marque|test|\?|-|x", re.I)
# Text copied from the back of the pack into the name field.
_LABEL_TEXT = re.compile(r"\b(?:best before|use by|store in a|keep refrigerated|keep frozen|nutrition info"
                         r"|calories per)", re.I)
_INGREDIENTS = re.compile(r"ingredients:", re.I)
# The website's placeholder saved as a name ("Loading…", "Loading..."), a slogan copied from
# the pack ("Mi sedaap. Mi segara. Jelas terasa sedapnya. ..."), and text that starts in the
# middle of a sentence ("at seasonings, kaya bagay sa kahit"; 36 names in the whole export,
# none of them a usable name).
_LOADING = re.compile(r"loading\s*(?:…|\.{2,})?", re.I)
_SLOGAN = re.compile(r"jelas terasa|we'?re here to help|is this halal|^\W*only one image\W*$", re.I)
_MID_SENTENCE = re.compile(r"(?:at|and|or|of|to|for|is|are|kaya|sa|ng)\s")      # lower case on purpose


def decode_entities(value):
    """Clean text with HTML entities turned back into characters.

    Some names were escaped more than once on their way into the export:
    "Oats &amp;amp; Honey" -> "Oats &amp; Honey" -> "Oats & Honey". Three rounds are plenty.
    """
    text = clean_text(value)
    for _ in range(3):
        decoded = html.unescape(text) if "&" in text else text
        if decoded == text:
            break
        text = decoded
    return clean_text(text)


def letter_count(text):
    return sum(1 for ch in text if ch.isalpha())


def junk_name(name):
    """True for text that is not a food name: a barcode, a stray letter, "test", label text."""
    core = "".join(ch for ch in name if ch.isalnum())       # letters and digits only
    if not core or core.isdigit():
        return True
    if not _CJK.search(name):
        # One character, or two plain letters ("h", "xx"). Short names with a digit are real
        # ("c2", "R-1"), and so is a single Japanese, Chinese or Korean character.
        if len(core) == 1 or (len(core) == 2 and core.isascii() and core.isalpha()):
            return True
    # "Unknown", "N/A", "test." ... (stray punctuation around the word does not save it)
    bare = name.strip(" .,;:!?-_*'\"()[]")
    if _LOADING.fullmatch(bare) or _SLOGAN.search(name) or _MID_SENTENCE.match(name):
        return True
    return bool(_PLACEHOLDER_NAME.fullmatch(bare) or _LABEL_TEXT.search(name))


def usable_name(*choices):
    """The first of the given texts that works as a food name, or "" when none does.

    "SWEET PICKLE INGREDIENTS: CUCUMBER, ..." keeps the part before "ingredients:" when
    that is at least four letters long.
    """
    for name in choices:
        found = _INGREDIENTS.search(name)
        if found:
            name = name[:found.start()].rstrip(" -,;:.(")
            if letter_count(name) < 4:
                continue
        if name and not junk_name(name):
            return name
    return ""


def adds_search_words(generic, name):
    """Is the product's "generic name" worth keeping as another name to search by?

    Till-style Philippine names such as "nc bulalo 40g" come with a readable generic name
    ("Nissin Cup Noodles Bulalo"). It is kept when it is short, in Latin letters and not
    already part of the name.
    """
    if not 4 <= len(generic) <= 60 or not re.search("[A-Za-z]", generic):
        return False
    plain = plain_words(generic)
    return bool(plain) and plain not in plain_words(name)


# --- sodium ---

def off_sodium_mg(sodium_g, salt_g):
    """Milligrams of sodium from the file's two fields (grams of sodium, grams of salt), or None.

    The two should say the same thing (salt is 40 % sodium). When only one is usable it is
    taken. When they disagree and one of them is next to nothing, that one is a unit slip
    and the other is taken. When they disagree otherwise there is no telling which is
    right, and the row gets no sodium figure.
    """
    from_sodium = None if sodium_g is None else sodium_g * 1000
    from_salt = None if salt_g is None else sodium_mg_from_salt_g(salt_g)
    sodium_ok = from_sodium is not None and 0 <= from_sodium <= MAX_SODIUM_MG
    salt_ok = from_salt is not None and 0 <= from_salt <= MAX_SODIUM_MG
    if sodium_ok and salt_ok:
        low, high = min(from_sodium, from_salt), max(from_sodium, from_salt)
        if high - low <= max(0.25 * high, 50):
            return from_sodium
        if low < 5 and 50 <= high <= 2000:
            return high
        return None
    if sodium_ok:
        return from_sodium
    if salt_ok and (from_sodium is None or from_salt > 0):
        return from_salt            # an impossible sodium figure is only replaced by real salt, not by 0
    return None


def sodium_typed_in_mg(sodium_mg, serving_g):
    """True when a high sodium figure is milligrams typed where grams were asked for.

    More than 3 500 mg per 100 g is real for seasonings, but then one serving is a few
    grams. When one stated serving alone would hold over 4 000 mg, the figure is wrong.
    Nothing can be said when the serving is unknown or is simply 100 g.
    """
    return (sodium_mg is not None and sodium_mg > 3500 and serving_g is not None
            and not 95 <= serving_g <= 105 and sodium_mg * serving_g / 100 > 4000)


# --- calories against protein, carbs and fat ---

_ALCOHOL_WORDS = re.compile(
    r"\b(?:beer|lager|ale|stout|wine|vodka|whisky|whiskey|rum|gin|tequila|brandy|liqueur|sake|soju|cider"
    r"|seltzer|chu-hi)\b"
    r"|ビール|酒|チューハイ|サワー|맥주|소주"                 # beer, sake, chu-hi, sour; beer, soju
    r"|(?<!\d)\d{1,2}\s?[%％]", re.I)                         # "7%" on a can


def is_alcoholic(row, alcohol, categories):
    """Alcohol carries calories that are in none of the macros, so such a drink is expected
    to have "too many" calories. A name alone only counts for something with next to no
    protein and fat ("86% cacao" is not a drink)."""
    if alcohol is not None and alcohol > 0.5:
        return True
    if "alcoholic beverages" in [token.strip().lower() for token in categories.split(",")]:
        return True
    return ((row["protein_g"] or 0) < 2 and (row["fat_g"] or 0) < 1
            and _ALCOHOL_WORDS.search(row["name"]) is not None)


def calorie_conflict(row, kcal, serving_g, alcohol, categories):
    """Do kcal calories contradict the row's own protein, carbs and fat? "" when they do not.

    Protein and carbs give about 4 kcal per gram and fat 9, so the macros put a floor and
    a ceiling on the calories:
      "a"  fewer calories than protein, sugars and fat alone account for
      "b"  fewer than protein, carbs (less fibre) and fat account for; not asked of items
           that are nearly all sweetener or water, whose carbs carry few calories
      "c"  more than everything together explains (all three macros must be known);
           not asked of alcoholic drinks
    Each test allows 30 to 40 % plus 10 to 20 kcal of slack. A label rounded for one small
    serving and scaled up to 100 g can be off by more: then the slack is 1 400 kcal divided
    by the serving weight, when that is larger. Missing numbers count as 0.
    """
    protein, carbs, sugars = row["protein_g"] or 0, row["carbs_g"] or 0, row["sugars_g"] or 0
    fat, fibre = row["fat_g"] or 0, row["fibre_g"] or 0
    sure_sugars = min(sugars, carbs) if row["carbs_g"] is not None else sugars
    alcohol_g = alcohol if alcohol is not None and 0 < alcohol <= 100 else 0
    floor_sugars = 4 * protein + 4 * sure_sugars + 9 * fat
    floor_carbs = 4 * protein + 4 * max(carbs - fibre, 0) + 9 * fat
    ceiling = 4 * protein + 4 * max(carbs, sugars) + 9 * fat + 2 * fibre + 5.5 * alcohol_g
    rounding = 1400 / serving_g if serving_g is not None and serving_g < 100 else 0

    if kcal < floor_sugars - max(0.30 * floor_sugars + 10, rounding):
        return "a"
    if not (protein < 1 and fat < 1 and sugars < 5) and kcal < floor_carbs - max(0.40 * floor_carbs + 10, rounding):
        return "b"
    if (kcal > ceiling + max(0.30 * ceiling + 20, rounding)
            and None not in (row["protein_g"], row["carbs_g"], row["fat_g"])
            and not is_alcoholic(row, alcohol, categories)):
        return "c"
    return ""


def _label_round(grams):
    """Is this a figure as printed on a label: a multiple of half a gram (or missing)?"""
    return grams is None or abs(grams * 2 - round(grams * 2)) <= 0.04


def repair_calories(row, conflict, serving_g, kilojoules, alcohol, categories):
    """Try the two safe repairs on a row that failed calorie_conflict. True when one worked.

    1. "0 kcal" next to a real kilojoule figure: take the calories from the kilojoules,
       when all three macros are known and agree with the result. Marked as an estimate.
    2. Calories per 100 g but protein, carbs and fat typed per serving, exactly as printed
       (multiples of half a gram): scale the macros up (or, for a bottle or a whole meal,
       down) to 100 g. Only done when the calories then fit within 15 %. Sodium is scaled
       too when it is a round label figure, otherwise it is dropped. Calories are kept.
    Anything else stays unrepaired: there is no telling which of its numbers is wrong.
    """
    kcal, protein, carbs, fat = row["calories_kcal"], row["protein_g"], row["carbs_g"], row["fat_g"]
    if (kcal == 0 and kilojoules is not None and kilojoules > 0
            and None not in (protein, carbs, fat) and protein + carbs + fat <= 100):
        from_kilojoules = kcal_from_kj(kilojoules)
        if (from_kilojoules <= MAX_KCAL_PER_100G
                and not calorie_conflict(row, from_kilojoules, serving_g, alcohol, categories)):
            row["calories_kcal"], row["basis"] = from_kilojoules, "estimate"
            return True

    if serving_g is None or kcal <= 0:
        return False
    small_serving = conflict == "c" and 5 <= serving_g <= 60            # a handful, a slice, a bar
    large_serving = conflict in ("a", "b") and 125 <= serving_g <= 1000   # a bottle, a ready meal
    from_macros = 4 * (protein or 0) + 4 * (carbs or 0) + 9 * (fat or 0)
    if not (small_serving or large_serving) or from_macros < 5:
        return False
    if not (_label_round(protein) and _label_round(carbs) and _label_round(fat)):
        return False
    scale = 100.0 / serving_g
    if abs(kcal / from_macros - scale) > 0.15 * scale:
        return False
    scaled = {column: row[column] * scale for column in GRAM_COLUMNS if row[column] is not None}
    if any(value > 100 for value in scaled.values()):
        return False
    if scaled.get("protein_g", 0) + scaled.get("carbs_g", 0) + scaled.get("fat_g", 0) > 105:
        return False
    row.update(scaled)
    sodium = row["sodium_mg"]
    if sodium is not None:
        on_the_label = abs(sodium / 5 - round(sodium / 5)) < 0.002      # a multiple of 5 mg
        row["sodium_mg"] = sodium * scale if on_the_label else None
    return True


def fill_missing_macro(row, alcohol, categories):
    """When exactly one of protein, carbs and fat is blank, work it out from the calories.

    The app shows a blank macro as 0 g, which is wrong for instant noodles with no carbs
    figure. The missing one is what the calories leave over after the other two; it is
    only filled when that comes to between 1 and 100 g. Marked as an estimate. Returns
    True when a figure was filled in. Alcoholic drinks are left alone (their spare
    calories are alcohol, not carbs).
    """
    macros = ("protein_g", "carbs_g", "fat_g")
    blank = [column for column in macros if row[column] is None]
    if len(blank) != 1 or is_alcoholic(row, alcohol, categories):
        return False
    known_kcal = 4 * (row["protein_g"] or 0) + 4 * (row["carbs_g"] or 0) + 9 * (row["fat_g"] or 0)
    grams = (row["calories_kcal"] - known_kcal) / (9 if blank[0] == "fat_g" else 4)
    if not 1 <= grams <= 100 or grams + sum(row[column] or 0 for column in macros) > 105:
        return False
    row[blank[0]], row["basis"] = grams, "estimate"
    return True


def reconcile_parts(row):
    """Sugars are part of the carbs and saturates part of the fat: a part cannot exceed its whole.

    A small excess (up to 1 g or 10 %) is rounding and the part is set to the whole. A
    larger one means one of the two is wrong: the part is dropped, unless the calories show
    that it is the carbs figure that is wrong (a soft drink with "carbs 0, sugars 10.6"),
    in which case carbs become the sugars. Nothing is done when the whole is missing.
    """
    carbs, sugars = row["carbs_g"], row["sugars_g"]
    if carbs is not None and sugars is not None and sugars > carbs:
        if sugars <= carbs + max(1, 0.10 * carbs):
            row["sugars_g"] = carbs
        else:
            protein, fat, kcal = row["protein_g"] or 0, row["fat_g"] or 0, row["calories_kcal"]

            def explains_calories(carbohydrate):
                expected = 4 * protein + 4 * carbohydrate + 9 * fat
                return abs(kcal - expected) <= max(0.15 * expected, 10)

            if explains_calories(sugars) and not explains_calories(carbs) and protein + sugars + fat <= 105:
                row["carbs_g"] = sugars
            else:
                row["sugars_g"] = None
    fat, saturates = row["fat_g"], row["saturates_g"]
    if fat is not None and saturates is not None and saturates > fat:
        row["saturates_g"] = fat if saturates <= fat + max(1, 0.10 * fat) else None


# --- serving size ---

_COMMA_THOUSANDS = re.compile(r"\d{1,3},\d{3}\s*(?:g|ml)", re.I)                    # "1,330g"
_BARE_NUMBER = re.compile(r"\d+(?:\.\d+)?")                                        # "30"
_NUMBER_AND_UNIT = re.compile(r"\d+(?:\.\d+)?\s*(?:g|ml)", re.I)                    # "1 g"
_PLAIN_100 = re.compile(r"(?:1 )?(?:portion|serving)? ?\(?100 ?(?:g|ml)\)?", re.I)  # "1 portion (100 g)"


def off_portions(serving_size, serving_g, kcal):
    """The portions list for an Open Food Facts row: [] or one [label, grams] entry.

    The first portion is the amount the app logs by default, so a wrong one does harm:
      - "1,330g" read by the source as 1.33 is put right
      - a bare "30" with no weight is taken as 30 g
      - no portion is kept when it would be over 1 000 kcal (a whole jar or pack, or a
        typing slip), when it is just "100 g" (the app offers that anyway), or when it is
        "1 g" or less with no household measure
    """
    label = clean_text(serving_size)
    if not label:
        return []
    grams = serving_g
    if grams is None:
        if not (_BARE_NUMBER.fullmatch(label) and 5 <= float(label) <= 500):
            return []
        grams = float(label)
        label = f"{tidy_number(grams)} g"
    elif grams < 10 and _COMMA_THOUSANDS.fullmatch(label):
        grams *= 1000
    if kcal * grams / 100 > 1000:
        return []
    if grams == 100 and _PLAIN_100.fullmatch(label):
        return []
    if grams <= 1 and _NUMBER_AND_UNIT.fullmatch(label):
        return []
    return [[label[:60], tidy_number(round(grams, 2))]]


# --- labels typed per serving ---
# Many Philippine rows hold the label's PER SERVING column in the per-100 g fields: the
# calories agree with the macros, so nothing above catches them, but they are 2 to 6 times
# too low ("fish crackers 117 kcal", "bread 141") or, for a bottle, too high ("cola 72 per
# 100 ml"). Such a row is recognised by its kind of food, read from words in its name and
# categories, and the range real labels of that kind fall in (SERVING_KINDS). The ranges
# were measured on the Philippine rows of the real export (per 100 g; per 100 ml for drinks):
# a dry snack, cookie or cracker is 340 to 620 kcal, bread 200 to 500, a soft drink 15 to 60.
# A row is suspect when its calories are below the kind's floor (above its ceiling for a
# drink); a real low-calorie product of a kind that is not listed (soup, pickles, fresh
# noodles, a coffee drink, a diet soda) is never suspect.
#
# A suspect row must also look like a label column copied whole: its calories agree with
# its own protein, carbs and fat (to 20 % or 10 kcal). What is then done (serving_typed_fix):
#   1. the serving's weight is looked for: serving_quantity (or a weight in serving_size),
#      then a weight written in the name ("cm cookies 33g", "c2 lemon 455ml"), then the
#      pack's "quantity". The first weight (5 to 2000 g, not 90 to 110) that puts the
#      calories back in the plausible range, with no more than 100 g of anything in 100 g,
#      is taken: the numbers are re-based to per 100 g, the row is marked an estimate, and
#      that serving becomes the default portion (pinned first like a curated
#      "default_portion": "serving"). It may be more than 100 g (a 500 ml bottle).
#   2. when no stated weight works and the calories are far enough off (the kind's "sure"
#      figure; noodles and drink powders have none, as their low figures may honestly be
#      "as prepared"):
#      a. a dry food (crackers, chips, chocolate, candy, nuts, nut spread, cereal, oil) is nearly
#         all protein, carbs and fat, so the serving the numbers describe weighs about
#         their sum (divided by the kind's "solids" share, 0.90 to 1). The row is re-based
#         on that weight and the serving is called "1 serving (about 29 g)".
#      b. any other kind (bread, cheese, mayonnaise, a drink) is stored per serving (at
#         least 15 kcal), labelled with its serving text when that text names no weight,
#         else "1 serving (as on the pack)".
#   3. otherwise the row is left as it is: there is no telling.
# Whatever is done, the default portion shows the calories the label gives for a serving.
#
# One more case. Open Food Facts lets a contributor type the label "per serving" and works
# the per-100 g figures out from the serving size typed next to it. When that size was the
# whole pack ("90g" for a 22 g serving of fish crackers), the file holds 117 kcal, 1.11 g,
# 17.8 g, 4.44 g: not label figures, but they become the label's own (105 kcal, 1, 16, 4 g)
# when taken back to 90 g. Such a row (_typed_for_serving) is put back to its label figures
# first, its stated serving is known to be wrong and is not tried, and step 2 applies without
# the "sure" test for a dry food. Drinks are not read this way.
# Separately, a dry food (any kind except drinks; ice cream under 5 kcal) showing under
# 10 kcal is dropped, and so is one above 1.5 times the top of its range. Drinks are never
# dropped for 0 kcal: water, diet and zero drinks are real.
#
# Ice cream has no range of its own, but Philippine tubs print their numbers per 100 ml,
# and 100 ml of ice cream weighs about 60 g (it is half air): "129 kcal" is then shown for
# 100 g when 100 g holds about 215. A row with 80 to 160 kcal, at least 2 g of fat (not a
# water ice) and a "100 ml" serving, or no serving at all and "ice cream" in its name, is
# re-based on that weight with "100 ml (about 60 g)" first (_ice_cream_by_volume). Bars,
# cones, sticks and anything light or with a serving in grams are left as they are.
# import_off applies all of this to Philippine rows only (tagged PH or a 480 barcode).

_LIQUID_UNIT = re.compile(r"\d\s*(?:ml|cl|l|ltrs?|liters?|litres?|fl\.?\s*oz)\b", re.I)
# Words that make a food wet, frozen, a meal, a seasoning or a drink: such a row is never one of the dry kinds.
_WET = re.compile(
    r"ice cream|gelato|sorbet|sundae|popsicle|frozen|\bdrinks?\b|drinking|juice|beverage|shake|smoothie|latte"
    r"|coffee|\btea\b|yogh?urt|soup|sauce|syrup|cornetto|\bcones?\b|ready.?to.?(?:eat|drink)|\bmeals?\b|\bdip\b"
    r"|\bmilk\b(?! ?(?:chocolate|choco|biscuits?|cookies?|crackers?|candy|candies|tablets?|bars?|bread|buns?))"
    r"|chicken|beef|bacon|\bham\b|sau[sd]ages?|hot ?dogs?|longganisa|tocino|tuna|sardines?|corned|\bmeat|turkey"
    r"|pepperoni|salami|mortadella|prosciutto|salmon|\bcod\b|breast|fillets?|nuggets?|embutido|siomai|dumplings?"
    r"|pierogi|krainer|bratwurst|franks?|frankfurters?|fries|\bmash|salad|sandwich(?:es)?\b(?! ?(?:cookies?"
    r"|biscuits?|crackers?|wafers?|spread|chocolate))|\bsubs?\b|pizza|burrito|pickles?|seasoning|spice|\bblend\b"
    r"|vinegar|bread (?:&|and) butter|\bbrats?\b|steak|grits|scramble|casserole|\bbakes\b|pudding|mousse|jell-?o"
    r"|\bmole\b|green beans|happy apples|apples? (?:&|and)", re.I)
_DRINK_PLACE = re.compile(r"beverages|drinks|sodas|\bteas\b|juices|nectars", re.I)    # categories of drinks
_DRINK_NOT = (
    r"milk|latte|coffee|cappuccino|mocha|macchiato|yogh?urt|yakult|probiotic|cultured|\bsoy|\boat|almond|choco"
    r"|cocoa|malt|collagen|protein|powder|\bmix\b|concentrate|cordial|syrup|crackers?|biscuits?|cand(?:y|ies)"
    r"|gumm|jelly(?! drink)|ice cream|cake|cookies?|oreo|zero|diet|light|lite|no sugar|sugar.?free|unsweetened"
    r"|\bwater\b|vinegar|sauce|seasoning|tuna|instant")
_ZERO_CUE = re.compile(r"sugar.?free|no sugar|zero|diet|\blite?\b|light", re.I)


class _Kind:
    """One kind of food: the range its real labels fall in and what happens outside it.

    words: matched in the name (and, with by_brand, in "name | brand"); places: matched in
    categories_en, only when no kind's words matched the name; skip: words in the name or
    the serving text that rule the kind out. low, high: the plausible range per 100 g (per
    100 ml for drinks); a kind without one (ice cream) is only checked for 0 kcal. flag:
    suspect below this (above it for drinks). sure: below this a row is stored per serving
    even when no weight explains it (at or above it for drinks); None = never, a weight is
    always needed. dry: a wet word or a liquid amount rules the kind out. max_carbs: per
    100 g, after re-basing. solids: the share of the food's weight that is protein, carbs
    and fat, for the kinds dry enough that a serving's weight can be told from them.
    """
    __slots__ = ("name", "words", "places", "skip", "low", "high", "flag", "sure", "drink", "dry", "by_brand",
                 "max_carbs", "solids")

    def __init__(self, name, words, places, skip, low, high, flag, sure, drink=False, dry=None, by_brand=False,
                 max_carbs=None, solids=None):
        self.solids = solids
        self.name, self.low, self.high, self.flag, self.sure = name, low, high, flag, sure
        self.words = re.compile(words, re.I)
        self.places = re.compile(places, re.I) if places else None
        self.skip = re.compile(skip, re.I) if skip else None
        self.drink, self.by_brand, self.max_carbs = drink, by_brand, max_carbs
        self.dry = (not drink) if dry is None else dry              # a drink is never dry


# In the order they are tried; the first kind that matches decides (so "peanut butter
# cookies" are cookies, not peanut butter, and "cheese peanuts" are nuts).
SERVING_KINDS = (
    _Kind("ice cream", r"ice cream|gelato|helado|sorbet|sundae|popsicle|ice pops?|ice bars?|ice lolly|ice lollies"
                       r"|ice candy|frozen dessert|frozen yogh?urt|cornetto|\b(?:selecta|big scoop|creamline|aice"
                       r"|carmen'?’?s best|arce dairy|dan eric'?’?s|btic|melona|binggrae|magnum|halo top|yasso|tofutti"
                       r"|dipping dots|jude'?’?s|ben & jerry'?’?s|h[aä]agen|breyers|talenti|goodpop)\b",
          r"ice creams|frozen desserts|ice pops|sorbets", r"hydration|energy|performance|\bdrink|\bprime\b|\bc4\b",
          None, None, None, None, dry=False, by_brand=True),
    _Kind("soda", r"\b(?:colas?|coke|coca.?cola|sodas?|soft drinks?|sprite|royal|tru.?orange|7.?up|seven up"
                  r"|mountain dew|pepsi|mirinda|sarsi|root ?beer|ginger ale|fanta|carbonated)\b",
          r"sodas|soft drinks|carbonated drinks|colas", _DRINK_NOT, 15, 60, 60, 60, drink=True),
    _Kind("tea", r"\b(?:iced tea|ice tea|green tea|lemon tea|c2|nestea|tea drink|black tea|oolong|jasmine tea"
                 r"|honey tea)\b", r"iced teas|tea-based beverages|ready-to-drink teas", _DRINK_NOT,
          10, 55, 55, 60, drink=True),
    _Kind("juice", r"\b(?:juices?|nectar|fruit drink|lemonade|dalandan|energy drinks?|sports drinks?|isotonic"
                   r"|gatorade|pocari|zesto|minute maid)\b",
          r"juices|nectars|fruit-based beverages|energy drinks|sports drinks",
          _DRINK_NOT + r"|\bin\b|slices|halves|drained|canned|cocktail|tidbits|chunks|segments", 15, 75, 75, 85,
          drink=True),
    # Milk, chocolate milk and coffee drinks in a bottle, can or carton (a liquid amount is required):
    # 100 ml of any of them stays under 95 kcal; evaporated, condensed and coconut milk and cream do not.
    _Kind("milk drink", r"\b(?:milk|choco(?:late)? drink|coffee drink|latte|cappuccino|mocha|macchiato|chuckie"
                        r"|nutri ?boost|milk tea)\b", None,
          r"evaporad[ao]|evaporated|condensed|condensada|kremdensada|\bcream\b(?! ?milk)|creamer|coconut|\bgata\b"
          r"|powder|ensure|pediasure|supplement|formula|shake|smoothie|yogh?urt|yakult|kefir|butter|concentrate"
          r"|syrup|ice cream|liqueur|\bmix\b|instant|3 ?in ?1|sachet|protein|collagen|whey|zero|diet|no sugar"
          r"|sugar.?free", 25, 95, 100, None, drink=True),
    _Kind("drink powder", r"\b(?:3 ?in ?1|3-in-1|2 ?in ?1|coffee mix|creamer|coffee.?mate|milk powder"
                          r"|powdered milk|powdered (?:chocolate|choco|juice|drink|milk drink)|milo|ovaltine"
                          r"|energen|tang|juice powder|choco(?:late)? drink mix|chocolate malt|malt drink)\b",
          r"powdered milk|dried milks|milk powders|creamers|powdered (?:chocolate|juice|drink)",
          r"ready.?to.?drink|\brtd\b|bottle|\bcan\b|\buht\b|sterili[sz]ed|liquid|\bblack\b|\bpure\b|classic|baking"
          r"|seasoning|protein|whey|collagen|creatine|pre.?workout|bcaa|cocoa powder|tablea|ice cream|electrolyte"
          r"|hydration|sugar.?free|zero|diet|light|lite|\btea\b|lemonade", 330, 580, 250, None, dry=False),
    _Kind("bakery", r"\b(?:bread|breads|pandesal|pan de sal|ensaymada|ensaimada|monay|muffins?|cupcakes?|brownies?"
                    r"|donuts?|doughnuts?|croissants?|eclairs?|brioche|mamon|pastry|pastries|buns?|bagels?|toast"
                    r"|crinkles|cakes?|swiss roll|chiffon|pianono|brazo|hopia|hopiang|neu ?bake|gardenia)\b",
          r"\b(?:breads|muffins|pastries|brioches)\b|(?<!biscuits and )\bcakes\b",
          r"crumbs|breading|\bmix\b|flour|rice cakes?|fish cakes?|crab cakes?|yam cakes?(?! bar)|cheesecake"
          r"|\bloaf\b(?! bread)|pancakes?|mochi|tikoy|kakanin|puto|bibingka|kutsinta|suman|sapin|cassava|halaya"
          r"|leche|flan|cream puff|custard|fruit ?cake|stuffing|tortillas?|spread|wraps?\b|egg white|dipper"
          r"|panera|einstein|seltzer|roaster|blend", 200, 500, 200, 150),
    _Kind("crackers", r"\b(?:crackers?|biscuits?|cookies?|wafers?|pretzels?|biscocho|galletas?|otap|sky ?flakes"
                      r"|polvoron|rosquillos|barquillos|uraro|broas|piaya)\b",
          r"\b(?:biscuits|crackers|cookies|wafers)\b(?! and cakes)",
          r"cookie dough|egg ?roll wrappers?|spring roll|soft|flapjack|buttermilk|homestyle|home style|southern"
          r"|flaky|grands|dough|waffles?|layers|jumbo|cheesecake", 340, 600, 300, 230, solids=0.95),
    _Kind("chips", r"\b(?:chips|crisps|curls|chicharr?ons?|cornicks?|corniks|nachos|puffs|popcorn|cracklets"
                   r"|cracklin|crackling|kropek|kropeck|taco shells?|cheese rings|potato sticks|corn (?:snacks?|bits)"
                   r"|pork rinds?|chippy|piattos|tortillos|tostillas)\b", r"\b(?:chips|crisps|popcorn|puffed)\b",
          r"fruit|mango|banana|apple|jackfruit|dried|kale|pickles?|shrimp(?! (?:crackers?|chips))|chocolate chips?"
          r"|choco chips?|baking chips|chips delight|\boven\b|triple cook|thick cut|chunky|steak|crinkle|curly"
          r"|hash|shredded|potato curls|cream puffs?|pancake|cereal|cauliflower|\bsalt\b|wetherspoon|side plate",
          350, 620, 350, 230, solids=0.95),
    _Kind("nut spread", r"\b(?:peanut butter|nut butter|almond butter|cashew butter|hazelnut spread"
                        r"|chocolate spread|choco spread|nutella|choco hazelnut|cocoa spread)\b", None,
          r"cookies?|crackers?|biscuits?|wafers?|\bbars?\b|cups?|sandwich|cand(?:y|ies)|powder|protein|whey"
          r"|filled|flavou?r|cereal|oat|chips|pretzels?|light|reduced", 450, 700, 450, 250, solids=0.95),
    _Kind("mayonnaise", r"\b(?:mayonnaise|mayo|sandwh?ich spread)\b", None,
          r"light|lite|low.?fat|reduced|fat.?free|onigiri|potato|vegan|half", 250, 760, 250, 150),
    _Kind("oil", r"\b(?:cooking|vegetable|coconut|olive|canola|palm|corn|soybean|soya|sesame|sunflower|rice bran"
                 r"|avocado|peanut|grapeseed|safflower)\s+oil\b|\bolein\b", None,
          r"\bin\b|spray|fish|flakes|chunks|capsules?|softgels?|chil[il]|garlic|infused|chips|loaf|bread|ciabatta"
          r"|focaccia|crackers?|dressing|mayo", 700, 950, 700, 400, solids=1.0),
    _Kind("nuts", r"\b(?:peanuts?|cashews?|almonds?|pistachios?|macadamias?|walnuts?|hazelnuts?|pecans?|pili"
                  r"|kasoy|mixed nuts|nuts|sunflower seeds|pumpkin seeds|melon seeds|watermelon seeds"
                  r"|butong pakwan|kuaci)\b", r"salted nuts|roasted nuts|mixed nuts|\bpeanuts\b|cashews|almonds"
          r"|pistachios", r"butter|spread|\boil\b|flour|\bbars?\b|cookies?|coconut|paste|cream|unsweetened|vanilla"
          r"|beverage|alpro|so nice|\bsilk\b|foam|\bdates?\b|cupcakes?|cakes?|brioche|rolls?|baklava"
          r"|carrots?|broccoli", 430, 720, 400, 260, solids=0.93),
    _Kind("butter", r"\b(?:margarine|ghee|shortening|lard|butter)\b", None,
          r"peanut|\bnut\b|almond|cashew|cocoa|shea|cakes?|bread|buns?|rolls?|mochi|popcorn|\bcorn\b|garlic|rice"
          r"|flavou?r|scotch|cream|cand(?:y|ies)|toffee|caramel|honey|\bbars?\b|cups?|shrimp|crab|prawn|squid"
          r"|cinnamon|sugar|beans?|squash|lemon|tarts?|\bpie\b|coconut(?! butter)|\bube\b|\byam\b|cheese"
          r"|oats?|granola|fried|fish|spread|apple|fruit|pumpkin|pear|plum|chick|light|lite|reduced|potato"
          r"|carrot|r[oœ]sti|\bau lard|cherry|berry|peach|apricot|vanilla|emuls|extract|olive", 500, 900, 500, 200),
    _Kind("cheese", r"\b(?:cheese|cheddar|mozzarella|parmesan|gouda|edam|queso|quezo|cheez|cheezee|kesong)\b", None,
          r"cream cheese|cottage|ricotta|feta|light|lite|low.?fat|reduced|vegan|plant|flavou?re?d?|curls|rings"
          r"|balls|sticks|cake|ensaymada|dressing|mayo|lasagna|macaroni|\bmac\b|burger|\bpie\b|tart|pastillas"
          r"|tomato|spaghetti|pasta|stuffed|bites|snack|\bcorn\b|powder|\bbars?\b|yema|\boil\b|butter|popped"
          r"|cheesies|crisps|chocolate|gateau|seasoned|potato|onion|nacho|queso dip|no dairy|dairy.?free|broccoli"
          r"|pistachio", 230, 480, 230, 100, max_carbs=15),
    _Kind("cereal", r"\b(?:cereals?|granola|corn ?flakes|muesli|koko krunch|oats|rolled oats|oatmeal|cheerios"
                    r"|froot loops|honey stars)\b", r"breakfast cereals|cereal flakes|granolas|mueslis|rolled oats",
          r"\bbars?\b|\bjars?\b|bites|porridge|cups?\b|prepared|cooked|with milk|ready", 330, 520, 300, 200,
          solids=0.93),
    _Kind("noodles", r"\b(?:instant noodles?|noodles?|pancit canton|ramen|ramyun|ramyeon|ramyon|mami|bihon"
                     r"|sotanghon|canton|spaghetti|pasta|macaroni|vermicelli|misua|miswa|udon|soba|lomi|miki"
                     r"|odong|chow mein|yakisoba|mi goreng|fettuccine|penne|linguine|lm pc|lm)\b",
          r"instant noodles|pastas|noodles",
          r"fresh|cooked|ready|tteokbokki|stir.?fry mix|strips|\bkit\b|baked|carbonara|salad|meatballs|chilled"
          r"|soup base|prepared|bowl|konjac|shirataki|keto|zero|slim|miracle|kelp|palmini|palm|zucchini|veggie"
          r"|vegetable|squash|lentil|chickpea|bean|edamame|soy", 330, 520, 300, None),
    _Kind("chocolate", r"\b(?:chocolates?|choco bars?|pralines?|bonbons?|truffles)\b",
          r"\bchocolates\b|chocolate candies",
          r"sugar.?free|no sugar|zero|creatine|vitamins?|supplement|collagen|protein|cakes?|mochi|pudding|spread"
          r"|cereal|flan|gateau|g[aâ]teau|mousse|\bpots?\b|dessert|custard|matcha|bomb pop|\bpops?\b|non.?dairy"
          r"|keto|fresh milk|jell-?o", 420, 620, 300, 250, solids=0.97),
    _Kind("candy", r"\b(?:candy|candies|lollipops?|lollies|gumm(?:y|ies)|jelly beans|marshmallows?|mallows"
                   r"|tic ?tac|toffees?|pastillas|yema|nougat|caramels)\b",
          r"confectioner|candies|gummies|marshmallows",
          r"sugar.?free|no sugar|zero|\bgum\b|creatine|vitamins?|supplement|collagen|protein|cakes?|mochi|pudding"
          r"|jelly cups?|gulaman|gelatin|spread|cereal|grapes?|matcha|\btea\b|vinegar|elderberry|melatonin|biotin"
          r"|probiotic|fib(?:er|re)|cotton candy|xylitol|flan|custard|rice|\briz\b|g[aâ]teau|dessert|powder"
          r"|\bc gummies", 280, 450, 280, 180, solids=0.90),
)
# "Cookies and Cream" names a flavour (of an ice cream, a chocolate bar, a drink), not a cookie.
_FLAVOUR_NAME = re.compile(r"\bcookies?\s*(?:and|&|n|['’‘]?n['’]?)\s*cream\b", re.I)


def serving_kind(name, brand, categories, serving_size, quantity):
    """The _Kind a row belongs to, or None.

    The name's words are tried first, then (only when no kind's words match the name) the
    categories. Drinks also need a liquid amount or a drink category; the dry kinds never
    take a row with a wet word or a liquid amount; drink powders take no liquid amount.
    """
    name, categories = _FLAVOUR_NAME.sub(" ", name or ""), categories or ""
    liquid = bool(_LIQUID_UNIT.search(serving_size or "") or _LIQUID_UNIT.search(quantity or "")
                  or _LIQUID_UNIT.search(name))
    wet = liquid or bool(_WET.search(name))
    own_words = name + " | " + (serving_size or "")

    def fits(kind):
        if kind.skip is not None and kind.skip.search(own_words):
            return False
        if kind.drink and not (liquid or (_DRINK_PLACE.search(categories) and kind.name != "milk drink")):
            return False
        return True

    branded = name + " | " + (brand or "")
    found = ([kind for kind in SERVING_KINDS if kind.words.search(branded if kind.by_brand else name)]
             or [kind for kind in SERVING_KINDS if kind.places is not None and kind.places.search(categories)])
    for kind in found:
        if not fits(kind):
            continue
        if (kind.dry and wet) or (kind.name == "drink powder" and liquid):
            return None
        return kind
    return None


_WEIGHT_IN_TEXT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|g|gm|gms|grams?|ml|cl|l|ltrs?|liters?|litres?)\b", re.I)
_UNIT_FACTOR = {"kg": 1000, "cl": 10, "l": 1000, "ltr": 1000, "ltrs": 1000, "liter": 1000, "liters": 1000,
                "litre": 1000, "litres": 1000}


def text_weight(text):
    """The first weight or volume written in a text, in g or ml: "cm cookies 33g" -> 33,
    "Zesto 1.5L" -> 1500, "4 x 110 ml" -> 110. None when there is none."""
    found = _WEIGHT_IN_TEXT.search(text or "")
    if not found:
        return None
    unit = found.group(2).lower()
    return float(found.group(1).replace(",", ".")) * _UNIT_FACTOR.get(unit, 1)


def _typed_for_serving(row, serving_g):
    """True when the per-100 g figures were worked out from numbers typed for one serving.

    The file's figures are then not label figures (117 kcal, 1.11 g, 17.8 g, 4.44 g with a
    "90g" serving) but become ones when taken back to that serving (105 kcal, 1, 16, 4 g):
    whole calories and protein, carbs and fat in half grams, allowing for the three
    significant digits the file keeps. At least one of the four figures must be off the
    label grid as it stands, so that a plain round row is never read this way.
    """
    if serving_g is None:
        return False
    odd = False
    for column, step in (("calories_kcal", 1.0), ("protein_g", 0.5), ("carbs_g", 0.5), ("fat_g", 0.5)):
        value = row[column]
        if value is None:
            continue
        if abs(value / step - round(value / step)) * step > 0.02:
            odd = True
        back = value * serving_g / 100.0
        if abs(back / step - round(back / step)) * step > max(0.03, 0.006 * back):
            return False
    return odd


SERVING_SODIUM_MAX = 2500       # mg per 100 g after re-basing; more is a typing slip, and the figure is dropped


def _rebased(figures, factor, kind):
    """The figures (calories and grams) times factor, or None when they would be impossible for 100 g."""
    scaled = {column: figures[column] * factor for column in ("calories_kcal",) + GRAM_COLUMNS
              if figures[column] is not None}
    if not kind.low <= scaled["calories_kcal"] <= kind.high:
        return None
    if any(scaled.get(column, 0) > 100 for column in GRAM_COLUMNS):
        return None
    if scaled.get("protein_g", 0) + scaled.get("carbs_g", 0) + scaled.get("fat_g", 0) > 105:
        return None
    if kind.max_carbs is not None and scaled.get("carbs_g", 0) > kind.max_carbs:
        return None
    return scaled


ICE_CREAM_G_PER_ML = 0.6        # a tub of ordinary ice cream: 100 ml weighs about 60 g
_ICE_CREAM_NAME = re.compile(r"ice ?cream|gelato|sundae|sorbetes", re.I)
_NOT_TUB_ICE_CREAM = re.compile(
    r"sugar.?free|no sugar|zero|diet|\blite?\b|light|low.?fat|reduced|yogh?urt|\bmilk\b(?! ?choco)|latte|shake|soda"
    r"|sorbet\b|pops?\b|popsicle|loll(?:y|ies)|ice (?:candy|bars?|sticks?|drops?)|sandwich|\bcones?\b|\bbars?\b"
    r"|sticks?\b|powder|\bmix\b|syrup|topping", re.I)


def _ice_cream_by_volume(row, name, label_text, serving_g):
    """Re-base a tub ice cream whose numbers are per 100 ml on the weight of 100 ml. True when done."""
    kcal = row["calories_kcal"]
    if not 80 <= kcal < 160 or (row["fat_g"] or 0) < 2 or _NOT_TUB_ICE_CREAM.search(name):
        return False
    from_macros = 4 * (row["protein_g"] or 0) + 4 * (row["carbs_g"] or 0) + 9 * (row["fat_g"] or 0)
    if abs(kcal - from_macros) > max(0.20 * max(kcal, from_macros), 10):
        return False
    if label_text:
        if not (_LIQUID_UNIT.search(label_text) and serving_g is not None and 90 <= serving_g <= 110):
            return False                                        # a serving in grams, a bar, a cup, a whole tub
    elif not _ICE_CREAM_NAME.search(name):
        return False
    for column in ("calories_kcal",) + GRAM_COLUMNS + ("sodium_mg",):
        if row[column] is not None:
            row[column] = row[column] / ICE_CREAM_G_PER_ML
    grams = tidy_number(round(100 * ICE_CREAM_G_PER_ML, 2))
    row["basis"] = "estimate"
    row["portions"] = [[f"100 ml (about {grams} g)", grams, PIN_MARK]]
    return True


def serving_typed_fix(row, name, brand, categories, serving_size, serving_g, quantity):
    """Find and repair a row whose label was typed per serving (see the notes above).

    row: the cleaned row (its portions already set by off_portions). Returns what was done:
    "" nothing, "rebased" (numbers now per 100 g, the serving pinned first), "per_serving"
    (numbers kept, stored per serving), "drop_zero" or "drop_high" (the row must be thrown away).
    """
    kind = serving_kind(name, brand, categories, serving_size, quantity)
    if kind is None:
        return ""
    kcal = row["calories_kcal"]
    if kind.low is None:                                        # ice cream: 0 kcal, and numbers per 100 ml
        if kcal < 5 and not _ZERO_CUE.search(name):
            return "drop_zero"
        label_text = clean_text(serving_size)
        by_volume = _ice_cream_by_volume(row, name, label_text,
                                         text_weight(label_text) if serving_g is None else serving_g)
        return "rebased" if by_volume else ""
    if kind.drink:
        if kcal <= kind.flag or kcal > 250:                     # above 250 it is a syrup or powder, not a bottle
            return ""
    else:
        if kcal < 10:                                           # no dry snack, bread or powder is that light
            return "drop_zero"
        if kcal > 1.5 * kind.high:
            return "drop_high"
        if kcal >= kind.flag:
            return ""
    # The pattern looked for is a label column copied whole: calories that agree with the
    # row's own protein, carbs and fat (to 20 %, or 10 kcal). Otherwise the calories figure
    # itself may be the wrong one, and re-basing would scale the error.
    from_macros = 4 * (row["protein_g"] or 0) + 4 * (row["carbs_g"] or 0) + 9 * (row["fat_g"] or 0)
    if abs(kcal - from_macros) > max(0.20 * max(kcal, from_macros), 10):
        return ""

    label_text = clean_text(serving_size)
    if serving_g is None:
        serving_g = text_weight(label_text)                     # "1 pack (25 g)" without serving_quantity
    # The label's own figures for one serving: the row's numbers as they stand, or, when
    # they were worked out from a serving typed with the wrong weight, taken back to it.
    columns = ("calories_kcal",) + GRAM_COLUMNS + ("sodium_mg",)
    figures = {column: row[column] for column in columns}
    derived = not kind.drink and _typed_for_serving(row, serving_g)
    if derived:
        figures = {column: None if value is None else value * serving_g / 100.0 for column, value in figures.items()}

    def rebase(grams, label, others=()):
        """Store the figures per 100 g with this serving first. False when grams cannot be their weight."""
        scaled = _rebased(figures, 100.0 / grams, kind)
        if scaled is None:
            return False
        row.update(scaled)
        sodium = figures["sodium_mg"]
        row["sodium_mg"] = None if sodium is None or sodium * 100.0 / grams > SERVING_SODIUM_MAX \
            else sodium * 100.0 / grams
        row["basis"] = "estimate"
        amount = tidy_number(round(grams, 2))
        row["portions"] = [[label[:60], amount, PIN_MARK]] + [p for p in others if p[1] != amount]
        return True

    # 1. A stated weight that explains the numbers: the serving, the name, the pack size.
    weights = [] if derived or serving_g is None else [("serving", serving_g)]
    for source, text in (("name", name), ("pack", quantity)):
        grams = text_weight(text)
        if grams is not None:
            weights.append((source, grams))
    tried = derived
    for source, grams in weights:
        if not 5 <= grams <= 2000 or 90 <= grams <= 110:        # a weight near 100 g changes nothing
            continue
        tried = True
        amount = tidy_number(round(grams, 2))
        unit = "ml" if kind.drink else "g"
        if source == "serving" and label_text and re.search(r"\d", label_text):
            label = label_text if re.search(r"[A-Za-z]", label_text) else f"{amount} {unit}"
        elif source == "serving":
            label = f"{amount} {unit}"
        else:
            label = f"1 {'bottle' if kind.drink else 'pack'} ({amount} {unit})"
        others = [] if derived else off_portions(serving_size, serving_g, row["calories_kcal"] * 100.0 / grams)
        if rebase(grams, label, others):
            return "rebased"

    # 2. No stated weight explains them. Only when they are far enough off to be sure
    #    (for numbers known to be typed for a serving of a dry food it is enough that the
    #    serving typed with them is at least half again as heavy as they allow).
    plain_label = label_text if label_text and not tried and not _NUMBER_AND_UNIT.search(label_text) \
        and re.search(r"[A-Za-z]", label_text) else ""
    far_off = kind.sure is not None and (kcal >= kind.sure if kind.drink else kcal < kind.sure)
    if kind.solids is not None and (far_off or derived):
        # a. A dry food: the serving weighs about its protein, carbs and fat together.
        macros = (figures["protein_g"] or 0) + (figures["carbs_g"] or 0) + (figures["fat_g"] or 0)
        grams = math.floor(macros / kind.solids + 0.5)
        if (far_off or grams * 1.5 <= serving_g) and 5 <= grams <= 2000 \
                and rebase(grams, f"{plain_label or '1 serving'} (about {grams} g)"):
            return "rebased"
    if not far_off or figures["calories_kcal"] < 15:            # under 15 kcal: not even one serving of anything
        return ""
    # b. Per serving, as the label has it.
    row.update({column: value for column, value in figures.items() if value is not None})
    row["per_unit"] = "serving"
    row["basis"] = "estimate"
    row["portions"] = [[(plain_label or "1 serving (as on the pack)")[:60], None]]
    return "per_serving"


# --- doubtful rows outside the Philippines (kept, but not chosen for the app) ---
# A row from another market that shows one of the patterns below is stored with a short
# reason in the "doubt" column. pick_off_rows never takes such a row into a market pool;
# the pools have far more candidates than places (about 679 000 US rows for 11 000), so the
# next row in rank order takes the place. The numbers are not changed. Philippine rows are
# never marked: serving_typed_fix repairs those, and every one of them is exported.
#   per_serving  a US label's per-serving column typed into the per-100 g boxes. A US label
#                converted properly to 100 g gives uneven figures (a 68 g Clif bar of 260 kcal
#                is 382 per 100 g); here the per-100 g figures are themselves round label
#                figures (kcal a multiple of 5, protein, carbs and fat in half grams) while
#                the stated serving's share of them is not (260 x 68 / 100 = 176.8). The
#                figures re-based on the serving must be possible (at most 900 kcal and 100 g
#                of protein, carbs and fat together). Only rows tagged US and not UK, whose
#                barcode is not British or Asian: European labels are round per 100 g.
#   per_bottle   the same mistake on a drink whose serving (240, 360, 500 ml) makes the
#                serving's share round too: a liquid serving of 150 ml or more and more
#                calories per 100 ml than any plain drink has (100; 130 for milk drinks;
#                165 for shakes and meal drinks), and no word of a dense liquid in the name.
#                A drink with more than 12 g of protein per 100 ml is one too (a 325 ml
#                shake with 30 g of protein has 9): its figures are a bottle's.
#   juice_high   juice or soda in a drinks category at 100 to 300 kcal per 100 g: a bottle's
#                figure, with no serving that would tell which bottle.
#   dry_coffee   ground or instant coffee carrying the figures of the dry powder (397 kcal,
#                14.4 g protein), which a cup of coffee logged from it would take on. The
#                exact figures of that one reference record (396.6 kcal, 14.4 g protein,
#                40.2 g carbs, 15.4 g fat) count under any name ("French Roast", "Colombian").
#   zero_kcal    a plainly caloric food (bread, cheese, juice, mayonnaise ...) at 0 kcal with
#                0 g of protein, carbs and fat.
#   thin_oil     a plain oil (the name ends with "oil" and names no butter, spread or fish
#                "in" or "with" it; no protein or carbs) with 1 to 75 g of fat per 100 g: a
#                spoonful's figures (olive oil at 120 kcal).
#   macro_mismatch  calories more than 25 % (and more than 15 kcal, and more than rounding a
#                small serving's label can explain) away from what the row's own protein,
#                carbs and fat give at 4, 4 and 9 kcal per gram, fibre counted as nothing on
#                the low side and as 2 kcal on the high side. One of the numbers is wrong and
#                nothing says which: a cereal carrying its with-milk calories, popcorn at
#                380 kcal next to 36 g of fat. Not asked on the low side of sugar-free and
#                high-fibre products, nor of anything that is nearly all sweetener or water;
#                not asked on the high side of alcoholic drinks. (calorie_conflict already
#                throws away rows that are 30 to 40 % out; this keeps the nearer misses out
#                of the app.)
#   not_food     shampoo, soap, pills, capsules and pet food.
#   listed       one of the rows of KNOWN_WRONG below: read by hand, plainly wrong, and showing
#                no pattern a rule could go by.
_GRID_SLACK = 0.01
# Rows of the US pool (and three of step 5) that were read by hand (November 2026) and carry plainly wrong calories
# with nothing in the row to tell a rule so: mostly a label's per-serving figures typed per
# 100 g on a row without a serving size. Barcode -> what the row says per 100 g and why that
# is wrong. A believable row of the same product, when there is one, takes the place.
KNOWN_WRONG = {
    "0028400006057": "Doritos Nacho Cheese at 210 kcal (the label gives about 536)",
    "0028400620468": "Doritos Reduced Fat Flamas at 130 kcal (one serving's figures)",
    "0028400712293": "Doritos nacho cheese crackers at 64 kcal",
    "0028400062411": "Doritos Classic mix at 50 kcal",
    "7790310984192": "Doritos Queso 85g at 126 kcal",
    "0028400056977": "Sun Chips French Onion at 140 kcal (one serving's figures)",
    "0028400017503": "Lay's salt and vinegar at 160 kcal (one serving's figures)",
    "0851769007089": "Siete No Salt Tortilla Chips at 130 kcal (one serving's figures)",
    "0021908111254": "Food Should Taste Good Multigrain Tortilla Chips at 292 kcal (about 500)",
    "0009800000753": "Nutella at 445 kcal with 23.5 g of carbs (539 kcal, 57.5 g)",
    "0080000515568": "Jif Creamy at 106 kcal with a can of tuna's figures",
    "0052000241235": "Gatorade Cool Blue at 48 kcal per 100 ml (about 22)",
    "0815154023269": "REIGN Total Body Fuel at 211 kcal per 100 ml (about 2)",
    "0049000555189": "Raspberry Spiced Coke at 250 kcal per 100 ml (about 42)",
    "0049000006209": "Coca-Cola (refresco) at 210 kcal per 100 ml (a bottle's figures)",
    "0611269001846": "Red Bull The Iced Edition at 160 kcal per 100 ml (a can's figures)",
    "0082592720153": "Naked Green Machine at 240 kcal per 100 ml (a bottle's figures)",
    "0025000049484": "Minute Maid Peach Punch at 80 kcal per 100 ml (a glass's figures)",
    "8715700124575": "Heinz Tomato Ketchup at 15 kcal (a spoonful's figures)",
    "0000013414204": "Amora Ketchup at 15 kcal (a spoonful's figures)",
    "0812049006604": "naturipe Blueberries at 123 kcal (about 57)",
    "0740075001648": "North Bay Strawberries at 57 kcal (about 32)",
    "0715756200023": "Driscoll's Strawberries at 45 kcal (a cup's figures)",
    "0015000045203": "Gerber puffs at 0 kcal (about 370)",
    "0810003512680": "Once Upon A Farm puffs at 0 kcal",
    "0858023005270": "Sun Valley Raisins at 110 kcal (about 300)",
    "0036200221631": "Bertolli Alfredo sauce at 110 kcal (a quarter cup's figures)",
    "0194346000586": "Sourdough bread at 110 kcal (a slice's figures)",
    "0041331090452": "Goya soft taco tortillas at 120 kcal (one tortilla's figures)",
    "0044000046026": "belVita Sandwich at 230 kcal (a pack's figures)",
    "0026200461724": "David sunflower seeds at 190 kcal (one serving's figures)",
    "0052200034729": "Beech-Nut Oatmeal Cereal at 60 kcal (one serving of the dry cereal)",
    "0857065007020": "Parmalat 2% milk at 120 kcal per 100 ml (a cup's figures)",
    "0041415018631": "Publix reduced fat milk at 23 kcal per 100 ml (about 50)",
    "0037600378079": "Hormel corned beef hash at 380 kcal (a cup's figures)",
    "0893869003288": "Sahale glazed cashew mix at 220 kcal (one serving's figures)",
    "0021000615421": "Kraft grated parmesan at 20 kcal (two teaspoons' figures)",
    "0083783110098": "Sierra Nevada Hazy Little Thing IPA at 20 kcal per 100 ml (about 60)",
    "0051000024213": "Swanson beef broth at 20 kcal per 100 ml (about 6)",
    "0000011241742": "Philadelphia whipped cream cheese at 156 kcal (about 230)",
    # read in step 5 (the last one in the US pool, the others in the Japan and Korea pools)
    "0078742300634": "Great Value Organic cane sugar at 542 kcal with 21 g protein and 33 g fat (another food's figures)",
    "4903307672283": "Riken sesame dressing at 5 kcal (about 300), its serving given as 350 ml",
    "8801005995009": "Sempio gochugaru (red pepper powder) at 10 kcal (about 300)",
    # read in the second reading of step 5 (November 2026): no rule can tell these, the name names
    # no kind or the figures belong to another product
    "4903423231029": "Lawson Karaagekun at 45 kcal (one piece's figures; fried chicken is about 240)",
    "4901777205109": "Suntory Gokuri grapefruit at 379 kcal per 100 ml with 16.5 g fat (about 45)",
    "4901777204799": "Suntory Nachan orange at 411 kcal per 100 ml, all sugar (about 45)",
    "4901313204207": "Kameda age ichiban at 360 kcal (a fried rice cracker is about 520)",
    "4972195946967": "FamilyMart salt-grilled salmon at 121 kcal with 12.4 g protein (about 200)",
    "4902715697604": "Muradai loin ham at 50 kcal with 7 g protein (about 115)",
    "4901111665026": "AGF Blendy Matcha at 29 kcal (one made-up cup's figures; the powder is about 380)",
    "4549414106343": "Topvalu Instant Miso Soup at 28 kcal (its own macros give about 106)",
    "4901108011713": "Ebito Yakiniku Tare at 29 kcal (a yakiniku sauce is 120 to 200)",
    "4589749924021": "Base Bread plain at 205 kcal (one bun's figures; about 270)",
    "4589786120639": "AND THE FRIET fries at 100 kcal (about 300)",
    "4562452231129": "Verde garlic toast spread at 35 kcal (one 4 g serving's figures)",
    "4903308012811": "Emina cheese dessert with nuts at 47 kcal (one 15 g piece's figures; about 300)",
    "4901360331017": "Bourbon Choco Brownie Rich Milk at 219 kcal (one 44 g pack's figures; about 500)",
    "4902410324867": "Fuji Pan snack sandwich at 120 kcal (bread with filling is about 300)",
    "4902555111407": "Fujiya Milky at 227 kcal (milk candy is about 400)",
    "4571552660845": "Beni Bites dried sweet potato bar at 134 kcal (about 290)",
    "4982895112883": "Wadamata Abekawa mochi rice cake at 100 kcal (one 40 g piece's figures; about 250)",
    "4934635062644": "FamilyMart onsen egg with sauce at 288 kcal (about two eggs' figures on a 25 g serving)",
    "8801392037139": "Bibigo tteokbokki at 322 kcal (half a pack's figures worked out from kJ; about 180)",
    "8996001526378": "Mayora Wow spageti at 300 kcal (the 75 g pack's figures; about 400)",
    "8888030303191": "FairPrice Natural Fruits & Nuts at 146 kcal (a fruit and nut mix is about 450)",
}
_LIQUID_LABEL = re.compile(r"\d\s*(?:ml|cl|l|liters?|litres?)\b|fl\.?\s*oz|\bfloz\b", re.I)
_SHAKE_NAME = re.compile(r"shake|nutrition|ensure|boost|osmolite|protein|formula|meal|huel|soylent|kate farms", re.I)
_DAIRY_NAME = re.compile(r"milk|dairy|kefir|latte|chai|mocha", re.I)
_DENSE_LIQUID = re.compile(
    r"\boils?\b|huile|syrup|creamer|cream\b|sauce|soup|chil[ie]|condensed|evaporated|concentrate|powder|\bmix\b|melts"
    r"|tablets?|cocoa|honey|dressing|whipped|half ?(?:&|and) ?half|eggnog|liqueur|wine|beer|vodka|whiske?y|\brum\b"
    r"|tequila|sake|coconut milk|coconut cream|broth|stock|gravy|batter|yogh?urt|pudding|ice cream|gelato|frozen"
    r"|\bdip\b|hummus|queso|salsa|marinade|\beggs?\b|\bkit\b|lunchmakers?", re.I)
_DRINK_CATEGORY = re.compile(
    r"^(?:beverages|.*\b(?:juices?|nectars?|sodas?|soft drinks|carbonated drinks|lemonades?|iced teas"
    r"|kombuchas?|waters|milks|drinks))$", re.I)
_JUICE_NAME = re.compile(r"juice|\bsoda\b|lemonade|\bcola\b|coke|pepsi|sprite|fanta|ginger ale|root beer|cream soda"
                         r"|iced tea|sweet tea|energy drink|sports drink|gatorade|powerade|punch|nectar|kombucha", re.I)
_NOT_JUICE = re.compile(r"chocolate|candy|chips?|cereal|snack|\bdry\b|dried|powder|toddler|infant|smoothie|\bmilk\b"
                        r"|shake|protein", re.I)
_PIECE_LABEL = re.compile(r"tbsp|tablespoon|tsp|teaspoon|envelope|packet|scoop|stick|pouch|piece|slice|square|bar\b", re.I)
_COFFEE_NAME = re.compile(r"coffee|espresso|caf[eé]", re.I)
_CALORIC_NAME = re.compile(
    r"mayo|strawberr|puffs|pasta|\bmac\b|apple juice|orange juice|ginger ale|cookie|cracker|chips|bread|cereal"
    r"|granola|\brice\b|noodle|cheese|yogh?urt|chocolate|candy|\bnuts?\b|\bbars?\b", re.I)
_ZERO_NAME = re.compile(r"zero|diet|sugar.?free|calorie.?free|light|lite\b|lemon|lime|water|sparkling|seltzer|essenza|pickle"
                        r"|\bdill\b|drink|energy|\bteas?\b|vinegar|walden farms", re.I)     # read in the name and the brand
# Carbs that carry few calories: on such a product fewer calories than the macros give is expected.
_FEW_KCAL_CARBS = re.compile(
    r"sugar.?free|zero sugar|no sugar|keto|sweetener|stevia|erythritol|allulose|xylitol|monk ?fruit|\bgum\b|\bmints?\b"
    r"|fiber|fibre|psyllium|konjac|shirataki|low carb|carb (?:balance|smart|savvy)|sugar alcohol|splenda|truvia|swerve",
    re.I)
_PLAIN_OIL = re.compile(r"\boil\s*$", re.I)
_NOT_PLAIN_OIL = re.compile(r"\b(?:butter|spread|mayo\w*|margarine|dressing|with|in|and)\b|&|\bw/", re.I)
_NOT_FOOD = re.compile(
    r"\b(?:shampoo|conditioner|soap|lotion|toothpaste|multi-?vitamins?|capsules?|softgels?|caplets?|coq10|fish oil"
    r"|infant formula|toddler formula|melatonin|dog food|cat food|dog treats?|cat treats?|hairtamin|hair vitamins?)\b", re.I)


# Neighbouring-market foods whose labels were found round per 100 g when the per_serving test
# was tried on them (a pudding, mochi, sausages, an ice, a rice meal, a medical drink).
_AS_ROUND_PER_100G = re.compile(r"pudding|mochi|glace|sosis|sausage|\brice\b|ข้าว|resource|ice ?bars?|ice ?pops?|popsicle", re.I)
_AS_COCONUT = re.compile(r"coconut (?:milk|cream)(?! (?:biscuit|cookie|candy|candies|powder|wafer|cracker))", re.I)


def _on_grid(value, step, slack=_GRID_SLACK):
    """Is value within slack of a multiple of step (a figure as printed on a label)?"""
    return value is not None and abs(value / step - round(value / step)) * step <= slack


def pool_doubt(row, serving_size, serving_g, categories, alcohol=None):
    """Why a row from outside the Philippines should not be chosen for the app ("" = no reason).

    row: the cleaned row (numbers per 100 g, checked by apply_rules); serving_size and
    serving_g: the source's serving text and weight; categories: categories_en; alcohol:
    grams of alcohol per 100 g when the source states it. See the notes above for each
    reason. The row is not changed.
    """
    name = row["name"] or ""
    if _NOT_FOOD.search(name) and not _COFFEE_NAME.search(name):
        return "not_food"
    if row["barcode"] in KNOWN_WRONG:
        return "listed"
    if asia_market(row["barcode"], row["region_tags"]) and (
            _A_NOT_FOOD.search(name) or (_A_SUPPLEMENT.search(name) and not _A_NOT_SUPPLEMENT.search(name))
            or {item.strip().lower() for item in (categories or "").split(",")} & _A_NOT_FOOD_CATEGORIES):
        return "not_food"                   # infant formula and supplements of the three Asian pools
    if row["per_unit"] != "100g":
        return ""
    kcal, protein, carbs, fat = row["calories_kcal"], row["protein_g"], row["carbs_g"], row["fat_g"]
    label = clean_text(serving_size)
    grams = serving_g
    if grams is None and label:
        grams = float(label) if _BARE_NUMBER.fullmatch(label) else text_weight(label)
    regions = (row["region_tags"] or "").split(",")
    name_and_brand = name + " " + (row["brand"] or "")

    if kcal == 0 and protein == 0 and carbs == 0 and fat == 0 and _CALORIC_NAME.search(name) \
            and (not _ZERO_NAME.search(name_and_brand) or re.search(r"mayo", name, re.I)):
        return "zero_kcal"
    if protein is not None and abs(protein - 14.4) <= 0.2 and (
            (_COFFEE_NAME.search(name) and 380 <= kcal <= 410)
            or (abs(kcal - 396.6) <= 0.5 and abs(protein - 14.4) <= 0.05 and None not in (carbs, fat)
                and abs(carbs - 40.2) <= 0.05 and abs(fat - 15.4) <= 0.05)):
        return "dry_coffee"
    if (_PLAIN_OIL.search(name) and not _NOT_PLAIN_OIL.search(name) and kcal > 0 and None not in (protein, carbs, fat)
            and protein <= 0.5 and carbs <= 0.5 and 1 <= fat <= 75):
        return "thin_oil"

    market = asia_market(row["barcode"], row["region_tags"])
    us_label = ("US" in regions and "UK" not in regions
                and market_origin(row["barcode"], row["region_tags"]) not in ("UK", "JP", "KR", "AS"))
    # Labels from the seven neighbouring markets follow the US style (a per-serving panel), so
    # the two tests below are asked of them too; not of Japan or Korea, where they were wrong
    # one time in three. A drink of 60 kcal or less with a liquid serving, and ice cream, are
    # left alone there (soy milk, pudding and ice cream labels are round per 100 g too).
    # Read again (second reading of step 5): a serving of 80 to 125 g changes the calories by less
    # than a quarter either way, and coconut milk or cream is round per 100 g too.
    as_label = (market == "AS" and not _ICE_CREAM_NAME.search(name) and not _AS_ROUND_PER_100G.search(name)
                and not (kcal <= 60 and _LIQUID_LABEL.search(label))
                and not (grams is not None and 80 <= grams <= 125)
                and not (_AS_COCONUT.search(name) and (fat or 0) >= 10))
    if (kcal > 0 and None not in (protein, carbs, fat) and grams is not None and 5 <= grams <= 2000
            and not 90 <= grams <= 110 and (us_label or as_label)
            and _on_grid(kcal, 5) and all(_on_grid(value, 0.5) for value in (protein, carbs, fat))):
        factor = 100.0 / grams
        possible = kcal * factor <= 900 and (protein + carbs + fat) * factor <= 100
        if possible and not _on_grid(kcal * grams / 100.0, 5, 0.05):
            return "per_serving" if us_label else "per_serving_as"
        limit = 165 if _SHAKE_NAME.search(name) else 130 if _DAIRY_NAME.search(name) else 100
        if possible and grams >= 150 and (kcal > limit or protein > 12) and _LIQUID_LABEL.search(label) \
                and not _DENSE_LIQUID.search(name):
            return "per_bottle" if us_label else "per_bottle_as"

    if market:
        why = asia_doubt(row, label, categories)
        if why:
            return why

    drink_place = any(_DRINK_CATEGORY.match(item.strip()) for item in (categories or "").split(","))
    if (drink_place and _JUICE_NAME.search(name) and 100 < kcal <= 300 and not _NOT_JUICE.search(name)
            and not _DENSE_LIQUID.search(name) and not _PIECE_LABEL.search(label)
            and not (grams is not None and grams < 100)):
        return "juice_high"

    if None not in (protein, carbs, fat):
        fibre = row["fibre_g"] or 0
        floor = 4 * protein + 4 * max(carbs - fibre, 0) + 9 * fat
        ceiling = 4 * protein + 4 * carbs + 9 * fat + 2 * fibre
        rounding = 1400.0 / grams if grams is not None and 0 < grams < 100 else 0     # as in calorie_conflict
        if (kcal < floor - max(0.25 * floor, rounding, 15) and not _FEW_KCAL_CARBS.search(name_and_brand)
                and not (protein < 1 and fat < 1 and (row["sugars_g"] or 0) < 5)):
            return "macro_mismatch"
        if kcal > ceiling + max(0.25 * ceiling, rounding, 15) and not is_alcoholic(row, alcohol, categories or ""):
            return "macro_mismatch"
    return ""


def market_origin(barcode, regions):
    """barcode_origin, read together with the row's tags (regions: region text such as "US,AS").

    A short 8-digit code (stored with five leading zeros) whose digits 6 to 8 read as Japanese
    counts as Japanese only when the row is tagged Japan or only the seven neighbouring markets
    ("AS"). Many American and British packs carry 8-digit codes that start 45 or 49 (a Walmart
    roll 0000045639494, Little Debbie 0000049403954, Sainsbury's 0000049616200); read as
    Japanese they took 45 places of the Japan pool. Such a row gets no origin and goes through
    its tags like any other.
    """
    origin = barcode_origin(barcode)
    if origin == "JP":
        code = normalize_barcode(barcode) or ""
        tags = (regions or "").split(",")
        if code.startswith("00000") and "JP" not in tags and (regions or "") != "AS":
            return None
    return origin


def asia_market(barcode, regions):
    """'JP', 'KR' or 'AS' for a row from the Japanese, Korean or neighbouring-markets shelves, else None.

    That is a barcode registered there (see market_origin), or a row tagged for the seven
    neighbouring markets and for neither the United States, the United Kingdom nor the
    Philippines (Lay's from Thailand carries an American barcode). Philippine rows are never
    asked: they are repaired by serving_typed_fix, not kept out.
    """
    origin = market_origin(barcode, regions)
    if origin in ("JP", "KR", "AS"):
        return origin
    tags = (regions or "").split(",")
    if "AS" in tags and not {"US", "UK", "PH"} & set(tags):
        return "AS"
    return None


# --- Japanese, Korean and neighbouring-market labels typed per pack, piece or serving ---
# Measured on the 83 513 rows of these markets (November 2026): the usual mistake is a whole
# label column for one pack, piece, bottle or serving typed into the per-100 g boxes. Calories
# and macros then still agree, so macro_mismatch cannot see it. Each test below names a kind
# of food whose calories cannot be that low (or, for drinks, that high) per 100 g.
#   snack     crisps, crackers, senbei, wafers, biscuits, cookies, Pocky, Pepero, Pringles ... under
#             300 kcal (they are 380 to 560); not a drink, ice cream, cake, bread, meat dish or
#             mochi, and no serving in ml.
#   seaweed   seasoned laver snacks (gim, nori snack, Tao Kae Noi) under 150 kcal (500 to 600);
#             plain nori sheets (190 to 250) and seaweed salads are left alone.
#   noodles   instant noodles under 130 kcal with a dry-pack serving under 130 g (or none), or
#             under 60 kcal whatever the serving: figures per 100 g of noodles made up with
#             water, while the app offers the dry pack (a 74 g Maggi pack would log 51 kcal).
#   chocolate a name that is nothing but chocolate words and plain modifiers, under 350 kcal.
#   nuts      a name that is nothing but nut words and plain modifiers, under 400 kcal.
#   drink     a soft drink, tea, sports drink or juice at 70 to 170 kcal per 100 ml with little
#             protein and fat (soft drinks are 40 to 50 at most, juices 45 to 60): a bottle's
#             figures. Grape, prune, aronia, mango, durian and nectar juices, energy shots,
#             syrups, powders and amazake really are that rich and are left alone. Also a soy,
#             oat or almond milk or plain milk over 100 kcal, a sports drink over 45, and any
#             sugar water (over 95) or milk coffee or milk tea (over 100) with a serving of
#             150 ml or more.
#   bar       a cereal, protein, granola or nut bar under 280 kcal (they are 330 to 500).
#   nut butter  peanut, almond or cashew butter and hazelnut spread under 250 kcal (580 to 650).
#   candy     candy, gummies and chews under 100 kcal (300 to 400; sugar-free ones 200).
#   baking mix  a muffin, cake, brownie, waffle or cookie mix under 250 kcal (350 to 450).
#   cereal    granola, muesli, flakes and other breakfast cereals under 300 kcal (350 to 480).
#   cheese    parmesan, cheddar, gouda, camembert, mozzarella and processed cheese under 150 kcal.
#   paste     gochujang, doenjang, ssamjang and miso under 100 kcal (170 to 230).
# A Pocky, Pepero, KitKat or Black Thunder counts as a snack whatever else its name says, crisps
# proper (potato chips, Pringles, Lay's) are asked for 400 kcal, not 300, and sheets of nori or
# laver under 100 kcal count as seaweed.
#   mix       3-in-1 coffee, tea or malt drink powder (MILO, Ovaltine, NESCAFE 3 in 1) under 250
#             kcal: one sachet's figures (the powder is 400 to 470).
#   zero      a food named as one that has calories (dumplings, mandu, bread, noodles, rice,
#             cookies, cheese, sausage, a bar ...) at 0 kcal with 0 g of protein, carbs and fat
#             (Kimchi Mandu), unless the name says zero, diet, sugar-free, konjac and the like.
# All but the next three give the reason "dense_low"; drinks give "drink_high", mixes "mix_low",
# zero "zero_food". Some snack, chocolate, nut, bar and nut butter rows are repaired instead (see
# asia_serving_fix).
# These reasons, and per_serving_as and per_bottle_as (the US tests asked of these markets, see
# pool_doubt), stay with their own row when rows merge (OWN_DOUBTS): they were measured on these
# markets only, so a twin from another market never takes them.
_A_NOT_DRY = re.compile(
    r"drink|milk|(?<![a-z])ice(?![a-z])|ice ?cream|gelato|sorbet|yog|shake|latte|(?<![a-z])tea(?![a-z])|coffee|soup"
    r"|sauce|(?<![a-z])dip|pudding|jelly|juice|beverage|smoothie|mousse|spread|oil|paste|tofu|kimchi|dressing|soda"
    r"|water|chicken|fish|shrimp|prawn|squid|meat|pork|beef|pollock|cutlet|corn ?dog|sandwich|monaka"
    r"|vegetable(?! (?:crackers?|chips|crisps|biscuits?))"
    r"|pickle|bamboo|frozen|mochi|cake|pie(?![a-z])|tart|bread|bun(?![a-z])|cream|soft|fresh|wet|kit(?![a-z])(?! ?kat)|meal"
    r"|sweet potato|bagel|burrito|wrap|(?<![a-z])eis(?![a-z])|サンド|ขนมปัง|นม|アイス|ドリンク|飲料|우유|아이스|ไอศกรีม"
    r"|蒲鉾|かまぼこ|氷|パフェ|parfait|(?<![a-z])gà(?![a-z])|popcorn chicken|muffin|pancake|scone|do(?:ugh)?nut"
    r"|croissant|steamed|cooked", re.I)
# Ice-cream makers (a "Macadamia Nut" or a "Cookie Dough" of theirs is a tub of ice cream) and a
# fish-cake maker, read in the name and the brand.
_A_ICE_BRAND = re.compile(r"häagen|haagen|movenpick|mövenpick|wall'?s|magnum|\baice\b|gelatelli|ben ?& ?jerry|cornetto"
                          r"|ice ?cream|蒲鉾|mcflurry|gelato", re.I)
_A_SNACK = re.compile(
    r"(?<![a-z])chips?(?![a-z])|crisps|cracker|senbei|rice snack|pretzel|popcorn|wafer|biscuit|cookie|pocky|pepero"
    r"|jagabee|jagariko|koala|hello panda|potato sticks?|cheetos|doritos|pringles|(?<![a-z])lay'?s(?![a-z])|taro chips|ポテトチップ|せんべい"
    r"|クッキー|ビスケット|クラッカー|スナック|과자|쿠키|비스킷|크래커|餅乾|ข้าวเกรียบ|มันฝรั่งทอด", re.I)
_A_SNACK_CATEGORIES = frozenset((
    "crisps", "potato crisps", "salty snacks made from potato", "crackers (appetizers)", "biscuits", "wafers",
    "chocolate biscuits", "rice crackers", "puffed salty snacks", "corn chips", "tortilla chips", "shortbread cookies",
    "dry biscuits", "cookies"))
_A_SEAWEED = re.compile(r"(?:seaweed|laver|(?<![a-z])nori(?![a-z])).*(?:snack|roast|season|crisp|sheet|tempura|big roll"
                        r"|crunchy|flakes? snack)|(?:snack|roast|season|crisp|sheet|tempura|crunchy).*(?:seaweed|laver"
                        r"|(?<![a-z])nori(?![a-z]))", re.I)
_A_SEAWEED_NATIVE = re.compile(r"(?:^|\s)[가-힣]*김(?:\s|$)|สาหร่าย.*(?:ทอด|อบ|กรอบ|แผ่น|ย่าง)")
_A_SEAWEED_BRAND = re.compile(r"tao ?kae ?noi|taokaenoi", re.I)
_A_NOT_SEAWEED_SNACK = re.compile(
    r"chips?(?![a-z])|fries|crisps|potato|bean|peanut|nut|rice|kimbap|gimbap|dumpling|soup|sauce|ramen|noodle|furikake"
    r"|cracker|salad|chicken|roll|paste|tsukudani|bamboo|佃煮|만두|밥|김치|라면|튀김|ถั่ว|ข้าว", re.I)
_A_NOODLE = re.compile(r"ramen|ramyun|ramyeon|ramyon|noodle|mi goreng|(?<![a-z])mee(?![a-z])|(?<![a-z])mie(?![a-z])"
                       r"|indomie|vermicelli|bihun|(?<![a-z])pho(?![a-z])|라면|ラーメン|麺|麵|มาม่า|บะหมี่", re.I)
_A_INSTANT = re.compile(r"instant|ramen|ramyun|ramyeon|mi goreng|(?<![a-z])mama(?![a-z])|koka|maggi|indomie|vifon|mamee"
                        r"|cup noodle|2 minute|nissin|라면|ラーメン|มาม่า|บะหมี่กึ่ง|泡麵|速食|即席", re.I)
_A_NOT_INSTANT = re.compile(
    r"sauce|soup base|seasoning|paste|frozen|fresh|chilled|cooked|prepared|udon|naengmyeon|cold|kit|snack|chip|cracker"
    r"|dumpling|gochujang|チルド|生|冷|soup only|broth|スープ|salad|サラダ|kimbap|konjac|shirataki|tofu|zero|egg white"
    r"|roll|こんにゃく|糖質|sausage|chicken breast|soup|wonton|almond|vegetables? for|鍋|つゆ", re.I)
_A_NUT_WORDS = re.compile(r"almonds?|peanuts?|cashews?|nuts?|pistachios?|walnuts?|macadamias?|pecans?|hazelnuts?|kernels?"
                          r"|ナッツ|アーモンド|ピーナッツ|カシューナッツ|견과|아몬드|땅콩|호두", re.I)
_A_NUT_MODS = re.compile(
    r"salted|unsalted|lightly|roasted|baked|smoked|honey|coated|mixed|mix|sugar(?:ed)?|spicy|hot|garlic|wasabi|nori"
    r"|seaweed|flavou?r(?:ed)?|original|everyday|daily|premium|natural|raw|whole|sliced|blanched|dry|dried|fried|and"
    r"|with|of|in|shell|skin|the|crunchy|crispy|bbq|barbecue|chilli|chili|salt|sea|plain|black pepper|pepper"
    r"|tom ?yum|coffee|cuttlefish|squid|prawn|shrimp|flavor|organic|jumbo|large|giant|super|deluxe|selection|select"
    r"|toasted|caramelized|candied|sweet|japanese|korean|thai|grams?|g|pack|can|\d+|[-&/(),.'+%:!]|味|入り|ロースト|素焼き|塩|소금|구운", re.I)
_A_CHOC_WORDS = re.compile(r"chocolates?|choco|m&m'?s|kit ?kat|ghana|meltykiss|chocoball|toblerone|cadbury|dairy milk"
                           r"|hershey'?s|kisses|ミルクチョコレート|ブラックチョコレート|チョコレート|チョコ|초콜릿|초코|ช็อกโกแลต|巧克力",
                           re.I)
_A_CHOC_MODS = re.compile(
    r"milk|dark|white|black|bitter|sweet|semi|creamy|rich|bar|bars|block|ball|balls|bites?|mini|pieces?|squares?"
    r"|almonds?|hazelnuts?|crisp(?:y|ed)?|rice|caramel|mint|orange|peanuts?|fruit|nut|raisins?|and|with|&|of|the"
    r"|original|classic|premium|extra|minis|treat|share|pack|bag|box|\d+%?|cacao|cocoa|[-/(),.'+%:!]|g|板|ミルク"
    r"|ハイカカオ|アーモンド|ブラック|ビター|ホワイト|味|入り|밀크|다크|화이트", re.I)
_A_NOT_CHOC = re.compile(r"(?:choc\w*|ช็อกโกแลต) milk|milk$|drink|häagen|haagen|movenpick|mövenpick|wall'?s|magnum"
                         r"|ice cream|cookie|aice|drumstick", re.I)
_A_DRINK = re.compile(r"juice|drink|soda|(?<![a-z])tea(?![a-z])|(?<![a-z])teh(?![a-z])|tehbotol|trà|cola|sprite|lemonade|beverage|sparkling|(?<![a-z])water"
                      r"|isotonic|ジュース|茶|ソーダ|음료|주스|사이다|น้ำ|飲料", re.I)
_A_DRINK_CATEGORIES = frozenset((
    "sodas", "carbonated drinks", "soft drinks", "iced teas", "tea-based beverages", "waters", "flavored waters",
    "energy drinks", "sport drinks", "fruit juices", "juices and nectars"))
_A_NOT_THIN_DRINK = re.compile(
    r"syrup|cordial|concentrat|powder|mix|instant|3 ?in ?1|jam|honey|condensed|sauce|jelly|smoothie|yog|shake|protein"
    r"|milk|latte|coffee|cocoa|choco|malt|soy|(?<![a-z])oat|almond|coconut milk|sikhye|bubble|boba|pearl|ginseng"
    r"|amazake|sake|wine|beer|liqueur|plum wine|umeshu|vinegar|stick|leaves|bag|loose|leaf|granul|candy|gummy|cake"
    r"|bar(?![a-z])|ice|flavou?r(?:ed)? (?:powder|base)|sorbet|ぜんざい|ようかん|羊羹|あずき|chestnut|น้ำจิ้ม|เชื่อม"
    r"|นมเปรี้ยว|kratingdaeng|krating|m-150|m150|carabao|lipovitan|red ?bull|sambal|mochi|aloe|nata|ゼリー|サクレ|coolish"
    r"|calippo|soda ?seven|ソーダセブン|popsicle|enzyme|extract|ginger|酢|식초|金時|餅|dessert|paste|choya|梅酒|gel(?![a-z])"
    r"|ジュレ|オレ|au lait|dutch ?mill|ดัชมิลล์|キャンデー|pirulo|原液|しそ|抹茶|ぷりん|プリン|pudding|soluble|袋", re.I)
_A_SOFT = re.compile(
    r"cola|sprite|7 ?up|fanta|(?<![a-z])soda|soft drink|sparkling|(?<![a-z])tea(?![a-z])|isotonic|sport|pocari|gatorade|aquarius"
    r"|coconut water|lemonade|tonic|(?<![a-z])sting(?![a-z])|vitamin (?:drink|water)|(?<![a-z])teh(?![a-z])|tehbotol|茶|ソーダ|サイダー|コーラ|スポーツ"
    r"|사이다|콜라|에이드|peachade|(?<![a-z])ade(?![a-z])|ชา|โคล่า|น้ำมะพร้าว|เกเตอเรด|trà|nước ngọt|汽水|可樂", re.I)
_A_JUICE = re.compile(r"juice|(?<![a-z])jus(?![a-z])|ジュース|果汁|주스|น้ำส้ม|น้ำ.*100|nước ép|buavita|minute maid|มินิทเมด",
                      re.I)
_A_RICH_JUICE = re.compile(r"grape|ぶどう|องุ่น|prune|aronia|アロニア|durian|ทุเรียน|甘酒|amazake|mango|nectar", re.I)
_A_MIX = re.compile(r"\b[23] ?in ?1\b|\b[23]in1\b|coffee ?mix|white coffee|milk tea powder|\bmilo\b|ovaltine", re.I)
_A_NOT_MIX = re.compile(r"\bbars?\b|cookies?|biscuits?|wafers?|cereal|nuggets?|cube|chocolate|snack|ice|cake|cone|kosong"
                        r"|no sugar|sugar ?free|without sugar|black|bean|ground|ready to drink|\brtd\b|can\b|bottle|uht"
                        r"|plus|kopi o\b", re.I)
# Chocolate-biscuit lines that are a product name by themselves, read in the name and the brand
# ("Choco filled" by Pepero, "KitKat Wasabi"): a snack like the words above.
_A_SNACK_LINE = re.compile(r"pocky|pepero|kit ?kat|black thunder|kinoko no yama|takenoko no sato|kinoko to takenoko|(?:kinoko|takenoko).*meiji|meiji.*(?:kinoko|takenoko)|alfort|toppo(?![a-z])|(?<![a-z])pretz(?![a-z])"
                           r"|ブラックサンダー|キットカット|ポッキー|빼빼로", re.I)
# Crisps proper are 450 to 570 kcal: for them the snack test asks for 400, not 300.
_A_FRIED_CHIPS = re.compile(r"potato chips?|potato crisps|(?<![a-z])crisps(?![a-z])|pringles|(?<![a-z])lay'?s(?![a-z])|doritos|cheetos"
                            r"|honey butter chip|ポテトチップ|감자칩|มันฝรั่งทอด", re.I)
# Cereal, protein and nut bars (330 to 500 kcal). Other "bars" are ice creams, fish cakes, chicken breast and hot bars.
_A_BAR = re.compile(r"(?:protein|granola|cereal|energy|(?<![a-z])oat|muesli|(?<![a-z])nut|endurance|sesame|chunky) bars?(?![a-z])"
                    r"|(?<![a-z])bars?,? (?:protein|granola|cereal|energy)", re.I)
_A_BAR_BRAND = re.compile(r"pow ?bar|nature valley|(?<![a-z])quest(?![a-z])", re.I)
_A_NOT_BAR = re.compile(r"sasami|surimi|salad chicken|tuna|bonito|sausage|jerky|(?<![a-z])egg|hot bar|crepe|sundae|(?<![a-z])ice(?![a-z])|frozen|paleta|popsicle|サラダチキン|鰹|ちくわ", re.I)
_A_NUT_BUTTER = re.compile(r"peanut butter|almond butter|cashew butter|hazelnut (?:cocoa )?spread|nutella", re.I)
_A_NOT_NUT_BUTTER = re.compile(
    r"cookie|biscuit|cracker|sandwich|wafer|bread|toast|bun(?![a-z])|cake|cup|pretzel|cereal|granola|(?<![a-z])bars?(?![a-z])"
    r"|powder|filled|filling|flavou?r|(?<![a-z])ice(?![a-z])|milk|drink|shake|smoothie|chocolate|candy|puff|(?<![a-z])pops?"
    r"|snack|roll|pie(?![a-z])|m&m|oreo|ritz|(?<![a-z])with(?![a-z])|jelly|cream(?!y)|mochi|ball|bites?|coated|noodle|sauce"
    r"|pancake|waffle|kit(?![a-z])|light|lite|reduced|whip|pb ?2|pbfit", re.I)
_A_CANDY = re.compile(r"cand(?:y|ies)|gumm(?:y|ies)|mentos|hi-?chew|lozenges?|(?<![a-z])chews?(?![a-z])|lollipop"
                      r"|mintia|(?<![a-z])frisk(?![a-z])|breath mints?|キャンディ|キャンデー|グミ", re.I)
# Breakfast cereals (350 to 480 kcal), hard and processed cheeses (250 to 480), fermented bean and
# pepper pastes (170 to 230) and dried seaweed sold by the pinch (100 to 250).
_A_CEREAL = re.compile(r"granola|muesli|(?:corn|bran|wheat|oat|frosted|snowy) ?flakes|cereal|frugra|フルグラ|グラノーラ|シリアル", re.I)
_A_NOT_CEREAL = re.compile(r"(?<![a-z])bars?(?![a-z])|porridge|oatmeal|overnight|cookie|biscuit|toast|yog|トースト|ヨーグルト|パン", re.I)
_A_HARD_CHEESE = re.compile(r"parmesan|parmigiano|cheddar|gouda|camembert|emmental|(?<![a-z])edam(?![a-z])|mozzarella"
                            r"|(?:processed|sliced) cheese|cheese slices?|粉チーズ|スライスチーズ|プロセスチーズ", re.I)
_A_NOT_CHEESE = re.compile(
    r"flavou?r|snack|cracker|biscuit|cookie|chips?(?![a-z])|crisps|pizza|burger|pasta|risotto|gratin|curry|roll|ball"
    r"|dumpling|gyoza|ramen|noodle|rice|sausage|(?<![a-z])ham(?![a-z])|egg|(?<![a-z])dog|toast|potato|corn|dessert|powdered"
    r"|macaroni|spaghetti|lasagn|salad|omelet|katsu|croquette|korokke|tteok|topokki|hotteok|cod(?![a-z])|tara|taco"
    r"|chikuwa|kamaboko|ちくわ", re.I)
_A_BEAN_PASTE = re.compile(r"(?:gochujang|doenjang|ssamjang|(?:hot|red) pepper paste|soy ?bean paste|(?<![a-z])miso)$", re.I)
_A_DRIED_SEAWEED = re.compile(r"sea vegetable|wakame|hijiki|kombu(?!cha)|kelp|seaweed|(?<![a-z])(?:nori|laver)(?![a-z])|わかめ|昆布|ひじき", re.I)
_A_NOT_DRIED_SEAWEED = re.compile(r"soup|salad|drink|(?<![a-z])tea(?![a-z])|paste|sauce|noodle|ramen|udon|soba|rice|snack"
                                  r"|chips?(?![a-z])|roll|maki|onigiri|bento|tofu|egg|natto|dashi|mayo|butter|cheese|flavou?r"
                                  r"|pickle|kimchi|fresh|(?<![a-z])wet|frozen|jelly|konjac|スープ|サラダ|茶", re.I)
# A coffee, cocoa or tea-with-milk powder sold by the stick or sachet (380 to 480 kcal), named without "3 in 1".
_A_STICK_MIX = re.compile(r"latte|cappuccino|mocha|caf[eé] au lait|milk tea|hot chocolate|(?<![a-z])cocoa(?![a-z])", re.I)
_A_NORI = re.compile(r"(?<![a-z])(?:nori|laver)(?![a-z])|seasoned layer", re.I)
_A_NOT_NORI = re.compile(r"maki|bento|onigiri|musubi|wrap|tsukudani|ramen|udon|soba|tempura batter|mayo|butter|cheese"
                         r"|tofu|egg|natto|wakame|kombu|kelp|dashi|drink|tea(?![a-z])|ochazuke|shio|のりしお|flavou?r", re.I)
_A_BAKING_MIX = re.compile(r"(?<![a-z])(?:muffin|cake|brownie|waffle|cookie) mix", re.I)
# Drinks whose calories per 100 ml are known within a narrow band, read in the name and the brand:
# soy, oat and almond milks (30 to 85) and plain milk (45 to 70) over 100 kcal, and sports
# drinks (10 to 30) over 45, carry a glass's or a bottle's figures. Powders, creams, puddings,
# coffees and the like are left alone.
_A_PLANT_MILK = re.compile(r"oat ?milk|oat ?drink|soya? ?milk|soya? ?bean (?:milk|drink)|soya? drink|almond ?milk|nutrisoy"
                           r"|oatside|豆乳(?:飲料)?$|두유$|นมถั่วเหลือง|豆奶$|豆漿$", re.I)
_A_FRESH_MILK = re.compile(r"(?:fresh|whole|full cream|low ?fat|skimm?(?:ed)?|pasteuri[sz]ed|uht|\d ?%) milk(?! (?:powder|tea|coffee"
                           r"|chocolate|candy|bread|biscuit|cookie|cake|pudding|toast))|牛乳$", re.I)
# Coffee or tea with milk, ready to drink (40 to 90 kcal per 100 ml).
_A_MILK_COFFEE = re.compile(r"latte|au lait|coffee milk|milk coffee|milk tea|ミルクティー|カフェオレ|カフェラテ", re.I)
_A_NOT_MILK = re.compile(
    r"powder|condensed|evaporated|creamer|cream(?![a-z])|(?<![a-z])ice(?![a-z])|pudding|tofu|yog|(?<![a-z])bars?(?![a-z])"
    r"|biscuit|cookie|bread|cake|candy|toast|jam|spread|cheese|butter|protein|(?<![a-z])mix|cereal|granola|pancake"
    r"|dessert|jelly|skin|sheet|stick|okara|flour|wafer|roll|bun(?![a-z])|pie(?![a-z])|soap|kaya|custard|mochi|donut"
    r"|sauce|soup|noodle|tau ?huay|beancurd|douhua|stew|sachet|濃縮|浓缩|粉|パン|クッキー|アイス|プリン|スープ|鍋|ラテの素", re.I)
_A_ISOTONIC = re.compile(r"pocari|gatorade|aquarius|100 ?plus|isotonic|sports? drink|powerade|ion (?:supply|water)"
                         r"|ポカリ|アクエリアス|이온음료|이온 ?워터", re.I)
_A_NOT_ISOTONIC = re.compile(r"powder|jelly|gel(?![a-z])|tablet|candy|concentrat|ice|sachet|stick|パウダー|粉|ゼリー", re.I)
_A_CALORIC = re.compile(r"\b(?:mandu|dumplings?|gyoza|buns?|cakes?|bread|noodles?|ramen|ramyun|rice|cookies?|crackers?"
                        r"|chips|crisps|chocolate|candy|cheese|yogh?urt|sausages?|meat|chicken|pork|beef|fish|bars?|nuts?"
                        r"|almonds|peanuts|snacks?|biscuits?|wafers?|mochi|pies?|pasta|cereal|granola|curry|tteok"
                        r"|tteokbokki|pizza|burger|sandwich|onigiri|sushi|kimbap|gimbap)\b", re.I)


# An oil or a fat stored at 0 kcal with 0 g of fat (an oil is 880 to 900).
_A_ZERO_FAT = re.compile(r"(?<![a-z])(?:oils?|butter|ghee|lard|margarine|shortening)(?![a-z])", re.I)
_A_NOT_ZERO_FAT = re.compile(r"spray|in (?:olive |sunflower |vegetable )?oil|(?:peanut|apple|almond|nut|cocoa|shea) butter"
                             r"|flavou?r|butterfly|bear", re.I)
# Dry instant-noodle and pasta packs sold by the pack (an 85 g Koka or Mama pack is 450 to 480 kcal
# per 100 g dry): under 250 kcal with a serving of 130 g or less (or none) their figures are the
# made-up bowl's or one serving's. Read in the name and the brand; cups, bowls and soups are left alone.
_A_DRY_PACK = re.compile(r"(?<![a-z])koka(?![a-z])|(?<![a-z])mama(?![a-z])|maggi|indomie|nissin|mayora|(?<![a-z])wow(?![a-z])"
                         r"|la fonte|goreng", re.I)
_A_PASTA = re.compile(r"spag(?:h)?et(?:t)?i|la fonte", re.I)
_A_NOT_DRY_PACK = re.compile(r"(?<![a-z])cups?(?![a-z])|bowl|soup|udon|frozen|cooked|prepared|chilled|fresh|sauce|seasoning"
                             r"|perencah|bumbu|paste|カップ|컵", re.I)
# Infant formula and supplements, kept out of the three pools (read in the name; never a bare
# "formula": Thai milks say "Sugar Formula", and never "C1000": House C1000 drinks are drinks).
# A protein bar or powder, a nutrition drink or a collagen jelly that names a supplement is food.
_A_NOT_FOOD = re.compile(
    r"optipro|(?<![a-z])nan (?:optipro|ha|pro)(?![a-z])|lactogen|similac|enfamil|enfagrow|s-?26 (?:gold|progress)|bebelac"
    r"|dugro|nestogen|friso(?:lac| gold)|(?<![a-z])mamil(?![a-z])|aptamil|nutrilon|isomil|infant formula|follow[- ]?on formula"
    r"|growing[- ]?up (?:milk|formula)", re.I)
_A_SUPPLEMENT = re.compile(r"creatine|(?<![a-z])hmb \d|dietary supplement|e-omega|vitamin c \d+ ?mg", re.I)
_A_NOT_SUPPLEMENT = re.compile(r"protein|(?<![a-z])bars?(?![a-z])|ensure|jelly|drink", re.I)
_A_NOT_FOOD_CATEGORIES = frozenset(("infant formulas", "baby milks"))


def _a_plain(name, words, modifiers):
    """True when name is nothing but words (at least one of them) and modifiers."""
    if not words.search(name):
        return False
    rest = modifiers.sub(" ", words.sub(" ", name.lower()))
    return not re.sub(r"\s+", "", rest)


def asia_kind(row, categories):
    """Which of the food kinds above a row from these markets is (with too few calories), or ""."""
    name, brand = row["name"] or "", row["brand"] or ""
    kcal = row["calories_kcal"]
    portions = row["portions"] or []
    labels = [str(p[0]) for p in portions]
    cats = {item.strip().lower() for item in (categories or "").split(",") if item.strip()}
    no_ml = not any(re.search(r"(?<![a-z])ml(?![a-z])|(?<![a-z])ice(?![a-z])", label, re.I) for label in labels)
    if _A_ICE_BRAND.search(name + " " + brand):
        return ""
    dry = no_ml and not _A_NOT_DRY.search(name)
    if dry and (kcal < 300 and (_A_SNACK.search(name) or cats & _A_SNACK_CATEGORIES or _A_SNACK_LINE.search(name + " " + brand))
                or kcal < 400 and _A_FRIED_CHIPS.search(name) and not re.search(r"protein|baked|プロテイン", name, re.I)):
        return "snack"
    if dry and kcal < 280 and (_A_BAR.search(name) or _A_BAR_BRAND.search(brand) and re.search(r"(?<![a-z])bars?(?![a-z])", name + " " + brand, re.I)) \
            and not _A_NOT_BAR.search(name):
        return "bar"
    if no_ml and kcal < 250 and _A_NUT_BUTTER.search(name) and not _A_NOT_NUT_BUTTER.search(name):
        return "nutbutter"                  # 580 to 650 kcal; two spoonfuls are 190
    if dry and 0 < kcal < 100 and _A_CANDY.search(name) \
            and not re.search(r"cotton candy|floss|konjac|konnyaku|こんにゃく|蒟蒻|red ?bull|energy", name, re.I):
        return "candy"
    if no_ml and kcal < 250 and _A_BAKING_MIX.search(name) and not re.search(r"prepared|ready", name, re.I):
        return "bakingmix"
    if dry and kcal < 300 and _A_CEREAL.search(name) and not _A_NOT_CEREAL.search(name) \
            and not any(re.search(r"(?<![a-z])(?:can|box|bottle)(?![a-z])", label, re.I) for label in labels):
        return "cereal"                     # not a ready-to-eat can or box of cereal
    if dry and kcal < 150 and _A_HARD_CHEESE.search(name) and not _A_NOT_CHEESE.search(name):
        return "cheese"
    if no_ml and 0 < kcal < 100 and _A_BEAN_PASTE.search(re.sub(r"\s*\([^)]*\)", "", name).strip()) \
            and not re.search(r"konjac|konnyaku", name, re.I):
        return "paste"
    spoon = any(re.search(r"tbsp|tsp|spoon", label, re.I) for label in labels)     # nori paste, not a snack
    if kcal < 150 and not spoon and (_A_SEAWEED.search(name) or _A_SEAWEED_NATIVE.search(name) or _A_SEAWEED_BRAND.search(brand + " " + name)) \
            and not _A_NOT_SEAWEED_SNACK.search(name):
        return "seaweed"
    if kcal < 100 and not spoon and _A_NORI.search(name) and not _A_NOT_SEAWEED_SNACK.search(name) \
            and not _A_NOT_NORI.search(name) and not _A_NOT_DRY.search(name):
        return "seaweed"                    # sheets of nori or laver are 190 to 350 kcal
    if kcal < 50 and not spoon and portions and portions[0][1] and portions[0][1] <= 10 and _A_DRIED_SEAWEED.search(name) \
            and not _A_NOT_DRIED_SEAWEED.search(name):
        return "seaweed"                    # a serving of 10 g or less is dried seaweed
    if kcal < 400 and no_ml and _a_plain(name, _A_NUT_WORDS, _A_NUT_MODS) \
            and not any(p[1] and p[1] >= 150 for p in portions) \
            and not any(re.search(r"glass|(?<![a-z])cup|bottle|carton", label, re.I) for label in labels):
        return "nuts"                       # not a glass of almond drink ("Almond Original", 1 glass (180 g))
    if kcal < 350 and no_ml and _a_plain(name, _A_CHOC_WORDS, _A_CHOC_MODS) and not _A_NOT_CHOC.search(name + " " + brand):
        return "chocolate"
    first = portions[0][1] if portions else None
    if _A_NOODLE.search(name) and _A_INSTANT.search(name + " " + brand) \
            and (not _A_NOT_INSTANT.search(name) or re.search(r"cup noodle", name, re.I)) \
            and kcal < 130 and (first is None or first < 130):
        return "noodles"
    if (_A_NOODLE.search(name) or _A_PASTA.search(name)) and _A_DRY_PACK.search(name + " " + brand) \
            and not _A_NOT_DRY_PACK.search(name) and kcal < 250 and (first is None or first <= 130):
        return "noodles"                    # a dry pack's made-up figures (see _A_DRY_PACK)
    return ""


# --- More food kinds with a calorie floor (second reading of step 5, November 2026) ---
# Read on every candidate row of the three pools that the tests above let through: biscuits not
# named "biscuit" (Bisco, Elise, Calorie Mate), bread and buns, Japanese sweets, oats and bran,
# protein and amino-acid powders, plain eggs, honey, mirin and cooking sake, thick sauces, dried
# squid and natto, each with one serving's, piece's or spoonful's figures typed per 100 g. Each
# kind: (words in the name, words that make it another food, floor, range once re-based on the
# pack's own serving). Under the floor the row is re-based on its serving when that lands in the
# range (asia_serving_fix), and kept out ("dense_low") otherwise. An egg is also kept out above
# 220 kcal (several eggs' figures on one egg's serving). Ice cream, quail eggs and ice-cream
# monaka were wrong or unsure there and are left alone; a row whose serving is in ml is left
# alone except for the sauce and mirin kinds.
_K_ICE = re.compile(r"(?<![a-z])ice(?![a-z])|(?<![a-z])eis(?![a-z])|ice ?cream|gelato|sorbet|frozen|アイス|아이스|ไอศกรีม|monaka|coolish|(?<![a-z])pino"
                    r"(?![a-z])|häagen|haagen|dazs|soft serve|popsicle|(?<![a-z])cone|yukimi|sundae|quail|うずら|메추리", re.I)
_K_LIQUID = re.compile(r"(?<![a-z])ml(?![a-z])|fl ?oz|(?<![a-z])l(?![a-z])|bottle|(?<![a-z])can(?![a-z])|本|缶", re.I)
_K_WET = re.compile(r"drink|milk(?! choc)|juice|(?<![a-z])tea(?![a-z])|latte(?! stick)|shake|smoothie|jelly|yog|pudding|soup"
                    r"|rtd|beverage|water|soda|ゼリー|ドリンク|우유|음료|นม", re.I)
ASIA_KIND_FLOORS = (
    ("biscuit", re.compile(r"(?<![a-z])bisco(?![a-z])|elise|biscuit|cookie|cracker|wafer|rusk|crepe roll|brownie|calorie ?mate"
                           r"|cheese bake|sabl[eé]|langue de chat|financier|baumkuchen|madeleine|senbei|arare|okaki|age ?ichiban"
                           r"|rice snack|pretzel|クッキー|ビスケット|せんべい|과자|쿠키", re.I),
     re.compile(r"sandwich ice|ice cream|cream puff|cheesecake|ramen|noodle|soup|drink|milk(?! choc)|cereal|bar ?ice"
                r"|cookies? ?(?:and|&|n) ?cream|tteok|pokki|(?<![a-z])bars?(?![a-z])|protein|yog|(?<![a-z])buns?(?![a-z])"
                r"|macaroni|with crackers|spread", re.I), 300, (350, 600)),
    ("wagashi", re.compile(r"manju|daifuku|(?<![a-z])mochi(?![a-z])|dango|yokan|ohagi|dorayaki|taiyaki|abekawa|kintsuba"
                           r"|まんじゅう|大福|団子|羊羹", re.I),
     re.compile(r"konjac|konnyaku|jelly|drink|ice|cream|chocolate|soup|zenzai|shiruko|amazake|kinako|flour|powder", re.I),
     170, (200, 340)),
    ("oats", re.compile(r"(?<![a-z])oats?(?![a-z])|oatmeal|granola|muesli|flakes|cereal|frugra|(?<![a-z])bran(?![a-z])"
                        r"|フルグラ|グラノーラ|シリアル|オートミール", re.I),
     re.compile(r"(?<![a-z])bars?(?![a-z])|drink|milk(?! choc)|yog|cookie|biscuit|bread|porridge|cooked|prepared"
                r"|ready to eat|soup|seasoning|salmon|tuna|bonito|katsuo|fish|chili|chilli|pepper|coconut|onion|garlic"
                r"|potato|corn soup|seaweed|nori|rice|m.?lk|(?<![a-z])ya(?![a-z])|congee|fettuccine|noodle|pasta|overnight|loaf"
                r"|flakes in|haw|milo", re.I), 280, (340, 480)),
    ("powder", re.compile(r"whey|protein powder|(?<![a-z])eaa(?![a-z])|bcaa|collagen|soy protein|isopro|(?<![a-z])wpi(?![a-z])"
                          r"|casein|プロテイン|ホエイ|단백질", re.I),
     re.compile(r"drink|water|shake(?! powder)|rtd|jelly|yog|milk(?! protein powder)|bar(?![a-z])|cookie|chips|snack"
                r"|latte|ready|bottle|ゼリー|ドリンク|ヨーグルト|バー", re.I), 250, (300, 430)),
    ("egg", re.compile(r"^(?:(?:boiled|soft|hard|semi|half|[- ]|onsen|hot spring|poached|seasoned|marinated|smoked|salted|raw"
                       r"|fresh|free range|vitamin [a-z]|extra|with|white|brown|large|medium|organic|japanese|flavou?r|plain"
                       r"|sauce|&|and)\s*)*eggs?(?![a-z])|(?<![a-z])(?:boiled|soft[- ]boiled|onsen|hot spring|seasoned|marinated"
                       r"|smoked) eggs?|ajitama|nitamago|ゆでたまご|ゆで卵|温泉たまご|味付.*たまご|^卵$|^たまご$|계란$|달걀$", re.I),
     re.compile(r"noodle|sandwich|salad|tart|pudding|soup|rice|white|porridge|omelet|roll|furikake|sauce|mayo|curry"
                r"|bread|biscuit|cookie|cake|drink|nog|powder|dumpling|ramen|udon|soba|tofu|custard|chips|snack|plant"
                r"|pan(?![a-z])|gohan|simmered|bar(?![a-z])|protein|salted egg|flavou?r|chip|crisp|skin|popcorn|pastry"
                r"|waffle|toast|todu|shumai|spread|crab|anchov|bean", re.I), 115, (125, 200)),
    ("bread", re.compile(r"(?<![a-z])bread|toast|(?<![a-z])buns?(?![a-z])|(?<![a-z])pan(?![a-z])|minipan|sandwich bread"
                         r"|english muffin|bagel|melon ?pan|croissant|(?<![a-z])roti(?![a-z])|loaf|食パン|パン|빵", re.I),
     re.compile(r"ice|with buns|japchae|cream sandwich|sandwich(?! bread)|chicken|pork|beef|tuna|fish|shrimp|prawn|siu ?mai"
                r"|dim sum|cabbage|vegetable|curry|egg|nugget|chocolate|coconut|almond|crumb|panko|mix|flour|spread|jam|soup"
                r"|pan ?fried|frying pan|pancake|japan|pandan|pancit|spanish|panna|sauce|pudding|salad", re.I), 180, (220, 420)),
    ("honey", re.compile(r"^(?:(?:pure|raw|white|natural|acacia|wild|manuka|clover|flower|forest|organic|japanese|korean|thai"
                         r"|premium|domestic|mountain|lychee|longan|sunflower|buckwheat|chestnut|(?:\d+%?)|[-&/(),.']|\s)+)?honey$"
                         r"|^はちみつ$|^蜂蜜$|^(?:\S+ )?꿀$", re.I),
     re.compile(r"drink|tea|water|vinegar|lemon|yuzu|citron|chips|nuts|almond|butter|yog|candy|cake", re.I), 250, (280, 340)),
    ("mirin", re.compile(r"(?<![a-z])(?:hon)?mirin(?![a-z])|みりん|cooking sake|cooking rice wine|ryori ?shu|料理酒|미림|맛술", re.I),
     re.compile(r"style|fu(?![a-z])|風|type", re.I), 50, (70, 260)),
    ("sauce", re.compile(r"worcester|tonkatsu sauce|okonomi|chuno|中濃|ウスター|takoyaki sauce|yakisoba sauce|relish", re.I),
     re.compile(r"chips|snack|flavou?r|cracker|noodle|ramen", re.I), 60, (90, 180)),
    ("dried fish", re.compile(r"dried squid|surume|saki ?ika|(?<![a-z])jerky|dried fish|niboshi|dried (?:salmon|scallop|shrimp)"
                              r"|(?:dried|roasted) filefish|するめ|さきいか|ジャーキー|쥐포|육포", re.I),
     re.compile(r"soup|stock|dashi|broth|furikake|sauce|chips|cracker|flavou?r|congee|porridge|rice", re.I), 190, (230, 420)),
    ("natto", re.compile(r"natto|納豆|낫또", re.I), re.compile(r"powder|snack|freeze|dried|roll|maki|sushi|soup|furikake", re.I),
     130, (170, 230)),
)
_K_LIQUID_KINDS = ("mirin", "sauce")
_K_DRY_KINDS = ("biscuit", "oats", "powder", "bread", "wagashi")
_K_EGG_CEILING = 220


def asia_kind_floor(row):
    """(kind, (low, high)) for a row of one of the kinds of ASIA_KIND_FLOORS whose calories are off for it, else None."""
    name, brand = row["name"] or "", row["brand"] or ""
    kcal = row["calories_kcal"]
    portions = row["portions"] or []
    labels = " ".join(str(p[0]) for p in portions)
    for kind, words, other, floor, rebase in ASIA_KIND_FLOORS:
        if not words.search(name) or other.search(name) or (kind == "oats" and other.search(brand)):   # "Flakes" by Century Tuna
            continue
        if _K_ICE.search(name + " " + brand):
            return None
        if kind not in _K_LIQUID_KINDS and _K_LIQUID.search(labels):
            return None
        if kind in _K_DRY_KINDS and _K_WET.search(name):
            return None
        if kind == "powder" and any(p[1] and p[1] >= 100 for p in portions):
            return None                     # a bottle or a 250 g drink, not a powder
        if kcal < floor or (kind == "egg" and kcal > _K_EGG_CEILING):
            return kind, rebase
        return None
    return None


def asia_doubt(row, label, categories):
    """The doubt reason for a row from these markets (see the notes above), or ""."""
    name = row["name"] or ""
    kcal, protein, carbs, fat = row["calories_kcal"], row["protein_g"], row["carbs_g"], row["fat_g"]
    if kcal == 0 and not protein and not carbs and not fat and _A_CALORIC.search(name) \
            and not _ZERO_NAME.search(name + " " + (row["brand"] or "")) and not _FEW_KCAL_CARBS.search(name):
        return "zero_food"
    if kcal == 0 and not fat and _A_ZERO_FAT.search(name) and not _A_NOT_ZERO_FAT.search(name):
        return "zero_food"                  # "Avocado Oil" at 0 kcal and 0 g fat
    if kcal == 0 and not protein and not carbs and not fat and re.search(r"protein (?:bar|shake|powder|drink)", name, re.I):
        return "zero_food"                  # "Zero (Protein Bar)" with no protein
    if asia_kind(row, categories) or asia_kind_floor(row):
        return "dense_low"
    labels = [str(p[0]) for p in row["portions"] or []]
    cats = {item.strip().lower() for item in (categories or "").split(",") if item.strip()}
    liquid = any(p[1] and p[1] >= 150 and re.search(r"ml|fl ?oz|(?<![a-z])l(?![a-z])|can|bottle|缶|本|kaleng|botol",
                                                    str(p[0]), re.I) for p in row["portions"] or [])
    if (70 <= kcal <= 170 and (protein or 0) < 2 and (fat or 0) < 1 and (carbs or 0) > 15
            and (_A_DRINK.search(name) or liquid or cats & _A_DRINK_CATEGORIES) and not _A_NOT_THIN_DRINK.search(name)
            and not any(re.search(r"ice|bar|tb|tbsp|spoon", text, re.I) for text in labels)
            and not any(p[1] and p[1] < 50 for p in row["portions"] or [])          # a powder or a concentrate
            and not re.search(r"resource|ぶどう糖|glucose", name, re.I)):         # a medical drink, glucose cider
        if _A_SOFT.search(name) or (_A_JUICE.search(name) and kcal > 85 and not _A_RICH_JUICE.search(name)):
            return "drink_high"
    named = name + " " + (row["brand"] or "")
    if 100 < kcal and not _A_NOT_MILK.search(name) and not _A_ICE_BRAND.search(named) \
            and (kcal <= 200 and (fat or 0) < 12 and (_A_PLANT_MILK.search(named) or _A_FRESH_MILK.search(name))
                 or re.search(r"fresh milk$", name, re.I)):
        return "drink_high"
    if 45 < kcal <= 200 and _A_ISOTONIC.search(named) and not _A_NOT_ISOTONIC.search(name) \
            and not any(p[1] and p[1] < 100 for p in row["portions"] or []):
        return "drink_high"
    # A drink sold by the can or bottle (a serving of 150 ml or more, in ml): sugar water over 95 kcal
    # per 100 ml whatever its name ("Rasa Original" by Coca-Cola at 100, a 400 ml can at 224), and a
    # coffee or tea with milk over 100, carry the can's or the bottle's figures.
    by_ml = any(p[1] and p[1] >= 150 and re.search(r"(?<![a-z])ml(?![a-z])", str(p[0]), re.I) for p in row["portions"] or [])
    if by_ml and not any(re.search(r"ice|bar|tb|tbsp|spoon", text, re.I) for text in labels) \
            and not re.search(r"resource|ぶどう糖|glucose", name, re.I):
        if 95 < kcal <= 300 and (protein or 0) <= 1 and (fat or 0) < 0.5 and 4 * (carbs or 0) >= 0.75 * kcal \
                and not _A_NOT_THIN_DRINK.search(name) and not _A_RICH_JUICE.search(name):
            return "drink_high"                 # the calories are sugar's (a whisky or a sake has none)
        if 100 < kcal <= 250 and _A_MILK_COFFEE.search(name) and not _A_NOT_MILK.search(name) and not _A_ICE_BRAND.search(named) \
                and not re.search(r"cone|waffle|monaka|soft|コーン|ワッフル|モナカ|ソフト", name, re.I):
            return "drink_high"
    small = [p[1] for p in row["portions"] or [] if p[1]]
    if 40 < kcal < 250 and small and max(small) <= 40 and _A_STICK_MIX.search(name) and not _A_NOT_MIX.search(name) \
            and not _A_NOT_DRY.search(_A_STICK_MIX.sub(" ", name)) and not any(_LIQUID_LABEL.search(text) for text in labels) \
            and (carbs or 0) >= 4 * (protein or 0):
        return "mix_low"                    # "Nescafe Latte Caramel" at 105 kcal with a 25 g sachet
    if kcal < 250 and _A_MIX.search(name) and not _A_NOT_MIX.search(name) and not _LIQUID_LABEL.search(label or "") \
            and not any(_LIQUID_LABEL.search(text) for text in labels) and "pudding" not in name.lower() \
            and not any(p[1] and p[1] >= 150 for p in row["portions"] or []):      # a ready-to-drink bottle
        return "mix_low"
    return ""


# Calories per 100 g a snack, chocolate, nut, bar or nut butter product may have once re-based on its serving.
_A_REBASE_RANGE = {"snack": (400, 600), "chocolate": (450, 600), "nuts": (500, 700), "bar": (330, 600),
                   "nutbutter": (520, 700), "cereal": (340, 520)}


def asia_serving_fix(row, serving_size, serving_g, categories):
    """Repair a snack, chocolate, nut, bar or nut butter row from these markets whose label was typed per serving.

    The row is one asia_kind calls "snack", "chocolate", "nuts", "bar" or "nutbutter" (too few
    calories for the food). When its stated serving (5 to 120 g) explains the numbers, that is, the figures
    taken as that serving's give a believable figure per 100 g (_A_REBASE_RANGE) with no more
    than 100 g of protein, carbs and fat, the figures are re-based to 100 g, the basis becomes
    "estimate" and the serving is pinned as the first portion: Lay's from Thailand at 150 kcal
    with a 30 g serving becomes 500 kcal per 100 g, opening on "30 g". Returns True when done.
    """
    kind = asia_kind(row, categories)
    heaviest = 120
    if kind in _A_REBASE_RANGE:
        low, high = _A_REBASE_RANGE[kind]
    elif kind:
        return False
    else:
        floor = asia_kind_floor(row)        # the kinds of ASIA_KIND_FLOORS, serving 5 to 150 g
        if not floor:
            return False
        (low, high), heaviest = floor[1], 150
    label = clean_text(serving_size)
    grams = serving_g if serving_g is not None else (text_weight(label) if label else None)
    if grams is None or not 5 <= grams <= heaviest or 90 <= grams <= 110:
        return False
    factor = 100.0 / grams
    if not low <= row["calories_kcal"] * factor <= high:
        return False
    if any((row[column] or 0) * factor > 100 for column in GRAM_COLUMNS) \
            or ((row["protein_g"] or 0) + (row["carbs_g"] or 0) + (row["fat_g"] or 0)) * factor > 100:
        return False
    for column in ("calories_kcal",) + GRAM_COLUMNS:
        if row[column] is not None:
            row[column] = row[column] * factor
    sodium = row["sodium_mg"]
    row["sodium_mg"] = None if sodium is None or sodium * factor > SERVING_SODIUM_MAX else sodium * factor
    row["basis"] = "estimate"
    amount = tidy_number(round(grams, 2))
    first = label if label and re.search(r"\d", label) and re.search(r"[^\d\s.,]", label) else f"{amount} g"
    row["portions"] = [[first[:60], amount, PIN_MARK]] + [p for p in off_portions(serving_size, serving_g, row["calories_kcal"])
                                                          if p[1] != amount]
    return True


def import_off(importer, path):
    """The Open Food Facts product export: one product per line, roughly 200 columns, numbers per 100 g."""
    count = kept = 0
    found = set()
    for record in csv_records(path, OFF_COLUMNS, found):
        count += 1
        if record is None:
            importer.invalid += 1
            continue
        (code, product_name, product_name_en, brands, countries, image, serving_size, serving_quantity,
         scans, kcal, kj, energy, protein, carbs, sugars, fat, saturates, fibre, sodium, salt,
         completeness, carbs_total, alcohol, categories, generic_name, quantity, image_small) = record
        # The website and its API call the small front photo "image_front_small_url"; the bulk CSV
        # export calls the same photo "image_small_url". Take whichever the file has.
        image = image or image_small

        # Region first: most of the file is sold elsewhere and is skipped right here.
        tags = {tag.strip().lower() for tag in countries.split(",")}
        sold_in = {OFF_COUNTRIES.get(tag) for tag in tags}
        if "AS" in sold_in and barcode_origin(code) == "PH":
            sold_in.discard("AS")       # a Philippine product is never also marked "AS" (see region_text)
        regions = region_text(sold_in)
        if not regions:
            importer.filtered_region += 1
            continue

        row = blank_row("off")

        # Name and brand: decode "&amp;", refuse text that is not a name, drop placeholder brands.
        generic = decode_entities(generic_name)
        row["name"] = usable_name(decode_entities(product_name), decode_entities(product_name_en),
                                  generic if letter_count(generic) >= 4 else "")
        if not row["name"]:
            importer.invalid += 1
            importer.off_dropped["name"] += 1
            continue
        brand = decode_entities(brands.split(",")[0])
        row["brand"] = None if not brand or _PLACEHOLDER_BRAND.fullmatch(brand) else brand
        if adds_search_words(generic, row["name"]):
            row["aka"] = [generic]

        row["category"] = "supermarket"
        row["region_tags"] = regions
        row["barcode"] = normalize_barcode(code)
        row["thumbnail_url"] = http_url(image)
        row["popularity"] = max(0, int(to_number(scans) or 0))
        # Facts used later to choose rows for the app: in how many other countries the product
        # is sold, and how complete Open Food Facts itself rates the record (as a whole percent,
        # so that 0.7874999 and 0.7875 are the same).
        tags.discard("")
        row["foreign_tags"] = sum(1 for tag in tags if tag not in OFF_COUNTRIES and tag not in OFF_NEUTRAL_TAGS)
        complete = to_number(completeness)
        row["completeness"] = None if complete is None else int(math.floor(100 * complete + 0.5))

        # Energy: kcal when given, otherwise from kilojoules.
        kilojoules = to_number(kj)
        row["calories_kcal"] = to_number(kcal)
        if row["calories_kcal"] is None:
            any_kilojoules = kilojoules if kilojoules is not None else to_number(energy)
            if any_kilojoules is not None:
                row["calories_kcal"] = kcal_from_kj(any_kilojoules)
        row["protein_g"], row["sugars_g"] = to_number(protein), to_number(sugars)
        row["fat_g"], row["saturates_g"], row["fibre_g"] = to_number(fat), to_number(saturates), to_number(fibre)
        # Carbs: newer records keep them in a column of their own.
        row["carbs_g"] = to_number(carbs)
        if row["carbs_g"] is None:
            row["carbs_g"] = to_number(carbs_total)
        # Without usable calories nothing below can be checked. The gram figures are checked
        # at the end (apply_rules), after the repairs below have had their chance: macros
        # typed for a whole bottle can be over 100 g and are scaled back to 100 g there.
        kcal_now = row["calories_kcal"]
        if kcal_now is None or kcal_now < 0 or kcal_now > MAX_KCAL_PER_100G:
            importer.invalid += 1
            importer.off_dropped["numbers"] += 1
            continue
        serving_g = to_number(serving_quantity)
        if serving_g is not None and serving_g <= 0:
            serving_g = None
        alcohol_g = to_number(alcohol)

        # Sodium: the file gives grams of sodium and grams of salt; they have to agree.
        row["sodium_mg"] = off_sodium_mg(to_number(sodium), to_number(salt))
        if sodium_typed_in_mg(row["sodium_mg"], serving_g):
            row["sodium_mg"] = None

        # Calories have to fit the row's own protein, carbs and fat. A row that fails is
        # repaired when that is safe and thrown away otherwise.
        conflict = calorie_conflict(row, row["calories_kcal"], serving_g, alcohol_g, categories)
        if conflict:
            if not repair_calories(row, conflict, serving_g, kilojoules, alcohol_g, categories):
                importer.invalid += 1
                importer.off_dropped["calories"] += 1
                continue
            importer.repaired += 1
        # One blank macro is worked out from the calories (the app would show it as 0 g).
        if fill_missing_macro(row, alcohol_g, categories):
            importer.filled += 1
        # Sugars cannot exceed carbs, nor saturates fat.
        reconcile_parts(row)

        row["portions"] = off_portions(serving_size, serving_g, row["calories_kcal"])

        # Labels typed per serving (Philippine rows only). Calories and macros agree, so the
        # checks above pass them, but the numbers are for one serving (or one bottle), not
        # 100 g. Re-based to 100 g with that serving first when its weight can be found,
        # otherwise stored per serving; a food that plainly has calories showing none is
        # dropped. Measured on other markets the same words misjudged one row in five
        # (restaurant items, US biscuits, oven chips), so those rows are left as they are.
        philippine = "PH" in regions.split(",") or barcode_origin(row["barcode"]) == "PH"
        if philippine:
            fixed = serving_typed_fix(row, row["name"], row["brand"], categories, serving_size, serving_g, quantity)
            if fixed:
                importer.serving_typed[fixed] += 1
                if fixed.startswith("drop"):
                    importer.invalid += 1
                    continue

        if not apply_rules(row):
            importer.invalid += 1
            importer.off_dropped["numbers"] += 1
            continue
        # Rows from other markets: patterns that make the numbers doubtful are noted, so the
        # row is not chosen for the app (see pool_doubt). The numbers stay as they are.
        if not philippine:
            # A Japanese, Korean or neighbouring-market snack whose serving explains its too-few
            # calories is re-based on that serving first (see asia_serving_fix).
            if asia_market(row["barcode"], regions) and asia_serving_fix(row, serving_size, serving_g, categories):
                importer.asia_rebased += 1
            row["doubt"] = pool_doubt(row, serving_size, serving_g, categories, alcohol_g) or None
            if row["doubt"]:
                importer.doubts[row["doubt"]] = importer.doubts.get(row["doubt"], 0) + 1
        code = code.strip()
        row["source_ref"] = row["barcode"] or code or "name:" + name_key(row["name"], row["brand"])
        importer.stage(row)
        kept += 1
    importer.read["off"] += count
    missing = {"code", "product_name", "countries_tags"} - found
    if count and missing:
        importer.warn(f"{path}: column(s) {', '.join(sorted(missing))} not found; is this an Open Food Facts export?")
    elif count and "completeness" not in found:
        importer.warn(f"{path}: no 'completeness' column; the app's food list cannot be chosen without it")
    importer.flush()
    importer.say(f"off      {path}: read {count}, accepted {kept}")


# ---------------------------------------------------------------------------------------
# The app's food list
# ---------------------------------------------------------------------------------------

PIN_MARK = "serving"        # third item of a curated row's first portion: "keep this one first"

_MEASURE = re.compile(r"\b(cups?|oz|fl|tbsp|tsp|tablespoons?|teaspoons?|ml|g|grams?|lb|pint|quart|scoop|packet)\b")
_JUST_SERVING = re.compile(r"[\d/. ]*(servings?|portions?)")         # a bare "1 serving" (70 g of pastry) names no unit
_NOT_WHOLE = re.compile(r"slice|piece|wedge|half|section|segment|chunk|cube|ring|container|serving")
_DRINK_GROUP = re.compile(r"beverage|\bcoffee\b|\btea\b|^(other fruit|citrus|apple|vegetable) juice|\bdrinks?\b|\bwater\b|\bwine\b|\bbeer\b|liquor"
                          r"|cocktail|smoothie|milk shake|plant-based milk|flavored milk|^milk\b|alcoholic")
_DRINK_NAME = re.compile(r"^(\w+ )?(milk|soymilk|buttermilk|kefir|eggnog|cocoa)\b|\b(soymilk|soy milk|almond milk|oat milk|rice milk)\b"
                         r"|^[^,]*\b(juice|nectar)\b|drinkable|yogurt drink|yogurt, liquid|smoothie|milkshake")
_NOT_A_DRINK = re.compile(r"\b(dry|dried|powder|powdered|condensed|evaporated)\b")
_MADE_UP = re.compile(r"(?<!not )\b(prepared|reconstituted)\b")             # a powder made up with water or milk is a drink again
_FRUIT_GROUP = re.compile(r"fruit|banana|apple|citrus|peach|pear|pineapple|melon|mango|strawberr|berries|grape|orange")
_NOT_RAW_FRUIT = re.compile(r"dried|juice|canned|cooked|stewed|sauce|frozen|jam|pie|salad|syrup|nectar|puree|"
                            r"dehydrated|candied|pickled|chips|butter|powder|leather|cocktail|drink")
_BAKERY_GROUP = re.compile(r"yeast bread|baked products|rolls and buns|bagel|biscuits|muffin|cookies|cracker|"
                           r"cakes and pies|doughnut|pancake|turnover|\bbread|bakery|tortillas?$")
_DRY_MIX = re.compile(r"\bdry\b|(?<!from )\bmix\b")                # a dry baking mix is not a piece, unless "prepared"
_DISH_GROUP = re.compile(r"sandwich|burger|burrito|taco|egg rolls|dumpling|sushi|pizza")
_UNIT_NAME = re.compile(r"sandwich(?! steak)|burger|hot dog|frankfurter|burrito|taco|taquito|enchilada|tamale|quesadilla|fajita|\bwrap\b|"
                        r"pizza|sushi|dumpling|egg roll|spring roll|empanada|pupusa|gordita|chimichanga|pretzels?, soft|"
                        r"rice cakes?|\bbars?\b|ice cream cone|popsicle|freezer pop|snow cone")
_SWEET_GROUP = re.compile(r"\bbars?\b|candy|candies|sweets")
_SWEET_UNIT = re.compile(r"\b(bars?|pieces?|candy|candies|lollipop|stick|squares?|pouch|patty|tablet)\b")
_ICE_CREAM = re.compile(r"ice cream|frozen dairy")
_ICE_CREAM_UNIT = re.compile(r"cone|\bbars?\b|sandwich|popsicle|\bpop\b|sundae")
_SPOON_LABEL = re.compile(r"\b(tbsp|tablespoons?|tsp|teaspoons?|packets?|pats?|dollop|scoops?|envelopes?|cubes?)\b")
_SPOON_FOOD = re.compile(r"\boils?\b|\bfats?\b|butter|margarine|spread|dressing|mayonnaise|sauce|gravy|ketchup|catsup|mustard|"
                         r"condiment|\bjams?\b|jelly|jellies|preserve|honey|\bsugars?\b|sweetener|syrup|topping|frosting|icing|"
                         r"seasoning|spice|herb|\bsalt\b|vinegar|\bdip|salsa|cream(?! of)|shortening|lard|molasses|marmalade|powders?\b")


def _keeps_serving(category, group, name, label, grams):
    """True when the food's own default portion (label, grams) should stay first (exceptions a to d of the 100 g rule)."""
    group, name = (group or "").lower(), (name or "").lower()
    head = ", ".join(name.split(", ")[:2])        # "carrots, cooked, ..., with salt" -> "carrots, cooked": what the food is
    core = re.sub(r"\([^)]*\)|\bwithout\b.*", "", (label or "").lower())    # "1 slice (1 oz)" -> "1 slice"
    just_serving = bool(_JUST_SERVING.fullmatch((label or "").lower().strip()))
    if category == "dining_out":                                                    # a. eaten out
        return True
    if (_DRINK_GROUP.search(group) or _DRINK_NAME.search(name)) \
            and (not _NOT_A_DRINK.search(name) or _MADE_UP.search(name)):
        return True                                                                 # b. drinks
    if _SPOON_LABEL.search(core) and (_SPOON_FOOD.search(group) or _SPOON_FOOD.search(head) or "powder" in name):
        return True                                                                 # d. by the spoon (or scoop)
    if just_serving and grams <= 40 and _SPOON_FOOD.search(group):
        return True                         # d. Survey's condiment groups call 2 tbsp of salsa "1 serving"
    # c. counted in units: the default portion must be the unit, not a cup, a weight or just "1 serving".
    counted = not _MEASURE.search(core) and not just_serving
    if (re.search(r"\beggs?\b|omelet", name) or "eggs and omelets" in group) \
            and re.search(r"\b(egg|omelet|omelette|large|medium|small|jumbo)\b", core):
        return True
    if _FRUIT_GROUP.search(group) and not _NOT_RAW_FRUIT.search(name):
        return counted and not _NOT_WHOLE.search(core)
    if _SWEET_GROUP.search(group):
        return bool(_SWEET_UNIT.search(core))
    if _ICE_CREAM.search(group):
        return bool(_ICE_CREAM_UNIT.search(core))
    if _BAKERY_GROUP.search(group) or _DISH_GROUP.search(group) or _UNIT_NAME.search(head):
        return counted and not (_DRY_MIX.search(name) and not _MADE_UP.search(name))
    return False


def default_portions(item, portions):
    """The portions as the app should list them: 100 g first, unless the food is counted another way.

    item: a food_items row; portions: its stored list. The first portion is the one the app
    shows calories for. Rows without a gram weight (per serving), without portions, or whose
    curated entry pinned its serving ("default_portion") keep their order; see _keeps_serving
    for the foods that keep their own portion. The database is not changed.
    """
    if item["per_unit"] == "serving" or not portions or not portions[0][1]:
        return portions
    if len(portions[0]) > 2:                                    # pinned by default_portion: "serving"
        return [p[:2] for p in portions]
    if _keeps_serving(item["category"], item["group_label"], item["name"], *portions[0]):
        return portions
    if item["source"] == "off" and _off_drink(item["name"], portions[0][0]):
        return portions                                         # b. drinks, for Open Food Facts rows
    if item["source"] == "off" and not _philippine(item) \
            and _off_keeps_serving(item["name"], _field(item, "brand"), *portions[0][:2]):
        return portions                                         # b to d, read from a product label
    return [["100 g", 100]] + [p for p in portions if p[1] != 100]


def _field(item, column):
    """item[column], or None when the row (a database row or a dictionary) has no such column."""
    try:
        return item[column]
    except (KeyError, IndexError):
        return None


def _philippine(item):
    """A row of the Philippine part of the food list (tagged PH, or a 480 barcode)."""
    return (_field(item, "region_tags") or "").startswith("PH") or barcode_origin(_field(item, "barcode")) == "PH"


# Open Food Facts rows from other markets: their serving label (rather than a food group)
# often says the food is a drink, a unit or a spoonful, in words _keeps_serving does not
# read: "1 can (355 g)", "1 bar (22 g)", a peanut butter's "1 serving (32 g)". Such a row
# opens on its own serving (measured on the US rows: 1 988 of 7 070 that opened on 100 g).
#   drink  no food word in the name, and the label names a can, bottle or carton of 150 g or
#          more, or the name says it is a drink and the serving is 150 to 1 000 g
#   unit   the label (brackets aside) is "1 bar", "2 cookies", "1 slice", "1 pie" ..., 700 g
#          or less, or any count from a half to 60 of something that is no measure
#          ("2 pastries", "8 crackers", "55 pieces", "3 skillet-cooked links"; not cups,
#          ounces, spoons or servings), 300 g or less. Chips, crisps, puffs and nuts are
#          poured, not counted: "13 chips" or "32 pieces" of them is no unit, "1 bag" is.
#          A share of a pack under a half ("1/5 package", "1/3 bar") is no unit either. A
#          label that is only a weight is read from its brackets ("19 g (1 tub)"). A bar
#          named as one (not a bar of chocolate) opens on a serving of 20 to 100 g even
#          when the label names no unit ("35g", "1 serving (68 g)").
#   spoon  40 g or less of a food whose name ends with a spoon food (peanut butter, mayo,
#          sauce, oil, honey ...) or whose label says tbsp or tsp; never a snack or a bar
_OFF_DRINK_WORDS = re.compile(
    r"\b(?:soda|cola|coke|pepsi|sprite|lemonade|kombucha|seltzer|sparkling|tonic|energy drink|sports drink|electrolyte"
    r"|gatorade|powerade|beer|ipa|lager|ale|cider|wine|juice|nectar|smoothie|shake|milk|almondmilk|oatmilk|soymilk"
    r"|kefir|coffee drink|cold brew|latte|iced tea|tea(?! bags?)|water|broth|drink|beverage)\b", re.I)
_OFF_NOT_DRINK = re.compile(
    r"\b(?:chocolate|choc|cookies?|bars?|cereal|crackers?|candy|powder|powdered|mix|condensed|evaporated|cheese|bread"
    r"|biscuits?|cake|ice cream|frozen|gummies|fruit snacks|crisps|chips|in water|in spring water|tuna|chicken|beans"
    r"|beanz|sauce|soup|oats|hash)\b", re.I)
_OFF_NUMBER = r"(?P<count>\d{0,2}\.\d+|\d{1,2}|\d/\d|one|two|three|half)"
_OFF_COUNT = re.compile(r"^\s*(?:about\s+)?" + _OFF_NUMBER + r"\s+(?P<what>[a-z][a-z' -]*?)\s*$", re.I)
_OFF_NOT_COUNTED = re.compile(
    r"\b(?:cups?|c|oz|oun\w+|onz|oza|fl|tbsps?|tbs|tsps?|tablespoons?|teaspoons?|table ?spoons?|ml|cl|l|g|gr|grams?|kg"
    r"|lbs?|pints?|quarts?|liters?|litres?|servings?|portions?|quantit(?:y|ies)|of|inch(?:es)?|cm|sec|seconds?|sprays?"
    r"|about|approx|chips?|crisps?|nuts?|almonds?|peanuts?|cashews?|pistachios?|pecans?|walnuts?|seeds|kernels|fries"
    r"|grapes|berries|raisins)\b", re.I)
_OFF_BARE_WEIGHT = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:g|gr|grams?|ml)?\s*$", re.I)
_OFF_BAR_NAME = re.compile(r"\bbars?\b", re.I)
_OFF_NOT_A_BAR = re.compile(r"chocolate bars?\b|candy bars?\b|baking|\bsoap\b|\bbars? mix\b|\bbar[- ]?s\b|\bbar (?:cake|cheese|soap)", re.I)
# Nuts, chips and puffs are poured, not counted, even where the label counts "32 pieces"
# (four or more: "1 bag" or "1 bar" of them is still a unit).
_OFF_POURED_NAME = re.compile(r"\b(?:nuts|almonds|peanuts|cashews|pistachios|pecans|walnuts|popcorn|trail mix|seeds"
                              r"|chips(?! ahoy)|crisps|puffs|straws)\b", re.I)
_OFF_UNIT = re.compile(
    r"^\s*(?:about\s+)?" + _OFF_NUMBER + r"?\s*(?:large\s+|small\s+|mini\s+|grilled\s+|whole\s+)?"
    r"(?:bars?|cookies?|biscuits?|cakes?|cupcakes?|brownies?|buns?|bagels?|muffins?|croissants?|donuts?|doughnuts?"
    r"|tortillas?|wraps?|pies?|bowls?|trays?|containers?|pots?|pouch(?:es)?|packets?|sachets?|sticks?|envelopes?"
    r"|franks?|links?|sausages?|patt(?:y|ies)|burgers?|eggs?|packs?|packages?|bags?|pieces?|pcs|fillets?|slices?"
    r"|waffles?|pancakes?|pizzas?|meals?|entrees?|sandwich(?:es)?|burritos?|tubs?|cans?|bottles?|tea ?bags?"
    r"|teabags?|scoops?|pods?|cones?|squares?|nuggets?)\b", re.I)
_OFF_SPOON_END = re.compile(
    r"\b(?:peanut butter|almond butter|nut butter|cookie butter|sunflower butter|butter|margarine|spread|mayo|mayonnaise"
    r"|aioli|sauce|salsa|dressing|vinaigrette|ketchup|catsup|mustard|relish|oil|syrup|honey|jam|jelly|preserves"
    r"|marmalade|seasoning|rub|salt|sugar|sweetener|creamer|dip|hummus|vinegar|pesto|gravy|bouillon|base|paste"
    r"|gochujang|sriracha|chili crisp|tahini|nutella|frosting|icing|whipped topping|cream cheese|sour cream)s?$", re.I)
_OFF_SPOON_FILLER = re.compile(
    r"\b(?:imp|original|classic|organic|natural|creamy|crunchy|smooth|chunky|mild|medium|hot|spicy|light|reduced fat"
    r"|zero sugar|sugar free)\b|\d+(?:\.\d+)?\s*(?:g|ml|oz|fl oz)\b")
# Read in the name. A brand is only read for the two that sell nothing but snacks.
_OFF_SNACK = re.compile(r"\b(?:chips|crisps|crackers?|peanuts|almonds|cashews|pistachios|nuts|pringles|popcorn|pops?|bars?"
                        r"|chocolate|dipped|cracklings|pretzels?|cookies?|cereal|granola|jerky|kind|snacks?)\b")
_OFF_SNACK_BRAND = re.compile(r"\b(?:kind|pringles)\b")
_OFF_CHOCOLATE_SPOON = re.compile(r"\bchocolate\b.*\b(?:spread|syrup|sauce|frosting|icing|dip|butter|topping)s?$")


_LOST_SLASH = {"12": "1/2", "13": "1/3", "14": "1/4", "23": "2/3", "34": "3/4", "18": "1/8"}
_LOST_SLASH_LIGHT = {"cup": 20, "tbsp": 5, "tsp": 2}    # fewer grams per cup or spoon than this is no real count
_LABEL_FIXES = (
    (re.compile(r"\bONZ\b"), "oz"), (re.compile(r"\bOZA\b"), "fl oz"),          # US label codes
    (re.compile(r"\bthsp\b", re.I), "tbsp"),
    (re.compile(r"^(\d+(?:\.\d+)?) z \("), r"\1 oz ("),                           # "1.5 z (40 g)"
    # Open Food Facts writes a label's "about" after a bar sign: "13 CHIPS | ABOUT (28 g)",
    # "28 g (13 CHIPS | ABOUT)", "15.0 g (15 g | g)", "3 PRETZELS) | (ABOUT (30 g)".
    (re.compile(r"\)\s*\|\s*\("), " | "),
    (re.compile(r"\s*\|\s*(?:about|approx\.?|approximately|per|g|ml)?\s*(?=[()]|$)", re.I), " "),
    (re.compile(r"\s*\|\s*"), ", "),
    (re.compile(r"\(\s*\)"), ""), (re.compile(r"\s+\)"), ")"), (re.compile(r"\s{2,}"), " "),
    (re.compile(r"^1 portion\b", re.I), "1 serving"),
)
_DECIMALS = re.compile(r"(\d+\.(?:\d{2,}|0))(?=\s*(?:g|ml)\b)", re.I)
_REPEATED_WEIGHT = re.compile(r"(\(\d+(?:\.\d+)?\s*(?:g|ml)\))\s*\1", re.I)
_SAME_WEIGHT_TWICE = re.compile(r"^(\d+(?:\.\d+)?)\s*(g|ml)\s*\(\1\s*\2\)$", re.I)
_LEADING_DECIMAL = re.compile(r"^(0?\.\d+)(?= [A-Za-z])")
_PLAIN_FRACTIONS = ((1 / 2, "1/2"), (1 / 3, "1/3"), (2 / 3, "2/3"), (1 / 4, "1/4"), (3 / 4, "3/4"), (1 / 8, "1/8"),
                    (1 / 6, "1/6"), (1 / 5, "1/5"))


def _short_decimal(found):
    """"236.58799999999997" -> "236.6", "56.699" -> "56.7", "15.0" -> "15"; under 10 two decimals stay ("1.25")."""
    value = float(found.group(1))
    return str(tidy_number(round(value, 1 if value >= 10 else 2)))


def _balanced(text):
    """text without the brackets that have no partner ("3 pretzels) (30 g)" -> "3 pretzels (30 g)")."""
    kept, opened = [], []
    for letter in text:
        if letter == "(":
            opened.append(len(kept))
        elif letter == ")":
            if not opened:
                continue
            opened.pop()
        kept.append(letter)
    for position in reversed(opened):
        del kept[position]
    return "".join(kept)


def tidy_label(label, grams=None):
    """A product's serving label as a person would write it; the weight in it is not changed.

    "2 ONZ (56 g)" -> "2 oz (56 g)", "5 crackers (16 g) (16 g)" -> "5 crackers (16 g)",
    "236.58799999999997g" -> "236.6g", "1 thsp" -> "1 tbsp", "4 SLICES (56 g)" ->
    "4 slices (56 g)", "1 portion (30 g)" -> "1 serving (30 g)", "13 CHIPS | ABOUT (28 g)" ->
    "13 chips (28 g)", "15.0 g (15 g | g)" -> "15 g", "0.667 cup (170 g)" -> "2/3 cup (170 g)".
    "23 cup (68 g)" -> "2/3 cup (68 g)" when grams (the portion's weight) is given and is
    too little for that many cups or spoons: "12 cups (2880 g)" is twelve cups.
    """
    text = str(label)
    for pattern, replacement in _LABEL_FIXES:
        text = pattern.sub(replacement, text)
    text = _balanced(text)
    text = _REPEATED_WEIGHT.sub(r"\1", text)
    text = _DECIMALS.sub(_short_decimal, text).strip()
    text = _SAME_WEIGHT_TWICE.sub(r"\1 \2", text)
    lost = re.match(r"^(12|13|14|23|34|18) (cup|tsp|tbsp)s?\b", text, re.I)
    if lost and not grams:
        stated = re.search(r"\((\d+(?:\.\d+)?)\s*(?:g|ml)\)", text, re.I)
        grams = float(stated.group(1)) if stated else None
    if lost and grams and grams / int(lost.group(1)) < _LOST_SLASH_LIGHT[lost.group(2).lower()]:
        text = _LOST_SLASH[lost.group(1)] + text[2:]
    part = _LEADING_DECIMAL.match(text)
    if part:
        for value, written in _PLAIN_FRACTIONS:
            if abs(float(part.group(1)) - value) < 0.004:
                text = written + text[part.end():]
                break
    # Capitals are lowered where a whole part is in capitals: the words outside the brackets
    # ("8 FL OZ", "4 SLICES") or the inside of a bracket ("(13 CHIPS)"); "BBQ sauce" stays.
    words = re.sub(r"\([^)]*\)", "", text)
    parts = re.split(r"(\([^)]*\))", text)
    for index, part in enumerate(parts):
        whole = words if not part.startswith("(") else part
        if re.search(r"[A-Z]{2}", whole) and not re.search(r"[a-z]", whole):
            parts[index] = re.sub(r"[A-Z]{2,}", lambda found: found.group(0).lower(), part)
    return "".join(parts).strip() or str(label)


def _off_counted(text):
    """How many units or pieces text counts ("1 bar" 1, "8 crackers" 8, "1/2 pizza" 0.5) and the most such a
    serving may weigh; (0, 0) for a measure ("2 cups"), for a share of a pack ("1/5 package", "0.11 box":
    under a half is no unit) and for more than 60 pieces."""
    found, most = _OFF_UNIT.match(text), 700
    if not found:
        found, most = _OFF_COUNT.match(text), 300           # a unit this rule has no word for: "1 jar", "2 tranches"
        if not found or _OFF_NOT_COUNTED.search(found.group("what")):
            return 0, 0
    number = (found.group("count") or "1").lower()
    if "/" in number:
        count = int(number[0]) / int(number[2]) if number[2] != "0" else 0
    else:
        count = {"one": 1, "two": 2, "three": 3, "half": 0.5}.get(number) or float(number)
    return (count, most) if 0.5 <= count <= 60 else (0, 0)


def _off_keeps_serving(name, brand, label, grams):
    """True when an Open Food Facts row from another market should open on its own serving (see above)."""
    name, label = name or "", tidy_label(label or "", grams)
    if not _OFF_NOT_DRINK.search(name):
        if re.search(r"\b(?:can|bottle|carton)\b", label, re.I) and 150 <= grams <= 1000:
            return True
        if _OFF_DRINK_WORDS.search(name) and 150 <= grams <= 1000:
            return True
    core = re.sub(r"\([^)]*\)", "", label)
    inside = re.search(r"\(([^)]*)\)", label)
    if _OFF_BARE_WEIGHT.match(core) and inside:             # "19 g (1 tub)": the unit is in the brackets
        core = inside.group(1)
    count, most = _off_counted(core)
    if count and grams <= most and not (count > 3 and _OFF_POURED_NAME.search(name)):
        return True
    named = name + " " + (brand or "")
    if 20 <= grams <= 100 and _OFF_BAR_NAME.search(named) and not _OFF_NOT_A_BAR.search(named) \
            and (_OFF_BARE_WEIGHT.match(core) or _JUST_SERVING.fullmatch(core.lower().strip())):
        return True
    low = name.lower()
    head = " ".join(re.sub(r"[^a-z ]", " ", _OFF_SPOON_FILLER.sub(" ", low)).split())
    snack = (_OFF_SNACK.search(low) and not _OFF_CHOCOLATE_SPOON.search(head)) or _OFF_SNACK_BRAND.search((brand or "").lower())
    if grams <= 40 and not snack:
        if _OFF_SPOON_END.search(head) or (_SPOON_LABEL.search(label.lower()) and _SPOON_FOOD.search(head)):
            return True
    return False


# Open Food Facts rows have no food group, which is where _keeps_serving recognises most
# drinks. For them a drink is also recognised by its serving ("1 can (330 ml)", "8 fl oz")
# or by a drink word in the name ("Iced Tea", "Coconut Water"). The unit and spoon rules of
# _keeps_serving already work on the name alone.
_LIQUID_SERVING = re.compile(r"\d\s*(?:ml|cl|l|fl\.?\s*oz)\b", re.I)


def _off_drink(name, label):
    name = (name or "").lower()
    if _NOT_A_DRINK.search(name) and not _MADE_UP.search(name):
        return False                                            # "milk powder", "dried mango juice"
    return bool(_LIQUID_SERVING.search(label or "") or _DRINK_GROUP.search(name))


# --- Rows of the Japan, Korea and Asia pools as the app shows them ---
# Their brands, names and serving labels are often in Japanese, Korean or Thai, or carry a
# company's legal name, which a phone with an English keyboard cannot type. The export (never
# the database) tidies them for these three pools only; Philippine, US and UK rows are written
# as stored.
_NATIVE = re.compile("[々〆฀-๿぀-ヿ㐀-鿿가-힯ｦ-ﾟ]")
# The Latin names of brands that are often written in Japanese, Korean, Thai or Chinese script
# (read in the brand: a brand that contains the left text is written as the right text; the
# longest left text is tried first). A Japanese or Chinese left text must stand by itself in the
# brand, or be followed only by a company word (食品, 製菓, 乳業 ...): 明治 is Meiji but 明治屋 is
# another firm, 日清食品 is Nissin (noodles) but 日清フーズ and 日清製粉 are the Nisshin flour group,
# and イオン inside ライオン is no Aeon.
ASIA_BRANDS = (
    ("神戸物産", "Kobe Bussan"), ("富澤商店", "Tomizawa"), ("伊藤園", "Ito En"), ("雪印メグミルク", "Snow Brand Megmilk"),
    ("セブンイレブン", "7-Eleven"), ("セブン-イレブン", "7-Eleven"), ("セブンプレミアム", "7-Eleven"), ("ブルボン", "Bourbon"),
    ("ファミリーマート", "FamilyMart"), ("ファミマル", "FamilyMart"), ("ファミマ", "FamilyMart"), ("キッコーマン", "Kikkoman"),
    ("ハウス食品", "House Foods"), ("カルビー", "Calbee"), ("ロッテ", "Lotte"), ("シマダヤ", "Shimadaya"), ("日清", "Nissin"),
    ("日清シスコ", "Nissin Cisco"), ("日清ヨーク", "Nissin York"), ("日清フーズ", "Nisshin Foods"),
    ("日清製粉ウェルナ", "Nisshin Seifun Welna"), ("日清製粉", "Nisshin Seifun"), ("日清オイリオ", "Nisshin Oillio"),
    ("明治屋", "Meidi-ya"), ("壱番屋", "CoCo Ichibanya"), ("アルプロン", "Alpron"), ("ライオン菓子", "Lion"),
    ("บอส", "Boss"), ("ไวตามิ้ลค์", "Vitamilk"), ("ริทซ์", "Ritz"), ("ヤマザキビスケット", "Yamazaki Biscuits"),
    ("日清焼きそば", "Nissin"),
    ("森永", "Morinaga"), ("明治", "Meiji"), ("味の素", "Ajinomoto"), ("エスビー食品", "S&B"), ("西友", "Seiyu"),
    ("エバラ", "Ebara"), ("グリコ", "Glico"), ("サントリー", "Suntory"), ("アサヒ", "Asahi"), ("キリン", "Kirin"),
    ("無印良品", "Muji"), ("トップバリュ", "Topvalu"), ("イオン", "Aeon"), ("ローソン", "Lawson"), ("山崎製パン", "Yamazaki"),
    ("フジパン", "Fujipan"), ("不二家", "Fujiya"), ("亀田製菓", "Kameda"), ("ミツカン", "Mizkan"), ("キユーピー", "Kewpie"),
    ("日本ハム", "Nippon Ham"), ("伊藤ハム", "Itoham"), ("プリマハム", "Prima Ham"), ("サラヤ", "Saraya"),
    ("中沢乳業", "Nakazawa"), ("九鬼産業", "Kuki Sangyo"), ("丸美屋", "Marumiya"), ("三島", "Mishima"), ("やおきん", "Yaokin"),
    ("ヤマザキ", "Yamazaki"), ("丸大食品", "Marudai"), ("ニチレイ", "Nichirei"), ("永谷園", "Nagatanien"), ("湖池屋", "Koikeya"),
    ("빙그레", "Binggrae"), ("서울우유", "Seoul Milk"), ("오리온", "Orion"), ("롯데햄", "Lotte Ham"), ("롯데", "Lotte"),
    ("농심", "Nongshim"), ("매일", "Maeil"), ("남양", "Namyang"), ("동원", "Dongwon"), ("제일제당", "CJ CheilJedang"),
    ("오뚜기", "Ottogi"), ("풀무원", "Pulmuone"), ("삼양", "Samyang"), ("팔도", "Paldo"), ("해태", "Haitai"), ("크라운", "Crown"),
    ("청정원", "Chung Jung One"), ("비비고", "Bibigo"), ("담터", "Damteo"), ("오즈키친", "Oz Kitchen"), ("샘표", "Sempio"),
    ("เคลล็อกส์", "Kellogg's"), ("เนสกาแฟ", "Nescafe"), ("มาม่า", "Mama"), ("เถ้าแก่น้อย", "Tao Kae Noi"), ("เมจิ", "Meiji"),
    ("กูลิโกะ", "Glico"), ("ทองการ์เด้น", "Tong Garden"), ("เป๊ปซี่", "Pepsi"), ("เกเตอเรด", "Gatorade"), ("โออิชิ", "Oishi"),
    ("ネスレ", "Nestlé"), ("ดัชมิลล์", "Dutch Mill"), ("ซีพี", "CP"), ("햇반", "CJ Hetbahn"), ("백설", "CJ Beksul"),
    ("大塚製薬", "Otsuka"), ("ベストプライス", "Topvalu Best Price"), ("チェリオ", "Cheerio"), ("アルプロ", "Alpro"),
    ("เด็กสมบูรณ์", "Healthy Boy"), ("ทาโร่", "Taro"), ("神戸屋", "Kobeya"), ("可口可樂", "Coca-Cola"), ("維他奶", "Vitasoy"), ("李錦記", "Lee Kum Kee"), ("康師傅", "Master Kong"), ("統一", "Uni-President"),
)
_ASIA_BRANDS_LONGEST = tuple(sorted(ASIA_BRANDS, key=lambda pair: -len(pair[0])))
_KANA_KANJI = re.compile("[々〆぀-ヿ㐀-鿿ｦ-ﾟ]")
_BRAND_SUFFIX = re.compile(r"(?:食品|製菓|乳業|飲料|ビール|ビバレッジ|フーズ|グループ|本社|ホールディングス|製パン|商事|日本|ジャパン|スーパー"
                           r"|株式会社|\(株\)|（株）)*")
# A company's legal form, wherever it stands, and a lone last word that says "company".
_COMPANY_WORDS = re.compile(
    r"株式会社|有限会社|[(（]\s*[株有]\s*[)）]|\bpublic company limited\b|\bcompany limited\b|\bco\.?\s*[,-]?\s*ltd\.?"
    r"|\bpte\.?\s*ltd\.?|\bsdn\.?\s*bhd\.?|\btbk\.?$", re.I)
_COMPANY_TAIL = re.compile(r"(?<!&)[\s,]+(?:ltd|limited|inc|corporation|corp|co)\.?$", re.I)
_BRAND_STOP = frozenset(("the", "a", "an", "co", "new", "mr", "my", "by", "no", "of", "ltd", "inc"))
# One word that is no brand without the "Co." or "Corporation" after it ("Tomato Corporation", "Trading Co.").
_BRAND_GENERIC = frozenset(("tomato", "trading", "food", "foods", "natural", "organic", "fresh", "farm", "farms", "tea"))
# 7-Eleven and its own label (Seven & i, Seven Premium), with or without a country after it.
_SEVEN_ELEVEN = re.compile(r"^(?:7[- ]?11|7/11|7 ?-? ?eleven|seven ?-? ?eleven|seven ?& ?i|7 ?& ?i|セブン|7(?=[- ]premium)"
                           r"|seven(?= premium))(?:[- ]?(?:japan|thailand|singapore|malaysia|hong kong|taiwan|jp|th|sg"
                           r"|my|hk|tw|premium))*$", re.I)


def _table_brand(text):
    """The Latin name of a brand written in Japanese, Korean, Thai or Chinese (see ASIA_BRANDS), or None."""
    for native, latin in _ASIA_BRANDS_LONGEST:
        at = text.find(native)
        while at >= 0:
            if not _KANA_KANJI.search(native):
                return latin                            # Korean and Thai run their words together
            before = text[at - 1] if at else " "
            rest = text[at + len(native):]
            rest = rest[_BRAND_SUFFIX.match(rest).end():]
            if not _NATIVE.match(before) and not (rest and _NATIVE.match(rest[0])):
                return latin
            at = text.find(native, at + 1)
    return None


def asia_brand(brand):
    """A brand of the Japan, Korea or Asia pools as a person would type it (None stays None).

    "株式会社 神戸物産" -> "Kobe Bussan", "Shimadaya シマダヤ" -> "Shimadaya", "Lotte Chilsung
    Beverage Co. Ltd." -> "Lotte Chilsung Beverage", "7-11" or "7/11" -> "7-Eleven". A brand left
    with no Latin letters, or with nothing but a word like "The" ("The 미식"), keeps its own
    spelling; a last "Co." or "Corporation" stays where it is the brand ("Tomato Corporation",
    "Trading Co.", "Peanut Butter & Co.").
    """
    if not brand:
        return brand
    whole = str(brand).strip()
    found = _table_brand(whole)
    if found:
        return found
    text = whole
    if _NATIVE.search(text) and re.search("[A-Za-z]{3}", text):
        text = _NATIVE.sub(" ", text)
        text = re.sub(r"[•・/|()（）\[\]「」【】]+", " ", text)
    text = _COMPANY_WORDS.sub(" ", text)
    text = " ".join(text.replace(" ,", ",").split()).strip(" ,-")
    if text.startswith("&") and re.search("[A-Za-z]", text):
        text = text.lstrip("& ")                    # "& You Konnyaku" -> "You Konnyaku"
    if text.endswith(" &"):
        text = text[:-2].rstrip()
    shorter = _COMPANY_TAIL.sub("", text).strip(" ,.-")
    if shorter != text and (len(shorter.split()) >= 2 or shorter.lower() not in _BRAND_GENERIC):
        text = shorter
    if not _COMPANY_TAIL.search(text):
        text = text.strip(" ,.-")
    if _SEVEN_ELEVEN.match(text):
        return "7-Eleven"
    if not text or (text != whole and text.lower() in _BRAND_STOP):
        return whole
    return text


_STORE_CODE = re.compile(r"^1\d{7}\s+")                                        # "12001187 Vida Salty Lychee"
_SIZE = r"\d+(?:[.,]\d+)?\s?(?:g|gr|gm|grs?|ml|l|kg)\.?"
_PACK_SIZE = re.compile(r"(?<![Ff]or)[\s,-]+(?:\(" + _SIZE + r"\)|" + _SIZE + r")$", re.I)             # "Kraft Cheddar 160g"
# "(250 mL x 6)", "(8 x 13.3 g)", "5 x 80ml", "1X60G", "(9 Packs)"
_MULTIPACK = re.compile(r"\s*\(\s*" + _SIZE + r"\s*[x×]\s*\d+\s*\)$|\s*\(\s*\d+\s*[x×]\s*" + _SIZE + r"\s*\)$"
                        r"|[\s,-]+\d+\s*[x×]\s*" + _SIZE + r"$|\s*\(\s*\d+\s*(?:packs?|pcs|pieces|sachets|sticks|bags)\s*\)$", re.I)
_IMPORTED = re.compile(r"\s+imp\.?$", re.I)                                      # "Tostitos ... chips imp"
_NAME_NOTE = re.compile(r"\s*\[only one image\]|\s*内容量\s*[:：]?\s*" + _SIZE + r"$", re.I)
_SIZE_WORD = re.compile(r"(?<![Kk]ing)(?<![Ff]amily)(?<![Pp]arty)(?<![Ff]un)(?<![Bb]ite)(?<![Ss]nack)(?<![Ss]hare)(?<![Ss]haring)"
                        r"(?<![Bb]ig)(?<![Mm]ega)(?<![Jj]umbo)(?<![Vv]alue)(?<![Tt]ravel)(?<![Rr]egular)(?<![Ll]arge)"
                        r"(?<![Ss]mall)(?<![Mm]ini)(?<![Ss]ingle)\s+size$", re.I)                                      # "... Dipping Sauce Size 200 ML."


def asia_name(name, brand=None):
    """A product name of the Japan, Korea or Asia pools without a shop's item code in front or the
    pack size and "imp" (imported) at the end: "17001422 Mini Oreo original 8s 163.20 gm" ->
    "Mini Oreo original 8s", "Kraft Cheddar 160g" -> "Kraft Cheddar", "Cookies - 106 g (8 x 13.3 g)"
    -> "Cookies". A " - Brand" tail said twice, or one that repeats the row's brand or the name's
    own first words, is said once or not at all: "Udon - Nissin - Nissin - Nissin" by no brand ->
    "Udon - Nissin", "Nissin Udon - Nissin - Nissin" -> "Nissin Udon". A name that would keep
    fewer than three plain letters in a row is left as it is; one with "3 in 1" or a % keeps its sizes."""
    text = str(name)
    new = _NAME_NOTE.sub("", _STORE_CODE.sub("", text))
    if not re.search(r"\d\s*in\s*1|%", text, re.I):
        for _ in range(3):
            before = new
            new = _IMPORTED.sub("", _MULTIPACK.sub("", new))
            sized = _PACK_SIZE.sub("", new)
            new = _SIZE_WORD.sub("", sized) if sized != new else new
            if new == before:
                break
    parts = new.split(" - ")
    while len(parts) > 1:
        last, head = parts[-1].strip().lower(), " - ".join(parts[:-1]).lower()
        if last and (last == parts[-2].strip().lower() or last == (brand or "").strip().lower()
                     or re.match(re.escape(last) + r"(?![a-z])", head)):
            parts.pop()
        else:
            break
    new = " - ".join(parts).strip(" ,")
    new = re.sub(r"\s+-$", "", re.sub(r"^-\s+", "", new))
    return new if _TYPEABLE.search(new) else text


# Serving words of Japanese, Korean, Thai and Chinese labels, longest first, and the English
# word for one of them. 本 is a bottle when the serving is liquid or weighs 100 g or more,
# otherwise a stick.
_ASIA_UNITS = (
    ("個包装", "pack"), ("食分", "serving"), ("人前", "serving"), ("杯分", "cup"), ("カップ", "cup"), ("コップ", "cup"),
    ("パック", "pack"), ("キャン", "can"), ("切れ", "slice"), ("製品", "pack"), ("袋", "pack"), ("個", "piece"),
    ("枚", "piece"), ("箱", "box"), ("粒", "piece"), ("包", "pack"), ("杯", "cup"), ("缶", "can"), ("食", "serving"),
    ("本", None), ("봉지", "pack"), ("인분", "serving"), ("개", "piece"), ("봉", "pack"), ("컵", "cup"), ("팩", "pack"),
    ("병", "bottle"), ("캔", "can"), ("ช้อนโต๊ะ", "tbsp"), ("ช้อนชา", "tsp"), ("ซอง", "sachet"), ("ชิ้น", "piece"),
    ("กรัม", "g"), ("มล.", "ml"), ("มล", "ml"), ("克", "g"),
)
_PLURAL = {"pack": "packs", "piece": "pieces", "box": "boxes", "slice": "slices", "cup": "cups", "can": "cans",
           "bottle": "bottles", "stick": "sticks", "sachet": "sachets", "serving": "servings"}


_WEIGHT = r"\d+(?:\.\d+)?\s*(?:g|ml)"
_LABEL_JUNK = re.compile(r"^\d+(?:\.\d+)?\s*kcal\s*\((" + _WEIGHT + r")\)$|^1\s+(" + _WEIGHT + r")\.?(?:\s*\(" + _WEIGHT + r"\))?$"
                         r"|^\d+\s+\d+\s*\((" + _WEIGHT + r")\)$|^\d{3,}\s*oz\s*\((" + _WEIGHT + r")\)$"
                         r"|^1\s+l\s*\((\d+(?:\.\d+)?\s*g)\)$", re.I)


def asia_label(label, grams=None):
    """A serving label of the Japan, Korea or Asia pools in plain English where the meaning is certain.

    "1袋(80g)" -> "1 pack (80 g)", "14粒(21g)" -> "14 pieces (21 g)", "1本(110ml)" -> "1 bottle
    (110 ml)", "1 ซอง (80 g)" -> "1 sachet (80 g)", "1.0 portion (30 g)" -> "1 serving (30 g)",
    "1 barre (75 ml)" -> "1 bar (75 ml)", "8 gm (8 g)" -> "8 g (8 g)". A label that still has
    Japanese, Korean or Thai letters after the table ("1 食を温める場合は約 (150 g)") is left as it is.
    """
    text = str(label)
    junk = _LABEL_JUNK.match(text)
    if junk:                                # "386 kcal (85 g)", "1 2000 ml (2000 ml)", "1 8 (180 ml)", "1060 oz (30 g)"
        text = next(part for part in junk.groups() if part)
    text = re.sub(r"^\d+\.\d+\s+(?=\d+\s+[A-Za-z])", "", text)                # "6.52 1 Can (185 g)" -> "1 Can (185 g)"
    text = re.sub(r"^1\s+(?=\d+(?:\.\d+)?\s*[A-Za-z])", "", text)              # "1 1 piece (28 g)" -> "1 piece (28 g)"
    if _NATIVE.search(text):
        text = re.sub(r"当たり|あたり|約", " ", text)
        for native, english in _ASIA_UNITS:
            if native not in text:
                continue
            if english is None:
                english = "bottle" if re.search(r"ml|ミリ", text, re.I) or (grams or 0) >= 100 else "stick"
            text = text.replace(native, " " + english + " ")
        text = re.sub(r"\((?:bag|pack|piece|bottle|can|cup|box|stick)s?\)", " ", text, flags=re.I)
        if _NATIVE.search(text):
            return str(label)
    text = re.sub(r"(\d+)\.0+(?=\s|$|\))", r"\1", text)                             # "12.0" -> "12"
    text = re.sub(r"\bportions?\b", "serving", text, flags=re.I)
    text = re.sub(r"\bbarre\b", "bar", text, flags=re.I)
    text = re.sub(r"(\d)\s*gms?\b", r"\1 g", text, flags=re.I)
    text = re.sub(r"\s*\(\s*", " (", text)
    text = re.sub(r"\s*\)", ")", text)
    text = re.sub(r"(\d)\s*(g|ml)\b", r"\1 \2", text)
    text = " ".join(text.split())
    count = re.match(r"^(\d+(?:\.\d+)?)\s+([a-z]+)\b", text)
    if count and float(count.group(1)) > 1 and count.group(2) in _PLURAL:
        text = count.group(1) + " " + _PLURAL[count.group(2)] + text[count.end():]
    return text.strip() or str(label)


# Single-serve packs that open on their own serving in these pools rather than on 100 g (the
# app still offers 100 g). Read in the name; the serving must be one of the row's portions.
#   noodles  an instant-noodle pack or cup of 250 kcal or more per 100 g (dry), serving 40 to 200 g
#   sachet   a 3-in-1 coffee, tea, Milo or Ovaltine stick or sachet of 250 kcal or more, 3 to 45 g
#   cup      a yogurt, Yakult or pudding under 200 kcal, serving 40 to 250 g
_ASIA_NOODLE_PACK = re.compile(r"instant noodle|cup noodle|\bramen\b|\bramyun\b|\bramyeon\b|mi goreng|yakisoba|\budon\b"
                               r"|\bsoba\b|buldak|chapaghetti|chapagetti|\bindomie\b|\bmaggi\b|\bnoodles?\b", re.I)
_ASIA_SACHET = re.compile(r"\b[23] ?in ?1\b|\b[23]in1\b|\bsticks?\b|sachet|coffee mix|\blatte\b|cappuccino|macchiato"
                          r"|milk tea|caf[eé] au lait|\bmilo\b|ovaltine", re.I)
_ASIA_NOT_SACHET = re.compile(r"cookie|biscuit|wafer|\bbars?\b|chocolate|pretz|pocky|snack|nugget|cereal|chips", re.I)
_ASIA_CUP = re.compile(r"yogh?urt|yoghourt|yakult|pudding", re.I)


# A food eaten by the piece or the pack (read in the name): it opens on its serving of 15 to 350 g
# (a 400 g "serving" of dumplings is the bag).
_ASIA_UNIT_FOOD = re.compile(
    r"onigiri|rice ?balls?|sandwich|burger|bento|kimbap|gimbap|(?<![a-z])sushi|(?<![a-z])pies?(?![a-z])|ice ?cream|ice bar"
    r"|popsicle|melona|monaka|daifuku|dorayaki|taiyaki|(?<![a-z])buns?(?![a-z])|(?<![a-z])baos?(?![a-z])|croissant"
    r"|do(?:ugh)?nuts?|muffin|pudding|cup noodles?|(?<![a-z])bars?(?![a-z])|hot ?dog|corn ?dog|pizza|dumplings?|mandu|gyoza", re.I)
_ASIA_NOT_UNIT = re.compile(r"\bmix\b|powder|flour|sauce|seasoning|paste|syrup|spread|flavou?r(?:ed)? (?:milk|drink|tea)|soap"
                            r"|crumbs?|wrappers?|skins?\b|chips?\b|crisps?\b|biscuits?|cookies?|crackers?|wafers?|oreo|calbee"
                            r"|potato(?!.*(?:corn ?dog|hot ?dog|bun|pie|croquette|burger))|crispy sandwich|cream sandwich|butter sandwich|sandwich (?:biscuits?|cookies?|crackers?)", re.I)
# A food used a spoonful, a sachet or a sheet at a time (read in the name): it opens on its serving of 30 g or less.
_ASIA_SPOON_FOOD = re.compile(
    r"\b(?:sauce|dressing|oil|mayonnaise|mayo|ketchup|paste|vinegar|syrup|honey|jam|butter|margarine|spread|sugar|salt"
    r"|sweetener|creamer|seasoning|powder|soup|stock|bouillon|cubes?|furikake|dashi|coffee|tea|matcha|cocoa|gochujang"
    r"|doenjang|sambal|sesame seeds|miso)$|instant coffee|miso soup|soup (?:mix|powder|base|stock)|stock cubes?"
    r"|seasoning|coffee mix|tea bags?|(?:soy|fish|oyster|chilli|chili) sauce", re.I)
_ASIA_NOT_SPOON = re.compile(r"chips?\b|crisps|fries|biscuit|cookie|cracker|wafer|oreo|cheetos|lay'?s|doritos|pringles|chocolate"
                             r"|cand(?:y|ies)|cake|bread|\bbuns?\b|\bnuts?\b|almond|cashew|snack|\bbars?\b|\bpie\b|roll|puff|pocky"
                             r"|pretz|kit ?kat|jelly|\bice\b|flavou?r(?:ed)?\b|popcorn|sandwich|toast|noodle|ramen", re.I)
# A dry powder, stock or soup mix, or an instant miso paste, is 100 kcal or more per 100 g (most
# 120 to 450). Under 80 its figures are the made-up soup's or drink's, and a 7 g stick of them would
# log 2 kcal: such a row opens on 100 g (Instant Miso Soup at 28 kcal with an 18 g packet, Blendy
# Matcha at 29 with 7.6 g; a Shijimi miso soup paste at 145 keeps its 18 g packet).
_ASIA_DRY_SPOON = re.compile(r"soup|stock|bouillon|cubes?|dashi|powder|coffee|(?<![a-z])tea(?![a-z])|matcha|cocoa|creamer", re.I)
# A sauce, an oil or a dressing: a "serving" over 60 g is the bottle, so such a row opens on 100 g
# (and so does any row whose "serving" is 750 g or more: a litre of milk, a 2 litre bottle).
_ASIA_BOTTLED = re.compile(r"^sauce\b|\b(?:sauce|dressing|oil|mayonnaise|mayo|ketchup|paste|vinegar|syrup|ghee|gochujang"
                           r"|doenjang|sambal|seasoning)s?$", re.I)


def asia_portions(item, portions):
    """The portions of a Japan, Korea or Asia pool row: labels in English (see asia_label) and the
    row's own serving first where the food is counted that way (see default_portions and above).

    The order is decided on the label as shown ("33 g" whether it was stored "33g" or "33 g"):
    a bare weight on crisps opens on 100 g, a cheeseburger on its serving (a unit food, below).
    """
    shown = [[asia_label(p[0], p[1])] + list(p[1:]) for p in portions]
    ordered = default_portions(item, shown)
    if item["per_unit"] != "100g" or not ordered:
        return ordered
    name, kcal = item["name"] or "", item["calories_kcal"]
    bottled = bool(_ASIA_BOTTLED.search(name.strip())) and not re.search(r"\b(?:in|with|w/)\b|&", name, re.I)
    if ordered[0][1] != 100 or ordered[0][0] != "100 g":
        if ((bottled and (ordered[0][1] or 0) > 60) or (ordered[0][1] or 0) >= 750) and len(portions[0]) <= 2:
            return [["100 g", 100]] + [p for p in ordered if p[1] != 100]
        return ordered
    snack = bool(_OFF_SNACK.search(name.lower()))
    for label, grams in [p[:2] for p in ordered[1:]]:
        if not grams:
            continue
        if (_ASIA_NOODLE_PACK.search(name) and not bottled and kcal >= 250 and 40 <= grams <= 200) \
                or (_ASIA_SACHET.search(name) and not _ASIA_NOT_SACHET.search(name) and kcal >= 250 and 3 <= grams <= 45) \
                or (_ASIA_CUP.search(name) and kcal < 200 and 40 <= grams <= 250) \
                or (_ASIA_UNIT_FOOD.search(name) and not _ASIA_NOT_UNIT.search(name + " " + (_field(item, "brand") or ""))
                    and 15 <= grams <= 350 and kcal * grams / 100 <= 900) \
                or (_ASIA_SPOON_FOOD.search(re.sub(r"\s*\([^)]*\)", "", name).strip()) and not snack
                    and not _ASIA_NOT_SPOON.search(name) and grams <= 30
                    and not (kcal < 80 and _ASIA_DRY_SPOON.search(name))):
            return [[label, grams]] + [p for p in ordered[1:] if p[:2] != [label, grams]]
    return ordered


def old_rules_database(conn):
    """True when the database holds Open Food Facts rows and not one has a completeness figure.

    That is what a database filled before version 3 looks like after its upgrade. Its rows
    were merged by the old rules (any same-name product, whatever its calories; pack photos
    and barcodes handed to USDA and curated rows) and a new import would carry those merges
    forward, so the app's food list must not be built from it.
    """
    with_figure, total = conn.execute(
        "SELECT COUNT(completeness), COUNT(*) FROM food_items WHERE source = 'off'").fetchone()
    return total > 0 and with_figure == 0


def unmarked_database(conn):
    """True when the database holds 10 000 or more Open Food Facts rows from other markets and
    not one of them has a doubt mark.

    A real import marks about one such row in a hundred (see pool_doubt), so this is what a
    database filled before version 4 looks like after its upgrade. A market pool chosen from
    it would let through the rows the marks are there to keep out.
    """
    total, marked = conn.execute("SELECT COUNT(*), COUNT(doubt) FROM food_items"
                                 " WHERE source = 'off' AND substr(region_tags, 1, 2) <> 'PH'").fetchone()
    return total >= 10000 and marked == 0


UNMARKED_MESSAGE = (
    "{db}: none of the Open Food Facts rows from other markets in this database has been tested "
    "for doubtful numbers, so it was filled by an older version of this script. A market pool "
    "cannot be chosen from it. Build a new database: give --db a file that does not exist yet "
    "and import the curated lists, USDA and Open Food Facts again (or export with every pool "
    "set to 0).")

OLD_RULES_MESSAGE = (
    "{db}: none of the Open Food Facts rows in this database has a completeness figure, so it "
    "was filled by an older version of this script (or from a file without a 'completeness' "
    "column). The app's food list cannot be chosen from it. Build a new database: give --db a "
    "file that does not exist yet and import the curated lists, USDA and Open Food Facts again.")

_TYPEABLE = re.compile("[A-Za-z]{3,}")      # a name the owner can type: three plain letters in a row
# A product named as one of these is measured by the spoon, the glass or the piece, so a row
# of it without a serving size (the app would offer only 100 g) counts for half its rank in
# the US pool: a twin with a serving, or any product scanned half as often, goes first, but
# a well-known product with no twin (Tabasco, an oat drink) still gets in. Not asked of a
# food that only has such a word in its description (sardines in olive oil, beans in water,
# butter popcorn, honey granola) or that has no calories (water: a serving changes nothing).
_NEEDS_SERVING = re.compile(
    r"\b(?:mayo|mayonnaise|ketchup|catsup|mustard|dressing|vinaigrette|sauce|salsa|oils?|syrup|honey|jam|jelly"
    r"|preserves|spread|butter|margarine|seasoning|spices?|pepper|salt|sugar|sweetener|creamer|bouillon|chili crisp"
    r"|instant coffee|coffee|espresso|tea|soda|cola|juice|drinks?|beverage|water|lemonade|kombucha|seltzer|energy"
    r"|beer|bars?|cookies?|candy|gum)\b", re.I)
_SERVED_WHOLE = re.compile(
    r"\b(?:in|with|flavou?red)\b.*\b(?:oils?|water|syrup|juice|sauce|brine|butter|sugar|honey|salt|pepper)\b"
    r"|\b(?:no|low|less|reduced|unsalted|lightly)[ -](?:salt|sugar|sodium|salted)\b"
    r"|\b(?:chips|crisps|popcorn|cereal|granola|sardines|tuna|beans|nuts|peanuts|almonds|crackers|pretzels)\b", re.I)
_LATIN = re.compile("[A-Za-z0-9]")


# A shop's listing saved as a name: "SNICKERS SNICKERS PACK SNICKERS COFFEE 25x $1 PROL",
# "Yakisoba 130G. With Kewpie Mayonnaise .5pack. Free Shipping".
_SHOP_LISTING = re.compile(r"\$\s?\d|free shipping|\bprol\b", re.I)


def half_latin(text):
    """1 when at least half of the letters of text are plain Latin letters (or it has none), else 0."""
    letters = [ch for ch in str(text or "") if ch.isalpha()]
    latin = sum(1 for ch in letters if "a" <= ch.lower() <= "z" or ch in "àáâãäåçèéêëìíîïñòóôõöùúûüýÿ")
    return 1 if not letters or 2 * latin >= len(letters) else 0


def needs_serving(name, kcal):
    """1 when a row with no serving size would be awkward to log (see _NEEDS_SERVING), else 0."""
    name = name or ""
    if (kcal or 0) < 1 or not _NEEDS_SERVING.search(name):
        return 0
    return 1 if re.search(r"\bbars?\b", name, re.I) or not _SERVED_WHOLE.search(name) else 0


def upce_to_upca(code):
    """The 12-digit UPC-A of an 8-digit UPC-E ("04913207" -> "049000001327"), or None when the
    eight digits are no UPC-E or the check digit does not fit. (The app has the same rule.)"""
    if len(code) != 8 or not code.isdigit() or code[0] not in "01" or code[:2] == "00":
        return None
    body, last = code[1:7], code[6]
    if last in "012":
        middle = body[:2] + last + "0000" + body[2:5]
    elif last == "3":
        middle = body[:3] + "00000" + body[3:5]
    elif last == "4":
        middle = body[:4] + "00000" + body[4]
    else:
        middle = body[:5] + "0000" + last
    upca = code[0] + middle
    total = sum(int(digit) * (1 if place % 2 else 3) for place, digit in enumerate(upca))
    return upca + code[7] if (10 - total % 10) % 10 == int(code[7]) else None


# British supermarkets' own labels. Their packs carry short in-store codes with no country
# in them, and many are tagged United States by mistake.
_UK_SHOP_BRAND = re.compile(
    r"\b(?:tesco|sainsbury'?s?|sainburys|asda|morrisons|waitrose|m ?& ?s|marks ?(?:&|and) ?spencers?)\b", re.I)


def british_pack(barcode, brand, regions):
    """1 when a row belongs in the UK pool whatever its tags say, else 0:
      - a barcode registered in the United Kingdom, in 13 digits (50...) or in 8 (EAN-8 50...);
      - a British supermarket's own brand (Tesco, Sainsbury's, M&S, Asda, Morrisons, Waitrose);
      - tagged UK and carrying a short in-store code (8 digits starting with 2, or with 0 or 1
        and not a US UPC-E), which no US pack has except Trader Joe's own, and a Trader
        Joe's code that is also tagged UK is shared with a British shop's product.
    """
    code = barcode or ""
    if barcode_origin(barcode) == "UK" or _UK_SHOP_BRAND.search(brand or ""):
        return 1
    if len(code) == 13 and code.startswith("00000"):
        short = code[5:]
        if short.startswith("50"):
            return 1
        if "UK" in (regions or "").split(",") and (short[0] == "2" or (short[0] in "01" and not upce_to_upca(short))):
            return 1
    return 0


# Western supermarkets' own brands, and Western brands seen with short codes that read as Korean
# (second reading of step 5): a Calvé peanut butter (0000088031304, an 8-digit code stored with
# five zeros), a Trader Joe's nut milk or a Lidl cheese is no product of the Japanese, Korean or
# Asian shelves, whatever its tags say. Such a row enters none of those three pools unless its
# name is written in Japanese, Korean, Thai or Chinese.
_WESTERN_SHOP_BRAND = re.compile(
    r"(?<![a-z])(?:lidl|aldi|trader ?joe'?s?|sainsbury'?s?|waitrose|tesco|kirkland|auchan|carrefour|eroski|calv[eé]"
    r"|marks (?:and|&) spencer|italiamo|mcennedy|maille)(?![a-z])", re.I)


def away_from_asia(barcode, brand, name, korean_brands):
    """1 when a row must not enter the Japan, Korea or Asia pools (see _WESTERN_SHOP_BRAND), else 0.

    korean_brands: the brands (lower case) of rows with a full 13-digit Korean code. A short code
    that reads as Korean counts only with Hangul in the name or brand, or with one of those brands.
    """
    if _NATIVE.search(name or ""):
        return 0
    if brand and _WESTERN_SHOP_BRAND.search(brand):
        return 1
    code = barcode or ""
    if code.startswith("00000880") and not _NATIVE.search(brand or "") \
            and (brand or "").strip().lower() not in korean_brands:
        return 1
    return 0


def pick_off_rows(conn, quota):
    """Choose the Open Food Facts rows for the app. Returns {pool: [id, ...]} with the keys
    "PH" and the five pools of OFF_POOLS; each list of food_items ids is in the order to write.

    Scan counts cannot be compared across countries (British shoppers scan far more than
    Filipino or Japanese ones), so one global "most scanned" list fills up with whatever
    Europe scans most. Instead:

    PH    every row tagged Philippines, or whose barcode was registered in the Philippines.
          No other test: this is the owner's home market.
    then, in this order, each from the rows not taken yet and up to its quota:
    KR    barcode registered in South Korea          JP    barcode registered in Japan
    AS    barcode registered in Thailand, Malaysia, Singapore, Indonesia, Vietnam,
          Hong Kong, Taiwan or China (what fills Philippine import shelves), or tagged
          only for the seven neighbouring markets (region_tags = 'AS'), whatever the
          barcode, except a Japanese or Korean barcode (that product stays in its own pool).
          An American or British pack also tagged for one of the seven ("US,AS") stays in
          the US or UK pool: its scan counts are American or British, not Asian.
    US    tagged United States                       UK    tagged United Kingdom

    A row can enter a pool only when it is
      - local: tagged with no other country (foreign_tags = 0), or carrying a barcode from
        one of the markets above (a product scanned in 30 countries is usually the French
        or Moroccan pack, not the one on a Manila shelf);
      - typeable: the name has three plain letters in a row (the app has no camera scanner,
        so a name in Japanese script could never be found);
      - complete: protein, carbs and fat are all known (the app would log a blank as 0 g);
      - wanted by somebody: scanned at least once, or it has a photo and a brand in Latin
        letters;
      - not doubtful: import_off found none of the patterns of pool_doubt in it (a label
        typed per serving, a dry-coffee figure on coffee, shampoo ...).
    A British pack (see british_pack: a British barcode, a British supermarket's own brand,
    or a short in-store code tagged UK) is left out of the US pool and belongs to the UK
    pool whatever its tags say. Worldwide packs with other European barcodes (Nutella,
    Lindt, Coca-Cola from Belgium) stay in the US pool.
    Inside a pool the order is: rank, completeness, has a photo, has a brand, has a serving
    size, id. In the US pool (about 679 000 candidates for 11 000 places) a row without a
    serving size whose name says it is used by the spoon, poured or eaten by the piece
    (mayonnaise, chili crisp, instant coffee, a soda, a cereal bar: the app would offer
    only 100 g) counts for half its rank (see needs_serving). In the Japan, Korea and Asia
    pools a row nobody has scanned whose name is less than half Latin letters comes after every
    other candidate (the owner types in Latin letters; a scanned one, such as "Tuna シーチキン",
    keeps its place), a shop's listing saved as a name is left out (see _SHOP_LISTING), a row
    with no brand named like a curated or USDA food
    ("Milk", "Mango", "Cookie") is left out, and in the Asia pool a row tagged for none of the
    seven markets (there for its barcode only; its scans are British or American) counts for
    half its rank. A barcode "registered in Japan" is read by market_origin: an 8-digit code
    that only looks Japanese counts when the row is tagged Japan. Rank is the scan count, times
    three for a brand seen on at least two
    Philippine rows (a brand that is on Philippine shelves). While a pool is filled, a row
    is skipped when a chosen row already has the same plain name and the same calories:
    that is usually the same product under another spelling of its brand. The US and UK pools
    count only chosen rows that are Philippine or sold in their own market for this, so a
    Japanese, Korean or Asian product never takes the place of an American one of the same name.
    """
    conn.create_function("market_origin", 2, market_origin, deterministic=True)
    conn.create_function("typeable", 1, lambda name: 1 if _TYPEABLE.search(name or "") else 0,
                         deterministic=True)
    conn.create_function("half_latin", 1, half_latin, deterministic=True)
    conn.create_function("shop_listing", 1, lambda name: 1 if _SHOP_LISTING.search(name or "") else 0, deterministic=True)
    conn.create_function("plain_words", 1, plain_words, deterministic=True)
    conn.create_function("latin_text", 1, lambda text: 1 if _LATIN.search(text or "") else 0,
                         deterministic=True)
    conn.create_function("needs_serving", 2, needs_serving, deterministic=True)
    conn.create_function("british_pack", 3, british_pack, deterministic=True)
    korean_brands = frozenset((brand or "").strip().lower() for (brand,) in conn.execute(
        "SELECT DISTINCT brand FROM food_items WHERE source = 'off' AND barcode LIKE '880%' AND length(barcode) = 13"))
    conn.create_function("away_from_asia", 3, lambda barcode, brand, name: away_from_asia(barcode, brand, name, korean_brands),
                         deterministic=True)

    # Brands on Philippine shelves: the brand half of name_key ("lucky me|pancit canton"),
    # seen on two or more rows tagged Philippines first.
    conn.execute("DROP TABLE IF EXISTS temp.ph_brand")
    conn.execute("""
        CREATE TEMP TABLE ph_brand AS
        SELECT substr(name_key, 1, instr(name_key, '|') - 1) AS brand_key FROM food_items
        WHERE source = 'off' AND substr(region_tags, 1, 2) = 'PH' AND instr(name_key, '|') > 1
        GROUP BY 1 HAVING COUNT(*) >= 2""")
    conn.execute("CREATE UNIQUE INDEX temp.ph_brand_key ON ph_brand (brand_key)")

    # The plain names of the curated and USDA rows, and of the first part of a USDA-style name
    # ("Milk, whole" -> "milk"): a Japanese, Korean or Asian product with no brand and one of
    # these names ("Milk", "Jelly", "Mango", "Cookie") adds nothing next to them.
    conn.execute("DROP TABLE IF EXISTS temp.reference_names")
    conn.execute("CREATE TEMP TABLE reference_names (name TEXT PRIMARY KEY) WITHOUT ROWID")
    names = set()
    for (name,) in conn.execute("SELECT name FROM food_items WHERE source IN ('curated', 'usda')"):
        names.update(filter(None, (plain_words(name), plain_words((name or "").split(",")[0]))))
    conn.executemany("INSERT INTO temp.reference_names VALUES (?)", ((name,) for name in sorted(names)))

    # One line of facts per Open Food Facts row that is Philippine or may enter a pool.
    # Innermost: the plain facts. Middle: the entry test. Outermost: drop the rest.
    conn.execute("DROP TABLE IF EXISTS temp.off_facts")
    conn.execute("""
        CREATE TEMP TABLE off_facts AS
        SELECT id, name, calories_kcal, popularity, region_tags, origin, rank, completeness,
               has_photo, has_brand, has_serving, is_ph, may_enter, british, away,
               (NOT has_serving AND needs_serving(name, calories_kcal)) AS no_serving,
               half_latin(name) AS latin, (NOT has_brand AND plain_words(name) IN temp.reference_names) AS generic,
               shop_listing(name) AS listing
        FROM (SELECT *, ((foreign_tags = 0 OR origin IS NOT NULL) AND typeable AND has_macros
                         AND (rank > 0 OR (has_photo AND latin_brand)) AND doubt IS NULL) AS may_enter
              FROM (SELECT f.id, f.name, f.calories_kcal, f.popularity, f.region_tags, f.foreign_tags,
                           f.completeness, f.doubt,
                           market_origin(f.barcode, f.region_tags) AS origin,
                           british_pack(f.barcode, f.brand, f.region_tags) AS british,
                           away_from_asia(f.barcode, f.brand, f.name) AS away,
                           f.popularity * (CASE WHEN b.brand_key IS NULL THEN 1 ELSE 3 END) AS rank,
                           (f.thumbnail_url IS NOT NULL) AS has_photo,
                           (f.brand IS NOT NULL) AS has_brand,
                           (f.portions <> '[]') AS has_serving,
                           (f.protein_g IS NOT NULL AND f.carbs_g IS NOT NULL AND f.fat_g IS NOT NULL) AS has_macros,
                           typeable(f.name) AS typeable,
                           latin_text(f.brand) AS latin_brand,
                           (substr(f.region_tags, 1, 2) = 'PH') AS is_ph
                    FROM food_items AS f
                    LEFT JOIN ph_brand AS b ON b.brand_key = substr(f.name_key, 1, instr(f.name_key, '|') - 1)
                    WHERE f.source = 'off'))
        WHERE is_ph OR origin = 'PH' OR may_enter""")

    # Chosen rows, and their (plain name, whole kcal) with the markets of the rows that took it.
    taken_ids, taken_foods = set(), {}
    picked = {"PH": []}
    for row_id, name, kcal in conn.execute("""SELECT id, name, calories_kcal FROM off_facts
                                              WHERE is_ph OR origin = 'PH' ORDER BY popularity DESC, id"""):
        picked["PH"].append(row_id)
        taken_ids.add(row_id)
        taken_foods.setdefault((plain_words(name), int(round(kcal))), set()).add("PH")

    members = {"KR": "origin = 'KR'", "JP": "origin = 'JP'",
               "AS": "(origin = 'AS' OR (region_tags = 'AS' AND COALESCE(origin, '') NOT IN ('JP', 'KR')))",
               "US": "instr(region_tags, 'US') > 0 AND NOT british",
               "UK": "(instr(region_tags, 'UK') > 0 OR british)"}
    for pool in OFF_POOLS:
        chosen = picked[pool] = []
        wanted = quota.get(pool, DEFAULT_OFF_QUOTA[pool])
        if wanted <= 0:
            continue
        asian = pool in ("JP", "KR", "AS")
        rank = {"US": "(CASE WHEN no_serving THEN rank / 2 ELSE rank END)",
                "AS": "(CASE WHEN instr(region_tags, 'AS') = 0 THEN rank / 2 ELSE rank END)"}.get(pool, "rank")
        candidates = conn.execute(f"""
            SELECT id, name, calories_kcal, region_tags FROM off_facts
            WHERE may_enter AND {members[pool]} {"AND NOT generic AND NOT listing AND NOT away" if asian else ""}
            ORDER BY {"(latin OR popularity > 0) DESC," if asian else ""} {rank} DESC,
                     completeness DESC, has_photo DESC, has_brand DESC, has_serving DESC, id""")
        # The US and UK pools skip a repeat only of a Philippine row or of a row sold in their own
        # market: "Soy Sauce" by Kikkoman (US) stays although a Korean "Soy sauce" of the same
        # calories was chosen first. A Pocky with a Japanese barcode that is tagged US, chosen for
        # the Japan pool, still keeps its American twin out.
        blocking = None if asian else {"PH", "US"} | ({"UK"} if pool == "UK" else set())
        for row_id, name, kcal, regions in candidates:
            if row_id in taken_ids:
                continue
            food = (plain_words(name), int(round(kcal)))
            takers = taken_foods.get(food)
            if takers and (blocking is None or takers & blocking):
                continue
            chosen.append(row_id)
            taken_ids.add(row_id)
            taken_foods.setdefault(food, set()).update((regions or "").split(","))
            if len(chosen) >= wanted:
                break
        candidates.close()                      # a full pool leaves rows unread; let go of the table
    conn.execute("DROP TABLE temp.off_facts")
    conn.execute("DROP TABLE temp.ph_brand")
    conn.execute("DROP TABLE temp.reference_names")
    return picked


def export_app(conn, out_path, off_quota, built):
    """Write food-db.json "version 2" (section 6 of the data contract).

    Order: curated, USDA survey foods, other USDA foods, then Open Food Facts: Philippine
    products (most scanned first), then the US, UK, Japan, Korea and Asia pools (see
    pick_off_rows). Rows are written one at a time.

    Besides the rows, the file gets "alias": [[row number, barcode, barcode, ...], ...],
    the barcodes of other packs that were merged into a row (as whole numbers; the app puts
    the leading zeros back). Typing one of them finds that row.

    Returns (row counts by source, {"pools": rows per Open Food Facts pool, "aliases": count}).
    """
    if old_rules_database(conn):
        raise InputError(OLD_RULES_MESSAGE.format(db=conn.execute("PRAGMA database_list").fetchone()[2] or "database"))
    if any(off_quota.get(pool, DEFAULT_OFF_QUOTA[pool]) > 0 for pool in OFF_POOLS) and unmarked_database(conn):
        raise InputError(UNMARKED_MESSAGE.format(db=conn.execute("PRAGMA database_list").fetchone()[2] or "database"))

    conn.execute("DROP TABLE IF EXISTS temp.export_pick")
    conn.execute("CREATE TEMP TABLE export_pick (pos INTEGER PRIMARY KEY AUTOINCREMENT, id INTEGER, code INTEGER,"
                 " pool TEXT)")
    pick = "INSERT INTO export_pick (id, code) SELECT id, {code} FROM food_items WHERE {where} ORDER BY {order}"
    conn.execute(pick.format(code=0, where="source = 'curated'", order="id"))
    conn.execute(pick.format(code=1, where="source = 'usda' AND substr(source_ref, 1, 7) = 'survey:'", order="id"))
    conn.execute(pick.format(code=2, where="source = 'usda' AND (source_ref IS NULL"
                                           " OR substr(source_ref, 1, 7) <> 'survey:')", order="id"))
    off_rows = pick_off_rows(conn, off_quota)
    for pool in ("PH",) + OFF_WRITE_ORDER:
        conn.executemany("INSERT INTO export_pick (id, code, pool) VALUES (?, 3, ?)",
                         ((row_id, pool) for row_id in off_rows[pool]))

    def shown_brand(brand, pool):
        """The brand as written into the file: tidied for the Japan, Korea and Asia pools (see asia_brand)."""
        return asia_brand(brand) if pool in ASIA_POOLS else brand

    # First pass: the two lists of repeated texts (brands, groups), the counts and the aliases.
    brands, groups = {}, {}
    counts = {"curated": 0, "usda": 0, "off": 0}
    aliases, alias_codes = [], set()
    listing = conn.execute("""SELECT f.brand, f.group_label, p.code, f.alt_barcodes, p.pool FROM export_pick AS p
                              JOIN food_items AS f ON f.id = p.id ORDER BY p.pos""")
    for number, (brand, group, code, alt_barcodes, pool) in enumerate(listing):     # number = the row's place in "items"
        brand = shown_brand(brand, pool)
        if brand and brand not in brands:
            brands[brand] = len(brands)
        if group and group not in groups:
            groups[group] = len(groups)
        counts[("curated", "usda", "usda", "off")[code]] += 1
        if alt_barcodes == "[]":
            continue
        # A merged barcode is passed on when its own pack was scanned at least once or is
        # tagged PH, JP or KR. One that is (by now) some row's own barcode is left out, and
        # so is one already listed for an earlier row.
        codes = []
        for barcode, (popularity, regions) in sorted(aliases_read(alt_barcodes).items()):
            if popularity < 1 and not any(region in regions.split(",") for region in ALIAS_REGIONS):
                continue
            if barcode in alias_codes or not barcode.isdigit():
                continue
            if conn.execute("SELECT 1 FROM food_items WHERE barcode = ?", (barcode,)).fetchone():
                continue
            alias_codes.add(barcode)
            codes.append(int(barcode))
        if codes:
            aliases.append([number] + codes)

    def one_decimal(value):
        return None if value is None else tidy_number(round(value, 1))

    def app_row(item, code, pool):
        try:
            portions = json.loads(item["portions"] or "[]")
            aka = json.loads(item["aka"] or "[]")
        except ValueError:
            portions, aka = [], []
        name, brand = item["name"], shown_brand(item["brand"], pool)
        if pool in ASIA_POOLS:
            name = asia_name(name, brand)
            portions = asia_portions(item, portions)
            if brand == "7-Eleven" and "Seven Eleven" not in aka:
                aka = aka + ["Seven Eleven"]                    # typed in words, as people say it
        else:
            portions = default_portions(item, portions)
        if item["source"] == "off" and not _philippine(item):
            portions = [[tidy_label(label, rest[0] if rest else None)] + rest for label, *rest in portions]
        thumb = item["thumbnail_url"] or 0
        if thumb and thumb.startswith(THUMB_BASE):
            thumb = thumb[len(THUMB_BASE):]
        return [
            name,
            brands.get(brand, -1),
            groups.get(item["group_label"], -1),
            CATEGORIES.index(item["category"]),
            item["region_tags"] or "",
            item["barcode"] or 0,
            thumb,
            1 if item["per_unit"] == "serving" else 0,
            int(round(item["calories_kcal"])),
            one_decimal(item["protein_g"]), one_decimal(item["carbs_g"]), one_decimal(item["sugars_g"]),
            one_decimal(item["fat_g"]), one_decimal(item["saturates_g"]), one_decimal(item["fibre_g"]),
            None if item["sodium_mg"] is None else int(round(item["sodium_mg"])),
            portions,
            code,
            aka or 0,
            item["basis"] if item["basis"] in ("official", "estimate") else 0,   # 0 = plain database figure
        ]

    head = {"version": 2, "built": built, "note": EXPORT_NOTE, "fields": EXPORT_FIELDS,
            "brands": list(brands), "groups": list(groups), "thumbBase": THUMB_BASE, "counts": counts,
            "alias": aliases}
    folder = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(folder, exist_ok=True)
    # Second pass: write to a side file and swap it in at the end, so a failed run never
    # leaves half a food list where the app expects a whole one.
    side_file = out_path + ".tmp"
    with open(side_file, "w", encoding="utf-8", newline="\n") as out:
        out.write(compact_json(head)[:-1] + ',"items":[')
        rows = conn.execute("""SELECT f.*, p.code AS export_code, p.pool AS export_pool FROM export_pick AS p
                               JOIN food_items AS f ON f.id = p.id ORDER BY p.pos""")
        for number, item in enumerate(rows):
            out.write(("," if number else "") + compact_json(app_row(item, item["export_code"], item["export_pool"])))
        out.write("]}")
    os.replace(side_file, out_path)
    conn.execute("DROP TABLE temp.export_pick")
    details = {"pools": {pool: len(off_rows[pool]) for pool in ("PH",) + OFF_WRITE_ORDER},
               "aliases": len(alias_codes)}
    return counts, details


def off_quota_argument(text):
    """--off-quota "US=12000,UK=2500" -> {"US": 12000, "UK": 2500, "JP": 3500, "KR": 2500, "AS": 1500}.

    Pools that are not named keep their default.
    """
    quota = dict(DEFAULT_OFF_QUOTA)
    for part in text.split(","):
        if not part.strip():
            continue
        pool, equals, number = (piece.strip() for piece in part.partition("="))
        pool = pool.upper()
        if pool == "PH":
            raise argparse.ArgumentTypeError("PH takes no quota: Philippine rows are always exported in full")
        if not equals or pool not in quota:
            raise argparse.ArgumentTypeError(
                f"{part.strip()!r} is not POOL=N with POOL one of {', '.join(OFF_WRITE_ORDER)}")
        if not (number.isascii() and number.isdigit()):
            raise argparse.ArgumentTypeError(f"{part.strip()!r}: the row count must be a whole number, 0 or more")
        quota[pool] = int(number)
    return quota


# ---------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------

def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        description="Import curated, USDA and Open Food Facts foods into the build-time SQLite "
                    "database and optionally write the app's food list.")
    parser.add_argument("--db", required=True, metavar="PATH", help="SQLite file (created when missing)")
    parser.add_argument("--curated-json", action="append", default=[], metavar="FILE",
                        help="curated list in JSON; may be given more than once")
    parser.add_argument("--usda-path", action="append", default=[], metavar="PATH",
                        help="FoodData Central CSV folder or flat CSV file; may be given more than once")
    parser.add_argument("--off-path", metavar="FILE", help="Open Food Facts export (tab-separated, .gz allowed)")
    parser.add_argument("--export-app", metavar="OUT.json", help="write the app's food list here")
    default_quota = ",".join(f"{pool}={DEFAULT_OFF_QUOTA[pool]}" for pool in OFF_WRITE_ORDER)
    parser.add_argument("--off-quota", type=off_quota_argument, default=dict(DEFAULT_OFF_QUOTA), metavar="POOLS",
                        help="most Open Food Facts rows to export per market pool, as POOL=N pairs "
                             f"(default {default_quota}); 0 leaves a pool out; Philippine rows are always exported")
    parser.add_argument("--built", default="", metavar="YYYY-MM-DD", help="date to write into the exported file")
    parser.add_argument("--quiet", action="store_true", help="print only the final summary line")
    args = parser.parse_args(argv)

    if not args.db.strip():
        parser.error("--db needs a file name")
    if args.built:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.built):
                raise ValueError
            datetime.date.fromisoformat(args.built)
        except ValueError:
            parser.error("--built must be a date like 2026-11-22")
    for path in args.curated_json + ([args.off_path] if args.off_path else []):
        if not os.path.isfile(path):
            parser.error(f"file not found: {path}")
    for path in args.usda_path:
        if not os.path.exists(path):
            parser.error(f"file or folder not found: {path}")
    return args


def main(argv=None):
    # A file name or food name with unusual letters must not crash printing on a Windows console.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = parse_arguments(argv)
    started = time.time()
    raise_csv_field_limit()
    conn = None
    try:
        if sqlite3.sqlite_version_info < (3, 33):
            raise InputError(f"SQLite 3.33 or newer is needed; this Python has {sqlite3.sqlite_version}")
        conn = food_schema.connect(args.db)
        food_schema.migrate(conn)
        if args.export_app and old_rules_database(conn):
            # Say so before any work is done: importing into this database would carry its
            # old merges forward (see old_rules_database).
            raise InputError(OLD_RULES_MESSAGE.format(db=args.db))
        importer = Importer(conn, args.quiet)
        for path in args.curated_json:
            import_curated(importer, path)
        for path in args.usda_path:
            if os.path.isdir(path):
                import_usda_folder(importer, path)
            else:
                import_usda_flat(importer, path)
        if args.off_path:
            import_off(importer, args.off_path)
        importer.save()
        summary = importer.summary()
        if args.export_app:
            counts, details = export_app(conn, args.export_app, args.off_quota, args.built)
            size = os.path.getsize(args.export_app)
            pools = ", ".join(f"{pool} {rows}" for pool, rows in details["pools"].items())
            importer.say(f"exported {args.export_app}: {sum(counts.values())} rows, {size} bytes; "
                         f"Open Food Facts pools: {pools}; {details['aliases']} extra barcodes")
            summary["export"] = {"rows": sum(counts.values()), "bytes": size, "off_pools": details["pools"],
                                 "aliases": details["aliases"]}
        importer.say(f"done in {time.time() - started:.1f} s")
    except (InputError, OSError, EOFError, ValueError, RuntimeError, csv.Error, zlib.error,
            sqlite3.Error) as problem:
        # ValueError also covers text that is not UTF-8; EOFError and zlib.error a damaged .gz file.
        print(f"import_foods: error: {problem}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
    print(json.dumps(summary))                              # always the last line on stdout
    return 0


if __name__ == "__main__":
    sys.exit(main())
