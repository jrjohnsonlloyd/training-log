"""Contract tests for the food database: scripts/food_schema.py and scripts/import_foods.py.

Run from the repository root:

    python -m unittest discover -s tests -p "test_*.py" -v

How these tests are built
- Every expected value was worked out by hand from the data contract and from the
  small synthetic fixtures in tests/fixtures/. Nothing was copied from importer output.
- food_schema is imported directly for the pure helper and migration tests.
- The importer is treated as a black box: it is started with subprocess from the
  repository root and only its exit code, its stdout summary, the SQLite file and
  the exported app bundle are looked at.
- The main import (curated + flat USDA file + Open Food Facts file) runs once and is
  shared by all read-only test classes. Everything is written to a temporary folder.

What the main import must end up with (hand count)
  curated  11 read: 8 kept, 3 skipped as invalid (bad category, bad region, no calories)
  usda     13 read: 1 invalid (no calories), 1 loses to a curated row -> 11 kept
  off      52 read: 2 filtered by region, 6 invalid, 7 lose a de-duplication -> 37 kept
  duplicates 8: barcode C3/A1, U2/A3, Choco Pie pair, Meiji pair;
                name+brand Chippy pair, Skippy, SkyFlakes, Pork Adobo
  total_rows 56
  app bundle: 8 curated + 11 USDA + 29 Open Food Facts rows chosen by the pool rules (see OFF_POOLS)

The rule fixtures (off_rules.csv, off_dedupe.csv, off_alias_guard.csv) and the importer that makes
them are described in tests/fixtures/make_off_fixtures.py. Every expected value below was derived by
hand from the audit digests (the CORRECTED recommendations) and the decision list of the
Open Food Facts import task, never from importer output. Details the specification leaves open
are not asserted.
"""
import csv
import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPTS = os.path.join(REPO, "scripts")
FIXTURES = os.path.join(HERE, "fixtures")
IMPORTER = os.path.join("scripts", "import_foods.py")   # relative: the importer is started from REPO

OFF_TSV = os.path.join(FIXTURES, "sample_off.csv")
OFF_RULES = os.path.join(FIXTURES, "off_rules.csv")
OFF_DEDUPE = os.path.join(FIXTURES, "off_dedupe.csv")
OFF_GUARD = os.path.join(FIXTURES, "off_alias_guard.csv")
USDA_FLAT = os.path.join(FIXTURES, "sample_usda.csv")
USDA_FOLDER = os.path.join(FIXTURES, "usda_fdc_mini")
CURATED = os.path.join(FIXTURES, "local_ph_curated.json")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    import food_schema
    _SCHEMA_IMPORT_ERROR = None
except Exception as exc:   # not written yet, or broken: schema tests then fail with a clear message
    food_schema = None
    _SCHEMA_IMPORT_ERROR = exc

BUILT = "2026-11-22"
THUMB_BASE = "https://images.openfoodfacts.org/images/products/"
REGION_ORDER = ("PH", "US", "UK", "JP", "KR")
MACROS = ("protein_g", "carbs_g", "sugars_g", "fat_g", "saturates_g", "fibre_g")

BUNDLE_FIELDS = ["name", "brand", "group", "category", "regions", "barcode", "thumb", "per",
                 "kcal", "protein", "carbs", "sugars", "fat", "saturates", "fibre", "sodium",
                 "portions", "source", "aka", "basis"]

V1_COLUMNS = ["id", "name", "group_label", "kcal", "protein", "carbs", "fat", "portions",
              "source", "source_ref"]
V2_COLUMNS = ["id", "name", "brand", "group_label", "category", "region_tags", "barcode",
              "thumbnail_url", "per_unit", "calories_kcal", "protein_g", "carbs_g", "sugars_g",
              "fat_g", "saturates_g", "fibre_g", "sodium_mg", "portions", "aka", "basis",
              "source", "source_priority", "source_ref", "popularity", "name_key"]
V3_NEW_COLUMNS = ["foreign_tags", "completeness", "alt_barcodes"]
V3_COLUMNS = V2_COLUMNS + V3_NEW_COLUMNS

# Every column the importer reads from the Open Food Facts export that the fixtures must carry.
OFF_COLUMNS = ["code", "product_name", "product_name_en", "generic_name", "brands", "categories_en",
               "countries_tags", "image_front_small_url", "image_small_url", "serving_size",
               "serving_quantity", "completeness", "unique_scans_n", "energy-kcal_100g",
               "energy-kj_100g", "energy_100g", "proteins_100g", "carbohydrates_100g",
               "carbohydrates-total_100g", "sugars_100g", "fat_100g", "saturated-fat_100g",
               "fiber_100g", "sodium_100g", "salt_100g", "alcohol_100g"]
USDA_FLAT_COLUMNS = ["fdc_id", "data_type", "description", "brand_owner", "gtin_upc",
                     "food_category", "serving_size_g", "household_serving", "energy_kcal",
                     "energy_kj", "protein_g", "carbohydrate_g", "sugars_g", "fat_g",
                     "saturated_fat_g", "fiber_g", "sodium_mg"]

SUMMARY_KEYS = {"read", "kept", "filtered_region", "invalid", "duplicates", "total_rows"}

# Open Food Facts rows of the main import that the app bundle must list, pool by pool, as
# (name, barcode as the bundle writes it; 0 = no barcode). Hand-derived: the Philippine slice is
# every row tagged PH or with a 480 barcode, by scans; the other pools are filled in the order
# KR, JP, AS, US, UK by rank = scans (times 3 for a brand on two or more PH-tagged rows), then
# completeness, and are written in the order PH, US, UK, JP, KR, AS.
OFF_PH = [
    ("Chippy BBQ", "4800016644504"),                    # PH,US 300 (80 own, 300 from the merged pack)
    ("Kikkoman Soy Sauce", "4901515110504"),            # PH,UK,JP 260 (one foreign tag; PH slice has no test)
    ("Milo Activ-Go Powder", "4800361413480"),          # PH 55
    ("Bear Brand Powdered Milk", "4800361002950"),      # PH 12
    ("チョコ", "3012345000012"),                          # PH 7: no typeable name, no macros, French barcode
    ("Pinoy Spread Peanut", "4800001234500"),           # tagged US only, barcode prefix 480, 3 scans
]
OFF_US = [
    ("Nesquik Chocolate Powder", "0028000242008"),      # rank 30 = 10 scans x 3 (Nestle is on two PH rows)
    ("Maltesers", "0040000579816"),                     # 25 (brand Mars)
    ("Kikkoman Teriyaki Sauce", "0041390000189"),       # 20: Kikkoman is on one PH row only, no boost
    ("Maltesers", "0040000579847"),                     # 10 (Mars Food, 480 kcal: not a repeat of 500 kcal)
    ("Plain Crackers Salted", "0070000000019"),         # 8, completeness 90
    ("Gouda Cheese Slices", "4002468135795"),           # 8, completeness 50; tags US + world + european-union
]
OFF_UK = [
    ("Snickers", "5000159407236"),                      # 950
    ("Weetabix Original", "5010029000023"),             # 420
    ("McVitie's Digestive Biscuits", "5000168001142"),  # 386: two foreign tags, barcode prefix 500, no division
    ("Pot Noodle Original Curry", "5000300000011"),     # 150
    ("Baked Beans in Tomato Sauce", "5000169005743"),   # 33
    ("Deli Roast Chicken Slices", 0),                   # 15
    ("Corner Shop Flapjack", 0),                        # 3
]
OFF_JP = [
    ("Pocky Chocolate", "4901330502881"),               # 310
    ("Kewpie Mayonnaise", "4901577000010"),             # 130, tagged JP,US: the JP pool is filled before US
    ("Oi Ocha Green Tea", "4514603365511"),             # 90
    ("Meiji Milk Chocolate", "4902777000015"),          # 20
    ("Black Thunder Chocolate Bar", "4903032230116"),   # 0 scans, Latin-letter brand and a photo
]
OFF_KR = [
    ("Shin Ramyun", "8801043014830"),                   # 640, tagged US,KR: the KR pool is filled first
    ("Banana Flavored Milk", "8801056038861"),          # 75
    ("Choco Pie Original", "8801117760007"),            # 10
    ("Bamboo Salt Seasoning", "8801007000015"),         # 5
]
OFF_AS = [
    ("Indomie Mi Goreng", "8996001600269"),             # 29, tagged US + Indonesia, barcode prefix 899
]
OFF_POOLS = (("PH", OFF_PH), ("US", OFF_US), ("UK", OFF_UK), ("JP", OFF_JP), ("KR", OFF_KR), ("AS", OFF_AS))
OFF_EXPORT_ORDER = OFF_PH + OFF_US + OFF_UK + OFF_JP + OFF_KR + OFF_AS

# Open Food Facts rows that stay in the database but must not be exported, with the reason.
OFF_LEFT_OUT = {
    "Calpis Water": "protein, carbs and fat are all blank",
    "Fromage Blanc Nature": "two foreign tags (France, Germany) and a barcode prefix (301) of no wanted market",
    "Halva Peanut Bar": "tagged Japan only with barcode prefix 460: it belongs to no pool",
    "ポッキー チョコレート": "no run of three Latin letters in the name",
    "Meltykiss Premium Chocolate": "never scanned and no photo",
    "Plain Wafer Rolls": "never scanned and no brand",
    "Hokkaido Milk Caramel": "never scanned and the brand has no Latin letter or digit",
}
# The second "Maltesers" row (brand Mars Wrigley, 500.4 kcal) is left out as well: its plain name
# and rounded calories (500) are already taken by the Mars row. It is checked by brand, not by name.

# Barcodes of merged rows that the bundle must list under "alias", keyed by the survivor's name.
# Codes are written as numbers, so leading zeros are gone. All three merged rows had scans and PH tags.
MAIN_ALIASES = {
    "Chippy BBQ": {4800016644511},                       # the 300-scan "Jack 'n Jill Chippy BBQ" pack
    "CREAMY PEANUT BUTTER": {37600105002},               # USDA survivor (flat file: the name is as written); the 210-scan Skippy pack (037600105002)
    "Skyflakes Crackers": {4800092110016},               # curated survivor; the 150-scan PH,US pack
}

