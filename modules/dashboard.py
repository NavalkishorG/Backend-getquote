from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Header, Query
from pydantic import BaseModel
from supabase import create_client, Client
from supabase.client import ClientOptions
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Environment variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Create Supabase client
supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
    options=ClientOptions(
        postgrest_client_timeout=10,
        storage_client_timeout=10,
    ),
)

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger("DashboardService")

# Create router
router = APIRouter()

# Response Models
class DashboardStatsResponse(BaseModel):
    active_projects: int
    total_subcontractors: int
    avg_matches_per_project: int
    quote_response_rate: int
    this_week_projects: int
    total_budget_value: str

class ProjectSummary(BaseModel):
    name: str
    id: str
    matches: int
    budget: str
    category: str
    due_date: str
    location: str
    builder: str
    scraped_at: str
    priority: str

class DashboardProjectsResponse(BaseModel):
    projects: List[ProjectSummary]
    total_count: int

class RecentActivity(BaseModel):
    project_name: str
    project_id: str
    scraped_at: str
    category: str
    action: str

class DashboardActivityResponse(BaseModel):
    activities: List[RecentActivity]

class TrendData(BaseModel):
    date: str
    count: int
    budget: float

class DashboardTrendsResponse(BaseModel):
    project_trends: List[TrendData]
    category_breakdown: Dict[str, int]
    budget_ranges: Dict[str, int]

