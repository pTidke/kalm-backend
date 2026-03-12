#!/usr/bin/env python3
"""
ingest.py — One-time ingestion script for Kalm RAG
Reads dsm_structured.json + all .txt knowledge documents,
chunks them, embeds with Azure OpenAI ada-002, and stores
in local ChromaDB with source + topic metadata for filtered retrieval.

Run once:  python ingest.py
Re-run:    python ingest.py --reset   (wipes and rebuilds the DB)
"""

import os
import sys
import json
import time
import argparse
import hashlib
from pathlib import Path
from dotenv import load_dotenv

import chromadb
from chromadb.config import Settings
from openai import AzureOpenAI
from colorama import init, Fore, Style
import tiktoken

load_dotenv()
init(autoreset=True)

# ─── Config ──────────────────────────────────────────────────────────────────

DSM_JSON_PATH   = Path("dsm_structured.json")
CHROMA_DB_PATH  = os.getenv("CHROMA_DB_PATH", "./kalm_db")
COLLECTION_NAME = "dsm_knowledge"

AZURE_ENDPOINT          = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY           = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION       = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
EMBEDDING_DEPLOYMENT    = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")

# ada-002 max input tokens
MAX_CHUNK_TOKENS  = 7500
EMBED_BATCH_SIZE  = 16
EMBED_RETRY_WAIT  = 2

# ── TXT Knowledge Document Registry ─────────────────────────────────────────
# Each entry defines a .txt file and its metadata for ChromaDB filtering.
# source:     unique tag used in TOPIC_SOURCE_ROUTING in config.py
# topic_tags: comma-separated topics this document covers
# agent_type: which specialist domain it belongs to

TXT_DOCUMENTS = [
    {
        "filename":   "ToolboxTalks.txt",
        "source":     "toolbox_talks",
        "topic_tags": "emotional,crisis,workplace",
        "agent_type": "peer",
        "label":      "CIASP Toolbox Talks",
    },
    {
        "filename":   "HASuicidePrevention.txt",
        "source":     "ha_suicide",
        "topic_tags": "crisis,emotional",
        "agent_type": "peer",
        "label":      "CPWR Hazard Alert: Suicide Prevention",
    },
    {
        "filename":   "SAMHSA.txt",
        "source":     "samhsa",
        "topic_tags": "substance,emotional",
        "agent_type": "peer",
        "label":      "SAMHSA Substance Use & Mental Health",
    },
    {
        "filename":   "CIASP.txt",
        "source":     "ciasp",
        "topic_tags": "workplace,crisis",
        "agent_type": "hr",
        "label":      "CIASP EAP & Crisis Protocols",
    },
    {
        "filename":   "WorkplaceSuicidePrevention.txt",
        "source":     "workplace_suicide",
        "topic_tags": "grief,crisis,workplace",
        "agent_type": "hr",
        "label":      "Workplace Suicide Postvention Guide",
    },
    {
        "filename":   "NIOSH.txt",
        "source":     "niosh",
        "topic_tags": "emotional,workplace",
        "agent_type": "osh",
        "label":      "NIOSH Worker Well-Being",
    },
    {
        "filename":   "OSHA.txt",
        "source":     "osha",
        "topic_tags": "safety,workplace",
        "agent_type": "osh",
        "label":      "OSHA Construction Safety Guide",
    },
]

TXT_CHUNK_SIZE      = 400    # target words per TXT chunk
TXT_CHUNK_OVERLAP   = 50     # words overlap between consecutive chunks

# Sections to index — ordered by priority
SECTION_PRIORITY = [
    "Diagnostic Features",
    "Description",
    "Risk and Prognostic Factors",
    "Functional Consequences",
    "Association With Suicidal Thoughts or Behavior",
    "Development and Course",
    "Differential Diagnosis",
    "Comorbidity",
    "Specifiers",
    "Prevalence",
    "Culture-Related Diagnostic Issues",
    "Sex- and Gender-Related Diagnostic Issues",
]

