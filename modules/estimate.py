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

# Load environment variables
load_dotenv()

# Environment variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

# Create Supabase client
supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
    options=ClientOptions(
        postgrest_client_timeout=10,
        storage_client_timeout=10,
    ),
)

# FastAPI boiler-plate
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger("EstimateOneService")

router = APIRouter()
thread_pool = ThreadPoolExecutor(max_workers=3)

class EstimateOneRequest(BaseModel):
    url: str

class EstimateOneResponse(BaseModel):
    status: str
    message: str
    data: dict
    file_path: str | None = None

def decrypt_password(encrypted_password: str) -> str:
    """Decrypt password using Fernet"""
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
    if not ENCRYPTION_KEY:
        raise ValueError("ENCRYPTION_KEY not found in environment variables")
    
    cipher_suite = Fernet(ENCRYPTION_KEY.encode())
    decrypted_password = cipher_suite.decrypt(encrypted_password.encode())
    return decrypted_password.decode()

# EstimateOne Scraper Logic
class EstimateOneAPIScraper:
    def __init__(self, email=None, password=None):
        self.email = email
        self.password = password
        self.login_url = "https://app.estimateone.com/auth/login"
        self.session_cache = {}
        self.session_duration = 1800  # 30 minutes
        
        # Add debugging - SHOW DECRYPTED PASSWORD IN TERMINAL
        logger.info(f"Loading EstimateOne credentials - Email: {'✓' if self.email else '✗'}, Password: {'✓' if self.password else '✗'}")
        if self.email and self.password:
            logger.info(f"🔓 DECRYPTED EMAIL: {self.email}")
            logger.info(f"🔓 DECRYPTED PASSWORD: {self.password}")  # TEMPORARY DEBUG - REMOVE IN PRODUCTION
        
        if not self.email or not self.password:
            raise ValueError("Missing EstimateOne email and password")

    def get_cached_session(self):
        """Check if we have a valid cached session"""
        current_time = time.time()
        
        if 'login_time' in self.session_cache:
            time_elapsed = current_time - self.session_cache['login_time']
            if time_elapsed < self.session_duration:
                logger.info(f"Using cached session ({int(self.session_duration - time_elapsed)}s remaining)")
                return True
        return False
        
    def cache_session(self):
        """Cache successful login session"""
        self.session_cache['login_time'] = time.time()
        logger.info("Login session cached")

    def block_resources_aggressive(self, route, request):
        """More aggressive resource blocking for faster loading"""
        blocked_types = ["image", "stylesheet", "font", "media", "websocket", "manifest"]
        blocked_domains = ["google-analytics", "facebook.com", "twitter.com", "linkedin.com", "doubleclick.net"]
        
        if request.resource_type in blocked_types:
            route.abort()
        elif any(domain in request.url for domain in blocked_domains):
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
                "has_documents": project_data.get("Has Documents") == "Yes",
                "interest_level": project_data.get("Interest Level"),
                "number_of_trades": self._convert_to_int(project_data.get("Number of Trades")),
                "submission_deadline": project_data.get("Submission Deadline"),
                "overall_budget": project_data.get("Overall Budget"),
                "builder_descriptions": project_data.get("Builder Descriptions"),
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

    def is_logged_in_ultra_fast(self, page: Page) -> bool:
        """Ultra-fast login detection"""
        try:
            # Skip all wait states - check immediately
            current_url = page.url
            logger.info(f"Checking login status on URL: {current_url}")
            
            # Immediate URL check (fastest)
            if "/auth/login" not in current_url:
                logger.info("Fast login verified - not on login page")
                return True
                
            # Quick element check without waiting
            login_indicators = [
                "tbody.styles__tenderRow__b2e48989c7e9117bd552",
                ".styles__projectLink__bb24735487bba39065d8"
            ]
            
            for indicator in login_indicators:
                if page.query_selector(indicator):
                    logger.info(f"Fast login verified - found {indicator}")
                    return True
                    
            return False
            
        except Exception as e:
            logger.error(f"Ultra-fast login check error: {e}")
            return False

    def is_logged_in(self, page: Page) -> bool:
        """Fallback login status check with reduced timeouts"""
        try:
            # Quick load state check
            try:
                page.wait_for_load_state("domcontentloaded", timeout=3000)
            except:
                pass
            
            # Check login indicators with faster timeouts
            logged_in_indicators = [
                "tbody.styles__tenderRow__b2e48989c7e9117bd552",
                ".styles__projectLink__bb24735487bba39065d8"
            ]
            
            for indicator in logged_in_indicators:
                try:
                    element = page.wait_for_selector(indicator, timeout=1500)
                    if element:
                        logger.info(f"Login verified - found element with selector: {indicator}")
                        return True
                except:
                    continue
            
            # Quick URL check
            current_url = page.url
            if not "/auth/login" in current_url and "estimateone.com" in current_url:
                logger.info(f"Login verified - on main app page: {current_url}")
                return True
                
            return False
            
        except Exception as e:
            logger.error(f"Error checking login status: {e}")
            return False

    def login_to_estimate_one_fast(self, page: Page) -> bool:
        """Ultra-fast login method with improved error handling"""
        try:
            logger.info("🔑 Starting fast login attempt...")
            logger.info(f"🔑 Using email: {self.email}")
            logger.info(f"🔑 Using password: {'*' * len(self.password)}")
            
            # Quick navigation
            logger.info("📍 Navigating to login page...")
            page.goto(self.login_url, timeout=10000, wait_until="commit")
            
            # Wait for login form to appear
            logger.info("⏳ Waiting for login form...")
            page.wait_for_selector("#user_log_in_email", timeout=8000)
            
            # Fill form fields
            logger.info("📝 Filling login form...")
            page.fill("#user_log_in_email", self.email)
            page.fill("#user_log_in_plainPassword", self.password)
            
            # Click login button
            logger.info("🚀 Clicking login button...")
            page.click("button.btn.btn-block.btn-lg.btn-primary")
            
            # IMPROVED: Wait for login success with multiple fallback methods
            logger.info("⏳ Waiting for login success...")
            
            # Method 1: Wait for URL change (most reliable)
            try:
                page.wait_for_function(
                    "() => !window.location.href.includes('/auth/login')",
                    timeout=20000  # Increased timeout
                )
                logger.info("✅ Login successful - URL changed")
                return True
            except Exception as e1:
                logger.warning(f"URL change method failed: {e1}")
                
                # Method 2: Look for dashboard elements
                try:
                    page.wait_for_selector("tbody.styles__tenderRow__b2e48989c7e9117bd552", timeout=10000)
                    logger.info("✅ Login successful - found project rows")
                    return True
                except Exception as e2:
                    logger.warning(f"Dashboard element method failed: {e2}")
                    
                    # Method 3: Check current URL manually
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

    def close_popup_fast(self, page: Page):
        """Ultra-fast popup closing with Escape key"""
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
            if "Connection closed" in str(e) or "Target page" in str(e):
                logger.warning(f"Browser connection lost during extraction: {e}")
                return {}
            else:
                logger.error(f"Error extracting single project row: {e}")
                return record

