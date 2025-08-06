# main.py (no changes needed)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from modules.supabase_auth import router as auth_supabase_router
from modules.estimate import router as estimate_router
import sys
import asyncio

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

@app.get("/")
async def health():
    return {"status": "ok", "message": "GetQuote extension backend is running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
