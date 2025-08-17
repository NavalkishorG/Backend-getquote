from __future__ import annotations

import asyncio
import logging
import traceback
import re
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Dict, Any, Tuple
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, Page
from dotenv import load_dotenv
from cryptography.fernet import Fernet
from supabase import create_client, Client
from supabase.client import ClientOptions

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
    options=ClientOptions(
        postgrest_client_timeout=10,
        storage_client_timeout=10,
    ),
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger("EstimateOneProjectSearch")
router = APIRouter()
thread_pool = ThreadPoolExecutor(max_workers=3)

class ProjectSearchRequest(BaseModel):
    project_ids: List[str]  # Always use list, even for single ID
    url: str = "https://app.estimateone.com/tenders"

class ProjectSearchResponse(BaseModel):
    status: str
    message: str
    data: dict

def decrypt_password(encrypted_password: str) -> str:
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
    if not ENCRYPTION_KEY:
        raise ValueError("ENCRYPTION_KEY not found in environment variables")
    cipher_suite = Fernet(ENCRYPTION_KEY.encode())
    decrypted_password = cipher_suite.decrypt(encrypted_password.encode())
    return decrypted_password.decode()

class EstimateOneProjectSearchScraper:
    def __init__(self, email=None, password=None):
        self.email = email
        self.password = password
        self.login_url = "https://app.estimateone.com/auth/login"
        self.session_cache = {}
        self.session_duration = 1800
        logger.info(f"Loading EstimateOne credentials - Email: {'✓' if self.email else '✗'}, Password: {'✓' if self.password else '✗'}")
        if not self.email or not self.password:
            raise ValueError("Missing EstimateOne email and password")

    def block_resources_aggressive(self, route, request):
        blocked_types = ["image", "stylesheet", "font", "media", "websocket", "manifest"]
        blocked_domains = ["google-analytics", "facebook.com", "twitter.com", "linkedin.com", "doubleclick.net"]
        if request.resource_type in blocked_types:
            route.abort()
        elif any(domain in request.url for domain in blocked_domains):
            route.abort()
        else:
            route.continue_()

    def is_logged_in_ultra_fast(self, page: Page) -> bool:
        try:
            current_url = page.url
            logger.info(f"Checking login status on URL: {current_url}")
            if "/auth/login" not in current_url:
                logger.info("Fast login verified - not on login page")
                return True
            login_indicators = [
                "tbody.styles__tenderRow__b2e48989c7e9117bd552",
                ".styles__projectLink__bb24735487bba39065d8",
                'input[placeholder*="Search by project name"]'
            ]
            for indicator in login_indicators:
                if page.query_selector(indicator):
                    logger.info(f"Fast login verified - found {indicator}")
                    return True
            return False
        except Exception as e:
            logger.error(f"Ultra-fast login check error: {e}")
            return False

    def login_to_estimate_one_fast(self, page: Page) -> bool:
        try:
            logger.info("🔑 Starting fast login attempt...")
            logger.info(f"🔑 Using email: {self.email}")
            logger.info("📍 Navigating to login page...")
            page.goto(self.login_url, timeout=10000, wait_until="commit")
            logger.info("⏳ Waiting for login form...")
            page.wait_for_selector("#user_log_in_email", timeout=8000)
            logger.info("📝 Filling login form...")
            page.fill("#user_log_in_email", self.email)
            page.fill("#user_log_in_plainPassword", self.password)
            logger.info("🚀 Clicking login button...")
            page.click("button.btn.btn-block.btn-lg.btn-primary")
            logger.info("⏳ Waiting for login success...")
            try:
                page.wait_for_function(
                    "() => !window.location.href.includes('/auth/login')",
                    timeout=20000
                )
                logger.info("✅ Login successful - URL changed")
                return True
            except Exception as e1:
                logger.warning(f"URL change method failed: {e1}")
                try:
                    page.wait_for_selector("tbody.styles__tenderRow__b2e48989c7e9117bd552", timeout=10000)
                    logger.info("✅ Login successful - found project rows")
                    return True
                except Exception as e2:
                    logger.warning(f"Dashboard element method failed: {e2}")
                    current_url = page.url
                    if "/auth/login" not in current_url:
                        logger.info("✅ Login successful - manual URL check")
                        return True
                    else:
                        logger.error("❌ Login failed - still on login page")
                        return False
        except Exception as e:
            logger.error(f"❌ Login error: {e}")
            return False

    def check_project_exists_supabase(self, project_id: str) -> bool:
        """Returns True if project ID exists in Supabase, else False."""
        try:
            if not project_id:
                return False
            result = supabase.table("tenders").select("project_id").eq("project_id", project_id).execute()
            exists = bool(result.data)
            if exists:
                logger.info(f"⚠️ Project {project_id} exists in Supabase, skipping.")
            return exists
        except Exception as e:
            logger.error(f"❌ Error checking project in Supabase for {project_id}: {e}")
            return False

    def search_project_by_id(self, page: Page, project_id: str) -> bool:
        try:
            logger.info(f"🔍 Searching for project ID: {project_id}")
            search_input_selector = 'input[placeholder*="Search by project name, project id, address, brand or product"]'
            page.wait_for_selector(search_input_selector, timeout=5000)
            page.click(search_input_selector)
            page.fill(search_input_selector, "")
            page.fill(search_input_selector, project_id)
            logger.info("⏳ Waiting for search results...")
            page.wait_for_selector('.styles__autocomplete__d2da89763ad53db5dcf7', timeout=5000)
            suggested_project = page.query_selector('.styles__suggestedProject__f400d5576aec8e4ea183 a')
            if suggested_project:
                logger.info(f"✅ Found project {project_id} in autocomplete")
                suggested_project.click()
                return True
            else:
                logger.warning(f"❌ Project {project_id} not found in autocomplete")
                return False
        except Exception as e:
            logger.error(f"❌ Error searching for project {project_id}: {e}")
            return False

    def expand_read_more_in_popup(self, details_section, page):
        """Patch: expand all builder descriptions if global read more button is present."""
        try:
            read_btn = details_section.query_selector("a.styles__hideShow__e8f2d705067479d13623")
            if read_btn:
                read_btn.scroll_into_view_if_needed()
                page.evaluate("el => el.click()", read_btn)
                time.sleep(1.2)
                logger.info("Clicked global Read more for builder descriptions")
        except Exception as e:
            logger.debug(f"Error clicking 'Read more': {e}")

    def extract_project_details_fast(self, page: Page) -> Dict[str, Any]:
        details = {}
        try:
            detail_selectors = [
                "#project-details",
                ".styles__projectSection__f1b9aeb71ec0b48e56e0",
                ".ReactModal__Content"
            ]
            details_section = None
            for selector in detail_selectors:
                try:
                    page.wait_for_selector(selector, timeout=800)
                    details_section = page.query_selector(selector)
                    if details_section:
                        break
                except:
                    continue
            if not details_section:
                details_section = page.query_selector(".ReactModal__Content, [role='dialog']")
            if not details_section:
                return details

            # --- PATCH: expand all builder descriptions before scraping ---
            self.expand_read_more_in_popup(details_section, page)

            all_text = details_section.inner_text()
            trades_match = re.search(r'(\d+)\s+trades', all_text)
            if trades_match:
                details["Number of Trades"] = trades_match.group(1)
            deadline_match = re.search(r'submitted by\s+(.+?)\.', all_text)
            if deadline_match:
                details["Submission Deadline"] = deadline_match.group(1).strip()
            overall_budget_elem = details_section.query_selector(".styles__budgetRange__b101ae22d71fd54397d0")
            if overall_budget_elem:
                details["Overall Budget"] = overall_budget_elem.inner_text().strip()
            project_name_elem = details_section.query_selector("h1, h2, h3, .project-title, [class*='title']")
            if project_name_elem:
                details["Project Name"] = project_name_elem.inner_text().strip()
            address_selectors = [".styles__projectAddress__e13a9deabdbf43356939", "[class*='address']", "[class*='location']"]
            for selector in address_selectors:
                address_elem = details_section.query_selector(selector)
                if address_elem:
                    details["Project Address"] = address_elem.inner_text().strip()
                    break
            # Builder descriptions
            builder_descriptions = []
            description_items = details_section.query_selector_all(".styles__stageDescription__a6f572d1edbede52b379")
            for item in description_items:
                description_data = {}
                builder_name_elem = item.query_selector("strong")
                if builder_name_elem:
                    builder_text = builder_name_elem.inner_text().strip()
                    builder_name = builder_text.replace(" says:", "").strip()
                    description_data["builder_name"] = builder_name
                full_description = item.inner_text().strip()
                # Optionally: parse builder_budget as in previous logic if needed
                description_data["description"] = full_description
                if description_data:
                    builder_descriptions.append(description_data)
            if builder_descriptions:
                details["Builder Descriptions"] = builder_descriptions
            return details
        except Exception as e:
            logger.warning(f"Fast extraction error (continuing): {e}")
            return details

    def close_popup_fast(self, page: Page):
        try:
            page.keyboard.press("Escape")
            try:
                page.wait_for_selector(".ReactModal__Overlay--after-open", state="hidden", timeout=300)
                return "success"
            except:
                page.keyboard.press("Escape")
                return "success"
        except Exception as e:
            logger.warning(f"Popup close error (continuing): {e}")
            return "success"

    def insert_to_supabase(self, project_data: Dict[str, Any]) -> bool:
        try:
            supabase_data = {
                "url": project_data.get("source_url", ""),
                "scraped_at": datetime.utcnow().isoformat(),
                "project_name": project_data.get("Project Name"),
                "project_id": project_data.get("Project ID"),
                "project_address": project_data.get("Project Address"),
                "max_budget": project_data.get("Max Budget"),
                "distance": project_data.get("Distance"),
                "category": project_data.get("Category"),
                "builder": project_data.get("Builder"),
                "quote_due_builder": project_data.get("Quote Due (Builder)"),
                "project_due_date": project_data.get("Project Due Date"),
                "has_documents": project_data.get("Has Documents") == "Yes",
                "interest_level": project_data.get("Interest Level"),
                "number_of_trades": int(project_data.get("Number of Trades", 0)) if project_data.get("Number of Trades") else None,
                "submission_deadline": project_data.get("Submission Deadline"),
                "overall_budget": project_data.get("Overall Budget"),
                "builder_descriptions": project_data.get("Builder Descriptions"),
            }
            supabase_data = {k: v for k, v in supabase_data.items() if v is not None}
            result = supabase.table("tenders").insert(supabase_data).execute()
            if result.data:
                logger.info(f"✅ Inserted project '{project_data.get('Project Name', 'Unknown')}' to Supabase")
                return True
            else:
                logger.error(f"❌ Failed to insert project to Supabase: {result}")
                return False
        except Exception as e:
            logger.error(f"❌ Supabase insertion error: {e}")
            return False

