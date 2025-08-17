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
from pathlib import Path
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
logger = logging.getLogger("EstimateOneService")
router = APIRouter()
thread_pool = ThreadPoolExecutor(max_workers=3)

class EstimateOneRequest(BaseModel):
    url: str

class ProjectScrapeRequest(BaseModel):
    project_ids: List[str]
    url: str = "https://app.estimateone.com/tenders"

class EstimateOneResponse(BaseModel):
    status: str
    message: str
    data: dict
    file_path: str | None = None

def decrypt_password(encrypted_password: str) -> str:
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
    if not ENCRYPTION_KEY:
        raise ValueError("ENCRYPTION_KEY not found in environment variables")
    cipher_suite = Fernet(ENCRYPTION_KEY.encode())
    decrypted_password = cipher_suite.decrypt(encrypted_password.encode())
    return decrypted_password.decode()

class EstimateOneAPIScraper:
    def __init__(self, email=None, password=None):
        self.email = email
        self.password = password
        self.login_url = "https://app.estimateone.com/auth/login"
        self.session_cache = {}
        self.session_duration = 1800
        self.scraped_projects = []
        logger.info(f"Loading EstimateOne credentials - Email: {'✓' if self.email else '✗'}")
        if not self.email or not self.password:
            raise ValueError("Missing EstimateOne email and password")

    def check_project_exists_early(self, project_id: str) -> bool:
        try:
            if not project_id:
                return False
            result = supabase.table("tenders").select("project_id").eq("project_id", project_id).execute()
            exists = bool(result.data)
            if exists:
                logger.info(f"⚠️ DUPLICATE FOUND: Project ID {project_id} already exists in database - SKIPPING")
                return True
            else:
                logger.debug(f"✅ NEW PROJECT: Project ID {project_id} is new - will process")
                return False
        except Exception as e:
            logger.error(f"❌ Error in early duplicate check for {project_id}: {e}")
            return False

    def filter_duplicate_project_ids(self, project_ids: List[str]) -> Tuple[List[str], List[str]]:
        if not project_ids:
            return [], []
        new_ids, duplicate_ids = [], []
        for project_id in project_ids:
            if self.check_project_exists_early(project_id):
                duplicate_ids.append(project_id)
            else:
                new_ids.append(project_id)
        return new_ids, duplicate_ids

    def get_cached_session(self):
        current_time = time.time()
        if 'login_time' in self.session_cache:
            time_elapsed = current_time - self.session_cache['login_time']
            if time_elapsed < self.session_duration:
                logger.debug(f"Using cached session ({int(self.session_duration - time_elapsed)}s remaining)")
                return True
        return False

    def cache_session(self):
        self.session_cache['login_time'] = time.time()
        logger.debug("Login session cached")

    def block_resources_aggressive(self, route, request):
        blocked_types = ["image", "stylesheet", "font", "media", "websocket", "manifest"]
        blocked_domains = ["google-analytics", "facebook.com", "twitter.com", "linkedin.com", "doubleclick.net"]
        if request.resource_type in blocked_types:
            route.abort()
        elif any(domain in request.url for domain in blocked_domains):
            route.abort()
        else:
            route.continue_()

    def _convert_to_int(self, value):
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except (ValueError, AttributeError):
            return None

    def insert_to_supabase(self, project_data: Dict[str, Any]) -> bool:
        try:
            self.scraped_projects.append(project_data.copy())
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
                "number_of_trades": self._convert_to_int(project_data.get("Number of Trades")),
                "submission_deadline": project_data.get("Submission Deadline"),
                "overall_budget": project_data.get("Overall Budget"),
                "builder_descriptions": project_data.get("Builder Descriptions"),
                "row_number": project_data.get("Row Number")
            }
            supabase_data = {k: v for k, v in supabase_data.items() if v is not None}
            result = supabase.table("tenders").insert(supabase_data).execute()
            if result.data:
                logger.info(f"Successfully saved project ID {project_data.get('Project ID')} to database")
                return True
            else:
                logger.error(f"Failed to insert project to database - no data returned")
                return False
        except Exception as e:
            logger.error(f"Database insertion error for project ID {project_data.get('Project ID')}: {e}")
            return False

    def is_logged_in_ultra_fast(self, page: Page) -> bool:
        try:
            current_url = page.url
            logger.debug(f"Checking login status on URL: {current_url}")
            if "/auth/login" not in current_url:
                logger.debug("Fast login verified - not on login page")
                return True
            login_indicators = [
                "tbody.styles__tenderRow__b2e48989c7e9117bd552",
                ".styles__projectLink__bb24735487bba39065d8",
                'input[placeholder*="Search by project name"]'
            ]
            for indicator in login_indicators:
                if page.query_selector(indicator):
                    logger.debug(f"Fast login verified - found {indicator}")
                    return True
            return False
        except Exception as e:
            logger.warning(f"Ultra-fast login check error: {e}")
            return False

    def is_logged_in(self, page: Page) -> bool:
        try:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=3000)
            except:
                pass
            logged_in_indicators = [
                "tbody.styles__tenderRow__b2e48989c7e9117bd552",
                ".styles__projectLink__bb24735487bba39065d8"
            ]
            for indicator in logged_in_indicators:
                try:
                    element = page.wait_for_selector(indicator, timeout=1500)
                    if element:
                        logger.debug(f"Login verified - found element with selector: {indicator}")
                        return True
                except:
                    continue
            current_url = page.url
            if not "/auth/login" in current_url and "estimateone.com" in current_url:
                logger.debug(f"Login verified - on main app page: {current_url}")
                return True
            return False
        except Exception as e:
            logger.warning(f"Error checking login status: {e}")
            return False

    def login_to_estimate_one_fast(self, page: Page) -> bool:
        try:
            logger.info("Starting login attempt...")
            page.goto(self.login_url, timeout=10000, wait_until="commit")
            page.wait_for_selector("#user_log_in_email", timeout=8000)
            page.fill("#user_log_in_email", self.email)
            page.fill("#user_log_in_plainPassword", self.password)
            page.click("button.btn.btn-block.btn-lg.btn-primary")
            try:
                page.wait_for_function(
                    "() => !window.location.href.includes('/auth/login')",
                    timeout=20000
                )
                logger.info("Login successful - URL changed")
                return True
            except Exception as e1:
                logger.debug(f"URL change method failed: {e1}")
                try:
                    page.wait_for_selector("tbody.styles__tenderRow__b2e48989c7e9117bd552", timeout=10000)
                    logger.info("Login successful - found project rows")
                    return True
                except Exception as e2:
                    logger.debug(f"Dashboard element method failed: {e2}")
                    current_url = page.url
                    if "/auth/login" not in current_url:
                        logger.info("Login successful - manual URL check")
                        return True
                    else:
                        logger.error("Login failed - still on login page")
                        return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    def click_read_more_if_present(self, page: Page, item_element):
        try:
            read_more_selectors = [
                "a.styles__hideShow__e8f2d705067479d13623",
                "a[href='#project-details']",
                "a:has-text('Read more')",
                ".styles__hideShowWrapper__cf01bc021f03d3785134 a"
            ]
            for selector in read_more_selectors:
                read_more_elem = item_element.query_selector(selector)
                if read_more_elem:
                    read_more_elem.scroll_into_view_if_needed()
                    page.evaluate("el => el.click()", read_more_elem)
                    time.sleep(0.8)
                    return True
            return False
        except Exception as e:
            logger.debug(f"Error in Read More clicking: {e}")
            return False

    def extract_full_description_advanced(self, page: Page, item_element) -> tuple:
        self.click_read_more_if_present(page, item_element)
        raw = item_element.inner_text().strip()
        if not raw:
            return "", ""
        parts = re.split(r'(?i)their approximate budget is|approximate budget', raw, 1)
        full_desc = parts[0].strip()
        builder_budget = ""
        if len(parts) > 1:
            m = re.search(
                r'\$[\d,]+(?:\.\d+)?[mk]?\s*-\s*\$[\d,]+(?:\.\d+)?[mk]?|\$[\d,]+(?:\.\d+)?[mk]?',
                parts[1]
            )
            if m:
                builder_budget = m.group(0).strip()
        return full_desc, builder_budget

    def extract_project_details_fast(self, page: Page) -> Dict[str, Any]:
        details = {}
        try:
            logger.debug("Extracting project details from popup...")
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
                        logger.debug(f"Found details section with selector: {selector}")
                        break
                except:
                    continue
            if not details_section:
                details_section = page.query_selector(".ReactModal__Content, [role='dialog']")
                if details_section:
                    logger.debug("Found details section with fallback selector")
            if not details_section:
                logger.warning("No details section found")
                return details
            read_btn = details_section.query_selector("a.styles__hideShow__e8f2d705067479d13623")
            if read_btn:
                read_btn.scroll_into_view_if_needed()
                page.evaluate("el => el.click()", read_btn)
                time.sleep(1.2)
            all_text = details_section.inner_text().strip()
            project_name_elem = details_section.query_selector("h1, h2, h3, .project-title, [class*='title']")
            if project_name_elem:
                details["Project Name"] = project_name_elem.inner_text().strip()
            address_selectors = [
                ".styles__projectAddress__e13a9deabdbf43356939",
                "[class*='address']",
                "[class*='location']"
            ]
            for selector in address_selectors:
                address_elem = details_section.query_selector(selector)
                if address_elem:
                    details["Project Address"] = address_elem.inner_text().strip()
                    break
            trades_match = re.search(r'(\d+)\s+trades', all_text)
            if trades_match:
                details["Number of Trades"] = trades_match.group(1)
            deadline_match = re.search(r'submitted by\s+(.+?)\.', all_text)
            if deadline_match:
                details["Submission Deadline"] = deadline_match.group(1).strip()
            overall_budget_elem = details_section.query_selector(".styles__budgetRange__b101ae22d71fd54397d0")
            if overall_budget_elem:
                details["Overall Budget"] = overall_budget_elem.inner_text().strip()
            builder_descriptions = []
            description_items = details_section.query_selector_all(".styles__stageDescription__a6f572d1edbede52b379")
            for item in description_items:
                description_data = {}
                builder_name_elem = item.query_selector("strong")
                if builder_name_elem:
                    builder_text = builder_name_elem.inner_text().strip()
                    builder_name = builder_text.replace(" says:", "").strip()
                    description_data["builder_name"] = builder_name
                full_description, builder_budget = self.extract_full_description_advanced(page, item)
                if full_description:
                    description_data["description"] = full_description
                if builder_budget:
                    description_data["builder_budget"] = builder_budget
                if description_data:
                    builder_descriptions.append(description_data)
            if builder_descriptions:
                details["Builder Descriptions"] = builder_descriptions
            logger.debug(f"Successfully extracted {len(details)} fields from popup")
            return details
        except Exception as e:
            logger.warning(f"Fast extraction error (continuing): {e}")
            return details

    def extract_single_project_row(self, row_element) -> Dict[str, Any]:
        record = {}
        try:
            project_name_elem = row_element.query_selector(".styles__projectLink__bb24735487bba39065d8")
            if project_name_elem:
                record["Project Name"] = project_name_elem.inner_text().strip()
            project_id_elem = row_element.query_selector(".styles__projectId__a99146050623e131a1bf")
            if project_id_elem:
                record["Project ID"] = project_id_elem.inner_text().strip()
            address_elem = row_element.query_selector(".styles__projectAddress__e13a9deabdbf43356939")
            if address_elem:
                record["Project Address"] = address_elem.inner_text().strip()
            budget_elem = row_element.query_selector(".styles__budgetRange__b101ae22d71fd54397d0")
            if budget_elem:
                record["Max Budget"] = budget_elem.inner_text().strip()
            distance_cells = row_element.query_selector_all("td")
            for cell in distance_cells:
                text = cell.inner_text().strip()
                if "km" in text and text.endswith("km"):
                    record["Distance"] = text
                    break
            category_elem = row_element.query_selector(".styles__lowPriority__ca01365a4bba34b27c8a span")
            if category_elem:
                record["Category"] = category_elem.inner_text().strip()
            builder_elem = row_element.query_selector(".styles__builderName__f71d1b6dc7d0969616ea")
            if builder_elem:
                record["Builder"] = builder_elem.inner_text().strip()
            quote_date_elem = row_element.query_selector(".styles__quoteDate__b21c670d4b980f23ba7c .styles__projectDate__efdf1ddef6a4526d58ac")
            if quote_date_elem:
                record["Quote Due (Builder)"] = quote_date_elem.inner_text().strip()
            project_due_elems = row_element.query_selector_all(".styles__projectDate__efdf1ddef6a4526d58ac")
            if len(project_due_elems) > 1:
                record["Project Due Date"] = project_due_elems[-1].inner_text().strip()
            elif len(project_due_elems) == 1:
                record["Project Due Date"] = project_due_elems[0].inner_text().strip()
            no_docs_elem = row_element.query_selector(".styles__noDocsTag__d3dc744a652a94be3eea")
            record["Has Documents"] = "No" if no_docs_elem else "Yes"
            interest_elem = row_element.query_selector(".reactSelect__single-value")
            if interest_elem:
                record["Interest Level"] = interest_elem.inner_text().strip()
            else:
                record["Interest Level"] = "Please Select"
            return record
        except Exception as e:
            if "Connection closed" in str(e) or "Target page" in str(e):
                logger.warning(f"Browser connection lost during extraction: {e}")
                return {}
            else:
                logger.warning(f"Error extracting single project row: {e}")
                return record

    def close_popup_fast(self, page: Page):
        try:
            logger.debug("Closing popup...")
            page.keyboard.press("Escape")
            try:
                page.wait_for_selector(".ReactModal__Overlay--after-open", state="hidden", timeout=300)
                return "success"
            except:
                page.keyboard.press("Escape")
                return "success"
        except Exception as e:
            logger.debug(f"Popup close error (continuing): {e}")
            return "success"

    def search_project_by_id_and_extract_row_data(self, page: Page, project_id: str) -> Dict[str, Any]:
        try:
            logger.debug(f"Searching for project ID: {project_id}")
            current_url = page.url
            if "search" in current_url.lower() or "project" in current_url.lower():
                logger.debug("Navigating back to main tenders page...")
                page.goto("https://app.estimateone.com/tenders", wait_until="commit", timeout=8000)
                time.sleep(1)
            search_input_selector = 'input[placeholder*="Search by project name, project id, address, brand or product"]'
            page.wait_for_selector(search_input_selector, timeout=5000)
            page.click(search_input_selector)
            page.fill(search_input_selector, "")
            page.fill(search_input_selector, project_id)
            search_button_selector = 'button.btn.btn-primary.ml-1.fs-ignore-dead-clicks'
            try:
                page.wait_for_selector(search_button_selector, timeout=2000)
                page.click(search_button_selector)
                logger.debug("Clicked search button")
                time.sleep(2)
            except:
                page.keyboard.press("Enter")
                logger.debug("Pressed Enter key as fallback")
                time.sleep(2)
            project_data = {}
            try:
                page.wait_for_selector('.styles__autocomplete__d2da89763ad53db5dcf7', timeout=3000)
                logger.debug("Found autocomplete dropdown")
                suggested_project = page.query_selector('.styles__suggestedProject__f400d5576aec8e4ea183 a')
                if suggested_project:
                    logger.debug(f"Found project {project_id} in autocomplete - clicking...")
                    suggested_project.click()
                    time.sleep(1)
                    popup_data = self.extract_project_details_fast(page)
                    project_data.update(popup_data)
                    return project_data
            except:
                logger.debug("No autocomplete dropdown, checking for search results page...")
                try:
                    page.wait_for_selector("tbody.styles__tenderRow__b2e48989c7e9117bd552", timeout=5000)
                    logger.debug("Found search results page")
                    project_rows = page.query_selector_all("tbody.styles__tenderRow__b2e48989c7e9117bd552")
                    for idx, row in enumerate(project_rows):
                        project_id_elem = row.query_selector(".styles__projectId__a99146050623e131a1bf")
                        if project_id_elem:
                            row_project_id = project_id_elem.inner_text().strip()
                            if project_id in row_project_id:
                                logger.debug(f"Found matching project {project_id} in search results")
                                project_data = self.extract_single_project_row(row)
                                project_link = row.query_selector(".styles__projectLink__bb24735487bba39065d8")
                                if project_link:
                                    project_link.click()
                                    time.sleep(1)
                                    popup_data = self.extract_project_details_fast(page)
                                    project_data.update(popup_data)
                                return project_data
                except Exception as search_error:
                    logger.warning(f"Error in search results processing: {search_error}")
                    logger.warning(f"Project {project_id} not found in search results")
                    return {}
            return {}
        except Exception as e:
            logger.error(f"Error searching for project {project_id}: {e}")
            return {}

