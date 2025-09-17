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
    // structural signals (prefer these in headless)
    if (el.querySelector('button, [role="button"], [data-test*=\"add\"], [data-qa*=\"add\"]')) return true;
    // text fallback
    const t = (el.innerText || el.textContent || '').toLowerCase();
    return /add\s*to\s*cart|shop\s*all\s*options|add\s*to\s*basket|add\s*to\s*trolley/.test(t);
  }
  const out = [];
  for (const c of cards) {
    try {
      if (!bigEnough(c) || !hasAction(c)) continue;
      // use closest "card" ancestor for stable geometry
      const pdp = c.querySelector('a[href*=\"/p/\"]:not([href*=\"/brand/\"]):not([href*=\"/search\"])');
      if (!pdp) continue;
      const card = pdp.closest('article,li,div[data-ref*=\"product\"],div[data-ref*=\"tile\"],div[class*=\"product\"],div[class*=\"card\"]') || c;
      const r = card.getBoundingClientRect();
      const x = Math.round(r.left + window.scrollX), y = Math.round(r.top + window.scrollY);
      let title = pdp.getAttribute('title') || pdp.innerText || pdp.textContent || '';
      title = title.trim();
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
  const links = Array.from(scope.querySelectorAll('a[href*=\"/p/\"]:not([href*=\"/brand/\"]):not([href*=\"/search\"])'));
  const out = [];
  for (const pdp of links) {
    try {
      const card = pdp.closest('article,li,div[data-ref*=\"product\"],div[data-ref*=\"tile\"],div[class*=\"product\"],div[class*=\"card\"]') || pdp;
      const r = card.getBoundingClientRect();
      if (r.width <= 120 || r.height <= 120) continue;
      const x = Math.round(r.left + window.scrollX), y = Math.round(r.top + window.scrollY);
      let title = pdp.getAttribute('title') || pdp.innerText || pdp.textContent || '';
      title = title.trim();
      if (!title) continue;
      out.push({ href: pdp.href, title, x, y, w: Math.round(r.width), h: Math.round(r.height) });
    } catch(e){}
  }
  out.sort((a,b)=> (a.y-b.y) || (a.x-b.x));
  return out;
}
"""

async def force_products_tab(page):
    for s in ["a:has-text('Products')", "button:has-text('Products')", "li:has-text('Products') a"]:
        try:
            el = page.locator(s).first
            if await el.is_visible(timeout=1500):
                await el.click()
                return
        except Exception:
            pass

async def scroll_to_bottom(page, max_iters=60):
    stable = 0
    last = await page.evaluate("document.body.scrollHeight")
    for _ in range(max_iters):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1200)
        cur = await page.evaluate("document.body.scrollHeight")
        if cur <= last:
            stable += 1
            if stable >= 2: break
        else:
            stable = 0
        last = cur
    await page.wait_for_timeout(1200)

def assign_spots(items: List[Dict[str, Any]]) -> None:
    for i, it in enumerate(items, start=1):
        it["spot"] = i

async def wait_for_min_products(page, minimum=8, timeout_ms=30000):
    # Wait until at least `minimum` PDP links exist (headless-safe)
    step = 0
    elapsed = 0
    while elapsed < timeout_ms:
        count = await page.locator("a[href*='/p/']").count()
        if count >= minimum: return
        await page.wait_for_timeout(500)
        elapsed += 500
        step += 1

async def find_spot(search_category: str, product_name: str) -> Tuple[Optional[int], list, bool, Optional[str]]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale="en-ZA",
            timezone_id="Africa/Johannesburg",
            extra_http_headers={"Accept-Language": ACCEPT_LANG},
            geolocation={"latitude": -26.2041, "longitude": 28.0473},
            permissions=["geolocation"],
        )
        page = await context.new_page()

        # Direct search (headless-safe)
        q = quote_plus(search_category)
        url = f"https://www.takealot.com/all?_sb={q}"
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        await force_products_tab(page)
        await wait_for_min_products(page, minimum=8, timeout_ms=30000)
        await scroll_to_bottom(page, max_iters=60)

        # Try strict first
        items = await page.evaluate(JS_SCRAPE_ACTION_ONLY)
        items = dedupe_by_box(items)
        items.sort(key=lambda x: (x["y"], x["x"]))
        assign_spots(items)
        used_fallback = False

        # Fallback if nothing found
        if len(items) == 0:
            used_fallback = True
            items = await page.evaluate(JS_SCRAPE_RELAXED)
            items = dedupe_by_box(items)
            items.sort(key=lambda x: (x["y"], x["x"]))
            assign_spots(items)

        titles = [it["title"] for it in items]
        spot, matched_title = smart_match(product_name, titles)

        await context.close(); await browser.close()
        return spot, items, used_fallback, matched_title

# ============================== UI ==============================
st.set_page_config(page_title="Takealot Spot Finder", page_icon="🔎", layout="centered")
st.title("Takealot Spot Finder")

# Prefill from URL params for Excel links
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
        with st.spinner("Searching Takealot and locating the product..."):
            spot, seen, used_fallback, matched_title = asyncio.run(find_spot(search_category.strip(), product_name.strip()))

        st.subheader("Result")
        if spot is not None:
            if used_fallback:
                st.info("Fallback used (no action buttons detected in headless).")
            if matched_title and norm_title(matched_title) != norm_title(product_name):
                st.success(f"Spot: {spot}  \nMatched: **{matched_title}**")
            else:
                st.success(f"Spot: {spot}")
            st.caption("Spots are counted left→right in a 4-column grid (1–4, 5–8, 9–12, …).")
        else:
            st.warning("Product title not found among parsed tiles.")
            if used_fallback:
                st.caption("Note: Fallback mode used as no action buttons were detected in headless rendering.")
            if seen:
                st.write("First 12 parsed titles on the page:")
                for it in seen[:12]:
                    st.write(f"{it['spot']:>3}: {it['title']}")

