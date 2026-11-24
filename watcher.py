import asyncio
import json
import os
import re
from collections import defaultdict
from urllib.parse import urljoin
import httpx
from playwright.async_api import async_playwright

DEFAULT_MIN_DISCOUNT = 50
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

TARGETS = [
    {
        "store": "Vodafone eShop",
        "url": "https://eshop.vodafone.al/en/special-offer",
        "card_selector": "a[href*='/en/']",
        "min_discount": 40,
    },
    {
        "store": "One Albania",
        "url": "https://www.one.al/sq/individi/e-shop/e_shop/telefona",
        "card_selector": "[class*='product'], .card, article",
        "min_discount": 40,
    },
    {
        "store": "Neptun Oferta",
        "url": "https://neptun.al/ns-newsarticle-black-friday-oferta-2026",
        "card_selector": "[class*='product'], .product-item, article",
        "min_discount": 50,
    },
    {
        "store": "Neptun Gaming",
        "url": "https://www.neptun.al/ns-newsarticle-black-friday-gaming",
        "card_selector": "[class*='product'], .product-item, article",
        "min_discount": 45,
    },
    {
        "store": "Globe",
        "url": "https://globe.al/black-friday",
        "card_selector": ".product-miniature, .product-item, [class*='product']",
        "min_discount": 50,
    },
    {
        "store": "Gjirafa50",
        "url": "https://gjirafa50.com/te-gjitha-ne-zbritje-2026",
        "card_selector": ".product-item, [class*='product-box'], article",
        "min_discount": 50,
    },
    {
        "store": "Foleja",
        "url": "https://www.foleja.al/",
        "card_selector": ".product-box, .card-body, [class*='product']",
        "min_discount": 50,
    },
]

CATEGORY_RULES = [
    (
        "💻 ELECTRONICS & TECH",
        [
            "laptop", "macbook", "monitor", "ssd", "nvme", "ram", "gpu", "rtx",
            "phone", "celular", "iphone", "samsung", "galaxy", "xiaomi", "redmi",
            "tablet", "ipad", "watch", "tv", "televizor", "oled", "qled", "uhd",
            "audio", "speaker", "boks", "jbl", "sony", "headphone", "kufje", "airpods",
            "ps5", "playstation", "nintendo", "switch", "gaming", "mouse", "tastier",
            "keyboard", "router", "wi-fi", "wifi", "camera", "kamera", "lens", "lente",
            "charger", "karikues", "usb", "hdmi", "ups", "shelly", "smart", "box",
        ],
    ),
    (
        "🏠 HOME & APPLIANCES",
        [
            "lavatrice", "lavatriçe", "tharëse", "tharese", "frigorifer", "furre",
            "furrë", "pianure", "kondicioner", "inverter", "btu", "fshese", "fshesë",
            "vacuum", "hekur", "ekspres", "kafe", "air fryer", "mikrovale", "tenxhere",
            "tigan", "tefal", "kuzhin", "tavoline", "tavolinë", "karrige", "dyshek",
            "pompe", "pompë", "scala",
        ],
    ),
]


def classify_category(text: str, url: str) -> str:
    haystack = f"{text} {url}".lower()
    for label, keywords in CATEGORY_RULES:
        if any(kw in haystack for kw in keywords):
            return label
    return "📦 OTHER DEALS"


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


async def send_telegram(grouped_lines: dict[str, list[str]]) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secrets not set; skipping push notification.")
        return

    sections = []
    for category in ["💻 ELECTRONICS & TECH", "🏠 HOME & APPLIANCES", "📦 OTHER DEALS"]:
        items = grouped_lines.get(category, [])
        if items:
            sections.append(f"<b>{category}</b>\n" + "\n".join(items))

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks, current = [], ""
    for section in sections:
        if len(current) + len(section) + 2 > 3800:
            if current:
                chunks.append(current)
            current = section
        else:
            current = f"{current}\n\n{section}" if current else section
    if current:
        chunks.append(current)

    async with httpx.AsyncClient(timeout=20) as client:
        for chunk in chunks:
            resp = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()


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

        lines = [
            l.strip()
            for l in text.splitlines()
            if len(l.strip()) > 6
            and "%" not in l
            and not re.match(r"^[\d.,\s]+(ALL|Lekë|L|€)?$", l.strip(), re.IGNORECASE)
        ]
        title = lines[0][:90] if lines else full_url.split("/")[-1][:90]
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
                    "category": classify_category(f"{title} {text}", full_url),
                    "url": full_url,
                }
            )
    print(f"  -> Found {len(deals)} matching deals on {store}")
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
        price_str = f" — <b>{d['price']}</b>" if d["price"] else ""
        tag = (
            f"🔥 <b>-{d['discount']}%</b>"
            if d["discount"] > 0
            else f"🎯 <b>{d['wishlist']}</b>"
        )
        line = f"• {tag} [{d['store']}] <a href=\"{d['url']}\">{d['title']}</a>{price_str}"

        if key not in seen:
            seen[key] = True
            grouped_new[d["category"]].append(line)
            new_count += 1

    if new_count:
        await send_telegram(grouped_new)
        save_seen(seen)
        print(f"\nSent & saved {new_count} new deals.")
    else:
        print("\nNo brand-new deals since last check.")


if __name__ == "__main__":
    asyncio.run(main())
