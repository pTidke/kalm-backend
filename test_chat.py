#!/usr/bin/env python3
"""
test_chat.py — Local chat test for Kalm with Reddit peer knowledge

Runs the full pipeline (dual RAG retrieval → system prompt → Azure OpenAI)
in an interactive terminal loop. No Supabase, no auth, no server needed.

Usage:
    python test_chat.py [--persona mack|ray|deb|lou] [--chroma-path ./kalm_db]

Requires .env with:
    AZURE_OPENAI_ENDPOINT
    AZURE_OPENAI_API_KEY
"""

import os
import sys
import argparse
from dotenv import load_dotenv

load_dotenv()

# ── Import Kalm config (personas, stages, rules, etc.) ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (
    PERSONAS, ALGEE_STAGES, CORE_RULES,
    CRISIS_SIGNALS, HOPELESSNESS_SIGNALS,
    SAFETY_ADDENDUM_CRISIS, SAFETY_ADDENDUM_HOPELESSNESS,
    CRISIS_RESOURCES, DEFAULT_PERSONA,
)

from openai import AzureOpenAI
import chromadb
from chromadb.config import Settings


# ── Azure OpenAI ──
AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
CHAT_DEPLOYMENT = os.getenv("AZURE_CHAT_DEPLOYMENT", "gpt-4.1")
EMBEDDING_DEPLOYMENT = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")

if not AZURE_ENDPOINT or not AZURE_API_KEY:
    print("❌ Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY in your .env")
    sys.exit(1)

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


# ── ChromaDB ──
def load_collections(chroma_path):
    client = chromadb.PersistentClient(
        path=chroma_path,
        settings=Settings(anonymized_telemetry=False),
    )

    dsm = None
    reddit = None

    try:
        dsm = client.get_collection("dsm_knowledge")
        print(f"  ✅ dsm_knowledge:          {dsm.count():,} docs")
    except Exception:
        print("  ⚠️  dsm_knowledge:          not found")

    try:
        reddit = client.get_collection("reddit_peer_knowledge")
        print(f"  ✅ reddit_peer_knowledge:   {reddit.count():,} docs")
    except Exception:
        print("  ⚠️  reddit_peer_knowledge:   not found (run ingest_csv_reddit.py first)")

    return dsm, reddit


# ── RAG (same logic as patched api.py) ──
def retrieve_context(query, dsm_collection, reddit_collection):
    result = {"dsm": "", "peer": ""}

    try:
        response = embed_client.embeddings.create(
            input=[query], model=EMBEDDING_DEPLOYMENT,
        )
        query_vector = response.data[0].embedding
    except Exception as e:
        print(f"  ⚠️  Embedding failed: {e}")
        return result

    if dsm_collection:
        try:
            dsm_results = dsm_collection.query(
                query_embeddings=[query_vector],
                n_results=5,
                include=["documents", "metadatas", "distances"],
            )
            docs = dsm_results["documents"][0]
            metadatas = dsm_results["metadatas"][0]
            distances = dsm_results["distances"][0]

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
            result["dsm"] = "\n\n".join(parts)
        except Exception as e:
            print(f"  ⚠️  DSM retrieval failed: {e}")

    if reddit_collection:
        try:
            reddit_results = reddit_collection.query(
                query_embeddings=[query_vector],
                n_results=4,
                include=["documents", "metadatas", "distances"],
            )
            docs = reddit_results["documents"][0]
            metadatas = reddit_results["metadatas"][0]
            distances = reddit_results["distances"][0]

            parts = []
            for doc, meta, dist in zip(docs, metadatas, distances):
                similarity = round((1 - dist) * 100, 1)
                if similarity < 30:
                    continue
                content_type = meta.get("content_type", "peer_comment")
                score = meta.get("score", 0)
                label = {
                    "lived_experience": "Worker sharing their experience",
                    "peer_advice":      f"Peer advice (community score: {score})",
                    "peer_response":    f"Peer response (score: {score})",
                    "peer_comment":     "Community discussion",
                }.get(content_type, "Peer context")
                parts.append(f"[{label} — relevance: {similarity}%]\n{doc}")
            result["peer"] = "\n\n".join(parts)
        except Exception as e:
            print(f"  ⚠️  Reddit retrieval failed: {e}")

    return result


# ── Safety ──
def detect_safety_level(text):
    lower = text.lower()
    if any(s in lower for s in CRISIS_SIGNALS):
        return 2
    if sum(1 for s in HOPELESSNESS_SIGNALS if s in lower) >= 1:
        return 1
    return 0


