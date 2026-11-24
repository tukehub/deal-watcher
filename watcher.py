import asyncio, json, os, re
from urllib.parse import urljoin
import httpx
from playwright.async_api import async_playwright

MIN_DISCOUNT = 50
STATE_FILE = "seen_deals.json"
WISHLIST = ["xiaomi tv box", "nvme", "ulanzi", "nd filter", "monitor", "shelly", "ups", "scala1", "scala2", "gan charger"]

TARGETS = [
    {"store": "Vodafone eShop", "url": "https://eshop.vodafone.al/en/special-offer", "card_selector": "a[href*='/en/']"},
    {"store": "Vodafone Accessories", "url": "https://eshop.vodafone.al/en/accessories", "card_selector": "a[href*='/en/']"},
    {"store": "Neptun", "url": "https://www.neptun.al/", "card_selector": "[class*='product'], .product-item, article"},
    {"store": "Globe", "url": "https://globe.al/", "card_selector": ".product-miniature, .product-item, [class*='product']"},
    {"store": "Gjirafa50", "url": "https://gjirafa50.com/te-gjitha-ne-zbritje-2026", "card_selector": ".product-item, [class*='product-box'], article"},
    {"store": "Foleja", "url": "https://www.foleja.al/", "card_selector": ".product-box, .card-body, [class*='product']"},
]

def load_seen():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_seen(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

async def send_telegram(lines):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secrets not set; skipping push notification.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) + 2 > 3800:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n\n{line}" if current else line
    if current:
        chunks.append(current)
    async with httpx.AsyncClient(timeout=20) as client:
        for chunk in chunks:
            r = await client.post(url, json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True})
            r.raise_for_status()

def extract_discount(text):
    matches = re.findall(r"(?<!\d)(\d{2})\s*%", text)
    valid = [int(m) for m in matches if 5 <= int(m) <= 95]
    return max(valid) if valid else 0

def extract_price(text):
    m = re.search(r"(\d[\d.,]*\s*(?:ALL|Lekë|L|€|EUR))", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""

async def scrape_store(page, target):
    store, base_url = target["store"], target["url"]
    print(f"Scanning {store} ({base_url})...")
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
        "els => els.map(el => { const a = el.tagName === 'A' ? el : el.querySelector('a[href]'); return { text: (el.innerText || '').trim(), href: a ? a.getAttribute('href') : '' }; })"
    )
    deals, seen_urls = [], set()
    for card in raw_cards:
        text, href = card.get("text", ""), card.get("href", "")
        if not text or not href or href.startswith("#") or "javascript:" in href:
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 6 and "%" not in l and not re.match(r"^[\d.,\s]+(ALL|Lekë|€)?$", l.strip())]
        title = lines[0][:90] if lines else full_url.split("/")[-1][:90]
        discount = extract_discount(text)
        price = extract_price(text)
        wishlist_hit = next((w for w in WISHLIST if w in f"{title} {full_url}".lower()), None)
        if discount >= MIN_DISCOUNT or wishlist_hit:
            deals.append({"store": store, "title": title, "discount": discount, "price": price, "wishlist": wishlist_hit, "url": full_url})
    return deals

async def main():
    seen = load_seen()
    all_deals = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = await context.new_page()
        for target in TARGETS:
            all_deals.extend(await scrape_store(page, target))
        await browser.close()

    print(f"\nTotal matching items scanned: {len(all_deals)}")
    new_messages = []
    for d in all_deals:
        key = f"{d['store']}|{d['url']}|{d['discount']}"
        price_str = f" — <b>{d['price']}</b>" if d["price"] else ""
        tag = f"🔥 <b>-{d['discount']}%</b>" if d["discount"] >= MIN_DISCOUNT else f"🎯 <b>Wishlist ({d['wishlist']})</b>"
        summary = f"{tag} [{d['store']}] <a href=\"{d['url']}\">{d['title']}</a>{price_str}"
        print(f"  - [{d['store']}] -{d['discount']}% | {d['title']} ({d['price']}) -> {d['url']}")
        if key not in seen:
            seen[key] = True
            new_messages.append(summary)

    if new_messages:
        await send_telegram(new_messages)
        save_seen(seen)
        print(f"\nSaved {len(new_messages)} new deals to {STATE_FILE}.")
    else:
        print("\nNo brand-new deals since last check.")

if __name__ == "__main__":
    asyncio.run(main())
