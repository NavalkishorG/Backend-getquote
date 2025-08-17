from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from modules.supabase_auth import router as auth_supabase_router
from modules.estimate import router as estimate_router
from modules.dashboard import router as dashboard_router
import sys
import asyncio
import os

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

app = FastAPI(
    title="GetQuote Extension Auth API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Setup routes
app.include_router(auth_supabase_router, prefix="/supabase", tags=["Auth-Supabase"])
app.include_router(estimate_router, prefix="/scrapper", tags=["scrapper"])
app.include_router(dashboard_router, prefix="/dashboard", tags=["Dashboard"])

@app.get("/")
async def health():
    return {"status": "ok", "message": "GetQuote extension backend is running"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "getquote-backend"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run("main:app", host=host, port=port, reload=False)
