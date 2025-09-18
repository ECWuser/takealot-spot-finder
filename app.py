import asyncio, json, re
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import quote_plus
from difflib import SequenceMatcher

import streamlit as st
from playwright.async_api import async_playwright, Response

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

def best_match(target: str, candidates: List[str]) -> Tuple[Optional[int], Optional[str], float]:
    """Return (1-based index, matched_title, similarity score). Always returns best candidate if any."""
    if not candidates:
        return None, None, 0.0
    nt = norm_title(target)
    best_i, best_t, best_s = None, None, 0.0
    for i, c in enumerate(candidates, start=1):
        nc = norm_title(c)
        if nc == nt:
            return i, c, 1.0
        if nt in nc or nc in nt:
            return i, c, 0.99
        s = SequenceMatcher(None, nt, nc).ratio()
        if s > best_s:
            best_s, best_i, best_t = s, i, c
    return best_i, best_t, best_s

def assign_spots(items: List[Dict[str, Any]]) -> None:
    for i, it in enumerate(items, start=1):
        it["spot"] = i

# --------- DOM fallbacks (only used if network JSON not found) ----------
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
  out.sort((a,b)=> (a.y-b.y) || (a.x-b.x));
  return out;
}
"""

async def wait_for_min_products(page, minimum=8, timeout_ms=25000):
    elapsed = 0
    while elapsed < timeout_ms:
        count = await page.locator("a[href*='/p/']").count()
        if count >= minimum:
            return True
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

# ---------- Network JSON helpers ----------
def _extract_products_from_json(obj: Any) -> List[Dict[str, Any]]:
    """
    Heuristic: search nested dict/list for arrays of product-like objects:
    - must have a title/name AND a PDP URL slug or URL containing '/p/'
    - optional availability flags used to filter actionable items
    """
    found: List[Dict[str, Any]] = []

    def looks_like_product(x: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(x, dict):
            return None
        # title/name fields
        title = x.get("title") or x.get("name") or x.get("productTitle") or x.get("product_name")
        # PDP URL fields
        url = (x.get("url") or x.get("pdpUrl") or x.get("productUrl") or x.get("link") or "")
        slug = x.get("slug") or x.get("slugUrl") or x.get("seoUrl") or ""
        # availability-ish flags
        buyable = x.get("buyable") or x.get("isBuyable") or x.get("is_buyable") or x.get("available") or x.get("inStock")

        # Compose a PDP path if we only have a slug-ish piece
        if not url and slug:
            url = str(slug)
        if not (title and isinstance(title, str)):
            return None
        if not isinstance(url, str):
            return None
        if "/p/" not in url:
            # try to coerce to full PDP path
            if url and not url.startswith("http"):
                url = f"/p/{url.strip('/')}"
            # still not PDP-like? give up
            if "/p/" not in url:
                return None
        return {"title": title, "url": url, "buyable": bool(buyable)}

    def walk(node: Any):
        if isinstance(node, list):
            # if this list looks like products, pull them all
            prod_candidates = []
            for el in node:
                prod = looks_like_product(el)
                if prod:
                    prod_candidates.append(prod)
            if prod_candidates:
                found.extend(prod_candidates)
            else:
                for el in node:
                    walk(el)
        elif isinstance(node, dict):
            for _, v in node.items():
                walk(v)

    walk(obj)
    return found

async def collect_products_via_network(page) -> List[Dict[str, Any]]:
    """
    Attach a response listener while the page loads; parse any JSON that contains product arrays.
    Return the first stable, non-empty list we find.
    """
    collected: List[Dict[str, Any]] = []
    done = asyncio.Event()

    async def on_response(resp: Response):
        try:
            ct = resp.headers.get("content-type", "")
            if "application/json" not in ct:
                return
            # Ignore too-small or too-large responses to save time
            if resp.request.resource_type in {"image", "font"}:
                return
            if resp.status != 200:
                return
            data = await resp.json()
            prods = _extract_products_from_json(data)
            if prods:
                # keep only title/url/buyable
                for p in prods:
                    p["title"] = (p.get("title") or "").strip()
                    p["url"] = p.get("url") or ""
                collected[:] = prods  # replace with latest
                # we don't set done immediately; we let page settle a bit
        except Exception:
            return

    page.on("response", on_response)
    return collected, done  # caller can read from 'collected' after navigation

# ---------- Core find routine ----------
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

        # Start network collection
        net_products, _ = await collect_products_via_network(page)

        # Navigate directly to search
        q = quote_plus(search_category)
        url = f"https://www.takealot.com/all?_sb={q}"
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # Give network a short window to populate
        await wait_for_min_products(page, minimum=8, timeout_ms=20000)
        await asyncio.sleep(0.5)  # tiny grace
        await scroll_to_bottom(page, max_iters=12)

        # Prefer network JSON if available
        items: List[Dict[str, Any]] = []
        if net_products:
            # Keep ordering: left->right, top->bottom is a UI concept, but network list is already in listing order.
            # We just map them to a flat list.
            for p in net_products:
                items.append({"title": p["title"], "href": p["url"]})
        else:
            # Fallback to DOM relaxed
            dom = await page.evaluate(JS_SCRAPE_RELAXED)
            for d in dom:
                items.append({"title": d["title"], "href": d.get("href", "")})

        assign_spots(items)
        titles = [it["title"] for it in items]
        idx, matched_title, score = best_match(product_name, titles)

        await context.close(); await browser.close()
        return idx, matched_title, score, titles[:30]  # return a sample for debugging in UI

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
            spot, matched, score, sample = asyncio.run(find_spot(search_category.strip(), product_name.strip()))

        st.subheader("Result")
        if spot is not None:
            extra = f"  \n(approximate match {score:.2f})" if score < 0.98 else ""
            st.success(f"Spot: {spot}{extra}")
            if matched and norm_title(matched) != norm_title(product_name):
                st.caption(f"Matched: **{matched}**")
        else:
            st.warning("Product title not found in the first parsed results.")
        if sample:
            with st.expander(f"First {len(sample)} titles I saw (for troubleshooting):"):
                for i, t in enumerate(sample, start=1):
                    st.write(f"{i:>3}: {t}")

