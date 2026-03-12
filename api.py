#!/usr/bin/env python3
"""
api.py — Kalm FastAPI Backend
Wraps the existing chat.py logic and exposes REST endpoints
for the React frontend.

Run locally:
    uvicorn api:app --reload --port 8000

With ngrok (for demo):
    ngrok http 8000
"""

import os
import uuid
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

# ─── App Setup ──────────────────────────────────────────────────────────────

app = FastAPI(title="Kalm API", version="1.0.0")

# Allow all origins for demo — tighten for production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Azure + ChromaDB Clients ─────────────────────────────────────────────

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

chroma_client = chromadb.PersistentClient(
    path=CHROMA_DB_PATH,
    settings=Settings(anonymized_telemetry=False),
)

try:
    collection = chroma_client.get_collection(COLLECTION_NAME)
    print(f"✓ ChromaDB loaded — {collection.count():,} chunks")
except Exception as e:
    print(f"✗ ChromaDB failed: {e}")
    collection = None

# ─── In-Memory Session Store ─────────────────────────────────────────────

sessions: dict[str, dict] = {}

def new_session(persona_id: str = DEFAULT_PERSONA) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "persona_id": persona_id,
        "history": [],
        "algee_stage": 0,
        "safety_level": 0,
    }

# ─── Safety Detection ────────────────────────────────────────────────────

def detect_safety_level(text: str) -> int:
    lower = text.lower()
    if any(s in lower for s in CRISIS_SIGNALS):
        return 2
    hopeless_hits = sum(1 for s in HOPELESSNESS_SIGNALS if s in lower)
    if hopeless_hits >= 1:
        return 1
    return 0

# ─── RAG Retrieval ───────────────────────────────────────────────────────

def retrieve_context(query: str) -> str:
    if not collection:
        return ""
    try:
        response = embed_client.embeddings.create(
            input=[query],
            model=EMBEDDING_DEPLOYMENT,
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

        seen: dict = {}
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

# ─── Prompt Assembly ─────────────────────────────────────────────────────

def build_system_prompt(persona_id: str, algee_stage: int, dsm_context: str, safety_level: int) -> str:
    persona     = PERSONAS[persona_id]
    stage       = ALGEE_STAGES[min(algee_stage, len(ALGEE_STAGES) - 1)]
    stage_name  = stage["name"].upper().replace("_", " ")
    stage_guide = stage["guidance"]

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

# ─── Request / Response Models ────────────────────────────────────────────

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

# ─── Endpoints ───────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "db_chunks": collection.count() if collection else 0,
        "personas": list(PERSONAS.keys()),
    }

@app.post("/session/new")
def create_session(req: NewSessionRequest):
    if req.persona_id not in PERSONAS:
        raise HTTPException(400, f"Unknown persona: {req.persona_id}")
    session = new_session(req.persona_id)
    sessions[session["id"]] = session

    # Generate greeting
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
        raise HTTPException(404, "Session not found. Create a new session first.")

    # Safety check
    msg_safety = detect_safety_level(req.message)
    session["safety_level"] = max(session["safety_level"], msg_safety)

    # RAG
    dsm_context = retrieve_context(req.message)

    # Build prompt
    system_prompt = build_system_prompt(
        persona_id  = session["persona_id"],
        algee_stage = session["algee_stage"],
        dsm_context = dsm_context,
        safety_level= session["safety_level"],
    )

    # Build messages
    history_window = session["history"][-(MAX_HISTORY_TURNS * 2):]
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history_window)
    messages.append({"role": "user", "content": req.message})

    # Call Azure GPT
    try:
        response = az_client.chat.completions.create(
            model=CHAT_DEPLOYMENT,
            messages=messages,
            max_tokens=MAX_RESPONSE_TOKENS,
            temperature=0.5,
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        reply = f"Sorry, I had a bit of trouble connecting just now. Please try again. ({e})"

    # Update session
    session["history"].append({"role": "user", "content": req.message})
    session["history"].append({"role": "assistant", "content": reply})
    session["algee_stage"] = min(session["algee_stage"] + 1, len(ALGEE_STAGES) - 1)

    # ALGEE hold on crisis
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

@app.post("/session/{session_id}/switch-persona")
def switch_persona(session_id: str, req: NewSessionRequest):
    if session_id not in sessions:
        raise HTTPException(404, "Session not found.")
    if req.persona_id not in PERSONAS:
        raise HTTPException(400, f"Unknown persona: {req.persona_id}")
    sessions[session_id]["persona_id"] = req.persona_id
    sessions[session_id]["history"] = []
    sessions[session_id]["algee_stage"] = 0
    sessions[session_id]["safety_level"] = 0
    return {"status": "ok", "persona": PERSONAS[req.persona_id]["label"]}
