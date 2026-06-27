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
FIFA_LOGIN_URL = "https://tickets.fifa.com/en/login"
STUBHUB_SEARCH_URL = "https://www.stubhub.com/search?q=FIFA+World+Cup+2026"
STATE_FILE = Path("state.json")

EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_TO = os.environ.get("EMAIL_TO", "charlesrondos@yahoo.com")
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]

FIFA_EMAIL = os.environ.get("FIFA_EMAIL", "")
FIFA_PASSWORD = os.environ.get("FIFA_PASSWORD", "")
DEBUG_MODE = os.environ.get("DEBUG_MODE", "").lower() in ("1", "true", "yes")

COOLDOWN_SECONDS = 1800


# ── Venue matching ────────────────────────────────────────────────────────────
def is_target_venue(text: str) -> bool:
    return any(v in text.lower() for v in TARGET_VENUES)


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


# ── Debug helper ──────────────────────────────────────────────────────────────
def _debug_structure(obj, depth=0):
    """Compact structural summary for debugging — shows keys/types without values."""
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        return {k: _debug_structure(v, depth + 1) for k, v in list(obj.items())[:10]}
    if isinstance(obj, list):
        if not obj:
            return []
        return [_debug_structure(obj[0], depth + 1), f"...({len(obj)} items)"]
    return f"{type(obj).__name__}={repr(obj)[:40]}"


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
        lines += [
            f"  [{t.get('source', '?').upper()}]",
            f"  Match : {t.get('match', 'Unknown')}",
            f"  Venue : {t.get('venue', 'Unknown')}",
            f"  Date  : {t.get('date', 'TBD')}",
            f"  Price : ${t.get('price', '?')}",
            f"  Link  : {t.get('url', FIFA_BASE_URL)}",
            "",
        ]
    lines += [
        f"FIFA official: {FIFA_BASE_URL}",
        f"StubHub:       {STUBHUB_SEARCH_URL}",
        f"\n(Alert sent {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
    ]

    msg.attach(MIMEText("\n".join(lines), "plain"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.mail.yahoo.com", 465, context=ctx) as server:
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"Alert email sent to {EMAIL_TO}")


# ── Generic JSON extractor ────────────────────────────────────────────────────
_PRICE_KEYS = {"price", "Price", "amount", "Amount", "faceValue", "basePrice",
               "totalPrice", "cost", "listingPrice", "currentPrice", "minPrice",
               "ticketLow", "priceFrom", "lowestPrice"}
_VENUE_KEYS = {"venue", "Venue", "stadium", "Stadium", "location", "Location",
               "venueName", "venueeName"}
_MATCH_KEYS = {"match", "matchName", "title", "name", "eventName", "description",
               "event", "eventTitle"}
_DATE_KEYS  = {"date", "startDate", "dateTime", "kickoff", "matchDate",
               "eventDate", "eventDateLocal", "performanceDate"}
_LINK_KEYS  = {"url", "link", "purchaseUrl", "ticketUrl", "href", "eventUrl", "listingUrl"}


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
        # Also handle nested venue objects: {"venue": {"name": "MetLife"}}
        if venue_val is None and "venue" in obj and isinstance(obj["venue"], dict):
            venue_val = obj["venue"].get("name") or obj["venue"].get("venueName")
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


# ── StubHub-specific extractor ────────────────────────────────────────────────
def _extract_stubhub(obj, depth=0) -> list[dict]:
    """
    Handles StubHub's event-level structure where venue and price
    live at different nesting levels within the same event object.
    """
    if depth > 8:
        return []
    results = []
    if isinstance(obj, list):
        for item in obj:
            results.extend(_extract_stubhub(item, depth + 1))
    elif isinstance(obj, dict):
        # Resolve venue — direct string or nested {"venue": {"name": ...}}
        venue_val = next((str(obj[k]) for k in ["venueName", "venue_name"] if k in obj and isinstance(obj[k], str)), None)
        if venue_val is None and "venue" in obj:
            v = obj["venue"]
            venue_val = v if isinstance(v, str) else (v.get("name") or v.get("venueName") if isinstance(v, dict) else None)

        if venue_val and is_target_venue(venue_val):
            # Resolve price — direct or nested in ticketInfo / priceRange
            price_val = next((obj[k] for k in ["ticketLow", "minPrice", "priceFrom",
                                                 "lowestPrice", "price", "minTicketPrice"] if k in obj), None)
            if price_val is None and isinstance(obj.get("ticketInfo"), dict):
                ti = obj["ticketInfo"]
                price_val = ti.get("minPrice") or ti.get("lowPrice") or ti.get("price")
            if price_val is None and isinstance(obj.get("priceRange"), dict):
                pr = obj["priceRange"]
                price_val = pr.get("min") or pr.get("low") or pr.get("minimum")

            if price_val is not None:
                try:
                    price = float(str(price_val).replace("$", "").replace(",", "").strip())
                    if 0 < price < MAX_PRICE_USD:
                        name = next((str(obj[k]) for k in ["name", "title", "eventName",
                                                             "description", "eventTitle"] if k in obj),
                                    "FIFA World Cup 2026")
                        date = next((str(obj[k]) for k in ["eventDateLocal", "startDate",
                                                             "dateTime", "date", "performanceDate"] if k in obj), "TBD")
                        link = next((str(obj[k]) for k in ["eventUrl", "url", "link", "href"] if k in obj),
                                    STUBHUB_SEARCH_URL)
                        results.append({
                            "id": f"StubHub:{obj.get('id', f'{venue_val}_{price}')}",
                            "source": "StubHub",
                            "price": price,
                            "venue": venue_val,
                            "match": name,
                            "date": date,
                            "url": link if link.startswith("http") else STUBHUB_SEARCH_URL,
                        })
                except (ValueError, TypeError):
                    pass

        for v in obj.values():
            if isinstance(v, (dict, list)):
                results.extend(_extract_stubhub(v, depth + 1))
    return results


# ── Browser helper ────────────────────────────────────────────────────────────
def _browser_args():
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


# ── FIFA login ────────────────────────────────────────────────────────────────
async def _fifa_login(page) -> bool:
    if not FIFA_EMAIL or not FIFA_PASSWORD:
        print("FIFA: no credentials provided, skipping login")
        return False
    try:
        print("FIFA: navigating to login page...")
        await page.goto(FIFA_LOGIN_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        if DEBUG_MODE:
            await page.screenshot(path="fifa_login_before.png")
            print(f"FIFA: login page URL = {page.url}")
            print(f"FIFA: page title = {await page.title()}")

        # Step 1 — email field (handles both single-page and two-step flows)
        email_filled = False
        for sel in ["input[type='email']", "input[name='email']",
                    "input[id*='email']", "input[placeholder*='email' i]",
                    "input[autocomplete='email']"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.fill(FIFA_EMAIL)
                    email_filled = True
                    print(f"FIFA: filled email using selector '{sel}'")
                    break
            except Exception:
                pass

        if not email_filled:
            print("FIFA: could not find email field")

        # Some flows show email → Continue → then password on next screen
        for sel in ["button:has-text('Continue')", "button:has-text('Next')",
                    "button[type='submit']:not(:has-text('Sign'))"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    await el.click()
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                pass

        # Step 2 — password field
        pw_filled = False
        for sel in ["input[type='password']", "input[name='password']",
                    "input[id*='password']", "input[autocomplete='current-password']"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=3000):
                    await el.fill(FIFA_PASSWORD)
                    pw_filled = True
                    print(f"FIFA: filled password using selector '{sel}'")
                    break
            except Exception:
                pass

        if not pw_filled:
            print("FIFA: could not find password field")

        # Submit
        for sel in ["button[type='submit']", "button:has-text('Sign in')",
                    "button:has-text('Log in')", "button:has-text('Login')",
                    "button:has-text('Continue')"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    await page.wait_for_timeout(5000)
                    break
            except Exception:
                pass

        if DEBUG_MODE:
            await page.screenshot(path="fifa_login_after.png")
            print(f"FIFA: post-login URL = {page.url}")

        if "login" not in page.url.lower() and "signin" not in page.url.lower():
            print("FIFA: login successful")
            return True

        print(f"FIFA: login failed — still on {page.url}")
        return False

    except Exception as e:
        print(f"FIFA: login error: {e}")
        return False


# ── FIFA scraper ──────────────────────────────────────────────────────────────
async def scrape_fifa() -> list[dict]:
    captured = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(**_browser_args())
        page = await ctx.new_page()

        async def on_response(response):
            if response.status == 200 and "json" in response.headers.get("content-type", ""):
                try:
                    captured.append({"url": response.url, "data": await response.json()})
                except Exception:
                    pass

        page.on("response", on_response)
        await _fifa_login(page)

        try:
            print("FIFA: loading ticket listings...")
            await page.goto(FIFA_BASE_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(4000)
        except Exception as e:
            print(f"FIFA: page load warning: {e}")

        for sel in ["a[href*='resale']", "a[href*='ticket']", "a[href*='match']",
                    "button:has-text('Tickets')", "a:has-text('Buy Tickets')",
                    "a:has-text('Resale')"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    await el.click()
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                pass

        await browser.close()

    print(f"FIFA: captured {len(captured)} API responses")
    if DEBUG_MODE:
        for r in captured:
            print(f"  {r['url']}")
            print(f"  {json.dumps(_debug_structure(r['data']), indent=2)[:400]}\n")

    found = []
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
        ctx = await browser.new_context(**_browser_args())
        page = await ctx.new_page()

        async def on_response(response):
            if response.status == 200 and "json" in response.headers.get("content-type", ""):
                if "stubhub" in response.url:
                    try:
                        captured.append({"url": response.url, "data": await response.json()})
                    except Exception:
                        pass

        page.on("response", on_response)

        try:
            print("StubHub: loading search page...")
            await page.goto(STUBHUB_SEARCH_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(5000)

            for sel in ["a:has-text('FIFA World Cup')", "a:has-text('World Cup 2026')",
                        "[data-testid='event-card']"]:
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

    print(f"StubHub: captured {len(captured)} API responses")
    if DEBUG_MODE:
        for r in captured:
            print(f"  {r['url']}")
            print(f"  {json.dumps(_debug_structure(r['data']), indent=2)[:400]}\n")

    found = []
    for resp in captured:
        # Try StubHub-specific extractor first, fall back to generic
        specific = _extract_stubhub(resp["data"])
        generic = _extract(resp["data"], "StubHub", STUBHUB_SEARCH_URL)
        seen_ids = {t["id"] for t in specific}
        combined = specific + [t for t in generic if t["id"] not in seen_ids]
        for ticket in combined:
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
        "url": STUBHUB_SEARCH_URL,
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
