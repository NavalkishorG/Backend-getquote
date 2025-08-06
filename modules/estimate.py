"""

FastAPI endpoint /scrape-tenders for browser extension integration

Behaviour:

1. A URL is posted from the browser extension (EstimateOne main page)

2. Scrapes all projects from the current page with popup details

3. Writes each project as ONE row to Supabase `tenders` table

4. Returns summary response to extension

"""

from __future__ import annotations

import asyncio

import logging

import traceback

import re

import os

from concurrent.futures import ThreadPoolExecutor

from datetime import datetime

from typing import List, Dict, Any, Tuple

from fastapi import APIRouter, HTTPException

from pydantic import BaseModel

from playwright.sync_api import sync_playwright, Page

from dotenv import load_dotenv

from modules.supabase_auth import supabase # re-use your global client

# Load environment variables

load_dotenv()

# ───────────────────────────── FastAPI boiler-plate ───────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")

logger = logging.getLogger("EstimateOneService")

router = APIRouter()

thread_pool = ThreadPoolExecutor(max_workers=3)

class EstimateOneRequest(BaseModel):
    url: str

class EstimateOneResponse(BaseModel):
    status: str
    message: str
    data: dict # returns summary of scraped projects
    file_path: str | None = None # kept for backward compatibility (always None)

# ───────────────────────── EstimateOne Scraper Logic ─────────────────────────

