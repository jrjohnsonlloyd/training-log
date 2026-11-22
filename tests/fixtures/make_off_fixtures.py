"""Writes the Open Food Facts test fixtures (tab-separated, like the real export, with a gzip-free layout).

    python tests/fixtures/make_off_fixtures.py [OUTPUT_FOLDER]

Ten files are written next to this script:

    sample_off.csv          the main import: 52 rows (32 old anchor rows + 20 selection rows)
    off_rules.csv           36 rows, one per validation / repair rule branch
    off_dedupe.csv          15 rows in groups that must (or must not) merge by name + brand
    off_alias_guard.csv     1 row whose barcode is also an alias stored on another row
    off_serving.csv         18 rows for labels typed per serving (with a "quantity" column)
    off_doubt.csv           23 rows from other markets with doubtful numbers, and the US pool's rules
    off_asia.csv            15 rows for the seven neighbouring markets (region "AS") and pool membership
    off_asia2.csv           30 rows for the rules of the Japan, Korea and Asia pools (step 5)
    off_step6.csv           19 rows for the UK shelf and the step 6 repairs (with a "quantity" column)
    off_step7.csv           23 Philippine rows for the step 7 pack and serving re-base, powders and hand entries

Every row is written out in full below so a reader can follow the expected values in
tests/test_import_foods.py without running anything. Short column keys:
    name name_en generic brand cat ctry img img_s ss sq nutri comp scans kcal kj energy
    P C Ct S F sat fib na salt alc
(P protein, C carbohydrates_100g, Ct carbohydrates-total_100g, S sugars, F fat, sat saturated fat,
fib fibre, na sodium in grams, salt in grams, alc alcohol_100g, ss serving_size, sq serving_quantity,
comp completeness, scans unique_scans_n, img image_front_small_url, img_s image_small_url.)
"""
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else HERE      # an optional output folder (used by the test that checks the files)
THUMB = "https://images.openfoodfacts.org/images/products/"

HEADER = ["code", "url", "product_name", "product_name_en", "generic_name", "brands", "categories_en",
          "countries_tags", "image_front_small_url", "serving_size", "serving_quantity", "nutriscore_grade",
          "completeness", "unique_scans_n", "energy-kcal_100g", "energy-kj_100g", "energy_100g",
          "proteins_100g", "carbohydrates_100g", "carbohydrates-total_100g", "sugars_100g", "fat_100g",
          "saturated-fat_100g", "fiber_100g", "sodium_100g", "salt_100g", "alcohol_100g", "image_small_url"]
KEYS = {"name": "product_name", "name_en": "product_name_en", "generic": "generic_name", "brand": "brands",
        "cat": "categories_en", "ctry": "countries_tags", "img": "image_front_small_url",
        "img_s": "image_small_url", "ss": "serving_size", "sq": "serving_quantity", "nutri": "nutriscore_grade",
        "comp": "completeness", "scans": "unique_scans_n", "kcal": "energy-kcal_100g", "kj": "energy-kj_100g",
        "energy": "energy_100g", "P": "proteins_100g", "C": "carbohydrates_100g",
        "Ct": "carbohydrates-total_100g", "S": "sugars_100g", "F": "fat_100g", "sat": "saturated-fat_100g",
        "fib": "fiber_100g", "na": "sodium_100g", "salt": "salt_100g", "alc": "alcohol_100g"}


def ean(prefix, n):
    """A 13-digit code: 3 prefix digits + 9-digit serial + a last digit (no check digit is computed)."""
    return "%s%09d0" % (prefix, n)


def photo(code, version=3):
    return "%s%s/%s/%s/%s/front_en.%d.200.jpg" % (THUMB, code[0:3], code[3:6], code[6:9], code[9:], version)


def row(code, **kw):
    cells = dict.fromkeys(HEADER, "")
    cells["code"] = code
    cells["url"] = "http://world-en.openfoodfacts.org/product/" + code
    for key, value in kw.items():
        cells[KEYS[key]] = "" if value is None else str(value)
    return [cells[column] for column in HEADER]


def write(name, rows, header=HEADER):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", quoting=csv.QUOTE_NONE, quotechar=None, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    print("wrote %-22s %d rows" % (name, len(rows)))


# ======================================================================================
# sample_off.csv : the main import
# ======================================================================================
main = []
# --- the 32 anchor rows (A1 = Pancit Canton, A2 = Snickers, A3 = Cheerios, A4 = Shin Ramyun, A5 = Pocky) ---
main += [
    row("4807770270123", name="Pancit Canton Kalamansi", brand="Lucky Me!,Monde Nissin", ctry="en:philippines",
        img=THUMB + "480/777/027/0123/front_en.3.200.jpg", ss="60 g", sq=60, nutri="d", comp=0.8, scans=120,
        kcal=467, P=11.7, C=56.7, S=5, F=21.7, sat=10, fib=3.3, na=1.5, salt=3.75),
    row("5000159407236", name="Snickers", brand="Mars", ctry="en:united-kingdom",
        img=THUMB + "500/015/940/7236/front_en.4.200.jpg", ss="48 g", sq=48, nutri="e", comp=0.7875, scans=950,
        energy=2029, P=8.6, C=60.5, S=51.8, F=22.5, sat=7.9, salt=0.63),
    row("016000275287", name="Cheerios", brand="General Mills", ctry="en:united-states",
        img=THUMB + "001/600/027/5287/front_en.5.200.jpg", ss="39 g", sq=39, nutri="b", comp=0.85, scans=700,
        kcal=367, P=12.1, C=73.2, S=4.5, F=6.6, sat=1.2, fib=10.1, na=0.497),
    row("8801043014830", name="Shin Ramyun", brand="Nongshim", ctry="en:south-korea,en:united-states",
        img=THUMB + "880/104/301/4830/front_en.7.200.jpg", ss="120 g", sq=120, nutri="d", comp=0.9, scans=640,
        kcal=433, P=9.2, C=65, S=3.3, F=15, sat=7.5, fib=2.5, na=1.79),
    row("4901330502881", name="Pocky Chocolate", brand="Glico", ctry="en:japan",
        img=THUMB + "490/133/050/2881/front_ja.3.200.jpg", ss="1 pack (36 g)", sq=36, nutri="e", comp=0.7874999,
        scans=310, kcal=503, P=8.3, C=66.7, S=36.1, F=22.2, sat=11.1, fib=2.8, na=0.2, salt=0.5),
    row("3017620422003", name="Nutella", brand="Ferrero", ctry="en:france",
        img=THUMB + "301/762/042/2003/front_fr.9.200.jpg", ss="15 g", sq=15, nutri="e", comp=0.9, scans=5000,
        kcal=539, P=6.3, C=57.5, S=56.3, F=30.9, sat=10.6, na=0.0428, salt=0.107),
    row("5010029000023", name="Weetabix Original", brand="Weetabix", ctry="en:united-kingdom",
        img=THUMB + "501/002/900/0023/front_en.2.200.jpg", ss="2 biscuits (37.5 g)", sq=37.5, nutri="a",
        comp=0.8, scans=420, kj=1531, P=12, C=69, S=4.2, F=2, sat=0.6, fib=10, salt=0.28),
    row("4000417025005", name="Ritter Sport Alpine Milk", brand="Ritter Sport", ctry="en:germany,en:austria",
        img=THUMB + "400/041/702/5005/front_de.6.200.jpg", ss="100 g", sq=100, nutri="e", comp=0.8, scans=800,
        kcal=547, P=8, C=53, S=52, F=33, sat=20, fib=2, salt=0.25),
    row("4901515110504", name="Kikkoman Soy Sauce", brand="Kikkoman",
        ctry="en:japan,en:france,en:philippines,en:united-kingdom",
        img=THUMB + "490/151/511/0504/front_en.3.200.jpg", ss="1 tbsp (15 ml)", sq=15, comp=0.6, scans=260,
        kcal=77, P=10, C=8, S=3, F=0, sat=0, na=5.72, salt=14.3),
    row("4901340036512", name="Calpis Water", brand="Asahi", ctry="en:japan", ss="500 ml", sq=500, kcal=46),
    row("4514603365511", name="Oi Ocha Green Tea", brand="Ito En", ctry="en:japan",
        img=THUMB + "451/460/336/5511/front_ja.4.200.jpg", nutri="a", comp=1, scans=90,
        kcal=0, P=0, C=0, S=0, F=0, sat=0, fib=0, na=0.01),
    row("041220576555", name="Mega Sugar Syrup", brand="Testco", ctry="en:united-states", scans=12,
        kcal=300, P=0, C=120, F=0),                                  # carbs above 100 g: dropped by the range check
    row("041220576562", name="Impossible Protein Bar", brand="Testco", ctry="en:united-states", scans=8,
        kcal=500, P=40, C=40, S=10, F=30),
    row("041220576579", name="Negative Fibre Yogurt", brand="Testco", ctry="en:united-states", scans=5,
        kcal=60, P=4, C=7, S=7, F=1.5, fib=-1),
    row("5000111000017", name="Rocket Fuel Oil", brand="Testco", ctry="en:united-kingdom", scans=2,
        kcal=1200, P=0, C=0, F=100),
    row("4800011122334", brand="Mystery Brand", ctry="en:philippines", scans=4, kcal=100, P=2, C=20, F=1),
    row("4800011122341", name="No Energy Crackers", brand="Mystery Brand", ctry="en:philippines", scans=6,
        P=8, C=70, F=12),
    row("2000000012345", name="Deli Roast Chicken Slices", brand="Tesco", ctry="en:united-kingdom",
        img=THUMB + "200/000/001/2345/front_en.3.200.jpg", ss="100 g", sq=100, comp=0.4, scans=15, kcal=120,
        P=24, C=1, S=0.5, F=2.2, sat=0.7, fib=0, na=0.6),
    row("12345", name="Corner Shop Flapjack", brand="Corner Bakery", ctry="en:united-kingdom", comp=0.6,
        scans=3, kcal=450, P=6, C=58, S=30, F=21, sat=9, fib=4, salt=0.3),
    row("8801117760007", name="Choco Pie Original", brand="Orion", ctry="en:south-korea", ss="1 pie (39 g)",
        sq=39, comp=0.7, scans=10, kcal=432, P=4.1, C=66, S=37, F=17, sat=9, fib=1.2, na=0.2),
    row("8801117760007", name="Choco Pie", brand="Orion", ctry="en:south-korea",
        img=THUMB + "880/111/776/0007/front_ko.3.200.jpg", comp=0.3, scans=500, kcal=430, P=4),
    row("4902777000015", name="Meiji Milk Choco", brand="Meiji", ctry="en:japan", comp=0.2, scans=400,
        kcal=560, P=7),
    row("4902777000015", name="Meiji Milk Chocolate", brand="Meiji", ctry="en:japan",
        img=THUMB + "490/277/700/0015/front_ja.5.200.jpg", ss="50 g", sq=50, comp=0.75, scans=20, kcal=558,
        P=7.4, C=53, S=50, F=35, sat=21, fib=3, na=0.064),
    row("4800016644511", name="Jack 'n Jill Chippy BBQ", brand="Jack n Jill", ctry="en:philippines,en:united-states",
        comp=0.3, scans=300, kcal=505, P=6, F=27),
    row("4800016644504", name="Chippy BBQ", brand="Jack 'n Jill", ctry="en:philippines",
        img=THUMB + "480/001/664/4504/front_en.4.200.jpg", ss="1 pack (27 g)", sq=27, comp=0.8, scans=80,
        kcal=510, P=6.2, C=60, S=4, F=27.5, sat=12, fib=3, na=0.75),
    row("037600105002", name="Skippy Creamy Peanut Butter", brand="Skippy", ctry="en:united-states,en:philippines",
        img=THUMB + "003/760/010/5002/front_en.6.200.jpg", ss="2 tbsp (32 g)", sq=32, comp=0.8, scans=210,
        kcal=594, P=21.9, C=21.9, S=9.4, F=50, sat=9.4, fib=6.3, na=0.469),
    row("4800092110016", name="SkyFlakes Crackers", brand="M.Y. San", ctry="en:philippines,en:united-states",
        img=THUMB + "480/009/211/0016/front_en.8.200.jpg", ss="1 pack (25 g)", sq=25, comp=0.8, scans=150,
        kcal=480, P=8, C=68, S=4, F=20, sat=9, fib=2, na=0.56),
    row("5000169005743", name="Baked Beans in Tomato Sauce", ctry="en:united-kingdom",
        img=THUMB + "500/016/900/5743/front_en.3.200.jpg", ss="1/2 can (200 g)", sq=200, nutri="a", comp=0.5,
        scans=33, kcal=81, P=4.8, C=12.9, S=5, F=0.4, sat=0.1, fib=3.9, salt=0.6133),
    row("8801056038861", name="Banana Flavored Milk", name_en="Banana Milk Drink", brand="Binggrae",
        ctry="en:south-korea", img="/images/products/880/105/603/8861/front_ko.3.200.jpg", ss="240 ml", sq=240,
        comp=0.55, scans=75, kcal=87, P=2.9, C=11.3, S=11.3, F=3.3, sat=2.1, fib=0, na=0.046),
    row("4800361413480", name="  Milo   Activ-Go  Powder ", brand="Nestlé,Milo", ctry="en:philippines",
        img=THUMB + "480/036/141/3480/front_en.5.200.jpg", ss="1 sachet", comp=0.65, scans=55,
        kcal=410, P=9, C=72, S=44, F=9.5, sat=5, fib=6, na=0.2),
    row("4901577000010", name_en="Kewpie Mayonnaise", brand="Kewpie", ctry="en:japan,en:united-states",
        img=THUMB + "490/157/700/0010/front_en.3.200.jpg", ss="1 tbsp (15 g)", sq=15, comp=0.7, scans=130,
        kcal=668, P=2.8, C=0.6, S=0.6, F=72.5, sat=11, fib=0, na=0.72),
    row("8801007000015", name="Bamboo Salt Seasoning", brand="Insan", ctry="en:south-korea", comp=0.3,
        scans=5, kcal=5, P=0, C=1, S=0, F=0, sat=0, fib=0, na=45),
]
assert len(main) == 32

