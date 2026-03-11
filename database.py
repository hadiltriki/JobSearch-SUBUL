"""
database.py — PostgreSQL Azure connection + table creation + trigger

CORRECTIONS APPLIQUÉES (vs version originale) :
  FIX 1 — get_jobs_for_user : WHERE $1::integer = ANY(id_user)
           cast explicite évite le type mismatch asyncpg integer[] vs int4
  FIX 2 — get_jobs_for_user : gap_coverage calculé correctement
           était "1.0 if not gap else 0.0" → toujours 0% si gap non vide!
           maintenant : (total_req - missing) / total_req
  FIX 3 — user_has_jobs : même cast ::integer
"""

import json as _json
import logging
import os
from typing import Optional


from dotenv import load_dotenv
from pathlib import Path
from azure.cosmos import CosmosClient, PartitionKey, exceptions
import asyncio
import uuid
# Optional: fallback XAI when job was saved without xai (e.g. before column existed)
try:
    from xai_explainer import _fallback_xai as _xai_fallback
except Exception:
    _xai_fallback = None

load_dotenv(Path(__file__).parent / ".env")
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Connection string depuis .env
# ─────────────────────────────────────────────────────────────────────────────

def _get_cosmos_client() -> str:
    endpoint = os.getenv("AZURE_COSMOS_ENDPOINT", "").strip()
    key      = os.getenv("AZURE_COSMOS_KEY", "").strip()
    if not endpoint or not key:
        raise ValueError("CosmosDB credentials missing in .env")
    return CosmosClient(endpoint, credential=key)


# ─────────────────────────────────────────────────────────────────────────────
#  Pool global (initialisé au startup de FastAPI)
# ─────────────────────────────────────────────────────────────────────────────

_db_name        = os.getenv("AZURE_COSMOS_DATABASE_NAME", "EduTech_AI_Production")
_jobs_cname     = os.getenv("AZURE_COSMOS_JOBS_CONTAINER", "AgentSearchJobs")
_users_cname    = "users"
_chat_cname     = "chat_history"

def _get_containers():
    client = _get_cosmos_client()
    db = client.get_database_client(_db_name)
    return (
        db.get_container_client(_users_cname),
        db.get_container_client(_jobs_cname),
        db.get_container_client(_chat_cname),
    )

async def close_pool():
    logger.info("[db] CosmosDB — nothing to close")


# ─────────────────────────────────────────────────────────────────────────────
#  Création des tables + trigger (idempotent)
# ─────────────────────────────────────────────────────────────────────────────



async def init_db():
    try:
        client = _get_cosmos_client()
        db = client.get_database_client(_db_name)
        db.create_container_if_not_exists(id=_users_cname, partition_key=PartitionKey(path="/id"))
        db.create_container_if_not_exists(id=_jobs_cname,  partition_key=PartitionKey(path="/url"))
        db.create_container_if_not_exists(id=_chat_cname,  partition_key=PartitionKey(path="/user_id"))
        logger.info("[db] CosmosDB containers initialized OK")
    except Exception as e:
        logger.error(f"[db] init_db FAILED: {e}")
        raise


# ─────────────────────────────────────────────────────────────────────────────
#  CRUD users
# ─────────────────────────────────────────────────────────────────────────────

async def upsert_user(user_id: int, cv_structured: dict) -> bool:
    container_users, _, _ = _get_containers()
    existing = {}
    try:
        existing = await asyncio.to_thread(container_users.read_item, item=str(user_id), partition_key=str(user_id))
    except exceptions.CosmosResourceNotFoundError:
        pass
    doc = {
        "id": str(user_id),
        "first_name": cv_structured.get("first_name") or existing.get("first_name"),
        "last_name":  cv_structured.get("last_name")  or existing.get("last_name"),
        "email":      cv_structured.get("email")      or existing.get("email"),
        "linkedin":   cv_structured.get("linkedin")   or existing.get("linkedin"),
        "role":       cv_structured.get("role", ""),
        "seniority":  cv_structured.get("seniority", ""),
        "years_exp":  cv_structured.get("years_experience", ""),
        "industry":   cv_structured.get("industry", ""),
        "education":  cv_structured.get("education", ""),
        "skills":     cv_structured.get("skills", ""),
        "summary":    cv_structured.get("summary", ""),
        "bullets":    cv_structured.get("bullets", ""),
    }
    try:
        await asyncio.to_thread(container_users.upsert_item, body=doc)
        logger.info(f"[db] ✅ User {user_id} upserted OK")
        return True
    except Exception as e:
        logger.error(f"[db] ❌ upsert_user failed for user_id={user_id}: {e}")
        return False