def _scrape_estimate_one_sync(url: str, estimate_one_email: str, estimate_one_password: str) -> Tuple[int, dict, None]:
    rows_inserted, preview = 0, {}
    skipped_duplicates = 0
    scraper = EstimateOneAPIScraper(email=estimate_one_email, password=estimate_one_password)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--disable-extensions','--disable-plugins',
                '--memory-pressure-off','--max_old_space_size=2048',
                '--disable-background-timer-throttling','--disable-backgrounding-occluded-windows','--disable-renderer-backgrounding',
                '--disable-features=TranslateUI','--disable-ipc-flooding-protection'
            ]
        )
        context = browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            ignore_https_errors=True
        )
        context.route("**/*", scraper.block_resources_aggressive)
        context.set_default_timeout(8000)
        page = context.new_page()
        try:
            logger.info(f"Opening EstimateOne URL: {url}")
            page.goto(url, wait_until="commit", timeout=15000)
            if not scraper.is_logged_in_ultra_fast(page):
                if not scraper.get_cached_session():
                    logger.info("Need to login...")
                    if scraper.login_to_estimate_one_fast(page):
                        scraper.cache_session()
                        page.goto(url, wait_until="commit", timeout=10000)
                    else:
                        raise RuntimeError("Login failed")
                else:
                    page.reload(wait_until="commit", timeout=8000)
                if not scraper.is_logged_in(page):
                    logger.info("Cached session invalid, need fresh login...")
                    if scraper.login_to_estimate_one_fast(page):
                        scraper.cache_session()
                        page.goto(url, wait_until="commit", timeout=10000)
                    else:
                        raise RuntimeError("Login failed")
            logger.debug("Waiting for project rows to load...")
            page.wait_for_selector("tbody.styles__tenderRow__b2e48989c7e9117bd552", timeout=10000)
            project_rows = page.query_selector_all("tbody.styles__tenderRow__b2e48989c7e9117bd552")
            logger.info(f"Found {len(project_rows)} project rows")
            if not project_rows:
                raise RuntimeError("No project rows found on page")
            all_project_ids = []
            for i, row in enumerate(project_rows, 1):
                project_id_elem = row.query_selector(".styles__projectId__a99146050623e131a1bf")
                if project_id_elem:
                    project_id = project_id_elem.inner_text().strip()
                    all_project_ids.append((i, project_id, row))
                else:
                    logger.warning(f"Could not extract project ID from row {i}")
            projects_to_process = []
            for row_num, project_id, row_element in all_project_ids:
                if not scraper.check_project_exists_early(project_id):
                    projects_to_process.append((row_num, project_id, row_element))
                else:
                    skipped_duplicates += 1
            for row_num, project_id, row in projects_to_process:
                project_data = scraper.extract_single_project_row(row)
                if project_data:
                    project_data["Row Number"] = row_num
                    project_data["source_url"] = url
                    project_data["scraped_at"] = datetime.now().isoformat()
                    project_link = row.query_selector(".styles__projectLink__bb24735487bba39065d8")
                    if project_link:
                        try:
                            project_link.click(force=True)
                            try:
                                page.wait_for_selector("[class*='project'], .ReactModal__Content, #project-details", timeout=2000)
                            except:
                                pass
                            detailed_info = scraper.extract_project_details_fast(page)
                            project_data.update(detailed_info)
                            scraper.close_popup_fast(page)
                        except Exception as e:
                            logger.warning(f"Error processing popup for project {row_num}: {e}")
                            page.keyboard.press("Escape")
                    if scraper.insert_to_supabase(project_data):
                        rows_inserted += 1
                        if not preview:
                            preview = {
                                "project_name": project_data.get("Project Name"),
                                "project_id": project_data.get("Project ID"),
                                "category": project_data.get("Category"),
                                "max_budget": project_data.get("Max Budget"),
                                "number_of_trades": project_data.get("Number of Trades")
                            }
        finally:
            context.close()
            browser.close()
    return rows_inserted, preview, None

