#!/usr/bin/env python3
"""
ingest_csv_reddit.py — Load Dr. Bae's Reddit CSV into ChromaDB for Kalm

Takes the processed_construction_mental_health_full.csv and ingests it
into ChromaDB as a 'reddit_peer_knowledge' collection alongside the
existing DSM clinical knowledge.

Dataset: 8,350 records (102 original posts + 8,248 comments) across
106 threads from construction/trades subreddits discussing mental health.
Date range: Jan 2022 – Sep 2023.

Usage:
    python ingest_csv_reddit.py \
        --input processed_construction_mental_health_full.csv \
        [--min-score 2] \
        [--min-words 15] \
        [--max-depth 5] \
        [--collection reddit_peer_knowledge] \
        [--chroma-path ./kalm_db] \
        [--batch-size 50] \
        [--dry-run]

Research Context:
    Lab:     LINC (Laboratory for Interdisciplinary Research in Construction)
    PI:      Dr. JuHyeon Bae, SDSU
    Project: Kalm — LLM-Based Mental Health Support for Construction Workers
"""

import os
import re
import csv
import json
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kalm.ingest_csv")


# ─── Topic Classification (mirrors Kalm config.py) ─────────────────────────

TOPIC_KEYWORDS = {
    "crisis": [
        "suicide", "suicidal", "end it", "end my life", "kill myself",
        "want to die", "not worth living", "better off without me",
        "no reason to live", "can't go on", "hurt myself", "self harm",
        "nothing to live for",
    ],
    "substance": [
        "drinking", "alcohol", "beer", "drunk", "booze", "vodka", "whiskey",
        "can't stop drinking", "drugs", "pills", "painkillers", "weed",
        "opioid", "addicted", "substance", "sober", "relapse", "rehab",
        "hangover", "dui",
    ],
    "grief": [
        "someone died", "passed away", "lost a coworker", "death on site",
        "workmate died", "buddy died", "grieving", "grief", "funeral",
        "died on the job",
    ],
    "workplace": [
        "boss", "supervisor", "foreman", "job site", "coworker",
        "laid off", "fired", "job security", "workers comp", "workload",
        "eap", "bullying", "harassment", "overtime", "underpaid",
        "toxic work", "micromanag",
    ],
    "emotional": [
        "depressed", "depression", "anxious", "anxiety", "stressed",
        "overwhelmed", "hopeless", "angry", "numb", "empty",
        "lonely", "isolated", "burned out", "burnout", "exhausted",
        "can't sleep", "insomnia", "panic", "mental health", "ptsd",
        "trauma", "therapy", "therapist", "counselor", "counseling",
    ],
    "anger": [
        "pissed off", "angry", "furious", "losing it",
        "rage", "fed up", "had enough", "sick of it", "temper",
    ],
}


def classify_topics(text: str) -> list[str]:
    """Tag text with matching topic categories."""
    lower = text.lower()
    return [
        topic for topic, keywords in TOPIC_KEYWORDS.items()
        if any(kw in lower for kw in keywords)
    ]


# ─── Anonymization ──────────────────────────────────────────────────────────

_ANON_PATTERNS = [
    re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"),           # Phone numbers
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.\w{2,}\b"), # Emails
    re.compile(r"/?u/[A-Za-z0-9_-]+"),                            # Reddit usernames
    re.compile(r"https?://\S+"),                                   # URLs
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                         # SSNs
]


def anonymize_text(text: str) -> str:
    """Remove potentially identifying information from text."""
    if not text:
        return ""
    result = text
    for pattern in _ANON_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result.strip()


# ─── CSV Loading ────────────────────────────────────────────────────────────