# --- 20 selection rows ---
main += [
    # PH slice: a Philippine barcode (480) tagged only United States; a PH-tagged row with an untypeable
    # name, two foreign tags, a French barcode and no macros; a second Nestle row for the brand boost.
    row(ean("480", 123450), name="Pinoy Spread Peanut", brand="Datu", ctry="en:united-states", comp=0.5, scans=3,
        kcal=590, P=25, C=20, S=9, F=50, sat=10, fib=6, na=0.4),
    row("3012345000012", name="チョコ", brand="Nissin", ctry="en:philippines,en:france,en:germany", comp=0.2,
        scans=7, kcal=500),
    row("4800361002950", name="Bear Brand Powdered Milk", brand="Nestlé", ctry="en:philippines",
        img=THUMB + "480/036/100/2950/front_en.4.200.jpg", ss="4 tbsp (33 g)", sq=33, comp=0.7, scans=12,
        kcal=500, P=24, C=40, S=40, F=27, sat=17, fib=0, na=0.3),
    # left out: two foreign tags and a French barcode; Japan-only tag with a Russian barcode;
    # a name without three Latin letters in a row.
    row("3012345678903", name="Fromage Blanc Nature", brand="Yoplait", ctry="en:united-states,en:france,en:germany",
        comp=0.9, scans=900, kcal=70, P=8, C=4, S=4, F=2, sat=1, fib=0, na=0.05),
    row("4601234000019", name="Halva Peanut Bar", brand="Yashkino", ctry="en:japan",
        img=THUMB + "460/123/400/0019/front_ru.3.200.jpg", ss="40 g", sq=40, comp=0.8, scans=15,
        kcal=520, P=12, C=50, S=35, F=30, sat=6, fib=3, na=0.05),
    row("4901005109803", name="ポッキー チョコレート", brand="Glico", ctry="en:japan",
        img=THUMB + "490/100/510/9803/front_ja.3.200.jpg", ss="1 pack (36 g)", sq=36, comp=0.9, scans=40,
        kcal=503, P=8.3, C=66.7, S=36.1, F=22.2, sat=11.1, fib=2.8, na=0.2),
    # zero scans: Latin-letter brand and a photo are both needed
    row("4903032230116", name="Black Thunder Chocolate Bar", brand="Yuraku", ctry="en:japan",
        img=THUMB + "490/303/223/0116/front_ja.2.200.jpg", comp=0.5, kcal=540, P=7, C=60, S=30, F=30),
    row("4902777018393", name="Meltykiss Premium Chocolate", brand="Meiji", ctry="en:japan", comp=0.9,
        kcal=590, P=6, C=50, F=40),
    row("5000169100011", name="Plain Wafer Rolls", ctry="en:united-kingdom",
        img=THUMB + "500/016/910/0011/front_en.3.200.jpg", comp=0.6, kcal=480, P=5, C=70, F=20),
    row("4902888241437", name="Hokkaido Milk Caramel", brand="森永製菓", ctry="en:japan",
        img=THUMB + "490/288/824/1437/front_ja.5.200.jpg", comp=0.9, kcal=430, P=4, C=78, F=11),
    # kept in spite of a foreign tag because the barcode prefix is a wanted market
    row("5000168001142", name="McVitie's Digestive Biscuits", brand="McVitie's",
        ctry="en:united-kingdom,en:ireland,en:france", img=THUMB + "500/016/800/1142/front_en.6.200.jpg",
        ss="1 biscuit (15 g)", sq=15, comp=0.8, scans=386, kcal=483, P=7, C=62.5, S=16.5, F=21, sat=10,
        fib=3.5, salt=1.3),
    row("8996001600269", name="Indomie Mi Goreng", brand="Indomie", ctry="en:united-states,en:indonesia",
        img=THUMB + "899/600/160/0269/front_en.4.200.jpg", ss="1 pack (85 g)", sq=85, comp=0.7, scans=29,
        kcal=459, P=9.4, C=63, S=8, F=18.8, sat=9, fib=2.5, salt=1.1),
    # three "Maltesers" under three brands: the second repeats the first one's plain name and rounded kcal
    row("040000579816", name="Maltesers", brand="Mars", ctry="en:united-states",
        img=THUMB + "004/000/057/9816/front_en.5.200.jpg", ss="1 pack (37 g)", sq=37, comp=0.8, scans=25,
        kcal=500, P=8, C=61, S=52, F=25, sat=15, fib=1, na=0.15),
    row("040000579830", name="Maltesers", brand="Mars Wrigley", ctry="en:united-states",
        img=THUMB + "004/000/057/9830/front_en.2.200.jpg", ss="1 pack (37 g)", sq=37, comp=0.9, scans=20,
        kcal=500.4, P=8, C=61, S=52, F=25, sat=15, fib=1, na=0.15),
    row("040000579847", name="Maltesers", brand="Mars Food", ctry="en:united-states", comp=0.5, scans=10,
        kcal=480, P=8, C=58, F=24),
    # brand boost: Nestle is on two PH rows (Milo, Bear Brand), Kikkoman on only one
    row("028000242008", name="Nesquik Chocolate Powder", brand="Nestle", ctry="en:united-states",
        img=THUMB + "002/800/024/2008/front_en.3.200.jpg", ss="2 tbsp (20 g)", sq=20, comp=0.6, scans=10,
        kcal=380, P=5, C=80, S=70, F=3.5, sat=2, fib=5, na=0.2),
    row("0041390000189", name="Kikkoman Teriyaki Sauce", brand="Kikkoman", ctry="en:united-states",
        ss="1 tbsp (18 g)", sq=18, comp=0.7, scans=20, kcal=120, P=2, C=25, S=20, F=0, na=3.2),
    # same rank, different completeness; a neutral-tag row with a German barcode
    row("0070000000019", name="Plain Crackers Salted", brand="Rebisco", ctry="en:united-states", comp=0.9,
        scans=8, kcal=430, P=9, C=70, S=3, F=12, sat=5, fib=2, na=0.6),
    row("4002468135795", name="Gouda Cheese Slices", brand="Member's Mark",
        ctry="en:united-states,en:world,en:european-union", ss="1 slice (21 g)", sq=21, comp=0.5, scans=8,
        kcal=357, P=25, C=2, S=0, F=27, sat=18, na=0.8),
    # no division by foreign tags: 386 scans with two foreign tags still beats 150 clean scans
    row("5000300000011", name="Pot Noodle Original Curry", brand="Pot Noodle", ctry="en:united-kingdom",
        comp=0.5, scans=150, kcal=420, P=9, C=55, S=3, F=18, sat=8, fib=3, salt=0.2),
]
assert len(main) == 52
write("sample_off.csv", main)