def _scrape_projects_by_ids_sync(
    project_ids: List[str],
    url: str,
    estimate_one_email: str,
    estimate_one_password: str
) -> dict:
    results = {"processed": 0, "failed": 0, "details": [], "sample_project": {}, "json_file_path": None}
    successfully_processed_ids = []
    scraper = EstimateOneAPIScraper(email=estimate_one_email, password=estimate_one_password)
    new_project_ids, duplicate_project_ids = scraper.filter_duplicate_project_ids(project_ids)
    if duplicate_project_ids:
        for dup_id in duplicate_project_ids:
            results["details"].append(f"Project {dup_id}: SKIPPED (already exists in database)")
    if not new_project_ids:
        results["successfully_processed_ids"] = duplicate_project_ids
        return results
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--disable-extensions','--disable-plugins'
            ]
        )
        context = browser.new_context()
        context.route("**/*", scraper.block_resources_aggressive)
        page = context.new_page()
        try:
            logger.info(f"Opening EstimateOne URL: {url}")
            page.goto(url, wait_until="commit")
            if not scraper.is_logged_in_ultra_fast(page):
                logger.info("Not logged in, attempting login...")
                if not scraper.login_to_estimate_one_fast(page):
                    raise RuntimeError("Login failed")
                logger.info("Login successful, navigating back to main page...")
                page.goto(url, wait_until="commit")
            for i, project_id in enumerate(new_project_ids, 1):
                try:
                    project_data = scraper.search_project_by_id_and_extract_row_data(page, project_id)
                    if not project_data:
                        results["failed"] += 1
                        results["details"].append(f"Project {project_id}: Not found in search")
                        continue
                    project_data["Project ID"] = project_id
                    project_data["source_url"] = url
                    project_data["scraped_at"] = datetime.now().isoformat()
                    if scraper.insert_to_supabase(project_data):
                        results["processed"] += 1
                        successfully_processed_ids.append(project_id)
                        if not results["sample_project"]:
                            results["sample_project"] = {
                                "project_name": project_data.get("Project Name"),
                                "project_id": project_data.get("Project ID"),
                                "overall_budget": project_data.get("Overall Budget"),
                                "number_of_trades": project_data.get("Number of Trades")
                            }
                        results["details"].append(f"Project {project_id}: Successfully processed")
                    else:
                        results["failed"] += 1
                        results["details"].append(f"Project {project_id}: Database insertion failed")
                except Exception as e:
                    results["failed"] += 1
                    error_msg = f"Project {project_id}: {str(e)}"
                    results["details"].append(error_msg)
                    logger.error(f"Error processing project {project_id}: {e}")
        finally:
            context.close()
            browser.close()
    results["successfully_processed_ids"] = successfully_processed_ids + duplicate_project_ids
    return results