# The version 2 table, as food_schema created it before version 3 existed (kept here by hand
# so that the upgrade tests do not depend on the code under test).
V2_SCHEMA = (
    """CREATE TABLE food_items (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        brand           TEXT,
        group_label     TEXT,
        category        TEXT NOT NULL DEFAULT 'whole_food' CHECK (category IN ('supermarket', 'dining_out', 'whole_food')),
        region_tags     TEXT NOT NULL DEFAULT '',
        barcode         TEXT,
        thumbnail_url   TEXT,
        per_unit        TEXT NOT NULL DEFAULT '100g' CHECK (per_unit IN ('100g', 'serving')),
        calories_kcal   REAL NOT NULL,
        protein_g       REAL,
        carbs_g         REAL,
        sugars_g        REAL,
        fat_g           REAL,
        saturates_g     REAL,
        fibre_g         REAL,
        sodium_mg       REAL,
        portions        TEXT NOT NULL DEFAULT '[]',
        aka             TEXT NOT NULL DEFAULT '[]',
        basis           TEXT,
        source          TEXT NOT NULL,
        source_priority INTEGER NOT NULL,
        source_ref      TEXT,
        popularity      INTEGER NOT NULL DEFAULT 0,
        name_key        TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE UNIQUE INDEX idx_food_items_barcode ON food_items (barcode) WHERE barcode IS NOT NULL",
    "CREATE INDEX idx_food_items_name_key ON food_items (name_key)",
    "CREATE INDEX idx_food_items_category ON food_items (category)",
    "CREATE INDEX idx_food_items_source_priority ON food_items (source_priority)",
    "CREATE UNIQUE INDEX idx_food_items_source_ref ON food_items (source, source_ref)"
    " WHERE source_ref IS NOT NULL",
    "PRAGMA user_version = 2",
)
# (id, name, brand, category, region_tags, barcode, thumbnail_url, calories_kcal, protein_g, carbs_g,
#  fat_g, sodium_mg, portions, aka, basis, source, source_priority, source_ref, popularity, name_key)
V2_ROWS = [
    (3, "Chicken adobo", None, "whole_food", "PH", None, None, 141.0, 15.7, 3.3, 6.5, None,
     '[["1 serving with sauce",152]]', '["adobong manok"]', "database", "curated", 1, "chicken-adobo", 0,
     "|chicken adobo"),
    (4, "Banana, raw", None, "whole_food", "US", None, None, 97.0, 0.74, 23.0, 0.29, 0.0,
     '[["1 banana",126]]', "[]", "database", "usda", 2, "survey:2709224", 0, "|banana raw"),
    (9, "Weetabix Original", "Weetabix", "supermarket", "UK", "5010029000023",
     "https://images.openfoodfacts.org/images/products/501/002/900/0023/front_en.2.200.jpg",
     365.9, 12.0, 69.0, 2.0, 112.0, '[["2 biscuits (37.5 g)",37.5]]', "[]", "database", "off", 3,
     "5010029000023", 420, "weetabix|original"),
]
V2_ROW_COLUMNS = ("id, name, brand, category, region_tags, barcode, thumbnail_url, calories_kcal, protein_g,"
                  " carbs_g, fat_g, sodium_mg, portions, aka, basis, source, source_priority, source_ref,"
                  " popularity, name_key")


# --------------------------------------------------------------------------------------
# shared plumbing
# --------------------------------------------------------------------------------------

_CACHE = {}


def _workdir():
    tmp = _CACHE.get("tmp")
    if tmp is None:
        tmp = tempfile.TemporaryDirectory(prefix="tl_food_tests_", ignore_cleanup_errors=True)
        _CACHE["tmp"] = tmp
    return tmp.name


def tearDownModule():
    tmp = _CACHE.pop("tmp", None)
    _CACHE.clear()
    if tmp is not None:
        tmp.cleanup()


def new_dir(label):
    return tempfile.mkdtemp(prefix=label + "_", dir=_workdir())


def cached(name, build):
    """Run an expensive importer scenario once per session. Failures are remembered too."""
    if name not in _CACHE:
        try:
            _CACHE[name] = build()
        except Exception as exc:
            _CACHE[name] = exc
    value = _CACHE[name]
    if isinstance(value, Exception):
        raise AssertionError("importer scenario %r failed: %s" % (name, value))
    return value


def run_importer(args, isolated=False):
    """Start the importer from the repository root. Returns (exit code, stdout, stderr)."""
    if not os.path.isfile(os.path.join(REPO, IMPORTER)):
        raise AssertionError("scripts/import_foods.py does not exist (yet)")
    cmd = [sys.executable]
    if isolated:
        cmd.append("-I")
    cmd.append(IMPORTER)
    cmd.extend(args)
    proc = subprocess.run(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=300)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def parse_summary(stdout):
    """The last line printed to stdout must be one JSON object."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("the importer printed nothing on stdout")
    try:
        doc = json.loads(lines[-1])
    except ValueError:
        raise AssertionError("the last stdout line is not JSON: %r" % lines[-1][:300])
    if not isinstance(doc, dict):
        raise AssertionError("the last stdout line is not a JSON object: %r" % lines[-1][:300])
    return doc


def import_ok(args, isolated=False):
    code, out, err = run_importer(args, isolated=isolated)
    if code != 0:
        raise AssertionError("the importer exited with code %d\narguments: %s\nstderr (tail):\n%s"
                             % (code, " ".join(args), err[-3000:]))
    return {"summary": parse_summary(out), "stdout": out, "stderr": err}


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def snapshot(path):
    """Every food_items row without its id, in a stable order."""
    conn = open_db(path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info('food_items')") if r[1] != "id"]
        rows = conn.execute("SELECT %s FROM food_items" % ", ".join(cols)).fetchall()
        return sorted((tuple(zip(cols, tuple(r))) for r in rows), key=repr)
    finally:
        conn.close()


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def full_args(db, bundle=None, extra=()):
    args = ["--db", db, "--curated-json", CURATED, "--usda-path", USDA_FLAT,
            "--off-path", OFF_TSV]
    if bundle:
        args += ["--export-app", bundle]
    return args + list(extra)


def full_import():
    """Curated + flat USDA + Open Food Facts in one run, with the app bundle exported."""
    def build():
        d = new_dir("full")
        db = os.path.join(d, "foods.sqlite")
        bundle = os.path.join(d, "food-db.json")
        imp = import_ok(full_args(db, bundle, ["--built", BUILT]))
        imp.update(db=db, bundle=bundle)
        return imp
    return cached("full", build)


def off_only_import():
    def build():
        d = new_dir("off_only")
        db = os.path.join(d, "foods.sqlite")
        imp = import_ok(["--db", db, "--off-path", OFF_TSV, "--quiet"])
        imp.update(db=db)
        return imp
    return cached("off_only", build)


def off_gzip_import():
    def build():
        d = new_dir("off_gzip")
        db = os.path.join(d, "foods.sqlite")
        gz = os.path.join(d, "sample_off.csv.gz")
        with open(OFF_TSV, "rb") as src, gzip.open(gz, "wb") as dst:
            shutil.copyfileobj(src, dst)
        imp = import_ok(["--db", db, "--off-path", gz, "--quiet"])
        imp.update(db=db)
        return imp
    return cached("off_gzip", build)


def off_comma_import():
    """A comma-separated Open Food Facts file that only has a few of the columns."""
    def build():
        d = new_dir("off_comma")
        db = os.path.join(d, "foods.sqlite")
        path = os.path.join(d, "off_commas.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "product_name", "countries_tags", "energy-kcal_100g",
                        "sodium_100g"])
            w.writerow(["5012345678900", "Oat Biscuits, plain", "en:united-kingdom", "450", "0.3"])
            w.writerow(["5012345678917", "Negative Sodium Soup", "en:united-kingdom", "40", "-0.5"])
            w.writerow(["3012345678901", "French Only Jam", "en:france", "250", "0"])
        imp = import_ok(["--db", db, "--off-path", path, "--quiet"])
        imp.update(db=db)
        return imp
    return cached("off_comma", build)


def usda_folder_import():
    def build():
        d = new_dir("usda_folder")
        db = os.path.join(d, "foods.sqlite")
        imp = import_ok(["--db", db, "--usda-path", USDA_FOLDER, "--quiet"])
        imp.update(db=db)
        return imp
    return cached("usda_folder", build)


def rerun_import():
    """The same full command twice on the same database."""
    def build():
        d = new_dir("rerun")
        db = os.path.join(d, "foods.sqlite")
        bundle = os.path.join(d, "food-db.json")
        args = full_args(db, bundle, ["--built", BUILT, "--quiet"])
        first = import_ok(args)
        rows1, bundle1 = snapshot(db), read_json(bundle)
        second = import_ok(args)
        rows2, bundle2 = snapshot(db), read_json(bundle)
        return {"first": first["summary"], "second": second["summary"],
                "rows1": rows1, "rows2": rows2, "bundle1": bundle1, "bundle2": bundle2}
    return cached("rerun", build)


def reversed_runs_import():
    """Lowest priority first, in three separate runs: Open Food Facts, then USDA, then curated."""
    def build():
        d = new_dir("reversed")
        db = os.path.join(d, "foods.sqlite")
        import_ok(["--db", db, "--off-path", OFF_TSV, "--quiet"])
        import_ok(["--db", db, "--usda-path", USDA_FLAT, "--quiet"])
        last = import_ok(["--db", db, "--curated-json", CURATED, "--quiet"])
        last.update(db=db, rows=snapshot(db))
        return last
    return cached("reversed", build)


def quota_import(quota):
    """The full main import, exported with --off-quota (a string such as "US=1,UK=0") and no --built."""
    def build():
        d = new_dir("quota")
        db = os.path.join(d, "foods.sqlite")
        bundle = os.path.join(d, "food-db.json")
        imp = import_ok(full_args(db, bundle, ["--off-quota", quota, "--quiet"]))
        imp.update(db=db, bundle=bundle, doc=read_json(bundle))
        return imp
    return cached("quota:" + quota, build)


def rules_import():
    """off_rules.csv alone (one row per validation branch), no export."""
    def build():
        d = new_dir("rules")
        db = os.path.join(d, "foods.sqlite")
        imp = import_ok(["--db", db, "--off-path", OFF_RULES, "--quiet"])
        imp.update(db=db)
        return imp
    return cached("rules", build)


def dedupe_import():
    """off_dedupe.csv with the flat USDA file (which holds the Skippy twin), exported."""
    def build():
        d = new_dir("dedupe")
        db = os.path.join(d, "foods.sqlite")
        bundle = os.path.join(d, "food-db.json")
        args = ["--db", db, "--usda-path", USDA_FLAT, "--off-path", OFF_DEDUPE, "--export-app", bundle,
                "--built", BUILT, "--quiet"]
        first = import_ok(args)
        rows1, doc1 = snapshot(db), read_json(bundle)
        second = import_ok(args)
        rows2, doc2 = snapshot(db), read_json(bundle)
        first.update(db=db, bundle=bundle, doc=doc1, rows1=rows1, rows2=rows2, doc2=doc2,
                     summary2=second["summary"])
        return first
    return cached("dedupe", build)


def alias_guard_import():
    """off_dedupe.csv, then off_alias_guard.csv (a row whose barcode an earlier merge stored as an alias)."""
    def build():
        d = new_dir("guard")
        db = os.path.join(d, "foods.sqlite")
        bundle = os.path.join(d, "food-db.json")
        import_ok(["--db", db, "--off-path", OFF_DEDUPE, "--quiet"])
        imp = import_ok(["--db", db, "--off-path", OFF_GUARD, "--export-app", bundle, "--quiet"])
        imp.update(db=db, bundle=bundle, doc=read_json(bundle))
        return imp
    return cached("guard", build)


def old_rules_database():
    """A database whose Open Food Facts rows all have completeness NULL (as the old rules left them)."""
    def build():
        d = new_dir("old_rules")
        db = os.path.join(d, "foods.sqlite")
        shutil.copyfile(full_import()["db"], db)
        conn = open_db(db)
        try:
            conn.execute("UPDATE food_items SET completeness = NULL WHERE source = 'off'")
            conn.commit()
        finally:
            conn.close()
        return {"db": db, "dir": d}
    return cached("old_rules_db", build)


class DbCase(unittest.TestCase):
    """Read-only access to the database written by one shared importer scenario."""

    @classmethod
    def load(cls):
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        cls.imp = cls.load()
        cls.summary = cls.imp["summary"]
        cls.db = open_db(cls.imp["db"])

    @classmethod
    def tearDownClass(cls):
        db = getattr(cls, "db", None)
        if db is not None:
            db.close()
            cls.db = None

    def q(self, sql, params=()):
        return self.db.execute(sql, params).fetchall()

    def n_rows(self, where="1 = 1", params=()):
        return self.db.execute("SELECT COUNT(*) FROM food_items WHERE " + where, params).fetchone()[0]

    def one_row(self, where, params=()):
        found = self.q("SELECT * FROM food_items WHERE " + where, params)
        self.assertEqual(len(found), 1, "expected exactly one food_items row WHERE %s with %r, found %d"
                         % (where, params, len(found)))
        return found[0]

    def off_row(self, source_ref):
        return self.one_row("source = 'off' AND source_ref = ?", (source_ref,))

    def usda_row(self, source_ref):
        return self.one_row("source = 'usda' AND source_ref = ?", (source_ref,))

    def curated_row(self, name):
        return self.one_row("source = 'curated' AND name = ?", (name,))

    def near(self, actual, expected, places=1):
        self.assertIsNotNone(actual, "expected about %s but the stored value is NULL" % expected)
        self.assertAlmostEqual(float(actual), float(expected), places=places)

    def portions_of(self, row):
        return json.loads(row["portions"])

    def absent(self, name=None, codes=()):
        if name is not None:
            self.assertEqual(self.n_rows("name = ?", (name,)), 0,
                             "%r must not be in food_items" % name)
        for code in codes:
            self.assertEqual(self.n_rows("barcode = ? OR source_ref = ?", (code, code)), 0,
                             "no row may carry the code %r" % code)


class FullImportCase(DbCase):
    @classmethod
    def load(cls):
        return full_import()


# --------------------------------------------------------------------------------------
# the fixtures themselves
# --------------------------------------------------------------------------------------

class FixtureFileTests(unittest.TestCase):
    """The fixtures parse and hold the rows every other test counts on."""

    def off_rows(self):
        with open(OFF_TSV, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f, delimiter="\t"))

    def usda_rows(self):
        with open(USDA_FLAT, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def curated_items(self):
        return read_json(CURATED)["items"]

    def test_off_fixture_is_tab_separated_with_every_contract_column(self):
        with open(OFF_TSV, encoding="utf-8", newline="") as f:
            header = f.readline().rstrip("\r\n").split("\t")
        for col in OFF_COLUMNS:
            self.assertIn(col, header)

    def test_off_fixture_has_52_complete_rows(self):
        rows = self.off_rows()
        self.assertEqual(len(rows), 52)
        for row in rows:
            self.assertNotIn(None, row, "a row has more cells than the header")
            self.assertNotIn(None, row.values(), "a row has fewer cells than the header")

    def test_rule_fixtures_have_their_row_counts_and_complete_rows(self):
        for path, count in ((OFF_RULES, 36), (OFF_DEDUPE, 15), (OFF_GUARD, 1)):
            with self.subTest(file=os.path.basename(path)):
                with open(path, encoding="utf-8", newline="") as f:
                    header = f.readline().rstrip("\r\n").split("\t")
                    f.seek(0)
                    rows = list(csv.DictReader(f, delimiter="\t"))
                for col in OFF_COLUMNS:
                    self.assertIn(col, header)
                self.assertEqual(len(rows), count)
                for row in rows:
                    self.assertNotIn(None, row)
                    self.assertNotIn(None, row.values())

    def test_the_csv_fixtures_are_exactly_what_the_generator_writes(self):
        out = new_dir("fixture_check")
        proc = subprocess.run([sys.executable, "-B", os.path.join(FIXTURES, "make_off_fixtures.py"), out],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        for name in ("sample_off.csv", "off_rules.csv", "off_dedupe.csv", "off_alias_guard.csv"):
            with self.subTest(file=name):
                with open(os.path.join(out, name), "rb") as made, open(os.path.join(FIXTURES, name), "rb") as kept:
                    # line endings may differ after a git checkout on Windows
                    self.assertEqual(made.read().replace(b"\r\n", b"\n"), kept.read().replace(b"\r\n", b"\n"))

    def test_selection_anchors_carry_the_columns_a_pool_needs(self):
        # A1, A2, A4 and A5 must stay selected: completeness, scans, photo, Latin brand, three macros.
        by_code = {r["code"]: r for r in self.off_rows()}
        for code in ("4807770270123", "5000159407236", "8801043014830", "4901330502881"):
            with self.subTest(code=code):
                row = by_code[code]
                for col in ("completeness", "unique_scans_n", "image_front_small_url", "brands",
                            "proteins_100g", "carbohydrates_100g", "fat_100g"):
                    self.assertNotEqual(row[col], "", col)
                self.assertNotIn("en:france", row["countries_tags"])

    def test_off_fixture_holds_the_six_anchor_rows(self):
        by_code = {}
        for row in self.off_rows():
            by_code.setdefault(row["code"], row)
        expected = {"4807770270123": ("Pancit Canton Kalamansi", "Lucky Me!,Monde Nissin", "en:philippines"),
                    "5000159407236": ("Snickers", "Mars", "en:united-kingdom"),
                    "016000275287": ("Cheerios", "General Mills", "en:united-states"),
                    "8801043014830": ("Shin Ramyun", "Nongshim", "en:south-korea,en:united-states"),
                    "4901330502881": ("Pocky Chocolate", "Glico", "en:japan"),
                    "3017620422003": ("Nutella", "Ferrero", "en:france")}
        for code, (name, brands, countries) in expected.items():
            with self.subTest(code=code):
                row = by_code[code]
                self.assertEqual((row["product_name"], row["brands"], row["countries_tags"]),
                                 (name, brands, countries))

    def test_off_anchor_a2_has_kilojoules_and_salt_only(self):
        row = [r for r in self.off_rows() if r["code"] == "5000159407236"][0]
        self.assertEqual((row["energy-kcal_100g"], row["energy_100g"], row["sodium_100g"], row["salt_100g"]),
                         ("", "2029", "", "0.63"))

    def test_usda_fixture_has_the_contract_header_and_13_rows(self):
        with open(USDA_FLAT, encoding="utf-8", newline="") as f:
            header = next(csv.reader(f))
        self.assertEqual(header, USDA_FLAT_COLUMNS)
        self.assertEqual(len(self.usda_rows()), 13)

    def test_usda_fixture_holds_the_anchor_rows(self):
        by_id = {r["fdc_id"]: r for r in self.usda_rows()}
        self.assertEqual((by_id["2709224"]["description"], by_id["2709224"]["data_type"]),
                         ("Banana, raw", "survey_fndds_food"))
        self.assertEqual((by_id["2499999"]["data_type"], by_id["2499999"]["gtin_upc"]),
                         ("branded_food", "00016000275287"))
        self.assertEqual((by_id["2706000"]["description"], by_id["2706000"]["food_category"]),
                         ("Big Mac (McDonalds)", "Burgers"))
        self.assertEqual((by_id["171688"]["energy_kcal"], by_id["171688"]["energy_kj"]), ("", "1590"))

    def test_curated_fixture_is_json_with_11_items(self):
        doc = read_json(CURATED)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(len(doc["items"]), 11)

    def test_curated_fixture_holds_the_four_anchor_rows(self):
        by_name = {i["name"]: i for i in self.curated_items()}
        c1 = by_name["Chickenjoy 1 pc (thigh)"]
        self.assertEqual((c1["brand"], c1["category"], c1["serving"], c1["nutrition_per"], c1["calories_kcal"]),
                         ("Jollibee", "dining_out", {"label": "1 thigh", "grams": 125}, "serving", 380))
        c2 = by_name["Chicken adobo"]
        self.assertEqual((c2["brand"], c2["group"], c2["nutrition_per"], c2["calories_kcal"]),
                         (None, "Home dish", "100g", 141))
        c3 = by_name["Lucky Me! Pancit Canton Kalamansi"]
        self.assertEqual((c3["barcode"], c3["thumbnail_url"], c3["calories_kcal"], c3["sodium_mg"]),
                         ("4807770270123", None, 280, 900))
        c4 = by_name["Flat White (tall)"]
        self.assertEqual((c4["region_tags"], c4["serving"]["grams"], c4["nutrition_per"]),
                         (["PH", "US"], None, "serving"))

    def test_usda_folder_fixture_has_the_four_files_and_five_foods(self):
        for name in ("food.csv", "food_nutrient.csv", "food_portion.csv", "food_category.csv"):
            self.assertTrue(os.path.isfile(os.path.join(USDA_FOLDER, name)), name)
        with open(os.path.join(USDA_FOLDER, "food.csv"), encoding="utf-8", newline="") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 5)


# --------------------------------------------------------------------------------------
# food_schema: constants and pure helpers
# --------------------------------------------------------------------------------------

class SchemaModuleCase(unittest.TestCase):
    def setUp(self):
        if food_schema is None:
            self.fail("scripts/food_schema.py could not be imported: %r" % (_SCHEMA_IMPORT_ERROR,))


class SchemaConstantsTests(SchemaModuleCase):
    def test_latest_version_is_3(self):
        self.assertEqual(food_schema.LATEST_VERSION, 3)

    def test_categories(self):
        self.assertEqual(tuple(food_schema.CATEGORIES), ("supermarket", "dining_out", "whole_food"))

    def test_regions_in_fixed_order(self):
        self.assertEqual(tuple(food_schema.REGIONS), REGION_ORDER)

    def test_source_priority(self):
        self.assertEqual(dict(food_schema.SOURCE_PRIORITY), {"curated": 1, "usda": 2, "off": 3})


class NormalizeBarcodeTests(SchemaModuleCase):
    def test_upc_a_12_digits_is_padded_to_13(self):
        self.assertEqual(food_schema.normalize_barcode("016000275287"), "0016000275287")

    def test_13_digit_form_of_the_same_upc_is_unchanged(self):
        self.assertEqual(food_schema.normalize_barcode("0016000275287"), "0016000275287")

    def test_gtin_14_form_of_the_same_upc_gives_the_same_value(self):
        self.assertEqual(food_schema.normalize_barcode("00016000275287"), "0016000275287")

    def test_ean_13_is_unchanged(self):
        self.assertEqual(food_schema.normalize_barcode("4807770270123"), "4807770270123")

    def test_only_digits_are_kept(self):
        self.assertEqual(food_schema.normalize_barcode(" 4807-7702 70123\t"), "4807770270123")

    def test_ean_8_is_left_padded_to_13(self):
        self.assertEqual(food_schema.normalize_barcode("12345678"), "0000012345678")

    def test_fewer_than_8_digits_is_none(self):
        self.assertIsNone(food_schema.normalize_barcode("1234567"))

    def test_more_than_14_digits_is_none(self):
        self.assertIsNone(food_schema.normalize_barcode("123456789012345"))

    def test_all_zeros_is_none(self):
        self.assertIsNone(food_schema.normalize_barcode("00000000"))
        self.assertIsNone(food_schema.normalize_barcode("0000000000000"))

    def test_empty_text_is_none(self):
        self.assertIsNone(food_schema.normalize_barcode(""))
        self.assertIsNone(food_schema.normalize_barcode("n/a"))

    def test_none_is_none(self):
        self.assertIsNone(food_schema.normalize_barcode(None))

    def test_14_digits_without_leading_zero_stay_14(self):
        self.assertEqual(food_schema.normalize_barcode("10012345678902"), "10012345678902")

    def test_14_digits_with_one_leading_zero_become_13(self):
        self.assertEqual(food_schema.normalize_barcode("04807770270123"), "4807770270123")

    def test_in_store_code_starting_with_2_is_none(self):
        self.assertIsNone(food_schema.normalize_barcode("2000000012345"))

    def test_in_store_code_starting_with_02_is_none(self):
        self.assertIsNone(food_schema.normalize_barcode("0200123456789"))

    def test_12_digit_code_starting_with_2_is_none(self):
        # canonical form would be 0200123456789, which starts with "02"
        self.assertIsNone(food_schema.normalize_barcode("200123456789"))

    def test_result_is_text(self):
        self.assertIsInstance(food_schema.normalize_barcode("5000159407236"), str)


class BarcodeOriginTests(SchemaModuleCase):
    """barcode_origin(canonical barcode): the market the prefix was registered in, or None.

    PH 480; JP 450-459 and 490-499; KR 880; UK 500-509; US 000-019, 030-039 and 060-139;
    AS (the Philippines' neighbour markets) 885, 955, 888, 899, 893, 489, 471 and 690-699.
    A 14-digit code loses its first digit; an EAN-8 (canonical form 00000 + 8 digits) is read
    from its own first three digits and is only accepted for PH, JP and KR.
    """

    def origin(self, barcode):
        if not hasattr(food_schema, "barcode_origin"):
            self.fail("food_schema.barcode_origin does not exist")
        return food_schema.barcode_origin(barcode)

    def check(self, expected):
        for barcode, origin in expected.items():
            with self.subTest(barcode=barcode):
                self.assertEqual(self.origin(barcode), origin)

    def test_prefix_480_is_the_philippines(self):
        self.check({"4807770270123": "PH", "4800016644504": "PH"})

    def test_prefixes_next_to_480_are_not_the_philippines(self):
        self.check({"4797770270123": None, "4817770270123": None})

    def test_prefixes_450_to_459_are_japan(self):
        self.check({"4501234567890": "JP", "4514603365511": "JP", "4591234567890": "JP"})

    def test_prefixes_490_to_499_are_japan(self):
        self.check({"4901330502881": "JP", "4987035332510": "JP", "4991234567890": "JP"})

    def test_prefixes_460_to_479_are_not_japan(self):
        self.check({"4491234567890": None, "4601234000019": None, "4701234567890": None})

    def test_prefix_880_is_south_korea(self):
        self.check({"8801043014830": "KR"})

    def test_prefixes_next_to_880_are_not_south_korea(self):
        self.check({"8791234567890": None, "8811234567890": None})

    def test_prefixes_500_to_509_are_the_united_kingdom(self):
        self.check({"5000159407236": "UK", "5010029000023": "UK", "5091234567890": "UK"})

    def test_prefix_510_is_not_the_united_kingdom(self):
        self.check({"5101234567890": None})

    def test_prefixes_000_to_019_are_the_united_states(self):
        # a 12-digit UPC-A in its canonical 13-digit form starts with one extra zero
        self.check({"0016000275287": "US", "0037600105002": "US", "0096619123452": "US",
                    "0191234567890": "US"})

    def test_prefixes_030_to_039_are_the_united_states(self):
        self.check({"0301234567890": "US", "0391234567890": "US"})

    def test_prefixes_060_to_139_are_the_united_states(self):
        self.check({"0601234567890": "US", "0811620020015": "US", "1001234567890": "US",
                    "1391234567890": "US"})

    def test_prefixes_between_the_united_states_ranges_are_none(self):
        self.check({"0291234567890": None, "0401234567890": None, "0591234567890": None,
                    "1401234567890": None})

    def test_asian_neighbour_prefixes(self):
        self.check({"8851234567890": "AS",     # Thailand
                    "9551234567890": "AS",     # Malaysia
                    "8881234567890": "AS",     # Singapore
                    "8996001600269": "AS",     # Indonesia
                    "8931234567890": "AS",     # Vietnam
                    "4891234567890": "AS",     # Hong Kong
                    "4711234567890": "AS"})    # Taiwan

    def test_prefixes_690_to_699_are_asian_neighbours(self):
        self.check({"6901234567890": "AS", "6951234567890": "AS", "6991234567890": "AS"})

    def test_prefixes_next_to_the_asian_ones_are_none(self):
        self.check({"6891234567890": None, "7001234567890": None, "8841234567890": None,
                    "8861234567890": None})

    def test_other_prefixes_are_none(self):
        self.check({"3017620422003": None,     # France
                    "4000417025005": None,     # Germany
                    "8000500066027": None,     # Italy
                    "5449000054227": None,     # Belgium
                    "6111259347861": None})    # Morocco

    def test_14_digits_drop_the_first_digit(self):
        self.check({"14807770270123": "PH", "18801043014830": "KR", "10012345678902": "US",
                    "13017620422003": None})

    def test_ean_8_uses_its_own_first_three_digits(self):
        self.check({"0000048012345": "PH", "0000049123456": "JP", "0000045123456": "JP",
                    "0000088012345": "KR"})

    def test_ean_8_is_only_accepted_for_ph_jp_and_kr(self):
        self.check({"0000050123456": None,     # 501 would be the United Kingdom
                    "0000001234565": None,     # 012 would be the United States
                    "0000088512345": None,     # 885 would be an Asian neighbour
                    "0000030123456": None})

    def test_none_is_none(self):
        self.assertIsNone(self.origin(None))

    def test_normalised_upc_a_is_the_united_states(self):
        self.assertEqual(self.origin(food_schema.normalize_barcode("016000275287")), "US")


class NameKeyTests(SchemaModuleCase):
    def test_contract_example_with_the_brand_inside_the_name(self):
        self.assertEqual(food_schema.name_key("Lucky Me! Pancit Canton", "Lucky Me!"),
                         "lucky me|pancit canton")

    def test_contract_example_without_the_brand_inside_the_name(self):
        self.assertEqual(food_schema.name_key("Pancit Canton", "Lucky Me"), "lucky me|pancit canton")

    def test_no_brand_gives_an_empty_brand_part(self):
        self.assertEqual(food_schema.name_key("Banana, raw"), "|banana raw")
        self.assertEqual(food_schema.name_key("Banana, raw", None), "|banana raw")

    def test_empty_brand_counts_as_no_brand(self):
        self.assertEqual(food_schema.name_key("Banana", ""), "|banana")

    def test_case_is_ignored(self):
        self.assertEqual(food_schema.name_key("PANCIT CANTON", "LUCKY ME"), "lucky me|pancit canton")

    def test_accents_are_removed(self):
        self.assertEqual(food_schema.name_key("Crème Brûlée", "Nestlé"), "nestle|creme brulee")

    def test_ampersand_becomes_and(self):
        self.assertEqual(food_schema.name_key("Mac & Cheese"), "|mac and cheese")

    def test_apostrophes_are_deleted(self):
        self.assertEqual(food_schema.name_key("Wendy's Chili"), "|wendys chili")

    def test_full_stops_are_deleted(self):
        self.assertEqual(food_schema.name_key("SkyFlakes Crackers", "M.Y. San"),
                         "my san|skyflakes crackers")

    def test_other_punctuation_runs_become_one_space(self):
        self.assertEqual(food_schema.name_key("Chickenjoy 1 pc (thigh)", "Jollibee"),
                         "jollibee|chickenjoy 1 pc thigh")
        self.assertEqual(food_schema.name_key("Coca-Cola Zero", "Coca-Cola"), "coca cola|zero")

    def test_whitespace_is_trimmed_and_collapsed(self):
        self.assertEqual(food_schema.name_key("  Chicken   Adobo  "), "|chicken adobo")

    def test_ampersand_and_apostrophe_together(self):
        self.assertEqual(food_schema.name_key("M&M's Peanut", "M&M's"), "m and ms|peanut")

    def test_brand_prefix_is_removed_when_spelled_differently(self):
        self.assertEqual(food_schema.name_key("Jack 'n Jill Chippy BBQ", "Jack n Jill"),
                         "jack n jill|chippy bbq")

    def test_brand_prefix_must_be_followed_by_a_space(self):
        self.assertEqual(food_schema.name_key("Luckyme Noodles", "Lucky"), "lucky|luckyme noodles")

    def test_name_equal_to_brand_leaves_an_empty_name_part(self):
        self.assertEqual(food_schema.name_key("Cheerios", "Cheerios"), "cheerios|")

    def test_names_in_other_scripts_stay_distinct(self):
        # letters of any script are alphanumeric: two Korean product names must not both
        # collapse to an empty name part
        first = food_schema.name_key("신라면", "농심")
        second = food_schema.name_key("너구리", "농심")
        self.assertNotEqual(first, second)
        self.assertFalse(first.endswith("|"), first)


class UnitHelperTests(SchemaModuleCase):
    def test_kcal_from_kj(self):
        self.assertAlmostEqual(food_schema.kcal_from_kj(4184), 1000.0, places=6)

    def test_kcal_from_kj_snickers(self):
        self.assertAlmostEqual(food_schema.kcal_from_kj(2029), 484.9, places=1)

    def test_sodium_mg_from_salt_g(self):
        self.assertAlmostEqual(food_schema.sodium_mg_from_salt_g(0.63), 252.0, places=6)
        self.assertAlmostEqual(food_schema.sodium_mg_from_salt_g(2.5), 1000.0, places=6)

    def test_salt_g_from_sodium_mg(self):
        self.assertAlmostEqual(food_schema.salt_g_from_sodium_mg(1000), 2.5, places=6)
        self.assertAlmostEqual(food_schema.salt_g_from_sodium_mg(252), 0.63, places=6)

    def test_salt_and_sodium_round_trip(self):
        self.assertAlmostEqual(
            food_schema.salt_g_from_sodium_mg(food_schema.sodium_mg_from_salt_g(3.75)), 3.75, places=6)


# --------------------------------------------------------------------------------------
# food_schema: migrations
# --------------------------------------------------------------------------------------

V1_ROWS = [
    (1, "Banana, raw", "Bananas", 97.0, 0.74, 23.0, 0.29, '[["1 banana", 126]]', "usda", "survey:2709224"),
    (2, "Chicken adobo", "Home dish", 141.0, 15.7, 3.3, 6.5, '[["1 serving with sauce", 152]]', "curated", "chicken-adobo"),
    (7, "Shin Ramyun", None, 433.0, 9.2, 65.0, None, "[]", "off", "8801043014830"),
    (12, "Mystery, no macros", None, 250.0, None, None, None, "[]", "usda", None),
]


def dict_rows(conn, sql, params=()):
    """Rows as dicts, whatever row_factory the connection has."""
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, tuple(r))) for r in cur.fetchall()]


def column_names(conn):
    return [tuple(r)[1] for r in conn.execute("PRAGMA table_info('food_items')").fetchall()]


def user_version(conn):
    return tuple(conn.execute("PRAGMA user_version").fetchone())[0]


def schema_sql(conn):
    return sorted(tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall())