# ======================================================================================
# off_rules.csv : one row per rule branch (validation order: names, sodium, VAL-8, VAL-1, VAL-2, VAL-4, VAL-6)
# ======================================================================================
SAFE = dict(kcal=350, P=5, C=60, F=10)          # 4*5 + 4*60 + 9*10 = 350: passes every consistency test
rules = []
# ---- VAL-5 names and brands -------------------------------------------------------
rules += [
    row(ean("500", 5001), name="Crunchy oats&amp;honey", generic="Crunchy oats", brand="M&amp;amp;amp;S",
        ctry="en:united-kingdom", comp=0.6, scans=3, **SAFE),                                   # entities, aka skipped
    row("6111259347861", name="6111259347861", brand="pauz", ctry="en:united-kingdom", **SAFE),  # digits only: dropped
    row(ean("001", 5003), name="h", generic="Chocolate flavoured cereal", brand="Nesquik",
        ctry="en:united-states", **SAFE),                                                        # junk -> generic name
    row(ean("480", 5004), name="c2", generic="Apple Green Tea", brand="C2", ctry="en:philippines",
        ss="230 ml", sq=230, kcal=38, P=0, C=9.5, S=9.2, F=0),                                   # kept; aka appended
    row(ean("490", 5005), name="麺", generic="即席カップめん", brand="Nissin", ctry="en:japan", **SAFE),  # one CJK char kept
    row(ean("001", 5006), name="SWEET PICKLE INGREDIENTS: cucumbers, sugar, vinegar", brand="no brand",
        ctry="en:united-states", kcal=90, P=0.5, C=21, S=18, F=0.3),                             # cut at "ingredients:"
    row(ean("500", 5007), name="XY", brand="Foo", ctry="en:united-kingdom", **SAFE),             # two letters: dropped
]
# ---- VAL-3 sodium against salt ------------------------------------------------------
rules += [
    row(ean("001", 3001), name="Arnold Whole Grain Bread", brand="Arnold",
        ctry="en:united-states,en:france,en:germany", na=45, salt=1.1, **SAFE),                  # 45 g out of range -> salt 440
    row(ean("500", 3002), name="Plain Wheat Biscuits", brand="Tuck",
        ctry="en:united-kingdom,en:scotland,fr:angleterre,en:world, en:puerto-rico,en:guam,en:american-samoa,"
             "en:england,en:wales,en:northern-ireland,fr:angle,fr:international",
        comp=0.7874999, na=0.5, salt=1.5, **SAFE),                                               # agree within 25% -> sodium
    row(ean("001", 3003), name="Tomato Ketchup Squeezy", brand="Heinz",
        ctry="en:united-states, en:world,en:european-union,en:europe", comp=0.7875, na=0.00175, salt=1.75,
        **SAFE),                                                                                 # unit slip -> the larger
    row(ean("500", 3004), name="Skyr Natural Pot", brand="Yoplait", ctry="en:united-kingdom", comp=0.5, na=0,
        salt=0.14, **SAFE),                                                                      # false 0 -> 56 mg
    row(ean("500", 3005), name="Leerdammer Light Slices", brand="Leerdammer", ctry="en:united-kingdom", na=0.5,
        salt=3.75, **SAFE),                                                                      # conflict -> NULL
]
# ---- VAL-8 sodium typed in milligrams -----------------------------------------------
rules += [
    row(ean("480", 8001), name="Lava Crisps Cheese", brand="Oishi", ctry="en:philippines", ss="1 pack (30 g)",
        sq=30, comp=1, kcal=520, P=6, C=60, F=28, na=28),                                        # 28000 mg, 30 g -> NULL
    row(ean("500", 8002), name="Light Soy Sauce Splash", brand="Amoy", ctry="en:united-kingdom", ss="30 g",
        sq=30, kcal=55, P=3, C=9, S=5, F=0, na=8),                                               # 8000 mg, 2400 per serving: kept
    row(ean("500", 8003), name="Beef Bouillon Powder", brand="Bisto", ctry="en:united-kingdom",
        ss="1 serving (100 g)", sq=100, kcal=200, P=10, C=20, F=9, na=28),                       # 100 g serving: rule off
]
# ---- VAL-1 calories against macros --------------------------------------------------
rules += [
    row(ean("500", 1001), name="Zero Kcal Crackers", brand="Foo", ctry="en:united-kingdom", kcal=0, P=8, C=70,
        S=8, F=12),                                                                              # 0 kcal, real macros: dropped
    row(ean("500", 1002), name="Vodka Premium 40", brand="Foo", ctry="en:united-kingdom", kcal=231, P=0, C=0,
        S=0, F=0),                                                                               # alcoholic by name: kept
    row(ean("500", 1003), name="Plain Glaze Jar", brand="Foo", ctry="en:united-kingdom", kcal=231, P=0, C=0,
        S=0, F=0),                                                                               # same numbers: dropped
    row(ean("500", 1004), name="Twin Caramel Biscuit Bar", brand="Mars", ctry="en:united-kingdom",
        ss="1 bar (16 g)", sq=16, kcal=222, P=5, C=65, S=49, F=24, sat=14, fib=1.5),             # larger tolerance: dropped
    row(ean("500", 1005), name="Erythritol Sweetener Blend", brand="Foo", ctry="en:united-kingdom", kcal=5,
        P=0, C=99, S=0, F=0),                                                                    # zero-sugar sweetener: kept
    row(ean("500", 1006), name="Sugary Fruit Chews", brand="Foo", ctry="en:united-kingdom", kcal=5, P=0,
        C=99, S=5, F=0),                                                                         # 5 g sugars: dropped
    row("4987035332510", name="Pocari Sweat 500ml", brand="Otsuka", ctry="en:philippines,en:japan",
        ss="500 ml", sq=500, comp=0.6, scans=5, kcal=25, P=0, C=0, S=5.8, F=0, na=0.049),        # carbs wrong -> VAL-4
]
# ---- VAL-2 repairs ------------------------------------------------------------------
rules += [
    row(ean("480", 2001), name="fita crackers 30g", brand="My San", ctry="en:philippines", kcal=0, kj=2042,
        P=8, C=68, S=8, F=20, sat=9, salt=0.6),                                                  # R1: 488 kcal estimate
    row(ean("480", 2002), name="Protein Wafer Heavy", brand="Foo", ctry="en:philippines", kcal=0, kj=1900,
        P=25, C=70, S=10, F=6),                                                                  # P + C + F = 101: dropped
    row(ean("001", 2003), name="Pecan Halves", brand="Kirkland Signature", ctry="en:united-states",
        ss="1/4 cup (30 g)", sq=30, kcal=700, P=3, C=4, S=1, F=22, sat=2, fib=3, na=0.125),      # R2: x 100/30
    row(ean("001", 2004), name="Protein Shake Chocolate", brand="Core Power", ctry="en:united-states",
        ss="1 bottle (500 ml)", sq=500, kcal=49, P=20, C=30, S=28, F=5, sat=3, fib=0, na=0.5),   # R2 large: x 100/500
    row(ean("500", 2005), name="Roast Chicken Breast Slices", brand="Sainsbury's", ctry="en:united-kingdom",
        ss="1 slice (30 g)", sq=30, kcal=380, P=22.3, C=1.1, S=0.6, F=2.4, sat=0.7, fib=0, salt=0.6),  # not label-round
]
# ---- VAL-6 portions, VAL-4 sugars / saturates, the carbohydrates-total fallback, image_small_url --------
rules += [
    row(ean("480", 6001), name="Schweppes Tonic Water", brand="Schweppes", ctry="en:philippines",
        img_s=photo(ean("480", 6001), 5), ss="1,330g", sq=1.33, kcal=34, P=0, Ct=8.5, S=8.5, F=0),   # total carbs; thousands
    row(ean("001", 6002), name="Strawberry Jam Jar", brand="Smucker", ctry="en:united-states", ss="30", kcal=250,
        P=0.5, C=62, Ct=65, S=55, F=1.61, sat=6.45),                                             # bare 30; sat > fat; C wins
    row(ean("500", 6003), name="Strawberry Gelatin Dessert", brand="Hacendado", ctry="en:united-kingdom",
        ss="2", kcal=70, P=2, C=14.7, S=74.7, F=0.5),                                            # bare 2; sugars > carbs
    row(ean("500", 6004), name="Protein Cake Slab", brand="Foo", ctry="en:united-kingdom",
        ss="1 slab (300 g)", sq=300, kcal=400, P=10),                                            # 1200 kcal portion; two macros blank
    row(ean("001", 6005), name="Fresh Milk Low Fat", brand="Foo", ctry="en:united-states",
        ss="1 cup (100 g)", sq=100, kcal=50, P=3.2, C=4.83, S=4.88, F=1.5, sat=1.8),             # clamps; not a bare 100 g
    row(ean("500", 6006), name="Sandwich Bread Loaf", brand="Foo", ctry="en:united-kingdom", ss="1 g", sq=1,
        kcal=260, P=8, C=48, S=4, F=3),                                                          # a bare 1 g portion
]
# ---- exactly one of protein, carbs, fat blank ---------------------------------------
rules += [
    row(ean("001", 7001), name="Mi Goreng Fried Noodles", brand="Indomie", ctry="en:united-states", kcal=459,
        P=9.4, F=18.8),                                                                          # carbs = 63.05
    row(ean("001", 7002), name="Puffed Corn Snack", brand="Foo", ctry="en:united-states", kcal=290, C=50, F=10),
    row(ean("001", 7003), name="Peanut Brittle Bar", brand="Foo", ctry="en:united-states", kcal=600, P=30,
        F=30),
]
assert len(rules) == 36, len(rules)
write("off_rules.csv", rules)

