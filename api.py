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
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import chromadb
from chromadb.config import Settings
from openai import AzureOpenAI

from config import (
    PERSONAS, DEFAULT_PERSONA, ALGEE_STAGES, CORE_RULES,
    CRISIS_SIGNALS, HOPELESSNESS_SIGNALS, CRISIS_RESOURCES,
    SAFETY_ADDENDUM_CRISIS, SAFETY_ADDENDUM_HOPELESSNESS,
)

load_dotenv()

# ─── App ─────────────────────────────────────────────────────────

app = FastAPI(title="Kalm API", version="1.0.0")
START_TIME = time.time()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Azure Clients ────────────────────────────────────────────────

AZURE_ENDPOINT       = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY        = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION    = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
CHAT_DEPLOYMENT      = os.getenv("AZURE_CHAT_DEPLOYMENT", "gpt-4.1")
EMBEDDING_DEPLOYMENT = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")
CHROMA_DB_PATH       = os.getenv("CHROMA_DB_PATH", "./kalm_db")
TOP_K                = int(os.getenv("TOP_K_RESULTS", "5"))
COLLECTION_NAME      = "dsm_knowledge"
MAX_HISTORY_TURNS    = 8
MAX_RESPONSE_TOKENS  = 600

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

# ─── Sessions ─────────────────────────────────────────────────────

sessions: dict = {}

def new_session(persona_id: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "persona_id": persona_id,
        "history": [],
        "algee_stage": 0,
        "safety_level": 0,
        "turns_in_stage": 0,
    }

# ─── ALGEE Stage Advancement ─────────────────────────────────────
# Minimum user turns before the stage can advance.
# This prevents the bot from rushing to "see a professional" after 2 messages.
STAGE_MIN_TURNS = {
    "approach":               3,   # 3 turns just listening, no advice
    "listen":                 3,   # 3 turns validating before giving any info
    "give_info":              2,   # 2 turns of info before suggesting professional help
    "encourage_professional": 2,   # 2 turns before moving to self-help strategies
    "encourage_self":         99,  # never auto-advance past this
}

def should_advance_stage(session: dict, user_message: str) -> bool:
    """
    Advance the ALGEE stage only when:
    1. The minimum turns for this stage have been spent
    2. The user message shows meaningful engagement (>6 words)
       Short replies like "ok", "yeah", "idk" mean stay and keep listening
    """
    current_stage_name = ALGEE_STAGES[session["algee_stage"]]["name"]
    min_turns = STAGE_MIN_TURNS.get(current_stage_name, 2)
    turns_in_stage = session.get("turns_in_stage", 0)

    if turns_in_stage < min_turns:
        return False

    # Short messages = user hasn't opened up yet — give them more time
    word_count = len(user_message.strip().split())
    if word_count < 6 and turns_in_stage < (min_turns + 2):
        return False

    return True

# ─── Safety ───────────────────────────────────────────────────────

def detect_safety_level(text: str) -> int:
    lower = text.lower()
    if any(s in lower for s in CRISIS_SIGNALS):
        return 2
    hopeless_hits = sum(1 for s in HOPELESSNESS_SIGNALS if s in lower)
    if hopeless_hits >= 1:
        return 1
    return 0

# ─── RAG ──────────────────────────────────────────────────────────

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

# ─── Prompt ───────────────────────────────────────────────────────

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

# ─── Models ───────────────────────────────────────────────────────

class NewSessionRequest(BaseModel):
    persona_id: Optional[str] = DEFAULT_PERSONA

class ChatRequest(BaseModel):
    session_id: str
    message: str

class ChatResponse(BaseModel):
    reply: str
    safety_level: int
    algee_stage: int
    algee_stage_name: str
    crisis_resources: Optional[str] = None

# ─── Endpoints ────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Used by frontend to wake up the server and check status."""
    return {
        "status": "ok",
        "db_chunks": collection.count() if collection else 0,
        "personas": list(PERSONAS.keys()),
        "uptime_seconds": round(time.time() - START_TIME),
    }

@app.get("/ping")
def ping():
    """Lightweight wake-up endpoint."""
    return {"pong": True}

@app.post("/session/new")
def create_session(req: NewSessionRequest):
    if req.persona_id not in PERSONAS:
        raise HTTPException(400, f"Unknown persona: {req.persona_id}")
    session = new_session(req.persona_id)
    sessions[session["id"]] = session

    greetings = {
        "mate":      "Hey. Glad you showed up — that takes guts. What's going on?",
        "counselor": "Welcome. I'm Kalm — a safe space to talk. What brings you here today?",
        "mindful":   "Hello. Take a breath. Whatever brought you here, you're in the right place. What's present for you right now?",
        "info":      "Hi, I'm Kalm. I'm here to help you understand what you're experiencing. What would you like to talk about?",
    }
    greeting = greetings.get(req.persona_id, greetings["mate"])
    session["history"].append({"role": "assistant", "content": greeting})

    return {
        "session_id": session["id"],
        "greeting": greeting,
        "persona": PERSONAS[req.persona_id]["label"],
    }

@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session = sessions.get(req.session_id)
    if not session:
        raise HTTPException(404, "Session not found — please start a new session.")

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
            reply = f"Sorry, I had trouble connecting. Please try again."

    session["history"].append({"role": "user", "content": req.message})
    session["history"].append({"role": "assistant", "content": reply})

    # Increment turns in current stage
    session["turns_in_stage"] = session.get("turns_in_stage", 0) + 1

    # Only advance stage when the user is genuinely ready
    if should_advance_stage(session, req.message):
        old_stage = session["algee_stage"]
        session["algee_stage"] = min(session["algee_stage"] + 1, len(ALGEE_STAGES) - 1)
        # Reset turn counter when stage changes
        if session["algee_stage"] != old_stage:
            session["turns_in_stage"] = 0

    # ALGEE hold — never push past encourage_professional during crisis
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