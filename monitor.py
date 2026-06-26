"""
FIFA World Cup 2026 ticket monitor.
Watches for tickets under $1,000 at MetLife (NJ/NY), Gillette (Boston),
and Lincoln Financial Field (Philadelphia). Sends email when found.
"""

import asyncio
import json
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright

# ── Config ────────────────────────────────────────────────────────────────────
TARGET_VENUES = [
    "metlife",
    "gillette",
    "lincoln financial",
    "east rutherford",
    "foxborough",
    "foxboro",
    "philadelphia",
]
MAX_PRICE_USD = 1000
FIFA_BASE_URL = "https://tickets.fifa.com"
STATE_FILE = Path("state.json")

EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_TO = os.environ.get("EMAIL_TO", "charlesrondos@yahoo.com")
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]

# Don't re-alert for same tickets within 30 minutes
COOLDOWN_SECONDS = 1800


# ── Venue matching ────────────────────────────────────────────────────────────
def is_target_venue(text: str) -> bool:
    t = text.lower()
    return any(v in t for v in TARGET_VENUES)


# ── State (persisted via GitHub Actions cache) ────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_notified": 0, "notified_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(tickets: list[dict]):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"[FIFA ALERT] {len(tickets)} World Cup ticket(s) under ${MAX_PRICE_USD} available!"
    )
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    lines = [
        f"World Cup tickets matching your criteria are available NOW!\n",
        f"{len(tickets)} ticket(s) found under ${MAX_PRICE_USD} at your target venues:\n",
    ]
    for t in tickets:
        lines += [
            f"  Match : {t.get('match', 'Unknown')}",
            f"  Venue : {t.get('venue', 'Unknown')}",
            f"  Date  : {t.get('date', 'TBD')}",
            f"  Price : ${t.get('price', '?')}",
            f"  Link  : {t.get('url', FIFA_BASE_URL)}",
            "",
        ]
    lines += [
        f"Buy now: {FIFA_BASE_URL}",
        f"\n(Alert sent {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
    ]

    msg.attach(MIMEText("\n".join(lines), "plain"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.mail.yahoo.com", 465, context=ctx) as server:
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"Alert email sent to {EMAIL_TO}")


# ── JSON ticket extraction ─────────────────────────────────────────────────────
_PRICE_KEYS = {"price", "Price", "amount", "Amount", "faceValue", "basePrice", "totalPrice", "cost"}
_VENUE_KEYS = {"venue", "Venue", "stadium", "Stadium", "location", "Location", "venueName"}
_MATCH_KEYS = {"match", "matchName", "title", "name", "eventName", "description"}
_DATE_KEYS  = {"date", "startDate", "dateTime", "kickoff", "matchDate", "eventDate"}
_LINK_KEYS  = {"url", "link", "purchaseUrl", "ticketUrl", "href"}


def _extract(obj, depth=0) -> list[dict]:
    if depth > 12:
        return []
    results = []
    if isinstance(obj, list):
        for item in obj:
            results.extend(_extract(item, depth + 1))
    elif isinstance(obj, dict):
        price_val = next((obj[k] for k in _PRICE_KEYS if k in obj), None)
        venue_val = next((obj[k] for k in _VENUE_KEYS if k in obj), None)
        if price_val is not None and venue_val is not None:
            try:
                price = float(str(price_val).replace("$", "").replace(",", "").strip())
                ticket_id = str(
                    obj.get("id") or obj.get("ticketId") or obj.get("seatId")
                    or f"{venue_val}_{price}"
                )
                link = next((str(obj[k]) for k in _LINK_KEYS if k in obj), FIFA_BASE_URL)
                results.append({
                    "id": ticket_id,
                    "price": price,
                    "venue": str(venue_val),
                    "match": str(next((obj[k] for k in _MATCH_KEYS if k in obj), "Unknown Match")),
                    "date": str(next((obj[k] for k in _DATE_KEYS if k in obj), "TBD")),
                    "url": link if link.startswith("http") else FIFA_BASE_URL,
                })
            except (ValueError, TypeError):
                pass
        for v in obj.values():
            if isinstance(v, (dict, list)):
                results.extend(_extract(v, depth + 1))
    return results


# ── Scraper ───────────────────────────────────────────────────────────────────
async def scrape_tickets() -> list[dict]:
    captured = []
    found = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await ctx.new_page()

        # Intercept every JSON API response
        async def on_response(response):
            if response.status == 200 and "json" in response.headers.get("content-type", ""):
                try:
                    data = await response.json()
                    captured.append({"url": response.url, "data": data})
                except Exception:
                    pass

        page.on("response", on_response)

        # Load the main ticketing page
        try:
            print(f"Loading {FIFA_BASE_URL} ...")
            await page.goto(FIFA_BASE_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(4000)
        except Exception as e:
            print(f"Page load warning: {e}")

        # Try navigating into ticket listings
        for sel in [
            "a[href*='ticket']",
            "a[href*='match']",
            "button:has-text('Tickets')",
            "a:has-text('Buy Tickets')",
            "a:has-text('Resale')",
        ]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    await el.click()
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                pass

        await browser.close()

    print(f"Captured {len(captured)} API responses")

    for resp in captured:
        for ticket in _extract(resp["data"]):
            venue_text = ticket["venue"] + " " + ticket["match"]
            if is_target_venue(venue_text) and 0 < ticket["price"] < MAX_PRICE_USD:
                ticket["source_url"] = resp["url"]
                found.append(ticket)

    return found


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    state = load_state()
    notified_ids = set(state.get("notified_ids", []))
    last_notified = state.get("last_notified", 0)
    now = time.time()

    tickets = await scrape_tickets()
    print(f"Found {len(tickets)} matching ticket(s)")

    if not tickets:
        save_state(state)
        sys.exit(0)

    # Skip tickets we already alerted about (unless cooldown expired)
    cooldown_active = (now - last_notified) < COOLDOWN_SECONDS
    if cooldown_active:
        tickets = [t for t in tickets if t["id"] not in notified_ids]

    if not tickets:
        print("All found tickets already notified within cooldown window. Skipping.")
        sys.exit(0)

    send_email(tickets)

    state["last_notified"] = now
    new_ids = notified_ids | {t["id"] for t in tickets}
    state["notified_ids"] = list(new_ids)[-500:]  # cap history
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
