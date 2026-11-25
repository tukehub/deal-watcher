import asyncio
import html
import json
import os
import re
from collections import defaultdict
from urllib.parse import urljoin
import httpx
from playwright.async_api import async_playwright

DEFAULT_MIN_DISCOUNT = 50
INSANE_DISCOUNT = 80
NON_TECH_MAX_EUR = 50.0
ALL_PER_EUR = 98.0
STATE_FILE = "seen_deals.json"

WISHLIST = [
    # Android / Streaming TV Boxes
    "android box", "tv box", "mi box", "xiaomi tv", "google tv",
    "chromecast", "nvidia shield", "fire tv", "apple tv", "streaming",
    # Desk, Camera & Lab Gear
    "nvme", "ulanzi", "nd filter", "monitor", "shelly", "ups",
    "scala1", "scala2", "gan charger",
]

EXCLUDED_KEYWORDS = [
    "lavatrice", "lavatriçe", "rrobalarëse", "rrobalarese", "washing machine", "πλυντήριο",
    "tharëse", "tharese", "dryer", "στεγνωτήριο",
    "frigorifer", "ngrirës", "ngrires", "fridge", "refrigerator", "ψυγείο",
    "furre", "furrë", "oven", "φούρνος", "pianure", "pianurë", "hob", "εστίες",
    "enëlarëse", "enelarese", "dishwasher",
    "kondicioner", "inverter", "btu", "air conditioner", "κλιματιστικό",
    "fshese", "fshesë", "thithese", "thithëse", "vacuum", "σκούπα",
    "hekur", "iron", "σίδερο",
    "ekspres", "kafe", "coffee", "καφετιέρα", "air fryer", "mikrovale", "mikrovalë", "microwave",
    "tenxhere", "tigan", "tefal", "kuzhin", "cookware", "blender", "mikser", "toster",
]

TARGETS = [
    # --- ALBANIA (40% upwards) ---
    {"store": "Vodafone AL", "url": "https://eshop.vodafone.al/en/special-offer", "card_selector": "a[href*='/en/']", "min_discount": 40},
    {"store": "One AL", "url": "https://www.one.al/sq/individi/e-shop/e_shop/telefona", "card_selector": "[class*='product'], .card, article", "min_discount": 40},
    {"store": "Neptun AL", "url": "https://neptun.al/ns-newsarticle-black-friday-oferta-2026", "card_selector": "[class*='product'], .product-item, article", "min_discount": 40},
    {"store": "Neptun Gaming", "url": "https://www.neptun.al/ns-newsarticle-black-friday-gaming", "card_selector": "[class*='product'], .product-item, article", "min_discount": 40},
    {"store": "Globe AL", "url": "https://globe.al/black-friday", "card_selector": ".product-miniature, .product-item, [class*='product']", "min_discount": 40},
    {"store": "Foleja AL", "url": "https://www.foleja.al/", "card_selector": ".product-box, .card-body, [class*='product']", "min_discount": 40},

    # --- KOSOVO (50% upwards) ---
    {"store": "Gjirafa50 KS", "url": "https://gjirafa50.com/te-gjitha-ne-zbritje-2026", "card_selector": ".product-item, [class*='product-box'], article", "min_discount": 50},
    {"store": "Neptun KS", "url": "https://www.neptun-ks.com/", "card_selector": "[class*='product'], .product-item, article", "min_discount": 50},
    {"store": "Aztech KS", "url": "https://aztechonline.com/offers", "card_selector": "[class*='product'], .product-item, article", "min_discount": 50},

    # --- GREECE (60% upwards) ---
    {"store": "Plaisio GR", "url": "https://www.plaisio.gr/list/statusFlags--BlackFriday", "card_selector": "[class*='product'], .product-item, article", "min_discount": 60},
    {"store": "BestPrice GR", "url": "https://www.bestprice.gr/black-friday", "card_selector": "[class*='product'], .p-item, article", "min_discount": 60},
    {"store": "Public GR", "url": "https://www.public.gr/root/black-friday", "card_selector": "[class*='product'], .product-card, article", "min_discount": 60},
]

ELECTRONICS_KEYWORDS = [
    "laptop", "macbook", "monitor", "ssd", "nvme", "ram", "gpu", "rtx",
    "phone", "celular", "iphone", "samsung", "galaxy", "xiaomi", "redmi", "κινητό",
    "tablet", "ipad", "watch", "tv", "televizor", "oled", "qled", "uhd", "τηλεόραση",
    "android box", "tv box", "mi box", "google tv", "chromecast", "shield", "fire tv", "apple tv", "streaming",
    "audio", "speaker", "boks", "jbl", "sony", "headphone", "kufje", "airpods", "ακουστικά",
    "ps5", "playstation", "nintendo", "switch", "gaming", "mouse", "tastier", "keyboard",
    "router", "wi-fi", "wifi", "camera", "kamera", "lens", "lente",
    "charger", "karikues", "usb", "hdmi", "ups", "shelly", "smart", "box", "powerbank",
]

