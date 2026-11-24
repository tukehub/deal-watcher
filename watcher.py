import asyncio
import json
import os
import re
from collections import defaultdict
from urllib.parse import urljoin
import httpx
from playwright.async_api import async_playwright

DEFAULT_MIN_DISCOUNT = 50
INSANE_DISCOUNT = 80
STATE_FILE = "seen_deals.json"

WISHLIST = [
    "xiaomi tv box",
    "nvme",
    "ulanzi",
    "nd filter",
    "monitor",
    "shelly",
    "ups",
    "scala1",
    "scala2",
    "gan charger",
]

# Drop bulky home/kitchen appliances across AL, EN, and GR
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
    # --- ALBANIA ---
    {"store": "Vodafone AL", "url": "https://eshop.vodafone.al/en/special-offer", "card_selector": "a[href*='/en/']", "min_discount": 40},
    {"store": "One AL", "url": "https://www.one.al/sq/individi/e-shop/e_shop/telefona", "card_selector": "[class*='product'], .card, article", "min_discount": 40},
    {"store": "Neptun AL", "url": "https://neptun.al/ns-newsarticle-black-friday-oferta-2026", "card_selector": "[class*='product'], .product-item, article", "min_discount": 50},
    {"store": "Neptun Gaming", "url": "https://www.neptun.al/ns-newsarticle-black-friday-gaming", "card_selector": "[class*='product'], .product-item, article", "min_discount": 45},
    {"store": "Globe AL", "url": "https://globe.al/black-friday", "card_selector": ".product-miniature, .product-item, [class*='product']", "min_discount": 50},
    {"store": "Foleja AL", "url": "https://www.foleja.al/", "card_selector": ".product-box, .card-body, [class*='product']", "min_discount": 50},

    # --- KOSOVO ---
    {"store": "Gjirafa50 KS", "url": "https://gjirafa50.com/te-gjitha-ne-zbritje-2026", "card_selector": ".product-item, [class*='product-box'], article", "min_discount": 50},
    {"store": "Neptun KS", "url": "https://www.neptun-ks.com/", "card_selector": "[class*='product'], .product-item, article", "min_discount": 50},
    {"store": "Aztech KS", "url": "https://aztechonline.com/offers", "card_selector": "[class*='product'], .product-item, article", "min_discount": 50},

    # --- GREECE ---
    {"store": "Public GR", "url": "https://www.public.gr/root/black-friday", "card_selector": "[class*='product'], .product-card, article", "min_discount": 50},
    {"store": "Plaisio GR", "url": "https://www.plaisio.gr/campaign/blackfriday", "card_selector": "[class*='product'], .product-item, article", "min_discount": 50},
]

ELECTRONICS_KEYWORDS = [
    "laptop", "macbook", "monitor", "ssd", "nvme", "ram", "gpu", "rtx",
    "phone", "celular", "iphone", "samsung", "galaxy", "xiaomi", "redmi", "κινητό",
    "tablet", "ipad", "watch", "tv", "televizor", "oled", "qled", "uhd", "τηλεόραση",
    "audio", "speaker", "boks", "jbl", "sony", "headphone", "kufje", "airpods", "ακουστικά",
    "ps5", "playstation", "nintendo", "switch", "gaming", "mouse", "tastier", "keyboard",
    "router", "wi-fi", "wifi", "camera", "kamera", "lens", "lente",
    "charger", "karikues", "usb", "hdmi", "ups", "shelly", "smart", "box", "powerbank",
]


def is_excluded(text: str, url: str) -> bool:
    haystack = f"{text} {url}".lower()
    return any(bad in haystack for bad in EXCLUDED_KEYWORDS)


def classify_bucket(deal: dict) -> str:
    if deal["discount"] >= INSANE_DISCOUNT:
        return "🚨 INSANE DEALS (-80%+)"
    if deal["wishlist"]:
        return "🎯 WISHLIST HITS"
    haystack = f"{deal['title']} {deal['url']}".lower()
    if any(kw in haystack for kw in ELECTRONICS_KEYWORDS):
        return "💻 ELECTRONICS & TECH"
    return "📦 OTHER GEAR"


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
    resp.raise_for_status()


async def send_telegram(grouped_deals: dict[str, list[dict]]) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secrets not set; skipping push notification.")
        return

    bucket_order = [
        "🚨 INSANE DEALS (-80%+)",
        "🎯 WISHLIST HITS",
        "💻 ELECTRONICS & TECH",
        "📦 OTHER GEAR",
    ]

    async with httpx.AsyncClient(timeout=20) as client:
        for bucket in bucket_order:
            items = grouped_deals.get(bucket, [])
            if not items:
                continue

            # Sort highest discount first
            items.sort(key=lambda x: x["discount"], reverse=True)

            lines = [f"<b>{bucket} ({len(items)})</b>"]
            for d in items:
                badge = f"<b>-{d['discount']}%</b>" if d["discount"] > 0 else f"<b>{d['wishlist']}</b>"
                price = f" • <b>{d['price']}</b>" if d["price"] else ""
                lines.append(f"• {badge} | {d['store']} | <a href=\"{d['url']}\">{d['title']}</a>{price}")

            # Send each bucket cleanly (chunking only if a single bucket exceeds Telegram's 4096-char limit)
            chunk = ""
            for line in lines:
                if len(chunk) + len(line) + 1 > 3800:
                    await post_telegram_text(client, token, chat_id, chunk)
                    chunk = f"<b>{bucket} (cont.)</b>\n{line}"
                else:
                    chunk = f"{chunk}\n{line}" if chunk else line
            if chunk:
                await post_telegram_text(client, token, chat_id, chunk)


def extract_discount(text: str) -> int:
    matches = re.findall(r"(?<!\d)(\d{2})\s*%", text)
    valid = [int(m) for m in matches if 5 <= int(m) <= 95]
    return max(valid) if valid else 0


def extract_price(text: str) -> str:
    m = re.search(r"(\d[\d.,]*\s*(?:ALL|Lekë|L|€|EUR))", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


async def scrape_store(page, target: dict) -> list[dict]:
    store = target["store"]
    base_url = target["url"]
    min_disc = target.get("min_discount", DEFAULT_MIN_DISCOUNT)
    print(f"Scanning {store} ({base_url}) [min -{min_disc}%]...")

    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)
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
            and not re.match(r"^[\d.,\s]+(ALL|Lekë|L|€)?$", l.strip(), re.IGNORECASE)
        ]
        title = lines[0][:75] if lines else full_url.split("/")[-1][:75]
        discount = extract_discount(text)
        price = extract_price(text)

        title_lower = f"{title} {full_url}".lower()
        wishlist_hit = next((w for w in WISHLIST if w in title_lower), None)

        if discount >= min_disc or wishlist_hit:
            deals.append(
                {
                    "store": store,
                    "title": title,
                    "discount": discount,
                    "price": price,
                    "wishlist": wishlist_hit,
                    "url": full_url,
                }
            )
    print(f"  -> Found {len(deals)} clean deals on {store}")
    return deals


async def main() -> None:
    seen = load_seen()
    all_deals = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome" if os.getenv("GITHUB_ACTIONS") else None,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
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
