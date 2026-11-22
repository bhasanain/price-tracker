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
BUILD_TARGET = 9320.0  # ideal total for all parts marked in_build
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


LIST_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div span.a-price.a-text-price span.a-offscreen",
    "#corePrice_feature_div span.a-price.a-text-price span.a-offscreen",
    "span.basisPrice span.a-offscreen",
    "span.a-price.a-text-price[data-a-strike='true'] span.a-offscreen",
    "#listPrice",
]


def extract_list_price(html, price):
    """Amazon's crossed-out 'was' price, only if it is higher than the current price."""
    soup = BeautifulSoup(html, "html.parser")
    for selector in LIST_SELECTORS:
        node = soup.select_one(selector)
        if node:
            lp = parse_price(node.get_text())
            if lp and price and lp > price:
                return lp
    return None


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
        return None, f"http_{resp.status_code}", None
    price, status = extract_price(resp.text)
    return price, status, (extract_list_price(resp.text, price) if status == "ok" else None)


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


def build_summary(parts, current, history):
    """Total of all in_build parts now vs the ideal BUILD_TARGET."""
    total, stale = 0.0, []
    for p in parts:
        if not p.get("in_build"):
            continue
        n = p["name"]
        if n in current:
            total += current[n]
        else:  # page unreadable today: fall back to last good price
            total += (history.get(n) or [p["last_seen"]])[-1]
            stale.append(n)
    diff = total - BUILD_TARGET
    verdict = f"SAR {abs(diff):,.2f} {'ABOVE' if diff > 0 else 'BELOW'} ideal" if diff else "exactly at ideal"
    if diff <= 0:
        verdict += " - BUY NOW"
    text = f"Build total now: SAR {total:,.2f}\nIdeal: SAR {BUILD_TARGET:,.2f} ({verdict})"
    if stale:
        text += f"\n({len(stale)} part(s) unreadable today, used last known price)"
    return text


# Approximate dates; Hijri holidays shift by a day or two. Edit freely.
EVENTS = [
    ("2026-11-16", "2026-12-02", "White Friday / Black Friday sale season"),
    ("2026-12-10", "2026-12-31", "End-of-year sales"),
    ("2027-02-20", "2027-02-23", "Saudi Founding Day (Feb 22)"),
    ("2027-02-06", "2027-03-08", "Ramadan (expect Ramadan deals)"),
    ("2027-03-09", "2027-03-13", "Eid al-Fitr (approx.)"),
    ("2027-05-14", "2027-05-20", "Eid al-Adha (approx.)"),
    ("2027-09-21", "2027-09-24", "Saudi National Day (Sep 23)"),
]


def sale_context(today):
    """Lines about sales/holidays active now or starting within 14 days."""
    from datetime import date as _d, timedelta
    t = _d.fromisoformat(today)
    out = []
    for start, end, label in EVENTS:
        s, e = _d.fromisoformat(start), _d.fromisoformat(end)
        if s <= t <= e:
            out.append(f"Now: {label} (until about {end})")
        elif t < s <= t + timedelta(days=14):
            out.append(f"Soon: {label} (starts about {start})")
    return out


def buy_advice(parts, current, history, today, list_prices=None):
    """Every part: price, Amazon list price + discount, history, and a buy/wait verdict."""
    list_prices = list_prices or {}
    ordered = [p for p in parts if p.get("buying")] + [p for p in parts if not p.get("buying")]
    blocks = []
    for p in ordered:
        n = p["name"]
        if n not in current:
            continue
        price = current[n]
        past = history.get(n, [])
        count = len(past) + 1
        star = "* " if p.get("buying") else ""
        line1 = f"{star}{n}: SAR {price:,.2f}"
        lp = list_prices.get(n)
        if lp:
            line1 += f" (list SAR {lp:,.2f}, {(lp - price) / lp * 100:.0f}% off)"
        bits = []
        if p.get("last_seen"):
            ch = (price - p["last_seen"]) / p["last_seen"] * 100
            bits.append(f"you saw {p['last_seen']:,.2f} ({ch:+.0f}%)")
        if past:
            low, avg = min(past), sum(past) / len(past)
            bits.append(f"low {low:,.2f}, avg {avg:,.2f}, {count} readings")
        if p.get("target") and price <= p["target"]:
            verdict = "AT/BELOW YOUR TARGET - good time to buy"
        elif count < 5:
            verdict = f"too early to judge ({count} reading(s))"
        elif price <= low:
            verdict = "BEST PRICE SO FAR - good time to buy"
        elif price <= low * 1.03:
            verdict = "near the best price (within 3%)"
        else:
            verdict = f"WAIT - {(price - low) / low * 100:.0f}% above lowest"
        bits.append(verdict)
        blocks.append(line1 + "\n  " + " | ".join(bits))
    ctx = sale_context(today)
    if ctx:
        blocks.append("\n".join(ctx) + "\n(Sale prices are not always real discounts; trust the history.)")
    return "\n\n".join(blocks)


def send_telegram(text):
    token, chat_id = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram not configured; message below was NOT sent:\n" + text)
        return
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > 3800:
            chunks.append(cur)
            cur = ""
        cur = (cur + "\n\n" + block) if cur else block
    chunks.append(cur)
    for chunk in chunks:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": chunk[:4000], "disable_web_page_preview": "true"},
            timeout=30,
        )
        if resp.status_code != 200:
            print("Telegram error:", resp.status_code, resp.text[:200])


def main():
    parts = json.loads(PARTS_FILE.read_text(encoding="utf-8"))
    history = load_history()
    today = date.today().isoformat()
    rows, alerts, failures = [], [], []
    current, list_prices = {}, {}

    for part in parts:
        name, url = part["name"], part["url"]
        try:
            price, status, list_price = fetch(url)
            for _ in range(2):  # retry blocked pages; a new proxy address often works
                if status != "captcha" or not os.environ.get("SCRAPER_API_KEY"):
                    break
                time.sleep(3)
                price, status, list_price = fetch(url)
        except requests.RequestException as exc:
            price, status, list_price = None, f"error_{type(exc).__name__}", None
        rows.append([today, name, f"{price:.2f}" if price else "", status, url])
        if status == "ok":
            current[name] = price
            if list_price:
                list_prices[name] = list_price
            alerts += evaluate(part, price, history.get(name, []))
            print(f"OK   {name}: SAR {price:,.2f}")
        else:
            failures.append(f"{name} ({status})")
            print(f"FAIL {name}: {status}")
        time.sleep(random.uniform(1, 2) if os.environ.get('SCRAPER_API_KEY') else random.uniform(4, 9))  # be gentle

    final = os.environ.get("TRACKER_FINAL", "1") == "1"
    if failures and not final:
        print(f"{len(failures)} page(s) failed; not final attempt, will retry later.")
        return 3
    append_log(rows)

    lines = [build_summary(parts, current, history)]
    advice = buy_advice(parts, current, history, today)
    if advice:
        lines.append("ALL PARTS (* = you plan to buy)\n\n" + advice)
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
