"""Writes the Open Food Facts test fixtures (tab-separated, like the real export, with a gzip-free layout).

    python tests/fixtures/make_off_fixtures.py [OUTPUT_FOLDER]

Four files are written next to this script:

    sample_off.csv          the main import: 52 rows (32 old anchor rows + 20 selection rows)
    off_rules.csv           36 rows, one per validation / repair rule branch
    off_dedupe.csv          15 rows in groups that must (or must not) merge by name + brand
    off_alias_guard.csv     1 row whose barcode is also an alias stored on another row

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


def write(name, rows):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", quoting=csv.QUOTE_NONE, quotechar=None, lineterminator="\n")
        writer.writerow(HEADER)
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