# ======================================================================================
# off_dedupe.csv : groups sharing name + brand (and one USDA twin)
# ======================================================================================
dd = []
KF = dict(brand="Kellogg's", name="Corn Flakes Plain", ctry="en:united-states", P=2, C=20, F=2)
dd += [   # 100 and 111 merge (11 <= 10% of 111), 112 does not (12 > 11.2); 'a' is the first kept row
    row("0038000000011", scans=9, kcal=100, comp=0.5, **KF),
    row("0038000000035", scans=6, kcal=112, comp=0.5, **KF),
    row("0038000000028", scans=4, kcal=111, comp=0.5, **KF),
]
dd += [   # 2 and 6 kcal: 4 kcal apart is inside the 5 kcal floor
    row("0012000000014", name="Diet Cola Zero", brand="Pepsi", ctry="en:united-states", scans=3, kcal=2, P=0,
        C=0.5, F=0, comp=0.5),
    row("0012000000021", name="Diet Cola Zero", brand="Pepsi", ctry="en:united-states", scans=1, kcal=6, P=0,
        C=0.5, F=0, comp=0.5),
]
dd += [   # X has more numbers but its energy contradicts its macros (546 vs 4P+4C+9F = 420, 30 % too high,
          # whichever side the 20 % is measured from); Y is 18 % over, 496 kcal, and 50 kcal from X (<= 10 %): Y wins
    row("0029000000015", name="Mixed Nut Clusters", brand="Planters", ctry="en:united-states", scans=20,
        kcal=546, P=10, C=50, S=10, F=20, sat=3, fib=3, na=0.2, comp=0.9),
    row("0029000000022", name="Mixed Nut Clusters", brand="Planters", ctry="en:united-states", scans=5,
        kcal=496, P=10, C=50, F=20, comp=0.4),
]
CW = dict(name="Chocolate Wafer Bites", brand="Munchies", P=3, C=30, F=11)
dd += [   # winner W (7 numbers, no serving); aliases: A never scanned UK, B never scanned JP, C 2 scans + a serving
    row("0040000000012", ctry="en:united-states", scans=50, kcal=200, S=12, sat=5, fib=1, na=0.1, comp=0.9, **CW),
    row("5010000000019", ctry="en:united-kingdom", scans=0, kcal=204, comp=0.3, **CW),
    row("4901234567894", ctry="en:japan", scans=0, kcal=198, comp=0.3, **CW),
    row("0040000000029", ctry="en:united-states", scans=2, kcal=205, ss="30 g", sq=30, comp=0.3, **CW),
]
HS = dict(name="Hazelnut Spread Jar", brand="Hazelo", P=6, C=57, F=31)
dd += [   # W is tagged with two foreign countries; both others are clean; one of them is PH-tagged
    row("3012345000050", ctry="en:united-states,en:france,en:germany", scans=100, kcal=540, S=56, sat=10, fib=2,
        na=0.05, comp=0.8, **HS),
    row("0040000000036", ctry="en:united-states", scans=4, kcal=545, comp=0.3, **HS),
    row("4801234000019", ctry="en:philippines", scans=2, kcal=535, comp=0.3, **HS),
]
dd += [   # an Open Food Facts row that is 300 kcal against the USDA row's 594: must not merge into it
    row("0037600100002", name="Creamy Peanut Butter", brand="Skippy", ctry="en:united-states", scans=20,
        kcal=300, P=5, C=50, F=8, comp=0.5),
]
assert len(dd) == 15, len(dd)
write("off_dedupe.csv", dd)

# ======================================================================================
# off_alias_guard.csv : a row that owns the barcode another row holds as an alias
# ======================================================================================
guard = [row("0038000000028", name="Totally Different Snack", brand="Other Co", ctry="en:philippines", scans=5,
             comp=0.5, kcal=400, P=8, C=60, F=14)]
write("off_alias_guard.csv", guard)

# ======================================================================================
# off_serving.csv : labels typed per serving into the per-100 g fields (Philippine rows)
# ======================================================================================
# The same layout plus the export's "quantity" column (the pack size), which only this file carries.
SERVING_HEADER = HEADER + ["quantity"]


def serving_row(code, quantity="", **kw):
    return row(code, comp=0.5, scans=kw.pop("scans", 3), **kw) + [quantity]


PH = "en:philippines"
sv = [
    # S1 snack with a stated serving: 117 kcal is 25 g of crackers -> 468 kcal per 100 g, "25 g" first
    serving_row(ean("480", 7001), name="Fish Crackers", brand="Seaside", ctry=PH, ss="25 g", sq=25,
                kcal=117, P=2, C=17.8, S=2, F=4.4, na=0.2),
    # S2 the pack weight only in the name: 160 kcal for 33 g -> 484.8, "1 pack (33 g)" first
    serving_row(ean("480", 7002), name="Double Choco Cookies 33g", brand="Crunchy Co", ctry=PH,
                kcal=160, P=2, C=22, F=7),
    # S3 bread with no weight anywhere: stored per serving under its own serving text "1 piece"
    serving_row(ean("480", 7003), name="Cheese Ensaymada", brand="Panaderia", ctry=PH, ss="1 piece", scans=10,
                kcal=140, P=3, C=18, F=6),
    # S4 a sweet drink typed per bottle: 100 kcal for 500 ml -> 20 kcal per 100 ml, the bottle first
    serving_row(ean("480", 7004), name="Lemon Iced Tea", brand="Leafy", ctry=PH, ss="1 bottle (500 ml)", sq=500,
                kcal=100, P=0, C=25, S=25, F=0),
    # S5 a real low-calorie drink: left alone
    serving_row(ean("480", 7005), name="Iced Coffee Drink", brand="Brewed", ctry=PH, ss="240 ml", sq=240,
                kcal=30, P=1, C=5, F=0.7),
    # S6 a real low-calorie soup: left alone
    serving_row(ean("480", 7006), name="Chicken Noodle Soup", brand="Mamas", ctry=PH, ss="250 ml", sq=250,
                kcal=45, P=2, C=6, F=1.5),
    # S7 fresh noodles really are 150 kcal: left alone
    serving_row(ean("480", 7007), name="Fresh Miki Noodles", brand="Mamas", ctry=PH, ss="100 g", sq=100,
                kcal=150, P=5, C=30, F=1),
    # S8 biscuits at 0 kcal: dropped
    serving_row(ean("480", 7008), name="Iced Gem Biscuits", ctry=PH, kcal=0, P=0, C=0, F=0),
    # S9 a zero-calorie drink: kept as it is
    serving_row(ean("480", 7009), name="Cola Zero", brand="Fizz", ctry=PH, ss="330 ml", sq=330,
                kcal=0, P=0, C=0, F=0),
    # S10 the website's placeholder as the name, nothing else to name it by: dropped
    serving_row(ean("480", 7010), name="Loading…", brand="Selecta", ctry=PH, kcal=129, P=2, C=17, F=6),
    # S11 the same placeholder with a generic name: named by the generic name
    serving_row(ean("480", 7011), name="Loading...", generic="Strawberry Yogurt Drink", brand="Dairy Best",
                ctry=PH, kcal=70, P=2, C=12, F=1.5),
    # S12 the S1 pattern on a row sold in the United States only: left alone (Philippine rows only)
    serving_row(ean("001", 7012), name="Fish Crackers", brand="Seaside US", ctry="en:united-states", ss="25 g",
                sq=25, kcal=117, P=2, C=17.8, S=2, F=4.4),
    # S13 the pack weight only in the quantity column: 150 kcal for 30 g -> 500, "1 pack (30 g)" first
    serving_row(ean("480", 7013), quantity="30 g", name="Corn Chips", brand="Crispo", ctry=PH,
                kcal=150, P=2, C=19, F=7.5),
    # S14 the label (143 kcal, 3 / 20 / 6 g) typed per serving with the pack's 180 g as the serving, so the
    #     file's per-100 g figures are those divided by 1.8. Put back to the label figures; a dry snack, so
    #     the serving weighs about (3 + 20 + 6) / 0.95 = 31 g -> 461 kcal per 100 g, "1 serving (about 31 g)"
    serving_row(ean("480", 7014), name="Taco Shells", brand="Bambi", ctry=PH, ss="180 g", sq=180,
                kcal=79.4, P=1.67, C=11.1, F=3.33, na=0.0667),
    # S15 same name and brand as S3 but real numbers per 100 g: must stay a row of its own
    serving_row(ean("480", 7015), name="Cheese Ensaymada", brand="Panaderia", ctry=PH, scans=1,
                kcal=380, P=8, C=50, F=16),
    # S16 a dry snack with no weight anywhere: (2 + 15 + 8) / 0.95 = 26 g -> 538.5 kcal, "1 pouch (about 26 g)"
    serving_row(ean("480", 7016), name="Cheese Curls", brand="Snacky", ctry=PH, ss="1 pouch",
                kcal=140, P=2, C=15, F=8, na=0.9),
    # S17 a stated weight that explains nothing (65 kcal for a 250 g jar), not a dry food: per serving
    serving_row(ean("480", 7017), name="Sandwich Spread", brand="Ladys", ctry=PH, ss="250 g", sq=250,
                kcal=65, P=0, C=4, F=6),
    # S18 typed per serving like S14 (110 kcal, 3 / 20 / 2 g for a 60 g bun) but 183 kcal is not far
    #     enough off for bread and bread is not dry: left as it is, its bun still showing 110 kcal
    serving_row(ean("480", 7018), name="Burger Buns", brand="Bakerite", ctry=PH, ss="1 bun (60 g)", sq=60,
                kcal=183, P=5, C=33.3, F=3.33),
]
assert len(sv) == 18, len(sv)
write("off_serving.csv", sv, SERVING_HEADER)

