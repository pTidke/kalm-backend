#!/usr/bin/env python3
"""
chat.py — Kalm RAG CLI Chatbot
A privacy-aware, ALGEE-guided mental health support chatbot
for construction workers (and anyone who needs it).

Run:  python chat.py
      python chat.py --persona counselor
      python chat.py --persona mindful
      python chat.py --persona info
"""

import os
import sys
import argparse
from dotenv import load_dotenv
import random


import chromadb
from chromadb.config import Settings
from openai import AzureOpenAI
from colorama import init, Fore, Back, Style

from config import (
    PERSONAS, DEFAULT_PERSONA, ALGEE_STAGES, CORE_RULES,
    CRISIS_SIGNALS, HOPELESSNESS_SIGNALS, CRISIS_RESOURCES,
    SAFETY_ADDENDUM_CRISIS, SAFETY_ADDENDUM_HOPELESSNESS,
    TOPIC_KEYWORDS, TOPIC_SOURCE_ROUTING, DEFAULT_SOURCES,
)

load_dotenv()
init(autoreset=True)

# ─── Environment ─────────────────────────────────────────────────────────────

AZURE_ENDPOINT         = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY          = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION      = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
CHAT_DEPLOYMENT        = os.getenv("AZURE_CHAT_DEPLOYMENT", "gpt-4-1")
EMBEDDING_DEPLOYMENT   = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")
CHROMA_DB_PATH         = os.getenv("CHROMA_DB_PATH", "./kalm_db")
TOP_K                  = int(os.getenv("TOP_K_RESULTS", "5"))
COLLECTION_NAME        = "dsm_knowledge"
MAX_HISTORY_TURNS      = 8    # number of user+assistant turn pairs to keep
MAX_RESPONSE_TOKENS    = 600

# ─── Terminal UI helpers ──────────────────────────────────────────────────────

PERSONA_COLORS = {
    "mate":      Fore.YELLOW,
    "counselor": Fore.CYAN,
    "mindful":   Fore.GREEN,
    "info":      Fore.MAGENTA,
}

def clear_line():
    print("\r" + " " * 80 + "\r", end="", flush=True)

def print_kalm(text: str, persona: str):
    color = PERSONA_COLORS.get(persona, Fore.CYAN)
    label = PERSONAS[persona]["label"]
    print(f"\n{color}Kalm ({label}){Style.RESET_ALL}")
    # Word-wrap at 72 chars
    words = text.split()
    line  = ""
    for word in words:
        if len(line) + len(word) + 1 > 72:
            print(f"  {line}")
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        print(f"  {line}")
    print()

def print_system(text: str, color=Fore.WHITE):
    print(f"\n{color}  {text}{Style.RESET_ALL}")

def print_divider(color=Fore.WHITE):
    print(f"{color}  {'─' * 62}{Style.RESET_ALL}")

def print_welcome(persona: str):
    color = PERSONA_COLORS.get(persona, Fore.CYAN)
    p     = PERSONAS[persona]
    print(f"\n{color}{'═' * 64}")
    print(f"  K  A  L  M  —  Mental Health Support")
    print(f"{'═' * 64}{Style.RESET_ALL}")
    print(f"\n  Persona : {color}{p['label']}{Style.RESET_ALL} — {p['description']}")
    print(f"  Type    : your message and press Enter")
    print(f"  Commands: /persona <name>  /help  /quit")
    print(f"\n  Personas available: mate · counselor · mindful · info")
    print_divider(color)

def print_crisis_resources():
    print(f"\n{Fore.RED}{CRISIS_RESOURCES}{Style.RESET_ALL}")

def print_help():
    print(f"""
{Fore.CYAN}  KALM — Help
  ────────────────────────────────────────
  Just talk — there's no wrong thing to say.
  Kalm will listen and respond with care.

  Commands:
    /persona mate       Switch to Mate persona (casual)
    /persona counselor  Switch to Counselor persona
    /persona mindful    Switch to Mindful Guide persona
    /persona info       Switch to Informer persona
    /help               Show this help
    /resources          Show crisis helplines
    /quit  or  /exit    Exit Kalm

  Crisis support (always available):
    Call or text  988  (Suicide & Crisis Lifeline)
    Text HOME to  741741  (Crisis Text Line)
{Style.RESET_ALL}""")


