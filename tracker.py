"""Daily Amazon.sa price tracker.

Reads parts.json, fetches each product page, logs the price to prices.csv,
and sends a Telegram message for: target hit, new lowest logged price,
big jump (+10% vs last check), and pages it could not read.

It only reads public pages. It never logs in, adds to cart, or buys anything.
"""
import csv
import html
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.amazon.sa/",
}
COOKIE_FILE = ROOT / ".cookies.json"
PARTIAL_FILE = ROOT / ".partial.json"
DEAL_FILE = ROOT / ".deal_active"
SESSION = requests.Session()
try:  # reuse cookies between runs: a returning visitor gets fewer captchas
    SESSION.cookies.update(json.loads(COOKIE_FILE.read_text()))
except Exception:
    pass

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
        resp = SESSION.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    if resp.status_code != 200:
        return None, f"http_{resp.status_code}", None
    price, status = extract_price(resp.text)
    return price, status, (extract_list_price(resp.text, price) if status == "ok" else None)


def explain(html):
    """Debug: which price candidates exist on a page and who sells it."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("#productTitle")
    print("Title:", title.get_text(strip=True)[:120] if title else "(none)")
    for sel in ("#merchant-info", "#tabular-buybox .tabular-buybox-text", "#sellerProfileTriggerId"):
        node = soup.select_one(sel)
        if node:
            print("Seller info:", " ".join(node.get_text(" ", strip=True).split())[:160])
            break
    for sel in PRICE_SELECTORS + LIST_SELECTORS:
        node = soup.select_one(sel)
        if node:
            print(f"  {sel} -> {node.get_text(strip=True)}")


def check_part(fragment):
    parts = json.loads(PARTS_FILE.read_text(encoding="utf-8"))
    for p in parts:
        if fragment.lower() in p["name"].lower():
            print("Part:", p["name"], "| you saw:", p.get("last_seen"))
            resp = SESSION.get(p["url"], headers=HEADERS, timeout=30, allow_redirects=True)
            print("HTTP", resp.status_code)
            if any(m in resp.text for m in CAPTCHA_MARKERS):
                print("Captcha page - try again later.")
            else:
                print("Script reads:", extract_price(resp.text))
                explain(resp.text)
            return 0
    print("No part matches", fragment)
    return 1


def load_history():
    history = {}
    if LOG_FILE.exists():
        with LOG_FILE.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["status"] == "ok" and row["price"]:
                    history.setdefault(row["name"], []).append(float(row["price"]))
    return history


def load_days():
    """name -> set of distinct dates with a good reading (several runs a day count once)."""
    days = {}
    if LOG_FILE.exists():
        with LOG_FILE.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["status"] == "ok" and row["price"]:
                    days.setdefault(row["name"], set()).add(row["date"])
    return days


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


def goal_price(parts, current, history, part):
    """Highest price this part can cost for the whole build to still hit BUILD_TARGET."""
    others = 0.0
    for p in parts:
        if not p.get("in_build") or p["name"] == part["name"]:
            continue
        n = p["name"]
        others += current[n] if n in current else (history.get(n) or [p["last_seen"]])[-1]
    return BUILD_TARGET - others


def spam_reasons(parts, part, price, list_price, history, current):
    """Why this part deserves a loud alert right now (empty list = no)."""
    reasons = []
    pct = part.get("spam_discount")
    if part.get("goal_part"):
        gp = goal_price(parts, current, history, part)
        if price <= gp:
            reasons.append(f"at or below SAR {gp:,.0f}, which puts your whole build back on the SAR {BUILD_TARGET:,.0f} goal")
    if pct and list_price and (list_price - price) / list_price * 100 >= pct:
        reasons.append(f"{(list_price - price) / list_price * 100:.0f}% off Amazon's list price (SAR {list_price:,.2f})")
    past = history.get(part["name"], [])
    if pct and past:
        avg = sum(past) / len(past)
        if (avg - price) / avg * 100 >= pct:
            reasons.append(f"{(avg - price) / avg * 100:.0f}% below its average logged price (SAR {avg:,.2f})")
    return reasons


def spam_burst(part, price, reasons, count=5):
    for i in range(count):
        send_telegram(
            f"<b>\U0001F6A8 BUY ALERT {i + 1}/{count}</b>\n"
            f"<b>{esc(label(part['name']))}</b> is now <b>SAR {money(price)}</b>\n"
            + "\n".join(f"\u2022 {esc(r)}" for r in reasons)
            + f"\n<a href=\"{esc(part['url'])}\">Open on Amazon.sa</a>",
            use_html=True,
        )
        time.sleep(2)


def check_deals(parts, current, list_prices, history):
    """Loud alerts for 'buying' parts. Returns True while a deal is active."""
    active = False
    for p in parts:
        if not p.get("buying") or p.get("bought") or p["name"] not in current:
            continue
        reasons = spam_reasons(parts, p, current[p["name"]], list_prices.get(p["name"]), history, current)
        if reasons:
            active = True
            if not DEAL_FILE.exists():
                spam_burst(p, current[p["name"]], reasons)
    if active:
        DEAL_FILE.write_text(date.today().isoformat())
    elif DEAL_FILE.exists():
        DEAL_FILE.unlink()
        send_telegram("Deal alert ended: your watched parts are no longer at the alert level.")
    return active


def deal_mode():
    """Hourly follow-up while a deal is active: re-check only the 'buying' parts and keep pinging."""
    parts = json.loads(PARTS_FILE.read_text(encoding="utf-8"))
    history = load_history()
    current, list_prices = {}, {}
    for p in parts:
        if p.get("bought"):
            current[p["name"]] = p["bought"]
        if not p.get("buying") or p.get("bought"):
            continue
        try:
            price, status, lp = fetch(p["url"])
        except requests.RequestException:
            continue
        if status == "ok":
            current[p["name"]] = price
            if lp:
                list_prices[p["name"]] = lp
        time.sleep(random.uniform(10, 20))
    for p in parts:
        if p.get("buying") and not p.get("bought") and p["name"] in current:
            reasons = spam_reasons(parts, p, current[p["name"]], list_prices.get(p["name"]), history, current)
            if reasons:
                spam_burst(p, current[p["name"]], reasons, count=3)
                DEAL_FILE.write_text(date.today().isoformat())
            elif DEAL_FILE.exists() and not any(
                spam_reasons(parts, q, current[q["name"]], list_prices.get(q["name"]), history, current)
                for q in parts if q.get("buying") and not q.get("bought") and q["name"] in current
            ):
                DEAL_FILE.unlink()
                send_telegram("Deal alert ended: your watched parts are no longer at the alert level.")
    return 0


def esc(text):
    return html.escape(str(text), quote=False)


def money(x):
    return f"{x:,.2f}" if x < 100 else f"{x:,.0f}"


def label(name):
    """'GPU - XFX RX 9070 XT White' -> 'GPU · XFX RX 9070 XT White'."""
    return name.replace(" - ", " \u00b7 ", 1)


def part_status(p, price, past, count, list_price):
    """(icon, short verdict) for one part."""
    low = min(past) if past else None
    if p.get("target") and price <= p["target"]:
        return "\U0001F7E2", "at or below your target"
    if count < 5:
        return "\u26AA", f"too early to judge ({count} day(s) of data)"
    if max(past) == min(past) == price:
        return "\u26AA", "unchanged since tracking began"
    if price <= low:
        ls = p.get("last_seen")
        if ls and price > ls * 1.05:
            return "\U0001F7E1", f"lowest logged, but {(price - ls) / ls * 100:.0f}% above first seen"
        return "\U0001F7E2", "best price so far"
    if price <= low * 1.03:
        return "\U0001F7E1", "near the best price"
    return "\U0001F534", f"{(price - low) / low * 100:.0f}% above its lowest"


def build_numbers(parts, current, history):
    total, spent, stale = 0.0, 0.0, []
    for p in parts:
        if not p.get("in_build"):
            continue
        n = p["name"]
        if p.get("bought"):
            price = p["bought"]
            spent += price
        elif n in current:
            price = current[n]
        else:
            price = (history.get(n) or [p["last_seen"]])[-1]
            stale.append(n)
        total += price
    return total, spent, stale


def format_report(parts, current, list_prices, history, days, today, alerts, failures):
    """Telegram HTML report: summary, watched parts, alerts, other parts, unreadable pages."""
    from datetime import date as _d
    total, spent, stale = build_numbers(parts, current, history)
    diff = total - BUILD_TARGET
    mark = "\U0001F7E2" if diff <= 0 else "\U0001F534"
    head = f"<b>\U0001F4CA Price report \u00b7 {_d.fromisoformat(today).strftime('%a %d %b')}</b>"
    build = (f"<b>Build</b>  {money(total)} / goal {money(BUILD_TARGET)}  {mark} "
             f"{money(abs(diff))} {'under - BUY' if diff <= 0 else 'over'}")
    if spent:
        build += (f"\nBought {money(spent)} \u00b7 still to buy {money(total - spent)} "
                  f"\u00b7 budget left {money(BUILD_TARGET - spent)}")
    if stale:
        build += f"\n<i>{len(stale)} part(s) unreadable today, last known price used</i>"
    blocks = [head + "\n\n" + build]

    def line_parts(p):
        n = p["name"]
        price = current[n]
        past = history.get(n, [])
        count = len((days or {}).get(n, set()) | {today})
        icon, verdict = part_status(p, price, past, count, list_prices.get(n))
        bits = [f"<b>{money(price)}</b>"]
        if p.get("last_seen"):
            ch = (price - p["last_seen"]) / p["last_seen"] * 100
            bits.append("same as first seen" if abs(ch) < 0.5 else f"{ch:+.0f}% vs first seen")
        lp = list_prices.get(n)
        if lp:
            bits.append(f"list {money(lp)} ({(lp - price) / lp * 100:.0f}% off)")
        return icon, verdict, bits, past

    watched, others, bought = [], [], []
    for p in parts:
        n = p["name"]
        if p.get("bought"):
            bought.append(f"\u2705 {esc(label(n))} \u2014 bought {money(p['bought'])}")
        elif n in current:
            (watched if p.get("buying") else others).append(p)

    if watched or bought:
        rows = list(bought)
        for p in watched:
            icon, verdict, bits, past = line_parts(p)
            row = f"{icon} <b>{esc(label(p['name']))}</b>\n    " + " \u00b7 ".join(bits)
            if past:
                row += f"\n    low {money(min(past))} \u00b7 avg {money(sum(past) / len(past))}"
            row += f"\n    {esc(verdict)}"
            if p.get("goal_part"):
                gp = goal_price(parts, current, history, p)
                need = (current[p["name"]] - gp) / current[p["name"]] * 100
                row += (f"\n    goal price \u2264 {money(gp)}" + (f" (needs {need:.0f}% off)" if need > 0 else " \u2014 reached"))
            rows.append(row)
        blocks.append("<b>\u2B50 Watching</b>\n" + "\n".join(rows))

    if alerts:
        blocks.append("<b>\U0001F514 Alerts</b>\n" + "\n".join(f"\u2022 {esc(a)}" for a in alerts))

    if others:
        rows = []
        for p in others:
            icon, verdict, bits, _ = line_parts(p)
            rows.append(f"{icon} {esc(label(p['name']))} \u2014 " + " \u00b7 ".join(b.replace(" vs first seen", "") for b in bits))
        blocks.append("<b>\U0001F4CB Other parts</b>\n" + "\n".join(rows))

    ctx = sale_context(today)
    if ctx:
        blocks.append("\U0001F3F7 " + esc(" \u00b7 ".join(ctx)) + "\n<i>Sale prices are not always real discounts.</i>")

    if failures:
        rows = [f"\u2022 <a href=\"{esc(u)}\">{esc(label(n))}</a> ({esc(st)})" for n, st, u in failures]
        blocks.append(f"<b>\u26A0\uFE0F Could not read {len(failures)}</b> \u2014 tap to check by hand\n" + "\n".join(rows))
        if len(failures) == len([p for p in parts if not p.get("bought")]):
            blocks.append("Every page failed. Amazon is probably blocking this connection.")
    blocks.append("\U0001F7E2 good \u00b7 \U0001F7E1 near/ok \u00b7 \U0001F534 wait \u00b7 \u26AA too early")
    return "\n\n".join(blocks)


def parse_search(html):
    """Results from an Amazon.sa search page: [(title, price, url)]."""
    out = []
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select('div[data-component-type="s-search-result"]'):
        asin = card.get("data-asin")
        title = card.select_one("h2")
        price = card.select_one("span.a-price span.a-offscreen")
        if asin and title and price:
            val = parse_price(price.get_text())
            if val:
                out.append((title.get_text(" ", strip=True), val, f"https://www.amazon.sa/dp/{asin}"))
    return out


def run_searches(parts, current):
    """Weekly: look for other white models and message the cheapest ones."""
    try:
        searches = json.loads((ROOT / "searches.json").read_text(encoding="utf-8"))
    except Exception:
        return
    blocks = []
    for sr in searches:
        try:
            if os.environ.get("SCRAPER_API_KEY"):
                resp = requests.get("https://api.scraperapi.com/", params={"api_key": os.environ["SCRAPER_API_KEY"], "url": "https://www.amazon.sa/s?k=" + requests.utils.quote(sr["query"]), "country_code": "sa"}, timeout=90)
            else:
                resp = SESSION.get("https://www.amazon.sa/s", params={"k": sr["query"]}, headers=HEADERS, timeout=30)
            if resp.status_code != 200 or any(m in resp.text for m in CAPTCHA_MARKERS):
                blocks.append(f"{sr['name']}: search page unavailable today")
                continue
            hits = [r for r in parse_search(resp.text) if all(w in r[0].lower() for w in sr["must"])]
        except requests.RequestException:
            continue
        hits = sorted(hits, key=lambda r: r[1])[:4]
        if hits:
            blocks.append(sr["name"] + ":\n" + "\n".join(f"- SAR {p:,.2f} {t[:70]}\n  {u}" for t, p, u in hits))
        else:
            blocks.append(f"{sr['name']}: no matching white listings found")
        time.sleep(random.uniform(10, 20))
    if blocks:
        send_telegram("WEEKLY: OTHER WHITE OPTIONS (cheapest matches on Amazon.sa search)\n\n" + "\n\n".join(blocks))


def send_telegram(text, use_html=False):
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
            data={"chat_id": chat_id, "text": chunk[:4000], "disable_web_page_preview": "true", **({"parse_mode": "HTML"} if use_html else {})},
            timeout=30,
        )
        if resp.status_code != 200:
            print("Telegram error:", resp.status_code, resp.text[:200])


def main():
    parts = json.loads(PARTS_FILE.read_text(encoding="utf-8"))
    history = load_history()
    days = load_days()
    today = date.today().isoformat()
    current, list_prices, status_of = {}, {}, {}

    for p in parts:  # bought parts are locked at the price paid and never fetched
        if p.get("bought"):
            current[p["name"]] = p["bought"]
            status_of[p["name"]] = "bought"

    # Pages already read earlier today are not fetched again (fewer requests, fewer captchas).
    try:
        saved = json.loads(PARTIAL_FILE.read_text())
        if saved.get("date") == today:
            for name, (price, lp) in saved["ok"].items():
                current[name] = price
                if lp:
                    list_prices[name] = lp
                status_of[name] = "ok"
    except Exception:
        pass

    todo = [p for p in parts if p["name"] not in current]
    random.shuffle(todo)  # different order each run so the same pages aren't always last
    for part in todo:
        name, url = part["name"], part["url"]
        try:
            price, status, list_price = fetch(url)
            for _ in range(2):  # Amazon rate-limits bursts: wait, then retry captcha pages
                if status != "captcha":
                    break
                time.sleep(random.uniform(45, 75) if not os.environ.get("SCRAPER_API_KEY") else 3)
                price, status, list_price = fetch(url)
        except requests.RequestException as exc:
            price, status, list_price = None, f"error_{type(exc).__name__}", None
        status_of[name] = status
        if status == "ok":
            current[name] = price
            if list_price:
                list_prices[name] = list_price
            print(f"OK   {name}: SAR {price:,.2f}")
        else:
            print(f"FAIL {name}: {status}")
        time.sleep(random.uniform(1, 2) if os.environ.get("SCRAPER_API_KEY") else random.uniform(10, 20))

    try:
        COOKIE_FILE.write_text(json.dumps(requests.utils.dict_from_cookiejar(SESSION.cookies)))
    except Exception:
        pass

    check_deals(parts, current, list_prices, history)

    failures = [(p["name"], status_of.get(p["name"], "not_read"), p["url"]) for p in parts if p["name"] not in current]
    final = os.environ.get("TRACKER_FINAL", "1") == "1"
    if failures and not final:
        PARTIAL_FILE.write_text(json.dumps({"date": today, "ok": {n: [current[n], list_prices.get(n)] for n in current}}))
        print(f"{len(failures)} page(s) still unread; keeping the {len(current)} good ones, retrying later.")
        return 3

    rows = []
    for p in parts:
        n = p["name"]
        rows.append([today, n, f"{current[n]:.2f}" if n in current else "", status_of.get(n, "ok") if n in current else status_of.get(n, "not_read"), p["url"]])
    append_log(rows)
    PARTIAL_FILE.unlink(missing_ok=True)

    alerts = []
    for p in parts:
        if p["name"] in current and not p.get("bought"):
            alerts += evaluate(p, current[p["name"]], history.get(p["name"], []))

    send_telegram(format_report(parts, current, list_prices, history, days, today, alerts, failures), use_html=True)
    if date.today().weekday() == 4:  # Fridays: look for other white models
        run_searches(parts, current)
    return 0


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--deal":
    sys.exit(deal_mode())
if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--check":
    sys.exit(check_part(sys.argv[2]))
if __name__ == "__main__":
    sys.exit(main())
