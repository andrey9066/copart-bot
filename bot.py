import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

from playwright.async_api import async_playwright
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("copart-bot")

DATA_DIR = Path(__file__).parent / "data"
SEEN_FILE = DATA_DIR / "seen_listings.json"
COOKIES_FILE = DATA_DIR / "cookies.json"

COPART_BASE = "https://www.copart.com/lot/"
COPART_SEARCH = "https://www.copart.com/public/lots/search-results"

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
delete navigator.__proto__.webdriver;
window.chrome = { runtime: {}, csi: function(){}, loadTimes: function(){} };
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
    Promise.resolve({ state: Notification.permission }) :
    originalQuery(parameters)
);
"""

SEARCH_PAYLOAD = {
    "query": ["alfa romeo giulia"],
    "filter": {},
    "sort": [
        "salelight_priority asc",
        "member_damage_group_priority asc",
        "auction_date_type desc",
        "auction_date_utc asc",
    ],
    "page": 0,
    "size": 24,
    "start": 0,
    "watchListOnly": False,
    "freeFormSearch": True,
    "hideImages": False,
    "defaultSort": False,
    "specificRowProvided": False,
    "displayName": "",
    "searchName": "",
    "backUrl": "",
    "includeTagByField": {},
    "rawParams": {},
}


def load_seen() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict):
    DATA_DIR.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def parse_auction_date(ts_ms: int, tz_str: str) -> str:
    if not ts_ms:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except:
        return str(ts_ms)


def format_listing(item: dict) -> str:
    lot = item.get("lotNumberStr", "")
    make = item.get("mkn", "")
    model = item.get("mmod", "")
    year = item.get("lcy", "")
    color = item.get("clr", "")
    damage = item.get("dd", "")
    condition = item.get("lcd", "")
    odometer = item.get("orr", "")
    engine = item.get("egn", "")
    fuel = item.get("ft", "")
    transmission = item.get("tmtp", "")
    drive = item.get("drv", "")
    title = item.get("tgd", "")
    state = item.get("ts", "")
    yard = item.get("yn", "")
    auction_ts = item.get("ad", "")
    auction_tz = item.get("tz", "")
    currency = item.get("cuc", "USD")
    current_bid = item.get("dynamicLotDetails", {}).get("currentBid", 0)
    image_url = item.get("tims", "")
    hot_flags = item.get("lfd", [])
    tags = item.get("lic", [])

    auction_date = parse_auction_date(auction_ts, auction_tz)

    parts = [f"*{year} {make} {model}*"]
    if color:
        parts.append(f"Color: {color}")
    if engine:
        parts.append(f"Engine: {engine}")
    if transmission:
        parts.append(f"Trans: {transmission}")
    if drive:
        parts.append(f"Drive: {drive}")
    if fuel:
        parts.append(f"Fuel: {fuel}")
    if odometer:
        parts.append(f"Odometer: {odometer:,} mi")
    if damage:
        parts.append(f"Damage: {damage}")
    if condition:
        parts.append(f"Condition: {condition}")
    if title:
        parts.append(f"Title: {title}")
    if current_bid:
        parts.append(f"Current bid: ${current_bid:,} {currency}")
    if yard:
        parts.append(f"Location: {yard}")
    if auction_date:
        parts.append(f"Auction: {auction_date}")
    if hot_flags:
        parts.append(f"Flags: {', '.join(hot_flags)}")

    parts.append(f"\n[View on Copart]({COPART_BASE}{lot})")
    return "\n".join(parts)


async def fetch_listings_via_browser():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
            ],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = await context.new_page()
        await page.add_init_script(STEALTH_JS)

        log.info("Loading Copart homepage...")
        await page.goto("https://www.copart.com/", wait_until="domcontentloaded", timeout=60000)
        for i in range(20):
            await asyncio.sleep(2)
            t = await page.title()
            if "copart" in t.lower() or "auction" in t.lower():
                log.info("Homepage loaded")
                break

        await asyncio.sleep(2)

        all_listings = []
        page_num = 0
        page_size = 24

        while True:
            payload = dict(SEARCH_PAYLOAD)
            payload["page"] = page_num
            payload["start"] = page_num * page_size

            log.info("Fetching page %d...", page_num)
            result = await page.evaluate("""async (payload) => {
                const resp = await fetch('/public/lots/search-results', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
                    body: JSON.stringify(payload),
                });
                return await resp.json();
            }""", payload)

            content = result.get("data", {}).get("results", {}).get("content", [])
            total = result.get("data", {}).get("results", {}).get("totalElements", 0)

            if not content:
                break

            all_listings.extend(content)
            log.info("Got %d items (total so far: %d/%d)", len(content), len(all_listings), total)

            if len(all_listings) >= total or len(content) < page_size:
                break

            page_num += 1
            await asyncio.sleep(1)

        await browser.close()
        return all_listings


async def check_new_listings(bot: Bot, chat_id: str):
    seen = load_seen()

    try:
        listings = await fetch_listings_via_browser()
    except Exception as e:
        log.error("Failed to fetch listings: %s", e)
        return

    if not listings:
        log.info("No listings found")
        return

    new_listings = [item for item in listings if item.get("lotNumberStr") not in seen]
    if not new_listings:
        log.info("No new listings")
        return

    log.info("Found %d new listings!", len(new_listings))
    for item in new_listings:
        lot = item.get("lotNumberStr", "")
        text = format_listing(item)
        image_url = item.get("tims", "")

        try:
            if image_url:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=image_url,
                    caption=text,
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                )
        except Exception as e:
            log.error("Send failed for lot %s: %s", lot, e)
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
            except Exception as e2:
                log.error("Retry also failed: %s", e2)

        seen[lot] = datetime.utcnow().isoformat()
        await asyncio.sleep(0.5)

    save_seen(seen)


async def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    interval = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))

    if not token or not chat_id:
        log.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return

    bot = Bot(token=token)
    log.info("Bot started. Checking every %d minutes", interval)

    await bot.send_message(
        chat_id=chat_id,
        text=f"Copart monitor started!\n"
             f"Search: Alfa Romeo Giulia\n"
             f"Check interval: {interval} min",
    )

    await check_new_listings(bot, chat_id)

    while True:
        await asyncio.sleep(interval * 60)
        try:
            await check_new_listings(bot, chat_id)
        except Exception as e:
            log.error("Check failed: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
