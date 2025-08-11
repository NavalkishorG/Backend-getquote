#!/usr/bin/env python3
"""
Standalone test script for EstimateOne project ID search
Usage: python test.py
"""

import asyncio
import logging
import time
import re
from datetime import datetime
from typing import List, Dict, Any
from playwright.sync_api import sync_playwright, Page

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger("EstimateOneProjectSearchTest")

TEST_PROJECT_ID = "168512"
TEST_URL = "https://app.estimateone.com/"
TEST_EMAIL = "rhnkukreja@gmail.com"  # Replace with your EstimateOne email
TEST_PASSWORD = "Estimateone@2025"  # Replace with your EstimateOne password

class EstimateOneProjectSearchScraper:
    def __init__(self, email=None, password=None):
        self.email = email
        self.password = password
        self.login_url = "https://app.estimateone.com/auth/login"
        
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
        """Ultra-fast login method"""
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
        """Search for specific project ID using the search bar with button click"""
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
            logger.info(f"📝 Entered project ID: {project_id}")
            
            # NEW: Press the search button
            search_button_selector = 'button.btn.btn-primary.ml-1.fs-ignore-dead-clicks'
            try:
                page.wait_for_selector(search_button_selector, timeout=2000)
                page.click(search_button_selector)
                logger.info("🔘 Clicked search button")
                
                # Small delay after clicking search button
                time.sleep(1)
                
            except:
                # Fallback: Press Enter key if button not found
                page.keyboard.press("Enter")
                logger.info("⌨️ Pressed Enter key as fallback")
                time.sleep(1)
            
            # Wait for search results (either autocomplete dropdown OR results page)
            try:
                # Option 1: Autocomplete dropdown appears
                page.wait_for_selector('.styles__autocomplete__d2da89763ad53db5dcf7', timeout=3000)
                logger.info("✅ Found autocomplete dropdown")
                
                # Check if we found the project in autocomplete
                suggested_project = page.query_selector('.styles__suggestedProject__f400d5576aec8e4ea183 a')
                if suggested_project:
                    logger.info(f"✅ Found project {project_id} in autocomplete")
                    # Click the first suggested project
                    suggested_project.click()
                    return True
                    
            except:
                # Option 2: Direct search results page
                logger.info("⏳ Checking for search results page...")
                try:
                    # Wait for project rows to appear (search results)
                    page.wait_for_selector("tbody.styles__tenderRow__b2e48989c7e9117bd552", timeout=3000)
                    logger.info("✅ Found search results page")
                    
                    # Look for the specific project ID in results
                    project_rows = page.query_selector_all("tbody.styles__tenderRow__b2e48989c7e9117bd552")
                    for row in project_rows:
                        project_id_elem = row.query_selector(".styles__projectId__a99146050623e131a1bf")
                        if project_id_elem and project_id in project_id_elem.inner_text():
                            logger.info(f"✅ Found project {project_id} in search results")
                            # Click the project link
                            project_link = row.query_selector(".styles__projectLink__bb24735487bba39065d8")
                            if project_link:
                                project_link.click()
                                return True
                                
                except:
                    logger.warning(f"❌ No search results found for project {project_id}")
            
            logger.warning(f"❌ Project {project_id} not found")
            return False
            
        except Exception as e:
            logger.error(f"❌ Error searching for project {project_id}: {e}")
            return False

    def extract_project_details_fast(self, page: Page) -> Dict[str, Any]:
        """Fast extraction from popup"""
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
                logger.warning("❌ No popup details section found")
                return details
            
            # Fast extraction
            all_text = details_section.inner_text()
            logger.info(f"📄 Extracted text preview: {all_text[:200]}...")
            
            # Extract project name from popup
            project_name_elem = details_section.query_selector("h1, h2, h3, .project-title, [class*='title']")
            if project_name_elem:
                details["Project Name"] = project_name_elem.inner_text().strip()
                logger.info(f"📋 Project Name: {details['Project Name']}")
            
            # Extract project address from popup
            address_selectors = [".styles__projectAddress__e13a9deabdbf43356939", "[class*='address']", "[class*='location']"]
            for selector in address_selectors:
                address_elem = details_section.query_selector(selector)
                if address_elem:
                    details["Project Address"] = address_elem.inner_text().strip()
                    logger.info(f"📍 Project Address: {details['Project Address']}")
                    break
            
            # Extract overall budget
            overall_budget_elem = details_section.query_selector(".styles__budgetRange__b101ae22d71fd54397d0")
            if overall_budget_elem:
                details["Overall Budget"] = overall_budget_elem.inner_text().strip()
                logger.info(f"💰 Overall Budget: {details['Overall Budget']}")
            
            # Extract number of trades
            trades_match = re.search(r'(\d+)\s+trades', all_text)
            if trades_match:
                details["Number of Trades"] = trades_match.group(1)
                logger.info(f"🔧 Number of Trades: {details['Number of Trades']}")
            
            # Extract submission deadline
            deadline_match = re.search(r'submitted by\s+(.+?)\.', all_text)
            if deadline_match:
                details["Submission Deadline"] = deadline_match.group(1).strip()
                logger.info(f"⏰ Submission Deadline: {details['Submission Deadline']}")
            
            # Extract builder descriptions
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
                logger.info(f"🏗️ Found {len(builder_descriptions)} builder descriptions")
            
            return details
            
        except Exception as e:
            logger.warning(f"Fast extraction error (continuing): {e}")
            return details

    def close_popup_fast(self, page: Page):
        """Ultra-fast popup closing"""
        try:
            page.keyboard.press("Escape")
            try:
                page.wait_for_selector(".ReactModal__Overlay--after-open", state="hidden", timeout=300)
                logger.info("✅ Popup closed successfully")
                return "success"
            except:
                page.keyboard.press("Escape")
                logger.info("✅ Popup closed (fallback)")
                return "success"
        except Exception as e:
            logger.warning(f"Popup close error (continuing): {e}")
            return "success"

    def save_to_console(self, project_data: Dict[str, Any]) -> bool:
        """Save project data to console (instead of database)"""
        try:
            print("\n" + "="*80)
            print("📊 PROJECT DATA EXTRACTED:")
            print("="*80)
            
            for key, value in project_data.items():
                if value:
                    if key == "Builder Descriptions" and isinstance(value, list):
                        print(f"{key:30}: ")
                        for i, desc in enumerate(value, 1):
                            print(f"  Builder {i}:")
                            for desc_key, desc_value in desc.items():
                                print(f"    {desc_key:20}: {desc_value}")
                    else:
                        print(f"{key:30}: {value}")
            
            print("="*80 + "\n")
            return True
            
        except Exception as e:
            logger.error(f"❌ Console output error: {e}")
            return False

