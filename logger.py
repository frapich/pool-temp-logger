"""Pool temperature logger, run by GitHub Actions (.github/workflows/log.yml).

Logs in to Hayward OmniLogic, reads the pool water temperature, adds the outside
air temperature from Open-Meteo, appends the reading to data/readings.csv and
rebuilds site/index.html. Failures go to the "Various" Telegram bot: after two
failed runs in a row, again every 6 h while it stays broken, and once on recovery.

    python logger.py           # take a reading, update data, build the page
    python logger.py --check   # take a reading and print it, change nothing
    python logger.py --build   # only rebuild the page from data/readings.csv

Secrets (environment): HAYWARD_USER, HAYWARD_PASSWORD, HAYWARD_MSP_ID,
HAYWARD_BOW_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""

import csv
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_FILE = HERE / "data" / "readings.csv"
STATE_FILE = HERE / "data" / "alert_state.json"
TEMPLATE = HERE / "pool-page.template.html"
SITE_DIR = HERE / "site"

BASE_URL = "https://www.haywardomnilogic.com"
LOGIN_URL = f"{BASE_URL}/Login.aspx"
SELECT_URL = f"{BASE_URL}/Module/UserManagement/Select_Msp.aspx"
USER_AGENT = "Mozilla/5.0"

# Oro Valley, AZ
WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude=32.39&longitude=-110.97"
               "&current=temperature_2m&temperature_unit=fahrenheit")

MAX_RETRIES = 3
RETRY_DELAY_S = 2

ALERT_AFTER_FAILS = 2
ALERT_REPEAT_HOURS = 6
PAGE_URL = "https://frapich.github.io/pool-temp-logger/"
ARIZONA = timezone(timedelta(hours=-7), "MST")  # Arizona has no daylight saving


def log(msg, level="INFO"):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z | {level} | {msg}", flush=True)


def with_retry(action):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return action()
        except Exception as e:
            if attempt >= MAX_RETRIES:
                raise
            log(f"Attempt {attempt} failed: {e}. Retrying in {RETRY_DELAY_S}s...", "WARN")
            time.sleep(RETRY_DELAY_S)


def hidden_field(html, name):
    m = re.search(r'<input[^>]*name="' + re.escape(name) + r'"[^>]*value="([^"]*)"[^>]*>', html, re.I)
    if not m:
        raise RuntimeError(f"Hidden field '{name}' not found.")
    return m.group(1)


def get_pool_temperature():
    user, password = os.environ["HAYWARD_USER"], os.environ["HAYWARD_PASSWORD"]
    dash_url = (f"{BASE_URL}/Module/UserManagement/Dashboard.aspx"
                f"?mspID={os.environ['HAYWARD_MSP_ID']}&bowID={os.environ['HAYWARD_BOW_ID']}")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def fetch(url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
        with opener.open(req, timeout=60) as res:
            return res.geturl(), res.read().decode("utf-8", "replace")

    # Step 1: login page for fresh ASP.NET hidden fields
    _, login_html = with_retry(lambda: fetch(LOGIN_URL))
    form = {
        "__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "",
        "__VIEWSTATE": hidden_field(login_html, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": hidden_field(login_html, "__VIEWSTATEGENERATOR"),
        "__SCROLLPOSITIONX": "0", "__SCROLLPOSITIONY": "0",
        "__EVENTVALIDATION": hidden_field(login_html, "__EVENTVALIDATION"),
        "ddlLanguages": "en-US", "txtLoginName": user, "txtPassword": password, "btnLogin": "SIGN IN",
    }

    # Step 2: post the login form (redirects are followed)
    final_url, _ = with_retry(lambda: fetch(
        LOGIN_URL, urllib.parse.urlencode(form).encode(),
        {"Content-Type": "application/x-www-form-urlencoded", "Origin": BASE_URL, "Referer": LOGIN_URL}))
    if re.search(r"Login\.aspx", final_url, re.I):
        raise RuntimeError("Login failed. Still on Login.aspx. Check credentials or field handling.")

    # Step 3: dashboard
    _, dash_html = with_retry(lambda: fetch(dash_url, headers={"Referer": SELECT_URL}))
    m = re.search(r'id="cphMainContent_lblTempActual">(\d+)<', dash_html)
    if not m:
        raise RuntimeError("Dashboard loaded but pool temperature element was not found.")
    return int(m.group(1))


def get_ambient_temperature():
    def fetch():
        req = urllib.request.Request(WEATHER_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.load(res)
    try:
        temp = with_retry(fetch).get("current", {}).get("temperature_2m")
    except Exception as e:
        log(f"Weather lookup failed: {e}", "WARN")
        return None
    return None if temp is None else round(temp)


def append_reading(when, pool, air):
    new = not DATA_FILE.exists()
    DATA_FILE.parent.mkdir(exist_ok=True)
    with DATA_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["recorded_utc", "pool_f", "air_f"])
        w.writerow([when.strftime("%Y-%m-%dT%H:%M:%SZ"), pool, "" if air is None else air])


def build_page():
    rows = []
    if DATA_FILE.exists():
        with DATA_FILE.open(newline="") as f:
            for r in csv.DictReader(f):
                rows.append([r["recorded_utc"], int(r["pool_f"]), int(r["air_f"]) if r["air_f"] else None])
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("/*DATA*/", json.dumps(rows, separators=(",", ":")))
    html = html.replace("/*GENERATED*/", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(html, encoding="utf-8")
    log(f"Page built: {len(rows)} readings")


def send_telegram(text):
    """Never raises: an alert that can't be sent must not break the run."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("Telegram secrets missing; alert not sent.", "WARN")
        return False
    try:
        body = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=20) as res:
            return bool(json.load(res).get("ok"))
    except Exception as e:
        log(f"Telegram send failed: {e}", "WARN")
        return False