class SchemaCase(SchemaModuleCase):
    """A fresh database file per test."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="tl_schema_", ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "foods.sqlite")

    def connect(self):
        conn = food_schema.connect(self.db_path)
        self.addCleanup(conn.close)   # runs before the folder is removed
        return conn

    def fresh_v2(self):
        """A brand-new file brought to the latest version (the name is historic)."""
        conn = self.connect()
        food_schema.migrate(conn)
        return conn

    def v2_with_rows(self):
        """A version 2 file as the scripts wrote it before version 3 existed, with three rows."""
        conn = self.connect()
        for statement in V2_SCHEMA:
            conn.execute(statement)
        marks = ", ".join("?" for _ in V2_ROWS[0])
        conn.executemany("INSERT INTO food_items (%s) VALUES (%s)" % (V2_ROW_COLUMNS, marks), V2_ROWS)
        conn.commit()
        return conn

    def upgraded_from_v2(self):
        conn = self.v2_with_rows()
        food_schema.migrate(conn)
        return conn

    def v1_with_rows(self):
        conn = self.connect()
        food_schema.create_v1(conn)
        conn.executemany(
            "INSERT INTO food_items (id, name, group_label, kcal, protein, carbs, fat, portions, source, source_ref)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", V1_ROWS)
        conn.commit()
        return conn

    def upgraded(self):
        conn = self.v1_with_rows()
        food_schema.migrate(conn)
        return conn

    def by_id(self, conn):
        return {r["id"]: r for r in dict_rows(conn, "SELECT * FROM food_items")}


class FreshDatabaseMigrationTests(SchemaCase):
    def test_connect_uses_sqlite_row(self):
        conn = self.connect()
        self.assertIs(conn.row_factory, sqlite3.Row)

    def test_fresh_database_reaches_version_3(self):
        conn = self.connect()
        self.assertEqual(food_schema.migrate(conn), 3)
        self.assertEqual(user_version(conn), 3)

    def test_fresh_database_has_every_version_3_column(self):
        cols = column_names(self.fresh_v2())
        self.assertEqual(sorted(set(V3_COLUMNS) - set(cols)), [], "missing columns")

    def test_fresh_database_has_no_other_columns(self):
        self.assertEqual(sorted(column_names(self.fresh_v2())), sorted(V3_COLUMNS))

    def test_new_columns_have_the_documented_types(self):
        conn = self.fresh_v2()
        info = {tuple(r)[1]: tuple(r) for r in conn.execute("PRAGMA table_info('food_items')").fetchall()}
        # (cid, name, type, notnull, default, pk)
        self.assertEqual((info["foreign_tags"][2].upper(), info["foreign_tags"][3]), ("INTEGER", 1))
        self.assertEqual((info["completeness"][2].upper(), info["completeness"][3]), ("INTEGER", 0))
        self.assertEqual((info["alt_barcodes"][2].upper(), info["alt_barcodes"][3]), ("TEXT", 1))

    def test_fresh_database_has_no_version_1_nutrient_columns(self):
        cols = column_names(self.fresh_v2())
        for old in ("kcal", "protein", "carbs", "fat"):
            self.assertNotIn(old, cols)

    def test_fresh_database_starts_empty(self):
        conn = self.fresh_v2()
        self.assertEqual(dict_rows(conn, "SELECT COUNT(*) AS n FROM food_items")[0]["n"], 0)

    def test_migrating_twice_changes_nothing(self):
        conn = self.fresh_v2()
        before = schema_sql(conn)
        self.assertEqual(food_schema.migrate(conn), 3)
        self.assertEqual(schema_sql(conn), before)
        self.assertEqual(user_version(conn), 3)

    def test_version_survives_closing_and_reopening(self):
        conn = food_schema.connect(self.db_path)
        food_schema.migrate(conn)
        conn.close()
        again = self.connect()
        self.assertEqual(user_version(again), 3)
        self.assertIn("calories_kcal", column_names(again))
        self.assertIn("alt_barcodes", column_names(again))


class Version1UpgradeTests(SchemaCase):
    def test_create_v1_makes_the_old_shape(self):
        conn = self.connect()
        food_schema.create_v1(conn)
        self.assertEqual(column_names(conn), V1_COLUMNS)
        self.assertEqual(user_version(conn), 1)

    def test_upgrade_returns_and_stores_version_3(self):
        conn = self.v1_with_rows()
        self.assertEqual(food_schema.migrate(conn), 3)
        self.assertEqual(user_version(conn), 3)

    def test_upgrade_keeps_every_row(self):
        conn = self.upgraded()
        self.assertEqual(dict_rows(conn, "SELECT COUNT(*) AS n FROM food_items")[0]["n"], len(V1_ROWS))

    def test_upgrade_keeps_ids(self):
        rows = self.by_id(self.upgraded())
        self.assertEqual(sorted(rows), [1, 2, 7, 12])
        self.assertEqual({i: rows[i]["name"] for i in rows},
                         {1: "Banana, raw", 2: "Chicken adobo", 7: "Shin Ramyun", 12: "Mystery, no macros"})

    def test_upgrade_moves_nutrients_to_the_new_column_names(self):
        rows = self.by_id(self.upgraded())
        for rid, _name, _group, kcal, protein, carbs, fat, _p, _s, _ref in V1_ROWS:
            with self.subTest(id=rid):
                row = rows[rid]
                self.assertAlmostEqual(row["calories_kcal"], kcal, places=6)
                for col, old in (("protein_g", protein), ("carbs_g", carbs), ("fat_g", fat)):
                    if old is None:
                        self.assertIsNone(row[col], col)
                    else:
                        self.assertAlmostEqual(row[col], old, places=6, msg=col)

    def test_upgrade_drops_the_old_column_names(self):
        cols = column_names(self.upgraded())
        for old in ("kcal", "protein", "carbs", "fat"):
            self.assertNotIn(old, cols)

    def test_upgrade_has_every_version_3_column(self):
        cols = column_names(self.upgraded())
        self.assertEqual(sorted(set(V3_COLUMNS) - set(cols)), [], "missing columns")

    def test_upgrade_gives_old_rows_the_new_column_defaults(self):
        rows = self.by_id(self.upgraded())
        for rid, row in rows.items():
            with self.subTest(id=rid):
                self.assertEqual((row["foreign_tags"], row["completeness"], row["alt_barcodes"]),
                                 (0, None, "[]"))

    def test_upgrade_keeps_group_portions_source_and_source_ref(self):
        rows = self.by_id(self.upgraded())
        for rid, _name, group, _k, _p, _c, _f, portions, source, ref in V1_ROWS:
            with self.subTest(id=rid):
                row = rows[rid]
                self.assertEqual(row["group_label"], group)
                self.assertEqual(json.loads(row["portions"]), json.loads(portions))
                self.assertEqual(row["source"], source)
                self.assertEqual(row["source_ref"], ref)

    def test_upgrade_backfills_source_priority(self):
        rows = self.by_id(self.upgraded())
        self.assertEqual({i: rows[i]["source_priority"] for i in rows}, {1: 2, 2: 1, 7: 3, 12: 2})

    def test_upgrade_backfills_category_whole_food(self):
        rows = self.by_id(self.upgraded())
        self.assertEqual({r["category"] for r in rows.values()}, {"whole_food"})

    def test_upgrade_backfills_region_us_for_usda_rows_only(self):
        rows = self.by_id(self.upgraded())
        self.assertEqual({i: rows[i]["region_tags"] for i in rows}, {1: "US", 2: "", 7: "", 12: "US"})

    def test_upgrade_backfills_per_unit_100g(self):
        rows = self.by_id(self.upgraded())
        self.assertEqual({r["per_unit"] for r in rows.values()}, {"100g"})

    def test_upgrade_backfills_aka_empty_list(self):
        rows = self.by_id(self.upgraded())
        self.assertEqual({r["aka"] for r in rows.values()}, {"[]"})

    def test_upgrade_backfills_name_key(self):
        rows = self.by_id(self.upgraded())
        self.assertEqual({i: rows[i]["name_key"] for i in rows},
                         {1: "|banana raw", 2: "|chicken adobo", 7: "|shin ramyun",
                          12: "|mystery no macros"})

    def test_upgrade_leaves_the_new_optional_columns_empty(self):
        rows = self.by_id(self.upgraded())
        for rid, row in rows.items():
            with self.subTest(id=rid):
                for col in ("brand", "barcode", "thumbnail_url", "sugars_g", "saturates_g",
                            "fibre_g", "sodium_mg"):
                    self.assertIsNone(row[col], col)
                self.assertEqual(row["popularity"], 0)

    def test_upgrade_is_idempotent(self):
        conn = self.upgraded()
        before_rows = dict_rows(conn, "SELECT * FROM food_items ORDER BY id")
        before_schema = schema_sql(conn)
        self.assertEqual(food_schema.migrate(conn), 3)
        self.assertEqual(dict_rows(conn, "SELECT * FROM food_items ORDER BY id"), before_rows)
        self.assertEqual(schema_sql(conn), before_schema)

    def test_upgrade_is_committed(self):
        conn = food_schema.connect(self.db_path)
        food_schema.create_v1(conn)
        conn.executemany(
            "INSERT INTO food_items (id, name, group_label, kcal, protein, carbs, fat, portions, source, source_ref)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", V1_ROWS)
        conn.commit()
        food_schema.migrate(conn)
        conn.close()
        again = self.connect()
        self.assertEqual(user_version(again), 3)
        self.assertEqual(sorted(self.by_id(again)), [1, 2, 7, 12])

    def test_rows_added_after_the_upgrade_get_new_ids(self):
        conn = self.upgraded()
        cur = conn.execute("INSERT INTO food_items (name, calories_kcal, source, source_priority)"
                           " VALUES ('New food', 10, 'curated', 1)")
        self.assertGreater(cur.lastrowid, 12)


class Version2UpgradeTests(SchemaCase):
    """A version 2 file (the layout before foreign_tags, completeness and alt_barcodes)."""

    def test_the_hand_built_file_is_a_version_2_file(self):
        conn = self.v2_with_rows()
        self.assertEqual(user_version(conn), 2)
        self.assertEqual(column_names(conn), V2_COLUMNS)

    def test_upgrade_returns_and_stores_version_3(self):
        conn = self.v2_with_rows()
        self.assertEqual(food_schema.migrate(conn), 3)
        self.assertEqual(user_version(conn), 3)

    def test_upgrade_adds_the_three_new_columns(self):
        cols = column_names(self.upgraded_from_v2())
        self.assertEqual(sorted(set(V3_COLUMNS) - set(cols)), [], "missing columns")

    def test_upgrade_keeps_every_row_and_its_id(self):
        rows = self.by_id(self.upgraded_from_v2())
        self.assertEqual({i: rows[i]["name"] for i in rows},
                         {3: "Chicken adobo", 4: "Banana, raw", 9: "Weetabix Original"})

    def test_upgrade_keeps_every_old_value(self):
        rows = self.by_id(self.upgraded_from_v2())
        names = [c.strip() for c in V2_ROW_COLUMNS.split(",")]
        for old in V2_ROWS:
            with self.subTest(id=old[0]):
                row = rows[old[0]]
                self.assertEqual(tuple(row[c] for c in names), old)

    def test_old_rows_get_foreign_tags_zero(self):
        rows = self.by_id(self.upgraded_from_v2())
        self.assertEqual({r["foreign_tags"] for r in rows.values()}, {0})

    def test_old_rows_get_no_completeness(self):
        rows = self.by_id(self.upgraded_from_v2())
        self.assertEqual({r["completeness"] for r in rows.values()}, {None})

    def test_old_rows_get_an_empty_alt_barcodes_list(self):
        rows = self.by_id(self.upgraded_from_v2())
        self.assertEqual({r["alt_barcodes"] for r in rows.values()}, {"[]"})

    def test_upgrade_is_idempotent(self):
        conn = self.upgraded_from_v2()
        before_rows = dict_rows(conn, "SELECT * FROM food_items ORDER BY id")
        before_schema = schema_sql(conn)
        self.assertEqual(food_schema.migrate(conn), 3)
        self.assertEqual(dict_rows(conn, "SELECT * FROM food_items ORDER BY id"), before_rows)
        self.assertEqual(schema_sql(conn), before_schema)

    def test_upgrade_is_committed(self):
        conn = self.v2_with_rows()
        food_schema.migrate(conn)
        conn.close()
        again = food_schema.connect(self.db_path)
        self.addCleanup(again.close)
        self.assertEqual(user_version(again), 3)
        self.assertEqual(sorted(self.by_id(again)), [3, 4, 9])

    def test_rows_added_after_the_upgrade_get_new_ids(self):
        conn = self.upgraded_from_v2()
        cur = conn.execute("INSERT INTO food_items (name, calories_kcal, source, source_priority)"
                           " VALUES ('New food', 10, 'curated', 1)")
        self.assertGreater(cur.lastrowid, 9)

    def test_an_upgraded_and_a_fresh_database_have_the_same_columns(self):
        upgraded = column_names(self.upgraded_from_v2())
        tmp = tempfile.TemporaryDirectory(prefix="tl_schema_", ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        other = food_schema.connect(os.path.join(tmp.name, "fresh.sqlite"))
        self.addCleanup(other.close)
        food_schema.migrate(other)
        self.assertEqual(sorted(upgraded), sorted(column_names(other)))


class _ConstraintChecks:
    """Constraints and indexes of version 2. Mixed into one class per way of getting there."""

    def make_db(self):
        raise NotImplementedError

    def insert(self, conn, **over):
        row = {"name": "Test food", "category": "whole_food", "calories_kcal": 100.0,
               "source": "curated", "source_priority": 1}
        row.update(over)
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        return conn.execute("INSERT INTO food_items (%s) VALUES (%s)" % (cols, marks),
                            list(row.values())).lastrowid

    def index_shapes(self, conn):
        shapes = {}
        for ix in conn.execute("PRAGMA index_list('food_items')").fetchall():
            ix = tuple(ix)   # seq, name, unique, origin, partial
            cols = tuple(tuple(r)[2] for r in
                         conn.execute("PRAGMA index_info('%s')" % ix[1]).fetchall())
            shapes[cols] = {"unique": bool(ix[2]), "partial": bool(ix[4])}
        return shapes

    def test_minimal_row_gets_the_documented_defaults(self):
        conn = self.make_db()
        rid = conn.execute("INSERT INTO food_items (name, calories_kcal, source, source_priority)"
                           " VALUES ('Plain food', 100, 'curated', 1)").lastrowid
        row = dict_rows(conn, "SELECT * FROM food_items WHERE id = ?", (rid,))[0]
        self.assertEqual({k: row[k] for k in ("category", "region_tags", "per_unit", "portions",
                                              "aka", "popularity", "name_key", "foreign_tags",
                                              "completeness", "alt_barcodes")},
                         {"category": "whole_food", "region_tags": "", "per_unit": "100g",
                          "portions": "[]", "aka": "[]", "popularity": 0, "name_key": "",
                          "foreign_tags": 0, "completeness": None, "alt_barcodes": "[]"})

    def test_foreign_tags_cannot_be_null(self):
        conn = self.make_db()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(conn, foreign_tags=None)

    def test_alt_barcodes_cannot_be_null(self):
        conn = self.make_db()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(conn, alt_barcodes=None)

    def test_the_three_categories_are_accepted(self):
        conn = self.make_db()
        for i, category in enumerate(("supermarket", "dining_out", "whole_food")):
            self.insert(conn, category=category, source_ref="cat-%d" % i)
        self.assertEqual(
            dict_rows(conn, "SELECT COUNT(*) AS n FROM food_items WHERE source_ref LIKE 'cat-%'")[0]["n"], 3)

    def test_category_check_rejects_an_unknown_category(self):
        conn = self.make_db()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(conn, category="street_food")

    def test_per_unit_accepts_serving(self):
        conn = self.make_db()
        self.insert(conn, per_unit="serving", source_ref="per-serving")
        self.assertEqual(
            dict_rows(conn, "SELECT per_unit FROM food_items WHERE source_ref = 'per-serving'")[0]["per_unit"],
            "serving")

    def test_per_unit_check_rejects_an_unknown_unit(self):
        conn = self.make_db()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(conn, per_unit="cup")

    def test_two_rows_cannot_share_a_barcode(self):
        conn = self.make_db()
        self.insert(conn, barcode="5000159407236", source_ref="bar-a")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(conn, barcode="5000159407236", source="off", source_priority=3,
                        source_ref="bar-b")

    def test_many_rows_may_have_no_barcode(self):
        conn = self.make_db()
        for i in range(3):
            self.insert(conn, barcode=None, source_ref="nobar-%d" % i)
        self.assertEqual(
            dict_rows(conn, "SELECT COUNT(*) AS n FROM food_items WHERE source_ref LIKE 'nobar-%'")[0]["n"], 3)

    def test_source_and_source_ref_pair_is_unique(self):
        conn = self.make_db()
        self.insert(conn, source="off", source_priority=3, source_ref="dup-ref")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(conn, source="off", source_priority=3, source_ref="dup-ref")

    def test_same_source_ref_under_another_source_is_allowed(self):
        conn = self.make_db()
        self.insert(conn, source="off", source_priority=3, source_ref="shared-ref")
        self.insert(conn, source="curated", source_priority=1, source_ref="shared-ref")
        self.assertEqual(
            dict_rows(conn, "SELECT COUNT(*) AS n FROM food_items WHERE source_ref = 'shared-ref'")[0]["n"], 2)

    def test_many_rows_may_have_no_source_ref(self):
        conn = self.make_db()
        for _ in range(3):
            self.insert(conn, name="No ref food", source="off", source_priority=3, source_ref=None)
        self.assertEqual(
            dict_rows(conn, "SELECT COUNT(*) AS n FROM food_items WHERE name = 'No ref food'")[0]["n"], 3)

    def test_barcode_index_is_unique_and_partial(self):
        shapes = self.index_shapes(self.make_db())
        self.assertEqual(shapes.get(("barcode",)), {"unique": True, "partial": True})

    def test_source_ref_index_is_unique_and_partial(self):
        shapes = self.index_shapes(self.make_db())
        self.assertEqual(shapes.get(("source", "source_ref")), {"unique": True, "partial": True})

    def test_lookup_indexes_exist(self):
        shapes = self.index_shapes(self.make_db())
        for col in ("name_key", "category", "source_priority"):
            self.assertIn((col,), shapes)


class FreshSchemaConstraintTests(_ConstraintChecks, SchemaCase):
    def make_db(self):
        return self.fresh_v2()


class UpgradedSchemaConstraintTests(_ConstraintChecks, SchemaCase):
    def make_db(self):
        return self.upgraded()


class UpgradedFromV2SchemaConstraintTests(_ConstraintChecks, SchemaCase):
    def make_db(self):
        return self.upgraded_from_v2()


# --------------------------------------------------------------------------------------
# importer: summary line
# --------------------------------------------------------------------------------------

class SummaryTests(FullImportCase):
    def test_last_stdout_line_has_every_summary_key(self):
        self.assertEqual(sorted(SUMMARY_KEYS - set(self.summary)), [])

    def test_read_counts_per_source(self):
        self.assertEqual(self.summary["read"], {"curated": 11, "usda": 13, "off": 52})

    def test_kept_counts_per_source(self):
        self.assertEqual(self.summary["kept"], {"curated": 8, "usda": 11, "off": 37})

    def test_kept_counts_match_the_database(self):
        in_db = {r["source"]: r["n"] for r in
                 self.q("SELECT source, COUNT(*) AS n FROM food_items GROUP BY source")}
        self.assertEqual(in_db, {"curated": 8, "usda": 11, "off": 37})

    def test_filtered_region_count(self):
        self.assertEqual(self.summary["filtered_region"], 2)

    def test_invalid_count(self):
        # 3 curated entries + 1 USDA row + 6 Open Food Facts rows
        self.assertEqual(self.summary["invalid"], 10)

    def test_duplicates_count(self):
        self.assertEqual(self.summary["duplicates"], 8)

    def test_total_rows(self):
        self.assertEqual(self.summary["total_rows"], 56)

    def test_total_rows_matches_the_database(self):
        self.assertEqual(self.n_rows(), 56)

    def test_database_is_at_version_3(self):
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_the_last_line_is_still_one_json_object_with_the_old_keys(self):
        self.assertEqual(parse_summary(self.imp["stdout"]), self.summary)
        self.assertEqual(sorted(SUMMARY_KEYS - set(self.summary)), [])
        self.assertEqual(set(self.summary["read"]), {"curated", "usda", "off"})
        self.assertEqual(set(self.summary["kept"]), {"curated", "usda", "off"})


# --------------------------------------------------------------------------------------
# importer: region filter
# --------------------------------------------------------------------------------------

class RegionFilterTests(FullImportCase):
    def test_row_whose_only_country_is_unwanted_is_not_imported(self):
        self.absent(name="Nutella", codes=("3017620422003",))

    def test_row_with_two_unwanted_countries_is_not_imported(self):
        self.absent(name="Ritter Sport Alpine Milk", codes=("4000417025005",))

    def test_filtered_rows_are_counted_as_filtered_region(self):
        self.assertEqual(self.summary["filtered_region"], 2)

    def test_two_wanted_countries_come_out_in_the_fixed_order(self):
        # countries_tags is "en:south-korea,en:united-states"
        self.assertEqual(self.off_row("8801043014830")["region_tags"], "US,KR")

    def test_unwanted_country_is_dropped_from_a_mixed_row(self):
        # countries_tags is "en:japan,en:france,en:philippines,en:united-kingdom"
        self.assertEqual(self.off_row("4901515110504")["region_tags"], "PH,UK,JP")

    def test_single_country_rows_get_their_code(self):
        expected = {"5000159407236": "UK", "4901330502881": "JP", "8801056038861": "KR",
                    "4800361413480": "PH"}
        for ref, tags in expected.items():
            with self.subTest(code=ref):
                self.assertEqual(self.off_row(ref)["region_tags"], tags)

    def test_usda_rows_are_tagged_us(self):
        self.assertEqual(self.usda_row("survey:2709224")["region_tags"], "US")

    def test_every_region_tags_value_is_an_ordered_subset_of_the_regions(self):
        for row in self.q("SELECT name, region_tags FROM food_items"):
            tags = row["region_tags"]
            codes = tags.split(",") if tags else []
            self.assertEqual(codes, [c for c in REGION_ORDER if c in codes],
                             "bad region_tags %r on %r" % (tags, row["name"]))

    def test_every_off_row_has_at_least_one_region(self):
        self.assertEqual(self.n_rows("source = 'off' AND region_tags = ''"), 0)


# --------------------------------------------------------------------------------------
# importer: unit conversions on real rows
# --------------------------------------------------------------------------------------

class UnitConversionTests(FullImportCase):
    def test_off_energy_100g_kilojoules_become_kcal(self):
        self.near(self.off_row("5000159407236")["calories_kcal"], 484.9)    # 2029 / 4.184

    def test_off_energy_kj_100g_kilojoules_become_kcal(self):
        self.near(self.off_row("5010029000023")["calories_kcal"], 365.9)    # 1531 / 4.184

    def test_usda_kilojoules_become_kcal(self):
        self.near(self.usda_row("sr:171688")["calories_kcal"], 380.0)       # 1590 / 4.184

    def test_kcal_figure_is_used_as_given_when_present(self):
        self.near(self.off_row("8801043014830")["calories_kcal"], 433.0)

    def test_off_salt_becomes_sodium_mg(self):
        self.near(self.off_row("5000159407236")["sodium_mg"], 252.0)        # 0.63 g * 400

    def test_off_salt_becomes_sodium_mg_second_row(self):
        self.near(self.off_row("5010029000023")["sodium_mg"], 112.0)        # 0.28 g * 400

    def test_off_sodium_grams_become_mg(self):
        self.near(self.off_row("8801043014830")["sodium_mg"], 1790.0)       # 1.79 g * 1000

    def test_off_row_with_both_sodium_and_salt(self):
        self.near(self.off_row("4901330502881")["sodium_mg"], 200.0)        # 0.2 g, salt 0.5 g

    def test_usda_sodium_is_already_mg(self):
        self.near(self.usda_row("survey:2706000")["sodium_mg"], 460.0)

    def test_zero_sodium_is_stored_as_zero_not_null(self):
        self.near(self.usda_row("survey:2709224")["sodium_mg"], 0.0)

    def test_calories_are_stored_with_one_decimal(self):
        self.near(self.off_row("5000159407236")["calories_kcal"], 484.9, places=6)   # 484.9426...

    def test_grams_are_stored_with_two_decimals(self):
        row = self.curated_row("Lucky Me! Pancit Canton Kalamansi")
        self.near(row["carbs_g"], 58.33, places=6)                                    # 35 * 100 / 60

    def test_sodium_is_stored_with_one_decimal(self):
        self.near(self.off_row("5000169005743")["sodium_mg"], 245.3, places=6)        # 0.6133 * 400


# --------------------------------------------------------------------------------------
# importer: validation
# --------------------------------------------------------------------------------------

class ValidationTests(FullImportCase):
    def test_macro_above_100_discards_the_row(self):
        self.absent(name="Mega Sugar Syrup", codes=("041220576555", "0041220576555"))

    def test_protein_carbs_fat_above_105_discards_the_row(self):
        self.absent(name="Impossible Protein Bar", codes=("041220576562", "0041220576562"))

    def test_negative_macro_discards_the_row(self):
        self.absent(name="Negative Fibre Yogurt", codes=("041220576579", "0041220576579"))

    def test_more_than_950_kcal_discards_the_row(self):
        self.absent(name="Rocket Fuel Oil", codes=("5000111000017",))

    def test_missing_name_discards_the_row(self):
        self.absent(codes=("4800011122334",))
        self.assertEqual(self.n_rows("name IS NULL OR TRIM(name) = ''"), 0)

    def test_missing_energy_discards_the_row(self):
        self.absent(name="No Energy Crackers", codes=("4800011122341",))

    def test_usda_row_without_calories_is_discarded(self):
        self.assertEqual(self.n_rows("source_ref = 'branded:2500002'"), 0)
        self.absent(codes=("0041220999993",))

    def test_discarded_rows_are_counted_as_invalid(self):
        self.assertEqual(self.summary["invalid"], 10)

    def test_blank_macros_are_stored_as_null(self):
        row = self.off_row("4901340036512")
        self.near(row["calories_kcal"], 46.0)
        for col in MACROS + ("sodium_mg",):
            self.assertIsNone(row[col], col)

    def test_zero_kcal_row_is_kept(self):
        row = self.off_row("4514603365511")
        self.near(row["calories_kcal"], 0.0)

    def test_zero_macros_are_stored_as_zero_not_null(self):
        row = self.off_row("4514603365511")
        for col in MACROS:
            self.near(row[col], 0.0)

    def test_sodium_above_40000_mg_becomes_null_and_the_row_is_kept(self):
        row = self.off_row("8801007000015")
        self.assertIsNone(row["sodium_mg"])
        self.near(row["calories_kcal"], 5.0)

    def test_name_is_trimmed_and_inner_whitespace_collapsed(self):
        self.assertEqual(self.off_row("4800361413480")["name"], "Milo Activ-Go Powder")

    def test_no_stored_per_100g_row_breaks_the_limits(self):
        for row in self.q("SELECT * FROM food_items WHERE per_unit = '100g'"):
            with self.subTest(name=row["name"]):
                self.assertGreaterEqual(row["calories_kcal"], 0)
                self.assertLessEqual(row["calories_kcal"], 950)
                for col in MACROS:
                    if row[col] is not None:
                        self.assertGreaterEqual(row[col], 0, col)
                        self.assertLessEqual(row[col], 100, col)
                total = sum(row[c] or 0 for c in ("protein_g", "carbs_g", "fat_g"))
                self.assertLessEqual(total, 105.001)
                if row["sodium_mg"] is not None:
                    self.assertGreaterEqual(row["sodium_mg"], 0)
                    self.assertLessEqual(row["sodium_mg"], 40000)


# --------------------------------------------------------------------------------------
# importer: Open Food Facts mapping
# --------------------------------------------------------------------------------------

class OffMappingTests(FullImportCase):
    def test_name_and_brand_of_a_plain_row(self):
        row = self.off_row("5000159407236")
        self.assertEqual((row["name"], row["brand"]), ("Snickers", "Mars"))

    def test_name_falls_back_to_product_name_en(self):
        self.assertEqual(self.off_row("4901577000010")["name"], "Kewpie Mayonnaise")

    def test_product_name_wins_over_product_name_en(self):
        self.assertEqual(self.off_row("8801056038861")["name"], "Banana Flavored Milk")

    def test_brand_is_the_first_comma_separated_entry(self):
        # brands is "Nestlé,Milo"; this also proves the file is read as UTF-8
        self.assertEqual(self.off_row("4800361413480")["brand"], "Nestlé")

    def test_blank_brands_gives_no_brand(self):
        self.assertIsNone(self.off_row("5000169005743")["brand"])

    def test_every_off_row_is_supermarket(self):
        self.assertEqual(self.n_rows("source = 'off' AND category <> 'supermarket'"), 0)

    def test_every_off_row_has_priority_3(self):
        self.assertEqual(self.n_rows("source = 'off' AND source_priority <> 3"), 0)

    def test_every_off_row_has_basis_database(self):
        self.assertEqual(self.n_rows("source = 'off' AND (basis IS NULL OR basis <> 'database')"), 0)

    def test_every_off_row_is_per_100g(self):
        self.assertEqual(self.n_rows("source = 'off' AND per_unit <> '100g'"), 0)

    def test_barcode_is_the_canonical_code(self):
        self.assertEqual(self.off_row("5000159407236")["barcode"], "5000159407236")

    def test_in_store_code_has_no_barcode_and_keeps_the_raw_code_as_source_ref(self):
        row = self.off_row("2000000012345")
        self.assertIsNone(row["barcode"])
        self.assertEqual(row["name"], "Deli Roast Chicken Slices")

    def test_too_short_code_has_no_barcode_and_keeps_the_raw_code_as_source_ref(self):
        row = self.off_row("12345")
        self.assertIsNone(row["barcode"])
        self.assertEqual(row["name"], "Corner Shop Flapjack")

    def test_popularity_is_unique_scans_n(self):
        self.assertEqual(self.off_row("5000159407236")["popularity"], 950)

    def test_blank_unique_scans_n_gives_popularity_zero(self):
        self.assertEqual(self.off_row("4901340036512")["popularity"], 0)

    def test_portion_comes_from_serving_size_and_quantity(self):
        self.assertEqual(self.portions_of(self.off_row("5000159407236")), [["48 g", 48]])

    def test_fractional_serving_quantity_is_kept(self):
        self.assertEqual(self.portions_of(self.off_row("5010029000023")),
                         [["2 biscuits (37.5 g)", 37.5]])

    def test_blank_serving_quantity_gives_no_portions(self):
        self.assertEqual(self.portions_of(self.off_row("4800361413480")), [])

    def test_blank_serving_size_gives_no_portions(self):
        self.assertEqual(self.portions_of(self.off_row("4514603365511")), [])

    def test_each_macro_column_is_mapped(self):
        row = self.off_row("8801043014830")   # Shin Ramyun
        expected = {"protein_g": 9.2, "carbs_g": 65.0, "sugars_g": 3.3, "fat_g": 15.0,
                    "saturates_g": 7.5, "fibre_g": 2.5}
        for col, value in expected.items():
            with self.subTest(col=col):
                self.near(row[col], value)

    def test_name_key_is_stored(self):
        self.assertEqual(self.off_row("5000159407236")["name_key"], "mars|snickers")

    def test_name_key_drops_the_brand_prefix_of_the_name(self):
        self.assertEqual(self.off_row("4901515110504")["name_key"], "kikkoman|soy sauce")


class ThumbnailTests(FullImportCase):
    def test_https_url_is_stored_in_full(self):
        self.assertEqual(self.off_row("5000159407236")["thumbnail_url"],
                         THUMB_BASE + "500/015/940/7236/front_en.4.200.jpg")

    def test_url_that_does_not_start_with_http_becomes_null(self):
        # image_front_small_url is "/images/products/880/105/603/8861/front_ko.3.200.jpg"
        self.assertIsNone(self.off_row("8801056038861")["thumbnail_url"])

    def test_blank_url_is_null(self):
        self.assertIsNone(self.off_row("4901340036512")["thumbnail_url"])

    def test_every_stored_thumbnail_starts_with_http(self):
        self.assertEqual(self.n_rows("thumbnail_url IS NOT NULL AND thumbnail_url NOT LIKE 'http://%'"
                                     " AND thumbnail_url NOT LIKE 'https://%'"), 0)


# --------------------------------------------------------------------------------------
# importer: priority de-duplication
# --------------------------------------------------------------------------------------

class DedupCuratedOverOffBarcodeTests(FullImportCase):
    """Anchor C3 (curated) and anchor A1 (Open Food Facts) share barcode 4807770270123."""

    def survivor(self):
        return self.one_row("barcode = ?", ("4807770270123",))

    def test_the_two_rows_collapse_to_one(self):
        self.assertEqual(self.n_rows("barcode = ?", ("4807770270123",)), 1)

    def test_the_curated_row_survives(self):
        row = self.survivor()
        self.assertEqual((row["source"], row["source_priority"]), ("curated", 1))

    def test_the_off_row_is_gone(self):
        self.assertEqual(self.n_rows("source = 'off' AND source_ref = ?", ("4807770270123",)), 0)
        self.assertEqual(self.n_rows("name = ?", ("Pancit Canton Kalamansi",)), 0)

    def test_the_survivor_keeps_the_curated_name(self):
        self.assertEqual(self.survivor()["name"], "Lucky Me! Pancit Canton Kalamansi")

    def test_the_survivor_keeps_the_curated_numbers(self):
        row = self.survivor()
        # per pack of 60 g: 280 kcal, 6 P, 35 C, 3 sugars, 12 F, 6 sat, 2 fibre, 900 mg sodium
        expected = {"calories_kcal": 466.7, "protein_g": 10.0, "carbs_g": 58.33, "sugars_g": 5.0,
                    "fat_g": 20.0, "saturates_g": 10.0, "fibre_g": 3.33, "sodium_mg": 1500.0}
        for col, value in expected.items():
            with self.subTest(col=col):
                self.near(row[col], value)

    def test_the_survivor_inherits_the_off_thumbnail(self):
        self.assertEqual(self.survivor()["thumbnail_url"],
                         THUMB_BASE + "480/777/027/0123/front_en.3.200.jpg")

    def test_the_survivor_keeps_region_ph(self):
        self.assertEqual(self.survivor()["region_tags"], "PH")


class DedupUsdaOverOffBarcodeTests(FullImportCase):
    """Anchor U2 (USDA branded, GTIN-14 00016000275287) and anchor A3 (OFF, UPC-A 016000275287)."""

    def survivor(self):
        return self.one_row("barcode = ?", ("0016000275287",))

    def test_upc_a_and_gtin_14_meet_on_one_canonical_barcode(self):
        self.assertEqual(self.n_rows("barcode = ?", ("0016000275287",)), 1)
        self.assertEqual(self.n_rows("barcode IN ('016000275287', '00016000275287')"), 0)

    def test_the_usda_row_survives(self):
        row = self.survivor()
        self.assertEqual((row["source"], row["source_ref"]), ("usda", "branded:2499999"))

    def test_the_off_row_is_gone(self):
        self.assertEqual(self.n_rows("source = 'off' AND source_ref IN ('0016000275287', '016000275287')"), 0)

    def test_only_one_cheerios_row_is_left(self):
        self.assertEqual(self.n_rows("LOWER(name) = 'cheerios'"), 1)

    def test_the_survivor_keeps_the_usda_numbers(self):
        row = self.survivor()
        self.near(row["calories_kcal"], 359.0)    # the Open Food Facts row says 367
        self.near(row["protein_g"], 12.82)

    def test_the_survivor_inherits_the_off_thumbnail(self):
        self.assertEqual(self.survivor()["thumbnail_url"],
                         THUMB_BASE + "001/600/027/5287/front_en.5.200.jpg")

    def test_the_survivor_region_is_us(self):
        self.assertEqual(self.survivor()["region_tags"], "US")


class DedupSameSourceBarcodeTests(FullImportCase):
    """Two Open Food Facts rows with the same barcode: the more complete row wins.

    Choco Pie (8801117760007): the complete row comes first in the file, the sparse row
    second with more scans. Meiji (4902777000015): the other way round.
    """

    def test_complete_row_listed_first_survives(self):
        row = self.one_row("barcode = ?", ("8801117760007",))
        self.assertEqual(row["name"], "Choco Pie Original")
        self.near(row["calories_kcal"], 432.0)
        self.near(row["carbs_g"], 66.0)

    def test_complete_row_listed_second_survives(self):
        row = self.one_row("barcode = ?", ("4902777000015",))
        self.assertEqual(row["name"], "Meiji Milk Chocolate")
        self.near(row["calories_kcal"], 558.0)
        self.near(row["carbs_g"], 53.0)

    def test_sparse_duplicates_are_gone(self):
        self.assertEqual(self.n_rows("name IN ('Choco Pie', 'Meiji Milk Choco')"), 0)

    def test_survivor_inherits_the_thumbnail_of_the_dropped_duplicate(self):
        row = self.one_row("barcode = ?", ("8801117760007",))
        self.assertEqual(row["thumbnail_url"], THUMB_BASE + "880/111/776/0007/front_ko.3.200.jpg")

    def test_survivor_keeps_its_own_popularity(self):
        self.assertEqual(self.one_row("barcode = ?", ("8801117760007",))["popularity"], 10)
        self.assertEqual(self.one_row("barcode = ?", ("4902777000015",))["popularity"], 20)


class DedupNameBrandTests(FullImportCase):
    def test_off_rows_with_the_same_name_and_brand_collapse_to_the_more_complete_one(self):
        # "Jack 'n Jill Chippy BBQ" / "Jack n Jill" and "Chippy BBQ" / "Jack 'n Jill"
        row = self.one_row("name_key = ?", ("jack n jill|chippy bbq",))
        self.assertEqual(row["name"], "Chippy BBQ")
        self.near(row["calories_kcal"], 510.0)

    def test_name_survivor_keeps_its_own_barcode(self):
        row = self.one_row("name_key = ?", ("jack n jill|chippy bbq",))
        self.assertEqual(row["barcode"], "4800016644504")
        self.assertEqual(self.n_rows("barcode = ? OR source_ref = ?",
                                     ("4800016644511", "4800016644511")), 0)

    def test_name_survivor_region_tags_are_the_union(self):
        # survivor is tagged PH, the dropped row PH and US
        self.assertEqual(self.one_row("name_key = ?", ("jack n jill|chippy bbq",))["region_tags"],
                         "PH,US")

    def test_usda_beats_off_on_name_and_brand(self):
        row = self.one_row("name_key = ?", ("skippy|creamy peanut butter",))
        self.assertEqual((row["source"], row["source_ref"]), ("usda", "branded:2500001"))
        self.near(row["carbs_g"], 18.8)    # the Open Food Facts row says 21.9

    def test_usda_name_survivor_keeps_its_own_barcode(self):
        row = self.one_row("name_key = ?", ("skippy|creamy peanut butter",))
        self.assertEqual(row["barcode"], "0037600110754")
        self.assertEqual(self.n_rows("barcode = ? OR source_ref = ?",
                                     ("0037600105002", "0037600105002")), 0)

    def test_usda_name_survivor_takes_no_thumbnail_from_the_name_step_loser(self):
        # DD-3: a name + brand match is not proof of the same pack, so no pack photo goes onto a USDA row
        row = self.one_row("name_key = ?", ("skippy|creamy peanut butter",))
        self.assertIsNone(row["thumbnail_url"])

    def test_usda_name_survivor_keeps_the_dropped_barcode_as_an_alias(self):
        row = self.one_row("name_key = ?", ("skippy|creamy peanut butter",))
        self.assertIn("0037600105002", row["alt_barcodes"])

    def test_usda_name_survivor_region_tags_are_the_union(self):
        # USDA row is US, the dropped Open Food Facts row US and PH
        self.assertEqual(self.one_row("name_key = ?", ("skippy|creamy peanut butter",))["region_tags"],
                         "PH,US")

    def test_curated_beats_off_on_name_and_brand(self):
        row = self.one_row("name_key = ?", ("my san|skyflakes crackers",))
        self.assertEqual((row["source"], row["name"]), ("curated", "Skyflakes Crackers"))
        self.near(row["calories_kcal"], 476.0)    # the Open Food Facts row says 480

    def test_curated_survivor_takes_no_barcode_from_the_name_step_loser(self):
        # DD-3 (corrected): no curated or USDA row takes a barcode or photo from a name-step loser
        row = self.one_row("name_key = ?", ("my san|skyflakes crackers",))
        self.assertIsNone(row["barcode"])

    def test_curated_survivor_takes_no_thumbnail_from_the_name_step_loser(self):
        row = self.one_row("name_key = ?", ("my san|skyflakes crackers",))
        self.assertIsNone(row["thumbnail_url"])

    def test_curated_survivor_keeps_the_dropped_barcode_as_an_alias(self):
        row = self.one_row("name_key = ?", ("my san|skyflakes crackers",))
        self.assertIn("4800092110016", row["alt_barcodes"])
        self.assertEqual(self.n_rows("barcode = '4800092110016' OR source_ref = '4800092110016'"), 0)

    def test_curated_region_tags_never_change_in_a_merge(self):
        # the dropped Open Food Facts row is tagged PH and US; the curated row stays PH
        self.assertEqual(self.one_row("name_key = ?", ("my san|skyflakes crackers",))["region_tags"], "PH")

    def test_curated_beats_usda_on_name(self):
        row = self.one_row("name_key = ?", ("|pork adobo",))
        self.assertEqual((row["source"], row["name"]), ("curated", "Pork Adobo"))
        self.near(row["calories_kcal"], 228.0)    # the USDA row says 236
        self.assertEqual(self.n_rows("source_ref = 'survey:2706555'"), 0)

    def test_curated_over_usda_region_tags_never_change(self):
        # the USDA row is tagged US; a curated row's regions are not widened by a merge
        self.assertEqual(self.one_row("name_key = ?", ("|pork adobo",))["region_tags"], "PH")

    def test_name_survivor_takes_the_larger_popularity_of_the_group(self):
        # DD-5: the survivor Chippy BBQ had 80 scans, the merged pack 300
        self.assertEqual(self.one_row("name_key = ?", ("jack n jill|chippy bbq",))["popularity"], 300)

    def test_name_survivor_keeps_the_dropped_barcode_as_an_alias(self):
        row = self.one_row("name_key = ?", ("jack n jill|chippy bbq",))
        self.assertIn("4800016644511", row["alt_barcodes"])

    def test_the_survivor_stays_a_plain_database_figure(self):
        # the merged pack had one blank macro (carbs, estimated as 59.5 g) but lost to the fuller row
        self.assertEqual(self.one_row("name_key = ?", ("jack n jill|chippy bbq",))["basis"], "database")

    def test_rows_that_merged_nothing_have_an_empty_alias_list(self):
        self.assertEqual(self.off_row("5000159407236")["alt_barcodes"], "[]")
        self.assertEqual(self.off_row("4901330502881")["alt_barcodes"], "[]")

    def test_no_two_rows_share_a_barcode(self):
        dupes = self.q("SELECT barcode, COUNT(*) AS n FROM food_items WHERE barcode IS NOT NULL"
                       " GROUP BY barcode HAVING n > 1")
        self.assertEqual([tuple(r) for r in dupes], [])

    def test_no_two_rows_share_a_name_key(self):
        dupes = self.q("SELECT name_key, COUNT(*) AS n FROM food_items GROUP BY name_key HAVING n > 1")
        self.assertEqual([tuple(r) for r in dupes], [])

    def test_removed_rows_are_counted_as_duplicates(self):
        self.assertEqual(self.summary["duplicates"], 8)


# --------------------------------------------------------------------------------------
# importer: curated list
# --------------------------------------------------------------------------------------

class CuratedTests(FullImportCase):
    def test_per_serving_numbers_are_converted_to_per_100g(self):
        row = self.curated_row("Chickenjoy 1 pc (thigh)")    # 380 kcal, 27 P, 5 C, 28 F per 125 g
        self.assertEqual(row["per_unit"], "100g")
        self.near(row["calories_kcal"], 304.0)
        self.near(row["protein_g"], 21.6)
        self.near(row["carbs_g"], 4.0)
        self.near(row["fat_g"], 22.4)

    def test_converted_row_has_the_serving_as_first_portion(self):
        self.assertEqual(self.portions_of(self.curated_row("Chickenjoy 1 pc (thigh)"))[0],
                         ["1 thigh", 125])

    def test_null_nutrients_stay_null_after_conversion(self):
        row = self.curated_row("Chickenjoy 1 pc (thigh)")
        for col in ("sugars_g", "saturates_g", "fibre_g", "sodium_mg"):
            self.assertIsNone(row[col], col)

    def test_sodium_is_converted_with_the_other_nutrients(self):
        row = self.curated_row("Sinangag (garlic fried rice)")   # 288 kcal, 400 mg per 160 g
        self.near(row["calories_kcal"], 180.0)
        self.near(row["sodium_mg"], 250.0)

    def test_serving_without_grams_is_stored_per_serving(self):
        row = self.curated_row("Flat White (tall)")
        self.assertEqual(row["per_unit"], "serving")
        expected = {"calories_kcal": 170, "protein_g": 9, "carbs_g": 14, "sugars_g": 13,
                    "fat_g": 9, "saturates_g": 5, "sodium_mg": 115}
        for col, value in expected.items():
            with self.subTest(col=col):
                self.near(row[col], value)

    def test_per_serving_row_has_one_portion_without_grams(self):
        self.assertEqual(self.portions_of(self.curated_row("Flat White (tall)")),
                         [["1 tall (12 fl oz)", None]])

    def test_only_the_gramless_entry_is_stored_per_serving(self):
        rows = self.q("SELECT name FROM food_items WHERE per_unit = 'serving'")
        self.assertEqual([r["name"] for r in rows], ["Flat White (tall)"])

    def test_per_100g_entry_is_stored_as_given(self):
        row = self.curated_row("Chicken adobo")
        self.assertEqual(row["per_unit"], "100g")
        self.near(row["calories_kcal"], 141.0)
        self.near(row["protein_g"], 15.7)
        self.near(row["carbs_g"], 3.3)
        self.near(row["fat_g"], 6.5)

    def test_per_100g_entry_gets_its_serving_as_a_portion(self):
        self.assertEqual(self.portions_of(self.curated_row("Chicken adobo")),
                         [["1 serving with sauce", 152]])

    def test_extra_portions_follow_the_serving(self):
        self.assertEqual(self.portions_of(self.curated_row("Sinangag (garlic fried rice)")),
                         [["1 cup", 160], ["1/2 cup", 80], ["1 platter", 320]])

    def test_categories_are_stored_as_given(self):
        expected = {"Chickenjoy 1 pc (thigh)": "dining_out", "Chicken adobo": "whole_food",
                    "Lucky Me! Pancit Canton Kalamansi": "supermarket",
                    "Flat White (tall)": "dining_out", "Spam Classic": "supermarket",
                    "Sinangag (garlic fried rice)": "whole_food"}
        for name, category in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.curated_row(name)["category"], category)

    def test_region_tags_are_stored_as_a_comma_list(self):
        self.assertEqual(self.curated_row("Chickenjoy 1 pc (thigh)")["region_tags"], "PH")
        self.assertEqual(self.curated_row("Flat White (tall)")["region_tags"], "PH,US")

    def test_region_tags_given_out_of_order_are_stored_in_the_fixed_order(self):
        # the fixture says ["US", "PH"]
        self.assertEqual(self.curated_row("Spam Classic")["region_tags"], "PH,US")

    def test_barcode_is_stored_in_canonical_form(self):
        # the fixture gives the 12-digit UPC-A 037600138727
        self.assertEqual(self.curated_row("Spam Classic")["barcode"], "0037600138727")

    def test_own_thumbnail_is_kept(self):
        self.assertEqual(self.curated_row("Spam Classic")["thumbnail_url"],
                         "https://example.org/img/spam-classic.jpg")

    def test_brand_is_stored(self):
        self.assertEqual(self.curated_row("Chickenjoy 1 pc (thigh)")["brand"], "Jollibee")

    def test_generic_dish_has_no_brand(self):
        self.assertIsNone(self.curated_row("Chicken adobo")["brand"])

    def test_explicit_group_is_kept(self):
        self.assertEqual(self.curated_row("Chicken adobo")["group_label"], "Home dish")
        self.assertEqual(self.curated_row("Spam Classic")["group_label"], "Canned meat")

    def test_group_defaults_to_the_brand(self):
        self.assertEqual(self.curated_row("Lucky Me! Pancit Canton Kalamansi")["group_label"], "Lucky Me!")
        self.assertEqual(self.curated_row("Flat White (tall)")["group_label"], "Starbucks")

    def test_aka_is_a_json_list(self):
        self.assertEqual(json.loads(self.curated_row("Chickenjoy 1 pc (thigh)")["aka"]), ["chicken joy"])
        self.assertEqual(json.loads(self.curated_row("Flat White (tall)")["aka"]), [])

    def test_basis_is_stored(self):
        self.assertEqual(self.curated_row("Chickenjoy 1 pc (thigh)")["basis"], "official")
        self.assertEqual(self.curated_row("Pork Adobo")["basis"], "estimate")

    def test_every_curated_row_has_priority_1(self):
        self.assertEqual(self.n_rows("source = 'curated' AND source_priority <> 1"), 0)

    def test_every_curated_row_has_its_own_source_ref(self):
        refs = [r["source_ref"] for r in self.q("SELECT source_ref FROM food_items WHERE source = 'curated'")]
        self.assertEqual(len(refs), 8)
        self.assertTrue(all(refs), refs)
        self.assertEqual(len(set(refs)), 8, refs)

    def test_name_key_is_brand_then_name_without_the_brand(self):
        self.assertEqual(self.curated_row("Lucky Me! Pancit Canton Kalamansi")["name_key"],
                         "lucky me|pancit canton kalamansi")
        self.assertEqual(self.curated_row("Chicken adobo")["name_key"], "|chicken adobo")

    def test_entry_with_an_invalid_category_is_skipped(self):
        self.absent(name="Mystery Street Snack")
        self.assertEqual(self.n_rows("category NOT IN ('supermarket', 'dining_out', 'whole_food')"), 0)

    def test_entry_with_an_unknown_region_is_skipped(self):
        self.absent(name="Poutine")

    def test_entry_without_calories_is_skipped(self):
        self.absent(name="Taho")

    def test_skipped_entries_produce_a_warning_on_stderr(self):
        self.assertTrue(self.imp["stderr"].strip(), "nothing was written to stderr")

    def test_eight_curated_rows_are_kept(self):
        self.assertEqual(self.n_rows("source = 'curated'"), 8)


# --------------------------------------------------------------------------------------
# importer: USDA flat file
# --------------------------------------------------------------------------------------

class UsdaFlatFileTests(FullImportCase):
    def test_plain_survey_food_is_whole_food(self):
        self.assertEqual(self.usda_row("survey:2709224")["category"], "whole_food")

    def test_plain_sr_legacy_food_is_whole_food(self):
        self.assertEqual(self.usda_row("sr:171688")["category"], "whole_food")

    def test_foundation_food_is_whole_food_with_fdc_prefix(self):
        self.assertEqual(self.usda_row("fdc:321358")["category"], "whole_food")

    def test_restaurant_chain_in_the_description_is_dining_out(self):
        # anchor U3: "Big Mac (McDonalds)", food category "Burgers"
        self.assertEqual(self.usda_row("survey:2706000")["category"], "dining_out")

    def test_chain_name_matches_without_apostrophe_and_case(self):
        # "WENDY'S, Classic Single Hamburger, no cheese", food category "Beef Products"
        self.assertEqual(self.usda_row("sr:170310")["category"], "dining_out")

    def test_fast_food_in_the_description_is_dining_out(self):
        # "Fast foods, cheeseburger, single patty, plain", food category "Sandwiches"
        self.assertEqual(self.usda_row("sr:170720")["category"], "dining_out")

    def test_restaurant_in_the_description_is_dining_out(self):
        # "Chicken fried steak, from restaurant", food category "Meat Mixed Dishes"
        self.assertEqual(self.usda_row("survey:2706100")["category"], "dining_out")

    def test_restaurant_foods_category_is_dining_out(self):
        # "Fried rice with egg, family style", food category "Restaurant Foods"
        self.assertEqual(self.usda_row("sr:167650")["category"], "dining_out")

    def test_branded_food_is_supermarket(self):
        self.assertEqual(self.usda_row("branded:2499999")["category"], "supermarket")

    def test_branded_food_takes_the_brand_owner_as_brand(self):
        self.assertEqual(self.usda_row("branded:2499999")["brand"], "General Mills")

    def test_branded_food_barcode_is_the_normalised_gtin(self):
        self.assertEqual(self.usda_row("branded:2500003")["barcode"], "0012000001291")

    def test_branded_food_naming_a_chain_is_still_supermarket(self):
        # "STARBUCKS FRAPPUCCINO COFFEE DRINK": the branded rule comes before the chain rule
        self.assertEqual(self.usda_row("branded:2500003")["category"], "supermarket")

    def test_source_ref_prefix_follows_the_data_type(self):
        refs = {r["source_ref"] for r in self.q("SELECT source_ref FROM food_items WHERE source = 'usda'")}
        self.assertEqual(refs, {"survey:2709224", "survey:2706000", "survey:2706100",
                                "sr:171688", "sr:170720", "sr:167650", "sr:170310",
                                "fdc:321358",
                                "branded:2499999", "branded:2500001", "branded:2500003"})

    def test_group_label_is_the_food_category(self):
        self.assertEqual(self.usda_row("survey:2709224")["group_label"], "Bananas")
        self.assertEqual(self.usda_row("survey:2706000")["group_label"], "Burgers")

    def test_portion_comes_from_household_serving_and_grams(self):
        self.assertEqual(self.portions_of(self.usda_row("survey:2709224")), [["1 banana", 126]])

    def test_per_100g_values_are_stored_as_given(self):
        row = self.usda_row("survey:2709224")    # anchor U1
        self.assertEqual(row["name"], "Banana, raw")
        expected = {"calories_kcal": 97.0, "protein_g": 0.74, "carbs_g": 23.0, "sugars_g": 15.8,
                    "fat_g": 0.29, "saturates_g": 0.1, "fibre_g": 1.7}
        for col, value in expected.items():
            with self.subTest(col=col):
                self.near(row[col], value)

    def test_every_usda_row_has_priority_2(self):
        self.assertEqual(self.n_rows("source = 'usda' AND source_priority <> 2"), 0)

    def test_every_usda_row_has_basis_database(self):
        self.assertEqual(self.n_rows("source = 'usda' AND (basis IS NULL OR basis <> 'database')"), 0)

    def test_every_usda_row_is_per_100g(self):
        self.assertEqual(self.n_rows("source = 'usda' AND per_unit <> '100g'"), 0)

    def test_every_usda_row_is_tagged_us(self):
        for row in self.q("SELECT source_ref, region_tags FROM food_items WHERE source = 'usda'"):
            self.assertIn("US", row["region_tags"].split(","), row["source_ref"])


# --------------------------------------------------------------------------------------
# importer: USDA FoodData Central folder (SR Legacy layout)
# --------------------------------------------------------------------------------------

class UsdaFolderTests(DbCase):
    @classmethod
    def load(cls):
        return usda_folder_import()

    def test_four_of_the_five_foods_are_imported(self):
        self.assertEqual(self.n_rows(), 4)
        self.assertEqual(self.summary["total_rows"], 4)
        self.assertEqual(self.summary["kept"]["usda"], 4)

    def test_baby_food_is_skipped(self):
        self.assertEqual(self.n_rows("source_ref LIKE '%900002'"), 0)
        self.assertEqual(self.n_rows("LOWER(name) LIKE '%babyfood%'"), 0)

    def test_source_ref_uses_the_sr_prefix(self):
        refs = {r["source_ref"] for r in self.q("SELECT source_ref FROM food_items")}
        self.assertEqual(refs, {"sr:900001", "sr:900003", "sr:900004", "sr:900005"})

    def test_fast_foods_row_is_dining_out(self):
        self.assertEqual(self.usda_row("sr:900003")["category"], "dining_out")

    def test_other_rows_are_whole_food(self):
        for ref in ("sr:900001", "sr:900004", "sr:900005"):
            with self.subTest(ref=ref):
                self.assertEqual(self.usda_row(ref)["category"], "whole_food")

    def test_shouted_brand_name_is_tidied_like_the_old_builder(self):
        # food.csv says "McDONALD'S, BIG MAC"
        self.assertEqual(self.usda_row("sr:900003")["name"], "McDonald's, Big Mac")

    def test_distribution_program_note_is_removed_from_the_name(self):
        self.assertEqual(self.usda_row("sr:900001")["name"], "Apples, raw, with skin")

    def test_group_label_is_the_food_category_description(self):
        self.assertEqual(self.usda_row("sr:900003")["group_label"], "Fast Foods")
        self.assertEqual(self.usda_row("sr:900001")["group_label"], "Fruits and Fruit Juices")

    def test_nutrients_are_read_by_nutrient_id(self):
        row = self.usda_row("sr:900001")    # apple
        expected = {"calories_kcal": 52.0, "protein_g": 0.3, "fat_g": 0.2, "carbs_g": 13.8,
                    "sugars_g": 10.4, "fibre_g": 2.4, "sodium_mg": 1.0}
        for col, value in expected.items():
            with self.subTest(col=col):
                self.near(row[col], value)

    def test_saturated_fat_and_sodium_are_read(self):
        row = self.usda_row("sr:900003")    # Big Mac
        self.near(row["calories_kcal"], 257.0)
        self.near(row["saturates_g"], 3.8)
        self.near(row["sodium_mg"], 460.0)

    def test_calorie_fallback_uses_the_atwater_energy(self):
        # rice has a blank 1008 amount and 130 under nutrient 2047
        self.near(self.usda_row("sr:900004")["calories_kcal"], 130.0)

    def test_portions_are_amount_plus_modifier_in_sequence_order(self):
        self.assertEqual(self.portions_of(self.usda_row("sr:900001")),
                         [["1 cup, quartered or chopped", 125], ["1 medium", 182],
                          ["1/2 cup slices", 54.5]])

    def test_default_portion_is_the_first_that_is_not_a_bare_ounce(self):
        # sequence order is "1 oz", "1 cup, diced", "1 cubic inch"
        self.assertEqual(self.portions_of(self.usda_row("sr:900005")),
                         [["1 cup, diced", 132], ["1 oz", 28.4], ["1 cubic inch", 17]])

    def test_portions_without_modifier_or_weight_are_ignored(self):
        self.assertEqual(self.portions_of(self.usda_row("sr:900004")), [["1 cup", 158]])

    def test_rows_are_usda_priority_2_tagged_us(self):
        self.assertEqual(self.n_rows("source = 'usda' AND source_priority = 2 AND region_tags = 'US'"
                                     " AND per_unit = '100g' AND basis = 'database'"), 4)


# --------------------------------------------------------------------------------------
# importer: Open Food Facts file variants
# --------------------------------------------------------------------------------------

class OffOnlyImportTests(DbCase):
    """Only the Open Food Facts file: nothing outranks anchors A1 and A3 here."""

    @classmethod
    def load(cls):
        return off_only_import()

    def test_summary_lists_all_three_sources_even_when_unused(self):
        self.assertEqual(self.summary["read"], {"curated": 0, "usda": 0, "off": 52})

    def test_kept_count(self):
        # 52 read - 2 filtered - 6 invalid - 3 duplicates (Choco Pie, Meiji, Chippy)
        self.assertEqual(self.summary["kept"], {"curated": 0, "usda": 0, "off": 41})
        self.assertEqual(self.n_rows(), 41)

    def test_filtered_invalid_and_duplicate_counts(self):
        self.assertEqual((self.summary["filtered_region"], self.summary["invalid"],
                          self.summary["duplicates"]), (2, 6, 3))

    def test_anchor_a1_brand_is_the_first_of_two(self):
        row = self.off_row("4807770270123")
        self.assertEqual((row["name"], row["brand"]), ("Pancit Canton Kalamansi", "Lucky Me!"))

    def test_anchor_a1_sodium_grams_become_mg(self):
        self.near(self.off_row("4807770270123")["sodium_mg"], 1500.0)

    def test_anchor_a1_numbers(self):
        row = self.off_row("4807770270123")
        expected = {"calories_kcal": 467.0, "protein_g": 11.7, "carbs_g": 56.7, "sugars_g": 5.0,
                    "fat_g": 21.7, "saturates_g": 10.0, "fibre_g": 3.3}
        for col, value in expected.items():
            with self.subTest(col=col):
                self.near(row[col], value)

    def test_twelve_digit_upc_a_is_stored_as_13_digits(self):
        row = self.off_row("0016000275287")
        self.assertEqual((row["name"], row["barcode"]), ("Cheerios", "0016000275287"))


class OffGzipTests(unittest.TestCase):
    def test_gzip_file_gives_the_same_summary(self):
        self.assertEqual(off_gzip_import()["summary"], off_only_import()["summary"])

    def test_gzip_file_gives_the_same_rows(self):
        self.assertEqual(snapshot(off_gzip_import()["db"]), snapshot(off_only_import()["db"]))


class OffCommaSeparatedTests(DbCase):
    """Comma-separated file with only code, product_name, countries_tags, kcal and sodium."""

    @classmethod
    def load(cls):
        return off_comma_import()

    def test_comma_separated_file_is_accepted(self):
        self.assertEqual(self.summary["read"]["off"], 3)
        self.assertEqual(self.summary["kept"]["off"], 2)
        self.assertEqual(self.summary["filtered_region"], 1)

    def test_quoted_comma_stays_inside_the_name(self):
        self.assertEqual(self.off_row("5012345678900")["name"], "Oat Biscuits, plain")

    def test_values_are_read_from_the_right_columns(self):
        row = self.off_row("5012345678900")
        self.near(row["calories_kcal"], 450.0)
        self.near(row["sodium_mg"], 300.0)
        self.assertEqual(row["region_tags"], "UK")

    def test_missing_columns_count_as_blank(self):
        row = self.off_row("5012345678900")
        for col in ("brand", "thumbnail_url") + MACROS:
            self.assertIsNone(row[col], col)
        self.assertEqual(row["popularity"], 0)
        self.assertEqual(self.portions_of(row), [])

    def test_negative_sodium_becomes_null_and_the_row_is_kept(self):
        row = self.off_row("5012345678917")
        self.assertIsNone(row["sodium_mg"])
        self.near(row["calories_kcal"], 40.0)

    def test_negative_sodium_is_not_counted_as_invalid(self):
        self.assertEqual(self.summary["invalid"], 0)


# --------------------------------------------------------------------------------------
# importer: re-runs
# --------------------------------------------------------------------------------------

class IdempotentRerunTests(unittest.TestCase):
    """The same full command run twice on the same database."""

    @classmethod
    def setUpClass(cls):
        cls.r = rerun_import()

    def test_first_run_has_36_rows(self):
        self.assertEqual(self.r["first"]["total_rows"], 56)

    def test_second_run_reports_the_same_total_rows(self):
        self.assertEqual(self.r["second"]["total_rows"], 56)

    def test_second_run_reports_the_same_kept_counts(self):
        self.assertEqual(self.r["second"]["kept"], {"curated": 8, "usda": 11, "off": 37})

    def test_second_run_reads_the_same_inputs(self):
        self.assertEqual(self.r["second"]["read"], {"curated": 11, "usda": 13, "off": 52})

    def test_second_run_reports_the_same_filtered_and_invalid_counts(self):
        self.assertEqual((self.r["second"]["filtered_region"], self.r["second"]["invalid"]), (2, 10))

    def test_row_count_is_unchanged_in_the_database(self):
        self.assertEqual(len(self.r["rows2"]), 56)

    def test_rows_are_identical_after_the_second_run(self):
        self.assertEqual(self.r["rows2"], self.r["rows1"])

    def test_exported_bundle_is_identical_after_the_second_run(self):
        self.assertEqual(self.r["bundle2"], self.r["bundle1"])


class SeparateRunsTests(unittest.TestCase):
    """Three runs on one database, lowest priority first: OFF, then USDA, then curated.

    De-duplication covers everything in the database after each run, so the result must
    be the same rows as the single combined run.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = reversed_runs_import()

    def test_last_run_only_read_the_curated_file(self):
        self.assertEqual(self.r["summary"]["read"], {"curated": 11, "usda": 0, "off": 0})

    def test_final_counts_match_the_combined_run(self):
        self.assertEqual(self.r["summary"]["kept"], {"curated": 8, "usda": 11, "off": 37})
        self.assertEqual(self.r["summary"]["total_rows"], 56)

    def test_later_curated_row_replaces_the_off_row_already_stored(self):
        conn = open_db(self.r["db"])
        try:
            rows = conn.execute("SELECT source, name FROM food_items WHERE barcode = '4807770270123'").fetchall()
        finally:
            conn.close()
        self.assertEqual([tuple(r) for r in rows], [("curated", "Lucky Me! Pancit Canton Kalamansi")])

    def test_final_rows_match_the_combined_run(self):
        self.assertEqual(self.r["rows"], snapshot(full_import()["db"]))