async def get_user(user_id: int) -> Optional[dict]:
    container_users, _, _ = _get_containers()
    try:
        row = await asyncio.to_thread(container_users.read_item, item=str(user_id), partition_key=str(user_id))
        return {
            "first_name":       row.get("first_name"),
            "last_name":        row.get("last_name"),
            "email":            row.get("email"),
            "linkedin":         row.get("linkedin"),
            "role":             row.get("role"),
            "seniority":        row.get("seniority"),
            "years_experience": row.get("years_exp"),
            "industry":         row.get("industry"),
            "education":        row.get("education"),
            "skills":           row.get("skills"),
            "summary":          row.get("summary"),
            "bullets":          row.get("bullets"),
        }
    except exceptions.CosmosResourceNotFoundError:
        logger.info(f"[db] get_user: no user found for id={user_id}")
        return None
    except Exception as e:
        logger.error(f"[db] get_user failed for id={user_id}: {e}")
        return None


async def update_user_profile(
    user_id: int,
    first_name: str = None,
    last_name:  str = None,
    email:      str = None,
    linkedin:   str = None,
) -> bool:
    container_users, _, _ = _get_containers()
    try:
        row = await asyncio.to_thread(container_users.read_item, item=str(user_id), partition_key=str(user_id))
    except exceptions.CosmosResourceNotFoundError:
        logger.warning(f"[db] update_user_profile: user_id={user_id} not found in DB")
        return False
    if first_name is not None: row["first_name"] = first_name
    if last_name  is not None: row["last_name"]  = last_name
    if email      is not None: row["email"]      = email
    if linkedin   is not None: row["linkedin"]   = linkedin
    try:
        await asyncio.to_thread(container_users.replace_item, item=str(user_id), body=row)
        logger.info(f"[db] ✅ Profile updated for user_id={user_id}")
        return True
    except Exception as e:
        logger.error(f"[db] ❌ update_user_profile failed for user_id={user_id}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  CRUD jobs
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_company_for_dedupe(company: str) -> str:
    """Canonical company name for dedupe: strip ' — Sector', ' / Subsector' so same employer = one key."""
    c = (company or "").strip().lower()
    c = " ".join(c.split())
    # "Company — Defense / Intelligence" or "Company / Division" → "Company"
    if " — " in c:
        c = c.split(" — ")[0].strip()
    if " / " in c:
        c = c.split(" / ")[0].strip()
    for suffix in (", inc.", " inc.", ", inc", " inc", ", llc.", " llc.", " llc"):
        if c.endswith(suffix):
            c = c[: -len(suffix)].strip()
    return c


# Title prefixes/suffixes to strip for dedupe (must match scraping_pipeline._normalize_title_for_dedupe)
_TITLE_DEDUPE_STRIP = (
    "offre d'emploi ",
    "offre d'emploi",
    "we're hiring: ",
    "we're hiring:",
    "stage : ",
    "stage :",
    "formation ",
    "emploi tunisie ",
    "emploi tunisie",
    "découvrez les dernières offres en ",
    "0 ofertas de ",
    "ofertas de ",
    "emplois ",
)
_TITLE_DEDUPE_SUFFIXES = (
    " en argentina",
    " en france",
    " uk / en france",
    " (remote)",
    " (argentina)",
    " (tunisia)",
)


def _normalize_title_for_dedupe(title: str) -> str:
    """Canonical title for dedupe: strip common prefixes/suffixes and normalize whitespace/slashes."""
    t = (title or "").strip().lower()
    t = " ".join(t.split())
    for prefix in _TITLE_DEDUPE_STRIP:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    for suffix in _TITLE_DEDUPE_SUFFIXES:
        if t.endswith(suffix):
            t = t[: -len(suffix)].strip()
    for sep in (" / ", " /", "/ ", " – ", " - ", " — "):
        t = t.replace(sep, " ")
    t = " ".join(t.split())
    return t


def _job_dedupe_key(title: str, company: str) -> str:
    """Normalize title + company so same role from different sources = one row.
    When title is 'Title / Company' (e.g. eluta), use title part and company part so
    duplicates with or without company field still get the same key."""
    raw_title = (title or "").strip()
    raw_company = (company or "").strip()
    if " / " in raw_title:
        parts = raw_title.split(" / ", 1)
        title_for_key = parts[0].strip()
        company_from_title = parts[1].strip() if len(parts) > 1 else ""
        company_for_key = raw_company or company_from_title
    else:
        title_for_key = raw_title
        company_for_key = raw_company
    t = _normalize_title_for_dedupe(title_for_key)
    c = _normalize_company_for_dedupe(company_for_key)
    return f"{t}|{c}"


async def insert_job(user_id: int, card: dict) -> bool:
    logger.info(f"[db] insert_job called — user_id={user_id} url={card.get('url','')[:60]}")

    if not user_id or user_id <= 0:
        logger.warning("[db] insert_job skipped: user_id invalide")
        return False

    if not card.get("url"):
        logger.warning("[db] insert_job skipped: url vide")
        return False

    title_ = card.get("title", "")
    company_ = card.get("industry") or card.get("company", "")
    dedupe_key = _job_dedupe_key(title_, company_)

    _, container_jobs, _ = _get_containers()
    try:
        existing = list(await asyncio.to_thread(lambda: list(container_jobs.query_items(
            query="SELECT c.id FROM c WHERE c.user_id=@uid AND (c.dedupe_key=@dk OR (c.title=@t AND c.industry=@co)) OFFSET 0 LIMIT 1",
            parameters=[{"name":"@uid","value":user_id},{"name":"@dk","value":dedupe_key},{"name":"@t","value":title_},{"name":"@co","value":company_}],
            enable_cross_partition_query=True
        ))))
        if existing:
            logger.info(f"[db] skip duplicate — user={user_id} dedupe_key={dedupe_key[:50]}…")
            return True

        gap_missing       = card.get("gap_missing", [])
        xai_obj           = card.get("xai")
        raw_match         = card.get("match_score", -1)
        match_score_db    = float(raw_match) if raw_match is not None and raw_match >= 0 else None
        cosine_score_db   = float(card.get("cosine", card.get("cosine_score", 0)) or 0)
        combined_score_db = float(card.get("combined_score", 0) or 0)

        doc = {
            "id": str(uuid.uuid4()), "id_job": str(uuid.uuid4()),
            "id_user": [user_id], "user_id": user_id,
            "url":             card.get("url", ""),
            "source":          card.get("source", ""),
            "title":           card.get("title", ""),
            "industry":        card.get("industry") or card.get("company", ""),
            "location":        card.get("location", ""),
            "seniority":       card.get("experience", ""),
            "must_have":       card.get("skills_req", ""),
            "nice_to_have":    card.get("skills_bon", ""),
            "description":     card.get("description", ""),
            "responsibilities": card.get("description", ""),
            "requirements":    card.get("skills_req", ""),
            "salary":          card.get("salary", ""),
            "match_score":     match_score_db,
            "cosine_score":    cosine_score_db,
            "combined_score":  combined_score_db,
            "contract":        card.get("contract", ""),
            "education":       card.get("education", ""),
            "remote":          card.get("remote", ""),
            "skills_gap":      _json.dumps(gap_missing, ensure_ascii=False),
            "xai":             xai_obj,
            "dedupe_key":      dedupe_key,
        }
        await asyncio.to_thread(container_jobs.create_item, body=doc)
        logger.info(f"[db] ✅ Job inserted — user={user_id} url={card.get('url','')[:60]}")
        return True
    except Exception as e:
        logger.error(f"[db] ❌ insert_job failed — user={user_id} url={card.get('url','')[:60]}: {e}")
        return False

async def get_jobs_for_user(user_id: int) -> list[dict]:
    """
    Retourne tous les jobs où user_id est dans id_user[].
    Triés par match_score DESC (AI Match) pour alignement avec l’onglet Matches.

    FIX 1 : $1::integer — cast explicite pour éviter type mismatch asyncpg
             avec les colonnes INTEGER[] sur Azure PostgreSQL.
    FIX 2 : gap_coverage calculé correctement depuis must_have vs gap_missing.
    """
    _, container_jobs, _ = _get_containers()
    try:
        rows = list(await asyncio.to_thread(lambda: list(container_jobs.query_items(
            query="SELECT * FROM c WHERE c.user_id=@uid ORDER BY c.match_score DESC OFFSET 0 LIMIT 2000",
            parameters=[{"name":"@uid","value":user_id}],
            enable_cross_partition_query=True
        ))))
        raw_count = len(rows)
        # Build list then dedupe by (title, normalized company) so same role appears once
        def _row_key(r) -> str:
            return _job_dedupe_key(r.get("title") or "", r.get("industry") or "")

        seen_keys: set[str] = set()
        result = []
        for row in rows:
            nkey = _row_key(row)
            if nkey in seen_keys:
                continue
            seen_keys.add(nkey)
            gap = []
            try:
                gap = _json.loads(row.get("skills_gap") or "[]")
            except Exception:
                pass

            match_raw    = row.get("match_score")
            cosine_raw   = row.get("cosine_score")  or 0.0
            combined_raw = row.get("combined_score") or 0.0

            # ── FIX 2 : gap_coverage correct ──────────────────────────────
            must_have_str  = row.get("must_have") or ""
            must_have_list = [s.strip() for s in must_have_str.split(",") if s.strip()]
            total_skills   = len(must_have_list)
            missing_count  = len(gap)
            if total_skills > 0:
                gap_coverage = max(0.0, (total_skills - missing_count) / total_skills)
            else:
                # Pas de skills requis connus → couverture complète si pas de gap
                gap_coverage = 1.0 if missing_count == 0 else 0.5

            # xai from DB; if missing (old jobs saved before xai column), use fallback so UI still shows formula + interpretation
            xai_val = row.get("xai")
            needs_xai_backfill = False
            if xai_val is None and _xai_fallback is not None:
                needs_xai_backfill = True
                try:
                    xai_val = _xai_fallback(
                        float(cosine_raw or 0),
                        float(match_raw if match_raw is not None else 0),
                        float(combined_raw or 0),
                        gap_coverage,
                        total_skills,
                    )
                except Exception:
                    pass

            result.append({
                "id_job":         row.get("id_job"),
                "url":            row.get("url"),
                "source":         row.get("source"),
                "title":          row.get("title"),
                "industry":       row.get("industry") or "",
                "location":       row.get("location"),
                "remote":         row.get("remote"),
                "salary":         row.get("salary"),
                "contract":       row.get("contract"),
                "education":      row.get("education"),
                "experience":     row.get("seniority"),
                "match_score":         match_raw if match_raw is not None else -1,
                "cosine":              cosine_raw,
                "combined_score":      combined_raw,
                "match_score_display":    f"{(match_raw or 0) * 100:.2f}" if match_raw is not None and match_raw >= 0 else "—",
                "cosine_display":         f"{cosine_raw   * 100:.2f}",
                "combined_score_display": f"{combined_raw * 100:.2f}",
                "gap_missing":    gap,
                "gap_matched":    [],
                "gap_coverage":   gap_coverage,   # ← FIX 2
                "gap_total":      total_skills,
                "description":    row.get("description"),
                "skills_req":     row.get("must_have"),
                "skills_bon":     row.get("nice_to_have"),
                "tags":           row.get("requirements"),
                "event":          "job",
                "xai":            xai_val,
                "_needs_xai_backfill": needs_xai_backfill,
            })
        logger.info(f"[db] get_jobs_for_user: {len(result)} jobs (raw rows={raw_count}) for user_id={user_id}")
        return result
    except Exception as e:
        logger.error(f"[db] get_jobs_for_user failed for user_id={user_id}: {e}")
        return []


async def update_job_xai(id_job: int, xai_dict: dict) -> bool:
    _, container_jobs, _ = _get_containers()
    try:
        items = list(await asyncio.to_thread(lambda: list(container_jobs.query_items(
            query="SELECT * FROM c WHERE c.id_job=@id OFFSET 0 LIMIT 1",
            parameters=[{"name":"@id","value":str(id_job)}],
            enable_cross_partition_query=True
        ))))
        if not items: return False
        doc = items[0]; doc["xai"] = xai_dict
        await asyncio.to_thread(container_jobs.replace_item, item=doc["id"], body=doc)
        logger.debug("[db] update_job_xai OK — id_job=%s", id_job)
        return True
    except Exception as e:
        logger.error(f"[db] update_job_xai failed id_job={id_job}: {e}")
        return False


async def user_has_jobs(user_id: int) -> bool:
    _, container_jobs, _ = _get_containers()
    try:
        counts = list(await asyncio.to_thread(lambda: list(container_jobs.query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.user_id=@uid",
            parameters=[{"name":"@uid","value":user_id}],
            enable_cross_partition_query=True
        ))))
        count = counts[0] if counts else 0
        has = (count or 0) > 0
        logger.info(f"[db] user_has_jobs: user_id={user_id} → {count} jobs")
        return has
    except Exception as e:
        logger.error(f"[db] user_has_jobs failed for user_id={user_id}: {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
#  Chat History
# ─────────────────────────────────────────────────────────────────────────────

async def save_chat_message(user_id: int, role: str, content: str) -> bool:
    
    try:
        _, _, container_chat = _get_containers()
        await asyncio.to_thread(container_chat.create_item, body={
            "id": str(uuid.uuid4()), "user_id": user_id, "role": role, "content": content
        })
        logger.info(f"[db] save_chat_message OK — user_id={user_id} role={role}")
        return True
    except Exception as e:
        logger.error(f"[db] save_chat_message failed for user_id={user_id}: {e}")
        return False


async def load_chat_history(user_id: int, limit: int = 50) -> list[dict]:
    
    try:
        _, _, container_chat = _get_containers()
        rows = list(await asyncio.to_thread(lambda: list(container_chat.query_items(
            query=f"SELECT c.role, c.content FROM c WHERE c.user_id=@uid OFFSET 0 LIMIT {int(limit)}",
            parameters=[{"name":"@uid","value":user_id}],
            enable_cross_partition_query=True
        ))))
        result = [{"role": r["role"], "content": r["content"]} for r in rows]
        logger.info(f"[db] load_chat_history: {len(result)} messages for user_id={user_id}")
        return result
    except Exception as e:
        logger.error(f"[db] load_chat_history failed for user_id={user_id}: {e}")
        return []