def test_project_search_sync(project_id: str, url: str, email: str, password: str) -> dict:
    """Test project search functionality"""
    
    results = {"processed": 0, "failed": 0, "details": []}
    scraper = EstimateOneProjectSearchScraper(email=email, password=password)
    
    with sync_playwright() as p:
        # Same browser setup
        browser = p.chromium.launch(
            headless=False,  # Set to True for headless mode
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
            
            # Login if needed
            if not scraper.is_logged_in_ultra_fast(page):
                if not scraper.login_to_estimate_one_fast(page):
                    raise RuntimeError("❌ Login failed")
                page.goto(url, wait_until="commit")
            
            logger.info(f"🔄 Processing project ID: {project_id}")
            
            # 1. Search for project ID
            if not scraper.search_project_by_id(page, project_id):
                results["failed"] += 1
                results["details"].append(f"Project {project_id}: Not found in search")
                return results
            
            # 2. Wait for popup to open
            try:
                page.wait_for_selector(".ReactModal__Content, [role='dialog'], #project-details", timeout=5000)
                logger.info("✅ Popup opened successfully")
            except:
                logger.warning(f"⚠️ Popup may not have opened for project {project_id}")
            
            # 3. Extract project data
            project_data = scraper.extract_project_details_fast(page)
            project_data["Project ID"] = project_id
            project_data["source_url"] = url
            project_data["scraped_at"] = datetime.now().isoformat()
            
            # 4. Save to console (instead of database)
            if scraper.save_to_console(project_data):
                results["processed"] += 1
                logger.info(f"✅ Successfully processed project {project_id}")
            else:
                results["failed"] += 1
                results["details"].append(f"Project {project_id}: Console output failed")
            
            # 5. Close popup
            scraper.close_popup_fast(page)
            
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
            # Keep browser open for 5 seconds so you can see the result
            logger.info("🎯 Test completed! Keeping browser open for 5 seconds...")
            time.sleep(5)
            context.close()
            browser.close()
    
    return results

def main():
    """Main test function"""
    print("🚀 EstimateOne Project Search Test")
    print("="*60)
    print(f"Project ID: {TEST_PROJECT_ID}")
    print(f"URL: {TEST_URL}")
    print(f"Email: {TEST_EMAIL}")
    print("="*60)
    
    # Validate credentials
    if TEST_EMAIL == "your-email@example.com" or TEST_PASSWORD == "your-password":
        print("❌ ERROR: Please update TEST_EMAIL and TEST_PASSWORD with your actual EstimateOne credentials")
        print("Edit the variables at the top of test.py file")
        return
    
    try:
        # Run the test
        results = test_project_search_sync(
            project_id=TEST_PROJECT_ID,
            url=TEST_URL,
            email=TEST_EMAIL,
            password=TEST_PASSWORD
        )
        
        # Print results
        print("\n🏁 TEST RESULTS:")
        print("="*40)
        print(f"✅ Processed: {results['processed']}")
        print(f"❌ Failed: {results['failed']}")
        
        if results['details']:
            print(f"📝 Details: {results['details']}")
        
        if results['processed'] > 0:
            print("🎉 TEST PASSED - Project data extracted successfully!")
        else:
            print("💥 TEST FAILED - No projects processed")
            
    except Exception as e:
        print(f"❌ TEST ERROR: {e}")

if __name__ == "__main__":
    main()
