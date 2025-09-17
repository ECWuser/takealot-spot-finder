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
    return r.width > 100 && r.height > 100;
  }

  for (const el of cards) {
    if (!bigEnough(el)) continue;
    const text = el.innerText || "";
    if (!ACTION_RX.test(text)) continue;

    const r = el.getBoundingClientRect();
    const titleEl = el.querySelector("a[href*='product']");
    const title = titleEl ? titleEl.innerText.trim() : text.split("\n")[0].trim();

    out.push({
      x: r.left,
      y: r.top,
      title,
    });
  }
  return out;
}
"""

async def find_spot(search_category: str, product_name: str, save_debug: bool = False) -> Tuple[Optional[int], List[str]]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
        )
        page = await context.new_page()

        url = f"https://www.takealot.com/all?_sb={search_category}"
        await page.goto(url, timeout=60000)
        await page.wait_for_timeout(3000)

        items = await page.evaluate(JS_SCRAPE_ACTION_ONLY)
        items = dedupe_by_box(items)

        titles = [norm_title(it["title"]) for it in items]
        norm_target = norm_title(product_name)

        spot = None
        for idx, t in enumerate(titles, start=1):
            if norm_target == t:
                spot = idx
                break

        await browser.close()
        return spot, titles

# ---------- Streamlit UI ----------
st.set_page_config(page_title="Takealot Spot Finder", page_icon="🔎", layout="centered")
st.title("Takealot Spot Finder")

params = st.query_params
prefill_cat = params.get("cat", "")
prefill_name = params.get("name", "")

search_category = st.text_input("Search Category", value=prefill_cat, placeholder="e.g. blood pressure monitor")
product_name = st.text_input("Product Name (exact title)", value=prefill_name, placeholder="e.g. Beurer BM 28 Blood Pressure Monitor")

if st.button("Find Spot"):
    if not search_category or not product_name:
        st.warning("Please enter both category and product name.")
    else:
        with st.spinner("Searching Takealot..."):
            try:
                spot, seen = asyncio.run(find_spot(search_category.strip(), product_name.strip()))
                if spot is not None:
                    st.success(f"✅ '{product_name}' found at Spot **{spot}**")
                else:
                    st.error(f"❌ '{product_name}' not found in first {len(seen)} products.")
            except Exception as e:
                st.error(f"Error: {e}")

