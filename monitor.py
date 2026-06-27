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
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

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
STUBHUB_SEARCH_URL = "https://www.stubhub.com/search?q=FIFA+World+Cup+2026"
STUBHUB_VENUE_URLS = {
    "MetLife Stadium":        "https://www.stubhub.com/metlife-stadium-east-rutherford-tickets/",
    "Gillette Stadium":       "https://www.stubhub.com/gillette-stadium-foxborough-tickets/",
    "Lincoln Financial Field":"https://www.stubhub.com/lincoln-financial-field-philadelphia-tickets/",
}
STATE_FILE = Path("state.json")

EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_TO = os.environ.get("EMAIL_TO", "charlesrondos@yahoo.com")
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]

FIFA_EMAIL = os.environ.get("FIFA_EMAIL", "")
FIFA_PASSWORD = os.environ.get("FIFA_PASSWORD", "")
DEBUG_MODE = os.environ.get("DEBUG_MODE", "").lower() in ("1", "true", "yes")

COOLDOWN_SECONDS = 1800


# ── Helpers ───────────────────────────────────────────────────────────────────
def is_target_venue(text: str) -> bool:
    return any(v in text.lower() for v in TARGET_VENUES)


def parse_price(text: str) -> float | None:
    """Extract the lowest dollar amount from a string like 'from $450' or '$1,200'."""
    matches = re.findall(r'\$\s*([\d,]+(?:\.\d{1,2})?)', text)
    prices = []
    for m in matches:
        try:
            prices.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return min(prices) if prices else None


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