# ======================================================================================
# off_doubt.csv : rows from other markets whose numbers are doubtful (pool_doubt) and the
#                 US pool's own selection rules
# ======================================================================================
US, UK = "en:united-states", "en:united-kingdom"


def doubt_row(code, **kw):
    return row(code, comp=0.5, img=photo(code), **kw)


db = [
    # D1 per_serving: a 68 g bar's label (260 kcal, 9 / 42 / 7 g) typed per 100 g; 260 x 68 / 100 = 176.8 is no label figure
    doubt_row(ean("001", 8001), name="Crunchy Peanut Butter Bar", brand="Trail Co", ctry=US, ss="1 bar (68 g)", sq=68,
              scans=40, kcal=260, P=9, C=42, F=7),
    # D2 dry_coffee: the dry-grounds figures on ground coffee; more scans and more numbers than its twin D3
    doubt_row(ean("001", 8002), name="Ground Coffee", brand="Morning Roast", ctry=US, scans=20,
              kcal=396.6, P=14.4, C=40.2, F=15.4, sat=5, fib=10, na=0.074),
    # D3 the same coffee (same name and brand, calories within 10 %) without the tell-tale figures: it merges
    # into D2, and the row that is left stays marked
    doubt_row(ean("001", 8003), name="Ground Coffee", brand="Morning Roast", ctry=US, ss="2 tbsp (10 g)", sq=10, scans=1,
              kcal=380, P=12, C=40, F=15),
    # D4 per_bottle: a 450 ml bottle's 330 kcal typed per 100 ml (330 x 4.5 = 1485 is round too)
    doubt_row(ean("001", 8004), name="Protein Plus Drink", brand="Farmhouse", ctry=US, ss="1 container (450 ml)",
              sq=450, scans=35, kcal=330, P=30, C=37, F=6),
    # D5 juice_high: a bottle's figure on a juice in a drinks category, the serving written as "100 g"
    doubt_row(ean("001", 8005), name="100% Apple Juice", brand="Orchard", ctry=US, cat="Beverages,Fruit juices",
              ss="1 serving (100 g)", sq=100, scans=30, kcal=170, P=0, C=41, F=0),
    # D6 zero_kcal: strawberries at 0 kcal with 0 g of everything
    doubt_row(ean("001", 8006), name="Fresh Strawberries", brand="Berry Farms", ctry=US, ss="1 cup (144 g)", sq=144,
              scans=25, kcal=0, P=0, C=0, F=0),
    # D7 not_food: a shampoo
    doubt_row(ean("001", 8007), name="Argan Oil Shampoo", brand="Salon Co", ctry=US, scans=22, kcal=0, P=0, C=0, F=0),
    # D8 a believable US product: chosen
    doubt_row(ean("001", 8008), name="Classic Potato Chips", brand="Crispy Co", ctry=US, ss="1 oz (28 g)", sq=28,
              scans=5, kcal=536, P=7, C=53, F=34),
    # D9 a British pack (500 barcode) tagged US and UK: not in the US pool, it waits for the UK pool
    doubt_row(ean("500", 8009), name="Original Crumpets", brand="Bakehouse", ctry=US + "," + UK, ss="1 crumpet (55 g)",
              sq=55, scans=6, kcal=178, P=5.9, C=36.4, F=0.9),
    # D10 a worldwide pack with a French barcode, tagged US only: stays in the US pool
    doubt_row(ean("300", 8010), name="Hazelnut Spread", brand="Noisette", ctry=US, ss="1 tbsp (15 g)", sq=15,
              scans=3, kcal=539, P=6.3, C=57.5, F=30.9),
    # D11 the D1 pattern on a Philippine row (480 barcode, tagged PH): never marked, exported with the PH rows
    doubt_row(ean("480", 8011), name="Soy Sauce", brand="Toyo Co", ctry=PH, ss="1 tbsp (15 ml)", sq=15,
              scans=2, kcal=10, P=1, C=1, F=0),
    # D12 the D1 pattern on a row tagged UK only: British labels are round per 100 g, so it is not marked
    doubt_row(ean("500", 8012), name="Oat Biscuits", brand="Hobnob Co", ctry=UK, ss="1 biscuit (12 g)", sq=12,
              scans=2, kcal=470, P=7, C=63, F=20),
    # D13 round figures with a serving of 100 g: nothing to tell, not marked
    doubt_row(ean("001", 8013), name="Pretzel Twists", brand="Bakery Co", ctry=US, ss="1 serving (100 g)", sq=100,
              scans=4, kcal=380, P=10, C=78, F=3),
    # D14 mayonnaise without a serving size, scanned 8 times: it counts for half (4) in the US pool, so it comes
    # after the chips (5) and after the pretzels (4, with a serving), but before the spread (3)
    doubt_row(ean("001", 8014), name="Real Mayonnaise", brand="Golden Spoon", ctry=US, scans=8,
              kcal=680, P=1, C=0.6, F=75),
    # D15 mayonnaise with a spoon serving (680 x 100 / 14 is impossible, so not per_serving): an ordinary row
    doubt_row(ean("001", 8015), name="Whole Egg Mayonnaise", brand="Golden Spoon", ctry=US, ss="1 tbsp (14 g)", sq=14,
              scans=2, kcal=700, P=1, C=1, F=78),
    # D16 coffee capsules: "capsules" names a pill, but not on a coffee
    doubt_row(ean("001", 8016), name="Espresso Coffee Capsules", brand="Crema", ctry=US, ss="1 capsule (5 g)", sq=5,
              scans=1, kcal=0, P=0, C=0, F=0),
    # D17 per_serving: a 32 g serving's label (190 kcal, 7 / 8 / 16 g) typed per 100 g
    doubt_row(ean("001", 8017), name="Creamy Peanut Butter", brand="Nutty Co", ctry=US, ss="2 tbsp (32 g)", sq=32,
              scans=2, kcal=190, P=7, C=8, F=16),
    # D18 the same peanut butter with the same wrong numbers but no serving size, so nothing marks it by itself.
    # It has more numbers and more scans, so D17 merges into it: it takes D17's serving and D17's mark with it
    doubt_row(ean("001", 8018), name="Creamy Peanut Butter", brand="Nutty Co", ctry=US, scans=20,
              kcal=190, P=7, C=8, F=16, sat=3, fib=2, na=0.14),
    # D19 and D20: one barcode on two lines. The marked record (D19, more scans) loses to the believable one
    doubt_row(ean("001", 8019), name="Honey Oat Bar", brand="Trail Co", ctry=US, ss="1 bar (42 g)", sq=42,
              scans=30, kcal=190, P=3, C=29, F=7),
    doubt_row(ean("001", 8019), name="Honey Oat Bar", brand="Trail Co", ctry=US, ss="1 bar (42 g)", sq=42,
              scans=1, kcal=452.4, P=7.1, C=69, F=16.7),
    # D21 a British supermarket's own pack with a short in-store code, tagged US by mistake: UK pool, not US
    doubt_row("00357937", name="Semi Skimmed Milk", brand="Sainsbury's", ctry=US, ss="200 ml", sq=200,
              scans=3, kcal=50, P=3.6, C=4.8, F=1.8),
    # D22 per_serving: half a cup's label (20 kcal, 1 / 3 / 0 g) typed per 100 g; more scans and more numbers
    # than its twin D23, so it is walked first
    doubt_row(ean("001", 8022), name="Cut Green Beans", brand="Garden Co", ctry=US, ss="0.5 cup (121 g)", sq=121,
              scans=21, kcal=20, P=1, C=3, F=0, sat=0, fib=1, na=0.3),
    # D23 the same can worked out per 100 g (16.5 kcal), with a serving of its own: it passed the test D22
    # failed, so it changes places with D22 and is the row that stays
    doubt_row(ean("001", 8023), name="Cut Green Beans", brand="Garden Co", ctry=US, ss="121 g", sq=121,
              scans=2, kcal=16.5, P=0.83, C=3.31, F=0),
]
assert len(db) == 23, len(db)
write("off_doubt.csv", db)