def _process_projects_by_ids_sync(
    project_ids: List[str],
    url: str,
    estimate_one_email: str,
    estimate_one_password: str
) -> dict:
    """Process multiple project IDs, expanding 'read more', skipping already present projects."""
    results = {"processed": 0, "failed": 0, "details": [], "sample_project": {}}
    scraper = EstimateOneProjectSearchScraper(email=estimate_one_email, password=estimate_one_password)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-extensions',
                '--disable-plugins'
            ]
        )
        context = browser.new_context()
        context.route("**/*", scraper.block_resources_aggressive)
        page = context.new_page()
        try:
            logger.info("🌐 Opening EstimateOne URL: %s", url)
            page.goto(url, wait_until="commit")
            if not scraper.is_logged_in_ultra_fast(page):
                if not scraper.login_to_estimate_one_fast(page):
                    raise RuntimeError("❌ Login failed")
                page.goto(url, wait_until="commit")
            for i, project_id in enumerate(project_ids, 1):
                if scraper.check_project_exists_supabase(project_id):
                    results["details"].append(f"Project {project_id}: SKIPPED (already exists in database)")
                    continue
                try:
                    logger.info(f"🔄 Processing project {i}/{len(project_ids)}: ID {project_id}")
                    if not scraper.search_project_by_id(page, project_id):
                        results["failed"] += 1
                        results["details"].append(f"Project {project_id}: Not found in search")
                        continue
                    try:
                        page.wait_for_selector(".ReactModal__Content, [role='dialog'], #project-details", timeout=3000)
                    except:
                        logger.warning(f"⚠️ Popup may not have opened for project {project_id}")
                    project_data = scraper.extract_project_details_fast(page)
                    project_data["Project ID"] = project_id
                    project_data["source_url"] = url
                    project_data["scraped_at"] = datetime.now().isoformat()
                    if scraper.insert_to_supabase(project_data):
                        results["processed"] += 1
                        if not results["sample_project"]:
                            results["sample_project"] = {
                                "project_name": project_data.get("Project Name"),
                                "project_id": project_data.get("Project ID"),
                                "overall_budget": project_data.get("Overall Budget"),
                                "number_of_trades": project_data.get("Number of Trades")
                            }
                        logger.info(f"✅ Successfully processed project {project_id}")
                    else:
                        results["failed"] += 1
                        results["details"].append(f"Project {project_id}: Database insertion failed")
                    scraper.close_popup_fast(page)
                    time.sleep(0.5)
                except Exception as e:
                    results["failed"] += 1
                    results["details"].append(f"Project {project_id}: {str(e)}")
                    logger.error(f"❌ Error processing project {project_id}: {e}")
                    try:
                        scraper.close_popup_fast(page)
                    except:
                        pass
        finally:
            context.close()
            browser.close()
    return results

