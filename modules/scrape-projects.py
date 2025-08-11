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

# FastAPI setup
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger("EstimateOneProjectSearch")

router = APIRouter()
thread_pool = ThreadPoolExecutor(max_workers=3)

# Request/Response Models
class ProjectSearchRequest(BaseModel):
    project_ids: List[str]  # Always use list, even for single ID
    url: str = "https://app.estimateone.com/tenders"

class ProjectSearchResponse(BaseModel):
    status: str
    message: str
    data: dict

def decrypt_password(encrypted_password: str) -> str:
    """Decrypt password using Fernet"""
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
    if not ENCRYPTION_KEY:
        raise ValueError("ENCRYPTION_KEY not found in environment variables")
    cipher_suite = Fernet(ENCRYPTION_KEY.encode())
    decrypted_password = cipher_suite.decrypt(encrypted_password.encode())
    return decrypted_password.decode()

# Enhanced EstimateOne Scraper for Project Search
class EstimateOneProjectSearchScraper:
    def __init__(self, email=None, password=None):
        self.email = email
        self.password = password
        self.login_url = "https://app.estimateone.com/auth/login"
        self.session_cache = {}
        self.session_duration = 1800  # 30 minutes

        logger.info(f"Loading EstimateOne credentials - Email: {'✓' if self.email else '✗'}, Password: {'✓' if self.password else '✗'}")
        
        if not self.email or not self.password:
            raise ValueError("Missing EstimateOne email and password")

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

    def is_logged_in_ultra_fast(self, page: Page) -> bool:
        """Ultra-fast login detection"""
        try:
            current_url = page.url
            logger.info(f"Checking login status on URL: {current_url}")
            
            # Immediate URL check (fastest)
            if "/auth/login" not in current_url:
                logger.info("Fast login verified - not on login page")
                return True
            
            # Quick element check without waiting
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
        """Ultra-fast login method (same as estimate.py)"""
        try:
            logger.info("🔑 Starting fast login attempt...")
            logger.info(f"🔑 Using email: {self.email}")
            
            # Quick navigation
            logger.info("📍 Navigating to login page...")
            page.goto(self.login_url, timeout=10000, wait_until="commit")
            
            # Wait for login form
            logger.info("⏳ Waiting for login form...")
            page.wait_for_selector("#user_log_in_email", timeout=8000)
            
            # Fill form fields
            logger.info("📝 Filling login form...")
            page.fill("#user_log_in_email", self.email)
            page.fill("#user_log_in_plainPassword", self.password)
            
            # Click login button
            logger.info("🚀 Clicking login button...")
            page.click("button.btn.btn-block.btn-lg.btn-primary")
            
            # Wait for login success
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
                
                # Fallback: Look for dashboard elements
                try:
                    page.wait_for_selector("tbody.styles__tenderRow__b2e48989c7e9117bd552", timeout=10000)
                    logger.info("✅ Login successful - found project rows")
                    return True
                except Exception as e2:
                    logger.warning(f"Dashboard element method failed: {e2}")
            
            # Manual URL check
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

    def search_project_by_id(self, page: Page, project_id: str) -> bool:
        """Search for specific project ID using the search bar"""
        try:
            logger.info(f"🔍 Searching for project ID: {project_id}")
            
            # Locate search field
            search_input_selector = 'input[placeholder*="Search by project name, project id, address, brand or product"]'
            
            # Wait for and click search input
            page.wait_for_selector(search_input_selector, timeout=5000)
            page.click(search_input_selector)
            
            # Clear and enter project ID
            page.fill(search_input_selector, "")
            page.fill(search_input_selector, project_id)
            
            # Wait for autocomplete dropdown to appear
            logger.info("⏳ Waiting for search results...")
            page.wait_for_selector('.styles__autocomplete__d2da89763ad53db5dcf7', timeout=5000)
            
            # Check if we found the project in autocomplete
            suggested_project = page.query_selector('.styles__suggestedProject__f400d5576aec8e4ea183 a')
            if suggested_project:
                logger.info(f"✅ Found project {project_id} in autocomplete")
                # Click the first suggested project
                suggested_project.click()
                return True
            else:
                logger.warning(f"❌ Project {project_id} not found in autocomplete")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error searching for project {project_id}: {e}")
            return False

    def extract_project_details_fast(self, page: Page) -> Dict[str, Any]:
        """Fast extraction from popup (same as estimate.py)"""
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
            
            # Extract project name from popup
            project_name_elem = details_section.query_selector("h1, h2, h3, .project-title, [class*='title']")
            if project_name_elem:
                details["Project Name"] = project_name_elem.inner_text().strip()
            
            # Extract project address from popup
            address_selectors = [".styles__projectAddress__e13a9deabdbf43356939", "[class*='address']", "[class*='location']"]
            for selector in address_selectors:
                address_elem = details_section.query_selector(selector)
                if address_elem:
                    details["Project Address"] = address_elem.inner_text().strip()
                    break
            
            return details
            
        except Exception as e:
            logger.warning(f"Fast extraction error (continuing): {e}")
            return details

    def close_popup_fast(self, page: Page):
        """Ultra-fast popup closing (same as estimate.py)"""
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
        """Insert project data into Supabase (same as estimate.py)"""
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
                "number_of_trades": int(project_data.get("Number of Trades", 0)) if project_data.get("Number of Trades") else None,
                "submission_deadline": project_data.get("Submission Deadline"),
                "overall_budget": project_data.get("Overall Budget"),
                "builder_descriptions": project_data.get("Builder Descriptions"),
            }
            
            # Remove None values
            supabase_data = {k: v for k, v in supabase_data.items() if v is not None}
            
            # Insert into Supabase
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

