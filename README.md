# Training Log

An installable workout tracker for iPhone. Log a session live with a timer, add sets and reps or cardio distance as you go, record heart rate and effort at the end, then watch consistency, strength progression and heart-rate trends build up.

**Live app:** https://jrjohnsonlloyd.github.io/training-log/

## Install on iPhone

1. Open the live URL in Safari.
2. Tap Share → **Add to Home Screen** → Add.
3. Launch it from the Home Screen. It opens full-screen and works offline after the first visit.

## Your data

Nothing leaves the phone. Workouts are stored in the browser's storage for this app. Use Settings (gear icon) → **Share backup** to save a JSON file to Google Drive or Files, and **Restore from file** to bring it back on a new phone. Back up now and then; deleting the app deletes its data.

## How it's built

A single static page (`index.html`) with no framework and no build step, a service worker (`sw.js`) for offline use, and a web app manifest. Deployed to GitHub Pages by the workflow in `.github/workflows/pages.yml` on every push to `main`.

Strength progression uses the Epley estimate: 1RM ≈ weight × (1 + reps ÷ 30). Streaks count consecutive weeks (Monday to Sunday) at or above the weekly target.

To ship a change: edit `index.html`, bump `CACHE` in `sw.js` so installed copies refresh, push to `main`.