# ─── Helpers ─────────────────────────────────────────────────────────────────

def banner(msg: str, color=Fore.CYAN):
    print(f"\n{color}{'─' * 60}")
    print(f"  {msg}")
    print(f"{'─' * 60}{Style.RESET_ALL}")


def chunk_id(disorder_name: str, section: str, index: int = 0) -> str:
    """Stable, unique ID for each chunk."""
    raw = f"{disorder_name}::{section}::{index}"
    return hashlib.md5(raw.encode()).hexdigest()


def count_tokens(text: str, model: str = "cl100k_base") -> int:
    enc = tiktoken.get_encoding(model)
    return len(enc.encode(text))


def trim_to_token_limit(text: str, max_tokens: int = MAX_CHUNK_TOKENS) -> str:
    enc = tiktoken.get_encoding("cl100k_base")
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return enc.decode(tokens[:max_tokens])


def build_chunk_text(disorder_name: str, icd_codes: list, section: str, content: str) -> str:
    """Build the text that gets embedded — includes metadata for richer retrieval."""
    icd_str = f" (ICD: {', '.join(icd_codes)})" if icd_codes else ""
    return (
        f"Condition: {disorder_name}{icd_str}\n"
        f"Section: {section}\n\n"
        f"{content.strip()}"
    )


# ─── Load & Chunk DSM JSON ────────────────────────────────────────────────────

def load_and_chunk(path: Path) -> list[dict]:
    """
    Returns a list of chunk dicts:
      id, text, disorder_name, section, icd_codes, token_count,
      source, topic_tags, agent_type
    """
    banner("Loading DSM JSON", Fore.YELLOW)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)

    disorders = data.get("disorders", [])
    print(f"  Loaded {len(disorders)} disorder entries")

    chunks = []
    skipped = 0
    chunk_index = 0

    for disorder in disorders:
        name      = disorder.get("disorder_name", "").strip()
        icd_codes = disorder.get("icd_codes", [])
        sections  = disorder.get("sections", {})
        criteria  = disorder.get("diagnostic_criteria", [])

        if not name:
            skipped += 1
            continue

        # ── Chunk each named section ──
        for section in SECTION_PRIORITY:
            content = sections.get(section, "").strip()
            if not content or len(content) < 80:
                continue

            text = build_chunk_text(name, icd_codes, section, content)
            text = trim_to_token_limit(text)

            chunks.append({
                "id":            chunk_id(name, section, chunk_index),
                "text":          text,
                "disorder_name": name,
                "section":       section,
                "icd_codes":     ", ".join(icd_codes) if icd_codes else "",
                "token_count":   count_tokens(text),
                "source":        "dsm",
                "topic_tags":    "emotional,crisis,substance",
                "agent_type":    "peer",
            })
            chunk_index += 1

        # ── Chunk diagnostic criteria as one combined block ──
        if criteria:
            criteria_text = "\n".join(
                f"Criterion {c.get('criterion', '')}: {c.get('text', '')}"
                for c in criteria
                if c.get("text", "").strip()
            ).strip()

            if len(criteria_text) > 80:
                text = build_chunk_text(name, icd_codes, "Diagnostic Criteria", criteria_text)
                text = trim_to_token_limit(text)
                chunks.append({
                    "id":            chunk_id(name, "Diagnostic Criteria", chunk_index),
                    "text":          text,
                    "disorder_name": name,
                    "section":       "Diagnostic Criteria",
                    "icd_codes":     ", ".join(icd_codes) if icd_codes else "",
                    "token_count":   count_tokens(text),
                    "source":        "dsm",
                    "topic_tags":    "emotional,crisis,substance",
                    "agent_type":    "peer",
                })
                chunk_index += 1

    total_tokens = sum(c["token_count"] for c in chunks)
    print(f"  Created   {len(chunks)} chunks  ({skipped} entries skipped)")
    print(f"  Total tokens to embed: ~{total_tokens:,}")
    return chunks