UI_NOISE_PATTERNS = re.compile(
    r"^(black\s*friday|go\s*for\s*more|σύγκρινέ\s*το|προσθήκη.*|παράδοση.*|διαθέσιμο|μη\s*διαθέσιμο|κωδ\.\s*πλαίσιο.*|δωροεπιταγή.*)$",
    re.IGNORECASE,
)


def is_excluded(text: str, url: str) -> bool:
    haystack = f"{text} {url}".lower()
    return any(bad in haystack for bad in EXCLUDED_KEYWORDS)


def is_tech(title: str, url: str) -> bool:
    haystack = f"{title} {url}".lower()
    return any(kw in haystack for kw in ELECTRONICS_KEYWORDS)


def parse_number(raw: str) -> float | None:
    cleaned = raw.strip()
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) == 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned:
        parts = cleaned.split(".")
        if len(parts[-1]) == 3:
            cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def strip_savings_and_installments(text: str) -> str:
    cleaned = re.sub(
        r"(?:κέρδος|έκπτωση|όφελος|kurseni|zbritje|save)\s*:?\s*[\d.,]+\s*(?:ALL|Lekë|L|€|EUR)?",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"-\s*[\d.,]+\s*(?:ALL|Lekë|€|EUR)",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"[\d.,]+\s*(?:ALL|Lekë|€|EUR)\s*/\s*(?:μήνα|μηνα|muaj|month)",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def parse_prices_and_eur(text: str) -> tuple[str, float | None, list[float]]:
    sanitized = strip_savings_and_installments(text)
    matches = re.findall(
        r"(\d[\d.,]*)\s*(ALL|Lekë|L|€|EUR)|(?:€|EUR)\s*(\d[\d.,]*)",
        sanitized,
        re.IGNORECASE,
    )
    parsed = []
    for num1, curr, num2 in matches:
        raw_num = num1 or num2
        currency = (curr or "€").upper()
        val = parse_number(raw_num)
        if val is None or val <= 0:
            continue
        is_all = currency in {"ALL", "LEKË", "L"}
        eur_val = (val / ALL_PER_EUR) if is_all else val
        label = f"{int(val):,} ALL" if is_all else f"€{val:,.2f}"
        parsed.append((val, eur_val, label))

    if not parsed:
        return "", None, []

    lowest = min(parsed, key=lambda x: x[1])
    raw_values = [p[0] for p in parsed]
    return lowest[2], lowest[1], raw_values


def extract_discount(text: str, raw_prices: list[float]) -> int:
    pct_matches = re.findall(r"(?<!\d)(\d{2})(?:[.,]\d+)?\s*%", text)
    valid = [int(m) for m in pct_matches if 5 <= int(m) <= 95]
    if valid:
        return max(valid)

    if len(raw_prices) >= 2:
        high, low = max(raw_prices), min(raw_prices)
        if high > low > 0:
            calc = round(((high - low) / high) * 100)
            if 5 <= calc <= 95:
                return calc
    return 0


def classify_bucket(deal: dict) -> str:
    if deal["discount"] >= INSANE_DISCOUNT:
        return "🚨 INSANE DEALS (-80%+)"
    if deal["wishlist"]:
        return "🎯 WISHLIST & TV BOXES"
    if deal["is_tech"]:
        return "💻 ELECTRONICS & TECH"
    return "📦 OTHER GEAR (≤ €50)"


def load_seen() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(data: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


async def post_telegram_text(client: httpx.AsyncClient, token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = await client.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )
    if resp.status_code >= 400:
        print(f"Telegram API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()


async def send_telegram(grouped_deals: dict[str, list[dict]]) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secrets not set; skipping push notification.")
        return

    bucket_order = [
        "🚨 INSANE DEALS (-80%+)",
        "🎯 WISHLIST & TV BOXES",
        "💻 ELECTRONICS & TECH",
        "📦 OTHER GEAR (≤ €50)",
    ]

    async with httpx.AsyncClient(timeout=20) as client:
        for bucket in bucket_order:
            items = grouped_deals.get(bucket, [])
            if not items:
                continue

            items.sort(key=lambda x: x["discount"], reverse=True)
            safe_bucket = html.escape(bucket)
            lines = [f"<b>{safe_bucket} ({len(items)})</b>"]
            for d in items:
                badge = (
                    f"<b>-{d['discount']}%</b>"
                    if d["discount"] > 0
                    else f"<b>{html.escape(str(d['wishlist']))}</b>"
                )
                price = f" • <b>{html.escape(d['price'])}</b>" if d["price"] else ""
                safe_store = html.escape(d["store"])
                safe_title = html.escape(d["title"])
                safe_url = html.escape(d["url"], quote=True)
                lines.append(f'• {badge} | {safe_store} | <a href="{safe_url}">{safe_title}</a>{price}')

            chunk = ""
            for line in lines:
                if len(chunk) + len(line) + 1 > 3800:
                    await post_telegram_text(client, token, chat_id, chunk)
                    chunk = f"<b>{safe_bucket} (cont.)</b>\n{line}"
                else:
                    chunk = f"{chunk}\n{line}" if chunk else line
            if chunk:
                await post_telegram_text(client, token, chat_id, chunk)


async def scrape_store(page, target: dict) -> list[dict]:
    store = target["store"]
    base_url = target["url"]
    min_disc = target.get("min_discount", DEFAULT_MIN_DISCOUNT)
    print(f"Scanning {store} ({base_url}) [min -{min_disc}%]...")

    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4500)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        await page.wait_for_timeout(1500)
    except Exception as exc:
        print(f"Failed loading {store}: {exc}")
        return []

    raw_cards = await page.eval_on_selector_all(
        target["card_selector"],
        """els => els.map(el => {
            const anchor = el.tagName === 'A' ? el : el.querySelector('a[href]');
            return {
                text: (el.innerText || '').trim(),
                href: anchor ? anchor.getAttribute('href') : ''
            };
        })""",
    )

    deals = []
    seen_urls = set()
    for card in raw_cards:
        text = card.get("text", "")
        href = card.get("href", "")
        if not text or not href or href.startswith("#") or "javascript:" in href:
            continue

        full_url = urljoin(base_url, href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        if is_excluded(text, full_url):
            continue

        lines = [
            l.strip()
            for l in text.splitlines()
            if len(l.strip()) > 6
            and "%" not in l
            and "κέρδος" not in l.lower()
            and not UI_NOISE_PATTERNS.match(l.strip())
            and not re.match(r"^[\d.,\s]+(ALL|Lekë|L|€|EUR)?$", l.strip(), re.IGNORECASE)
        ]
        title = lines[0][:75] if lines else full_url.split("/")[-1][:75]

        price_label, price_eur, raw_prices = parse_prices_and_eur(text)
        discount = extract_discount(text, raw_prices)
        tech_flag = is_tech(f"{title} {text}", full_url)

        title_lower = f"{title} {full_url}".lower()
        wishlist_hit = next((w for w in WISHLIST if w in title_lower), None)

        if not tech_flag and not wishlist_hit:
            if price_eur is None or price_eur > NON_TECH_MAX_EUR:
                continue

        if discount >= min_disc or wishlist_hit:
            deals.append(
                {
                    "store": store,
                    "title": title,
                    "discount": discount,
                    "price": price_label,
                    "wishlist": wishlist_hit,
                    "is_tech": tech_flag,
                    "url": full_url,
                }
            )
    print(f"  -> Found {len(deals)} matching deals on {store} (from {len(raw_cards)} raw cards)")
    return deals


async def launch_browser(p):
    launch_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]
    if os.getenv("GITHUB_ACTIONS"):
        try:
            return await p.chromium.launch(channel="chrome", headless=True, args=launch_args)
        except Exception as exc:
            print(f"System chrome failed ({exc}), falling back to bundled chromium...")
    return await p.chromium.launch(headless=True, args=launch_args)


async def main() -> None:
    seen = load_seen()
    all_deals = []

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="el-GR",
            viewport={"width": 1366, "height": 900},
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()
        await page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font", "stylesheet"}
            else route.continue_(),
        )
        for target in TARGETS:
            all_deals.extend(await scrape_store(page, target))
        await browser.close()

    grouped_new = defaultdict(list)
    new_count = 0

    for d in all_deals:
        key = f"{d['store']}|{d['url']}|{d['discount']}"
        if key not in seen:
            seen[key] = True
            bucket = classify_bucket(d)
            grouped_new[bucket].append(d)
            new_count += 1

    if new_count:
        await send_telegram(grouped_new)
        save_seen(seen)
        print(f"\nSent & saved {new_count} new deals.")
    else:
        print("\nNo brand-new deals since last check.")


if __name__ == "__main__":
    asyncio.run(main())
