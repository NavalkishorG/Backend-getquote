# modules/tender.py
import asyncio
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright  # Changed to sync
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Pydantic models
class TenderRequest(BaseModel):
    url: str

class TenderResponse(BaseModel):
    status: str
    message: str
    data: dict
    file_path: str = None

# Create router
router = APIRouter()

def get_output_filename(url: str) -> str:
    """Generate a unique filename for the JSON output"""
    url_parts = url.rstrip("/").split("/")
    identifier = url_parts[-1] if url_parts[-1] else "tender"
    clean_id = "".join(c for c in identifier if c.isalnum() or c in ('-', '_'))[:30]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"tender_{clean_id}_{timestamp}.json"

def scrape_tender_sync(url: str) -> tuple[dict, str]:
    """
    Synchronous scraping function that works reliably on Windows
    Returns: (extracted_data, file_path)
    """
    logger.info(f"Starting sync scrape for URL: {url}")
    
    # Create data directory if it doesn't exist
    data_dir = Path("scraped_data")
    data_dir.mkdir(exist_ok=True)
    
    # Generate output filename
    filename = get_output_filename(url)
    file_path = data_dir / filename
    
    with sync_playwright() as p:
        browser = None
        context = None
        page = None
        
        try:
            # Launch browser
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-web-security',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding'
                ]
            )
            
            # Create context with realistic user agent
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1920, 'height': 1080}
            )
            
            page = context.new_page()
            
            # Navigate to URL
            logger.info(f"Navigating to: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            # Wait for content to load
            logger.info("Waiting for content to load...")
            try:
                page.wait_for_selector('.box.boxW.listInner .list-desc', timeout=15000)
                logger.info("Content loaded successfully")
            except Exception as e:
                logger.warning(f"Selector wait failed: {e}")
                # Continue anyway, might still be able to extract data
                page.wait_for_timeout(3000)
            
            # Extract data
            logger.info("Extracting data...")
            data = extract_tender_fields_sync(page)
            
            if not data:
                # Try alternative extraction methods
                logger.warning("No data found with primary method, trying alternatives...")
                data = extract_alternative_fields_sync(page)
            
            if not data:
                raise ValueError("No data could be extracted from the page")
            
            # Add metadata
            data["_metadata"] = {
                "url": url,
                "scraped_at": datetime.now().isoformat(),
                "total_fields": len([k for k in data.keys() if not k.startswith("_")])
            }
            
            # Save to JSON file
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Scrape successful! Data saved to: {file_path}")
            logger.info(f"Extracted {len(data) - 1} fields")  # -1 for metadata
            
            return data, str(file_path.absolute())
            
        except Exception as e:
            logger.error(f"Error during scraping: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
            
        finally:
            # Clean up resources
            try:
                if page:
                    page.close()
                if context:
                    context.close()
                if browser:
                    browser.close()
                logger.info("Browser resources cleaned up")
            except Exception as cleanup_error:
                logger.warning(f"Error during cleanup: {cleanup_error}")

def extract_tender_fields_sync(page) -> dict:
    """Extract tender fields from the page using sync selectors"""
    output = {}
    
    try:
        # Wait a bit more for dynamic content
        page.wait_for_timeout(2000)
        
        # Get all list description elements
        list_descs = page.query_selector_all('.box.boxW.listInner .list-desc')
        logger.info(f"Found {len(list_descs)} description elements")
        
        for i, desc in enumerate(list_descs):
            try:
                # Find label and value within each description
                label_element = desc.query_selector('label')
                value_element = desc.query_selector('.list-desc-inner')
                
                if label_element and value_element:
                    # Extract text content
                    label_text = label_element.inner_text()
                    value_text = value_element.inner_text()
                    
                    # Clean up the text
                    key = label_text.strip().replace(":", "").replace("\n", " ")
                    value = value_text.replace('\n', ' ').strip()
                    
                    if key and value:
                        output[key] = value
                        logger.debug(f"Extracted field: {key} = {value[:100]}...")
                        
            except Exception as field_error:
                logger.warning(f"Error extracting field {i}: {field_error}")
                continue
                
    except Exception as e:
        logger.error(f"Error in field extraction: {e}")
        raise
    
    return output

def extract_alternative_fields_sync(page) -> dict:
    """Try alternative extraction methods if primary method fails"""
    output = {}
    
    try:
        # Try different selectors
        alternative_selectors = [
            '.tender-details .field',
            '.content .row',
            '.info-section .detail',
            'tr td',  # table rows
            '.field-group .field'
        ]
        
        for selector in alternative_selectors:
            elements = page.query_selector_all(selector)
            if elements:
                logger.info(f"Trying alternative selector: {selector}, found {len(elements)} elements")
                
                for element in elements:
                    try:
                        text = element.inner_text()
                        if ':' in text and len(text.strip()) > 0:
                            parts = text.split(':', 1)
                            if len(parts) == 2:
                                key = parts[0].strip()
                                value = parts[1].strip()
                                if key and value and len(key) < 200 and len(value) < 1000:
                                    output[key] = value
                    except Exception:
                        continue
                
                if output:
                    logger.info(f"Successfully extracted {len(output)} fields with alternative method")
                    break
        
        # If still no data, try to get general page information
        if not output:
            try:
                title = page.title()
                if title:
                    output['Page Title'] = title
                
                # Try to get any meaningful text content
                content_element = page.query_selector('main, .content, .main-content, body')
                if content_element:
                    content_text = content_element.inner_text()
                    if content_text and len(content_text.strip()) > 50:
                        # Take first 500 characters as preview
                        output['Content Preview'] = content_text.strip()[:500] + "..."
                        
            except Exception as e:
                logger.warning(f"Error getting fallback content: {e}")
                
    except Exception as e:
        logger.error(f"Error in alternative extraction: {e}")
    
    return output

# Thread pool executor for running sync playwright in threads
thread_pool = ThreadPoolExecutor(max_workers=3)

# API Routes
@router.post("/scrape-tenders", response_model=TenderResponse)
async def scrape_tender(tender_request: TenderRequest):
    """
    Scrape tender data from the provided URL and save to JSON file
    Uses sync Playwright with threading to avoid Windows asyncio issues
    """
    url = tender_request.url
    
    # Validate URL
    if not url or not url.startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail="Invalid URL provided")
    
    logger.info(f"=== SCRAPING REQUEST STARTED ===")
    logger.info(f"URL: {url}")
    
    try:
        # Run sync scraping in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        data, file_path = await loop.run_in_executor(
            thread_pool, 
            scrape_tender_sync, 
            url
        )
        
        # Remove metadata from response data (keep it in file)
        response_data = {k: v for k, v in data.items() if not k.startswith("_")}
        
        if not response_data:
            logger.warning("No data extracted from the page")
            return TenderResponse(
                status="warning",
                message="No data could be extracted from the page",
                data={},
                file_path=file_path
            )
        
        print(f"=== SCRAPE SUCCESSFUL ===")
        print(f"Data saved to: {file_path}")
        logger.info(f"Extracted {len(response_data)} fields")
        
        return TenderResponse(
            status="success",
            message=f"Scrape successful! Data saved to {file_path}",
            data=response_data,
            file_path=file_path
        )
        
    except Exception as e:
        # Log detailed error information
        error_type = type(e).__name__
        error_message = str(e)
        
        logger.error(f"=== SCRAPING FAILED ===")
        logger.error(f"Error type: {error_type}")
        logger.error(f"Error message: {error_message}")
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
        logger.error(f"URL: {url}")
        
        raise HTTPException(
            status_code=500,
            detail=f"Scraping failed: {error_type}: {error_message}"
        )

@router.get("/health")
async def health_check():
    """Check if the scraping service is working"""
    try:
        # Test basic playwright functionality in thread
        def test_playwright():
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
                return True
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(thread_pool, test_playwright)
        
        return {
            "status": "healthy",
            "playwright": "working",
            "platform": sys.platform,
            "data_directory": str(Path("scraped_data").absolute())
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Service unhealthy: {str(e)}"
        )

@router.get("/files")
async def list_scraped_files():
    """List all scraped JSON files"""
    try:
        data_dir = Path("scraped_data")
        if not data_dir.exists():
            return {"files": [], "count": 0}
        
        json_files = []
        for file_path in data_dir.glob("*.json"):
            stat = file_path.stat()
            json_files.append({
                "filename": file_path.name,
                "path": str(file_path.absolute()),
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat()
            })
        
        # Sort by creation time (newest first)
        json_files.sort(key=lambda x: x["created_at"], reverse=True)
        
        return {
            "files": json_files,
            "count": len(json_files),
            "directory": str(data_dir.absolute())
        }
        
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Function to setup routes
def setup_tender_routes(app):
    """Setup tender routes with proper prefix"""
    app.include_router(router, prefix="/tender", tags=["tender"])
