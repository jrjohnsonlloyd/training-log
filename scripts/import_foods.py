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
        [--max-off N]              how many non-Philippine Open Food Facts rows to export (default 20000)
        [--built YYYY-MM-DD]       date written into the exported file
        [--quiet]                  print only the final summary line

Nothing is downloaded: every input is a file already on this computer. Standard library only.

What one run does
-----------------
1. Every row that is read is cleaned and checked, then parked in a temporary table
   ("cand", short for candidates). Input files are read one line at a time and written
   in small batches, so even the 10 GB Open Food Facts export needs little memory.
2. Rows already in the database join the candidates. A row read again (same source and
   same source_ref) replaces its older self and keeps its id.
3. Duplicates collapse: first rows sharing a barcode, then rows sharing the same
   name + brand key. A curated row always beats a USDA row, which always beats an Open
   Food Facts row. The winner keeps its own numbers but takes the loser's picture and
   regions (and barcode, when it has none).
4. food_items is brought in line with the surviving candidates in one all-or-nothing step.

Running the same command twice leaves the same rows. The last line printed is a JSON
summary of the run.
"""
import argparse
import csv
import datetime
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import zlib

# "python -I" (isolated mode) does not put this folder on the import path, so add it by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import food_schema  # noqa: E402  (must come after the line above)
from food_schema import (CATEGORIES, REGIONS, SOURCE_PRIORITY, kcal_from_kj,  # noqa: E402
                         name_key, normalize_barcode, sodium_mg_from_salt_g)

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
    "source_ref", "popularity", "name_key",
)
_COLUMN_LIST = ", ".join(ITEM_COLUMNS)

# How many of the seven optional numbers a row has. Used to pick the fuller of two duplicates.
FILLED_SQL = "(" + " + ".join(f"({column} IS NOT NULL)" for column in GRAM_COLUMNS + ("sodium_mg",)) + ")"

# Open Food Facts: the only columns read out of roughly 200, and the countries that are kept.
OFF_COLUMNS = (
    "code", "product_name", "product_name_en", "brands", "countries_tags", "image_front_small_url",
    "serving_size", "serving_quantity", "unique_scans_n", "energy-kcal_100g", "energy-kj_100g",
    "energy_100g", "proteins_100g", "carbohydrates_100g", "sugars_100g", "fat_100g",
    "saturated-fat_100g", "fiber_100g", "sodium_100g", "salt_100g",
)
OFF_COUNTRIES = {"en:philippines": "PH", "en:united-states": "US", "en:united-kingdom": "UK",
                 "en:japan": "JP", "en:south-korea": "KR"}

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


def regions_union(first, second):
    """"PH" and "US,KR" -> "PH,US,KR", always in the fixed order of REGIONS."""
    have = set((first or "").split(",")) | set((second or "").split(","))
    return ",".join(code for code in REGIONS if code in have)


def blank_row(source):
    """A food_items row with nothing filled in yet, as a dictionary keyed by column name."""
    return {
        "name": "", "brand": None, "group_label": None, "category": "whole_food", "region_tags": "",
        "barcode": None, "thumbnail_url": None, "per_unit": "100g", "calories_kcal": None,
        "protein_g": None, "carbs_g": None, "sugars_g": None, "fat_g": None, "saturates_g": None,
        "fibre_g": None, "sodium_mg": None, "portions": [], "aka": [], "basis": "database",
        "source": source, "source_priority": SOURCE_PRIORITY[source], "source_ref": None,
        "popularity": 0,
    }


def apply_rules(row):
    """Check and round the numbers of one row (rules b to e of the data contract).

    Returns False when the row must be thrown away. The upper limits only make sense for
    numbers per 100 g, so a row stored per serving is only checked for missing or negative
    values.
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

