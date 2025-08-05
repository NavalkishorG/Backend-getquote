"""
FastAPI endpoint  /scrape-tenders   (same name your extension already calls)

Behaviour
──────────
1. A URL is posted from the browser extension.
2. If that URL is a tender-detail page → scrape it, write ONE row to
   the Supabase table `tenders`.
3. If the URL is a list / search page → (optionally) type the single
   SEARCH_KEYWORD below, collect every “Full Details” link found on
   that and subsequent pages, visit each detail page, and write ONE row
   per tender to Supabase.

Edit SEARCH_KEYWORD once; everything else works automatically.
Nothing is written to disk, but the response object still matches the
original signature so the front-end code does not have to change.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

from modules.supabase_auth import supabase  # re-use your global client

# ════════════════════ EDIT THIS ONLY ════════════════════
SEARCH_KEYWORD = ""      # example: "construction"  (empty → no search typed)
# ════════════════════════════════════════════════════════

# ───────────────────────────── FastAPI boiler-plate ───────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger("TenderService")
router = APIRouter()
thread_pool = ThreadPoolExecutor(max_workers=3)

class TenderRequest(BaseModel):
    url: str

class TenderResponse(BaseModel):
    status: str
    message: str
    data: dict        # returns a tiny preview so UI can show something
    file_path: str | None = None   # kept for backward compatibility (always None)

# ───────────────────────── helper utilities ─────────────────────────
def _extract_detail(page) -> dict:
    """Return all field/value pairs on a tender-detail page."""
    record = {}
    for block in page.query_selector_all(".list-desc"):
        label = block.query_selector("label, span")
        value = block.query_selector(".list-desc-inner")
        if label and value:
            k = label.inner_text().strip().rstrip(":")
            v = value.inner_text().replace("\n", " ").strip()
            if k and v and k.lower() not in {"", " ", "nbsp"}:
                record[k] = v
    return record

def _insert_supabase(url: str, record: dict) -> None:
    payload = {
        "url": url,
        "scraped_at": datetime.utcnow().isoformat(),
        "data": record,
    }
    try:
        res = supabase.table("tenders").insert(payload).execute()
        logger.info("Supabase insert OK (row id %s)", res.data[0]["id"])
    except Exception as err:
        logger.warning("Supabase insert failed for %s → %s", url, err)

# ───────────────────────── synchronous worker ─────────────────────────
def _scrape_sync(url: str) -> Tuple[int, dict]:
    """Return (#rows_inserted, preview_record)."""
    rows_inserted, preview = 0, {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled",
                  "--disable-web-security"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1920, "height": 1080},
        )

        list_tab = ctx.new_page()
        logger.info("Opening %s", url)
        list_tab.goto(url, wait_until="domcontentloaded", timeout=30_000)

        is_list_page = bool(list_tab.query_selector("a.detail"))

        # ───────── tender-detail page ─────────
        if not is_list_page:
            record = _extract_detail(list_tab)
            if not record:
                raise RuntimeError("No data extracted – selectors may be wrong.")
            _insert_supabase(url, record)
            rows_inserted, preview = 1, record

        # ───────── list / search page ─────────
        else:
            # optional keyword search
            if SEARCH_KEYWORD:
                sb = list_tab.query_selector('input[name="Keyword"], #form-Keyword')
                if sb:
                    logger.info("Typing keyword '%s' into search box", SEARCH_KEYWORD)
                    sb.fill(SEARCH_KEYWORD)
                    btn = (list_tab.query_selector("button.searchIcon")
                           or list_tab.query_selector('button[type="submit"]'))
                    if btn:
                        btn.click()
                        list_tab.wait_for_load_state("domcontentloaded")

            # collect ALL detail links (across pagination) before opening any of them
            collected: List[str] = []
            page_no, MAX_PAGES = 1, 50
            while page_no <= MAX_PAGES:
                list_tab.wait_for_selector(".box.boxW.listInner", timeout=15_000)
                for card in list_tab.query_selector_all(".box.boxW.listInner"):
                    link = card.query_selector("a.detail")
                    if not link:
                        continue
                    href = link.get_attribute("href") or ""
                    base = f"{list_tab.url.split('//')[0]}//{list_tab.url.split('/')[2]}"
                    collected.append(base + href if href.startswith("/") else href)

                next_li = list_tab.query_selector(".pagination li.next:not(.disabled) a")
                if next_li and (href := next_li.get_attribute("href")):
                    next_url = (list_tab.url.split("?")[0] + href
                                if href.startswith("?") else href)
                    page_no += 1
                    logger.info("Moving to list page %s", page_no)
                    list_tab.goto(next_url, wait_until="domcontentloaded", timeout=30_000)
                    continue
                break  # no further pages

            logger.info("Collected %s detail URLs", len(collected))

            # scrape each detail page in a separate tab
            detail_tab = ctx.new_page()
            for idx, link in enumerate(collected, 1):
                logger.info("[%s/%s] %s", idx, len(collected), link.split('/')[-1])
                detail_tab.goto(link, wait_until="domcontentloaded", timeout=30_000)
                rec = _extract_detail(detail_tab)
                if rec:
                    if not preview:
                        preview = rec
                    _insert_supabase(link, rec)
                    rows_inserted += 1
            detail_tab.close()

        ctx.close(); browser.close()
    return rows_inserted, preview

# ───────────────────────── FastAPI endpoint ─────────────────────────
@router.post("/scrape-tenders", response_model=TenderResponse)
async def scrape_tender(req: TenderRequest):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL")

    logger.info("=== SCRAPE REQUEST === %s", url)
    try:
        loop = asyncio.get_event_loop()
        rows, preview = await loop.run_in_executor(thread_pool, _scrape_sync, url)

        return TenderResponse(
            status="success",
            message=f"{rows} tender row(s) saved to Supabase.",
            data=preview,
            file_path=None,
        )
    except Exception as exc:
        logger.error("Scraping failed: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(500, f"Scraping failed: {type(exc).__name__}: {exc}")
