# Training Log

An installable workout tracker for iPhone. Log a session live with a timer, add sets and reps or cardio distance as you go, record heart rate and effort at the end, then watch consistency, strength progression and heart-rate trends build up.

**Live app:** https://jrjohnsonlloyd.github.io/training-log/

## Install on iPhone

1. Open the live URL in Safari.
2. Tap Share → **Add to Home Screen** → Add.
3. Launch it from the Home Screen. It opens full-screen and works offline after the first visit.

## Your data

By default nothing leaves the phone. Workouts are stored in the browser's storage for this app. Use Settings (gear icon) → **Share backup** to save a JSON file to Google Drive or Files, and **Restore from file** to bring it back on a new phone. Back up now and then; deleting the app deletes its data.

## Cloud sync (optional)

Settings → **Cloud sync** copies the same backup file to a private GitHub repository every time a workout or setting is saved, so the data can be read away from the phone. It is one way: the phone is the source of truth and the file `training-log.json` in the repository is overwritten with each sync. If the phone is offline, the sync waits and runs when it is back online or the app is next opened.

1. Create a private repository to hold the data (for example `training-data`).
2. On GitHub: Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token. Repository access: **Only select repositories** → the data repository. Permissions → Repository permissions → **Contents: Read and write**. Nothing else.
3. In the app: Settings → Cloud sync → enter the repository as `owner/name`, paste the token, tap **Save and sync now**.

The token is stored only in this app's storage on the phone. It is never included in the backup file or in what is synced. **Turn off** removes it from the phone.

## Food log

The **Food** tab tracks calories and macros against a daily target (Settings → Daily calorie target; 1,900 kcal by default).

- **Add food** by typing the numbers, or start typing a name: matches appear under the box as you type. Each row shows a small pack photo (or an icon for the kind of food), the name, the brand or group, the serving, and the calories on the right. Tap a row to fill the form (then pick a **Serving size** such as 1 cup, 1 piece, 100 g or 1 g and set Servings), or tap **Add** to log one serving straight away.
- **Filters** above the results: **All**, **Supermarket & Imported** (packaged products), **Dining Out** (restaurant and fast-food items) and **Whole Foods** (plain foods and home dishes).
- **What is searched, all saved in the app and usable offline:** your favourites and recent foods first; the curated lists (`data/local_ph_curated.json` and the older `food-library.json`: Filipino dishes and Philippine restaurant items, approximate values with sources); and about 12,600 everyday foods and dishes from the USDA's public food tables. They are bundled as `food-db.json`.
- **Brands and barcodes:** a barcode number typed from a pack is matched against the bundled list first, with no network needed (12 or 13 digits alike, and the short 8-digit code of a small US can finds the same product as its full 12-digit code). **Find food** (or Enter) also looks up packaged products in Open Food Facts (worldwide, volunteer-entered, with pack photos) and US label data in USDA FoodData Central when the app itself has fewer than three matches; **Search brands online** does it on request. The USDA lookup works a few times an hour without a key; a free key from https://fdc.nal.usda.gov/api-key-signup pasted into Settings → *USDA food search key* removes that limit. The key stays on the phone and is not part of the backup or the sync.
- **More nutrition:** sugars, saturates, fibre and sodium are filled in when the food has them, saved with the entry, and totalled for the day. A toggle (and Settings → *Show as*) switches between **Sodium (mg)**, as on Philippine and US labels, and **Salt (g)**, as on UK labels. Sodium in mg is what is stored; salt g = sodium mg ÷ 400.
- **Recent**: the last foods you logged, one tap to log the same amount again.
- **Favourites**: tap *Favourite* on any entry, then log it again with one tap.
- **Photo for Claude / Ask Claude to estimate**: for meals you cannot look up. The photo (shrunk to 1280 px) or the description goes to the private data repository and the entry shows as *Pending*. A scheduled Claude task reads pending entries about every 12 hours and writes its estimates to `food-estimates.json` in that repository; the app picks them up when it opens. Needs Cloud sync on. Estimates are rough: check them and use *Edit* to correct.
- Food entries and favourites are part of the backup file and the cloud sync (under `food`). Photos are stored only in the private data repository, never in this one.

## Apple Health import

`tools/apple_health_import.py` turns an Apple Health export into a file the app can restore. Nothing is uploaded anywhere; it runs on your computer with Python 3 and no extra packages.

1. On the iPhone: Health app → profile picture → **Export All Health Data**, then get the zip onto your computer (AirDrop, Files, Drive).
2. Optional but recommended: in the app, Settings → **Download backup**, so logged workouts can be matched.
3. Run:

   ```
   python3 tools/apple_health_import.py "export.zip" --backup training-log-backup-2026-10-05.json --since 2026-01-01 --units lb mi -o training-log-import.json
   ```

