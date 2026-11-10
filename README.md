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

- **Add food** by typing the numbers, or type a name and tap **Find food**. The search covers your favourites and the bundled library of Filipino dishes and Philippine restaurant items (`food-library.json`, approximate values with sources). A barcode number typed from a pack is looked up in Open Food Facts; a text search of that database is offered too, but it is slow.
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

To ship a change: edit `index.html`, bump `CACHE` in `sw.js` so installed copies refresh, push to `main`.
