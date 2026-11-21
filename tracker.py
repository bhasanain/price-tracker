"""Daily Amazon.sa price tracker.

Reads parts.json, fetches each product page, logs the price to prices.csv,
and sends a Telegram message for: target hit, new lowest logged price,
big jump (+10% vs last check), and pages it could not read.

It only reads public pages. It never logs in, adds to cart, or buys anything.
"""
import csv
import json
import os
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
PARTS_FILE = ROOT / "parts.json"
LOG_FILE = ROOT / "prices.csv"
JUMP_PCT = 10.0  # alert when price rises/falls this much vs the previous check

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

PRICE_SELECTORS = [
    "#corePrice_feature_div span.a-price span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div span.a-price span.a-offscreen",
    "#apex_desktop span.a-price span.a-offscreen",
    "span.priceToPay span.a-offscreen",
    "span.a-price span.a-offscreen",
]
CAPTCHA_MARKERS = ("api-services-support@amazon", "Enter the characters you see", "/errors/validateCaptcha")


def parse_price(text):
    """Turn 'SAR 3,790.00' or '3.790,00 ر.س' style text into a float, or None."""
    if not text:
        return None
    m = re.search(r"\d[\d,\.\s]*", text)
    if not m:
        return None
    raw = m.group(0).strip().replace(" ", "")
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):  # 3.790,00
            raw = raw.replace(".", "").replace(",", ".")
        else:  # 3,790.00
            raw = raw.replace(",", "")
    elif "," in raw:
        tail = raw.split(",")[-1]
        raw = raw.replace(",", ".") if len(tail) in (1, 2) else raw.replace(",", "")
    try:
        value = float(raw.rstrip("."))
    except ValueError:
        return None
    return value if value > 0 else None


def extract_price(html):
    """Return (price, status). status is 'ok', 'captcha' or 'no_price'."""
    if any(marker in html for marker in CAPTCHA_MARKERS):
        return None, "captcha"
    soup = BeautifulSoup(html, "html.parser")
    for selector in PRICE_SELECTORS:
        node = soup.select_one(selector)
        if node:
            price = parse_price(node.get_text())
            if price:
                return price, "ok"
    return None, "no_price"


def fetch(url):
    api_key = os.environ.get("SCRAPER_API_KEY")
    if api_key:
        resp = requests.get(
            "https://api.scraperapi.com/",
            params={"api_key": api_key, "url": url, "country_code": "sa"},
            timeout=90,
        )
    else:
        resp = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    if resp.status_code != 200:
        return None, f"http_{resp.status_code}"
    return extract_price(resp.text)


def load_history():
    history = {}
    if LOG_FILE.exists():
        with LOG_FILE.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["status"] == "ok" and row["price"]:
                    history.setdefault(row["name"], []).append(float(row["price"]))
    return history


def append_log(rows):
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["date", "name", "price", "status", "url"])
        writer.writerows(rows)


def evaluate(part, price, past):
    """Return a list of alert strings for this reading."""
    alerts = []
    name, target = part["name"], part.get("target")
    if target and price <= target:
        alerts.append(f"TARGET HIT: {name} is SAR {price:,.2f} (target SAR {target:,.2f})")
    if past and price < min(past):
        alerts.append(f"NEW LOW: {name} is SAR {price:,.2f} (previous low SAR {min(past):,.2f})")
    if past:
        change = (price - past[-1]) / past[-1] * 100
        if change >= JUMP_PCT:
            alerts.append(f"PRICE JUMP: {name} up {change:.0f}% to SAR {price:,.2f} (was SAR {past[-1]:,.2f})")
        elif change <= -JUMP_PCT and not any("NEW LOW" in a or "TARGET" in a for a in alerts):
            alerts.append(f"PRICE DROP: {name} down {abs(change):.0f}% to SAR {price:,.2f} (was SAR {past[-1]:,.2f})")
    return alerts


def send_telegram(text):
    token, chat_id = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram not configured; message below was NOT sent:\n" + text)
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"},
        timeout=30,
    )
    if resp.status_code != 200:
        print("Telegram error:", resp.status_code, resp.text[:200])


def main():
    parts = json.loads(PARTS_FILE.read_text(encoding="utf-8"))
    history = load_history()
    today = date.today().isoformat()
    rows, alerts, failures = [], [], []

    for part in parts:
        name, url = part["name"], part["url"]
        try:
            price, status = fetch(url)
        except requests.RequestException as exc:
            price, status = None, f"error_{type(exc).__name__}"
        rows.append([today, name, f"{price:.2f}" if price else "", status, url])
        if status == "ok":
            alerts += evaluate(part, price, history.get(name, []))
            print(f"OK   {name}: SAR {price:,.2f}")
        else:
            failures.append(f"{name} ({status})")
            print(f"FAIL {name}: {status}")
        time.sleep(random.uniform(1, 2) if os.environ.get('SCRAPER_API_KEY') else random.uniform(4, 9))  # be gentle

    append_log(rows)

    lines = []
    if alerts:
        lines.append("Price alerts:\n" + "\n".join(f"- {a}" for a in alerts))
    if failures:
        lines.append(f"Could not read {len(failures)} of {len(parts)} pages:\n" + "\n".join(f"- {f}" for f in failures))
        if len(failures) == len(parts):
            lines.append("Every page failed. Amazon is probably blocking this server. Check prices manually and see README.")
    if lines:
        send_telegram("\n\n".join(lines))
    else:
        print("No alerts today.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
