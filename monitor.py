"""
FIFA World Cup 2026 ticket monitor.
Watches for tickets under $1,000 at MetLife (NJ/NY), Gillette (Boston),
and Lincoln Financial Field (Philadelphia).
Sources: FIFA official site + StubHub resale.
Sends email when found.
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
STUBHUB_SEARCH_URL = (
    "https://www.stubhub.com/search?q=FIFA+World+Cup+2026"
)
STATE_FILE = Path("state.json")

EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_TO = os.environ.get("EMAIL_TO", "charlesrondos@yahoo.com")
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]

COOLDOWN_SECONDS = 1800  # 30 min between repeat alerts for same tickets


# ── Venue matching ────────────────────────────────────────────────────────────
def is_target_venue(text: str) -> bool:
    t = text.lower()
    return any(v in t for v in TARGET_VENUES)


# ── State ─────────────────────────────────────────────────────────────────────
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
        "World Cup tickets matching your criteria are available NOW!\n",
        f"{len(tickets)} ticket(s) found under ${MAX_PRICE_USD} at your target venues:\n",
    ]
    for t in tickets:
        source = t.get("source", "unknown").upper()
        lines += [
            f"  [{source}]",
            f"  Match : {t.get('match', 'Unknown')}",
            f"  Venue : {t.get('venue', 'Unknown')}",
            f"  Date  : {t.get('date', 'TBD')}",
            f"  Price : ${t.get('price', '?')}",
            f"  Link  : {t.get('url', FIFA_BASE_URL)}",
            "",
        ]
    lines += [
        f"FIFA official: {FIFA_BASE_URL}",
        f"StubHub:       https://www.stubhub.com/search?q=FIFA+World+Cup+2026",
        f"\n(Alert sent {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
    ]

    msg.attach(MIMEText("\n".join(lines), "plain"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.mail.yahoo.com", 465, context=ctx) as server:
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"Alert email sent to {EMAIL_TO}")


# ── JSON ticket extraction (shared by both scrapers) ──────────────────────────
_PRICE_KEYS = {"price", "Price", "amount", "Amount", "faceValue", "basePrice",
               "totalPrice", "cost", "listingPrice", "currentPrice", "minPrice"}
_VENUE_KEYS = {"venue", "Venue", "stadium", "Stadium", "location", "Location",
               "venueName", "venueeName"}
_MATCH_KEYS = {"match", "matchName", "title", "name", "eventName", "description",
               "event", "eventTitle"}
_DATE_KEYS  = {"date", "startDate", "dateTime", "kickoff", "matchDate",
               "eventDate", "eventDateLocal", "performanceDate"}
_LINK_KEYS  = {"url", "link", "purchaseUrl", "ticketUrl", "href", "eventUrl",
               "listingUrl"}


def _extract(obj, source: str, fallback_url: str, depth=0) -> list[dict]:
    if depth > 12:
        return []
    results = []
    if isinstance(obj, list):
        for item in obj:
            results.extend(_extract(item, source, fallback_url, depth + 1))
    elif isinstance(obj, dict):
        price_val = next((obj[k] for k in _PRICE_KEYS if k in obj), None)
        venue_val = next((obj[k] for k in _VENUE_KEYS if k in obj), None)
        if price_val is not None and venue_val is not None:
            try:
                price = float(str(price_val).replace("$", "").replace(",", "").strip())
                ticket_id = f"{source}:{obj.get('id') or obj.get('ticketId') or obj.get('listingId') or f'{venue_val}_{price}'}"
                link = next((str(obj[k]) for k in _LINK_KEYS if k in obj), fallback_url)
                results.append({
                    "id": ticket_id,
                    "source": source,
                    "price": price,
                    "venue": str(venue_val),
                    "match": str(next((obj[k] for k in _MATCH_KEYS if k in obj), "Unknown Match")),
                    "date": str(next((obj[k] for k in _DATE_KEYS if k in obj), "TBD")),
                    "url": link if link.startswith("http") else fallback_url,
                })
            except (ValueError, TypeError):
                pass
        for v in obj.values():
            if isinstance(v, (dict, list)):
                results.extend(_extract(v, source, fallback_url, depth + 1))
    return results


# ── Browser helper ────────────────────────────────────────────────────────────
def _browser_context_args():
    return dict(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
    )


# ── FIFA scraper ──────────────────────────────────────────────────────────────
async def scrape_fifa() -> list[dict]:
    captured = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(**_browser_context_args())
        page = await ctx.new_page()

        async def on_response(response):
            if response.status == 200 and "json" in response.headers.get("content-type", ""):
                try:
                    captured.append({"url": response.url, "data": await response.json()})
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            print("FIFA: loading page...")
            await page.goto(FIFA_BASE_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(4000)
        except Exception as e:
            print(f"FIFA: page load warning: {e}")

        for sel in ["a[href*='ticket']", "a[href*='match']",
                    "button:has-text('Tickets')", "a:has-text('Buy Tickets')"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    await el.click()
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                pass

        await browser.close()

    found = []
    print(f"FIFA: captured {len(captured)} API responses")
    for resp in captured:
        for ticket in _extract(resp["data"], "FIFA", FIFA_BASE_URL):
            if is_target_venue(ticket["venue"] + " " + ticket["match"]) and 0 < ticket["price"] < MAX_PRICE_USD:
                found.append(ticket)
    print(f"FIFA: {len(found)} matching ticket(s)")
    return found


# ── StubHub scraper ───────────────────────────────────────────────────────────
async def scrape_stubhub() -> list[dict]:
    captured = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(**_browser_context_args())
        page = await ctx.new_page()

        async def on_response(response):
            if response.status == 200 and "json" in response.headers.get("content-type", ""):
                try:
                    data = await response.json()
                    # StubHub API responses tend to live under stubhub.com domains
                    if "stubhub" in response.url:
                        captured.append({"url": response.url, "data": data})
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            print("StubHub: loading search page...")
            await page.goto(STUBHUB_SEARCH_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(5000)

            # Try clicking into the FIFA World Cup event if it appears
            for sel in [
                "a:has-text('FIFA World Cup')",
                "a:has-text('World Cup 2026')",
                "[data-testid='event-card']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        await el.click()
                        await page.wait_for_timeout(4000)
                        break
                except Exception:
                    pass
        except Exception as e:
            print(f"StubHub: page load warning: {e}")

        await browser.close()

    found = []
    print(f"StubHub: captured {len(captured)} API responses")
    for resp in captured:
        for ticket in _extract(resp["data"], "StubHub", STUBHUB_SEARCH_URL):
            if is_target_venue(ticket["venue"] + " " + ticket["match"]) and 0 < ticket["price"] < MAX_PRICE_USD:
                found.append(ticket)
    print(f"StubHub: {len(found)} matching ticket(s)")
    return found


# ── Main ──────────────────────────────────────────────────────────────────────
FAKE_TICKETS = [
    {
        "id": "test-001",
        "source": "FIFA",
        "price": 299,
        "venue": "MetLife Stadium",
        "match": "USA vs Brazil - Quarter Final",
        "date": "2026-07-05T19:00:00",
        "url": "https://tickets.fifa.com",
    },
    {
        "id": "test-002",
        "source": "StubHub",
        "price": 749,
        "venue": "Lincoln Financial Field",
        "match": "Argentina vs England - Round of 16",
        "date": "2026-07-01T15:00:00",
        "url": "https://www.stubhub.com/search?q=FIFA+World+Cup+2026",
    },
]


async def main():
    test_mode = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes")

    if test_mode:
        print("TEST MODE — skipping scrapers, sending fake ticket email...")
        send_email(FAKE_TICKETS)
        print("Done.")
        sys.exit(0)

    state = load_state()
    notified_ids = set(state.get("notified_ids", []))
    last_notified = state.get("last_notified", 0)
    now = time.time()

    # Run both scrapers in parallel
    fifa_tickets, stubhub_tickets = await asyncio.gather(
        scrape_fifa(),
        scrape_stubhub(),
    )
    tickets = fifa_tickets + stubhub_tickets
    print(f"Total matching ticket(s) found: {len(tickets)}")

    if not tickets:
        save_state(state)
        sys.exit(0)

    cooldown_active = (now - last_notified) < COOLDOWN_SECONDS
    if cooldown_active:
        tickets = [t for t in tickets if t["id"] not in notified_ids]

    if not tickets:
        print("All found tickets already notified within cooldown window. Skipping.")
        sys.exit(0)

    send_email(tickets)

    state["last_notified"] = now
    new_ids = notified_ids | {t["id"] for t in tickets}
    state["notified_ids"] = list(new_ids)[-500:]
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