class Importer:
    def __init__(self, conn, quiet=False):
        self.conn = conn
        self.quiet = quiet
        self.read = {"curated": 0, "usda": 0, "off": 0}     # input rows seen in this run
        self.filtered_region = 0
        self.invalid = 0
        self.duplicates = 0
        self._waiting = []                                  # cleaned rows not yet written
        self._order = 0                                     # 1, 2, 3 ... in the order rows are read
        conn.create_function("regions_union", 2, regions_union)
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

    def collapse(self, key_sql):
        """Candidates sharing the same value of key_sql become one row (section 4 of the contract).

        The winner is the row with the lowest source_priority number; then the one with more
        of the optional numbers filled in; then the more popular one; then the one imported
        first. It keeps its own values, except: an empty thumbnail or barcode is taken from
        the best loser that has one, and its regions become everybody's regions together.
        Rows whose key is NULL are left alone. Returns how many rows were removed.
        """
        conn = self.conn
        # List the rows of every group of two or more, best row first, in a table of their own
        # so that it can be read while the candidates table is being changed.
        conn.execute("DROP TABLE IF EXISTS temp.dup")
        conn.execute(f"""
            CREATE TEMP TABLE dup AS
            SELECT rid, ({key_sql}) AS k, thumbnail_url, region_tags, barcode
            FROM cand
            WHERE ({key_sql}) IN (SELECT ({key_sql}) FROM cand WHERE ({key_sql}) IS NOT NULL
                                  GROUP BY 1 HAVING COUNT(*) > 1)
            ORDER BY 2, source_priority, {FILLED_SQL} DESC, popularity DESC, ord""")

        updates, deletes, removed = [], [], 0

        def write():
            conn.executemany("UPDATE cand SET thumbnail_url = ?, region_tags = ?, barcode = ?, changed = 1"
                             " WHERE rid = ?", updates)
            conn.executemany("DELETE FROM cand WHERE rid = ?", deletes)
            updates.clear()
            deletes.clear()

        def close_group(winner, before):
            if winner is not None and winner[1:] != before:
                updates.append((winner[1], winner[2], winner[3], winner[0]))

        winner, before, group = None, None, None            # winner = [rid, thumbnail, regions, barcode]
        for rid, key, thumbnail, regions, barcode in conn.execute(
                "SELECT rid, k, thumbnail_url, region_tags, barcode FROM dup ORDER BY rowid"):
            if winner is None or key != group:              # first row of a group: the winner
                close_group(winner, before)
                if len(updates) + len(deletes) >= BATCH:
                    write()
                winner, group = [rid, thumbnail, regions, barcode], key
                before = winner[1:]
                continue
            if winner[1] is None and thumbnail:
                winner[1] = thumbnail
            winner[2] = regions_union(winner[2], regions)
            if winner[3] is None and barcode:
                winner[3] = barcode
            deletes.append((rid,))
            removed += 1
        close_group(winner, before)
        write()
        conn.execute("DROP TABLE temp.dup")
        return removed

    def save(self):
        """De-duplicate everything and bring food_items in line with the result."""
        conn = self.conn
        self.flush()

        # The same source row read twice in this run (for example one barcode on two lines).
        self.duplicates += self.collapse("source || ':' || source_ref")

        # A row read again replaces the row saved earlier and keeps its id. What the saved
        # row had picked up from duplicates on an earlier run (picture, barcode, regions) is
        # carried over, so re-importing one source alone does not lose it.
        conn.execute("""
            UPDATE cand SET item_id = f.id,
                            thumbnail_url = COALESCE(cand.thumbnail_url, f.thumbnail_url),
                            barcode = COALESCE(cand.barcode, f.barcode),
                            region_tags = regions_union(cand.region_tags, f.region_tags)
            FROM food_items AS f
            WHERE f.source = cand.source AND f.source_ref = cand.source_ref AND f.source_ref IS NOT NULL""")
        conn.execute("CREATE INDEX temp.cand_item_id ON cand (item_id)")

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
        conn.execute("CREATE INDEX temp.cand_barcode ON cand (barcode)")
        conn.execute("CREATE INDEX temp.cand_name_key ON cand (name_key)")

        self.duplicates += self.collapse("barcode")         # step 1: same barcode
        self.duplicates += self.collapse("name_key")        # step 2: same name + brand

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
        return {"read": dict(self.read), "kept": kept, "filtered_region": self.filtered_region,
                "invalid": self.invalid, "duplicates": self.duplicates, "total_rows": total}


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
    row["region_tags"] = ",".join(code for code in REGIONS if code in regions)

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
         scans, kcal, kj, energy, protein, carbs, sugars, fat, saturates, fibre, sodium, salt) = record

        # Region first: most of the file is sold elsewhere and is skipped right here.
        sold_in = {OFF_COUNTRIES.get(tag.strip().lower()) for tag in countries.split(",")}
        regions = ",".join(region for region in REGIONS if region in sold_in)
        if not regions:
            importer.filtered_region += 1
            continue

        row = blank_row("off")
        row["name"] = clean_text(product_name) or clean_text(product_name_en)
        if not row["name"]:
            importer.invalid += 1
            continue
        row["brand"] = clean_text(brands.split(",")[0]) or None
        row["category"] = "supermarket"
        row["region_tags"] = regions
        row["barcode"] = normalize_barcode(code)
        row["thumbnail_url"] = http_url(image)
        row["popularity"] = max(0, int(to_number(scans) or 0))

        # Energy: kcal when given, otherwise from kilojoules.
        row["calories_kcal"] = to_number(kcal)
        if row["calories_kcal"] is None:
            kilojoules = to_number(kj)
            if kilojoules is None:
                kilojoules = to_number(energy)
            if kilojoules is not None:
                row["calories_kcal"] = kcal_from_kj(kilojoules)
        row["protein_g"], row["carbs_g"], row["sugars_g"] = to_number(protein), to_number(carbs), to_number(sugars)
        row["fat_g"], row["saturates_g"], row["fibre_g"] = to_number(fat), to_number(saturates), to_number(fibre)
        # Sodium: this file gives it in grams. When it is missing, work it out from salt.
        sodium_g, salt_g = to_number(sodium), to_number(salt)
        if sodium_g is not None:
            row["sodium_mg"] = sodium_g * 1000
        elif salt_g is not None:
            row["sodium_mg"] = sodium_mg_from_salt_g(salt_g)

        label, amount = clean_text(serving_size), to_number(serving_quantity)
        if label and amount and amount > 0:
            row["portions"] = [[label[:60], tidy_number(round(amount, 2))]]
        if not apply_rules(row):
            importer.invalid += 1
            continue
        code = code.strip()
        row["source_ref"] = row["barcode"] or code or "name:" + name_key(row["name"], row["brand"])
        importer.stage(row)
        kept += 1
    importer.read["off"] += count
    missing = {"code", "product_name", "countries_tags"} - found
    if count and missing:
        importer.warn(f"{path}: column(s) {', '.join(sorted(missing))} not found; is this an Open Food Facts export?")
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
    return [["100 g", 100]] + [p for p in portions if p[1] != 100]


