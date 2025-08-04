# modules/tender.py
"""
Tender-scraping router:
  • POST /tender/scrape-tenders  → scrape, store JSON, push to Supabase
  • GET  /tender/health          → basic health-check
  • GET  /tender/files           → list scraped JSON files
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

from modules.supabase_auth import supabase  # ← Re-use the global client

# ───────────────────────────── logging ──────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ───────────────────────────── models  ──────────────────────────────
class TenderRequest(BaseModel):
    url: str

class TenderResponse(BaseModel):
    status: str
    message: str
    data: dict
    file_path: str | None = None

# ───────────────────────────── router ───────────────────────────────
router = APIRouter()
thread_pool = ThreadPoolExecutor(max_workers=3)  # run sync scraping here

# ────────────────────────── helper utils  ───────────────────────────
def _safe_filename(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1] or "tender"
    slug = "".join(c for c in slug if c.isalnum() or c in "-_")[:30]
    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"tender_{slug}_{ts}.json"

# ──────────────────────── scraping (sync)  ──────────────────────────
def _scrape_sync(url: str) -> Tuple[dict, str]:
    """Scrape synchronously and return (data, absolute_file_path)."""
    data_dir  = Path("scraped_data"); data_dir.mkdir(exist_ok=True)
    out_file  = data_dir / _safe_filename(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
            ],
        )
        ctx   = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page  = ctx.new_page()

        logger.info("Navigating to %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_selector(".box.boxW.listInner .list-desc", timeout=15_000)
        except Exception:
            logger.warning("Primary selector not found; continuing anyway.")

        data: dict = {}
        for desc in page.query_selector_all(".box.boxW.listInner .list-desc"):
            lbl = desc.query_selector("label")
            val = desc.query_selector(".list-desc-inner")
            if lbl and val:
                key = lbl.inner_text().strip().rstrip(":")
                value = val.inner_text().replace("\n", " ").strip()
                if key and value:
                    data[key] = value

        if not data:
            raise RuntimeError("No data extracted – selectors may be wrong.")

        # metadata
        data["_metadata"] = {
            "url": url,
            "scraped_at": datetime.utcnow().isoformat(),
            "total_fields": len(data),
        }

        # write JSON locally
        out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        logger.info("Saved JSON → %s", out_file.resolve())

        # push to Supabase (jsonb column)
        try:
            payload = {
                "url": url,
                "scraped_at": data["_metadata"]["scraped_at"],
                "data": {k: v for k, v in data.items() if not k.startswith("_")},
            }
            res = supabase.table("tenders").insert(payload).execute()
            logger.info("Supabase insert ok (row id %s)", res.data[0]["id"])
        except Exception as db_err:
            logger.warning("Supabase insert failed: %s", db_err)

        # cleanup
        ctx.close(); browser.close()
        return data, str(out_file.absolute())

# ─────────────────────────── endpoints ──────────────────────────────
@router.post("/scrape-tenders", response_model=TenderResponse)
async def scrape_tender(req: TenderRequest):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL")

    logger.info("=== SCRAPING REQUEST STARTED === %s", url)
    try:
        loop = asyncio.get_event_loop()
        data, file_path = await loop.run_in_executor(thread_pool, _scrape_sync, url)
        body = {k: v for k, v in data.items() if not k.startswith("_")}

        return TenderResponse(
            status="success",
            message=f"Scrape successful! Data saved to {file_path}",
            data=body,
            file_path=file_path,
        )
    except Exception as e:
        logger.error("Scraping failed: %s\n%s", e, traceback.format_exc())
        raise HTTPException(500, f"Scraping failed: {type(e).__name__}: {e}")

@router.get("/health")
async def health():
    """Lightweight health-check."""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(thread_pool, lambda: sync_playwright().__enter__().stop())
        return {"status": "healthy", "platform": sys.platform}
    except Exception as e:
        raise HTTPException(503, f"Playwright error: {e}")

@router.get("/files")
async def list_files():
    data_dir = Path("scraped_data")
    files = [
        {"file": f.name, "size": f.stat().st_size, "created": datetime.fromtimestamp(f.stat().st_ctime).isoformat()}
        for f in data_dir.glob("*.json")
    ] if data_dir.exists() else []
    return {"count": len(files), "files": sorted(files, key=lambda x: x["created"], reverse=True)}

# ───────────────────── route-registration helper ────────────────────
def setup_tender_routes(app):
    """Call this from main.py to mount the routes under /tender."""
    app.include_router(router, prefix="/tender", tags=["tender"])
