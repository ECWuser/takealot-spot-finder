import asyncio, re
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import quote_plus
from difflib import SequenceMatcher

import streamlit as st
from playwright.async_api import async_playwright

# ---------- constants ----------
VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
ACCEPT_LANG = "en-ZA,en;q=0.9"

def norm_title(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("\u00a0", " ").replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[®™']", "", s)
    return s

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()

def smart_match(target: str, candidates: list) -> Tuple[Optional[int], Optional[str]]:
    nt = norm_title(target)
    best_idx, best_title, best_score = None, None, 0.0
    for idx, t in enumerate(candidates, start=1):
        ntc = norm_title(t)
        if ntc == nt: return idx, t                      # exact
        if nt in ntc or ntc in nt: return idx, t         # contains
        score = SequenceMatcher(None, nt, ntc).ratio()   # fuzzy
        if score > best_score:
            best_score, best_idx, best_title = score, idx, t
    if best_score >= 0.92:
        return best_idx, best_title
    return None, None

def dedupe_by_box(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for it in items:
        key = (round(it["x"], -1), round(it["y"], -1), norm_title(it["title"]))
        if key in seen: continue
        seen.add(key); out.append(it)
    return out

# Strict: require a visible action control inside the card (headless-safe)
JS_SCRAPE_ACTION_ONLY = r"""
() => {
  const scope = document.querySelector('main') || document.body;
  if (!scope) return [];
  const cards = Array.from(scope.querySelectorAll(
    'article, li, div[data-ref*="product"], div[data-ref*="tile"], div[class*="product"], div[class*="card"]'
  ));
  function bigEnough(el){
    const r = el.getBoundingClientRect(); return r.width > 120 && r.height > 120;
  }
  function hasAction(el){
    if (el.querySelector('button, [role="button"], [data-test*="add"], [data-qa*="add"]')) return true;
    const t = (el.innerText || el.textContent || '').toLowerCase();
    return /add\s*to\s*cart|shop\s*all\s*options|add\s*to\s*basket|add\s*to\s*trolley/.test(t);
  }
  const out = [];
  for (const c of cards) {
    try {
      if (!bigEnough(c) || !hasAction(c)) continue;
      const pdp = c.querySelector('a[href*="/p/"]:not([href*="/brand/"]):not([href*="/search"])');
      if (!pdp) continue;
      const card = pdp.closest('article,li,div[data-ref*="product"],div[data-ref*="tile"],div[class*="product"],div[class*="card"]') || c;
      const r = card.getBoundingClientRect();
      const x = Math.round(r.left + window.scrollX), y = Math.round(r.top + window.scrollY);
      let title = (pdp.getAttribute('title') || pdp.innerText || pdp.textContent || '').trim();
      if (!title) continue;
      out.push({ href: pdp.href, title, x, y, w: Math.round(r.width), h: Math.round(r.height) });
    } catch(e){}
  }
  out.sort((a,b)=> (a.y-b.y) || (a.x-b.x));
  return out;
}
"""

# Relaxed fallback: accept product tiles even if we can’t prove the action control
JS_SCRAPE_RELAXED = r"""
() => {
  const scope = document.querySelector('main') || document.body;
  if (!scope) return [];
  const links = Array.from(scope.querySelectorAll('a[href*="/p/"]:not([href*="/brand/"]):not([href*="/search"])'));
  const out = [];
  for (const pdp of links) {
    try {
      const card = pdp.closest('article,li,div[data-ref*="product"],div[data-ref*="tile"],div[class*="product"],div[class*="card"]') || pdp;
      const r = card.getBoundingClientRect();
      if (r.width <= 120 || r.height <= 120) continue;
      const x = Math.round(r.left + window.scrollX), y = Math.round(r.top + window.scrollY);
      let title = (pdp.getAttribute('title') || pdp.innerText || pdp.textContent || '').trim();
      if (!title) continue;
      out.push({ href: pdp.href, title, x, y, w: Math.round(r.width), h: Math.round(r.height) });
    } catch(e){}
  }
  out.sort((a,b)=> (a.y-b.y) || (a.x-b-x));
  return out;
}
"""

async def wait_for_min_products(page, minimum=8, timeout_ms=25000):
    elapsed = 0
    while elapsed < timeout_ms:
        count = await page.locator("a[href*='/p/']").count()
        if count >= minimum: return True
        await page.wait_for_timeout(500)
        elapsed += 500
    return False

async def scroll_to_bottom(page, max_iters=24):
    last = await page.evaluate("document.body.scrollHeight")
    for _ in range(max_iters):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)
        cur = await page.evaluate("document.body.scrollHeight")
        if cur <= last:
            break
        last = cur
    await page.wait_for_timeout(800)

def assign_spots(items: List[Dict[str, Any]]) -> None:
    for i, it in enumerate(items, start=1):
        it["spot"] = i

async def find_spot(search_category: str, product_name: str):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale="en-ZA",
            timezone_id="Africa/Johannesburg",
            extra_http_headers={"Accept-Language": ACCEPT_LANG},
        )
        page = await context.new_page()

        q = quote_plus(search_category)
        url = f"https://www.takealot.com/all?_sb={q}"
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # ensure enough tiles exist
        await wait_for_min_products(page, minimum=8, timeout_ms=25000)
        await scroll_to_bottom(page)

        # STRICT
        strict = await page.evaluate(JS_SCRAPE_ACTION_ONLY)
        strict = dedupe_by_box(strict)
        strict.sort(key=lambda x: (x["y"], x["x"]))
        assign_spots(strict)

        # RELAXED
        relaxed = await page.evaluate(JS_SCRAPE_RELAXED)
        relaxed = dedupe_by_box(relaxed)
        relaxed.sort(key=lambda x: (x["y"], x["x"]))
        assign_spots(relaxed)

        # Try match (first strict, then relaxed)
        spot = None
        matched_from = "strict"
        titles_strict = [it["title"] for it in strict]
        titles_relaxed = [it["title"] for it in relaxed]

        if titles_strict:
            spot_idx, matched_title = smart_match(product_name, titles_strict)
            if spot_idx is not None:
                spot = spot_idx
            else:
                matched_from = "relaxed"
        if spot is None:
            spot_idx, matched_title = smart_match(product_name, titles_relaxed)
            if spot_idx is not None:
                spot = spot_idx

        await context.close(); await browser.close()
        return spot, strict, relaxed

# ============================== UI ==============================
st.set_page_config(page_title="Takealot Spot Finder", page_icon="🔎", layout="centered")
st.title("Takealot Spot Finder")

params = st.query_params
prefill_cat = params.get("cat", "")
prefill_name = params.get("name", "")

with st.form("spot_form"):
    search_category = st.text_input("Search category:", value=(prefill_cat or "Blood pressure monitor"))
    product_name = st.text_input("Product name (exact title):", value=(prefill_name or ""))
    submitted = st.form_submit_button("Find spot")

if submitted:
    if not product_name.strip():
        st.error("Please enter the exact Product name.")
    else:
        with st.spinner("Searching..."):
            spot, strict, relaxed = asyncio.run(find_spot(search_category.strip(), product_name.strip()))

        st.subheader("Result")
        if spot is not None:
            st.success(f"Spot: {spot}  (counted left→right, 4 per row)")
        else:
            st.warning("Product title not found among parsed tiles.")

        # Always show what we saw (to debug headless rendering)
        with st.expander(f"Strict (action buttons present) — {len(strict)} items"):
            for it in strict[:30]:
                st.write(f"{it['spot']:>3}: {it['title']}")
        with st.expander(f"Relaxed (all product tiles) — {len(relaxed)} items"):
            for it in relaxed[:30]:
                st.write(f"{it['spot']:>3}: {it['title']}")

