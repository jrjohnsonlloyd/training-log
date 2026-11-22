#!/usr/bin/env python3
"""Headless UI smoke tests for Training Log (standard library only).

    python tests/run_ui_smoke.py                      run the "baseline" suite against the repository's food-db.json
    python tests/run_ui_smoke.py --suite v2           run the food-database suite against a version 2 bundle
    python tests/run_ui_smoke.py --suite NAME         run another suite from tests/ui_smoke.html
    python tests/run_ui_smoke.py --db-file PATH       serve PATH as /food-db.json instead of the repo's file

The "v2" suite needs a version 2 bundle as /food-db.json. --bundle says where it comes from:
    auto    (default) build it from tests/fixtures with scripts/import_foods.py when the importer and the three sample
            fixtures exist and the build works; otherwise serve the hand-written tests/fixtures/food-db.v2.sample.json
    built   build it, and stop if that is not possible
    sample  always serve the hand-written sample
The line starting "bundle:" says which one was served. --bundle sample or built also works with other suites;
--db-file wins over --bundle.

What it does: serves the repository root on 127.0.0.1 (ephemeral port), opens tests/ui_smoke.html in
headless Chrome, and reads back the JSON that the page writes after the marker word RESULT. The page
loads the real app in an iframe and drives it; nothing in the app is changed or mocked.

Chrome is taken from the CHROME environment variable, else from the usual install paths. It is started
with DNS switched off for every host, so the suite cannot reach the internet even by accident.

Exit code: 0 = the page finished, at least 10 checks ran and none failed; 1 = a failure; 2 = cannot run
(Chrome or the --db-file is missing).
"""
import argparse
import html
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
PAGE = "tests/ui_smoke.html"
MARKER = "RESULT "
MIN_CHECKS = 10
CHROME_PATHS = [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google/Chrome/Application/chrome.exe"),
]
CHROME_NAMES = ["chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
V2_SUITES = {"v2"}  # suites that need a version 2 bundle served as /food-db.json
V2_BUDGET = 60000   # they do more, so they get a longer virtual time budget unless --budget is given
RANKING_BUDGET = 400000   # the ranking suite types several hundred queries against a real bundle
SAMPLE_BUNDLE = "tests/fixtures/food-db.v2.sample.json"
IMPORTER = "scripts/import_foods.py"
BUILD_INPUTS = [("--curated-json", "tests/fixtures/local_ph_curated.json"), ("--usda-path", "tests/fixtures/sample_usda.csv"),
                ("--off-path", "tests/fixtures/sample_off.csv")]


def build_bundle(workdir):
    """Builds a version 2 bundle from the sample fixtures into workdir. Returns (path, None) or (None, why)."""
    missing = [rel for rel in [IMPORTER] + [rel for _, rel in BUILD_INPUTS] if not (ROOT / rel).is_file()]
    if missing:
        return None, "missing " + ", ".join(missing)
    out = os.path.join(workdir, "food-db.built.json")
    cmd = [sys.executable, IMPORTER, "--db", os.path.join(workdir, "foods.sqlite")]
    for flag, rel in BUILD_INPUTS:
        cmd += [flag, rel]
    cmd += ["--export-app", out]
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "%s did not run: %s" % (IMPORTER, e)
    if p.returncode != 0:
        tail = one_line(p.stderr.decode("utf-8", "replace").strip()[-300:])
        return None, "%s exited with code %d%s" % (IMPORTER, p.returncode, ": " + tail if tail else "")
    try:
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, "%s wrote no readable bundle: %s" % (IMPORTER, e)
    if not (isinstance(data, dict) and data.get("version") == 2 and isinstance(data.get("fields"), list)
            and isinstance(data.get("items"), list) and data["items"]):
        return None, "%s wrote a file that is not a version 2 bundle with rows" % IMPORTER
    return out, None


def pick_bundle(mode, workdir):
    """Returns (path, description, None) or (None, None, why it cannot be served)."""
    note = ""
    if mode in ("auto", "built"):
        path, why = build_bundle(workdir)
        if path:
            return path, "built from the sample fixtures with %s" % IMPORTER, None
        if mode == "built":
            return None, None, "--bundle built: " + why
        note = " (not built: %s)" % why
    sample = ROOT / SAMPLE_BUNDLE
    if not sample.is_file():
        return None, None, "%s does not exist%s" % (SAMPLE_BUNDLE, note)
    return str(sample), "hand-written sample %s%s" % (SAMPLE_BUNDLE, note), None


def find_chrome():
    """Returns (path, None) or (None, why)."""
    env = os.environ.get("CHROME")
    if env:
        hit = env if os.path.isfile(env) else shutil.which(env)
        return (hit, None) if hit else (None, "the CHROME environment variable points to %r, which does not exist" % env)
    for p in CHROME_PATHS:
        if os.path.isfile(p):
            return p, None
    for name in CHROME_NAMES:
        hit = shutil.which(name)
        if hit:
            return hit, None
    return None, "Google Chrome was not found in the usual places; set the CHROME environment variable to chrome.exe"


class QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # Chrome closing a connection early is not worth a traceback
        pass


class Handler(http.server.SimpleHTTPRequestHandler):
    db_file = None      # when set, GET /food-db.json returns this file
    request_log = None  # list of "status path" strings

    # Fixed types, so the Windows registry cannot turn .js into text/plain (the service worker needs a script type).
    extensions_map = dict(http.server.SimpleHTTPRequestHandler.extensions_map)
    extensions_map.update({
        ".html": "text/html", ".js": "text/javascript", ".json": "application/json", ".css": "text/css",
        ".webmanifest": "application/manifest+json", ".png": "image/png", ".svg": "image/svg+xml",
    })

    def send_head(self):
        if self.db_file and urlsplit(self.path).path == "/food-db.json":
            try:
                f = open(self.db_file, "rb")
            except OSError:
                self.send_error(404, "the --db-file could not be read")
                return None
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.end_headers()
            return f
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_request(self, code="-", size="-"):
        if self.request_log is not None:
            self.request_log.append("%s %s" % (getattr(code, "value", code), self.path))

    def log_message(self, format, *args):  # stay quiet
        pass


def start_server(db_file):
    log = []
    handler = type("SmokeHandler", (Handler,), {"db_file": db_file, "request_log": log})
    server = QuietServer(("127.0.0.1", 0), partial(handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, name="ui-smoke-http", daemon=True).start()
    return server, log


def run_chrome(chrome, url, budget_ms, timeout_s):
    """Returns (exit code, dumped DOM, stderr). Raises subprocess.TimeoutExpired."""
    profile = tempfile.mkdtemp(prefix="tl-ui-smoke-")
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-extensions",
        "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        "--user-data-dir=" + profile, "--window-size=390,900",
        "--virtual-time-budget=%d" % budget_ms, "--dump-dom", url,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    finally:
        for _ in range(5):  # Chrome can hold the folder for a moment after it exits
            shutil.rmtree(profile, ignore_errors=True)
            if not os.path.exists(profile):
                break
            time.sleep(0.4)


def extract_result(dom):
    """The JSON written by the page into its result element, or None."""
    m = re.search(r'<pre id="result"[^>]*>(.*?)</pre>', dom, re.S)
    if not m:
        return None
    text = html.unescape(m.group(1))
    i = text.find(MARKER)
    if i < 0:
        return None
    try:
        data = json.loads(text[i + len(MARKER):])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def one_line(s):
    return " ".join(str(s or "").split())


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # details can hold characters such as a middle dot; never crash on them
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Run the Training Log UI smoke tests in headless Chrome.")
    ap.add_argument("--suite", default="baseline", help="suite name in tests/ui_smoke.html (default: baseline)")
    ap.add_argument("--db-file", help="serve this file as /food-db.json instead of the repository's own")
    ap.add_argument("--bundle", choices=["auto", "built", "sample"], default="auto",
                    help="version 2 bundle for the v2 suite: built from the fixtures by scripts/import_foods.py, the hand-written "
                         "sample, or auto = built when possible, else the sample (default: auto)")
    ap.add_argument("--budget", type=int, help="virtual time budget for the page in ms (default: 30000; %d for the v2 suite)" % V2_BUDGET)
    ap.add_argument("--timeout", type=int, default=180, help="real seconds to wait for Chrome (default: 180)")
    ap.add_argument("-v", "--verbose", action="store_true", help="also print details of passing checks and the requests served")
    args = ap.parse_args(argv)
    needs_v2 = args.suite in V2_SUITES
    budget = args.budget if args.budget else (V2_BUDGET if needs_v2 else RANKING_BUDGET if args.suite == "ranking" else 30000)
    if args.suite == "ranking" and args.timeout == 180:
        args.timeout = 400

    db_file = None
    if args.db_file:
        db_file = os.path.abspath(args.db_file)
        if not os.path.isfile(db_file):
            print("Cannot run: --db-file %s does not exist." % db_file)
            return 2
    chrome, why = find_chrome()
    if not chrome:
        print("Cannot run: %s." % why)
        return 2

    workdir = None
    try:
        if db_file:
            if needs_v2 or args.bundle != "auto":
                print("bundle: --db-file %s" % db_file)
        elif needs_v2 or args.bundle != "auto":
            workdir = tempfile.mkdtemp(prefix="tl-ui-bundle-")
            db_file, what, why = pick_bundle(args.bundle, workdir)
            if not db_file:
                print("Cannot run: %s." % why)
                return 2
            print("bundle: %s" % what)

        server, log = start_server(db_file)
        url = "http://127.0.0.1:%d/%s?suite=%s" % (server.server_address[1], PAGE, quote(args.suite, safe=""))
        try:
            code, dom, err = run_chrome(chrome, url, budget, args.timeout)
        except subprocess.TimeoutExpired:
            print("FAIL Chrome did not finish within %d seconds." % args.timeout)
            return 1
        finally:
            server.shutdown()
            server.server_close()
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    if args.verbose:
        for line in log:
            print("  served " + line)
    result = extract_result(dom)
    if result is None:
        print("FAIL no %smarker in the page that Chrome printed (Chrome exit code %s)." % (MARKER, code))
        bad = [line for line in log if not line.startswith(("200 ", "304 "))]
        if not log:
            print("The local server received no requests at all.")
        elif bad:
            print("Requests that did not answer 200: " + "; ".join(bad[:10]))
        if err.strip():
            print("--- end of Chrome's error output ---")
            print(err.strip()[-1500:])
        print("--- start of the dumped page ---")
        print(dom[:2000] if dom.strip() else "(empty)")
        return 1

    checks = [c for c in (result.get("checks") or []) if isinstance(c, dict)]
    # A suite that cannot say anything about the file being served (the ranking suite on a small
    # bundle) reports why and counts as a clean skip, not a failure.
    if result.get("skipped") and result.get("done") and not checks:
        print("SKIPPED %s suite: %s" % (one_line(result.get("suite") or args.suite), one_line(result.get("skipped"))))
        return 0
    passed = failed = expected = 0
    for c in checks:
        name, detail = one_line(c.get("name") or "?"), one_line(c.get("detail"))
        if c.get("ok"):
            passed += 1
            print("PASS " + name + (": " + detail if args.verbose and detail else ""))
        elif c.get("xfail"):
            expected += 1
            print("XFAIL " + name + (": " + detail if detail else ""))
        else:
            failed += 1
            print("FAIL " + name + (": " + detail if detail else ""))
    problems = []
    if not result.get("done"):
        problems.append("the page did not finish (it used up the %d ms time budget or stopped)" % budget)
    if len(checks) < MIN_CHECKS:
        problems.append("only %d checks ran, at least %d are required" % (len(checks), MIN_CHECKS))
    for p in problems:
        print("FAIL " + p)
    ok = not failed and not problems
    print("%s suite: %d checks, %d passed, %d failed%s -> %s" % (
        one_line(result.get("suite") or args.suite), len(checks), passed, failed,
        ", %d expected failures" % expected if expected else "", "OK" if ok else "NOT OK"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
