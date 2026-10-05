# Training Log

An installable workout tracker for iPhone. Log a session live with a timer, add sets and reps or cardio distance as you go, record heart rate and effort at the end, then watch consistency, strength progression and heart-rate trends build up.

**Live app:** https://jrjohnsonlloyd.github.io/training-log/

## Install on iPhone

1. Open the live URL in Safari.
2. Tap Share → **Add to Home Screen** → Add.
3. Launch it from the Home Screen. It opens full-screen and works offline after the first visit.

## Your data

Nothing leaves the phone. Workouts are stored in the browser's storage for this app. Use Settings (gear icon) → **Share backup** to save a JSON file to Google Drive or Files, and **Restore from file** to bring it back on a new phone. Back up now and then; deleting the app deletes its data.

## Apple Health import

`tools/apple_health_import.py` turns an Apple Health export into a file the app can restore. Nothing is uploaded anywhere; it runs on your computer with Python 3 and no extra packages.

1. On the iPhone: Health app → profile picture → **Export All Health Data**, then get the zip onto your computer (AirDrop, Files, Drive).
2. Optional but recommended: in the app, Settings → **Download backup**, so logged workouts can be matched.
3. Run:

   ```
   python3 tools/apple_health_import.py "export.zip" --backup training-log-backup-2026-10-05.json --since 2026-01-01 --units lb mi -o training-log-import.json
   ```

4. Get `training-log-import.json` onto the phone and use Settings → **Restore from file**.

What it does: workouts you logged in the app that overlap an Apple workout get their average and max heart rate filled in (and a start time, if the entry was logged by hand). Apple workouts you never logged become their own entries: runs, walks, rides and the like as cardio with distance and time, strength sessions as a timed session with no sets (Apple doesn't record sets), yoga and stretching as mobility. Ids are stable, so running it again never duplicates anything. `--since` keeps the file small; `--units` should match the app's settings.

## How it's built

A single static page (`index.html`) with no framework and no build step, a service worker (`sw.js`) for offline use, and a web app manifest. Deployed to GitHub Pages by the workflow in `.github/workflows/pages.yml` on every push to `main`.

Strength progression uses the Epley estimate: 1RM ≈ weight × (1 + reps ÷ 30). Streaks count consecutive weeks (Monday to Sunday) at or above the weekly target.

To ship a change: edit `index.html`, bump `CACHE` in `sw.js` so installed copies refresh, push to `main`.