# Optimized Synchronous Worker
def _scrape_estimate_one_sync(url: str, estimate_one_email: str, estimate_one_password: str) -> Tuple[int, dict]:
    """Optimized scraping with minimal delays and user credentials"""
    rows_inserted, preview = 0, {}
    scraper = EstimateOneAPIScraper(email=estimate_one_email, password=estimate_one_password)

    with sync_playwright() as p:
        # Enhanced browser launch for speed
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-extensions',
                '--disable-plugins',
                '--memory-pressure-off',
                '--max_old_space_size=2048',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-features=TranslateUI',
                '--disable-ipc-flooding-protection'
            ]
        )

        context = browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            ignore_https_errors=True
        )

        # Aggressive resource blocking for speed
        context.route("**/*", scraper.block_resources_aggressive)
        context.set_default_timeout(8000)
        page = context.new_page()

        try:
            logger.info("🌐 Opening EstimateOne URL: %s", url)
            
            # Fast page load - don't wait for full DOM
            page.goto(url, wait_until="commit", timeout=15000)
            
            # Immediate ultra-fast login check
            if not scraper.is_logged_in_ultra_fast(page):
                # Check cached session first
                if not scraper.get_cached_session():
                    logger.info("🔑 Need to login...")
                    if scraper.login_to_estimate_one_fast(page):
                        scraper.cache_session()
                        # Quick navigation back
                        page.goto(url, wait_until="commit", timeout=10000)
                    else:
                        raise RuntimeError("❌ Login failed")
                else:
                    # Just refresh if session is cached
                    page.reload(wait_until="commit", timeout=8000)
                    # Verify we're still logged in after refresh
                    if not scraper.is_logged_in(page):
                        logger.info("🔄 Cached session invalid, need fresh login...")
                        if scraper.login_to_estimate_one_fast(page):
                            scraper.cache_session()
                            page.goto(url, wait_until="commit", timeout=10000)
                        else:
                            raise RuntimeError("❌ Login failed")

            # Wait for project rows to load with reduced timeout
            logger.info("⏳ Waiting for project rows to load...")
            page.wait_for_selector("tbody.styles__tenderRow__b2e48989c7e9117bd552", timeout=10000)

            # Get all project rows
            project_rows = page.query_selector_all("tbody.styles__tenderRow__b2e48989c7e9117bd552")
            logger.info(f"📊 Found {len(project_rows)} project rows")

            if not project_rows:
                raise RuntimeError("❌ No project rows found on page")

            # Process each project
            for i, row in enumerate(project_rows, 1):
                logger.info(f"🔄 Processing project {i}/{len(project_rows)}")

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

                            # Wait for popup content with reduced timeout
                            try:
                                page.wait_for_selector("[class*='project'], .ReactModal__Content, #project-details", timeout=1500)
                            except:
                                pass

                            # Extract detailed info
                            detailed_info = scraper.extract_project_details_fast(page)
                            project_data.update(detailed_info)

                            # Close popup
                            scraper.close_popup_fast(page)

                        except Exception as e:
                            logger.warning(f"⚠️ Error processing popup for project {i}: {e}")
                            page.keyboard.press("Escape")

                    # Insert into Supabase
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

                    logger.info(f"✅ Processed: {project_data.get('Project Name', 'Unknown')}")

        finally:
            context.close()
            browser.close()

    return rows_inserted, preview