@router.post("/scrape-tenders", response_model=EstimateOneResponse)
async def scrape_estimate_one(req: EstimateOneRequest, authorization: str = Header(None)):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL format. URL must start with http:// or https://")
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
        logger.info(f"Scraping request from user: {user_id}")
    except Exception as e:
        raise HTTPException(status_code=401, detail="Authentication failed. Please login again.")
    try:
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
        logger.error(f"Error fetching credentials for user {user_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Database connection failed. Please try again later."
        )
    logger.info(f"Starting EstimateOne scrape request for URL: {url}")
    try:
        loop = asyncio.get_event_loop()
        rows, preview, _ = await loop.run_in_executor(
            thread_pool,
            _scrape_estimate_one_sync,
            url,
            estimate_one_email,
            estimate_one_password
        )
        return EstimateOneResponse(
            status="success",
            message=f"{rows} EstimateOne project(s) saved to Supabase.",
            data={
                "projects_scraped": rows,
                "sample_project": preview,
                "scraped_at": datetime.utcnow().isoformat(),
                "source": "EstimateOne",
            },
            file_path=None,
        )
    except ValueError as ve:
        if "Missing EstimateOne email and password" in str(ve):
            raise HTTPException(
                status_code=400,
                detail="Invalid EstimateOne credentials. Please check your login details."
            )
        raise HTTPException(status_code=400, detail=str(ve))
    except ConnectionError:
        raise HTTPException(
            status_code=503,
            detail="Cannot connect to EstimateOne. Please check your internet connection."
        )
    except TimeoutError:
        raise HTTPException(
            status_code=408,
            detail="EstimateOne login timeout. Please try again."
        )
    except Exception as exc:
        error_msg = str(exc).lower()
        if "invalid credentials" in error_msg or "login failed" in error_msg:
            raise HTTPException(
                status_code=401,
                detail="EstimateOne login failed. Please check your credentials."
            )
        elif "page not found" in error_msg or "404" in error_msg:
            raise HTTPException(
                status_code=404,
                detail="EstimateOne page not found. Please check the URL."
            )
        elif "access denied" in error_msg or "forbidden" in error_msg:
            raise HTTPException(
                status_code=403,
                detail="Access denied to EstimateOne page. Check your account permissions."
            )
        else:
            logger.error(f"EstimateOne scraping failed: {exc}\n{traceback.format_exc()}")
            raise HTTPException(
                status_code=500,
                detail=f"Scraping failed: {type(exc).__name__}. Please try again or contact support."
            )