# ── FIFA scraper (Firefox — less fingerprinted than Chromium) ─────────────────
async def scrape_fifa() -> list[dict]:
    captured = []

    async with async_playwright() as pw:
        # Firefox is far less likely to get "Bad request" from anti-bot systems
        browser = await pw.firefox.launch(headless=True)
        ctx = await browser.new_context(**_browser_args())
        page = await ctx.new_page()

        async def on_response(response):
            if response.status == 200 and "json" in response.headers.get("content-type", ""):
                try:
                    captured.append({"url": response.url, "data": await response.json()})
                except Exception:
                    pass

        page.on("response", on_response)

        # Load homepage first — direct /login URL was returning "Bad request"
        try:
            print("FIFA: loading homepage via Firefox...")
            await page.goto(FIFA_BASE_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(3000)
            print(f"FIFA: homepage title = {await page.title()}")
        except Exception as e:
            print(f"FIFA: homepage warning: {e}")

        if DEBUG_MODE:
            await page.screenshot(path="fifa_homepage.png")

        # Sign in if credentials supplied
        if FIFA_EMAIL and FIFA_PASSWORD:
            print("FIFA: looking for sign-in button...")
            for sel in [
                "a:has-text('Sign in')", "button:has-text('Sign in')",
                "a:has-text('Login')",   "button:has-text('Login')",
                "a:has-text('Log in')",  "button:has-text('Log in')",
                "a[href*='login']",      "a[href*='signin']",
                "[data-testid*='login']","[aria-label*='sign' i]",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1500):
                        await el.click()
                        await page.wait_for_timeout(3000)
                        print(f"FIFA: clicked sign-in, now at {page.url}")
                        break
                except Exception:
                    pass

            if DEBUG_MODE:
                await page.screenshot(path="fifa_login_before.png")
                print(f"FIFA: login page title = {await page.title()}")

            # Email — handles single-step and two-step flows
            for sel in ["input[type='email']", "input[name='email']",
                        "input[autocomplete='email']", "input[autocomplete='username']",
                        "input[id*='email' i]", "input[placeholder*='email' i]"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        await el.fill(FIFA_EMAIL)
                        print(f"FIFA: filled email via '{sel}'")
                        break
                except Exception:
                    pass

            for sel in ["button:has-text('Continue')", "button:has-text('Next')"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1500):
                        await el.click()
                        await page.wait_for_timeout(2000)
                        break
                except Exception:
                    pass

            for sel in ["input[type='password']", "input[name='password']",
                        "input[autocomplete='current-password']", "input[id*='password' i]"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3000):
                        await el.fill(FIFA_PASSWORD)
                        print(f"FIFA: filled password via '{sel}'")
                        break
                except Exception:
                    pass

            for sel in ["button[type='submit']", "button:has-text('Sign in')",
                        "button:has-text('Log in')", "button:has-text('Continue')"]:
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
                print(f"FIFA: post-login url = {page.url}")

            if "login" not in page.url.lower() and "signin" not in page.url.lower():
                print("FIFA: login successful")
            else:
                print(f"FIFA: login may have failed, still at {page.url}")

        # Browse ticket/resale listings
        try:
            await page.goto(FIFA_BASE_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(4000)
        except Exception as e:
            print(f"FIFA: ticket page warning: {e}")

        for sel in ["a[href*='resale']", "a[href*='ticket']", "a[href*='match']",
                    "a:has-text('Resale')", "a:has-text('Buy Tickets')"]:
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
    found = []
    for resp in captured:
        for ticket in _extract_json(resp["data"], "FIFA", FIFA_BASE_URL):
            if is_target_venue(ticket["venue"] + " " + ticket["match"]) and 0 < ticket["price"] < MAX_PRICE_USD:
                found.append(ticket)
    print(f"FIFA: {len(found)} matching ticket(s)")
    return found


# ── StubHub scraper (venue pages + full-text extraction) ─────────────────────
async def _scrape_stubhub_venue(pw, venue_name: str, url: str) -> list[dict]:
    ctx = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
    )
    browser = ctx
    bctx = await browser.new_context(**_browser_args())
    page = await bctx.new_page()
    await stealth_async(page)

    found = []
    try:
        print(f"StubHub: loading {venue_name} page...")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # Dismiss cookie/consent banners
        for sel in ["button:has-text('Accept All')", "button:has-text('Accept')",
                    "#onetrust-accept-btn-handler", "[data-testid*='accept']",
                    "button:has-text('I Accept')"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    await page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        # Wait for JS to render + scroll to trigger lazy loading
        await page.wait_for_timeout(6000)
        await page.evaluate("window.scrollTo(0, 800)")
        await page.wait_for_timeout(2000)

        if DEBUG_MODE:
            await page.screenshot(path=f"stubhub_{venue_name.replace(' ', '_')}.png")

        # Strategy 1: full page text scan for FIFA events with prices
        body = await page.inner_text("body")
        if DEBUG_MODE:
            print(f"StubHub {venue_name}: body length={len(body)}")
            for line in body.splitlines():
                if any(k in line.lower() for k in ["fifa", "world cup", "$"]):
                    print(f"  >> {line[:120]}")

        lines = [l.strip() for l in body.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if not ("fifa" in line.lower() or "world cup" in line.lower()):
                continue
            context_lines = lines[max(0, i - 3): i + 6]
            context = " ".join(context_lines)
            price = parse_price(context)
            if price and 0 < price < MAX_PRICE_USD:
                date_match = re.search(
                    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}', context)
                found.append({
                    "id": f"StubHub:{venue_name}:{line[:50]}:{price}",
                    "source": "StubHub",
                    "price": price,
                    "venue": venue_name,
                    "match": line[:100],
                    "date": date_match.group(0) if date_match else "See link",
                    "url": url,
                })

        # Strategy 2: anchor links with price text
        links = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({ href: a.href, text: (a.innerText || '').trim() }))
                .filter(x => x.text.length > 10 && x.text.includes('$'))
        """)
        for link in links:
            text = link["text"]
            if "fifa" not in text.lower() and "world cup" not in text.lower():
                continue
            price = parse_price(text)
            if price and 0 < price < MAX_PRICE_USD:
                first_line = next((l for l in text.splitlines() if l.strip()), text)[:100]
                found.append({
                    "id": f"StubHub:{link['href'][:80]}:{price}",
                    "source": "StubHub",
                    "price": price,
                    "venue": venue_name,
                    "match": first_line,
                    "date": "See link",
                    "url": link["href"] if link["href"].startswith("http") else url,
                })

    except Exception as e:
        print(f"StubHub {venue_name}: error: {e}")
    finally:
        await bctx.close()
        await browser.close()

    return found


async def scrape_stubhub() -> list[dict]:
    async with async_playwright() as pw:
        results = await asyncio.gather(*[
            _scrape_stubhub_venue(pw, venue_name, url)
            for venue_name, url in STUBHUB_VENUE_URLS.items()
        ])

    # Flatten and deduplicate
    seen = set()
    found = []
    for batch in results:
        for t in batch:
            if t["id"] not in seen:
                seen.add(t["id"])
                found.append(t)

    print(f"StubHub: {len(found)} matching ticket(s)")
    return found


# ── Generic JSON extractor (for FIFA API responses) ───────────────────────────
_PRICE_KEYS = {"price", "Price", "amount", "faceValue", "basePrice", "totalPrice",
               "cost", "listingPrice", "currentPrice", "minPrice", "ticketLow"}
_VENUE_KEYS = {"venue", "Venue", "stadium", "Stadium", "location", "venueName"}
_MATCH_KEYS = {"match", "matchName", "title", "name", "eventName", "description", "eventTitle"}
_DATE_KEYS  = {"date", "startDate", "dateTime", "kickoff", "matchDate", "eventDateLocal"}
_LINK_KEYS  = {"url", "link", "purchaseUrl", "ticketUrl", "eventUrl", "listingUrl"}


def _extract_json(obj, source: str, fallback_url: str, depth=0) -> list[dict]:
    if depth > 12:
        return []
    results = []
    if isinstance(obj, list):
        for item in obj:
            results.extend(_extract_json(item, source, fallback_url, depth + 1))
    elif isinstance(obj, dict):
        price_val = next((obj[k] for k in _PRICE_KEYS if k in obj), None)
        venue_val = next((obj[k] for k in _VENUE_KEYS if k in obj), None)
        if venue_val is None and isinstance(obj.get("venue"), dict):
            venue_val = obj["venue"].get("name") or obj["venue"].get("venueName")
        if price_val is not None and venue_val is not None:
            try:
                price = float(str(price_val).replace("$", "").replace(",", "").strip())
                ticket_id = f"{source}:{obj.get('id') or obj.get('ticketId') or f'{venue_val}_{price}'}"
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
                results.extend(_extract_json(v, source, fallback_url, depth + 1))
    return results


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