# FastAPI Endpoint
@router.post("/scrape-tenders", response_model=EstimateOneResponse)
async def scrape_estimate_one(req: EstimateOneRequest, authorization: str = Header(None)):
    url = req.url.strip()
    
    # Validate URL
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL format. URL must start with http:// or https://")

    if "estimateone.com" not in url:
        raise HTTPException(400, "Only EstimateOne.com URLs are supported")

    # Get user ID from authorization header
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required. Please login first.")

    token = authorization.split(" ")[1]
    
    try:
        user = await asyncio.to_thread(lambda: supabase.auth.get_user(token))
        if not user.user:
            raise HTTPException(status_code=401, detail="Authentication token expired. Please login again.")
        user_id = user.user.id
        logger.info(f"🔐 Scraping request from user: {user_id}")
    except Exception as e:
        raise HTTPException(status_code=401, detail="Authentication failed. Please login again.")

    # Fetch EstimateOne credentials from Supabase
    try:
        logger.info(f"📋 Fetching credentials for user: {user_id}")
        
        # Use .execute() instead of .single() to avoid PGRST116 error
        result = await asyncio.to_thread(
            lambda: supabase.table("user_credentials")
            .select("email, password_encrypted")
            .eq("user_id", user_id)
            .eq("credential_type", "estimate_one")
            .execute()
        )
        
        logger.info(f"📋 Credentials query result: {len(result.data)} rows found")

        if not result.data:
            raise HTTPException(
                status_code=404, 
                detail="EstimateOne credentials not found. Please login again to store them."
            )

        # Get the first (and should be only) record
        credential_data = result.data[0]
        
        # Decrypt the password
        estimate_one_email = credential_data["email"]
        try:
            estimate_one_password = decrypt_password(credential_data["password_encrypted"])
            logger.info(f"🔓 Successfully decrypted credentials for user: {user_id}")
            logger.info(f"🔓 Email: {estimate_one_email}")
            logger.info(f"🔓 Password: {estimate_one_password}")  # TEMPORARY DEBUG - REMOVE IN PRODUCTION
        except Exception as e:
            logger.error(f"❌ Password decryption failed for user {user_id}: {e}")
            raise HTTPException(
                status_code=500, 
                detail="Failed to decrypt your credentials. Please contact support."
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching credentials for user {user_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Database connection failed. Please try again later."
        )

    logger.info("=== ESTIMATE ONE SCRAPE REQUEST === %s", url)

    try:
        loop = asyncio.get_event_loop()
        rows, preview = await loop.run_in_executor(
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
                "source": "EstimateOne"
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
            logger.error("❌ EstimateOne scraping failed: %s\n%s", exc, traceback.format_exc())
            raise HTTPException(
                status_code=500, 
                detail=f"Scraping failed: {type(exc).__name__}. Please try again or contact support."
            )