# ─── Load & Chunk TXT Documents ───────────────────────────────────────────────

def chunk_txt_by_words(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping word-count windows."""
    words  = text.split()
    chunks = []
    start  = 0
    while start < len(words):
        end = start + chunk_size
        chunks.append(" ".join(words[start:end]))
        start += chunk_size - overlap
    return [c for c in chunks if len(c.strip()) > 100]


def load_and_chunk_txt(doc_meta: dict) -> list[dict]:
    """
    Load a .txt knowledge document and chunk it into overlapping word windows.
    Returns chunk dicts with full metadata.
    """
    path = Path(doc_meta["filename"])
    if not path.exists():
        print(f"  {Fore.YELLOW}⚠ Skipping {doc_meta['filename']} — file not found")
        return []

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    # Strip RAG metadata header lines (lines starting with # at top of file)
    lines = raw.splitlines()
    content_lines = []
    in_header = True
    for line in lines:
        if in_header and (line.startswith("#") or line.strip() == ""):
            continue
        in_header = False
        content_lines.append(line)
    content = "\n".join(content_lines).strip()

    raw_chunks = chunk_txt_by_words(content, TXT_CHUNK_SIZE, TXT_CHUNK_OVERLAP)
    chunks     = []

    for i, chunk_text in enumerate(raw_chunks):
        # Prepend source label so embeddings carry document identity
        labeled = f"[Source: {doc_meta['label']}]\n\n{chunk_text}"
        labeled = trim_to_token_limit(labeled)
        cid     = chunk_id(doc_meta["source"], f"chunk_{i}", i)

        chunks.append({
            "id":            cid,
            "text":          labeled,
            "disorder_name": doc_meta["label"],   # reuse field for display
            "section":       f"chunk_{i}",
            "icd_codes":     "",
            "token_count":   count_tokens(labeled),
            "source":        doc_meta["source"],
            "topic_tags":    doc_meta["topic_tags"],
            "agent_type":    doc_meta["agent_type"],
        })

    print(f"  {doc_meta['label']:<45} → {len(chunks)} chunks")
    return chunks

def embed_chunks(chunks: list[dict], client: AzureOpenAI) -> list[dict]:
    """Add 'embedding' key to each chunk dict. Batched with retry."""
    banner("Embedding chunks with ada-002", Fore.YELLOW)

    total = len(chunks)
    embedded = []

    for i in range(0, total, EMBED_BATCH_SIZE):
        batch = chunks[i : i + EMBED_BATCH_SIZE]
        texts = [c["text"] for c in batch]

        for attempt in range(3):
            try:
                response = client.embeddings.create(
                    input=texts,
                    model=EMBEDDING_DEPLOYMENT,
                )
                vectors = [item.embedding for item in response.data]
                for chunk, vector in zip(batch, vectors):
                    embedded.append({**chunk, "embedding": vector})
                break
            except Exception as e:
                if attempt < 2:
                    print(f"  {Fore.YELLOW}Rate limit / error, retrying in {EMBED_RETRY_WAIT}s... ({e})")
                    time.sleep(EMBED_RETRY_WAIT * (attempt + 1))
                else:
                    print(f"  {Fore.RED}Failed to embed batch {i}–{i+len(batch)}: {e}")
                    # Skip failed batch rather than crash
                    for chunk in batch:
                        embedded.append({**chunk, "embedding": None})

        done = min(i + EMBED_BATCH_SIZE, total)
        pct  = done / total * 100
        bar  = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  [{bar}] {done}/{total} chunks  ({pct:.0f}%)", end="\r")

    print()  # newline after progress bar
    failed = sum(1 for c in embedded if c["embedding"] is None)
    if failed:
        print(f"  {Fore.YELLOW}Warning: {failed} chunks could not be embedded and will be skipped")

    return [c for c in embedded if c["embedding"] is not None]


# ─── Store in ChromaDB ────────────────────────────────────────────────────────

def store_in_chroma(chunks: list[dict], db_path: str, reset: bool = False):
    banner("Storing in ChromaDB", Fore.YELLOW)

    client = chromadb.PersistentClient(
        path=db_path,
        settings=Settings(anonymized_telemetry=False),
    )

    if reset:
        try:
            client.delete_collection(COLLECTION_NAME)
            print(f"  Deleted existing collection '{COLLECTION_NAME}'")
        except Exception:
            pass

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    existing_ids = set(collection.get(include=[])["ids"])
    new_chunks   = [c for c in chunks if c["id"] not in existing_ids]

    if not new_chunks:
        print(f"  All {len(chunks)} chunks already in DB — nothing to add")
        return collection

    print(f"  Adding {len(new_chunks)} new chunks  ({len(existing_ids)} already stored)")

    # Upsert in batches of 100
    BATCH = 100
    for i in range(0, len(new_chunks), BATCH):
        batch = new_chunks[i : i + BATCH]
        collection.upsert(
            ids        = [c["id"]         for c in batch],
            embeddings = [c["embedding"]  for c in batch],
            documents  = [c["text"]       for c in batch],
            metadatas  = [{
                "disorder_name": c["disorder_name"],
                "section":       c["section"],
                "icd_codes":     c["icd_codes"],
                "source":        c.get("source", "dsm"),
                "topic_tags":    c.get("topic_tags", "emotional"),
                "agent_type":    c.get("agent_type", "peer"),
            } for c in batch],
        )
        done = min(i + BATCH, len(new_chunks))
        print(f"  Stored {done}/{len(new_chunks)} chunks", end="\r")

    print()
    total_in_db = collection.count()
    print(f"  {Fore.GREEN}✓ Collection '{COLLECTION_NAME}' now has {total_in_db} chunks")
    return collection


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kalm RAG ingestion pipeline")
    parser.add_argument("--reset", action="store_true",
                        help="Wipe the ChromaDB collection and rebuild from scratch")
    parser.add_argument("--dsm", default=str(DSM_JSON_PATH),
                        help="Path to dsm_structured.json")
    args = parser.parse_args()

    # ── Validate environment ──
    missing = [v for v in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY") if not os.getenv(v)]
    if missing:
        print(f"{Fore.RED}✗ Missing environment variables: {', '.join(missing)}")
        print(f"  Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    dsm_path = Path(args.dsm)
    if not dsm_path.exists():
        print(f"{Fore.RED}✗ DSM JSON not found at: {dsm_path}")
        print(f"  Place dsm_structured.json in the project directory.")
        sys.exit(1)

    banner("KALM RAG — Ingestion Pipeline", Fore.CYAN)
    print(f"  DSM source : {dsm_path}")
    print(f"  TXT docs   : {len(TXT_DOCUMENTS)} documents registered")
    print(f"  ChromaDB   : {CHROMA_DB_PATH}")
    print(f"  Embedding  : {EMBEDDING_DEPLOYMENT}")
    print(f"  Mode       : {'RESET + REBUILD' if args.reset else 'incremental'}")

    # ── Azure embed client (ada-002 uses older API version) ──
    embed_client = AzureOpenAI(
        azure_endpoint = AZURE_ENDPOINT,
        api_key        = AZURE_API_KEY,
        api_version    = "2023-05-15",
    )

    # ── Run DSM pipeline ──
    chunks = load_and_chunk(dsm_path)

    # ── Run TXT pipeline ──
    banner("Loading TXT Knowledge Documents", Fore.YELLOW)
    for doc_meta in TXT_DOCUMENTS:
        txt_chunks = load_and_chunk_txt(doc_meta)
        chunks.extend(txt_chunks)

    print(f"\n  Total chunks across all sources: {len(chunks):,}")

    chunks = embed_chunks(chunks, embed_client)
    store_in_chroma(chunks, CHROMA_DB_PATH, reset=args.reset)

    banner("✓ Ingestion complete — ready to chat!", Fore.GREEN)
    print(f"  Run:  python chat.py\n")


if __name__ == "__main__":
    main()