# --------------------------------------------------------------------------------------
# importer: exported app bundle
# --------------------------------------------------------------------------------------

# --------------------------------------------------------------------------------------
# importer: schema version 3 facts (foreign tags, completeness) and the new Open Food Facts columns
# --------------------------------------------------------------------------------------

def ean(prefix, n):
    """The 13-digit code the fixture generator makes: 3 prefix digits + 9-digit serial + a last digit."""
    return "%s%09d0" % (prefix, n)


class RulesCase(DbCase):
    """off_rules.csv alone: 36 rows, one per rule branch, written by tests/fixtures/make_off_fixtures.py."""

    @classmethod
    def load(cls):
        return rules_import()

    def present(self, code):
        return self.n_rows("source = 'off' AND (source_ref = ? OR barcode = ?)", (code, code)) == 1

    def assertDropped(self, code):
        self.assertEqual(self.n_rows("source_ref = ? OR barcode = ?", (code, code)), 0,
                         "the row with code %s should have been dropped" % code)


class RulesSummaryTests(RulesCase):
    def test_counts(self):
        # 36 rows; dropped as invalid: digits-only name, "XY", 0 kcal with macros, a plain 231 kcal with no
        # macros, the larger-tolerance bar, the 5 g sugar chews, the 101 g wafer, the not-label-round chicken
        self.assertEqual(self.summary["read"], {"curated": 0, "usda": 0, "off": 36})
        self.assertEqual(self.summary["invalid"], 8)
        self.assertEqual(self.summary["filtered_region"], 0)
        self.assertEqual(self.summary["duplicates"], 0)
        self.assertEqual(self.summary["kept"], {"curated": 0, "usda": 0, "off": 28})
        self.assertEqual(self.summary["total_rows"], 28)


