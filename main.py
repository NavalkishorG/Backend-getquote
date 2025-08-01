from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from modules.supabase_auth import router as auth_supabase_router

app = FastAPI(
    title="GetQuote Extension Auth API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict as needed!
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_supabase_router, prefix="/api", tags=["Auth-Supabase"])

@app.get("/")
async def health():
    return {"status": "ok", "message": "GetQuote extension backend is running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