@router.post("/scrape-projects", response_model=ProjectSearchResponse)
async def scrape_projects_by_ids(
    req: ProjectSearchRequest,
    authorization: str = Header(None)
):
    if not req.project_ids:
        raise HTTPException(400, "No project IDs provided")
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL format")
    if "estimateone.com" not in url:
        raise HTTPException(400, "Only EstimateOne.com URLs are supported")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required. Please login first.")
    token = authorization.split(" ")[1]
    try:
        user = await asyncio.to_thread(lambda: supabase.auth.get_user(token))
        if not user.user:
            raise HTTPException(status_code=401, detail="Authentication token expired. Please login again.")
        user_id = user.user.id
        logger.info(f"🔐 Project search request from user: {user_id}")
    except Exception as e:
        raise HTTPException(status_code=401, detail="Authentication failed. Please login again.")
    try:
        logger.info(f"📋 Fetching credentials for user: {user_id}")
        result = await asyncio.to_thread(
            lambda: supabase.table("user_credentials")
            .select("email, password_encrypted")
            .eq("user_id", user_id)
            .eq("credential_type", "estimate_one")
            .execute()
        )
        if not result.data:
            raise HTTPException(
                status_code=404,
                detail="EstimateOne credentials not found. Please login again to store them."
            )
        credential_data = result.data[0]
        estimate_one_email = credential_data["email"]
        estimate_one_password = decrypt_password(credential_data["password_encrypted"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching credentials: {e}")
        raise HTTPException(
            status_code=500,
            detail="Database connection failed. Please try again later."
        )
    logger.info(f"=== PROJECT SEARCH REQUEST === {len(req.project_ids)} project IDs")
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            thread_pool,
            _process_projects_by_ids_sync,
            req.project_ids,
            url,
            estimate_one_email,
            estimate_one_password
        )
        total_projects = len(req.project_ids)
        processed_count = results["processed"]
        failed_count = results["failed"]
        if processed_count > 0 and failed_count == 0:
            message = f"🎉 Successfully processed all {processed_count} projects!"
            status = "success"
        elif processed_count > 0 and failed_count > 0:
            message = f"✅ Processed {processed_count}/{total_projects} projects. {failed_count} failed."
            status = "partial_success"
        else:
            message = f"❌ Failed to process any projects. {failed_count}/{total_projects} errors."
            status = "failed"
        return ProjectSearchResponse(
            status=status,
            message=message,
            data={
                "total_requested": total_projects,
                "processed": processed_count,
                "failed": failed_count,
                "success_rate": f"{(processed_count/total_projects)*100:.1f}%",
                "sample_project": results.get("sample_project", {}),
                "error_details": results.get("details", []),
                "scraped_at": datetime.utcnow().isoformat(),
                "source": "EstimateOne Project Search"
            }
        )
    except Exception as exc:
        logger.error("❌ Project search failed: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Project search failed: {type(exc).__name__}. Please try again."
        )