class ForeignTagsTests(RulesCase):
    """foreign_tags = country tags that are neither wanted (PH US UK JP KR) nor neutral."""

    def test_two_foreign_countries_count_two(self):
        self.assertEqual(self.off_row(ean("001", 3001))["foreign_tags"], 2)       # united-states, france, germany

    def test_every_neutral_tag_counts_zero_even_with_spaces_after_the_commas(self):
        # united-kingdom, scotland, england, wales, northern-ireland, angleterre, angle, international,
        # world, puerto-rico, guam, american-samoa (one tag follows a ", ")
        self.assertEqual(self.off_row(ean("500", 3002))["foreign_tags"], 0)

    def test_europe_and_european_union_are_neutral(self):
        self.assertEqual(self.off_row(ean("001", 3003))["foreign_tags"], 0)       # united-states, world, european-union, europe

    def test_a_plain_wanted_country_row_counts_zero(self):
        self.assertEqual(self.off_row(ean("500", 3005))["foreign_tags"], 0)


class ForeignTagsMainImportTests(FullImportCase):
    def test_one_foreign_tag(self):
        self.assertEqual(self.off_row("4901515110504")["foreign_tags"], 1)        # Kikkoman: france
        self.assertEqual(self.off_row("8996001600269")["foreign_tags"], 1)        # Indomie: indonesia

    def test_two_foreign_tags(self):
        self.assertEqual(self.off_row("5000168001142")["foreign_tags"], 2)        # McVitie's: ireland, france
        self.assertEqual(self.off_row("3012345678903")["foreign_tags"], 2)        # Fromage Blanc: france, germany

    def test_neutral_tags_only(self):
        self.assertEqual(self.off_row("4002468135795")["foreign_tags"], 0)        # Gouda: world, european-union

    def test_curated_and_usda_rows_have_none(self):
        self.assertEqual(self.n_rows("source <> 'off' AND foreign_tags <> 0"), 0)


class CompletenessTests(RulesCase):
    """completeness is stored as an integer percent: round(100 * completeness), NULL when blank."""

    def test_float_noise_cannot_reorder_rows(self):
        self.assertEqual(self.off_row(ean("500", 3002))["completeness"], 79)      # 0.7874999
        self.assertEqual(self.off_row(ean("001", 3003))["completeness"], 79)      # 0.7875

    def test_a_half_is_fifty(self):
        self.assertEqual(self.off_row(ean("500", 3004))["completeness"], 50)

    def test_a_blank_completeness_is_null(self):
        self.assertIsNone(self.off_row(ean("500", 3005))["completeness"])

    def test_the_stored_value_is_an_integer(self):
        kinds = {r[0] for r in self.q("SELECT DISTINCT typeof(completeness) FROM food_items WHERE completeness IS NOT NULL")}
        self.assertEqual(kinds, {"integer"})

def full_completeness():
    def build():
        conn = open_db(full_import()["db"])
        try:
            return {r["source_ref"]: r["completeness"] for r in
                    conn.execute("SELECT source_ref, completeness FROM food_items WHERE source = 'off'")}
        finally:
            conn.close()
    return cached("full_completeness", build)


class CompletenessMainImportTests(unittest.TestCase):
    def test_the_two_float_spellings_of_79_percent_agree(self):
        values = full_completeness()
        self.assertEqual((values["5000159407236"], values["4901330502881"]), (79, 79))   # 0.7875 and 0.7874999

    def test_a_blank_completeness_is_null(self):
        self.assertIsNone(full_completeness()["4901340036512"])                   # Calpis Water

    def test_a_completeness_of_one_is_one_hundred(self):
        self.assertEqual(full_completeness()["4514603365511"], 100)               # Oi Ocha Green Tea

    def test_other_values_are_percent_integers(self):
        values = full_completeness()
        self.assertEqual((values["4800016644504"], values["8801043014830"], values["8801056038861"]),
                         (80, 90, 55))