# ======================================================================================
# off_asia.csv : the seven neighbouring markets (Thailand, Malaysia, Singapore, Indonesia,
#                Vietnam, Hong Kong, Taiwan), stored under the one region code "AS"
# ======================================================================================
TH, MY, SG, VN, HK = "en:thailand", "en:malaysia", "en:singapore", "en:vietnam", "en:hong-kong"
AS_SAFE = dict(kcal=350, P=5, C=60, F=10)        # 4*5 + 4*60 + 9*10 = 350: no rule touches these rows


def asia_row(code, **kw):
    kw.setdefault("comp", 0.5)
    kw.setdefault("img", photo(code))
    for key, value in AS_SAFE.items():
        kw.setdefault(key, value)
    return row(code, **kw)


asia = [
    # AS1 Thailand only, Thai barcode: region "AS", in the Asia pool (40 scans: first there)
    asia_row(ean("885", 9001), name="Tom Yum Shrimp Noodles", brand="Mama", ctry=TH, scans=40),
    # AS2 Malaysia only, Swiss barcode: the tag alone makes it an Asia pool row (35 scans)
    asia_row(ean("761", 9002), name="Chocolate Malt Drink Powder", brand="Milo", ctry=MY, scans=35),
    # AS3 Japanese barcode, tagged Thailand only: region "AS", but a Japanese product stays in the Japan pool
    asia_row(ean("490", 9003), name="Matcha Biscuit Sticks", brand="Glico", ctry=TH, scans=30),
    # AS4 Korean barcode, tagged Korea and Singapore: region "KR,AS", in the Korea pool
    asia_row(ean("880", 9004), name="Honey Butter Chips", brand="Haitai", ctry="en:south-korea," + SG, scans=20,
             kcal=540, P=6, C=54, F=33),            # a crisp's own figures: under 400 kcal it would be kept out
    # AS5 Philippine barcode, tagged Philippines and Singapore: region "PH" (home is never also "AS"), PH slice
    asia_row(ean("480", 9005), name="Cheese Potato Crisps", brand="Piattos", ctry=PH + "," + SG, scans=15),
    # AS6 Philippine barcode, tagged Vietnam only: no region at all, so it is not imported (as before "AS")
    asia_row(ean("480", 9006), name="Garlic Corn Snack", brand="Boy Bawang", ctry=VN, scans=14),
    # AS7 American barcode, tagged US and Hong Kong: region "US,AS", but an American pack stays in the US pool
    asia_row(ean("001", 9007), name="Peanut Butter Cups", brand="Reeses", ctry="en:united-states," + HK, scans=50),
    # AS8 tagged US only, few numbers and 2 scans; AS9 below is its Singapore twin
    asia_row(ean("001", 9008), name="Wheat Thins Original", brand="Nabisco", ctry="en:united-states", scans=2),
    # AS9 the same name and brand, Singapore only, more numbers and 90 scans. Without a rule it would win the
    # merge; a row tagged only "AS" never replaces a five-market row and gives AS8 nothing but its barcode
    asia_row(ean("888", 9009), name="Wheat Thins Original", brand="Nabisco", ctry=SG, scans=90,
             kcal=358, C=62, S=10, sat=2, fib=3, na=0.5),
    # AS10 a Thai-script country tag (th:...): region "AS", Asia pool (12 scans)
    asia_row(ean("885", 9010), name="Green Curry Paste", brand="Mae Ploy", ctry="th:ไทย", scans=12),
    # AS11 a Chinese-script country tag for Taiwan (zh:...): region "AS", Asia pool (8 scans)
    asia_row(ean("471", 9011), name="Pineapple Cake", brand="Chia Te", ctry="zh:台灣", scans=8),
    # AS12 Thailand and France, French barcode: one foreign tag and no market barcode, so in no pool
    asia_row(ean("300", 9012), name="Coconut Cream Dessert", brand="Chaokoh", ctry=TH + ",en:france", scans=60),
    # AS13 Hong Kong only, Chinese barcode (690): Asia pool (25 scans)
    asia_row(ean("690", 9013), name="Premium Oyster Sauce", brand="Lee Kum Kee", ctry=HK, scans=25),
    # AS14 British barcode, tagged UK and Singapore: region "UK,AS", a British pack stays in the UK pool
    asia_row(ean("500", 9014), name="Digestive Thins", brand="McVities", ctry="en:united-kingdom," + SG, scans=45),
    # AS15 Vietnam only, Vietnamese barcode (5 scans): last in the Asia pool
    asia_row(ean("893", 9015), name="Instant Pho Beef Flavour", brand="Vifon", ctry=VN, scans=5),
]
assert len(asia) == 15, len(asia)
write("off_asia.csv", asia)