def load_csv(filepath: str) -> list[dict]:
    """
    Load the CSV matching Dr. Bae's schema:
        thread_id, thread_title, comment_id, author, created_utc,
        body, score, parent_id, depth, is_submitter, is_original_post
    """
    records = []
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Parse score safely
            try:
                score = int(row.get("score", "0"))
            except (ValueError, TypeError):
                score = 0

            # Parse depth safely
            try:
                depth = int(row.get("depth", "0"))
            except (ValueError, TypeError):
                depth = 0

            is_op = row.get("is_original_post", "").strip().upper() == "TRUE"

            records.append({
                "thread_id": row.get("thread_id", "").strip(),
                "thread_title": row.get("thread_title", "").strip(),
                "comment_id": row.get("comment_id", "").strip(),
                "created_utc": row.get("created_utc", "").strip(),
                "body": row.get("body", "").strip(),
                "score": score,
                "parent_id": row.get("parent_id", "").strip(),
                "depth": depth,
                "is_submitter": row.get("is_submitter", "").strip().upper() == "TRUE",
                "is_original_post": is_op,
            })
            # NOTE: 'author' is intentionally NOT loaded — anonymization by design

    return records


# ─── Quality Filtering ─────────────────────────────────────────────────────

def passes_quality_filter(
    record: dict,
    min_score: int,
    min_words: int,
    max_depth: int,
) -> bool:
    """
    Filter records for quality before ingestion.

    Args:
        min_score:  Minimum upvote score (community validation)
        min_words:  Minimum word count (enough context to be useful)
        max_depth:  Maximum comment depth (deeper = less focused)
    """
    body = record.get("body", "")

    # Skip empty or deleted
    if not body or body in ("[deleted]", "[removed]"):
        return False

    word_count = len(body.split())
    if word_count < min_words:
        return False

    # Score filter (original posts are always valuable, only filter comments)
    if not record["is_original_post"] and record["score"] < min_score:
        return False

    # Depth filter — very deep comments tend to be off-topic arguments
    if record["depth"] > max_depth:
        return False

    return True


# ─── Document Preparation ──────────────────────────────────────────────────

def prepare_document(record: dict) -> dict:
    """
    Convert a CSV record into a ChromaDB-ready document.

    Strategy:
    - Original posts: combine thread_title + body for full context
    - Comments: use body, but include thread_title in metadata for retrieval
    - Classify content type based on whether it's a post, high-score comment
      (peer advice), or regular comment
    """
    body = anonymize_text(record["body"])
    title = anonymize_text(record["thread_title"])
    is_op = record["is_original_post"]
    score = record["score"]
    depth = record["depth"]

    # Build document text
    if is_op and title:
        doc_text = f"{title}\n\n{body}"
    else:
        doc_text = body

    # Classify topics
    full_text = f"{title} {body}"
    topics = classify_topics(full_text)

    # Determine content type for RAG prompt differentiation
    if is_op:
        content_type = "lived_experience"       # Original post = someone sharing
    elif score >= 10 and depth <= 2:
        content_type = "peer_advice"             # High-score direct reply = validated advice
    elif score >= 5:
        content_type = "peer_response"           # Moderate-score = useful response
    else:
        content_type = "peer_comment"            # Lower-score = still real but less validated

    # Build unique ID
    if record["comment_id"]:
        doc_id = f"reddit_csv_{record['comment_id']}"
    else:
        doc_id = f"reddit_csv_{record['thread_id']}_op"

    metadata = {
        "source": "reddit",
        "source_detail": "linc_lab_csv_collection",
        "thread_id": record["thread_id"],
        "thread_title": title,
        "content_type": content_type,
        "score": score,
        "depth": depth,
        "is_original_post": is_op,
        "topics": ",".join(topics) if topics else "general",
        "primary_topic": topics[0] if topics else "general",
        "created_utc": record["created_utc"],
        "word_count": len(doc_text.split()),
    }

    return {
        "id": doc_id,
        "document": doc_text,
        "metadata": metadata,
    }


# ─── ChromaDB Ingestion ────────────────────────────────────────────────────