class CarbohydratesTotalTests(RulesCase):
    def test_carbohydrates_total_is_used_when_carbohydrates_is_blank(self):
        self.near(self.off_row(ean("480", 6001))["carbs_g"], 8.5)

    def test_carbohydrates_100g_wins_when_both_are_given(self):
        self.near(self.off_row(ean("001", 6002))["carbs_g"], 62.0)                # total says 65


class ImageSmallUrlTests(RulesCase):
    def test_image_small_url_is_used_when_the_front_photo_column_is_blank(self):
        self.assertEqual(self.off_row(ean("480", 6001))["thumbnail_url"],
                         THUMB_BASE + "480/000/006/0010/front_en.5.200.jpg")

    def test_a_row_with_no_photo_column_value_has_none(self):
        self.assertIsNone(self.off_row(ean("001", 6002))["thumbnail_url"])


# --------------------------------------------------------------------------------------
# importer: validation of Open Food Facts rows (VAL-1 to VAL-6)
# --------------------------------------------------------------------------------------

class Val5NameTests(RulesCase):
    def test_html_entities_are_decoded_in_name_and_brand_up_to_three_times(self):
        row = self.off_row(ean("500", 5001))
        self.assertEqual(row["name"], "Crunchy oats&honey")                        # "&amp;" once
        self.assertEqual(row["brand"], "M&S")                                     # "&amp;amp;amp;" three times

    def test_a_name_of_digits_only_drops_the_row(self):
        self.assertDropped("6111259347861")

    def test_a_single_character_name_is_replaced_by_the_generic_name(self):
        self.assertEqual(self.off_row(ean("001", 5003))["name"], "Chocolate flavoured cereal")

    def test_two_latin_letters_without_a_digit_are_junk(self):
        self.assertDropped(ean("500", 5007))

    def test_a_letter_plus_digit_name_is_a_real_name(self):
        self.assertEqual(self.off_row(ean("480", 5004))["name"], "c2")

    def test_a_single_cjk_character_is_a_real_name(self):
        self.assertEqual(self.off_row(ean("490", 5005))["name"], "麺")

    def test_text_after_ingredients_is_cut_off_the_name(self):
        self.assertEqual(self.off_row(ean("001", 5006))["name"], "SWEET PICKLE")

    def test_a_placeholder_brand_becomes_null(self):
        self.assertIsNone(self.off_row(ean("001", 5006))["brand"])                # "no brand"

    def test_a_real_brand_is_kept(self):
        self.assertEqual(self.off_row(ean("480", 5004))["brand"], "C2")

    def test_a_generic_name_not_in_the_name_is_added_to_aka(self):
        self.assertIn("Apple Green Tea", json.loads(self.off_row(ean("480", 5004))["aka"]))

    def test_a_generic_name_already_inside_the_name_is_not_added(self):
        self.assertEqual(json.loads(self.off_row(ean("500", 5001))["aka"]), [])   # "Crunchy oats" is in the name

    def test_a_generic_name_without_a_latin_letter_is_not_added(self):
        self.assertEqual(json.loads(self.off_row(ean("490", 5005))["aka"]), [])


class Val3SodiumTests(RulesCase):
    """sodium a = sodium_100g x 1000 mg, salt b = salt_100g x 400 mg."""

    def sodium(self, code):
        return self.off_row(code)["sodium_mg"]

    def test_a_sodium_out_of_range_falls_back_to_salt(self):
        self.near(self.sodium(ean("001", 3001)), 440.0)       # sodium 45 g = 45 000 mg, salt 1.1 g

    def test_both_figures_within_25_percent_use_sodium(self):
        self.near(self.sodium(ean("500", 3002)), 500.0)       # 500 mg against 600 mg

    def test_a_unit_slip_takes_the_larger_figure(self):
        self.near(self.sodium(ean("001", 3003)), 700.0)       # 1.75 mg against 700 mg

    def test_a_false_zero_takes_the_salt_figure(self):
        self.near(self.sodium(ean("500", 3004)), 56.0)        # sodium 0, salt 0.14 g

    def test_a_real_conflict_is_null_and_the_row_is_kept(self):
        row = self.off_row(ean("500", 3005))                  # 500 mg against 1 500 mg
        self.assertIsNone(row["sodium_mg"])
        self.near(row["calories_kcal"], 350.0)


class Val3SodiumOffOnlyTests(DbCase):
    """The anchor rows with only Open Food Facts around, so none of them lost to a curated or USDA row."""

    @classmethod
    def load(cls):
        return off_only_import()

    def test_sodium_only_and_salt_only_rows_are_unchanged(self):
        self.near(self.off_row("0016000275287")["sodium_mg"], 497.0)   # sodium 0.497 g, no salt
        self.near(self.off_row("5000159407236")["sodium_mg"], 252.0)   # salt 0.63 g, no sodium

    def test_agreeing_figures_use_sodium(self):
        self.near(self.off_row("4807770270123")["sodium_mg"], 1500.0)  # sodium 1.5 g, salt 3.75 g = 1 500 mg

    def test_sodium_over_the_limit_with_no_salt_is_null(self):
        self.assertIsNone(self.off_row("8801007000015")["sodium_mg"])  # 45 g and no salt


class Val8SodiumTests(RulesCase):
    def test_sodium_typed_in_mg_is_nulled_when_a_serving_would_hold_over_4000_mg(self):
        row = self.off_row(ean("480", 8001))                  # 28 000 mg per 100 g, 30 g serving: 8 400 mg
        self.assertIsNone(row["sodium_mg"])
        self.near(row["calories_kcal"], 520.0)

    def test_a_salty_food_is_kept_when_a_serving_holds_under_4000_mg(self):
        self.near(self.off_row(ean("500", 8002))["sodium_mg"], 8000.0)   # 8 000 mg per 100 g, 30 g: 2 400 mg

    def test_the_rule_is_off_for_a_100_g_serving(self):
        self.near(self.off_row(ean("500", 8003))["sodium_mg"], 28000.0)


class Val1CalorieTests(RulesCase):
    def test_zero_calories_with_real_macros_is_dropped(self):
        self.assertDropped(ean("500", 1001))

    def test_a_consistent_row_is_kept(self):
        self.assertTrue(self.present(ean("500", 8002)))
        self.assertTrue(self.present(ean("001", 6005)))

    def test_an_alcoholic_drink_named_vodka_is_kept_with_its_calories(self):
        row = self.off_row(ean("500", 1002))
        self.near(row["calories_kcal"], 231.0)

    def test_the_same_numbers_without_an_alcohol_cue_are_dropped(self):
        self.assertDropped(ean("500", 1003))

    def test_the_larger_tolerance_is_used_not_the_sum(self):
        # 222 kcal against 432 kcal from the macros (sugars 49 g) for a 16 g serving
        self.assertDropped(ean("500", 1004))

    def test_a_zero_sugar_sweetener_with_low_calories_is_kept(self):
        self.near(self.off_row(ean("500", 1005))["calories_kcal"], 5.0)

    def test_the_same_sweetener_with_5_g_sugars_is_dropped(self):
        self.assertDropped(ean("500", 1006))

    def test_a_row_whose_calories_match_its_sugars_but_not_its_carbs_survives_the_ceiling(self):
        # Pocari Sweat: 25 kcal, carbs 0, sugars 5.8: the ceiling uses the larger of the two
        self.assertTrue(self.present("4987035332510"))


class Val2RepairTests(RulesCase):
    def test_r1_zero_kcal_with_a_kilojoule_figure_is_repaired_as_an_estimate(self):
        row = self.off_row(ean("480", 2001))                  # 2042 kJ / 4.184 = 488.0
        self.near(row["calories_kcal"], 488.0)
        self.assertEqual(row["basis"], "estimate")

    def test_r1_is_not_used_when_protein_carbs_and_fat_add_up_to_more_than_100(self):
        self.assertDropped(ean("480", 2002))                  # 25 + 70 + 6 = 101

    def test_r2_macros_entered_per_30_g_serving_are_scaled_to_100_g(self):
        row = self.off_row(ean("001", 2003))                  # factor 100 / 30; calories are kept
        self.near(row["calories_kcal"], 700.0)
        expected = {"protein_g": 10.0, "carbs_g": 13.33, "sugars_g": 3.33, "fat_g": 73.33,
                    "saturates_g": 6.67, "fibre_g": 10.0}
        for col, value in expected.items():
            with self.subTest(col=col):
                self.near(row[col], value, places=2)

    def test_r2_scales_a_round_sodium_figure_too(self):
        self.near(self.off_row(ean("001", 2003))["sodium_mg"], 416.7)   # 125 mg x 100 / 30

    def test_r2_macros_entered_per_500_ml_bottle_are_scaled_to_100_ml(self):
        row = self.off_row(ean("001", 2004))                  # factor 100 / 500
        self.near(row["calories_kcal"], 49.0)
        expected = {"protein_g": 4.0, "carbs_g": 6.0, "sugars_g": 5.6, "fat_g": 1.0, "saturates_g": 0.6,
                    "fibre_g": 0.0}
        for col, value in expected.items():
            with self.subTest(col=col):
                self.near(row[col], value, places=2)
        self.near(row["sodium_mg"], 100.0)

    def test_r2_is_refused_when_the_macros_are_not_multiples_of_half_a_gram(self):
        self.assertDropped(ean("500", 2005))                  # protein 22.3 g


class Val4SugarsSaturatesTests(RulesCase):
    def test_sugars_far_above_carbs_become_null_when_the_calories_follow_carbs(self):
        row = self.off_row(ean("500", 6003))                  # carbs 14.7, sugars 74.7, 70 kcal
        self.assertIsNone(row["sugars_g"])
        self.near(row["carbs_g"], 14.7)

    def test_carbs_take_the_sugars_value_when_the_calories_follow_sugars(self):
        row = self.off_row("4987035332510")                   # 25 kcal, carbs 0, sugars 5.8
        self.near(row["carbs_g"], 5.8)
        self.near(row["sugars_g"], 5.8)

    def test_sugars_slightly_above_carbs_are_clamped_to_carbs(self):
        self.near(self.off_row(ean("001", 6005))["sugars_g"], 4.83)   # carbs 4.83, sugars 4.88

    def test_saturates_far_above_fat_become_null(self):
        row = self.off_row(ean("001", 6002))                  # fat 1.61, saturates 6.45
        self.assertIsNone(row["saturates_g"])
        self.near(row["fat_g"], 1.61)

    def test_saturates_slightly_above_fat_are_clamped_to_fat(self):
        self.near(self.off_row(ean("001", 6005))["saturates_g"], 1.5)  # fat 1.5, saturates 1.8


class Val6PortionTests(RulesCase):
    def portions(self, code):
        return json.loads(self.off_row(code)["portions"])

    def test_a_thousands_separator_text_multiplies_the_quantity_by_1000(self):
        self.assertEqual(self.portions(ean("480", 6001)), [["1,330g", 1330]])

    def test_a_bare_number_serving_of_30_without_a_quantity_becomes_30_g(self):
        self.assertEqual(self.portions(ean("001", 6002)), [["30 g", 30]])

    def test_a_bare_number_under_5_gives_no_portion(self):
        self.assertEqual(self.portions(ean("500", 6003)), [])

    def test_a_portion_worth_more_than_1000_kcal_is_not_stored(self):
        self.assertEqual(self.portions(ean("500", 6004)), [])          # 400 kcal x 300 g / 100 = 1 200

    def test_a_100_g_serving_text_is_not_stored_but_the_row_is_kept(self):
        self.assertEqual(self.portions(ean("500", 8003)), [])          # "1 serving (100 g)"

    def test_a_100_g_portion_with_another_unit_word_is_stored(self):
        self.assertEqual(self.portions(ean("001", 6005)), [["1 cup (100 g)", 100]])

    def test_a_bare_one_gram_portion_is_not_stored(self):
        self.assertEqual(self.portions(ean("500", 6006)), [])


class Val6PortionMainImportTests(FullImportCase):
    def test_a_100_g_serving_is_not_stored(self):
        self.assertEqual(self.portions_of(self.off_row("2000000012345")), [])     # Deli Roast: "100 g"

    def test_ordinary_portions_are_still_stored(self):
        self.assertEqual(self.portions_of(self.off_row("5000159407236")), [["48 g", 48]])
        self.assertEqual(self.portions_of(self.off_row("4901340036512")), [["500 ml", 500]])


class SingleBlankMacroTests(RulesCase):
    """When exactly one of protein, carbs, fat is blank and the calories imply 1 to 100 g, it is filled."""

    def test_a_blank_carbs_value_is_filled_from_the_calories_as_an_estimate(self):
        row = self.off_row(ean("001", 7001))                  # (459 - 4 x 9.4 - 9 x 18.8) / 4 = 63.05
        self.near(row["carbs_g"], 63.05, places=2)
        self.assertEqual(row["basis"], "estimate")

    def test_a_value_that_works_out_below_1_g_is_left_blank(self):
        row = self.off_row(ean("001", 7002))                  # protein = (290 - 200 - 90) / 4 = 0
        self.assertIsNone(row["protein_g"])
        self.assertEqual(row["basis"], "database")

    def test_nothing_is_filled_when_protein_carbs_and_fat_would_exceed_105_g(self):
        row = self.off_row(ean("001", 7003))                  # carbs would be 52.5, total 112.5
        self.assertIsNone(row["carbs_g"])
        self.assertEqual(row["basis"], "database")

    def test_two_blank_macros_are_not_filled(self):
        row = self.off_row(ean("500", 6004))                  # only protein (10 g) is given, 400 kcal
        self.assertEqual((row["carbs_g"], row["fat_g"], row["basis"]), (None, None, "database"))
        self.near(row["protein_g"], 10.0)


class SingleBlankMainImportTests(FullImportCase):
    def test_three_blank_macros_are_not_filled(self):
        row = self.off_row("4901340036512")                   # Calpis Water: calories only
        for col in ("protein_g", "carbs_g", "fat_g"):
            self.assertIsNone(row[col], col)


# --------------------------------------------------------------------------------------
# importer: de-duplication of Open Food Facts rows (DD-1 to DD-8, SEL-8)
# --------------------------------------------------------------------------------------

class DedupeCase(unittest.TestCase):
    """off_dedupe.csv with the flat USDA file, imported twice and exported (15 rows in five groups)."""

    @classmethod
    def setUpClass(cls):
        cls.imp = dedupe_import()
        cls.doc = cls.imp["doc"]
        cls.db = open_db(cls.imp["db"])

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def rows(self, name_key):
        return self.db.execute("SELECT * FROM food_items WHERE name_key = ?", (name_key,)).fetchall()

    def survivor(self, name_key):
        found = self.rows(name_key)
        self.assertEqual(len(found), 1, "expected one row with the key %r, found %d" % (name_key, len(found)))
        return found[0]

    def bundle_row(self, barcode):
        fields = self.doc["fields"]
        found = [dict(zip(fields, r)) for r in self.doc["items"] if r[fields.index("barcode")] == barcode]
        self.assertEqual(len(found), 1, "expected one bundle row with barcode %s" % barcode)
        return found[0]

    def aliases_by_survivor(self):
        fields = self.doc["fields"]
        out = {}
        for entry in self.doc.get("alias", []):
            item = self.doc["items"][entry[0]]
            out[item[fields.index("barcode")]] = set(entry[1:])
        return out


class Dd1CalorieGateTests(DedupeCase):
    def test_a_row_within_10_percent_of_the_larger_calories_merges(self):
        # 100 / 111 / 112 kcal: 111 merges into the 100 kcal row (11 <= 11.1), 112 does not (12 > 11.2)
        rows = self.rows("kelloggs|corn flakes plain")
        self.assertEqual(sorted((r["barcode"], r["calories_kcal"]) for r in rows),
                         [("0038000000011", 100.0), ("0038000000035", 112.0)])

    def test_the_merged_rows_barcode_is_gone_from_the_database_as_a_row(self):
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM food_items WHERE barcode = '0038000000028'"
                                         " OR source_ref = '0038000000028'").fetchone()[0], 0)

    def test_two_kcal_and_six_kcal_merge_inside_the_5_kcal_floor(self):
        row = self.survivor("pepsi|diet cola zero")
        self.assertEqual((row["barcode"], row["calories_kcal"]), ("0012000000014", 2.0))

    def test_an_open_food_facts_row_300_kcal_from_a_usda_row_at_594_does_not_merge_into_it(self):
        rows = self.rows("skippy|creamy peanut butter")
        self.assertEqual(sorted((r["source"], r["barcode"]) for r in rows),
                         [("off", "0037600100002"), ("usda", "0037600110754")])


class Dd1WinnerOrderTests(DedupeCase):
    def test_a_row_whose_energy_matches_its_macros_beats_a_fuller_row_that_does_not(self):
        # X: 546 kcal with 4P+4C+9F = 420 (7 numbers); Y: 496 kcal (3 numbers). Y wins.
        row = self.survivor("planters|mixed nut clusters")
        self.assertEqual((row["barcode"], row["calories_kcal"]), ("0029000000022", 496.0))

    def test_the_loser_barcode_is_kept_as_an_alias(self):
        self.assertIn("0029000000015", self.survivor("planters|mixed nut clusters")["alt_barcodes"])


class Dd5PopularityTests(DedupeCase):
    def test_the_survivor_takes_the_larger_popularity(self):
        self.assertEqual(self.survivor("planters|mixed nut clusters")["popularity"], 20)    # own 5, merged 20

    def test_the_survivor_keeps_its_own_popularity_when_it_is_larger(self):
        self.assertEqual(self.survivor("munchies|chocolate wafer bites")["popularity"], 50)

    def test_the_first_kept_row_of_a_group_keeps_nine_scans(self):
        row = [r for r in self.rows("kelloggs|corn flakes plain") if r["barcode"] == "0038000000011"][0]
        self.assertEqual(row["popularity"], 9)


class Dd2AliasDatabaseTests(DedupeCase):
    def test_every_alias_is_kept_in_the_database_whatever_its_popularity(self):
        text = self.survivor("munchies|chocolate wafer bites")["alt_barcodes"]
        json.loads(text)
        for code in ("5010000000019", "4901234567894", "0040000000029"):
            self.assertIn(code, text)

    def test_the_survivors_own_barcode_is_not_its_own_alias(self):
        row = self.survivor("munchies|chocolate wafer bites")
        self.assertNotIn(row["barcode"], row["alt_barcodes"])

    def test_alias_lists_are_json_and_duplicate_free(self):
        for row in self.db.execute("SELECT name_key, alt_barcodes FROM food_items"):
            codes = json.loads(row["alt_barcodes"])
            self.assertIsInstance(codes, list, row["name_key"])
            self.assertEqual(len(codes), len({json.dumps(c) for c in codes}), row["name_key"])


class Dd2AliasBundleTests(DedupeCase):
    def test_alias_entries_point_at_the_right_survivors(self):
        self.assertEqual(self.aliases_by_survivor(), {
            "0038000000011": {38000000028},                         # the 111 kcal pack, 4 scans
            "0012000000014": {12000000021},                         # the 6 kcal pack, 1 scan
            "0029000000022": {29000000015},                         # the 525 kcal pack
            "0040000000012": {4901234567894, 40000000029},          # JP-tagged (0 scans) and 2-scan packs
            "3012345000050": {40000000036, 4801234000019},          # a 4-scan US pack and a PH pack
        })

    def test_an_alias_whose_own_row_had_no_scans_and_no_ph_jp_kr_tag_is_filtered_out(self):
        listed = {code for entry in self.doc["alias"] for code in entry[1:]}
        self.assertNotIn(5010000000019, listed)                     # UK-tagged, never scanned

    def test_a_never_scanned_jp_tagged_alias_is_kept(self):
        self.assertIn(4901234567894, self.aliases_by_survivor()["0040000000012"])

    def test_a_survivor_without_a_portion_takes_the_merged_rows_portion(self):
        self.assertIn(["30 g", 30], self.bundle_row("0040000000012")["portions"])

    def test_the_alias_codes_are_integers_without_leading_zeros(self):
        for entry in self.doc["alias"]:
            for code in entry[1:]:
                self.assertIs(type(code), int)
        self.assertIn(12000000021, self.aliases_by_survivor()["0012000000014"])


class Sel8AndDd7Tests(DedupeCase):
    def test_the_survivor_takes_the_smallest_foreign_tag_count_of_its_group(self):
        row = self.survivor("hazelo|hazelnut spread jar")
        self.assertEqual(row["foreign_tags"], 0)             # it came with france + germany; the others have none

    def test_a_ph_tagged_row_does_not_win_a_tie_break(self):
        row = self.survivor("hazelo|hazelnut spread jar")
        self.assertEqual(row["barcode"], "3012345000050")    # the fuller row, not the PH-tagged one
        self.assertEqual(row["region_tags"], "PH,US")

    def test_the_survivor_is_exported(self):
        self.assertEqual(self.bundle_row("3012345000050")["name"], "Hazelnut Spread Jar")


class DedupeRerunTests(DedupeCase):
    def test_a_second_run_leaves_the_same_rows(self):
        self.assertEqual(self.imp["rows2"], self.imp["rows1"])

    def test_a_second_run_writes_the_same_bundle(self):
        self.assertEqual(self.imp["doc2"], self.doc)


class AliasGuardTests(unittest.TestCase):
    """A stored alias that has since become a row's own barcode must not be exported."""

    @classmethod
    def setUpClass(cls):
        cls.imp = alias_guard_import()
        cls.doc = cls.imp["doc"]

    def listed(self):
        return {code for entry in self.doc.get("alias", []) for code in entry[1:]}

    def test_the_new_row_owns_the_barcode_in_the_bundle(self):
        fields = self.doc["fields"]
        owners = [r[0] for r in self.doc["items"] if r[fields.index("barcode")] == "0038000000028"]
        self.assertEqual(owners, ["Totally Different Snack"])

    def test_that_code_is_not_listed_as_an_alias(self):
        self.assertNotIn(38000000028, self.listed())

    def test_other_aliases_are_still_listed(self):
        self.assertIn(29000000015, self.listed())


class BundleCase(unittest.TestCase):
    doc = None

    def recs(self):
        fields = self.doc["fields"]
        return [dict(zip(fields, row)) for row in self.doc["items"]]

    def rec(self, **match):
        found = [r for r in self.recs() if all(r.get(k) == v for k, v in match.items())]
        self.assertEqual(len(found), 1, "expected exactly one bundle row with %r, found %d"
                         % (match, len(found)))
        return found[0]

    def brand_of(self, rec):
        return None if rec["brand"] == -1 else self.doc["brands"][rec["brand"]]

    def group_of(self, rec):
        return None if rec["group"] == -1 else self.doc["groups"][rec["group"]]


