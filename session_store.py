"""
session_store.py — Persistent session storage backed by Supabase + Fernet encryption.

Chat messages are encrypted at the application level before being stored,
so even direct DB access cannot read conversation content.
"""

import os
import logging
from typing import Optional
from cryptography.fernet import Fernet
from supabase import create_client, Client

logger = logging.getLogger("kalm.sessions")

# ─── Supabase client (service role for server-side access) ───

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")

_sb: Optional[Client] = None


def _get_sb() -> Client:
    global _sb
    if _sb is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        _sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _sb


# ─── Encryption ──────────────────────────────────────────────

ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")
_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not ENCRYPTION_KEY:
            raise RuntimeError(
                "ENCRYPTION_KEY must be set (generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")"
            )
        _fernet = Fernet(ENCRYPTION_KEY.encode())
    return _fernet


def encrypt(text: str) -> str:
    return _get_fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()


# ─── Session CRUD ────────────────────────────────────────────

def create_session(session_id: str, user_id: str, persona_id: str) -> dict:
    """Insert a new session row and return session dict."""
    sb = _get_sb()
    row = {
        "id": session_id,
        "user_id": user_id,
        "persona_id": persona_id,
        "algee_stage": 0,
        "safety_level": 0,
        "turns_in_stage": 0,
    }
    sb.table("sessions").insert(row).execute()
    logger.info("Session created: %s for user %s", session_id, user_id[:8])
    return {**row, "history": []}


def load_session(session_id: str) -> Optional[dict]:
    """Load session + decrypted message history. Returns None if not found."""
    sb = _get_sb()
    result = sb.table("sessions").select("*").eq("id", session_id).execute()
    if not result.data:
        return None

    row = result.data[0]
    # Load messages
    msgs = (
        sb.table("messages")
        .select("role, content_enc")
        .eq("session_id", session_id)
        .order("created_at")
        .execute()
    )
    history = [
        {"role": m["role"], "content": decrypt(m["content_enc"])}
        for m in msgs.data
    ]

    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "persona_id": row["persona_id"],
        "algee_stage": row["algee_stage"],
        "safety_level": row["safety_level"],
        "turns_in_stage": row["turns_in_stage"],
        "history": history,
    }


def update_session(session_id: str, **fields) -> None:
    """Update session metadata (algee_stage, safety_level, turns_in_stage)."""
    allowed = {"algee_stage", "safety_level", "turns_in_stage"}
    update = {k: v for k, v in fields.items() if k in allowed}
    if update:
        _get_sb().table("sessions").update(update).eq("id", session_id).execute()


def append_message(session_id: str, role: str, content: str) -> None:
    """Encrypt and store a single chat message."""
    _get_sb().table("messages").insert({
        "session_id": session_id,
        "role": role,
        "content_enc": encrypt(content),
    }).execute()


# ─── User data (GDPR) ───────────────────────────────────────

def export_user_data(user_id: str) -> list[dict]:
    """Return all sessions and decrypted messages for a user."""
    sb = _get_sb()
    sessions = (
        sb.table("sessions")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
    )
    result = []
    for s in sessions.data:
        msgs = (
            sb.table("messages")
            .select("role, content_enc, created_at")
            .eq("session_id", s["id"])
            .order("created_at")
            .execute()
        )
        result.append({
            "session_id": s["id"],
            "persona_id": s["persona_id"],
            "created_at": s["created_at"],
            "messages": [
                {
                    "role": m["role"],
                    "content": decrypt(m["content_enc"]),
                    "created_at": m["created_at"],
                }
                for m in msgs.data
            ],
        })
    logger.info("Data export for user %s: %d sessions", user_id[:8], len(result))
    return result


def delete_user_data(user_id: str) -> int:
    """Delete all sessions and messages for a user. Returns count of deleted sessions."""
    sb = _get_sb()
    sessions = (
        sb.table("sessions")
        .select("id")
        .eq("user_id", user_id)
        .execute()
    )
    count = len(sessions.data)
    if count > 0:
        # CASCADE on messages handles cleanup
        sb.table("sessions").delete().eq("user_id", user_id).execute()
    logger.info("Deleted %d sessions for user %s", count, user_id[:8])
    return count