# Synchronous Worker Function
def _process_projects_by_ids_sync(
    project_ids: List[str], 
    url: str, 
    estimate_one_email: str, 
    estimate_one_password: str
) -> dict:
    """Process multiple project IDs - similar to _scrape_estimate_one_sync"""
    
    results = {"processed": 0, "failed": 0, "details": [], "sample_project": {}}
    scraper = EstimateOneProjectSearchScraper(email=estimate_one_email, password=estimate_one_password)
    
    with sync_playwright() as p:
        # Same browser setup as estimate.py
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
            # Same login logic as estimate.py
            logger.info("🌐 Opening EstimateOne URL: %s", url)
            page.goto(url, wait_until="commit")
            
            if not scraper.is_logged_in_ultra_fast(page):
                if not scraper.login_to_estimate_one_fast(page):
                    raise RuntimeError("❌ Login failed")
                page.goto(url, wait_until="commit")
            
            # Process each project ID
            for i, project_id in enumerate(project_ids, 1):
                try:
                    logger.info(f"🔄 Processing project {i}/{len(project_ids)}: ID {project_id}")
                    
                    # 1. Search for project ID
                    if not scraper.search_project_by_id(page, project_id):
                        results["failed"] += 1
                        results["details"].append(f"Project {project_id}: Not found in search")
                        continue
                    
                    # 2. Wait for popup to open
                    try:
                        page.wait_for_selector(".ReactModal__Content, [role='dialog'], #project-details", timeout=3000)
                    except:
                        logger.warning(f"⚠️ Popup may not have opened for project {project_id}")
                    
                    # 3. Extract project data
                    project_data = scraper.extract_project_details_fast(page)
                    project_data["Project ID"] = project_id
                    project_data["source_url"] = url
                    project_data["scraped_at"] = datetime.now().isoformat()
                    
                    # 4. Save to database
                    if scraper.insert_to_supabase(project_data):
                        results["processed"] += 1
                        
                        # Store sample project for preview
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
                    
                    # 5. Close popup
                    scraper.close_popup_fast(page)
                    
                    # Small delay between projects
                    time.sleep(0.5)
                    
                except Exception as e:
                    results["failed"] += 1
                    results["details"].append(f"Project {project_id}: {str(e)}")
                    logger.error(f"❌ Error processing project {project_id}: {e}")
                    
                    # Try to close any open popup
                    try:
                        scraper.close_popup_fast(page)
                    except:
                        pass
                        
        finally:
            context.close()
            browser.close()
    
    return results

# FastAPI Endpoint
@router.post("/scrape-projects", response_model=ProjectSearchResponse)
async def scrape_projects_by_ids(
    req: ProjectSearchRequest, 
    authorization: str = Header(None)
):
    """
    Scrape EstimateOne projects by specific project IDs
    
    Expected request:
    {
        "project_ids": ["169451", "145892", "178234"],  # Always array
        "url": "https://app.estimateone.com/tenders"
    }
    """
    
    # Validate request
    if not req.project_ids:
        raise HTTPException(400, "No project IDs provided")
    
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL format")
    
    if "estimateone.com" not in url:
        raise HTTPException(400, "Only EstimateOne.com URLs are supported")
    
    # Same authentication as estimate.py
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
    
    # Fetch EstimateOne credentials (same as estimate.py)
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
        
        # Format response message
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