def update_alert_state(error=None, reading=None):
    blank = {"fails": 0, "failingSince": None, "lastError": None, "lastAlert": None}
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else dict(blank)
    except Exception:
        state = dict(blank)
    now = datetime.now(timezone.utc)

    if not error:
        if state.get("lastAlert"):
            mins = (now - datetime.fromisoformat(state["failingSince"])).total_seconds() / 60 if state.get("failingSince") else 0
            down = f" after {mins / 60:.0f} h" if mins >= 120 else f" after {mins:.0f} min" if mins > 0 else ""
            send_telegram(f"✅ Pool logger back to normal{down}. {reading}")
        state = dict(blank)
    else:
        if state["fails"] == 0:
            state["failingSince"] = now.isoformat(timespec="seconds")
        state["fails"] += 1
        state["lastError"] = error
        last = state.get("lastAlert")
        due = state["fails"] >= ALERT_AFTER_FAILS and (
            not last or (now - datetime.fromisoformat(last)).total_seconds() >= ALERT_REPEAT_HOURS * 3600)
        if due:
            since = datetime.fromisoformat(state["failingSince"]).astimezone(ARIZONA).strftime("%b %-d %-I:%M %p")
            what = "still failing" if last else "failed"
            run = os.environ.get("RUN_URL", "")
            text = (f"⚠ Pool logger {what} on GitHub Actions - no reading since {since} AZ "
                    f"({state['fails']} failed runs).\n\nError: {error}\n\n"
                    + (f"Run: {run}\n" if run else "") + f"Page: {PAGE_URL}")
            if send_telegram(text):
                state["lastAlert"] = now.isoformat(timespec="seconds")
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def main():
    if "--build" in sys.argv:
        build_page()
        return 0
    check = "--check" in sys.argv
    try:
        pool = get_pool_temperature()
        air = get_ambient_temperature()
        air_str = "N/A" if air is None else f"{air} F"
        log(f"SUCCESS | Pool: {pool} F | Ambient: {air_str}")
        if check:
            return 0
        append_reading(datetime.now(timezone.utc), pool, air)
        update_alert_state(reading=f"Pool {pool} F, outside {air_str}.")
    except Exception as e:
        log(f"ERROR | {e}", "ERROR")
        if check:
            return 1
        update_alert_state(error=str(e))
    # The page is rebuilt either way so its "last reading" age stays honest.
    build_page()
    # Exit 0 even after a failed reading: Telegram is the alert channel, and a
    # failing scheduled workflow would also email on every run.
    return 0


if __name__ == "__main__":
    sys.exit(main())