# ======================================================================================
# off_asia2.csv : the rules for the Japan, Korea and Asia pools (step 5): labels typed per
#                 pack or serving, short Japanese-looking codes, names, brands and labels
#                 in the export. Imported with local_ph_curated.json (for B15).
# ======================================================================================
JP, KR = "en:japan", "en:south-korea"
asia2 = [
    # B1 Thai Lay's at 150 kcal with a 30 g serving: re-based to 500 kcal per 100 g, "30 g" first (Asia pool)
    asia_row(ean("885", 9101), name="Lays Original Flavor", brand="Lay's", ctry=TH, scans=30,
             kcal=150, P=2, C=18, F=8, ss="30 g", sq=30),
    # B2 Korean seaweed snack at 25 kcal: dense_low, in no pool
    asia_row(ean("880", 9102), name="Roasted Seaweed Snack", brand="Haesong", ctry=KR, scans=40,
             kcal=25, P=1, C=1, F=1.5),
    # B3 Thai instant noodles carrying the made-up figures, dry pack as serving: dense_low
    asia_row(ean("885", 9103), name="Instant Noodles Tom Yum Flavour", brand="Mama", ctry=TH, scans=40,
             kcal=72, P=1.5, C=9.5, F=3, ss="60 g", sq=60),
    # B4 a Thai cola at 100 kcal per 100 ml: drink_high
    asia_row(ean("885", 9104), name="Coca-Cola Original", brand="Coca-Cola", ctry=TH, scans=40,
             kcal=100, P=0, C=25, F=0, ss="325 ml", sq=325),
    # B5 a 3-in-1 malt drink powder at 123 kcal (one sachet's figures): mix_low
    asia_row(ean("955", 9105), name="Ovaltine 3 in 1", brand="Ovaltine", ctry=SG, scans=40,
             kcal=123, P=1.2, C=24, F=2),
    # B6 Korean dumplings at 0 kcal: zero_food
    asia_row(ean("880", 9106), name="Kimchi Mandu", brand="Sempio", ctry=KR, scans=40, kcal=0, P=0, C=0, F=0),
    # B7 a Malaysian label typed per serving (round figures, 68 g share 176.8 kcal): per_serving_as
    asia_row(ean("955", 9107), name="Chicken Curry Puff", brand="Ayam", ctry=MY, scans=40,
             kcal=260, P=9, C=42, F=7, ss="1 piece (68 g)", sq=68),
    # B8 an 8-digit code read as Japanese (49...) on a pack tagged only US: no origin, US pool
    asia_row("0000049403954", name="Zebra Cakes", brand="Little Debbie", ctry="en:united-states", scans=40),
    # B9 the same kind of code tagged Japan: Japan pool
    asia_row("0000045208812", name="Yakult Light", brand="Yakult", ctry=JP, scans=9),
    # B10 a brand written as a company's name in Japanese: "Kobe Bussan" in the file (Japan pool)
    asia_row(ean("490", 9110), name="Rich Toffee Biscuit Bars", brand="株式会社 神戸物産", ctry=JP, scans=8),
    # B11 "7-11": "7-Eleven" in the file, found by "Seven Eleven" (Asia pool)
    asia_row(ean("885", 9111), name="Egg Mayo Sandwich", brand="7-11", ctry=TH, scans=20),
    # B12 a Japanese serving label: "1 pack (80 g)", and the pack opens first (Japan pool)
    asia_row(ean("490", 9112), name="Chibi Sours Gummy", brand="Nobel", ctry=JP, scans=7, ss="1袋(80g)", sq=80),
    # B13 a shop's item code and the pack size in the name: "Vida Salty Lychee" (Asia pool)
    asia_row(ean("888", 9113), name="12001187 Vida Salty Lychee 325ml", brand="Vida", ctry=SG, scans=10,
             kcal=40, P=0, C=10, F=0),
    # B14 an instant-noodle cup with its weight as serving: opens on "68 g" (Korea pool)
    asia_row(ean("880", 9114), name="Shin Ramyun Cup", brand="Nongshim", ctry=KR, scans=30,
             kcal=440, P=9, C=66, F=15, ss="68 g", sq=68),
    # B15 no brand and the name of a curated row (Chicken adobo, 141 kcal: too far off to merge): left out of
    # the Korea pool
    asia_row(ean("880", 9115), name="Chicken Adobo", ctry=KR, scans=50),
    # B16 a name mostly in Japanese script that nobody has scanned (completeness 0.9): after every other Japan
    # row, also after B19 (not scanned either, completeness 0.5, a Latin name)
    asia_row(ean("490", 9116), name="ZEMBヌードル 海老とブロッコリーのプーリア風", brand="ZENB", ctry=JP, scans=0, comp=0.9),
    # B17 a Chinese barcode tagged only UK (30 scans) counts for half (15) in the Asia pool: after B18 (20)
    asia_row(ean("690", 9117), name="Sriracha Chilli Sauce", brand="Flying Goose", ctry="en:united-kingdom",
             scans=30),
    # B18 tagged Thailand (20 scans)
    asia_row(ean("885", 9118), name="Sweet Chilli Sauce", brand="Mae Ploy", ctry=TH, scans=21),
    # B19 a Latin-named Japan row nobody has scanned
    asia_row(ean("490", 9119), name="Curry Roux Medium Hot", brand="House", ctry=JP, scans=0),
    # B20 a name mostly in Japanese script WITH scans keeps its place: first in the Japan pool (50 scans)
    asia_row(ean("490", 9120), name="Tuna シーチキンマイルド", brand="Hagoromo", ctry=JP, scans=50),
    # B21 a KitKat typed per piece (60 kcal, 11 g): re-based to 545 kcal per 100 g, "11 g" first (Japan pool)
    asia_row(ean("490", 9121), name="KitKat Wasabi", brand="Nestle", ctry=JP, scans=6,
             kcal=60, P=0.7, C=6.6, F=3.4, ss="11 g", sq=11),
    # B22 a peanut butter typed per spoonful (84 kcal, 15 g): re-based to 560 kcal per 100 g (Asia pool)
    asia_row(ean("955", 9122), name="Crunchy Peanut Butter", brand="Amazin Graze", ctry=MY, scans=6,
             kcal=84, P=4, C=3, F=6.2, ss="15 g", sq=15),
    # B23 an oat milk at 130 kcal per 100 ml (a 200 ml carton's figures): drink_high
    asia_row(ean("899", 9123), name="Oat Milk Original", brand="Oatside", ctry="en:indonesia", scans=40,
             kcal=130, P=1, C=16, F=6, ss="200 ml", sq=200),
    # B24 Korean nori sheets at 7 kcal: dense_low
    asia_row(ean("880", 9124), name="Sushi Nori", brand="Kim Nori", ctry=KR, scans=40,
             kcal=7, P=1, C=0.5, F=0.1, ss="2 g", sq=2),
    # B25 a sauce whose "serving" is the bottle: opens on 100 g, not on "200 g" (Korea pool)
    asia_row(ean("880", 9125), name="Buldak Hot Chicken Sauce", brand="Samyang", ctry=KR, scans=12,
             kcal=300, P=3, C=60, F=5, ss="200 g", sq=200),
    # B26 a burger with its weight as serving, typed "160g": opens on "160 g" (Korea pool)
    asia_row(ean("880", 9126), name="Cheeseburger", brand="Sajo", ctry=KR, scans=11,
             kcal=289, P=12, C=30, F=13, ss="160g", sq=160),
    # B27 an instant soup with a small serving: opens on "1 serving (18 g)" (Japan pool)
    asia_row(ean("490", 9127), name="Instant Miso Soup", brand="Hikari", ctry=JP, scans=5,
             kcal=171, P=11, C=22, F=4, ss="1 serving (18 g)", sq=18),
    # B28, B29 "Soy Sauce" twice at the same calories: the Korean one (Korea pool) does not keep the American
    # one out of the US pool
    asia_row(ean("880", 9128), name="Soy Sauce", brand="Sempio", ctry=KR, scans=10, kcal=67, P=8, C=8, F=0),
    asia_row(ean("001", 9129), name="Soy Sauce", brand="Kikkoman", ctry="en:united-states", scans=10,
             kcal=67, P=8, C=8, F=0),
    # B30 a shop's listing saved as a name: left out of the Asia pool
    asia_row(ean("885", 9130), name="SNACK PACK COFFEE 25x $1 PROL", brand="Snickers", ctry=TH, scans=45),
]
assert len(asia2) == 30, len(asia2)
write("off_asia2.csv", asia2)

# ======================================================================================
# off_step6.csv : the UK shelf and the step 6 repairs (with a "quantity" column): stock cubes,
#                 bread typed per slice, a kJ repair, drink powders, squash, a zero drink storing
#                 the bottle, dry noodle packs, plain oils, a Philippine row read by hand, pack
#                 sizes as portions, and repeats whose barcodes are passed on.
# ======================================================================================
UK, US, KRUS = "en:united-kingdom", "en:united-states", "en:south-korea,en:united-states"


def s6_row(code, quantity="", **kw):
    kw.setdefault("comp", 0.5)
    kw.setdefault("img", photo(code))
    return row(code, **kw) + [quantity]


step6 = [
    # U1 Knorr stock cubes stored as made up (6 kcal) with the cube as serving: opens on "100 ml made up" (UK pool)
    s6_row(ean("500", 6001), name="Beef Stock Cubes", brand="Knorr", ctry=UK, scans=30,
           kcal=6, P=0.3, C=0.6, F=0.3, ss="1 cube (10 g)", sq=10),
    # U2 sourdough typed per 50 g slice (127 kcal): re-based to 254 kcal, "1 slice (50 g)" first (UK pool)
    s6_row(ean("505", 6002), name="Rye Sourdough Bread", brand="Tesco", ctry=UK, scans=29,
           kcal=127, P=4.5, C=21.3, F=1.9, ss="1 slice (50 g)", sq=50),
    # U3 a granary loaf at 112 kcal and no serving to explain it: uk_bread_low, in no pool
    s6_row(ean("501", 6003), name="Granary Bread", brand="Hovis", ctry=UK, scans=28, kcal=112, P=4, C=20, F=1.5),
    # U4 wheat biscuits at 227 kcal whose own kJ (1550) fits their macros: 370.5 kcal, basis "estimate" (UK pool)
    s6_row(ean("501", 6004), name="Original Wheat Biscuits", brand="Weetabix", ctry=UK, scans=27,
           kcal=227, kj=1550, P=12, C=69, F=2),
    # U5 a cappuccino powder stored as made up (24 kcal) with its 14 g sachet: uk_powder_made_up
    s6_row(ean("500", 6005), name="Cappuccino Unsweetened", brand="Nescafe", ctry=UK, scans=26,
           kcal=24, P=1, C=3, F=0.9, ss="1 sachet (14 g)", sq=14),
    # U6 squash stored diluted (4 kcal) with a 35 ml measure: opens on "1 glass, diluted (250 ml)" (UK pool);
    # at 10 kcal with 2.3 g of carbs it would be a no-added-sugar concentrate and keep its 35 ml
    s6_row(ean("500", 6006), name="Lemon Squash", brand="Robinsons", ctry=UK, scans=25,
           kcal=4, P=0.1, C=0.9, F=0, ss="35 ml", sq=35),
    # U7 a bar labelled per 100 g with round figures and a 68 g serving: the US per-serving test is not asked (UK pool)
    s6_row(ean("500", 6007), name="Peanut Bar", brand="Nutty", ctry=UK, scans=24,
           kcal=260, P=9, C=42, F=7, ss="1 bar (68 g)", sq=68),
    # U8 pots sold as "4 x 125g" with no serving: open on "1 unit (125 g)" (UK pool)
    s6_row(ean("500", 6008), "4 x 125g", name="Chocolate Dessert Pots", brand="Gu", ctry=UK, scans=23,
           kcal=150, P=4, C=20, F=6),
    # Z1 a Korean zero soda storing its 500 ml bottle per 100 ml (16 kcal): re-based to 3.2 (Korea pool)
    s6_row(ean("880", 6101), name="Milkis Zero", brand="Lotte", ctry="en:south-korea", scans=20,
           kcal=16, P=1, C=3, F=0, ss="500 ml", sq=500),
    # Z2 a Korean ramen block at 270 kcal with 8.2 g fat (prepared figures): dry_noodle
    s6_row(ean("880", 6102), name="Hot Chicken Flavor Ramen", brand="Samyang", ctry="en:south-korea", scans=19,
           kcal=270, P=6, C=43, F=8.2),
    # Z3 Philippine sesame oil typed per 15 ml (120 kcal): re-based to 800 kcal (Philippine part)
    s6_row(ean("480", 6201), name="Sesame Oil", brand="Good Life", ctry=PH, scans=5,
           kcal=120, P=0, C=0, F=14, ss="15 ml", sq=15),
    # Z4 a Philippine regular soda at 0 kcal, read by hand (KNOWN_WRONG_PH): left out of the Philippine part
    s6_row("4803925033124", name="7 up Regular in can", brand="7up", ctry=PH, scans=4, kcal=0, P=0, C=0, F=0),
    # Z5 a Thai lemon tea sold as 330 ml with no serving: opens on "1 bottle (330 ml)" (Asia pool)
    s6_row(ean("885", 6301), "330 ml", name="Lemon Tea Drink", brand="Chabaa", ctry="en:thailand", scans=18,
           kcal=40, P=0, C=10, F=0),
    # R1, R2 a Korean "Soy Sauce" tagged Korea and US (Korea pool) and Kikkoman's (US): the US one is chosen too
    s6_row(ean("880", 6401), name="Soy Sauce", brand="Sempio", ctry=KRUS, scans=10, kcal=67, P=8, C=8, F=0),
    s6_row(ean("004", 6402), name="Soy Sauce", brand="Kikkoman", ctry=US, scans=9, kcal=67, P=8, C=8, F=0),
    # R3, R4 two US waters of one name and calories: R4 is skipped as a repeat and its barcode finds R3
    s6_row(ean("007", 6403), name="Spring Water", brand="Aqua One", ctry=US, scans=8, kcal=0, P=0, C=0, F=0),
    s6_row(ean("007", 6404), name="Spring water", brand="Aqua Two", ctry=US, scans=7, kcal=0, P=0, C=0, F=0),
    # M1 a British EAN-8 (stored 0000050...) also sold in Germany: local enough for the UK pool (second fix pass)
    s6_row("50123459", name="English Mustard", brand="Colman's", ctry="en:germany," + UK, scans=6,
           kcal=190, P=6.6, C=16, F=10, ss="5g", sq=5),
    # M2 a product of MUST_HAVE_UK, unscanned and sold in four other countries: picked first for the UK pool
    s6_row("6003770000168", name="PERi-PERi Sauce Medium", brand="Nando's", ctry="en:australia,en:france," + UK,
           scans=0, kcal=51, P=0.5, C=1.5, F=4.5, ss="1 portion (20 g)", sq=20),
]
assert len(step6) == 19, len(step6)
write("off_step6.csv", step6, SERVING_HEADER)