def export_app(conn, out_path, max_off, built):
    """Write food-db.json "version 2" (section 6 of the data contract). Returns the row counts.

    Order: curated, USDA survey foods, other USDA foods, then Open Food Facts (Philippine
    products first, then the most scanned). Rows are written one at a time.
    """
    conn.execute("DROP TABLE IF EXISTS temp.export_pick")
    conn.execute("CREATE TEMP TABLE export_pick (pos INTEGER PRIMARY KEY AUTOINCREMENT, id INTEGER, code INTEGER)")
    pick = "INSERT INTO export_pick (id, code) SELECT id, {code} FROM food_items WHERE {where} ORDER BY {order}"
    conn.execute(pick.format(code=0, where="source = 'curated'", order="id"))
    conn.execute(pick.format(code=1, where="source = 'usda' AND substr(source_ref, 1, 7) = 'survey:'", order="id"))
    conn.execute(pick.format(code=2, where="source = 'usda' AND (source_ref IS NULL"
                                           " OR substr(source_ref, 1, 7) <> 'survey:')", order="id"))
    conn.execute(pick.format(code=3, where="source = 'off' AND substr(region_tags, 1, 2) = 'PH'",
                             order="popularity DESC, id"))
    conn.execute(pick.format(code=3, where="source = 'off' AND substr(region_tags, 1, 2) <> 'PH'",
                             order="popularity DESC, id") + " LIMIT ?", (max_off,))

    # First pass: the two lists of repeated texts (brands, groups) and the counts.
    brands, groups = {}, {}
    counts = {"curated": 0, "usda": 0, "off": 0}
    for brand, group, code in conn.execute("""SELECT f.brand, f.group_label, p.code FROM export_pick AS p
                                              JOIN food_items AS f ON f.id = p.id ORDER BY p.pos"""):
        if brand and brand not in brands:
            brands[brand] = len(brands)
        if group and group not in groups:
            groups[group] = len(groups)
        counts[("curated", "usda", "usda", "off")[code]] += 1

    def one_decimal(value):
        return None if value is None else tidy_number(round(value, 1))

    def app_row(item, code):
        try:
            portions = json.loads(item["portions"] or "[]")
            aka = json.loads(item["aka"] or "[]")
        except ValueError:
            portions, aka = [], []
        portions = default_portions(item, portions)
        thumb = item["thumbnail_url"] or 0
        if thumb and thumb.startswith(THUMB_BASE):
            thumb = thumb[len(THUMB_BASE):]
        return [
            item["name"],
            brands.get(item["brand"], -1),
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
            "brands": list(brands), "groups": list(groups), "thumbBase": THUMB_BASE, "counts": counts}
    folder = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(folder, exist_ok=True)
    # Second pass: write to a side file and swap it in at the end, so a failed run never
    # leaves half a food list where the app expects a whole one.
    side_file = out_path + ".tmp"
    with open(side_file, "w", encoding="utf-8", newline="\n") as out:
        out.write(compact_json(head)[:-1] + ',"items":[')
        rows = conn.execute("""SELECT f.*, p.code AS export_code FROM export_pick AS p
                               JOIN food_items AS f ON f.id = p.id ORDER BY p.pos""")
        for number, item in enumerate(rows):
            out.write(("," if number else "") + compact_json(app_row(item, item["export_code"])))
        out.write("]}")
    os.replace(side_file, out_path)
    conn.execute("DROP TABLE temp.export_pick")
    return counts


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
    parser.add_argument("--max-off", type=int, default=20000, metavar="N",
                        help="most non-Philippine Open Food Facts rows to export (default 20000)")
    parser.add_argument("--built", default="", metavar="YYYY-MM-DD", help="date to write into the exported file")
    parser.add_argument("--quiet", action="store_true", help="print only the final summary line")
    args = parser.parse_args(argv)

    if not args.db.strip():
        parser.error("--db needs a file name")
    if args.max_off < 0:
        parser.error("--max-off cannot be negative")
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
            counts = export_app(conn, args.export_app, args.max_off, args.built)
            importer.say(f"exported {args.export_app}: {sum(counts.values())} rows, "
                         f"{os.path.getsize(args.export_app)} bytes")
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