@router.post("/scrape-project", response_model=EstimateOneResponse)
async def scrape_projects_by_ids(
    req: ProjectScrapeRequest,
    authorization: str = Header(None)
):
    logger.info(f"Received project scrape request for {len(req.project_ids)} project IDs: {req.project_ids}")
    if not req.project_ids:
        raise HTTPException(400, "No project IDs provided")
    if not isinstance(req.project_ids, list):
        raise HTTPException(400, "project_ids must be a list")
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
        logger.info(f"Authentication successful for user: {user_id}")
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        raise HTTPException(status_code=401, detail="Authentication failed. Please login again.")
    try:
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
        logger.error(f"Error fetching credentials: {e}")
        raise HTTPException(
            status_code=500,
            detail="Database connection failed. Please try again later."
        )
    logger.info(f"Starting project processing for {len(req.project_ids)} project IDs")
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            thread_pool,
            _scrape_projects_by_ids_sync,
            req.project_ids,
            url,
            estimate_one_email,
            estimate_one_password
        )
        successfully_processed_ids = results.get("successfully_processed_ids", [])
        if successfully_processed_ids:
            logger.info(f"Successfully processed IDs (should be deleted from storage): {successfully_processed_ids}")
        total_projects = len(req.project_ids)
        processed_count = results["processed"]
        failed_count = results["failed"]
        if processed_count > 0 and failed_count == 0:
            message = f"Successfully processed all {processed_count} projects."
            status = "success"
        elif processed_count > 0 and failed_count > 0:
            message = f"Processed {processed_count}/{total_projects} projects. {failed_count} failed."
            status = "partial_success"
        else:
            message = f"Failed to process any projects. {failed_count}/{total_projects} errors."
            status = "failed"
        response_data = {
            "total_requested": total_projects,
            "processed": processed_count,
            "failed": failed_count,
            "success_rate": f"{(processed_count/total_projects)*100:.1f}%",
            "sample_project": results.get("sample_project", {}),
            "error_details": results.get("details", []),
            "successfully_processed_ids": successfully_processed_ids,
            "scraped_at": datetime.utcnow().isoformat(),
            "source": "EstimateOne Project Search",
        }
        logger.info(f"Project scraping completed. Status: {status}, Processed: {processed_count}, Failed: {failed_count}")
        return EstimateOneResponse(
            status=status,
            message=message,
            data=response_data,
            file_path=None,
        )
    except Exception as exc:
        logger.error(f"Project scraping failed: {exc}")
        logger.debug(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail=f"Project search failed: {type(exc).__name__}. Please try again or contact support."
        )