def ingest_to_chromadb(
    documents: list[dict],
    chroma_path: str,
    collection_name: str,
    batch_size: int,
) -> None:
    """Embed and store documents in ChromaDB."""
    try:
        from openai import AzureOpenAI
        import chromadb
        from chromadb.config import Settings
    except ImportError as e:
        log.error("Missing dependency: %s", e)
        log.error("Run: pip install openai chromadb")
        exit(1)

    # Azure OpenAI for embeddings (same config as Kalm backend)
    AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
    AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
    EMBEDDING_DEPLOYMENT = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")

    if not AZURE_ENDPOINT or not AZURE_API_KEY:
        log.error(
            "Missing AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_API_KEY in .env\n"
            "These should match your Kalm backend configuration."
        )
        exit(1)

    embed_client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version="2023-05-15",
    )

    client = chromadb.PersistentClient(
        path=chroma_path,
        settings=Settings(anonymized_telemetry=False),
    )

    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={
            "description": "Reddit peer experiences — construction worker mental health (LINC Lab dataset)",
        },
    )

    existing_count = collection.count()
    log.info("ChromaDB collection '%s' — %d existing documents", collection_name, existing_count)

    total = len(documents)
    ingested = 0
    skipped_dupes = 0
    skipped_errors = 0

    for i in range(0, total, batch_size):
        batch = documents[i : i + batch_size]

        ids = [d["id"] for d in batch]
        texts = [d["document"] for d in batch]
        metadatas = [d["metadata"] for d in batch]

        # Check for duplicates
        try:
            existing = collection.get(ids=ids)
            existing_ids = set(existing["ids"]) if existing["ids"] else set()
        except Exception:
            existing_ids = set()

        new_indices = [j for j, doc_id in enumerate(ids) if doc_id not in existing_ids]
        if not new_indices:
            skipped_dupes += len(batch)
            continue

        new_ids = [ids[j] for j in new_indices]
        new_texts = [texts[j] for j in new_indices]
        new_metadatas = [metadatas[j] for j in new_indices]

        # Embed
        try:
            response = embed_client.embeddings.create(
                input=new_texts,
                model=EMBEDDING_DEPLOYMENT,
            )
            embeddings = [item.embedding for item in response.data]
        except Exception as e:
            log.error("Embedding failed for batch %d: %s", i // batch_size, e)
            skipped_errors += len(new_ids)
            continue

        # Store
        try:
            collection.add(
                ids=new_ids,
                documents=new_texts,
                embeddings=embeddings,
                metadatas=new_metadatas,
            )
            ingested += len(new_ids)
            skipped_dupes += len(batch) - len(new_ids)
        except Exception as e:
            log.error("ChromaDB insert failed for batch %d: %s", i // batch_size, e)
            skipped_errors += len(new_ids)
            continue

        log.info(
            "  Batch %d/%d — ingested %d  (total: %d/%d)",
            i // batch_size + 1,
            (total + batch_size - 1) // batch_size,
            len(new_ids),
            ingested,
            total,
        )

    final_count = collection.count()
    log.info("=" * 60)
    log.info("INGESTION COMPLETE")
    log.info("  New documents ingested:  %d", ingested)
    log.info("  Duplicates skipped:      %d", skipped_dupes)
    log.info("  Errors:                  %d", skipped_errors)
    log.info("  Collection total:        %d", final_count)
    log.info("=" * 60)


# ─── Dry Run Report ─────────────────────────────────────────────────────────

def print_dry_run_report(documents: list[dict]) -> None:
    """Print what would be ingested without actually doing it."""
    import collections as coll

    print("\n" + "=" * 60)
    print("DRY RUN REPORT — What would be ingested")
    print("=" * 60)
    print(f"Total documents: {len(documents)}")

    # By content type
    ct_counts = coll.Counter(d["metadata"]["content_type"] for d in documents)
    print(f"\nBy content type:")
    for ct, count in ct_counts.most_common():
        print(f"  {ct:25s}  {count:>5}")

    # By topic
    topic_counts = coll.Counter()
    for d in documents:
        for t in d["metadata"]["topics"].split(","):
            if t:
                topic_counts[t] += 1
    print(f"\nBy topic:")
    for t, count in topic_counts.most_common():
        print(f"  {t:20s}  {count:>5}")

    # Score distribution
    scores = sorted(d["metadata"]["score"] for d in documents)
    print(f"\nScore distribution:")
    print(f"  Min: {scores[0]}, Median: {scores[len(scores)//2]}, Max: {scores[-1]}")

    # Word count distribution
    wcs = sorted(d["metadata"]["word_count"] for d in documents)
    print(f"\nWord count distribution:")
    print(f"  Min: {wcs[0]}, Median: {wcs[len(wcs)//2]}, Max: {wcs[-1]}")

    # Sample documents
    print(f"\n--- Sample LIVED_EXPERIENCE documents ---")
    for d in documents[:3]:
        if d["metadata"]["content_type"] == "lived_experience":
            print(f"\n  [{d['id']}] score={d['metadata']['score']} topics={d['metadata']['topics']}")
            print(f"  {d['document'][:200]}...")

    print(f"\n--- Sample PEER_ADVICE documents ---")
    count = 0
    for d in documents:
        if d["metadata"]["content_type"] == "peer_advice" and count < 3:
            print(f"\n  [{d['id']}] score={d['metadata']['score']} topics={d['metadata']['topics']}")
            print(f"  {d['document'][:200]}...")
            count += 1

    print("=" * 60)


# ─── CLI ────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Ingest LINC Lab Reddit CSV into ChromaDB for Kalm",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run first — see what would be ingested without touching ChromaDB
  python ingest_csv_reddit.py --input processed_construction_mental_health_full.csv --dry-run

  # Ingest with defaults (score>=2, words>=15, depth<=5)
  python ingest_csv_reddit.py --input processed_construction_mental_health_full.csv

  # Stricter filtering — only high-quality content
  python ingest_csv_reddit.py --input processed_construction_mental_health_full.csv \\
      --min-score 5 --min-words 30 --max-depth 3

  # Point to your existing Kalm ChromaDB
  python ingest_csv_reddit.py --input processed_construction_mental_health_full.csv \\
      --chroma-path /path/to/your/kalm_db
        """,
    )
    p.add_argument("--input", required=True, help="Path to the CSV file")
    p.add_argument("--min-score", type=int, default=2, help="Min upvote score for comments (default: 2, posts always kept)")
    p.add_argument("--min-words", type=int, default=15, help="Min word count (default: 15)")
    p.add_argument("--max-depth", type=int, default=5, help="Max comment depth to include (default: 5)")
    p.add_argument("--collection", default="reddit_peer_knowledge", help="ChromaDB collection name (default: reddit_peer_knowledge)")
    p.add_argument("--chroma-path", default="./kalm_db", help="ChromaDB storage path (default: ./kalm_db)")
    p.add_argument("--batch-size", type=int, default=50, help="Embedding batch size (default: 50)")
    p.add_argument("--dry-run", action="store_true", help="Preview what would be ingested without touching ChromaDB")
    return p.parse_args()


def main():
    args = parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        log.error("Input file not found: %s", input_path)
        exit(1)

    # Load
    log.info("Loading CSV from %s", input_path)
    records = load_csv(str(input_path))
    log.info("Loaded %d raw records", len(records))

    # Filter
    filtered = [
        r for r in records
        if passes_quality_filter(r, args.min_score, args.min_words, args.max_depth)
    ]
    log.info(
        "Quality filter (score>=%d for comments, words>=%d, depth<=%d): %d → %d (%.1f%% kept)",
        args.min_score, args.min_words, args.max_depth,
        len(records), len(filtered),
        100 * len(filtered) / len(records) if records else 0,
    )

    # Prepare documents
    documents = [prepare_document(r) for r in filtered]
    log.info("Prepared %d documents", len(documents))

    if args.dry_run:
        print_dry_run_report(documents)
        log.info("Dry run complete — no data was written to ChromaDB.")
        return

    # Ingest
    ingest_to_chromadb(
        documents=documents,
        chroma_path=args.chroma_path,
        collection_name=args.collection,
        batch_size=args.batch_size,
    )

    # Save metadata for reproducibility
    meta = {
        "script": "ingest_csv_reddit.py",
        "project": "Kalm — LLM-Based Mental Health Support for Construction Workers",
        "lab": "LINC Lab, SDSU",
        "pi": "Dr. JuHyeon Bae",
        "input_file": str(input_path),
        "ingestion_date": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "min_score": args.min_score,
            "min_words": args.min_words,
            "max_depth": args.max_depth,
        },
        "raw_records": len(records),
        "filtered_records": len(filtered),
        "documents_ingested": len(documents),
        "collection_name": args.collection,
        "anonymization": "authors excluded from CSV load; usernames, emails, phone numbers, URLs, SSNs stripped from text",
    }
    meta_path = input_path.with_suffix(".ingest_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    log.info("Metadata → %s", meta_path)


if __name__ == "__main__":
    main()
