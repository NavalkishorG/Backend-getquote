from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Optional
import os
from dotenv import load_dotenv
from supabase import create_client, Client
from supabase.client import ClientOptions
import asyncio

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
    options=ClientOptions(
        postgrest_client_timeout=10,
        storage_client_timeout=10,
    ),
)

router = APIRouter()

class UserLogin(BaseModel):
    email: str
    password: str

class UserSignup(BaseModel):
    email: str
    password: str

class TokenData(BaseModel):
    access_token: str
    token_type: str = "bearer"

# LOGIN endpoint
@router.post("/login")
async def login(user: UserLogin):
    try:
        # Supabase Python client is sync - run in thread for async API
        response = await asyncio.to_thread(
            lambda: supabase.auth.sign_in_with_password({
                "email": user.email,
                "password": user.password
            })
        )
        # response.session is None if login fails
        if not response.user or not response.session:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return {
            "authenticated": True,
            "access_token": response.session.access_token,
            "refresh_token": response.session.refresh_token,
            "user": {
                "id": response.user.id,
                "email": response.user.email,
                "user_metadata": response.user.user_metadata or {},
                # Add other user fields as needed
            }
        }
    except Exception as e:
        print("[LOGIN ERROR]", e)
        raise HTTPException(status_code=500, detail="Login failed")

# SIGNUP endpoint
@router.get("/me")
async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    
    token = authorization.split(" ")[1]
    try:
        # Verify the token and get the user
        user = await asyncio.to_thread(
            lambda: supabase.auth.get_user(token)
        )
        if not user.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        return {
            "id": user.user.id,
            "email": user.user.email,
            # Add other user fields as needed
        }
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

@router.post("/signup")
async def signup(user: UserSignup):
    try:
        response = await asyncio.to_thread(
            lambda: supabase.auth.sign_up({
                "email": user.email,
                "password": user.password
            })
        )
        # response.user is None if signup fails (e.g., duplicate email)
        if not response.user:
            raise HTTPException(status_code=400, detail="Signup failed (user may already exist)")
        return {
            "success": True,
            "user": {
                "id": response.user.id,
                "email": response.user.email
            }
        }
    except Exception as e:
        print("[SIGNUP ERROR]", e)
        raise HTTPException(status_code=500, detail="Signup failed")