# ─── Safety Detection ─────────────────────────────────────────────────────────

def detect_safety_level(text: str) -> int:
    """
    Returns:
      0 — no concern
      1 — hopelessness signals (2+ hits)
      2 — explicit crisis / suicidal language
    """
    lower = text.lower()
    if any(signal in lower for signal in CRISIS_SIGNALS):
        return 2
    hopeless_hits = sum(1 for s in HOPELESSNESS_SIGNALS if s in lower)
    if hopeless_hits >= 1:
        return 1
    return 0



# ─── Topic Classifier ─────────────────────────────────────────────────────────

def classify_topics(text: str):
    """
    Scan user message for topic keywords. Returns list of matched topic tags.
    A message can match multiple topics — e.g. substance + emotional together.
    Falls back to DEFAULT_SOURCES if nothing matches.
    """
    lower  = text.lower()
    topics = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            topics.append(topic)
    return topics if topics else []


def get_sources_for_topics(topics: list[str]):
    """
    Map detected topics → unique list of source tags to query in ChromaDB.
    Merges sources from all matched topics, deduplicates, preserves priority order.
    """
    if not topics:
        return DEFAULT_SOURCES

    seen    = set()
    sources = []
    for topic in topics:
        for src in TOPIC_SOURCE_ROUTING.get(topic, []):
            if src not in seen:
                seen.add(src)
                sources.append(src)
    return sources if sources else DEFAULT_SOURCES


def retrieve_context(
    query: str,
    embed_client: AzureOpenAI,
    collection,
    top_k: int = TOP_K,
    source_filter: list = None,
) -> tuple:
    """
    Embed query, retrieve top_k chunks filtered by source tags,
    deduplicate by source, return (context_string, sources_used).
    """
    try:
        response = embed_client.embeddings.create(
            input=[query],
            model=EMBEDDING_DEPLOYMENT,
        )
        query_vector = response.data[0].embedding
    except Exception as e:
        print_system(f"⚠ Embedding failed: {e} — proceeding without RAG context", Fore.YELLOW)
        return "", []

    # Build ChromaDB where filter
    where = None
    if source_filter and len(source_filter) == 1:
        where = {"source": {"$eq": source_filter[0]}}
    elif source_filter and len(source_filter) > 1:
        where = {"source": {"$in": source_filter}}

    try:
        query_kwargs = dict(
            query_embeddings = [query_vector],
            n_results        = top_k,
            include          = ["documents", "metadatas", "distances"],
        )
        if where:
            query_kwargs["where"] = where
        results = collection.query(**query_kwargs)
    except Exception as e:
        print_system(f"⚠ ChromaDB query failed: {e}", Fore.YELLOW)
        return "", []

    docs      = results["documents"][0]
    metadatas = results["metadatas"][0]
    distances = results["distances"][0]

    if not docs:
        return "", []

    # Deduplicate: keep best-scoring chunk per source
    seen_sources = {}
    for doc, meta, dist in zip(docs, metadatas, distances):
        source  = meta.get("source", "unknown")
        section = meta.get("section", "")
        name    = meta.get("disorder_name", "")
        if source not in seen_sources or dist < seen_sources[source][0]:
            seen_sources[source] = (dist, doc, section, name)

    # Build context block — top 4 unique sources
    context_parts = []
    sources_used  = []
    for source, (dist, doc, section, name) in list(seen_sources.items())[:4]:
        similarity = round((1 - dist) * 100, 1)
        label      = f"{name} / {section}" if name and section else source
        context_parts.append(
            f"[Reference: {label}  |  source: {source}  |  relevance: {similarity}%]\n{doc}"
        )
        sources_used.append(source)

    return "\n\n".join(context_parts), sources_used



# ─── Prompt Assembly ──────────────────────────────────────────────────────────

