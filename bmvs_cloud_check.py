"""
BMVS watcher - CLOUD version (runs on GitHub Actions).  (version 4)

NEW IN VERSION 4
  - Uses your calibrated clicks (New Family booking -> Next -> search).
  - Searches each place in WATCH_CENTRES one by one and logs every result.
  - HAP_ID and DOB secrets are no longer needed (your flow doesn't use them).
  - One screenshot per centre, e.g. state/last_view_Darwin.png

Number of checks per run is set in bmvs.yml (CHECKS_PER_RUN, GAP_SECONDS).
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

# ---- Read from GitHub Secrets (never type real values here) ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---- Read from bmvs.yml -----------------------------------------------------
CHECKS_PER_RUN = int(os.environ.get("CHECKS_PER_RUN", "1"))
GAP_SECONDS = max(int(os.environ.get("GAP_SECONDS", "75")), 60)  # floor: 1/min
MODE = os.environ.get("MODE", "") or "normal"   # normal | telegram_only | full_test

# ---- Settings ---------------------------------------------------------------
# Each name is typed into the search box, then that name's row is read.
# Use the name as it appears on the results page.
WATCH_CENTRES = ["Darwin", "Alice Springs", "Brisbasne", "Canberra"]          # e.g. ["Darwin", "Brisbane"] for testing

START_URL = "https://bmvs.onlineappointmentscheduling.net.au/oasis/"
NO_SLOT_TEXT = "no available slot"
SEARCH_BOX = "City, town, suburb or"
STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, "bmvs_last_state.txt")
HEARTBEAT_FILE = os.path.join(STATE_DIR, "last_heartbeat.txt")
DARWIN_TZ = timezone(timedelta(hours=9, minutes=30))
HEARTBEAT_HOUR = 8


def log(msg):
    print(f"{datetime.now(DARWIN_TZ):%Y-%m-%d %H:%M:%S} Darwin  {msg}", flush=True)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20
        )
        log(f"telegram {'sent' if r.status_code == 200 else 'FAILED ' + r.text[:150]}")
        return r.status_code == 200
    except Exception as exc:
        log(f"telegram error: {exc}")
        return False


# ----------------------------------------------------------------------------
# YOUR CALIBRATED CLICKS, split into two parts
# ----------------------------------------------------------------------------

def open_search_page(page):
    """Your first two calibrated clicks: get to the page with the search box."""
    page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
    page.get_by_role("button", name="New Family booking").click()
    page.get_by_role("button", name="Next").click()
    page.get_by_role("textbox", name=SEARCH_BOX).wait_for(timeout=30000)


def search_centre(page, centre):
    """Your last three calibrated clicks, with the place name filled in."""
    box = page.get_by_role("textbox", name=SEARCH_BOX)
    box.click()
    box.fill(centre)
    page.get_by_role("button", name="Search").click()
    page.wait_for_load_state("networkidle", timeout=30000)
    # Give the results list time to show this centre (not fatal if it doesn't)
    try:
        page.get_by_text(re.compile(re.escape(centre), re.I)).first.wait_for(timeout=15000)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# READING THE RESULT
# ----------------------------------------------------------------------------

def read_one(page, centre):
    """Return 'No available slot', a date, or 'UNREADABLE' for one centre."""
    body = page.inner_text("body")
    pattern = re.compile(
        rf"{re.escape(centre)}\b(.{{0,600}}?)"
        rf"(No available slot|\d{{1,2}}[\s/-]\w+[\s/-]\d{{2,4}})",
        re.I | re.S,
    )
    m = pattern.search(body)
    return m.group(2).strip() if m else "UNREADABLE"


def screenshot(page, centre):
    safe = re.sub(r"[^A-Za-z0-9]+", "_", centre)
    try:
        page.screenshot(path=os.path.join(STATE_DIR, f"last_view_{safe}.png"), full_page=True)
    except Exception:
        pass


def check_all_centres(browser):
    """
    One round: open the site once, then search each centre in turn.
    If a search fails, reopen the site and try that centre once more.
    Returns e.g. {'Darwin': 'No available slot', 'Brisbane': '14 Oct 2026'}
    """
    results = {}
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    page = context.new_page()
    try:
        open_search_page(page)
        for centre in WATCH_CENTRES:
            for attempt in (1, 2):
                try:
                    if attempt == 2:
                        open_search_page(page)
                    search_centre(page, centre)
                    results[centre] = read_one(page, centre)
                    break
                except Exception as exc:
                    log(f"  {centre}: attempt {attempt} failed: {type(exc).__name__}: {str(exc)[:120]}")
                    results[centre] = "UNREADABLE"
            screenshot(page, centre)
            log(f"  {centre:<15} -> {results[centre]}")
    finally:
        context.close()
    return results


# ----------------------------------------------------------------------------
# STATE AND HEARTBEAT
# ----------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, encoding="utf-8") as fh:
        return dict(l.rstrip("\n").split("|", 1) for l in fh if "|" in l)


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        for k, v in state.items():
            fh.write(f"{k}|{v}\n")


def maybe_send_heartbeat(latest):
    """Once a day, after 8am Darwin time, text a 'still alive' message."""
    now = datetime.now(DARWIN_TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour < HEARTBEAT_HOUR:
        return
    last = ""
    if os.path.exists(HEARTBEAT_FILE):
        with open(HEARTBEAT_FILE, encoding="utf-8") as fh:
            last = fh.read().strip()
    if last == today:
        return
    if send_telegram(f"Daily check-in: cloud watcher is running.\nLatest read: {latest}"):
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as fh:
            fh.write(today)


def format_results(results):
    return "\n".join(f"{c}: {v}" for c, v in results.items())


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main():
    if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
        log("Missing secrets. Add TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in GitHub.")
        sys.exit(1)

    os.makedirs(STATE_DIR, exist_ok=True)

    # ---- Test 1: Telegram only ----
    if MODE == "telegram_only":
        ok = send_telegram("TEST 1 passed: GitHub can reach your phone.")
        sys.exit(0 if ok else 1)

    # ---- Test 2: one real round, text the results whatever they are ----
    if MODE == "full_test":
        log(f"full test: {WATCH_CENTRES}")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                current = check_all_centres(browser)
            except Exception as exc:
                send_telegram(f"TEST 2 FAILED: {type(exc).__name__}\n{str(exc)[:200]}")
                raise
            finally:
                browser.close()
        bad = [c for c, v in current.items() if v == "UNREADABLE"]
        send_telegram(
            f"TEST 2 {'FAILED - unreadable: ' + ', '.join(bad) if bad else 'passed'}\n\n"
            f"{format_results(current)}"
        )
        sys.exit(1 if bad else 0)

    # ---- Normal scheduled run ----
    previous = load_state()
    log(f"this run: {CHECKS_PER_RUN} round(s), {GAP_SECONDS}s apart, centres: {WATCH_CENTRES}")
    failed_rounds = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for i in range(1, CHECKS_PER_RUN + 1):
                log(f"round {i}/{CHECKS_PER_RUN}")
                try:
                    current = check_all_centres(browser)

                    if all(v == "UNREADABLE" for v in current.values()):
                        failed_rounds += 1

                    for centre, value in current.items():
                        opened = NO_SLOT_TEXT not in value.lower() and value != "UNREADABLE"
                        if opened and value != previous.get(centre, ""):
                            send_telegram(
                                f"SLOT OPEN - {centre}\n"
                                f"First available: {value}\n\n"
                                f"Book now:\n{START_URL}"
                            )

                        # Warn once when a centre first becomes unreadable
                        if value == "UNREADABLE" and previous.get(centre) != "UNREADABLE":
                            send_telegram(
                                f"Cloud watcher: could not read {centre}. "
                                f"Check the Actions log / screenshot."
                            )

                    previous.update(current)
                    save_state(previous)

                except Exception as exc:
                    failed_rounds += 1
                    log(f"round {i} failed: {type(exc).__name__}: {exc}")

                if i < CHECKS_PER_RUN:
                    time.sleep(GAP_SECONDS)
        finally:
            browser.close()

    if failed_rounds < CHECKS_PER_RUN:
        maybe_send_heartbeat(format_results(previous))

    if failed_rounds == CHECKS_PER_RUN:
        log("ALL rounds failed this run - marking run as failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