# ── System prompt (same as patched api.py) ──
def build_system_prompt(persona_id, algee_stage, dsm_context, peer_context, safety_level):
    persona = PERSONAS[persona_id]
    stage = ALGEE_STAGES[min(algee_stage, len(ALGEE_STAGES) - 1)]
    stage_name = stage["name"].upper().replace("_", " ")
    stage_guide = stage["guidance"]

    context_block = ""

    if peer_context and algee_stage >= 1:
        context_block += (
            "\n\nREAL PEER EXPERIENCES (use to match language and tone; "
            "never quote directly — absorb the way workers talk about these issues "
            "and reflect it naturally in your voice):\n"
            f"{peer_context}"
        )

    if dsm_context and algee_stage >= 2:
        context_block += (
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


# ── ALGEE advancement ──
STAGE_MIN_TURNS = {
    "approach": 3, "listen": 3, "give_info": 2,
    "encourage_professional": 2, "encourage_self": 99,
}

def should_advance(algee_stage, turns_in_stage, message):
    stage_name = ALGEE_STAGES[algee_stage]["name"]
    min_turns = STAGE_MIN_TURNS.get(stage_name, 2)
    if turns_in_stage < min_turns:
        return False
    if len(message.split()) < 6 and turns_in_stage < (min_turns + 2):
        return False
    return True


# ── Main chat loop ──
def main():
    parser = argparse.ArgumentParser(description="Kalm local chat test")
    parser.add_argument("--persona", default="lou", choices=["mack", "ray", "deb", "lou"])
    parser.add_argument("--chroma-path", default="./kalm_db")
    parser.add_argument("--show-context", action="store_true", help="Print RAG context for each turn")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print("  KALM — Local Chat Test (Reddit + DSM RAG)")
    print(f"{'='*60}")
    print(f"  Persona:    {args.persona} ({PERSONAS[args.persona]['label']})")
    print(f"  ChromaDB:   {args.chroma_path}")
    print()

    dsm_collection, reddit_collection = load_collections(args.chroma_path)

    # Session state
    history = []
    algee_stage = 0
    safety_level = 0
    turns_in_stage = 0

    # Greeting
    greetings = {
        "mack": "Go ahead. I'm listening.",
        "ray":  "Alright, what's going on?",
        "deb":  "Hey. Whatever's on your mind — this is a good place for it.",
        "lou":  "Take your time. What's going on with you?",
    }
    greeting = greetings[args.persona]
    history.append({"role": "assistant", "content": greeting})

    stage_name = ALGEE_STAGES[algee_stage]["name"]
    print(f"\n  [{args.persona.upper()}]: {greeting}")
    print(f"  (stage: {stage_name} | safety: {safety_level})")
    print(f"\n  Type 'quit' to exit, 'stage' to see current ALGEE stage")
    print(f"  {'─'*56}\n")

    while True:
        try:
            user_input = input("  You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n  👋 Session ended.")
            break

        if not user_input:
            continue
        if user_input.lower() == "quit":
            print("\n  👋 Session ended.")
            break
        if user_input.lower() == "stage":
            stage_name = ALGEE_STAGES[algee_stage]["name"]
            print(f"  → ALGEE stage: {algee_stage} ({stage_name}) | turns in stage: {turns_in_stage} | safety: {safety_level}\n")
            continue

        # Safety check
        msg_safety = detect_safety_level(user_input)
        safety_level = max(safety_level, msg_safety)

        # RAG retrieval
        rag_context = retrieve_context(user_input, dsm_collection, reddit_collection)

        if args.show_context:
            print(f"\n  --- RAG Context ---")
            if rag_context["peer"]:
                print(f"  PEER: {rag_context['peer'][:300]}...")
            else:
                print(f"  PEER: (none)")
            if rag_context["dsm"]:
                print(f"  DSM:  {rag_context['dsm'][:300]}...")
            else:
                print(f"  DSM:  (none / not injected at this stage)")
            print(f"  -------------------\n")

        # Build prompt
        system_prompt = build_system_prompt(
            persona_id=args.persona,
            algee_stage=algee_stage,
            dsm_context=rag_context["dsm"],
            peer_context=rag_context["peer"],
            safety_level=safety_level,
        )

        # Build messages
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-(8 * 2):])  # last 8 turns
        messages.append({"role": "user", "content": user_input})

        # Call Azure OpenAI
        try:
            response = az_client.chat.completions.create(
                model=CHAT_DEPLOYMENT,
                messages=messages,
                max_tokens=250,
                temperature=0.5,
            )
            reply = response.choices[0].message.content.strip()
        except Exception as e:
            print(f"\n  ⚠️  LLM call failed: {e}")
            reply = "Sorry, had trouble connecting. Try again."

        # Update history
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": reply})

        # Advance ALGEE
        turns_in_stage += 1
        if should_advance(algee_stage, turns_in_stage, user_input):
            old = algee_stage
            algee_stage = min(algee_stage + 1, len(ALGEE_STAGES) - 1)
            if algee_stage != old:
                turns_in_stage = 0

        if safety_level >= 1:
            algee_stage = min(algee_stage, 3)

        stage_name = ALGEE_STAGES[algee_stage]["name"]
        print(f"\n  [{args.persona.upper()}]: {reply}")
        print(f"  (stage: {stage_name} | safety: {safety_level})\n")


if __name__ == "__main__":
    main()
