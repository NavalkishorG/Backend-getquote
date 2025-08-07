from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Optional
import os
from dotenv import load_dotenv
from supabase import create_client, Client
from supabase.client import ClientOptions
import asyncio
from cryptography.fernet import Fernet
import logging

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Generate encryption key if not exists
if not ENCRYPTION_KEY:
    key = Fernet.generate_key()
    print(f"Generated encryption key: {key.decode()}")
    print("Add this to your .env file as ENCRYPTION_KEY")
    ENCRYPTION_KEY = key.decode()

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

def encrypt_password(password: str) -> str:
    """Encrypt password using Fernet"""
    cipher_suite = Fernet(ENCRYPTION_KEY.encode())
    encrypted_password = cipher_suite.encrypt(password.encode())
    return encrypted_password.decode()

# SIGNUP endpoint - Only creates auth user, NO credentials storage
@router.post("/signup")
async def signup(user: UserSignup):
    try:
        logger.info(f"Signup attempt for email: {user.email}")
        
        # Only create user in Supabase Auth - NO credential storage
        response = await asyncio.to_thread(
            lambda: supabase.auth.sign_up({
                "email": user.email,
                "password": user.password
            })
        )

        if not response.user:
            logger.error("Signup failed - user already exists or invalid data")
            raise HTTPException(status_code=400, detail="Account with this email already exists")

        logger.info(f"Signup successful for user: {response.user.id}")
        return {
            "success": True,
            "message": "Account created successfully! Please login to store your credentials.",
            "user": {
                "id": response.user.id,
                "email": response.user.email
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Signup error: {e}")
        if "duplicate" in str(e).lower() or "already exists" in str(e).lower():
            raise HTTPException(status_code=400, detail="Account with this email already exists")
        raise HTTPException(status_code=500, detail="Registration service temporarily unavailable")

# LOGIN endpoint - Authenticates AND stores credentials
@router.post("/login")
async def login(user: UserLogin):
    try:
        logger.info(f"Login attempt for email: {user.email}")
        
        # Validate input
        if not user.email or not user.password:
            raise HTTPException(status_code=400, detail="Email and password are required")
        
        # First, authenticate with Supabase Auth
        response = await asyncio.to_thread(
            lambda: supabase.auth.sign_in_with_password({
                "email": user.email,
                "password": user.password
            })
        )

        if not response.user or not response.session:
            logger.error("Login failed - invalid credentials")
            raise HTTPException(status_code=401, detail="Invalid email or password")

        user_id = response.user.id
        logger.info(f"Authentication successful for user: {user_id}")
        
        credentials_stored = False
        
        # After successful login, try to store credentials
        try:
            logger.info(f"Checking existing credentials for user: {user_id}")
            
            # Check if credentials already exist - USE .execute() not .single()
            existing = await asyncio.to_thread(
                lambda: supabase.table("user_credentials")
                .select("id")
                .eq("user_id", user_id)
                .eq("credential_type", "estimate_one")
                .execute()
            )
            
            logger.info(f"Existing credentials check result: {len(existing.data)} rows found")

            # Only store if doesn't exist
            if not existing.data:  # No existing credentials
                logger.info(f"Storing new credentials for user: {user_id}")
                encrypted_password = encrypt_password(user.password)
                
                insert_result = await asyncio.to_thread(
                    lambda: supabase.table("user_credentials").insert({
                        "user_id": user_id,
                        "credential_type": "estimate_one",
                        "email": user.email,
                        "password_encrypted": encrypted_password
                    }).execute()
                )
                
                logger.info(f"Credential insertion result: {insert_result.data}")
                
                if insert_result.data:
                    credentials_stored = True
                    logger.info(f"Credentials stored successfully for user: {user_id}")
                else:
                    logger.error(f"Failed to insert credentials for user: {user_id}")
                    credentials_stored = False
            else:
                credentials_stored = True  # Already exists
                logger.info(f"Credentials already exist for user: {user_id}")

        except Exception as e:
            logger.error(f"Credential storage error for user {user_id}: {e}")
            logger.error(f"Error type: {type(e).__name__}")
            logger.error(f"Error details: {str(e)}")
            # Login still succeeds even if credential storage fails
            credentials_stored = False

        return {
            "authenticated": True,
            "access_token": response.session.access_token,
            "refresh_token": response.session.refresh_token,
            "user": {
                "id": response.user.id,
                "email": response.user.email,
                "user_metadata": response.user.user_metadata or {},
            },
            "credentials_stored": credentials_stored,
            "credential_status": "stored" if credentials_stored else "failed_to_store"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="Login service temporarily unavailable")

# GET CURRENT USER endpoint
@router.get("/me")
async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header missing or invalid format")

    token = authorization.split(" ")[1]

    try:
        user = await asyncio.to_thread(lambda: supabase.auth.get_user(token))
        
        if not user.user:
            raise HTTPException(status_code=401, detail="Authentication token expired or invalid")

        user_id = user.user.id
        
        # Check credential status
        credentials_stored = False
        try:
            # Use .execute() instead of .single() for checking
            result = await asyncio.to_thread(
                lambda: supabase.table("user_credentials")
                .select("id, created_at")
                .eq("user_id", user_id)
                .eq("credential_type", "estimate_one")
                .execute()
            )
            credentials_stored = bool(result.data)
            logger.info(f"Credential check for user {user_id}: {len(result.data)} credentials found")
        except Exception as e:
            logger.warning(f"Error checking credentials for user {user_id}: {e}")
            credentials_stored = False

        return {
            "id": user.user.id,
            "email": user.user.email,
            "credentials_stored": credentials_stored,
            "credential_status": "stored" if credentials_stored else "not_stored"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token verification error: {e}")
        raise HTTPException(status_code=401, detail="Token verification failed")

# LOGOUT endpoint
@router.post("/logout")
async def logout(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization required")

    token = authorization.split(" ")[1]

    try:
        # Sign out from Supabase
        await asyncio.to_thread(lambda: supabase.auth.sign_out())
        
        return {
            "success": True,
            "message": "Logged out successfully"
        }
    except Exception as e:
        logger.error(f"Logout error: {e}")
        # Even if server logout fails, return success so client can clear token
        return {
            "success": True,
            "message": "Logged out successfully"
        }

# GET CREDENTIALS STATUS endpoint
@router.get("/credentials/status")
async def get_credentials_status(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization required")

    token = authorization.split(" ")[1]

    try:
        user = await asyncio.to_thread(lambda: supabase.auth.get_user(token))
        if not user.user:
            raise HTTPException(status_code=401, detail="Invalid token")

        user_id = user.user.id
        
        logger.info(f"Checking credentials status for user: {user_id}")

        # Use .execute() instead of .single() to avoid PGRST116 error
        result = await asyncio.to_thread(
            lambda: supabase.table("user_credentials")
            .select("email, credential_type, created_at")
            .eq("user_id", user_id)
            .eq("credential_type", "estimate_one")
            .execute()
        )
        
        logger.info(f"Credentials status query result: {len(result.data)} rows found")

        if not result.data:
            return {
                "credentials_stored": False,
                "status": "not_stored",
                "message": "EstimateOne credentials not found. Please login again to store them."
            }

        # Get the first (and should be only) record
        credential_data = result.data[0]
        
        return {
            "credentials_stored": True,
            "status": "stored",
            "email": credential_data["email"],
            "credential_type": credential_data["credential_type"],
            "stored_at": credential_data["created_at"],
            "message": "EstimateOne credentials are securely stored."
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking credentials status: {e}")
        return {
            "credentials_stored": False,
            "status": "error",
            "message": "Failed to check credential status"
        }
