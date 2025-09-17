import asyncio, json, re
from typing import List, Dict, Any, Optional, Tuple

import streamlit as st
from playwright.async_api import async_playwright

# ---------- constants ----------
VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

def norm_title(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("\u00a0", " ").replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s)
    return s

def dedupe_by_box(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for it in items:
        key = (round(it["x"], -1), round(it["y"], -1), it["title"])
        if key in seen:
            continue
        seen.add(key); out.append(it)
    return out

# Final scrape after page fully loaded:
# - absolute page coords to keep true reading order
# - REQUIRE presence of "Add to Cart" OR "Shop all options" inside the card
JS_SCRAPE_ACTION_ONLY = r"""
() => {
  const scope = document.querySelector('main') || document.body;
  if (!scope) return [];

  const cards = Array.from(scope.querySelectorAll(
    'article, li, div[data-ref*="product"], div[data-ref*="tile"], div[class*="product"], div[class*="card"]'
  ));

  const out = [];
  const ACTION_RX = /(add\s*to\s*cart|shop\s*all\s*options)/i;

  function bigEnough(el){
    const r = el.getBoundingClientRect();
    return r.width > 120 && r.height > 120;
  }

  for (const c of cards) {
    try {
      if (!bigEnough(c)) continue;

      // Must contain bottom action text somewhere in the card
      const visibleText = (c.innerText || c.textContent || "").trim();
      if (!ACTION_RX.test(visibleText)) continue;

      const r = c.getBoundingClientRect();
      const x = Math.round(r.left + window.scrollX);
      const y = Math.round(r.top  + window.scrollY);

      // PDP link inside this card (ignore brand/search links)
      const pdp = c.querySelector('a[href*="/p/"]:not([href*="/brand/"]):not([href*="/search"])');
      const href = pdp ? pdp.href : "";

      // Prefer heading-like title; fallback to link text
      let title = "";
      const h = c.querySelector('h1,h2,h3,h4,[data-ref*="title"], .title, [class*="title"]');
      if (h) title = (h.innerText || h.textContent || "").trim();
      if (!title && pdp) title = (pdp.getAttribute("title") || pdp.innerText || pdp.textContent || "").trim();

      if (!title) continue;

      out.push({ href, title, x, y, w: Math.round(r.width), h: Math.round(r.height) });
    } catch(e){}
  }

  // absolute page order: top->bottom, left->right
  out.sort((a,b)=> (a.y-b.y) || (a.x-b.x));
  return out;
}
"""

async def dismiss_popups(page):
    sels = [
        "button:has-text('Accept')","button:has-text('Accept all')","button:has-text('Allow all')",
        "button:has-text('Got it')","button:has-text('OK')","button:has-text('Close')",
        "button[aria-label='Close']","button:has-text('No thanks')","button:has-text('Not now')",
        "[data-test='close']","div[role='dialog'] button:has-text('×')",
    ]
    for s in sels:
        try:
            el = page.locator(s).first
            if await el.is_visible(timeout=800): await el.click()
        except Exception:
            pass
    try:
        await page.evaluate("""
          () => {
            const blockers = Array.from(document.querySelectorAll('[role="dialog"], .modal, .overlay, [class*="cookie"]'));
            for (const b of blockers) b.style.display='none';
          }
        """)
    except Exception:
        pass

async def accept_cookies(page):
    for s in ["button:has-text('Accept')","button:has-text('Accept All')","text=Accept all cookies"]:
        try:
            el = page.locator(s).first
            if await el.is_visible(timeout=1000):
                await el.click(); return
        except Exception:
            pass

async def type_into_search(page, q: str):
    for s in ["input[placeholder*='Search']", "input[type='search']", "form[role='search'] input", "#search"]:
        try:
            box = page.locator(s).first
            await box.wait_for(state="visible", timeout=4000)
            await box.click(); await box.fill("")
            await box.type(q, delay=25)  # allow autosuggest
            await box.press("Enter")
            return
        except Exception:
            continue
    raise RuntimeError("Search box not found")

async def force_products_tab(page):
    for s in ["a:has-text('Products')", "button:has-text('Products')", "li:has-text('Products') a"]:
        try:
            el = page.locator(s).first
            if await el.is_visible(timeout=1000): await el.click(); return
        except Exception:
            pass

async def scroll_to_bottom(page, max_iters=50):
    stable = 0
    last = await page.evaluate("document.body.scrollHeight")
    for _ in range(max_iters):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1100)
        cur = await page.evaluate("document.body.scrollHeight")
        if cur <= last:
            stable += 1
            if stable >= 2: break
        else:
            stable = 0
        last = cur
    await page.wait_for_timeout(1200)

def assign_spots(items: List[Dict[str, Any]]) -> None:
    # Items are already top->bottom, left->right. Spots are 1..n in that order.
    for i, it in enumerate(items, start=1):
        it["spot"] = i

async def find_spot(search_category: str, product_name: str, save_debug=False) -> Tuple[Optional[int], list]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport=VIEWPORT, user_agent=USER_AGENT, locale="en-ZA")
        page = await context.new_page()

        await page.goto("https://www.takealot.com/", wait_until="domcontentloaded", timeout=45000)
        await accept_cookies(page); await dismiss_popups(page)
        await type_into_search(page, search_category)

        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        await force_products_tab(page)
        await scroll_to_bottom(page, max_iters=60)
        await dismiss_popups(page)

        if save_debug:
            try:
                await page.screenshot(path="debug_results.png", full_page=True)
                html = await page.content()
                with open("debug_results.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass

        items = await page.evaluate(JS_SCRAPE_ACTION_ONLY)
        items = dedupe_by_box(items)
        items.sort(key=lambda x: (x["y"], x["x"]))
        assign_spots(items)

        target = norm_title(product_name)
        spot = None
        for it in items:
            if norm_title(it["title"]) == target:
                spot = it["spot"]; break

        await context.close(); await browser.close()
        return spot, items

# ============================== UI ==============================
st.set_page_config(page_title="Takealot Spot Finder — Add-to-Cart filtered", page_icon="🛒", layout="centered")
st.title("🛒 Takealot Spot Finder (Add-to-Cart / Shop-all-options filtered)")
st.caption("Counts only cards that include **Add to Cart** or **Shop all options**. Spots: left→right, top→bottom (4 per row).")

with st.form("spot_form"):
    search_category = st.text_input("Search category (typed into Takealot search):", value="Blood pressure monitor")
    product_name = st.text_input("Product name (exact title to locate):", value="")
    save_debug = st.checkbox("Debug: save full-page screenshot + HTML", value=False)
    submitted = st.form_submit_button("Find spot")

if submitted:
    if not product_name.strip():
        st.error("Please enter the exact Product name.")
    else:
        with st.spinner("Searching Takealot and locating the product..."):
            spot, seen = asyncio.run(find_spot(search_category.strip(), product_name.strip(), save_debug))

        st.subheader("Result")
        if spot is not None:
            st.success(f"Spot: {spot}")
            st.caption("Spots are counted left→right in a 4-column grid (1–4, 5–8, 9–12, …).")
        else:
            st.warning("Product title not found among action-filtered tiles.")
            if seen:
                st.write("First 12 action-filtered titles on the page:")
                for it in seen[:12]:
                    st.write(f"{it['spot']:>3}: {it['title']}")
        if save_debug:
            st.caption("Saved: debug_results.png and debug_results.html next to app.py")