4. Get `training-log-import.json` onto the phone and use Settings → **Restore from file**.

The watch tends to split one gym visit into several short workouts (strength, then core, then functional) and auto-detects walks. By default the script stitches workouts that start within 20 minutes of the previous one ending into one session (`--merge-gap`), drops anything under 10 minutes (`--min-minutes`), and drops walk-only sessions under 30 minutes (`--walk-min`; use `--walk-min 9999` to leave walks out entirely). `--skip Walk 'Core training'` leaves named activities out. You can also re-run the script on a previous `training-log-import.json` to change these without re-reading the export.

What it does: workouts you logged in the app that overlap an Apple workout get their average and max heart rate filled in (and a start time, if the entry was logged by hand). Apple workouts you never logged become their own entries: runs, walks, rides and the like as cardio with distance and time, strength sessions as a timed session with no sets (Apple doesn't record sets), yoga and stretching as mobility. Ids are stable, so running it again never duplicates anything. `--since` keeps the file small; `--units` should match the app's settings.

## How it's built

A single static page (`index.html`) with no framework and no build step, a service worker (`sw.js`) for offline use, and a web app manifest. Deployed to GitHub Pages by the workflow in `.github/workflows/pages.yml` on every push to `main`.

Strength progression uses the Epley estimate: 1RM ≈ weight × (1 + reps ÷ 30). Streaks count consecutive weeks (Monday to Sunday) at or above the weekly target.

To ship a change: edit `index.html`, bump `CACHE` in `sw.js` so installed copies refresh, push to `main`. The bump is also required whenever `food-db.json` changes: that file is cached together with the app, so a new food list only reaches an installed phone when `CACHE` changes.

## Food data pipeline

`food-db.json` is built on a computer by `scripts/import_foods.py` (Python 3, no extra packages). It reads the sources line by line into a small SQLite database (`data/foods.sqlite`, not committed; its table and versioned migrations are in `scripts/food_schema.py`), removes duplicates, and writes the file the app loads.

```
python scripts/import_foods.py --db data/foods.sqlite \
  --curated-json data/local_ph_curated.json \
  --usda-path "<Survey Foods (FNDDS) CSV folder>" --usda-path "<SR Legacy CSV folder>" \
  --off-path "<Open Food Facts export, en.openfoodfacts.org.products.csv or .csv.gz>" \
  --export-app food-db.json --built 2026-11-22
```

- **Sources and priority:** 1 = the curated list, 2 = USDA FoodData Central (https://fdc.nal.usda.gov/download-datasets, public domain), 3 = Open Food Facts (https://world.openfoodfacts.org/data, Open Database Licence). When two rows share a barcode, or the same name and brand, the higher-priority row is kept; it inherits the other row's pack photo.
- **Open Food Facts rows** are kept only when tagged for the Philippines, the United States, the United Kingdom, Japan, South Korea, or one of seven neighbouring markets (Thailand, Malaysia, Singapore, Indonesia, Vietnam, Hong Kong, Taiwan), which share the one region code `AS`. A product tagged only for those seven goes to the Asia pool whatever its barcode, unless the barcode is Japanese or Korean (then it stays in that pool); it never replaces a Philippine, American, British, Japanese or Korean record of the same product, and a Philippine product is never marked `AS`. Every Philippine-tagged product is bundled; the others are limited to the most-scanned of each market (`--off-quota`, default `US=12000,UK=2500,JP=3500,KR=2500,AS=1500`) so the file stays small enough for a phone. A product from another market whose numbers look wrong (a label typed per serving, calories that do not fit the product's own protein, carbs and fat, a dry-coffee figure on coffee, a shampoo, or one of the few rows listed by barcode in `KNOWN_WRONG` after being read by hand) is kept in the database but not bundled; a believable record of the same product takes its place when there is one. British supermarket packs that are tagged United States wait for the United Kingdom pool. Anything left out is still found by the online lookup.
- **Japan, Korea and the neighbouring markets** have checks of their own, because their labels are often typed per pack, per piece or per bottle: crisps, biscuits, Pocky, KitKat, cereal and protein bars, breakfast cereals, peanut butter, candy and mints, hard cheese, miso and gochujang, seaweed snacks, nori sheets and dried seaweed, plain chocolate and nuts with too few calories, instant noodles carrying their made-up (with water) figures, soft drinks, juices, milk coffees, soy, oat and plain milks and sports drinks with a can's or a bottle's calories, 3-in-1, latte and milk tea powders with one sachet's figures, and foods at 0 kcal (an oil at 0 kcal too) are kept out of the bundle (reasons `dense_low`, `drink_high`, `mix_low`, `zero_food`; the US per-serving test also runs on the neighbouring markets as `per_serving_as` and `per_bottle_as`, except on a serving of 80 to 125 g, coconut milk and ice pops). So are biscuits under other names (Bisco, Calorie Mate, rusks, brownies), bread and buns, Japanese sweets, oats and bran flakes, protein and amino-acid powders, plain eggs (also over 220 kcal), honey, mirin and cooking sake, Worcestershire-type sauces, dried squid and fish, natto and dry instant-noodle packs with too few calories, infant formula and supplements (`not_food`), and Western supermarket brands or short codes that only read as Korean (a Calvé peanut butter, a Lidl cheese) never enter these three pools. A crisp, biscuit, chocolate, nut, bar, cereal or peanut butter pack, or one of the kinds just listed, whose stated serving explains its too-few calories is re-based on that serving instead (the numbers become per 100 g, marked *approximate*, and the serving opens first). In the bundle only, these three pools get Latin brand names for common Japanese, Korean and Thai brands (`株式会社 神戸物産` is written Kobe Bussan, `7-11` is 7-Eleven and is also found by typing "seven eleven"), names without a shop's item code, the pack size or a brand repeated at the end (`Udon - Nissin - Nissin` becomes `Udon - Nissin`), serving labels in English (`1袋(80g)` becomes `1 pack (80 g)`), and instant noodles, drink sachets, yogurt cups, burgers, onigiri, ice creams, instant soups, stock cubes and other single packs or spoon foods open on their own serving, while a sauce bottle or a litre of milk given as the "serving" opens on 100 g. A Japanese brand in the table is only read when it stands by itself (`明治` is Meiji, `明治屋` is not; `日清食品` is Nissin, `日清フーズ` is Nisshin Foods). Rows with no brand named like a curated or USDA food ("Milk", "Mango") and shop listings saved as names are left out, a name mostly in Japanese script that nobody has scanned comes after every other candidate, and in the Asia pool a product that is only there for its barcode (not tagged for any of the seven markets) counts for half its scans. An 8-digit code that only looks Japanese (45... or 49..., common on American packs) counts as Japanese only when the product is tagged Japan. The shipped `food-db.json` was exported with `--off-quota "US=11000,UK=0,JP=3000,KR=2000,AS=2400"` (36,157 rows, about 6.06 MB); the UK pool follows in a later step. The US and UK pools skip a repeat (same plain name and calories) only of a Philippine row or of a row sold in their own market, so a Korean "Soy sauce" does not take the place of an American one.
- **Search order:** a packaged product from another market (not tagged Philippines, no 480 barcode) loses one point, and two more when the words typed are an everyday food the reference tables know ("ketchup"), so Philippine products come first for everyday words while a brand search ("buldak", "pocky") still leads with that brand.
- **Clean-up rules:** values are per 100 g; kilojoules become calories (÷ 4.184) when calories are missing; salt becomes sodium (g × 400 = mg) when sodium is missing; rows with no name, no calories, or impossible numbers (more than 100 g of a nutrient per 100 g) are dropped. Baby foods and infant formulas are left out.
- **Curated list:** `data/local_ph_curated.json` is the list to grow. Copy an entry, fill it in, say whether the numbers are for the serving or for 100 g, mark `basis` honestly (`official`, `database` or `estimate`), then run the command above and commit the new `food-db.json`. A curated entry hides an older `food-library.json` entry when its name, or one of its `aka` names, is the same.

Tests (no downloads needed; they use the small made-up files in `tests/fixtures`):

```
python -m unittest discover -s tests -p "test_*.py"
python tests/run_ui_smoke.py
python tests/run_ui_smoke.py --db-file tests/fixtures/food-db.v1.sample.json
python tests/run_ui_smoke.py --suite v2
python tests/run_ui_smoke.py --suite ranking --db-file "<a real food-db.json with packaged products>"
```

The first checks the import rules and the database migration. The others drive the app itself in headless Chrome: the second and third prove that food data saved by the previous version still loads and is left untouched (with the current file and with an old-format one), the fourth checks filters, photos, barcode lookup and the sodium/salt toggle. The last checks the order of search results on a large real food list (plain foods before same-named products, curated entries first, restaurants by name, brands, plurals, accents, short US barcodes); it skips itself when the list has fewer than 20,000 rows.