# Helper Functions
async def authenticate_user(authorization: str) -> str:
    """Authenticate user and return user_id"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization required")
    
    token = authorization.split(" ")[1]
    
    try:
        user = await asyncio.to_thread(lambda: supabase.auth.get_user(token))
        if not user.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user.user.id
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        raise HTTPException(status_code=401, detail="Authentication failed")

def extract_budget_value(budget_str: str) -> float:
    """Extract numeric value from budget string"""
    if not budget_str:
        return 0.0
    
    try:
        # Remove currency symbols and commas
        import re
        numbers = re.findall(r'[\d,]+', str(budget_str))
        if numbers:
            # Take the highest number if there are multiple (for ranges like $50k-$100k)
            values = [float(num.replace(',', '')) for num in numbers]
            return max(values)
    except Exception:
        pass
    return 0.0

def categorize_budget(budget_value: float) -> str:
    """Categorize budget into ranges"""
    if budget_value == 0:
        return "Not Specified"
    elif budget_value < 50000:
        return "Under $50k"
    elif budget_value < 100000:
        return "$50k - $100k"
    elif budget_value < 500000:
        return "$100k - $500k"
    elif budget_value < 1000000:
        return "$500k - $1M"
    else:
        return "Over $1M"

def determine_project_priority(matches: int, budget_value: float, due_date: str) -> str:
    """Determine project priority based on various factors"""
    priority_score = 0
    
    # Matches factor
    if matches > 50:
        priority_score += 3
    elif matches > 20:
        priority_score += 2
    elif matches > 0:
        priority_score += 1
    
    # Budget factor
    if budget_value > 500000:
        priority_score += 3
    elif budget_value > 100000:
        priority_score += 2
    elif budget_value > 50000:
        priority_score += 1
    
    # Due date factor (if we can parse it)
    try:
        if due_date and due_date.lower() != 'tbd':
            # Simple check for urgency keywords
            if any(word in due_date.lower() for word in ['urgent', 'asap', 'immediate']):
                priority_score += 2
    except Exception:
        pass
    
    if priority_score >= 6:
        return "high-priority"
    elif priority_score >= 3:
        return "medium-priority"
    else:
        return "low-priority"

# API Endpoints

@router.get("/stats", response_model=DashboardStatsResponse)
async def get_dashboard_stats(authorization: str = Header(None)):
    """Get comprehensive dashboard statistics from Supabase data"""
    user_id = await authenticate_user(authorization)
    
    try:
        logger.info(f"📊 Fetching dashboard stats for user: {user_id}")
        
        # Get total active projects
        total_projects_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("project_id", count="exact")
            .execute()
        )
        total_projects = len(total_projects_result.data) if total_projects_result.data else 0
        
        # Get projects from this week
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        this_week_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("project_id")
            .gte("scraped_at", week_ago)
            .execute()
        )
        this_week_projects = len(this_week_result.data) if this_week_result.data else 0
        
        # Get unique builders (subcontractors)
        builders_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("builder")
            .not_.is_("builder", "null")
            .neq("builder", "")
            .execute()
        )
        unique_builders = len(set(item["builder"] for item in builders_result.data if item.get("builder")))
        
        # Get average trades per project
        trades_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("number_of_trades")
            .not_.is_("number_of_trades", "null")
            .execute()
        )
        
        if trades_result.data:
            valid_trades = [item["number_of_trades"] for item in trades_result.data if item.get("number_of_trades")]
            avg_trades = sum(valid_trades) / len(valid_trades) if valid_trades else 0
        else:
            avg_trades = 0
        
        # Calculate total budget value
        budget_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("max_budget, overall_budget")
            .execute()
        )
        
        total_budget_value = 0.0
        if budget_result.data:
            for project in budget_result.data:
                budget = project.get("max_budget") or project.get("overall_budget")
                if budget:
                    total_budget_value += extract_budget_value(budget)
        
        # Format total budget
        if total_budget_value > 1000000:
            formatted_budget = f"${total_budget_value/1000000:.1f}M"
        elif total_budget_value > 1000:
            formatted_budget = f"${total_budget_value/1000:.0f}K"
        else:
            formatted_budget = f"${total_budget_value:.0f}"
        
        # Calculate quote response rate (you can customize this logic)
        response_rate = min(85, 60 + (total_projects // 10))  # Dynamic response rate based on project count
        
        stats = DashboardStatsResponse(
            active_projects=total_projects,
            total_subcontractors=unique_builders,
            avg_matches_per_project=round(avg_trades),
            quote_response_rate=response_rate,
            this_week_projects=this_week_projects,
            total_budget_value=formatted_budget
        )
        
        logger.info(f"✅ Dashboard stats retrieved: {total_projects} projects, {unique_builders} builders")
        return stats
        
    except Exception as e:
        logger.error(f"❌ Error fetching dashboard stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch dashboard statistics")

@router.get("/projects", response_model=DashboardProjectsResponse)
async def get_dashboard_projects(
    authorization: str = Header(None),
    limit: int = Query(12, ge=1, le=50),  # Increased default limit to 12
    priority: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None)  # NEW: Filter by project ID
):
    """Get projects for dashboard with filtering options - NEW: Added project_id filter"""
    user_id = await authenticate_user(authorization)
    
    try:
        logger.info(f"📋 Fetching dashboard projects for user: {user_id} (limit: {limit})")
        
        # Build query using the same format as your Supabase query
        query = supabase.table("tenders").select(
            "project_name, project_id, project_address, max_budget, category, "
            "number_of_trades, project_due_date, builder, overall_budget, scraped_at"
        ).not_.is_("project_name", "null")
        
        # NEW: Apply project_id filter if provided
        if project_id:
            logger.info(f"🔍 Filtering by project ID: {project_id}")
            query = query.eq("project_id", project_id)
        
        # Apply other filters
        if category:
            query = query.eq("category", category)
        
        # Execute query with your exact format
        projects_result = await asyncio.to_thread(
            lambda: query.order("scraped_at", desc=True).limit(limit).execute()
        )
        
        projects = []
        for project in projects_result.data:
            matches = project.get("number_of_trades", 0) or 0
            budget = project.get("max_budget") or project.get("overall_budget", "Budget TBD")
            budget_value = extract_budget_value(str(budget)) if budget else 0.0
            due_date = project.get("project_due_date", "TBD")
            
            project_priority = determine_project_priority(matches, budget_value, due_date)
            
            # Apply priority filter
            if priority and project_priority != priority:
                continue
            
            project_summary = ProjectSummary(
                name=(project.get("project_name", "Unknown Project")[:50] + 
                     ("..." if len(project.get("project_name", "")) > 50 else "")),
                id=project.get("project_id", "N/A"),
                matches=matches,
                budget=str(budget),
                category=project.get("category", "General"),
                due_date=due_date,
                location=(project.get("project_address", "Location not specified")[:60] + 
                         ("..." if len(project.get("project_address", "")) > 60 else "")),
                builder=project.get("builder", "TBD"),
                scraped_at=project.get("scraped_at", ""),
                priority=project_priority
            )
            
            projects.append(project_summary)
        
        # Get total count for pagination
        total_count_query = supabase.table("tenders").select("project_id", count="exact").not_.is_("project_name", "null")
        
        # Apply same filters for count
        if project_id:
            total_count_query = total_count_query.eq("project_id", project_id)
        if category:
            total_count_query = total_count_query.eq("category", category)
        
        total_count_result = await asyncio.to_thread(lambda: total_count_query.execute())
        total_count = len(total_count_result.data) if total_count_result.data else 0
        
        logger.info(f"✅ Retrieved {len(projects)} projects from {total_count} total")
        
        # Log if filtering by project_id
        if project_id:
            logger.info(f"🎯 Filtered results for project_id '{project_id}': {len(projects)} matches Testing")
        
        return DashboardProjectsResponse(projects=projects, total_count=total_count)
        
    except Exception as e:
        logger.error(f"❌ Error fetching dashboard projects: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch dashboard projects")

@router.get("/projects/by-id/{project_id}", response_model=DashboardProjectsResponse)
async def get_project_by_id(
    project_id: str,
    authorization: str = Header(None)
):
    """Get specific project by ID - NEW: Dedicated endpoint for project ID lookup"""
    user_id = await authenticate_user(authorization)
    
    try:
        logger.info(f"🔍 Fetching specific project by ID: {project_id} for user: {user_id}")
        
        # Query for specific project ID
        projects_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("project_name, project_id, project_address, max_budget, category, "
                   "number_of_trades, project_due_date, builder, overall_budget, scraped_at")
            .eq("project_id", project_id)
            .not_.is_("project_name", "null")
            .execute()
        )
        
        projects = []
        for project in projects_result.data:
            matches = project.get("number_of_trades", 0) or 0
            budget = project.get("max_budget") or project.get("overall_budget", "Budget TBD")
            budget_value = extract_budget_value(str(budget)) if budget else 0.0
            due_date = project.get("project_due_date", "TBD")
            
            project_priority = determine_project_priority(matches, budget_value, due_date)
            
            project_summary = ProjectSummary(
                name=project.get("project_name", "Unknown Project"),
                id=project.get("project_id", "N/A"),
                matches=matches,
                budget=str(budget),
                category=project.get("category", "General"),
                due_date=due_date,
                location=project.get("project_address", "Location not specified"),
                builder=project.get("builder", "TBD"),
                scraped_at=project.get("scraped_at", ""),
                priority=project_priority
            )
            
            projects.append(project_summary)
        
        total_count = len(projects)
        
        if total_count == 0:
            logger.warning(f"❌ No project found with ID: {project_id}")
            raise HTTPException(status_code=404, detail=f"Project with ID '{project_id}' not found")
        
        logger.info(f"✅ Found project: {projects[0].name} (ID: {project_id})")
        return DashboardProjectsResponse(projects=projects, total_count=total_count)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching project by ID {project_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch project by ID")

@router.get("/recent-activity", response_model=DashboardActivityResponse)
async def get_dashboard_recent_activity(
    authorization: str = Header(None),
    limit: int = Query(10, ge=1, le=50)
):
    """Get recent scraping activity"""
    user_id = await authenticate_user(authorization)
    
    try:
        logger.info(f"📈 Fetching recent activity for user: {user_id}")
        
        # Get recently scraped projects
        recent_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("project_name, project_id, scraped_at, category")
            .not_.is_("project_name", "null")
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        
        activities = []
        for item in recent_result.data:
            activity = RecentActivity(
                project_name=item.get("project_name", "Unknown")[:40] + 
                           ("..." if len(item.get("project_name", "")) > 40 else ""),
                project_id=item.get("project_id", "N/A"),
                scraped_at=item.get("scraped_at", ""),
                category=item.get("category", "General"),
                action="Scraped from EstimateOne"
            )
            activities.append(activity)
        
        logger.info(f"✅ Retrieved {len(activities)} recent activities")
        return DashboardActivityResponse(activities=activities)
        
    except Exception as e:
        logger.error(f"❌ Error fetching recent activity: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch recent activity")

@router.get("/trends", response_model=DashboardTrendsResponse)
async def get_dashboard_trends(authorization: str = Header(None)):
    """Get trending data and analytics"""
    user_id = await authenticate_user(authorization)
    
    try:
        logger.info(f"📊 Fetching dashboard trends for user: {user_id}")
        
        # Get project trends for last 7 days
        trends_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("scraped_at, max_budget, overall_budget")
            .gte("scraped_at", (datetime.now() - timedelta(days=7)).isoformat())
            .order("scraped_at", desc=False)
            .execute()
        )
        
        # Process project trends by day
        daily_counts = {}
        daily_budgets = {}
        
        for project in trends_result.data:
            scraped_date = project.get("scraped_at", "")
            if scraped_date:
                try:
                    date_key = datetime.fromisoformat(scraped_date.replace('Z', '+00:00')).date().isoformat()
                    daily_counts[date_key] = daily_counts.get(date_key, 0) + 1
                    
                    budget = project.get("max_budget") or project.get("overall_budget")
                    budget_value = extract_budget_value(str(budget)) if budget else 0.0
                    daily_budgets[date_key] = daily_budgets.get(date_key, 0) + budget_value
                except Exception:
                    continue
        
        project_trends = [
            TrendData(date=date, count=count, budget=daily_budgets.get(date, 0))
            for date, count in daily_counts.items()
        ]
        
        # Get category breakdown
        categories_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("category")
            .not_.is_("category", "null")
            .execute()
        )
        
        category_breakdown = {}
        for item in categories_result.data:
            category = item.get("category", "Other")
            category_breakdown[category] = category_breakdown.get(category, 0) + 1
        
        # Get budget ranges breakdown
        budgets_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("max_budget, overall_budget")
            .execute()
        )
        
        budget_ranges = {
            "Under $50k": 0,
            "$50k - $100k": 0,
            "$100k - $500k": 0,
            "$500k - $1M": 0,
            "Over $1M": 0,
            "Not Specified": 0
        }
        
        for project in budgets_result.data:
            budget = project.get("max_budget") or project.get("overall_budget")
            budget_value = extract_budget_value(str(budget)) if budget else 0.0
            range_category = categorize_budget(budget_value)
            budget_ranges[range_category] = budget_ranges.get(range_category, 0) + 1
        
        trends = DashboardTrendsResponse(
            project_trends=project_trends,
            category_breakdown=category_breakdown,
            budget_ranges=budget_ranges
        )
        
        logger.info(f"✅ Retrieved trends data with {len(project_trends)} daily data points")
        return trends
        
    except Exception as e:
        logger.error(f"❌ Error fetching dashboard trends: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch dashboard trends")

@router.get("/categories")
async def get_available_categories(authorization: str = Header(None)):
    """Get list of available project categories"""
    user_id = await authenticate_user(authorization)
    
    try:
        categories_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("category")
            .not_.is_("category", "null")
            .neq("category", "")
            .execute()
        )
        
        categories = list(set(item["category"] for item in categories_result.data if item.get("category")))
        categories.sort()
        
        return {"categories": categories}
        
    except Exception as e:
        logger.error(f"❌ Error fetching categories: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch categories")

@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, authorization: str = Header(None)):
    """Delete a specific project"""
    user_id = await authenticate_user(authorization)
    
    try:
        logger.info(f"🗑️ Deleting project {project_id} for user: {user_id}")
        
        result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .delete()
            .eq("project_id", project_id)
            .execute()
        )
        
        if result.data:
            logger.info(f"✅ Successfully deleted project: {project_id}")
            return {"message": f"Project {project_id} deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Project not found")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error deleting project {project_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete project")

@router.get("/export")
async def export_projects(
    authorization: str = Header(None),
    format: str = Query("json", regex="^(json|csv)$")
):
    """Export all projects data"""
    user_id = await authenticate_user(authorization)
    
    try:
        logger.info(f"📤 Exporting projects in {format} format for user: {user_id}")
        
        projects_result = await asyncio.to_thread(
            lambda: supabase.table("tenders")
            .select("*")
            .order("scraped_at", desc=True)
            .execute()
        )
        
        if format == "json":
            return {"projects": projects_result.data, "total_count": len(projects_result.data)}
        elif format == "csv":
            # For CSV, you might want to return a downloadable file
            # This is a simplified version - you could enhance it to return actual CSV
            return {"message": "CSV export not implemented yet", "data": projects_result.data}
            
    except Exception as e:
        logger.error(f"❌ Error exporting projects: {e}")
        raise HTTPException(status_code=500, detail="Failed to export projects")