class EstimateOneAPIScraper:
    def __init__(self):
        self.email = os.getenv("ESTIMATE_ONE_EMAIL")
        self.password = os.getenv("ESTIMATE_ONE_PASSWORD")
        self.login_url = "https://app.estimateone.com/auth/login"
        self.session_file = 'estimate_one_session.json'
        
        # Add debugging
        logger.info(f"Loading EstimateOne credentials - Email: {'✓' if self.email else '✗'}, Password: {'✓' if self.password else '✗'}")
        
        if not self.email or not self.password:
            raise ValueError("Missing ESTIMATE_ONE_EMAIL and ESTIMATE_ONE_PASSWORD in .env")

    def block_resources(self, route, request):
        """Block unnecessary resources for faster loading"""
        if request.resource_type in ["image", "stylesheet", "font", "media"]:
            route.abort()
        else:
            route.continue_()

    def _convert_to_int(self, value):
        """Helper method to safely convert string to integer"""
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except (ValueError, AttributeError):
            return None

    def insert_to_supabase(self, project_data: Dict[str, Any]) -> bool:
        """Insert project data into Supabase tenders table"""
        try:
            # Map data to your actual table columns
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
                "has_documents": project_data.get("Has Documents") == "Yes",  # Convert to boolean
                "interest_level": project_data.get("Interest Level"),
                "number_of_trades": self._convert_to_int(project_data.get("Number of Trades")),
                "submission_deadline": project_data.get("Submission Deadline"),
                "overall_budget": project_data.get("Overall Budget"),
                "builder_descriptions": project_data.get("Builder Descriptions"),  # Already JSONB format
                "row_number": project_data.get("Row Number")
            }

            # Remove None values to avoid issues
            supabase_data = {k: v for k, v in supabase_data.items() if v is not None}

            # Insert into Supabase
            result = supabase.table("tenders").insert(supabase_data).execute()
            if result.data:
                logger.info(f"✅ Inserted EstimateOne project '{project_data.get('Project Name', 'Unknown')}' to Supabase")
                return True
            else:
                logger.error(f"❌ Failed to insert project to Supabase: {result}")
                return False

        except Exception as e:
            logger.error(f"❌ Supabase insertion error for project '{project_data.get('Project Name', 'Unknown')}': {e}")
            return False

    def is_logged_in(self, page: Page) -> bool:
        """Enhanced login status check with longer timeout and fallbacks"""
        try:
            # Wait longer for page to fully load after login
            try:
                page.wait_for_load_state("networkidle", timeout=15000)  # Increased from 5000ms
            except:
                # If networkidle fails, try domcontentloaded
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except:
                    logger.warning("Page load state timeout - proceeding with login check")
                    pass  # Continue anyway
            
            # Check multiple login indicators with individual timeouts
            logged_in_indicators = [
                "tbody.styles__tenderRow__b2e48989c7e9117bd552",
                "a[href*='logout']",
                ".user-menu",
                "[data-testid='user-menu']",
                ".styles__userName",
                ".styles__projectLink__bb24735487bba39065d8",  # Project links indicate logged in
                "table[class*='styles__table']"  # Any project table
            ]
            
            for indicator in logged_in_indicators:
                try:
                    # Use wait_for_selector with timeout for each indicator
                    element = page.wait_for_selector(indicator, timeout=3000)
                    if element:
                        logger.info(f"Login verified - found element with selector: {indicator}")
                        return True
                except:
                    continue  # Try next indicator
            
            # Check URL patterns that indicate successful login
            current_url = page.url
            logger.info(f"Current URL during login check: {current_url}")
            
            # More comprehensive URL pattern checks
            logged_in_patterns = ["dashboard", "projects", "tenders", "/app", "/main"]
            login_patterns = ["/auth/login", "/login", "/signin"]
            
            # If we're no longer on a login page, likely logged in
            if not any(pattern in current_url.lower() for pattern in login_patterns):
                if any(pattern in current_url.lower() for pattern in logged_in_patterns):
                    logger.info(f"Login verified by URL pattern: {current_url}")
                    return True
                # If we're on the main EstimateOne app page, we're likely logged in
                elif current_url == "https://app.estimateone.com/" or current_url == "https://app.estimateone.com":
                    logger.info(f"Login verified - on main app page: {current_url}")
                    return True
            
            logger.warning(f"Login verification failed - current URL: {current_url}")
            return False
            
        except Exception as e:
            logger.error(f"Error checking login status: {e}")
            return False

    def login_to_estimate_one(self, page: Page) -> bool:
        """Enhanced login with better error reporting and timing"""
        try:
            logger.info("Attempting to login to EstimateOne...")
            
            # Navigate to login page
            page.goto(self.login_url, timeout=15000, wait_until="domcontentloaded")
            logger.info(f"Navigated to login page: {page.url}")
            
            # Wait for page to be fully loaded with longer timeout
            try:
                page.wait_for_load_state("networkidle", timeout=15000)  # Increased timeout
            except:
                logger.warning("Network idle timeout - continuing with login attempt")
            
            # Check if login form is present
            email_field = page.wait_for_selector("#user_log_in_email", timeout=10000)
            if not email_field:
                logger.error("Login form not found - email field missing")
                return False
                
            # Fill and submit login form
            logger.info("Filling login credentials...")
            email_field.click()
            email_field.fill(self.email)
            
            password_field = page.wait_for_selector("#user_log_in_plainPassword", timeout=5000)
            password_field.click()
            password_field.fill(self.password)
            
            # Click login button
            login_button = page.wait_for_selector("button.btn.btn-block.btn-lg.btn-primary", timeout=5000)
            logger.info("Clicking login button...")
            login_button.click()
            
            # Wait longer for navigation after login
            logger.info("Waiting for login to complete...")
            try:
                # Wait for either successful navigation OR error message with longer timeout
                page.wait_for_function(
                    """() => {
                        return !window.location.href.includes('/auth/login') || 
                               document.querySelector('.alert, .error, [class*="error"]');
                    }""",
                    timeout=20000  # Increased from 15000ms
                )
            except:
                logger.warning("Navigation timeout - checking login status anyway")
            
            # Add a small delay to ensure page is settled
            page.wait_for_timeout(2000)
            
            # Check for login errors on the page
            error_elements = page.query_selector_all('.alert-danger, .error, [class*="error"]')
            if error_elements:
                for error_elem in error_elements:
                    error_text = error_elem.inner_text()
                    logger.error(f"Login error displayed: {error_text}")
            
            # Verify login success
            login_success = self.is_logged_in(page)
            logger.info(f"Login attempt result: {'SUCCESS' if login_success else 'FAILED'}")
            
            return login_success
            
        except Exception as e:
            logger.error(f"Login failed with exception: {e}")
            return False

    # ... (rest of your existing methods remain unchanged)
    def close_popup_fast(self, page: Page):
        """Ultra-fast popup closing with Escape key"""
        try:
            page.keyboard.press("Escape")
            try:
                page.wait_for_selector(".ReactModal__Overlay--after-open", state="hidden", timeout=500)
                return "success"
            except:
                page.keyboard.press("Escape")
                return "success"
        except Exception as e:
            logger.warning(f"Popup close error (continuing): {e}")
            return "success"

    def extract_project_details_fast(self, page: Page) -> Dict[str, Any]:
        """Fast extraction from popup with minimal waits"""
        details = {}
        try:
            # Quick check for details section
            detail_selectors = [
                "#project-details",
                ".styles__projectSection__f1b9aeb71ec0b48e56e0",
                ".ReactModal__Content"
            ]

            details_section = None
            for selector in detail_selectors:
                try:
                    page.wait_for_selector(selector, timeout=1000)
                    details_section = page.query_selector(selector)
                    if details_section:
                        break
                except:
                    continue

            if not details_section:
                details_section = page.query_selector(".ReactModal__Content, [role='dialog']")

            if not details_section:
                return details

            # Fast extraction
            all_text = details_section.inner_text()

            # Extract number of trades
            trades_match = re.search(r'(\d+)\s+trades', all_text)
            if trades_match:
                details["Number of Trades"] = trades_match.group(1)

            # Extract submission deadline
            deadline_match = re.search(r'submitted by\s+(.+?)\.', all_text)
            if deadline_match:
                details["Submission Deadline"] = deadline_match.group(1).strip()

            # Extract overall budget
            overall_budget_elem = details_section.query_selector(".styles__budgetRange__b101ae22d71fd54397d0")
            if overall_budget_elem:
                details["Overall Budget"] = overall_budget_elem.inner_text().strip()

            # Fast builder descriptions extraction
            builder_descriptions = []
            description_items = details_section.query_selector_all(".styles__stageDescription__a6f572d1edbede52b379")

            for item in description_items:
                description_data = {}

                # Extract builder name
                builder_name_elem = item.query_selector("strong")
                if builder_name_elem:
                    builder_text = builder_name_elem.inner_text()
                    builder_name = builder_text.replace(" says:", "").strip()
                    description_data["builder_name"] = builder_name

                # Extract descriptions
                desc_elems = item.query_selector_all(".styles__description__e5a48f83ebd7efa5e045")
                full_description = ""
                builder_budget = ""

                for desc_elem in desc_elems:
                    budget_elem = desc_elem.query_selector(".styles__budgetRange__b101ae22d71fd54397d0")
                    if budget_elem:
                        builder_budget = budget_elem.inner_text().strip()
                    else:
                        text_content = desc_elem.inner_text().strip()
                        if text_content and "approximate budget" not in text_content.lower():
                            full_description += text_content + " "

                if full_description:
                    description_data["description"] = full_description.strip()
                if builder_budget:
                    description_data["builder_budget"] = builder_budget

                if description_data:
                    builder_descriptions.append(description_data)

            if builder_descriptions:
                details["Builder Descriptions"] = builder_descriptions

            return details

        except Exception as e:
            logger.warning(f"Fast extraction error (continuing): {e}")
            return details

    def extract_single_project_row(self, row_element) -> Dict[str, Any]:
        """Extract data from a single project row"""
        record = {}
        try:
            # Extract Project Name
            project_name_elem = row_element.query_selector(".styles__projectLink__bb24735487bba39065d8")
            if project_name_elem:
                record["Project Name"] = project_name_elem.inner_text().strip()

            # Extract Project ID
            project_id_elem = row_element.query_selector(".styles__projectId__a99146050623e131a1bf")
            if project_id_elem:
                record["Project ID"] = project_id_elem.inner_text().strip()

            # Extract Project Address
            address_elem = row_element.query_selector(".styles__projectAddress__e13a9deabdbf43356939")
            if address_elem:
                record["Project Address"] = address_elem.inner_text().strip()

            # Extract Budget Range
            budget_elem = row_element.query_selector(".styles__budgetRange__b101ae22d71fd54397d0")
            if budget_elem:
                record["Max Budget"] = budget_elem.inner_text().strip()

            # Extract Distance
            distance_cells = row_element.query_selector_all("td")
            for cell in distance_cells:
                text = cell.inner_text().strip()
                if "km" in text and text.endswith("km"):
                    record["Distance"] = text
                    break

            # Extract Category/Sector
            category_elem = row_element.query_selector(".styles__lowPriority__ca01365a4bba34b27c8a span")
            if category_elem:
                record["Category"] = category_elem.inner_text().strip()

            # Extract Builder Information
            builder_elem = row_element.query_selector(".styles__builderName__f71d1b6dc7d0969616ea")
            if builder_elem:
                record["Builder"] = builder_elem.inner_text().strip()

            # Extract Quote Due Date
            quote_date_elem = row_element.query_selector(".styles__quoteDate__b21c670d4b980f23ba7c .styles__projectDate__efdf1ddef6a4526d58ac")
            if quote_date_elem:
                record["Quote Due (Builder)"] = quote_date_elem.inner_text().strip()

            # Extract Project Due Date
            project_due_elems = row_element.query_selector_all(".styles__projectDate__efdf1ddef6a4526d58ac")
            if len(project_due_elems) > 1:
                record["Project Due Date"] = project_due_elems[-1].inner_text().strip()
            elif len(project_due_elems) == 1:
                record["Project Due Date"] = project_due_elems[0].inner_text().strip()

            # Check for "No Docs" tag
            no_docs_elem = row_element.query_selector(".styles__noDocsTag__d3dc744a652a94be3eea")
            record["Has Documents"] = "No" if no_docs_elem else "Yes"

            # Extract Interest Level
            interest_elem = row_element.query_selector(".reactSelect__single-value")
            if interest_elem:
                record["Interest Level"] = interest_elem.inner_text().strip()

            return record

        except Exception as e:
            # Check if it's a connection error
            if "Connection closed" in str(e) or "Target page" in str(e):
                logger.warning(f"Browser connection lost during extraction: {e}")
                return {}  # Return empty record but continue processing
            else:
                logger.error(f"Error extracting single project row: {e}")
                return record