class BundleTests(BundleCase):
    @classmethod
    def setUpClass(cls):
        imp = full_import()
        with open(imp["bundle"], "rb") as f:
            cls.raw = f.read()
        cls.doc = json.loads(cls.raw.decode("utf-8"))

    # ---- envelope
    def test_version_is_2(self):
        self.assertEqual(self.doc["version"], 2)

    def test_built_is_the_date_given_on_the_command_line(self):
        self.assertEqual(self.doc["built"], BUILT)

    def test_note_is_a_sentence(self):
        self.assertIsInstance(self.doc["note"], str)
        self.assertTrue(self.doc["note"].strip())

    def test_fields_list_is_exact(self):
        self.assertEqual(self.doc["fields"], BUNDLE_FIELDS)

    def test_thumb_base(self):
        self.assertEqual(self.doc["thumbBase"], THUMB_BASE)

    def test_counts(self):
        self.assertEqual(self.doc["counts"], {"curated": 8, "usda": 11, "off": 29})

    def test_items_hold_every_row(self):
        self.assertEqual(len(self.doc["items"]), 48)

    def test_every_row_has_one_value_per_field(self):
        for row in self.doc["items"]:
            self.assertEqual(len(row), len(self.doc["fields"]), row[:1])

    def test_file_is_utf8_with_unescaped_characters(self):
        self.assertIn("Nestlé".encode("utf-8"), self.raw)
        self.assertNotIn(b"\\u00e9", self.raw)

    def test_file_is_compact_json(self):
        compact = json.dumps(self.doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # a line break per row would still be fine; padded separators would not
        self.assertLessEqual(len(self.raw), len(compact) + len(self.doc["items"]) + 16)

    # ---- string tables
    def test_brands_table_holds_each_brand_once(self):
        brands = self.doc["brands"]
        self.assertTrue(all(isinstance(b, str) and b for b in brands))
        self.assertEqual(len(brands), len(set(brands)))
        for expected in ("Jollibee", "Lucky Me!", "Starbucks", "General Mills", "Mars", "Nestlé"):
            self.assertIn(expected, brands)

    def test_groups_table_holds_each_group_once(self):
        groups = self.doc["groups"]
        self.assertTrue(all(isinstance(g, str) and g for g in groups))
        self.assertEqual(len(groups), len(set(groups)))
        for expected in ("Home dish", "Jollibee", "Bananas", "Burgers"):
            self.assertIn(expected, groups)

    def test_brand_and_group_indexes_are_in_range(self):
        for r in self.recs():
            self.assertIsInstance(r["brand"], int)
            self.assertIsInstance(r["group"], int)
            self.assertTrue(-1 <= r["brand"] < len(self.doc["brands"]), r["name"])
            self.assertTrue(-1 <= r["group"] < len(self.doc["groups"]), r["name"])

    def test_brand_index_points_at_the_brand(self):
        self.assertEqual(self.brand_of(self.rec(name="Chickenjoy 1 pc (thigh)")), "Jollibee")
        self.assertEqual(self.brand_of(self.rec(name="Snickers")), "Mars")
        self.assertEqual(self.brand_of(self.rec(barcode="0016000275287")), "General Mills")

    def test_row_without_brand_has_minus_one(self):
        self.assertEqual(self.rec(name="Chicken adobo")["brand"], -1)
        self.assertEqual(self.rec(name="Baked Beans in Tomato Sauce")["brand"], -1)

    def test_group_index_points_at_the_group(self):
        self.assertEqual(self.group_of(self.rec(name="Chicken adobo")), "Home dish")
        self.assertEqual(self.group_of(self.rec(name="Banana, raw")), "Bananas")
        self.assertEqual(self.group_of(self.rec(name="Lucky Me! Pancit Canton Kalamansi")), "Lucky Me!")

    # ---- codes
    def test_category_codes(self):
        expected = {"Lucky Me! Pancit Canton Kalamansi": 0, "Snickers": 0,       # supermarket
                    "Chickenjoy 1 pc (thigh)": 1, "Big Mac (McDonalds)": 1,      # dining_out
                    "Chicken adobo": 2, "Banana, raw": 2}                        # whole_food
        for name, code in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.rec(name=name)["category"], code)

    def test_source_codes(self):
        self.assertEqual(self.rec(name="Chickenjoy 1 pc (thigh)")["source"], 0)   # curated
        self.assertEqual(self.rec(name="Banana, raw")["source"], 1)               # usda survey
        self.assertEqual(self.rec(name="Oats, rolled, dry")["source"], 2)         # usda sr legacy
        self.assertEqual(self.rec(name="Hummus, commercial")["source"], 2)        # usda foundation
        self.assertEqual(self.rec(barcode="0016000275287")["source"], 2)          # usda branded
        self.assertEqual(self.rec(name="Snickers")["source"], 3)                  # open food facts

    def test_per_flag_is_1_only_for_the_per_serving_row(self):
        self.assertEqual(self.rec(name="Flat White (tall)")["per"], 1)
        self.assertEqual([r["name"] for r in self.recs() if r["per"] != 0], ["Flat White (tall)"])

    def test_per_flag_is_0_for_a_converted_row(self):
        self.assertEqual(self.rec(name="Chickenjoy 1 pc (thigh)")["per"], 0)

    def test_regions_are_a_comma_string(self):
        self.assertEqual(self.rec(name="Flat White (tall)")["regions"], "PH,US")
        self.assertEqual(self.rec(name="Shin Ramyun")["regions"], "US,KR")
        self.assertEqual(self.rec(name="Banana, raw")["regions"], "US")

    # ---- barcode and thumbnail
    def test_barcode_is_the_canonical_string(self):
        self.assertEqual(self.rec(name="Lucky Me! Pancit Canton Kalamansi")["barcode"], "4807770270123")
        self.assertEqual(self.rec(name="Snickers")["barcode"], "5000159407236")

    def test_missing_barcode_is_zero(self):
        self.assertEqual(self.rec(name="Chickenjoy 1 pc (thigh)")["barcode"], 0)
        self.assertEqual(self.rec(name="Deli Roast Chicken Slices")["barcode"], 0)

    def test_thumb_has_the_base_prefix_removed(self):
        self.assertEqual(self.rec(name="Snickers")["thumb"], "500/015/940/7236/front_en.4.200.jpg")

    def test_inherited_thumb_is_exported_for_the_curated_row(self):
        self.assertEqual(self.rec(name="Lucky Me! Pancit Canton Kalamansi")["thumb"],
                         "480/777/027/0123/front_en.3.200.jpg")

    def test_thumb_on_another_host_is_exported_in_full(self):
        self.assertEqual(self.rec(name="Spam Classic")["thumb"],
                         "https://example.org/img/spam-classic.jpg")

    def test_missing_thumb_is_zero(self):
        self.assertEqual(self.rec(name="Chickenjoy 1 pc (thigh)")["thumb"], 0)
        self.assertEqual(self.rec(name="Banana Flavored Milk")["thumb"], 0)

    def test_no_thumb_still_carries_the_base_prefix(self):
        for r in self.recs():
            if r["thumb"] != 0:
                self.assertIsInstance(r["thumb"], str)
                self.assertFalse(r["thumb"].startswith(THUMB_BASE), r["name"])

    # ---- numbers
    def test_kcal_is_an_integer_on_every_row(self):
        for r in self.recs():
            self.assertIs(type(r["kcal"]), int, r["name"])

    def test_kcal_values(self):
        expected = {"Chickenjoy 1 pc (thigh)": 304, "Lucky Me! Pancit Canton Kalamansi": 467,
                    "Flat White (tall)": 170, "Snickers": 485, "Banana, raw": 97,
                    "Weetabix Original": 366, "Oi Ocha Green Tea": 0}
        for name, kcal in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.rec(name=name)["kcal"], kcal)

    def test_macros_have_at_most_one_decimal(self):
        for r in self.recs():
            for f in ("protein", "carbs", "sugars", "fat", "saturates", "fibre"):
                v = r[f]
                if v is not None:
                    self.assertNotIsInstance(v, bool)
                    self.assertAlmostEqual(v, round(v, 1), places=9, msg="%s of %s" % (f, r["name"]))

    def test_macro_values_of_a_converted_curated_row(self):
        r = self.rec(name="Lucky Me! Pancit Canton Kalamansi")
        got = [r[f] for f in ("protein", "carbs", "sugars", "fat", "saturates", "fibre")]
        for value, expected in zip(got, [10, 58.3, 5, 20, 10, 3.3]):
            self.assertAlmostEqual(value, expected, places=6)

    def test_macro_values_of_a_usda_row(self):
        r = self.rec(name="Banana, raw")
        got = [r[f] for f in ("protein", "carbs", "sugars", "fat", "saturates", "fibre")]
        for value, expected in zip(got, [0.7, 23, 15.8, 0.3, 0.1, 1.7]):
            self.assertAlmostEqual(value, expected, places=6)

    def test_unknown_macros_are_null(self):
        r = self.rec(name="Chickenjoy 1 pc (thigh)")
        self.assertEqual([r[f] for f in ("sugars", "saturates", "fibre", "sodium")], [None] * 4)

    def test_sodium_is_an_integer_or_null_on_every_row(self):
        for r in self.recs():
            if r["sodium"] is not None:
                self.assertIs(type(r["sodium"]), int, r["name"])

    def test_sodium_values(self):
        expected = {"Snickers": 252, "Lucky Me! Pancit Canton Kalamansi": 1500, "Shin Ramyun": 1790,
                    "Flat White (tall)": 115, "Banana, raw": 0, "Baked Beans in Tomato Sauce": 245}
        for name, sodium in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.rec(name=name)["sodium"], sodium)

    # ---- portions and other names
    def test_portions_have_the_default_first(self):
        self.assertEqual(self.rec(name="Chickenjoy 1 pc (thigh)")["portions"], [["1 thigh", 125]])
        self.assertEqual(self.rec(name="Sinangag (garlic fried rice)")["portions"],
                         [["100 g", 100], ["1 cup", 160], ["1/2 cup", 80], ["1 platter", 320]])   # 100 g first (rule)
        self.assertEqual(self.rec(name="Banana, raw")["portions"], [["1 banana", 126]])        # a counted fruit keeps its unit

    def test_portion_without_grams_has_null(self):
        self.assertEqual(self.rec(name="Flat White (tall)")["portions"], [["1 tall (12 fl oz)", None]])

    def test_row_without_portions_has_an_empty_list(self):
        self.assertEqual(self.rec(name="Milo Activ-Go Powder")["portions"], [])

    def test_aka_is_a_list_when_there_are_other_names(self):
        self.assertEqual(self.rec(name="Chickenjoy 1 pc (thigh)")["aka"], ["chicken joy"])

    def test_aka_is_zero_when_empty(self):
        self.assertEqual(self.rec(name="Flat White (tall)")["aka"], 0)
        self.assertEqual(self.rec(name="Banana, raw")["aka"], 0)
        self.assertEqual(self.rec(name="Snickers")["aka"], 0)

    # ---- order
    def test_rows_are_grouped_curated_survey_other_usda_off(self):
        self.assertEqual([r["source"] for r in self.recs()], [0] * 8 + [1] * 3 + [2] * 8 + [3] * 29)

    def test_off_rows_come_in_pool_order_ph_us_uk_jp_kr_as(self):
        got = [(r["name"], r["barcode"]) for r in self.recs() if r["source"] == 3]
        self.assertEqual(got, OFF_EXPORT_ORDER)

    def test_dropped_duplicates_are_not_exported(self):
        names = [r["name"] for r in self.recs()]
        for gone in ("Pancit Canton Kalamansi", "Choco Pie", "Meiji Milk Choco",
                     "Jack 'n Jill Chippy BBQ", "Skippy Creamy Peanut Butter", "SkyFlakes Crackers"):
            self.assertNotIn(gone, names)

    # ---- alias
    def test_alias_is_a_list_of_item_index_then_integer_codes(self):
        alias = self.doc["alias"]
        self.assertIsInstance(alias, list)
        for entry in alias:
            self.assertIsInstance(entry, list)
            self.assertGreaterEqual(len(entry), 2)
            self.assertIs(type(entry[0]), int)
            self.assertTrue(0 <= entry[0] < len(self.doc["items"]), entry)
            for code in entry[1:]:
                self.assertIs(type(code), int, entry)

    def test_alias_points_at_the_right_item_index(self):
        by_name = {}
        for entry in self.doc["alias"]:
            by_name.setdefault(self.doc["items"][entry[0]][0], set()).update(entry[1:])
        self.assertEqual(by_name, MAIN_ALIASES)

    def test_alias_codes_have_no_leading_zeros_and_equal_the_stored_barcodes(self):
        # the USDA survivor of "Skippy" lists the 12-digit UPC 037600105002 as the number 37600105002
        entry = [e for e in self.doc["alias"] if self.doc["items"][e[0]][0] == "CREAMY PEANUT BUTTER"][0]
        self.assertEqual(entry[1:], [37600105002])

    def test_alias_does_not_list_a_survivors_own_barcode(self):
        own = {int(r["barcode"]) for r in self.recs() if r["barcode"]}
        listed = {code for entry in self.doc["alias"] for code in entry[1:]}
        self.assertEqual(own & listed, set())

    def test_alias_never_names_an_item_twice(self):
        indexes = [entry[0] for entry in self.doc["alias"]]
        self.assertEqual(len(indexes), len(set(indexes)))

    def test_version_and_fields_are_unchanged_by_the_alias_key(self):
        self.assertEqual(self.doc["version"], 2)
        self.assertEqual(self.doc["fields"], BUNDLE_FIELDS)

    # ---- the 100 g default serving for Open Food Facts rows
    def test_packaged_rows_get_100_g_first(self):
        self.assertEqual(self.rec(name="Snickers")["portions"], [["100 g", 100], ["48 g", 48]])
        self.assertEqual(self.rec(name="Weetabix Original")["portions"],
                         [["100 g", 100], ["2 biscuits (37.5 g)", 37.5]])
        self.assertEqual(self.rec(name="Baked Beans in Tomato Sauce")["portions"],
                         [["100 g", 100], ["1/2 can (200 g)", 200]])
        self.assertEqual(self.rec(name="Pocky Chocolate")["portions"], [["100 g", 100], ["1 pack (36 g)", 36]])

    def test_a_packaged_drink_keeps_its_ml_serving_first(self):
        self.assertEqual(self.rec(name="Banana Flavored Milk")["portions"], [["240 ml", 240]])

    def test_a_spoon_food_keeps_its_spoon_first(self):
        self.assertEqual(self.rec(name="Kewpie Mayonnaise")["portions"], [["1 tbsp (15 g)", 15]])
        self.assertEqual(self.rec(name="Kikkoman Soy Sauce")["portions"], [["1 tbsp (15 ml)", 15]])

    def test_a_row_whose_only_portion_is_100_g_has_none_stored_and_none_exported(self):
        self.assertEqual(self.rec(name="Deli Roast Chicken Slices")["portions"], [])


