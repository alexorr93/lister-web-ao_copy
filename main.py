from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import base64

app = FastAPI()

class ScrapeRequest(BaseModel):
    url: str
    wait_for: str = ""
    timeout: int = 30000
    screenshot: bool = False  # full page screenshot
    extract_images: bool = True

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/scrape")
async def scrape(req: ScrapeRequest):
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                viewport={"width": 1280, "height": 900}
            )
            page = await context.new_page()
            await page.goto(req.url, wait_until="networkidle", timeout=req.timeout)

            if req.wait_for:
                try:
                    await page.wait_for_selector(req.wait_for, timeout=10000)
                except:
                    pass

            # Scroll to trigger lazy loading
            await page.evaluate("""async () => {
                await new Promise(resolve => {
                    let pos = 0;
                    const timer = setInterval(() => {
                        window.scrollBy(0, 600);
                        pos += 600;
                        if (pos >= document.body.scrollHeight) {
                            window.scrollTo(0, 0);
                            clearInterval(timer);
                            resolve();
                        }
                    }, 150);
                });
            }""")
            await page.wait_for_timeout(2000)

            # Extract structured lots with images
            lots = await page.evaluate("""() => {
                const items = [];
                const selectors = [
                    '.lot-card', '.catalogue-item', '.lot-item',
                    '[class*="lot-row"]', '[class*="LotCard"]',
                    '[class*="auction-item"]', '[class*="item-card"]',
                    '.hibid-lot', '.lot-listing', '[class*="LotItem"]',
                    '[class*="lot_item"]', 'article'
                ];
                for (const sel of selectors) {
                    const cards = document.querySelectorAll(sel);
                    if (cards.length > 2) {
                        cards.forEach((card, i) => {
                            const title = card.querySelector('h1,h2,h3,h4,[class*="title"],[class*="name"],[class*="description"]')?.innerText?.trim() || '';
                            const lotNum = card.querySelector('[class*="lot-num"],[class*="lot-number"],[class*="lotNum"]')?.innerText?.trim() || String(i+1);
                            const price = card.querySelector('[class*="price"],[class*="estimate"],[class*="bid"],[class*="amount"]')?.innerText?.trim() || '';
                            // Get highest res image available
                            const imgEl = card.querySelector('img');
                            const img = imgEl?.dataset?.src || imgEl?.dataset?.lazySrc || imgEl?.src || '';
                            const imgSrcset = imgEl?.srcset?.split(',').pop()?.trim()?.split(' ')[0] || '';
                            if (title) items.push({
                                lot: lotNum.replace(/[^0-9]/g, '') || String(i+1),
                                title,
                                image_url: imgSrcset || img,
                                estimate: price,
                                html_snippet: card.outerHTML.slice(0, 500)
                            });
                        });
                        break;
                    }
                }
                return items;
            }""")

            # Full page text as fallback
            text = await page.inner_text("body")

            # Optional full page screenshot (base64)
            screenshot_b64 = ""
            if req.screenshot:
                shot = await page.screenshot(full_page=True, type="jpeg", quality=60)
                screenshot_b64 = base64.b64encode(shot).decode()

            # If structured lots found, take individual screenshots per lot
            lot_screenshots = []
            if lots and req.screenshot:
                selectors_to_try = [
                    '.lot-card', '.catalogue-item', '.lot-item',
                    '[class*="LotCard"]', '[class*="auction-item"]'
                ]
                for sel in selectors_to_try:
                    cards = await page.query_selector_all(sel)
                    if len(cards) > 2:
                        for i, card in enumerate(cards[:req.timeout // 1000 or 50]):
                            try:
                                shot = await card.screenshot(type="jpeg", quality=70)
                                lot_screenshots.append(base64.b64encode(shot).decode())
                            except:
                                lot_screenshots.append("")
                        break

            await browser.close()

            return {
                "url": req.url,
                "lots": lots,
                "raw_text": text[:20000],
                "lot_count": len(lots),
                "screenshot_b64": screenshot_b64,
                "lot_screenshots": lot_screenshots  # parallel list to lots
            }
    except Exception as e:
        raise HTTPException(500, f"Scrape error: {str(e)}")
