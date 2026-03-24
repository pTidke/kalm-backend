#!/usr/bin/env python3
"""
api.py — Kalm FastAPI Backend
Deployed on Render free tier (spins down after 15min idle).
Frontend handles the cold-start wake-up automatically.
"""

import os
import uuid
import time
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, validator
from dotenv import load_dotenv
from starlette.responses import Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import chromadb
from chromadb.config import Settings
from openai import AzureOpenAI
import jwt
from jwt import PyJWKClient

from config import (
    PERSONAS, DEFAULT_PERSONA, ALGEE_STAGES, CORE_RULES,
    CRISIS_SIGNALS, HOPELESSNESS_SIGNALS, CRISIS_RESOURCES,
    SAFETY_ADDENDUM_CRISIS, SAFETY_ADDENDUM_HOPELESSNESS,
)

load_dotenv()

# ─── App ─────────────────────────────────────────────────────

app = FastAPI(title="Kalm API", version="1.0.0")
START_TIME = time.time()

# ─── Rate Limiting ────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return Response(
        content='{"detail":"Too many requests — please slow down."}',
        status_code=429,
        media_type="application/json",
    )

# ─── CORS ─────────────────────────────────────────────────
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS", "https://kalm-omega.vercel.app"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ─── Security Headers ────────────────────────────────────
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# ─── Auth (Supabase JWKS) ───────────────────────────────────