class DefaultPortionTests(unittest.TestCase):
    """The 100 g default rule of the exporter (default_portions in scripts/import_foods.py)."""

    @classmethod
    def setUpClass(cls):
        import import_foods
        cls.rule = staticmethod(import_foods.default_portions)

    def apply(self, name, group, portions, category="whole_food", per_unit="100g", source="usda"):
        # a real food_items row also names its source, which the Open Food Facts cases below rely on
        item = {"name": name, "group_label": group, "category": category, "per_unit": per_unit,
                "source": source}
        return self.rule(item, portions)

    def assertKeeps(self, name, group, portions, category="whole_food"):
        self.assertEqual(self.apply(name, group, portions, category), portions, name)

    def assertHundredFirst(self, name, group, portions, category="whole_food"):
        got = self.apply(name, group, portions, category)
        self.assertEqual(got[0], ["100 g", 100], name)
        self.assertEqual(got[1:], [p for p in portions if p[1] != 100], name)

    # ---- general case
    def test_cooked_rice_gets_100_g_first_and_keeps_the_others_in_order(self):
        self.assertEqual(self.apply("Steamed white rice", "Rice & staples", [["1 cup cooked", 158], ["1 bowl", 250]]),
                         [["100 g", 100], ["1 cup cooked", 158], ["1 bowl", 250]])

    def test_meat_cuts_are_weighed_not_counted(self):
        self.assertHundredFirst("Beef, steak", "Beef, excludes ground", [["1 regular", 160], ["1 thick", 240]])
        self.assertHundredFirst("Chicken thigh, cooked", "Meat & poultry", [["1 thigh", 80]])
        self.assertHundredFirst("Chicken wing", "Chicken, whole pieces", [["1 wing", 35]])
        self.assertHundredFirst("Salmon fillet", "Fish", [["1 fillet", 140]])

    def test_cheese_cereal_vegetables_and_packaged_snacks_get_100_g_first(self):
        self.assertHundredFirst("Cheddar cheese", "Dairy & eggs", [["1 slice", 21], ["1 cup, shredded", 113]])
        self.assertHundredFirst("Cornflakes", "Breakfast Cereals", [["1 cup", 28]])
        self.assertHundredFirst("Potato, boiled", "Vegetables", [["1 medium", 170]])
        self.assertHundredFirst("Potato chips", "Snacks", [["1 bag", 28]], "supermarket")

    def test_an_existing_100_g_portion_is_not_listed_twice(self):
        self.assertEqual(self.apply("Sweet potato", "Vegetables", [["1 serving", 100], ["1 medium", 150]]),
                         [["100 g", 100], ["1 medium", 150]])

    def test_rows_per_serving_without_portions_or_without_gram_weight_are_untouched(self):
        self.assertEqual(self.apply("Garlic fried rice", "Rice", [["1 cup", None]], per_unit="serving"), [["1 cup", None]])
        self.assertEqual(self.apply("Garlic fried rice", "Rice", []), [])
        self.assertEqual(self.apply("Garlic fried rice", "Rice", [["1 cup", None]]), [["1 cup", None]])

    # ---- exceptions a to d keep the food's own default portion first
    def test_a_eating_out_keeps_the_item(self):
        self.assertKeeps("Big Mac", "McDonald's", [["1 burger", 215]], "dining_out")
        self.assertKeeps("Steamed rice", "Chicken restaurant", [["1 cup", 158]], "dining_out")

    def test_b_drinks_keep_the_cup_or_can(self):
        self.assertKeeps("Milk, whole", "Milk, whole", [["1 cup", 244]])
        self.assertKeeps("Coffee, brewed", "Coffee", [["1 cup (8 fl oz)", 240]])
        self.assertKeeps("Cola", "Soft drinks", [["1 can (12 fl oz)", 368]], "supermarket")
        self.assertKeeps("Orange juice, raw", "Fruits and Fruit Juices", [["1 cup", 248]])
        self.assertKeeps("Beer", "Beverages", [["1 can (12 fl oz)", 356]])
        self.assertKeeps("Soymilk", "Beverages", [["1 cup", 243]])
        self.assertKeeps("Banana smoothie", "Smoothies and grain drinks", [["1 bottle", 450]])
        self.assertKeeps("Whole milk", "Drinks", [["1 cup", 244]])

    def test_b_a_drink_without_any_portion_has_nothing_to_keep(self):
        self.assertEqual(self.apply("Tea, brewed", "Tea", []), [])

    def test_b_dry_milk_powder_is_not_a_drink(self):
        self.assertHundredFirst("Milk, dry, whole", "Dairy and Egg Products", [["1 cup", 128]])

    def test_c_foods_counted_in_units_keep_the_unit(self):
        self.assertKeeps("Egg, whole, raw", "Eggs and omelets", [["1 egg", 50], ["1 cup", 245]])
        self.assertKeeps("Fried egg", "Dairy & eggs", [["1 fried egg", 55]])
        self.assertKeeps("Banana", "Fruit", [["1 banana", 126], ["1 cup, sliced", 150]])
        self.assertKeeps("Apples, raw, with skin", "Fruits and Fruit Juices", [["1 medium (3\" dia)", 182]])
        self.assertKeeps("White bread", "Yeast breads", [["1 medium or regular slice", 28]])
        self.assertKeeps("Pandesal", "Bread & bakery", [["1 roll", 45]])
        self.assertKeeps("Cookies, chocolate chip", "Baked Products", [["1 cookie", 16]], "supermarket")
        self.assertKeeps("Pizza, cheese", "Pizza", [["1 slice", 107]])
        self.assertKeeps("Cheeseburger", "Burgers", [["1 sandwich", 120]])
        self.assertKeeps("Pork dumplings", "Egg rolls, dumplings, sushi", [["1 piece", 25]])
        self.assertKeeps("Nutrition bar", "Nutrition bars", [["1 bar", 50]])
        self.assertKeeps("Ice cream cone", "Ice cream and frozen dairy desserts", [["1 cone", 70]])

    def test_c_the_same_foods_by_the_cup_or_weight_are_weighed(self):
        self.assertHundredFirst("Strawberries, raw", "Fruits and Fruit Juices", [["1 cup", 152]])
        self.assertHundredFirst("Ice cream, vanilla", "Ice cream and frozen dairy desserts", [["1 cup", 132]])
        self.assertHundredFirst("Bread crumbs", "Baked Products", [["1 cup", 108]])
        self.assertHundredFirst("Apricots, dried", "Fruits and Fruit Juices", [["1 piece", 8]])

    def test_d_foods_used_by_the_spoon_keep_the_spoon(self):
        self.assertKeeps("Olive oil", "Fats and Oils", [["1 tablespoon", 13.5]])
        self.assertKeeps("Butter", "Dairy & eggs", [["1 pat", 5], ["1 tablespoon", 14]])
        self.assertKeeps("Peanut butter", "Nuts and seeds", [["1 tablespoon", 16]])
        self.assertKeeps("Jam or jelly", "Jams, syrups, toppings", [["1 tablespoon", 20]])
        self.assertKeeps("Sugar, white", "Sugars and honey", [["1 teaspoon", 4.2]])
        self.assertKeeps("Ketchup", "Tomato-based condiments", [["1 packet", 9]], "supermarket")
        self.assertKeeps("Salt", "Spices and Herbs", [["1 tsp", 6]])

    def test_d_the_same_foods_listed_by_the_cup_are_weighed(self):
        self.assertHundredFirst("Pasta sauce", "Soups, Sauces, and Gravies", [["1/2 cup", 125]])

    def test_a_packet_of_instant_oatmeal_is_not_a_spoonful_of_sugar(self):
        self.assertHundredFirst("Cereals, instant oatmeal, brown sugar, dry", "Breakfast Cereals", [["1 packet", 43]])
        self.assertHundredFirst("Cereals, Quaker, Instant Oatmeal, fruit and cream variety, dry", "Breakfast Cereals", [["1 packet", 35]])

    # ---- second-pass review: patterns seen in the real USDA rows
    def test_with_salt_or_reduced_fat_in_the_name_does_not_make_a_spoon_food(self):
        self.assertHundredFirst("Carrots, cooked, boiled, drained, with salt", "Vegetables and Vegetable Products", [["1 tbsp", 9.7], ["1/2 cup slices", 78]])
        self.assertHundredFirst("Cheese, Parmesan, dry grated, reduced fat", "Cheese", [["1 tablespoon", 7]])
        self.assertKeeps("White sugar", "Pantry", [["1 teaspoon", 4.2]])
        self.assertKeeps("Nuts, cashew butter, plain, with salt added", "Nut and Seed Products", [["1 tbsp", 16]])

    def test_a_small_bare_serving_of_a_condiment_counts_as_a_spoonful(self):
        self.assertKeeps("Salsa, red", "Tomato-based condiments", [["1 serving", 32], ["1 tablespoon", 16]])
        self.assertKeeps("Sugar", "Sugars and honey", [["1 serving", 8], ["1 cup", 200]])
        self.assertHundredFirst("Cherry pie filling", "Jams, syrups, toppings", [["1 serving", 130], ["1 cup", 260]])
        self.assertHundredFirst("Cookies, Archway, Molasses", "Baked Products", [["1 serving", 26]])

    def test_a_bare_serving_is_not_a_unit(self):
        self.assertHundredFirst("Croissant", "Doughnuts, sweet rolls, pastries", [["1 serving", 70], ["1 mini croissant", 28]])
        self.assertKeeps("Frankfurter, meat", "Sausages and Luncheon Meats", [["1 serving (1 hot dog)", 52]])

    def test_units_found_by_name_outside_their_usual_group(self):
        self.assertKeeps("Snacks, granola bars, hard, plain", "Snacks", [["1 bar", 21]])
        self.assertKeeps("Enchilada, beef", "Other Mexican mixed dishes", [["1 enchilada, any size", 150], ["1 cup", 170]])
        self.assertKeeps("Pizza, meat topping, thick crust, frozen, cooked", "Fast Foods", [["1 slice 1/8 of 12\" pizza", 103]])
        self.assertKeeps("Ice cream bar, chocolate covered", "Dairy and Egg Products", [["1 bar", 50]])
        self.assertKeeps("Popsicle", "Gelatins, ices, sorbets", [["1 single stick", 50]])
        self.assertKeeps("Cake, angelfood, dry mix, prepared", "Baked Products", [["1 piece (1/12 of 10\" dia)", 50]])
        self.assertHundredFirst("Beef, sandwich steaks, flaked, chopped, formed", "Beef Products", [["1 steak", 56]])
        self.assertHundredFirst("Corn chips, plain", "Tortilla, corn, other chips", [["1 small bag", 28]])

    def test_plant_milks_nectars_and_made_up_powders_are_drinks(self):
        self.assertKeeps("Silk Light Vanilla, soymilk", "Legumes and Legume Products", [["1 cup", 243]])
        self.assertKeeps("Mango nectar, canned", "Fruits and Fruit Juices", [["1 cup", 251]])
        self.assertKeeps("Milk, dry, reconstituted, nonfat", "Milk, nonfat", [["1 cup", 244]])
        self.assertHundredFirst("Milk, dry, not reconstituted", "Ingredient", [["1 serving", 43], ["1 cup", 128]])

    def test_protein_powder_and_bouillon_keep_the_scoop_or_cube(self):
        self.assertKeeps("Nutritional powder mix, whey based", "Protein and nutritional powders", [["1 scoop", 26]])
        self.assertKeeps("Soup, chicken broth cubes, dry", "Soups, Sauces, and Gravies", [["1 cube", 4.8]])
        self.assertHundredFirst("Soup, ramen noodle, any flavor, dry", "Soups, Sauces, and Gravies", [["1 package without flavor packet", 81]])

    # ---- Open Food Facts rows: no group label, category supermarket
    def off(self, name, portions):
        return self.apply(name, None, portions, "supermarket", source="off")

    def test_off_a_packaged_food_gets_100_g_first(self):
        self.assertEqual(self.off("Snickers", [["48 g", 48]]), [["100 g", 100], ["48 g", 48]])
        self.assertEqual(self.off("Weetabix Original", [["2 biscuits (37.5 g)", 37.5]]),
                         [["100 g", 100], ["2 biscuits (37.5 g)", 37.5]])

    def test_off_a_portion_in_ml_cl_l_or_fl_oz_marks_a_drink_that_keeps_its_serving(self):
        for label, amount in (("240 ml", 240), ("1 can (33 cl)", 330), ("1 bottle (1 l)", 1000),
                              ("12 fl oz", 355)):
            with self.subTest(label=label):
                self.assertEqual(self.off("Mystery Soda Lime", [[label, amount]]), [[label, amount]])

    def test_off_a_name_that_says_drink_keeps_its_serving(self):
        self.assertEqual(self.off("Orange Juice", [["1 cup (240 g)", 240]]), [["1 cup (240 g)", 240]])

    def test_off_a_unit_food_keeps_its_unit(self):
        self.assertEqual(self.off("Granola Bar Oats", [["1 bar (40 g)", 40]]), [["1 bar (40 g)", 40]])

    def test_off_a_spoon_food_keeps_its_spoon(self):
        self.assertEqual(self.off("Peanut Butter Creamy", [["2 tbsp (32 g)", 32]]), [["2 tbsp (32 g)", 32]])

    def test_off_a_row_without_portions_has_none(self):
        self.assertEqual(self.off("Snickers", []), [])

    # ---- e: "default_portion" in the curated list, end to end
    def curated_bundle(self, entries):
        d = new_dir("default_portion")
        path = os.path.join(d, "curated.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": entries}, f)
        db, bundle = os.path.join(d, "foods.sqlite"), os.path.join(d, "food-db.json")
        imp = import_ok(["--db", db, "--curated-json", path, "--export-app", bundle, "--quiet"])
        doc = read_json(bundle)
        rows = {r[0]: dict(zip(doc["fields"], r)) for r in doc["items"]}
        return imp, db, rows

    @staticmethod
    def entry(name, mode=None, category="whole_food", group="Test", serving=("1 cup", 150)):
        e = {"name": name, "brand": None, "group": group, "category": category, "region_tags": ["PH"],
             "barcode": None, "thumbnail_url": None, "aka": [],
             "serving": {"label": serving[0], "grams": serving[1]},
             "extra_portions": [{"label": "1 bowl", "grams": 300}], "nutrition_per": "100g",
             "calories_kcal": 100, "protein_g": 5, "carbs_g": 10, "sugars_g": None, "fat_g": 2,
             "saturates_g": None, "fibre_g": None, "sodium_mg": None, "basis": "estimate", "src": None, "notes": ""}
        if mode:
            e["default_portion"] = mode
        return e

    def test_e_serving_keeps_the_stated_serving_first_where_the_rule_would_not(self):
        _imp, db, rows = self.curated_bundle([self.entry("Test stew", "serving"), self.entry("Test stew two")])
        self.assertEqual(rows["Test stew"]["portions"], [["1 cup", 150], ["1 bowl", 300]])     # no marker in the bundle
        self.assertEqual(rows["Test stew two"]["portions"], [["100 g", 100], ["1 cup", 150], ["1 bowl", 300]])

    def test_e_100g_forces_100_g_first_even_for_a_drink(self):
        _imp, db, rows = self.curated_bundle([self.entry("Test tea", "100g", group="Tea"),
                                              self.entry("Test coffee", group="Coffee")])
        self.assertEqual(rows["Test tea"]["portions"], [["100 g", 100], ["1 cup", 150], ["1 bowl", 300]])
        self.assertEqual(rows["Test coffee"]["portions"], [["1 cup", 150], ["1 bowl", 300]])
        conn = open_db(db)
        try:   # the database itself holds the forced order, no schema change
            stored = conn.execute("SELECT portions FROM food_items WHERE name = 'Test tea'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(json.loads(stored), [["100 g", 100], ["1 cup", 150], ["1 bowl", 300]])

    def test_e_an_unknown_value_skips_the_entry_with_a_warning(self):
        imp, _db, rows = self.curated_bundle([self.entry("Test odd", "both"), self.entry("Test fine")])
        self.assertNotIn("Test odd", rows)
        self.assertIn("Test fine", rows)
        self.assertEqual(imp["summary"]["invalid"], 1)
        self.assertIn("default_portion", imp["stderr"])


class BundleSelectionTests(BundleCase):
    """Which Open Food Facts rows the main import puts in the app bundle, and in what order (SEL-1 to SEL-6)."""

    @classmethod
    def setUpClass(cls):
        cls.doc = read_json(full_import()["bundle"])
        cls.off = [(r["name"], r["barcode"], r["brand"]) for r in cls.recs_of(cls.doc) if r["source"] == 3]

    @staticmethod
    def recs_of(doc):
        return [dict(zip(doc["fields"], row)) for row in doc["items"]]

    def names(self):
        return [n for n, _b, _br in self.off]

    def barcodes(self):
        return [b for _n, b, _br in self.off]

    def test_the_philippine_slice_includes_a_480_row_tagged_only_united_states(self):
        self.assertIn("Pinoy Spread Peanut", self.names())
        self.assertEqual(self.rec(name="Pinoy Spread Peanut")["regions"], "US")

    def test_the_philippine_slice_is_written_first_by_scans(self):
        self.assertEqual([n for n, _b, _br in self.off][:6],
                         ["Chippy BBQ", "Kikkoman Soy Sauce", "Milo Activ-Go Powder", "Bear Brand Powdered Milk",
                          "チョコ", "Pinoy Spread Peanut"])

    def test_a_ph_tagged_row_with_an_untypeable_name_and_foreign_tags_is_kept(self):
        row = self.rec(name="チョコ")
        self.assertEqual((row["regions"], row["barcode"]), ("PH", "3012345000012"))
        self.assertEqual((row["protein"], row["carbs"], row["fat"]), (None, None, None))

    def test_a_foreign_tagged_row_with_a_barcode_of_no_wanted_market_is_left_out(self):
        self.assertNotIn("Fromage Blanc Nature", self.names())

    def test_a_foreign_tagged_row_with_a_wanted_barcode_prefix_is_kept(self):
        self.assertIn("McVitie's Digestive Biscuits", self.names())      # Ireland + France tags, prefix 500
        self.assertIn("Indomie Mi Goreng", self.names())                 # Indonesia tag, prefix 899

    def test_neutral_tags_do_not_count_as_foreign(self):
        # tagged United States + world + european-union, German barcode: kept only because no tag is foreign
        self.assertIn("Gouda Cheese Slices", self.names())

    def test_an_untypeable_name_is_left_out_of_the_pools(self):
        self.assertNotIn("ポッキー チョコレート", self.names())

    def test_a_japan_only_tag_with_a_russian_barcode_belongs_to_no_pool(self):
        self.assertNotIn("Halva Peanut Bar", self.names())

    def test_a_row_without_protein_carbs_and_fat_is_left_out_of_the_pools(self):
        self.assertNotIn("Calpis Water", self.names())

    def test_a_zero_scan_row_needs_a_latin_letter_brand_and_a_photo(self):
        self.assertIn("Black Thunder Chocolate Bar", self.names())       # brand Yuraku + photo
        self.assertNotIn("Meltykiss Premium Chocolate", self.names())    # brand, no photo
        self.assertNotIn("Plain Wafer Rolls", self.names())              # photo, no brand
        self.assertNotIn("Hokkaido Milk Caramel", self.names())          # photo, brand in kanji only

    def test_every_listed_left_out_row_is_absent(self):
        for name, why in OFF_LEFT_OUT.items():
            with self.subTest(name=name, why=why):
                self.assertNotIn(name, self.names())

    def test_a_brand_on_two_ph_rows_counts_triple_and_a_brand_on_one_does_not(self):
        order = self.names()
        # Nestle (accent folded) is on Milo and Bear Brand: 10 scans x 3 = 30 beats Maltesers at 25
        self.assertLess(order.index("Nesquik Chocolate Powder"), order.index("Maltesers"))
        # Kikkoman is on one PH row only: 20 scans stay 20 and rank below Maltesers at 25
        self.assertLess(order.index("Maltesers"), order.index("Kikkoman Teriyaki Sauce"))

    def test_equal_rank_is_broken_by_completeness(self):
        order = self.names()
        self.assertLess(order.index("Plain Crackers Salted"), order.index("Gouda Cheese Slices"))   # 90 % before 50 %

    def test_popularity_is_not_divided_by_foreign_tags(self):
        order = self.names()
        # 386 scans with two foreign tags would drop to 128 if divided, below the 150 of Pot Noodle
        self.assertLess(order.index("McVitie's Digestive Biscuits"), order.index("Pot Noodle Original Curry"))

    def test_a_row_repeating_a_taken_plain_name_and_rounded_kcal_is_skipped(self):
        maltesers = [(b, br) for n, b, br in self.off if n == "Maltesers"]
        self.assertEqual(maltesers, [("0040000579816", self.brand_index("Mars")),
                                     ("0040000579847", self.brand_index("Mars Food"))])
        self.assertNotIn("0040000579830", self.barcodes())               # Mars Wrigley, 500.4 kcal

    def brand_index(self, name):
        return self.doc["brands"].index(name)

    def test_the_pools_come_out_in_the_expected_order_and_size(self):
        position = 0
        for pool, rows in OFF_POOLS:
            with self.subTest(pool=pool):
                self.assertEqual([(n, b) for n, b, _br in self.off[position:position + len(rows)]], rows)
            position += len(rows)
        self.assertEqual(position, len(self.off))

    def test_the_kr_pool_is_filled_before_us_so_shin_ramyun_is_a_kr_row(self):
        self.assertIn(("Shin Ramyun", "8801043014830"), OFF_KR)
        self.assertNotIn(("Shin Ramyun", "8801043014830"), OFF_US)
        self.assertEqual(self.rec(name="Shin Ramyun")["regions"], "US,KR")

    def test_the_jp_pool_is_filled_before_us_so_kewpie_is_a_jp_row(self):
        self.assertEqual(self.names().index("Kewpie Mayonnaise"),
                         len(OFF_PH + OFF_US + OFF_UK) + 1)


class QuotaCase(BundleCase):
    quota = None

    @classmethod
    def setUpClass(cls):
        cls.imp = quota_import(cls.quota)
        cls.doc = cls.imp["doc"]

    def off_names(self):
        return [(r["name"], r["barcode"]) for r in self.recs() if r["source"] == 3]


class OffQuotaTruncationTests(QuotaCase):
    """--off-quota US=1,UK=1,JP=3,KR=2,AS=1 (and no --built): each pool keeps its best rows."""
    quota = "US=1,UK=1,JP=3,KR=2,AS=1"

    def test_each_pool_keeps_only_its_quota(self):
        expected = OFF_PH + OFF_US[:1] + OFF_UK[:1] + OFF_JP[:3] + OFF_KR[:2] + OFF_AS[:1]
        self.assertEqual(self.off_names(), expected)

    def test_the_philippine_slice_is_not_limited_by_any_quota(self):
        self.assertEqual(self.off_names()[:len(OFF_PH)], OFF_PH)

    def test_curated_and_usda_rows_are_not_limited(self):
        sources = [r["source"] for r in self.recs()]
        self.assertEqual((sources.count(0), sources.count(1), sources.count(2)), (8, 3, 8))

    def test_the_database_still_holds_every_off_row(self):
        self.assertEqual(self.imp["summary"]["kept"]["off"], 37)
        self.assertEqual(self.imp["summary"]["total_rows"], 56)

    def test_built_defaults_to_an_empty_string(self):
        self.assertEqual(self.doc["built"], "")


class OffQuotaZeroPoolTests(QuotaCase):
    """A pool may be 0: UK=0 leaves the UK-only rows out; US=2 keeps the two best US rows."""
    quota = "US=2,UK=0,JP=3500,KR=2500,AS=1500"

    def test_a_pool_set_to_zero_exports_nothing(self):
        uk_only = {n for n, _b in OFF_UK}
        self.assertEqual({n for n, _b in self.off_names()} & (uk_only - {"Kikkoman Soy Sauce"}), set())

    def test_the_other_pools_are_filled_as_usual(self):
        expected = OFF_PH + OFF_US[:2] + OFF_JP + OFF_KR + OFF_AS
        self.assertEqual(self.off_names(), expected)


class OffQuotaAllZeroTests(QuotaCase):
    quota = "US=0,UK=0,JP=0,KR=0,AS=0"

    def test_only_the_philippine_slice_is_exported(self):
        self.assertEqual(self.off_names(), OFF_PH)
        self.assertEqual(self.doc["counts"]["off"], len(OFF_PH))




# --------------------------------------------------------------------------------------
# importer: command line behaviour
# --------------------------------------------------------------------------------------

class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.dir = new_dir("cli")
        self.db = os.path.join(self.dir, "foods.sqlite")

    def test_runs_in_isolated_mode(self):
        imp = import_ok(["--db", self.db, "--curated-json", CURATED, "--quiet"], isolated=True)
        self.assertEqual(imp["summary"]["kept"]["curated"], 8)

    def test_creates_the_database_at_version_3(self):
        self.assertFalse(os.path.exists(self.db))
        import_ok(["--db", self.db, "--curated-json", CURATED, "--quiet"])
        conn = open_db(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM food_items").fetchone()[0], 8)
            self.assertEqual(sorted(set(V3_NEW_COLUMNS) - set(column_names(conn))), [])
        finally:
            conn.close()

    def test_upgrades_an_existing_version_2_database_and_keeps_its_rows(self):
        conn = sqlite3.connect(self.db)
        for statement in V2_SCHEMA:
            conn.execute(statement)
        marks = ", ".join("?" for _ in V2_ROWS[0])
        conn.executemany("INSERT INTO food_items (%s) VALUES (%s)" % (V2_ROW_COLUMNS, marks), V2_ROWS)
        conn.commit()
        conn.close()
        summary = import_ok(["--db", self.db, "--curated-json", CURATED, "--quiet"])["summary"]
        conn = open_db(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            old = {r["id"]: r for r in conn.execute("SELECT * FROM food_items WHERE id IN (3, 4, 9)")}
        finally:
            conn.close()
        self.assertEqual(sorted(old), [3, 4, 9], "a version 2 row was lost")
        self.assertEqual(old[9]["name"], "Weetabix Original")
        self.assertEqual((old[9]["barcode"], old[9]["popularity"]), ("5010029000023", 420))
        self.assertEqual(summary["kept"]["usda"], 1)
        self.assertEqual(summary["kept"]["off"], 1)

    def test_curated_only_summary(self):
        summary = import_ok(["--db", self.db, "--curated-json", CURATED, "--quiet"])["summary"]
        self.assertEqual(summary["read"], {"curated": 11, "usda": 0, "off": 0})
        self.assertEqual((summary["invalid"], summary["duplicates"], summary["total_rows"]), (3, 0, 8))

    def test_upgrades_an_existing_version_1_database_and_keeps_its_rows(self):
        if food_schema is None:
            self.fail("scripts/food_schema.py could not be imported: %r" % (_SCHEMA_IMPORT_ERROR,))
        conn = food_schema.connect(self.db)
        food_schema.create_v1(conn)
        conn.execute("INSERT INTO food_items (id, name, group_label, kcal, protein, carbs, fat, portions, source, source_ref)"
                     " VALUES (5, 'Old survey plantain', 'Plantains', 122, 1.3, 31.9, 0.4, '[]', 'usda', 'survey:1')")
        conn.commit()
        conn.close()
        summary = import_ok(["--db", self.db, "--curated-json", CURATED, "--quiet"])["summary"]
        conn = open_db(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            old = conn.execute("SELECT name, calories_kcal, source_ref FROM food_items WHERE id = 5").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(old, "the version 1 row was lost")
        self.assertEqual((old["name"], old["source_ref"]), ("Old survey plantain", "survey:1"))
        self.assertAlmostEqual(old["calories_kcal"], 122.0, places=1)
        self.assertEqual(summary["kept"], {"curated": 8, "usda": 1, "off": 0})
        self.assertEqual(summary["total_rows"], 9)

    def test_missing_input_file_exits_non_zero_with_a_message(self):
        missing = os.path.join(self.dir, "does_not_exist")
        for flag in ("--off-path", "--usda-path", "--curated-json"):
            with self.subTest(flag=flag):
                code, _out, err = run_importer(["--db", self.db, flag, missing, "--quiet"])
                self.assertNotEqual(code, 0)
                self.assertTrue(err.strip(), "no message on stderr")

    def test_missing_db_argument_exits_non_zero_with_a_message(self):
        code, _out, err = run_importer(["--curated-json", CURATED])
        self.assertNotEqual(code, 0)
        self.assertTrue(err.strip(), "no message on stderr")

    def test_non_numeric_off_quota_exits_non_zero_with_a_message(self):
        code, _out, err = run_importer(["--db", self.db, "--curated-json", CURATED,
                                        "--off-quota", "US=many,UK=1,JP=1,KR=1,AS=1"])
        self.assertNotEqual(code, 0)
        self.assertTrue(err.strip(), "no message on stderr")

    def test_the_documented_default_quotas_give_the_same_bundle_as_no_option(self):
        out = os.path.join(self.dir, "food-db.json")
        import_ok(full_args(self.db, out, ["--built", BUILT, "--quiet",
                                           "--off-quota", "US=12000,UK=2500,JP=3500,KR=2500,AS=1500"]))
        self.assertEqual(read_json(out), read_json(full_import()["bundle"]))


class CompletenessGuardTests(unittest.TestCase):
    """export_app refuses a database whose Open Food Facts rows all have completeness NULL (old rules)."""

    def setUp(self):
        self.dir = new_dir("guard_cli")
        self.out = os.path.join(self.dir, "food-db.json")

    def assertRefused(self, code, err):
        self.assertNotEqual(code, 0)
        self.assertTrue(err.strip(), "no message on stderr")
        self.assertTrue(any(word in err.lower() for word in ("completeness", "re-import", "reimport", "old rules")),
                        "the message does not say what is wrong: %r" % err[-300:])
        self.assertFalse(os.path.exists(self.out), "a food list was written anyway")

    def test_a_database_made_by_the_old_rules_is_refused(self):
        db = os.path.join(self.dir, "old.sqlite")
        shutil.copyfile(old_rules_database()["db"], db)
        code, _out, err = run_importer(["--db", db, "--export-app", self.out, "--quiet"])
        self.assertRefused(code, err)

    def test_a_file_without_a_completeness_column_gives_such_a_database_and_is_refused(self):
        db = os.path.join(self.dir, "comma.sqlite")
        path = os.path.join(self.dir, "off_commas.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "product_name", "countries_tags", "energy-kcal_100g"])
            w.writerow(["5012345678900", "Oat Biscuits plain", "en:united-kingdom", "450"])
        code, _out, err = run_importer(["--db", db, "--off-path", path, "--export-app", self.out, "--quiet"])
        self.assertRefused(code, err)

    def test_one_row_with_a_completeness_is_enough_to_export(self):
        db = os.path.join(self.dir, "partly.sqlite")
        shutil.copyfile(old_rules_database()["db"], db)
        conn = open_db(db)
        try:
            conn.execute("UPDATE food_items SET completeness = 79 WHERE source = 'off' AND source_ref = '5000159407236'")
            conn.commit()
        finally:
            conn.close()
        import_ok(["--db", db, "--export-app", self.out, "--quiet"])
        self.assertTrue(os.path.isfile(self.out))

    def test_a_database_without_any_off_row_still_exports(self):
        db = os.path.join(self.dir, "curated.sqlite")
        import_ok(["--db", db, "--curated-json", CURATED, "--export-app", self.out, "--quiet"])
        self.assertEqual(read_json(self.out)["counts"], {"curated": 8, "usda": 0, "off": 0})




if __name__ == "__main__":
    unittest.main()
