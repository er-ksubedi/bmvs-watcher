"""
BMVS watcher - CLOUD version (runs on GitHub Actions).

Different from the PC version in one way: it does ONE check and exits.
GitHub starts it again every ~5 minutes, so there is no loop here.

Your private details are NOT typed in this file. They come from
GitHub "Secrets" (see cloud_setup_guide.md, Step 3), so the file is
safe to sit in a public repository.
"""

import os
import re
import sys
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

# ---- Read from GitHub Secrets (never type real values here) ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HAP_ID = os.environ.get("HAP_ID", "")
DOB = os.environ.get("DOB", "")

# ---- Settings ---------------------------------------------------------------
WATCH_CENTRES = ["Darwin"]
START_URL = "https://bmvs.onlineappointmentscheduling.net.au/oasis/"
NO_SLOT_TEXT = "no available slot"
STATE_FILE = "state/bmvs_last_state.txt"   # kept between runs by GitHub cache


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC  {msg}", flush=True)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
    log(f"telegram {'sent' if r.status_code == 200 else 'FAILED ' + r.text[:150]}")


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
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        for k, v in state.items():
            fh.write(f"{k}|{v}\n")


def main():
    if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, HAP_ID, DOB]):
        log("Missing secrets. Add TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, HAP_ID, DOB in GitHub.")
        sys.exit(1)

    # Manual "test" run from the Actions tab: just prove Telegram works
    if os.environ.get("TEST_MODE") == "true":
        send_telegram("Cloud watcher test - GitHub can reach your phone.")
        return

    os.makedirs("state", exist_ok=True)
    previous = load_state()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        try:
            navigate_to_location(page)
            current = read_availability(page)
        finally:
            page.screenshot(path="state/last_view.png", full_page=True)
            browser.close()

    log(f"read: {current}")

    for centre, value in current.items():
        opened = NO_SLOT_TEXT not in value.lower() and value != "UNREADABLE"
        if opened and value != previous.get(centre, ""):
            send_telegram(
                f"SLOT OPEN - {centre}\nFirst available: {value}\n\nBook now:\n{START_URL}"
            )

    # Warn once if the page becomes unreadable (layout change / blocked)
    if any(v == "UNREADABLE" for v in current.values()) and \
       not any(v == "UNREADABLE" for v in previous.values()):
        send_telegram("Cloud watcher: page could not be read. Check the Actions log.")

    save_state(current)


if __name__ == "__main__":
    main()
