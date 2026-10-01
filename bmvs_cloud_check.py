"""
BMVS watcher - CLOUD version (runs on GitHub Actions).  (version 2)

NEW IN VERSION 2: several checks per run
  GitHub can't start a scheduled run more often than every 5 minutes.
  So each run now does a few checks, with a short wait between them, then
  exits. Example: 3 checks, 75 seconds apart = a check about every 1.5-2 min.

  You change the numbers in bmvs.yml (CHECKS_PER_RUN and GAP_SECONDS),
  not in this file.

Your private details are NOT typed in this file. They come from GitHub
Secrets (see cloud_setup_guide.md, Step 3).
"""

import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

# ---- Read from GitHub Secrets (never type real values here) ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HAP_ID = os.environ.get("HAP_ID", "")
DOB = os.environ.get("DOB", "")

# ---- Read from bmvs.yml -----------------------------------------------------
CHECKS_PER_RUN = int(os.environ.get("CHECKS_PER_RUN", "1"))
GAP_SECONDS = int(os.environ.get("GAP_SECONDS", "75"))
GAP_SECONDS = max(GAP_SECONDS, 60)      # safety floor: never faster than 1/min

# ---- Settings ---------------------------------------------------------------
WATCH_CENTRES = ["Darwin"]
START_URL = "https://bmvs.onlineappointmentscheduling.net.au/oasis/"
NO_SLOT_TEXT = "no available slot"
STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, "bmvs_last_state.txt")


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC  {msg}", flush=True)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20
        )
        log(f"telegram {'sent' if r.status_code == 200 else 'FAILED ' + r.text[:150]}")
    except Exception as exc:
        log(f"telegram error: {exc}")


def navigate_to_location(page):
    """
    PASTE YOUR CALIBRATED LINES HERE.

    Copy the block between CALIBRATE START and END from bmvs_watch_v3.py
    once it works on your PC. Same lines, same 4-space indent.
    """
    page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)

    # ---------- CALIBRATE: START ----------
    page.get_by_role("button", name="New Family booking").click()
    page.get_by_role("button", name="Next").click()
    page.get_by_role("textbox", name="City, town, suburb or").click()
    page.get_by_role("textbox", name="City, town, suburb or").fill("Darwin")
    page.get_by_role("button", name="Search").click()
    # ---------- CALIBRATE: END ----------

    page.wait_for_url(re.compile("Location", re.I), timeout=30000)
    page.wait_for_load_state("networkidle")


def read_availability(page):
    results = {}
    body = page.inner_text("body")
    for centre in WATCH_CENTRES:
        pattern = re.compile(
            rf"{re.escape(centre)}\b(.{{0,600}}?)"
            rf"(No available slot|\d{{1,2}}[\s/-]\w+[\s/-]\d{{2,4}})",
            re.I | re.S,
        )
        m = pattern.search(body)
        results[centre] = m.group(2).strip() if m else "UNREADABLE"
    return results


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


def check_once(browser):
    """One full check with a fresh session. Returns {'Darwin': '...'}."""
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    page = context.new_page()
    try:
        navigate_to_location(page)
        return read_availability(page)
    finally:
        try:
            page.screenshot(path=os.path.join(STATE_DIR, "last_view.png"), full_page=True)
        except Exception:
            pass
        context.close()


def main():
    if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, HAP_ID, DOB]):
        log("Missing secrets. Add TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, HAP_ID, DOB in GitHub.")
        sys.exit(1)

    # Manual "test" run from the Actions tab: just prove Telegram works
    if os.environ.get("TEST_MODE") == "true":
        send_telegram("Cloud watcher test - GitHub can reach your phone.")
        return

    os.makedirs(STATE_DIR, exist_ok=True)
    previous = load_state()
    log(f"this run: {CHECKS_PER_RUN} check(s), {GAP_SECONDS}s apart")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for i in range(1, CHECKS_PER_RUN + 1):
                try:
                    current = check_once(browser)
                    log(f"check {i}/{CHECKS_PER_RUN}  read: {current}")

                    for centre, value in current.items():
                        opened = NO_SLOT_TEXT not in value.lower() and value != "UNREADABLE"
                        if opened and value != previous.get(centre, ""):
                            send_telegram(
                                f"SLOT OPEN - {centre}\n"
                                f"First available: {value}\n\n"
                                f"Book now:\n{START_URL}"
                            )

                    # Warn once if the page becomes unreadable
                    now_bad = any(v == "UNREADABLE" for v in current.values())
                    was_bad = any(v == "UNREADABLE" for v in previous.values())
                    if now_bad and not was_bad:
                        send_telegram("Cloud watcher: page could not be read. Check the Actions log.")

                    previous = current
                    save_state(previous)

                except Exception as exc:
                    log(f"check {i} failed: {type(exc).__name__}: {exc}")

                if i < CHECKS_PER_RUN:
                    time.sleep(GAP_SECONDS)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