# ───────────────────────── Synchronous Worker ─────────────────────────

def _scrape_estimate_one_sync(url: str) -> Tuple[int, dict]:
    """Return (#rows_inserted, preview_record)."""
    rows_inserted, preview = 0, {}
    scraper = EstimateOneAPIScraper()

    with sync_playwright() as p:
        # Optimized browser launch - headless for API usage
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-extensions',
                '--disable-images',
                '--memory-pressure-off',
                '--max_old_space_size=4096',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows'
            ]
        )

        context = browser.new_context(
            viewport={'width': 1366, 'height': 768},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )

        # Block unnecessary resources for speed
        context.route("**/*", scraper.block_resources)
        context.set_default_timeout(15000)
        page = context.new_page()

        try:
            logger.info("Opening EstimateOne URL: %s", url)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Check if login is needed
            if not scraper.is_logged_in(page):
                logger.info("Need to login...")
                if not scraper.login_to_estimate_one(page):
                    raise RuntimeError("Login failed")

                # Navigate back to target page after login
                page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Wait for project rows to load
            page.wait_for_selector("tbody.styles__tenderRow__b2e48989c7e9117bd552", timeout=10000)

            # Get all project rows
            project_rows = page.query_selector_all("tbody.styles__tenderRow__b2e48989c7e9117bd552")
            logger.info(f"Found {len(project_rows)} project rows")

            if not project_rows:
                raise RuntimeError("No project rows found on page")

            # Process each project
            for i, row in enumerate(project_rows, 1):
                logger.info(f"Processing project {i}/{len(project_rows)}")

                # Extract basic row data
                project_data = scraper.extract_single_project_row(row)
                if project_data:
                    project_data["Row Number"] = i
                    project_data["source_url"] = url
                    project_data["scraped_at"] = datetime.now().isoformat()

                    # Click project link for popup details
                    project_link = row.query_selector(".styles__projectLink__bb24735487bba39065d8")
                    if project_link:
                        try:
                            # Click to open popup
                            project_link.click(force=True)

                            # Wait for popup content
                            try:
                                page.wait_for_selector("[class*='project'], .ReactModal__Content, #project-details", timeout=2000)
                            except:
                                pass

                            # Extract detailed info
                            detailed_info = scraper.extract_project_details_fast(page)
                            project_data.update(detailed_info)

                            # Close popup
                            scraper.close_popup_fast(page)

                        except Exception as e:
                            logger.warning(f"Error processing popup for project {i}: {e}")
                            page.keyboard.press("Escape") # Force close

                    # Insert into Supabase
                    if scraper.insert_to_supabase(project_data):
                        rows_inserted += 1
                        if not preview: # Use first successful project as preview
                            preview = {
                                "project_name": project_data.get("Project Name"),
                                "project_id": project_data.get("Project ID"),
                                "category": project_data.get("Category"),
                                "max_budget": project_data.get("Max Budget"),
                                "number_of_trades": project_data.get("Number of Trades")
                            }

                    logger.info(f"✅ Processed: {project_data.get('Project Name', 'Unknown')}")

        finally:
            context.close()
            browser.close()

    return rows_inserted, preview

# ───────────────────────── FastAPI Endpoint ─────────────────────────

@router.post("/scrape-tenders", response_model=EstimateOneResponse)
async def scrape_estimate_one(req: EstimateOneRequest):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL")

    # Ensure it's an EstimateOne URL
    if "estimateone.com" not in url:
        raise HTTPException(400, "URL must be from estimateone.com")

    logger.info("=== ESTIMATE ONE SCRAPE REQUEST === %s", url)

    try:
        loop = asyncio.get_event_loop()
        rows, preview = await loop.run_in_executor(thread_pool, _scrape_estimate_one_sync, url)

        return EstimateOneResponse(
            status="success",
            message=f"{rows} EstimateOne project(s) saved to Supabase.",
            data={
                "projects_scraped": rows,
                "sample_project": preview,
                "scraped_at": datetime.utcnow().isoformat(),
                "source": "EstimateOne"
            },
            file_path=None,
        )

    except Exception as exc:
        logger.error("EstimateOne scraping failed: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(500, f"EstimateOne scraping failed: {type(exc).__name__}: {exc}")