SUPABASE_URL = os.getenv(
    "SUPABASE_URL", "https://uqsetzkddvjjrguqoemn.supabase.co"
)
jwks_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")
security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Verify Supabase JWT and return user payload."""
    if not credentials:
        raise HTTPException(401, "Authentication required")
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(
            credentials.credentials
        )
        payload = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=["ES256"],
            audience="authenticated",
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired — please sign in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")


# ─── Azure Clients ──────────────────────────────────────────

AZURE_ENDPOINT       = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY        = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION    = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
CHAT_DEPLOYMENT      = os.getenv("AZURE_CHAT_DEPLOYMENT", "gpt-4.1")
EMBEDDING_DEPLOYMENT = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")
CHROMA_DB_PATH       = os.getenv("CHROMA_DB_PATH", "./kalm_db")
TOP_K                = int(os.getenv("TOP_K_RESULTS", "5"))
COLLECTION_NAME      = "dsm_knowledge"
MAX_HISTORY_TURNS    = 8
MAX_RESPONSE_TOKENS  = 250

az_client = AzureOpenAI(
    azure_endpoint=AZURE_ENDPOINT,
    api_key=AZURE_API_KEY,
    api_version=AZURE_API_VERSION,
)

embed_client = AzureOpenAI(
    azure_endpoint=AZURE_ENDPOINT,
    api_key=AZURE_API_KEY,
    api_version="2023-05-15",
)

try:
    chroma_client = chromadb.PersistentClient(
        path=CHROMA_DB_PATH,
        settings=Settings(anonymized_telemetry=False),
    )
    collection = chroma_client.get_collection(COLLECTION_NAME)
    print(f"✓ ChromaDB loaded — {collection.count():,} chunks")
except Exception as e:
    print(f"✗ ChromaDB failed: {e}")
    collection = None

# ─── Sessions ───────────────────────────────────────────────

sessions: dict = {}


def new_session(persona_id: str, user_id: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "persona_id": persona_id,
        "history": [],
        "algee_stage": 0,
        "safety_level": 0,
        "turns_in_stage": 0,
    }


# ─── ALGEE Stage Advancement ───────────────────────────────

STAGE_MIN_TURNS = {
    "approach":               3,
    "listen":                 3,
    "give_info":              2,
    "encourage_professional": 2,
    "encourage_self":         99,
}


def should_advance_stage(session: dict, user_message: str) -> bool:
    current_stage_name = ALGEE_STAGES[session["algee_stage"]]["name"]
    min_turns = STAGE_MIN_TURNS.get(current_stage_name, 2)
    turns_in_stage = session.get("turns_in_stage", 0)

    if turns_in_stage < min_turns:
        return False

    word_count = len(user_message.strip().split())
    if word_count < 6 and turns_in_stage < (min_turns + 2):
        return False

    return True


# ─── Safety ─────────────────────────────────────────────────

def detect_safety_level(text: str) -> int:
    lower = text.lower()
    if any(s in lower for s in CRISIS_SIGNALS):
        return 2
    hopeless_hits = sum(1 for s in HOPELESSNESS_SIGNALS if s in lower)
    if hopeless_hits >= 1:
        return 1
    return 0


# ─── RAG ────────────────────────────────────────────────────

def retrieve_context(query: str) -> str:
    if not collection:
        return ""
    try:
        response = embed_client.embeddings.create(
            input=[query], model=EMBEDDING_DEPLOYMENT,
        )
        query_vector = response.data[0].embedding
        results = collection.query(
            query_embeddings=[query_vector],
            n_results=TOP_K,
            include=["documents", "metadatas", "distances"],
        )
        docs      = results["documents"][0]
        metadatas = results["metadatas"][0]
        distances = results["distances"][0]

        seen = {}
        for doc, meta, dist in zip(docs, metadatas, distances):
            disorder = meta.get("disorder_name", "Unknown")
            if disorder not in seen or dist < seen[disorder][0]:
                seen[disorder] = (dist, doc, meta.get("section", ""))

        parts = []
        for disorder, (dist, doc, section) in list(seen.items())[:3]:
            similarity = round((1 - dist) * 100, 1)
            parts.append(
                f"[Clinical Reference — {disorder} / {section} (relevance: {similarity}%)]\n{doc}"
            )
        return "\n\n".join(parts)
    except Exception as e:
        print(f"RAG error: {e}")
        return ""


# ─── Prompt ─────────────────────────────────────────────────

def build_system_prompt(persona_id, algee_stage, dsm_context, safety_level) -> str:
    persona    = PERSONAS[persona_id]
    stage      = ALGEE_STAGES[min(algee_stage, len(ALGEE_STAGES) - 1)]
    stage_name = stage["name"].upper().replace("_", " ")
    stage_guide= stage["guidance"]

    context_block = ""
    if dsm_context and algee_stage >= 2:
        context_block = (
            f"\n\nRELEVANT CLINICAL BACKGROUND (translate fully into plain language; never quote directly):\n"
            f"{dsm_context}"
        )

    safety_block = ""
    if safety_level == 2:
        safety_block = SAFETY_ADDENDUM_CRISIS
    elif safety_level == 1:
        safety_block = SAFETY_ADDENDUM_HOPELESSNESS

    return (
        f"{persona['system_prompt']}\n\n"
        f"{CORE_RULES}\n\n"
        f"CURRENT ALGEE PHASE [{stage_name}]:\n{stage_guide}"
        f"{context_block}"
        f"{safety_block}"
    )


# ─── Models ─────────────────────────────────────────────────

class NewSessionRequest(BaseModel):
    persona_id: Optional[str] = DEFAULT_PERSONA


class ChatRequest(BaseModel):
    session_id: str
    message: str

    @validator("message")
    def message_must_be_valid(cls, v):
        if len(v.strip()) == 0:
            raise ValueError("Message cannot be empty")
        if len(v) > 2000:
            raise ValueError("Message too long (max 2000 characters)")
        return v.strip()


class ChatResponse(BaseModel):
    reply: str
    safety_level: int
    algee_stage: int
    algee_stage_name: str
    crisis_resources: Optional[str] = None


# ─── Endpoints ──────────────────────────────────────────────

@app.get("/health")
@limiter.limit("10/minute")
def health(request: Request):
    """Public — used by frontend to wake up the server."""
    return {"status": "ok"}


@app.get("/ping")
@limiter.limit("10/minute")
def ping(request: Request):
    """Public — lightweight wake-up endpoint."""
    return {"pong": True}


@app.post("/session/new")
@limiter.limit("5/minute")
def create_session(
    request: Request,
    req: NewSessionRequest,
    user: dict = Depends(get_current_user),
):
    if req.persona_id not in PERSONAS:
        raise HTTPException(400, f"Unknown persona: {req.persona_id}")

    user_id = user.get("sub", "anonymous")
    session = new_session(req.persona_id, user_id)
    sessions[session["id"]] = session

    greetings = {
        "mack": "Go ahead. I'm listening.",
        "ray":  "Alright, what's going on?",
        "deb":  "Hey. Whatever's on your mind — this is a good place for it.",
        "lou":  "Take your time. What's going on with you?",
    }
    greeting = greetings.get(req.persona_id, greetings["mack"])
    session["history"].append({"role": "assistant", "content": greeting})

    return {
        "session_id": session["id"],
        "greeting": greeting,
        "persona": PERSONAS[req.persona_id]["label"],
    }


@app.post("/chat", response_model=ChatResponse)
@limiter.limit("10/minute")
def chat(
    request: Request,
    req: ChatRequest,
    user: dict = Depends(get_current_user),
):
    session = sessions.get(req.session_id)
    if not session:
        raise HTTPException(404, "Session not found — please start a new session.")

    # Ensure user owns this session
    if session["user_id"] != user.get("sub"):
        raise HTTPException(403, "Not authorized for this session")

    msg_safety = detect_safety_level(req.message)
    session["safety_level"] = max(session["safety_level"], msg_safety)
    dsm_context = retrieve_context(req.message)

    system_prompt = build_system_prompt(
        persona_id  =session["persona_id"],
        algee_stage =session["algee_stage"],
        dsm_context =dsm_context,
        safety_level=session["safety_level"],
    )

    history_window = session["history"][-(MAX_HISTORY_TURNS * 2):]
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history_window)
    messages.append({"role": "user", "content": req.message})

    try:
        response = az_client.chat.completions.create(
            model=CHAT_DEPLOYMENT,
            messages=messages,
            max_tokens=MAX_RESPONSE_TOKENS,
            temperature=0.5,
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        error_str = str(e)
        if "content_filter" in error_str.lower():
            reply = (
                "I want to make sure you're okay. If you're in a dark place right now, "
                "please reach out to the 988 Suicide & Crisis Lifeline — call or text 988. "
                "They're available 24/7 and understand what you're going through."
            )
        else:
            reply = "Sorry, I had trouble connecting. Please try again."

    session["history"].append({"role": "user", "content": req.message})
    session["history"].append({"role": "assistant", "content": reply})

    session["turns_in_stage"] = session.get("turns_in_stage", 0) + 1

    if should_advance_stage(session, req.message):
        old_stage = session["algee_stage"]
        session["algee_stage"] = min(session["algee_stage"] + 1, len(ALGEE_STAGES) - 1)
        if session["algee_stage"] != old_stage:
            session["turns_in_stage"] = 0

    if session["safety_level"] >= 1:
        session["algee_stage"] = min(session["algee_stage"], 3)

    stage_name = ALGEE_STAGES[session["algee_stage"]]["name"]

    return ChatResponse(
        reply=reply,
        safety_level=session["safety_level"],
        algee_stage=session["algee_stage"],
        algee_stage_name=stage_name,
        crisis_resources=CRISIS_RESOURCES if msg_safety == 2 else None,
    )