def build_system_prompt(
    persona_id: str,
    algee_stage: int,
    dsm_context: str,
    safety_level: int,
) -> str:
    persona     = PERSONAS[persona_id]
    stage       = ALGEE_STAGES[min(algee_stage, len(ALGEE_STAGES) - 1)]
    stage_name  = stage["name"].upper().replace("_", " ")
    stage_guide = stage["guidance"]

    # Inject context from give_info stage onward
    context_block = ""
    if dsm_context and algee_stage >= 2:
        context_block = (
            f"\n\nRELEVANT BACKGROUND KNOWLEDGE (use to inform your response — "
            f"translate fully into plain, human language; never quote text directly; "
            f"never say 'according to the document'):\n"
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


# ─── Session State ────────────────────────────────────────────────────────────

class Session:
    def __init__(self, persona_id: str):
        self.persona_id    = persona_id
        self.history = []
        self.algee_stage   = 0
        self.session_safety_level = 0    # ratchets up, never down

    def add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def get_history_window(self) -> list[dict]:
        """Return last N turn-pairs (each pair = 1 user + 1 assistant)."""
        max_msgs = MAX_HISTORY_TURNS * 2
        return self.history[-max_msgs:]

    def advance_algee(self, safety_level: int = 0):
        # Don't advance past give_info if safety level is elevated
        if safety_level >= 1 and self.algee_stage >= 3:
            return  # hold at encourage_professional, don't push to self-help
        self.algee_stage = min(self.algee_stage + 1, len(ALGEE_STAGES) - 1)

    def update_safety(self, level: int):
        self.session_safety_level = max(self.session_safety_level, level)

    def reset(self, persona_id: str):
        self.persona_id   = persona_id or self.persona_id
        self.history      = []
        self.algee_stage  = 0
        self.session_safety_level = 0


# ─── Chat Turn ────────────────────────────────────────────────────────────────

def chat_turn(
    user_input: str,
    session: Session,
    az_client: AzureOpenAI,
    embed_client: AzureOpenAI,
    collection,
) -> str:
    # 1. Safety check (before any API call)
    safety_level = detect_safety_level(user_input)
    session.update_safety(safety_level)

    if safety_level == 2:
        print_crisis_resources()

    # 2. Classify topics → get targeted source filter
    topics        = classify_topics(user_input)
    source_filter = get_sources_for_topics(topics)

    # Show topic detection in dim text (helpful during development)
    if topics:
        print_system(
            f"Topics: {', '.join(topics)}  →  sources: {', '.join(source_filter)}",
            Fore.WHITE + Style.DIM
        )

    # 3. Retrieve context filtered by topic-matched sources
    print_system("Thinking...", Fore.WHITE + Style.DIM)
    rag_context, sources_used = retrieve_context(
        user_input, embed_client, collection, source_filter=source_filter
    )
    clear_line()

    # 4. Build system prompt
    system_prompt = build_system_prompt(
        persona_id   = session.persona_id,
        algee_stage  = session.algee_stage,
        dsm_context  = rag_context,
        safety_level = session.session_safety_level,
    )

    # 5. Build messages for API
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(session.get_history_window())
    messages.append({"role": "user", "content": user_input})

    # 6. Call Azure GPT
    try:
        response = az_client.chat.completions.create(
            model       = CHAT_DEPLOYMENT,
            messages    = messages,
            max_tokens  = MAX_RESPONSE_TOKENS,
            temperature = 0.5,
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        err = str(e)
        if "content_filter" in err or "content management policy" in err:
            reply = (
                "Hey, I hear you — and I'm really glad you said something. "
                "Whatever you're carrying right now, you don't have to carry it alone. "
                "Please reach out to the 988 Suicide & Crisis Lifeline — "
                "call or text 988, they're there right now, any time of day. "
                "You can also text HOME to 741741. Are you somewhere safe right now?"
            )
        else:
            reply = (
                "Sorry, I had a bit of trouble connecting just now. "
                "Please try again in a moment."
            )

    # 7. Update session
    session.add_message("user", user_input)
    session.add_message("assistant", reply)
    session.advance_algee(safety_level=session.session_safety_level)

    return reply


# ─── Main Loop ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kalm RAG Mental Health CLI")
    parser.add_argument(
        "--persona", default=DEFAULT_PERSONA,
        choices=list(PERSONAS.keys()),
        help="Starting persona (mate / counselor / mindful / info)"
    )
    args = parser.parse_args()

    # ── Validate environment ──
    missing = [v for v in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY") if not os.getenv(v)]
    if missing:
        print(f"{Fore.RED}✗ Missing environment variables: {', '.join(missing)}")
        print("  Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    # ── Connect to ChromaDB ──
    try:
        chroma_client = chromadb.PersistentClient(
            path=CHROMA_DB_PATH,
            settings=Settings(anonymized_telemetry=False),
        )
        collection = chroma_client.get_collection(COLLECTION_NAME)
        doc_count  = collection.count()
    except Exception as e:
        print(f"\n{Fore.RED}✗ Could not load ChromaDB collection: {e}")
        print(f"  Run  python ingest.py  first to build the knowledge base.\n")
        sys.exit(1)

    # ── Azure OpenAI clients ──
    az_client = AzureOpenAI(
        azure_endpoint = AZURE_ENDPOINT,
        api_key        = AZURE_API_KEY,
        api_version    = AZURE_API_VERSION,
    )
    embed_client = AzureOpenAI(
        azure_endpoint = AZURE_ENDPOINT,
        api_key        = AZURE_API_KEY,
        api_version    = "2023-05-15",     # ada-002 requires this version
    )

    # ── Session ──
    session = Session(persona_id=args.persona)
    print_welcome(args.persona)
    print_system(f"Knowledge base loaded — {doc_count:,} chunks ready", Fore.GREEN)

    # ── Opening message ──
    greetings = {
        "mate": [
            "Hey. Glad you showed up — that takes guts. What's going on?",
            "Hey. Good to have you here. What's on your mind?",
            "Hey. Whatever brought you here today, you made the right call. What's up?",
        ],
        "counselor": [
            "Welcome. I'm Kalm — a safe space to talk. What brings you here today?",
            "Hi there. Whatever's going on, you don't have to carry it alone. What's on your mind?",
        ],
        "mindful": [
            "Hello. Take a breath. Whatever brought you here, you're in the right place. What's present for you right now?",
            "Hi. Glad you're here. Let's just slow down for a second — what's going on for you today?",
        ],
        "info": [
            "Hi, I'm Kalm. I can help you understand what you're experiencing. What would you like to talk about?",
            "Hey. I'm here to help you make sense of things. What's on your mind?",
        ],
    }

    greeting = random.choice(greetings[args.persona])
    print_kalm(greeting, args.persona)
    session.add_message("assistant", greeting)

    color = PERSONA_COLORS.get(args.persona, Fore.CYAN)

    # ── Input loop ──
    while True:
        try:
            print(f"{color}  You ›{Style.RESET_ALL} ", end="", flush=True)
            user_input = input().strip()
        except (KeyboardInterrupt, EOFError):
            print_system("Take care of yourself. Goodbye. 👋", Fore.CYAN)
            break

        if not user_input:
            continue

        # ── Commands ──
        if user_input.startswith("/"):
            parts   = user_input.lower().split()
            command = parts[0]

            if command in ("/quit", "/exit", "/q"):
                print_system("Take care of yourself. Goodbye. 👋", Fore.CYAN)
                break

            elif command == "/help":
                print_help()

            elif command == "/resources":
                print_crisis_resources()

            elif command == "/persona":
                if len(parts) < 2 or parts[1] not in PERSONAS:
                    print_system(
                        f"Available personas: {', '.join(PERSONAS.keys())}", Fore.YELLOW
                    )
                else:
                    new_persona = parts[1]
                    session.reset(persona_id=new_persona)
                    color = PERSONA_COLORS.get(new_persona, Fore.CYAN)
                    p     = PERSONAS[new_persona]
                    print_system(
                        f"Switched to {p['label']} — {p['description']}", color
                    )
                    print_kalm(greetings[new_persona], new_persona)
                    session.add_message("assistant", greetings[new_persona])

            else:
                print_system(f"Unknown command '{command}'. Type /help for options.", Fore.YELLOW)

            continue

        # ── Normal chat turn ──
        reply = chat_turn(user_input, session, az_client, embed_client, collection)
        print_kalm(reply, session.persona_id)
        print_divider(PERSONA_COLORS.get(session.persona_id, Fore.WHITE))


if __name__ == "__main__":
    main()
