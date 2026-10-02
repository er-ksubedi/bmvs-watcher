"""
BMVS watcher - CLOUD version (runs on GitHub Actions).  (version 3)

NEW IN VERSION 3: ways to know it is working
  1. "full_test" button: does a real check and texts you what it read.
  2. Daily "still alive" Telegram message (once per day, ~8am Darwin time).
  3. If every check in a run fails, the run shows a RED X in GitHub
     (before, failures were hidden behind a green tick).

Number of checks per run is set in bmvs.yml (CHECKS_PER_RUN, GAP_SECONDS).
Private details come from GitHub Secrets, never typed in this file.
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
HAP_ID = os.environ.get("HAP_ID", "")
DOB = os.environ.get("DOB", "")

# ---- Read from bmvs.yml -----------------------------------------------------
CHECKS_PER_RUN = int(os.environ.get("CHECKS_PER_RUN", "1"))
GAP_SECONDS = max(int(os.environ.get("GAP_SECONDS", "75")), 60)  # floor: 1/min
MODE = os.environ.get("MODE", "") or "normal"   # normal | telegram_only | full_test

# ---- Settings ---------------------------------------------------------------
WATCH_CENTRES = ["Darwin"]
START_URL = "https://bmvs.onlineappointmentscheduling.net.au/oasis/"
NO_SLOT_TEXT = "no available slot"
STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, "bmvs_last_state.txt")
HEARTBEAT_FILE = os.path.join(STATE_DIR, "last_heartbeat.txt")
DARWIN_TZ = timezone(timedelta(hours=9, minutes=30))
HEARTBEAT_HOUR = 8          # send the daily "alive" message from 8am Darwin time


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

    os.makedirs(STATE_DIR, exist_ok=True)

    # ---- Test 1: Telegram only ----
    if MODE == "telegram_only":
        ok = send_telegram("TEST 1 passed: GitHub can reach your phone.")
        sys.exit(0 if ok else 1)

    # ---- Test 2: one real check, text the result whatever it is ----
    if MODE == "full_test":
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                current = check_once(browser)
            except Exception as exc:
                send_telegram(f"TEST 2 FAILED: {type(exc).__name__}\n{str(exc)[:200]}")
                raise
            finally:
                browser.close()
        log(f"read: {current}")
        bad = any(v == "UNREADABLE" for v in current.values())
        send_telegram(
            f"TEST 2 {'FAILED - page unreadable' if bad else 'passed'}\n"
            f"The site currently says: {current}"
        )
        sys.exit(1 if bad else 0)

    # ---- Normal scheduled run ----
    previous = load_state()
    log(f"this run: {CHECKS_PER_RUN} check(s), {GAP_SECONDS}s apart")
    failures = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for i in range(1, CHECKS_PER_RUN + 1):
                try:
                    current = check_once(browser)
                    log(f"check {i}/{CHECKS_PER_RUN}  read: {current}")

                    if any(v == "UNREADABLE" for v in current.values()):
                        failures += 1

                    for centre, value in current.items():
                        opened = NO_SLOT_TEXT not in value.lower() and value != "UNREADABLE"
                        if opened and value != previous.get(centre, ""):
                            send_telegram(
                                f"SLOT OPEN - {centre}\n"
                                f"First available: {value}\n\n"
                                f"Book now:\n{START_URL}"
                            )

                    now_bad = any(v == "UNREADABLE" for v in current.values())
                    was_bad = any(v == "UNREADABLE" for v in previous.values())
                    if now_bad and not was_bad:
                        send_telegram("Cloud watcher: page could not be read. Check the Actions log.")

                    previous = current
                    save_state(previous)

                except Exception as exc:
                    failures += 1
                    log(f"check {i} failed: {type(exc).__name__}: {exc}")

                if i < CHECKS_PER_RUN:
                    time.sleep(GAP_SECONDS)
        finally:
            browser.close()

    if failures < CHECKS_PER_RUN:
        maybe_send_heartbeat(previous)

    # Every check failed -> make the run RED in GitHub so you notice
    if failures == CHECKS_PER_RUN:
        log("ALL checks failed this run - marking run as failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