# ======================================================================================
# off_step7.csv : Philippine rows carrying a pack's or a serving's figures per 100 g (ph_pack_fix),
#                 made-up powders, a 0 kcal seasoning mix, the step 7 hand entries, and rows that are
#                 right as they are and must come out unchanged. All tagged Philippines.
# ======================================================================================
step7 = [
    # P1 a stock-list name with the pack weight: 250 kcal for one 55 g pack -> 454.5, "1 pack (55 g)"
    s6_row("4807770270024", name="lm chicken 55g", brand="Lucky Me!", ctry=PH, scans=5, kcal=250, P=6, C=34, F=10),
    # P2 the serving (30 g) wins over the name's 45 g pack: 90 -> 300, "1 serving (30 g)"
    s6_row("7622210892126", name="eden original 45g", brand="Eden", ctry=PH, scans=4, kcal=90, P=3, C=3, F=7,
           ss="30g", sq=30),
    # P3 serving_quantity says 100, the serving text says 7.8 g: 24 -> 307.7, "1 serving (7.8 g)"
    s6_row("4801958390108", name="crispy fry (r) 62g original", brand="Ajinomoto", ctry=PH, scans=3,
           kcal=24, P=0.6, C=5, F=0, ss="7.8 g (100 g)", sq=100),
    # P4 a family pack in the name (450 g) is never used: left as it is
    s6_row(ean("480", 7104), name="Corned Beef 450g", brand="Beefy", ctry=PH, scans=2, kcal=120, P=10, C=2, F=8),
    # P5 a liver spread at 176 is right (liver spreads are about 175): its 50 g serving is not used
    s6_row(ean("480", 7105), name="Liver Spread", brand="Spready", ctry=PH, scans=2, kcal=176, P=10, C=10, F=10,
           ss="50 g", sq=50),
    # P6 a coffee mix at 200 with a 30 g sachet that does not explain it (667): powder_made_up, kept out
    s6_row("4800016022210", "30 g", name="Great Taste White Caramel", brand="Universal Robina", ctry=PH, scans=2,
           kcal=200, P=2, C=27, F=9),
    # P7 a ready-to-drink milk named like a powder brand: left as it is
    s6_row("4800361331500", name="bear brand sterilized 200ml", brand="Bear Brand", ctry=PH, scans=2,
           kcal=72, P=3.5, C=8, F=3, ss="200ml", sq=200),
    # P8 a seasoning mix at 0 kcal: zero_dry, kept out
    s6_row("4801958911006", name="Ginisa Mix", ctry=PH, scans=1, kcal=0, P=0, C=0, F=0),
    # P9 a lechon sauce at 30 with "2 tbsp (32g)": 93.75, the label's own words kept, "2 tbsp (32 g)"
    s6_row("4801668100271", name="All-around sarsa", brand="Mang Tomas", ctry=PH, scans=1, kcal=30, P=0, C=7, F=0,
           ss="2 tbsp (32g)", sq=32),
    # P10 Nova left KNOWN_WRONG_PH: re-based on its 30 g quantity, 140 -> 466.7, "1 pack (30 g)"
    s6_row("4800016666261", "30g", name="Nova Multigrain Snacks", brand="Jack n Jill", ctry=PH, scans=1,
           kcal=140, P=3, C=21, F=4.5),
    # P11 KNOWN_PER_PACK with the made-up glass: 73 per 100 g of the 200 g glass from a 33 g sachet -> 442.4
    s6_row("4800361403443", name="Adult plus bear brand", ctry=PH, scans=1, kcal=73, P=2.35, C=9.1, F=3,
           ss="200.0g", sq=200),
    # P12 a new KNOWN_WRONG_PH entry (Eden at 495): listed, kept out
    s6_row("7622210278432", "220ml", name="Eden", brand="Eden", ctry=PH, scans=1, kcal=495, P=5, C=5, F=50,
           ss="40g", sq=40),
    # G1..G4 right as they are (step 7 reader's list): unchanged
    s6_row("5449000214799", name="Coke Zero", brand="Coca-Cola", ctry=PH, scans=9, kcal=0, P=0, C=0, F=0,
           ss="1 can (330 ml)", sq=330),
    s6_row("4807770270017", name="lm beef 55g", brand="Lucky", ctry=PH, scans=8, kcal=436, P=10, C=60, F=17),
    s6_row("0748485100401", name="century tuna (p) 155g", brand="Century Pacific Food Inc", ctry=PH, scans=7,
           kcal=179, P=21, C=2, F=10, ss="56 g", sq=56),
    s6_row("4800016074899", name="Great Taste Chocolate Mocha", brand="Universal Robina Corporation", ctry=PH,
           scans=6, kcal=65, P=1.5, C=11, F=1.6, ss="1 bottle (200 ml)", sq=200),
    # second fix pass of step 7
    # W1 canned meat whose figures Open Food Facts worked out from the 56 g serving (74 kcal, 7 g protein, 2 g
    # carbs, 4 g fat): already per 100 g, left as it is (the first pass made it 236)
    s6_row("0748485800035", "260g", name="argentina corned beef 260g", brand="Argentina", ctry=PH, scans=2,
           kcal=132.142857142857, P=12.5, C=3.57142857142857, F=7.14285714285714, ss="56 g", sq=56),
    # W2 canned fish worked out from the 56 g serving (49 kcal, 5 g, 3 g, 2 g): left as it is (the first pass made it 156)
    s6_row("4800163443043", "155g", name="ligo sardines (g) 155g", brand="Ligo", ctry=PH, scans=2,
           kcal=87.5, P=8.92857142857143, C=5.35714285714286, F=3.57142857142857, ss="56g", sq=56),
    # W3 franks worked out from the 55 g serving (90 kcal, 1 g carbs, 7 g fat) with no protein figure: the
    # protein the importer works out from the calories is left out of the test, so it is left as it is
    s6_row("4800249044140", "500 g", name="Super Jumbo Chicken Franks", brand="CDO", ctry=PH, scans=2,
           kcal=164, C=1.82, F=12.7, fib=12.7, ss="55g", sq=55),
    # W4 canned meat typed per 56 g serving in whole label figures: still re-based, 110 -> 196.4, "1 serving (56 g)"
    s6_row(ean("480", 7117), "340g", name="Corned Beef Hot Peppers", brand="Tester", ctry=PH, scans=2,
           kcal=110, P=12, C=1, F=7, ss="56g", sq=56),
    # H1 Clara Ole sugar-free pancake syrup, worked out from its 30 g serving (25 kcal, 6 g carbs): left at 83.3
    s6_row("4806026200594", name="Pancke Syrup", brand="Clara Olé", ctry=PH, scans=1,
           kcal=83.333333333333, P=0, C=20, F=0, ss="30.0g", sq=30),
    # H2, H3 KNOWN_WRONG_PH and KNOWN_TWIN: 555 TUNA ADOBO 155G at 89 is kept out; its barcode finds the
    # other row of the same can (250)
    s6_row("0074848570001", "155 g", name="555 TUNA ADOBO 155G", brand="555", ctry=PH, scans=1,
           kcal=89, P=5.35, C=8.9, F=5.35, ss="155 g", sq=155),
    s6_row("0748485700014", "155 g", name="555 tuna adobo 155g", brand="555", ctry=PH, scans=1,
           kcal=250, P=25, C=3.57142857142857, F=14.2857142857143, ss="56 g", sq=56),
]
assert len(step7) == 23, len(step7)
write("off_step7.csv", step7, SERVING_HEADER)
