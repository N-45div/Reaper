"""Precedent memory: how clauses shaped like this one resolved before.

Retrieval over past obligations, embedded with gemini-embedding-001 and
matched by brute-force VECTOR_SEARCH in BigQuery. It is advisory and nothing
more: the lookup runs AFTER the deterministic date gate has already ruled, so
history can never move a verdict. Prior cases are context for the human-facing
report, not evidence.

Fails OPEN in every direction. If the table is missing, expired, unreachable,
unauthorised or simply switched off, recall() returns available=False with the
reason, the ledger records that precedent could not be read, and the
obligation is filed exactly as it would have been. A failure must never
become a contractual verdict.
"""

import hashlib
import io
import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import llm
from .config import (
    BQ_DATASET,
    BQ_LOCATION,
    BQ_TABLE,
    EMBED_DIM,
    EMBED_MAX_CHARS,
    EMBED_MODEL,
    EMBED_TASK,
    GCP_PROJECT,
    PRECEDENT_BACKEND,
    PRECEDENT_MAX_MATCHES,
    PRECEDENT_MIN_SIMILARITY,
    PRECEDENT_TIMEOUT_S,
    PRECEDENT_TOP_K,
    PRECEDENT_WARN_SIMILARITY,
    REPO_ROOT,
)

_SCHEMA_PATH = REPO_ROOT / "data" / "precedents" / "schema.json"

_bq = None


def enabled() -> bool:
    """Read at call time so tests can monkeypatch the module constant."""
    return PRECEDENT_BACKEND == "bigquery"


def _client():
    global _bq
    if _bq is None:
        from google.cloud import bigquery  # deferred: ~15s cold import

        _bq = bigquery.Client(project=GCP_PROJECT, location=BQ_LOCATION)
    return _bq


def _table_id() -> str:
    return f"{GCP_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"


# --------------------------------------------------------------------------
# embeddings

def embed_clause(text: str, *, task_type: str = EMBED_TASK, dim: int = EMBED_DIM) -> list[float]:
    """L2-normalised embedding of one clause. Raises on failure — callers wrap.

    Routed through llm.call so it inherits key rotation and backoff, and it
    spends the embedding quota, never the scarce generate quota. Text is
    truncated client-side because the API truncates silently past ~2048 tokens.
    """
    clipped = (text or "")[:EMBED_MAX_CHARS]

    def _do(c):
        from google.genai import types  # deferred

        resp = c.models.embed_content(
            model=EMBED_MODEL,
            contents=clipped,
            config=types.EmbedContentConfig(
                task_type=task_type, output_dimensionality=dim),
        )
        return list(resp.embeddings[0].values)

    vals = llm.call(_do)
    n = math.sqrt(sum(x * x for x in vals)) or 1.0
    return [x / n for x in vals]


def embed_batch(texts: list[str], *, task_type: str = EMBED_TASK,
                dim: int = EMBED_DIM) -> list[list[float]]:
    """Embed up to 100 clauses in one request (seed-time only)."""
    clipped = [(t or "")[:EMBED_MAX_CHARS] for t in texts]

    def _do(c):
        from google.genai import types  # deferred

        resp = c.models.embed_content(
            model=EMBED_MODEL,
            contents=clipped,
            config=types.EmbedContentConfig(
                task_type=task_type, output_dimensionality=dim),
        )
        return [list(e.values) for e in resp.embeddings]

    out = []
    for vals in llm.call(_do):
        n = math.sqrt(sum(x * x for x in vals)) or 1.0
        out.append([x / n for x in vals])
    return out


# --------------------------------------------------------------------------
# recall

def _unavailable(reason: str) -> dict:
    return {
        "available": False, "reason": reason, "backend": "bigquery",
        "embedding_model": EMBED_MODEL, "embedding_dim": EMBED_DIM,
        "rows_scanned": None, "matches": [], "summary": None,
        "warning": None, "latency_ms": 0,
    }


def recall(*, vendor: str, clause_text: str, method: str | None = None,
           engine_deadline: str | None = None,
           top_k: int = PRECEDENT_TOP_K) -> dict:
    """Never raises. History is advisory; a lookup failure is a recorded miss."""
    t0 = time.monotonic()
    if not enabled():
        return _unavailable("disabled")
    if not (clause_text or "").strip():
        return _unavailable("no_clause")
    try:
        vec = embed_clause(clause_text)
    except Exception as exc:
        return _unavailable(type(exc).__name__)
    try:
        from google.cloud import bigquery  # deferred

        k = max(1, min(20, int(top_k) * 4))  # over-fetch, dedupe client-side
        sql = f"""
SELECT base.clause_id            AS clause_id,
       base.source               AS source,
       base.obligation_id        AS obligation_id,
       base.vendor               AS vendor,
       base.clause_text          AS clause_text,
       base.gate_verdict         AS gate_verdict,
       base.gate_reason          AS gate_reason,
       base.status_final         AS status_final,
       base.notice_period_value  AS notice_period_value,
       base.notice_period_unit   AS notice_period_unit,
       base.anchor               AS anchor,
       base.notice_method        AS notice_method,
       base.billing_stopped      AS billing_stopped,
       base.dispute_opened       AS dispute_opened,
       base.resolution           AS resolution,
       base.terminal_receipt_hash AS terminal_receipt_hash,
       distance
FROM VECTOR_SEARCH(
  TABLE `{_table_id()}`,
  'embedding',
  (SELECT @q AS embedding),
  top_k => {k},
  distance_type => 'COSINE')
ORDER BY distance
"""
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("q", "FLOAT64", vec)],
            use_query_cache=True,
        )
        rows = list(_client().query(
            sql, job_config=job_config, timeout=PRECEDENT_TIMEOUT_S,
        ).result(timeout=PRECEDENT_TIMEOUT_S))
    except Exception as exc:
        return _unavailable(type(exc).__name__)

    matches = []
    for r in rows:
        similarity = round(1.0 - float(r["distance"]), 4)
        period = None
        if r["notice_period_value"] and r["notice_period_unit"]:
            period = f"{int(r['notice_period_value'])} {r['notice_period_unit']}"
        matches.append({
            "clause_id": r["clause_id"],
            "source": r["source"],
            "obligation_id": r["obligation_id"],
            "vendor": r["vendor"] or "",
            "same_vendor": _norm(r["vendor"] or "") == _norm(vendor or ""),
            "clause_snippet": (r["clause_text"] or "")[:200],
            "gate_verdict": r["gate_verdict"] or "",
            "gate_reason": r["gate_reason"],
            "status_final": r["status_final"],
            "notice_period": period,
            "anchor": r["anchor"],
            "notice_method": r["notice_method"],
            "billing_stopped": r["billing_stopped"],
            "dispute_opened": r["dispute_opened"],
            "resolution": r["resolution"] or "",
            "terminal_receipt_hash": r["terminal_receipt_hash"],
            "similarity": float(similarity),
        })
    shaped = summarize(matches, vendor=vendor)
    return {
        "available": True, "reason": None, "backend": "bigquery",
        "embedding_model": EMBED_MODEL, "embedding_dim": EMBED_DIM,
        "rows_scanned": len(rows), "matches": shaped["matches"],
        "summary": shaped["summary"], "warning": shaped["warning"],
        "latency_ms": int((time.monotonic() - t0) * 1000),
    }


def summarize(matches: list[dict], *, vendor: str = "",
              min_similarity: float = PRECEDENT_MIN_SIMILARITY,
              warn_similarity: float = PRECEDENT_WARN_SIMILARITY,
              max_matches: int = PRECEDENT_MAX_MATCHES) -> dict:
    """Pure: threshold, dedupe, rank, and phrase. This is what the tests hit."""
    kept: dict[str, dict] = {}
    for m in matches:
        if float(m.get("similarity", 0.0)) < min_similarity:
            continue
        cid = m.get("clause_id") or ""
        if cid not in kept or m["similarity"] > kept[cid]["similarity"]:
            kept[cid] = m
    ranked = sorted(kept.values(), key=lambda m: -m["similarity"])[:max_matches]

    if not ranked:
        return {"matches": [], "summary": None, "warning": None}

    best = ranked[0]
    bits = [best["vendor"] or "unnamed vendor"]
    if best.get("notice_period"):
        bits.append(best["notice_period"])
    if best.get("gate_verdict"):
        bits.append(f"gate {best['gate_verdict']}")
    if best.get("billing_stopped") is True:
        bits.append("billing stopped")
    summary = (f"{len(ranked)} prior clause{'s' if len(ranked) != 1 else ''} "
               f"matched (best {best['similarity']:.2f}: {', '.join(bits)}). "
               "Advisory only.")[:240]

    warning = None
    if best["similarity"] >= warn_similarity:
        if best.get("gate_verdict") in ("MISMATCH", "AMBIGUOUS"):
            why = best.get("gate_reason") or "the two readings disagreed"
            warning = (f"a near-identical clause ({best['vendor']}, similarity "
                       f"{best['similarity']:.2f}) was BLOCKED before: {why}. "
                       "This is prior history, not a verdict.")
        elif best.get("status_final") == "REFUTED" or best.get("dispute_opened"):
            warning = (f"{best['vendor']} billed anyway after a near-identical "
                       f"clause (similarity {best['similarity']:.2f}) and the "
                       "obligation went to dispute. This is prior history, "
                       "not a verdict.")
    return {"matches": ranked, "summary": summary, "warning": warning}


# --------------------------------------------------------------------------
# rows (seed-time only)

def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _slug(vendor: str) -> str:
    return "".join(c for c in (vendor or "").replace(" ", "_") if c.isalnum() or c == "_")


def clause_id(vendor: str, clause_text: str, source: str) -> str:
    digest = hashlib.sha256(_norm(clause_text).encode()).hexdigest()[:12]
    return f"{source}:{_slug(vendor)}:{digest}"


def _schema_columns() -> list[dict]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def build_row(**fields) -> dict:
    """One NDJSON-ready dict, keys checked against schema.json at build time."""
    cols = {c["name"]: c for c in _schema_columns()}
    unknown = set(fields) - set(cols)
    if unknown:
        raise KeyError(f"not in precedent schema: {sorted(unknown)}")

    def _date(v):
        if v in (None, ""):
            return None
        if isinstance(v, date):
            return v.isoformat()
        return str(v)[:10]

    row = {}
    for name, col in cols.items():
        v = fields.get(name)
        if v is None:
            row[name] = None if col["mode"] != "REPEATED" else []
            continue
        t = col["type"]
        if t == "DATE":
            row[name] = _date(v)
        elif t == "TIMESTAMP":
            row[name] = v if isinstance(v, str) else v.isoformat()
        elif t == "FLOAT" and col["mode"] == "REPEATED":
            row[name] = [float(x) for x in v]
        elif t == "FLOAT":
            row[name] = float(v)
        elif t == "INTEGER":
            row[name] = int(v)
        elif t == "BOOLEAN":
            row[name] = bool(v)
        else:
            row[name] = str(v)
    if row.get("clause_text"):
        row["clause_text"] = row["clause_text"][:2000]
    if row.get("gate_reason"):
        row["gate_reason"] = row["gate_reason"][:200]
    row.setdefault("created_at", None)
    if not row["created_at"]:
        row["created_at"] = datetime.now(timezone.utc).isoformat()
    row["embedding_model"] = EMBED_MODEL
    row["embedding_dim"] = EMBED_DIM
    return row


def load_rows(rows: list[dict], *, replace: bool = False) -> dict:
    """The only write path (sandbox forbids DML and streaming; load jobs work)."""
    from google.cloud import bigquery  # deferred

    schema = [
        bigquery.SchemaField(c["name"], c["type"], mode=c["mode"])
        for c in _schema_columns()
    ]
    cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=schema,
        write_disposition=(bigquery.WriteDisposition.WRITE_TRUNCATE if replace
                           else bigquery.WriteDisposition.WRITE_APPEND),
    )
    buf = io.BytesIO(("".join(json.dumps(r) + "\n" for r in rows)).encode())
    job = _client().load_table_from_file(buf, _table_id(), job_config=cfg)
    job.result()
    return {"ok": True, "job_id": job.job_id, "rows": len(rows)}


def touch_expiry(days: int = 59) -> dict:
    """Roll the sandbox 60-day clock forward. Fails open."""
    try:
        table = _client().get_table(_table_id())
        table.expires = datetime.now(timezone.utc) + timedelta(days=days)
        _client().update_table(table, ["expires"])
        left = (table.expires - datetime.now(timezone.utc)).days
        return {"ok": True, "expires": table.expires.isoformat(), "days_left": left}
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__}


def table_status() -> dict:
    """Health snapshot for docs and the hosted endpoint. Fails open."""
    if not enabled():
        return {"available": False, "reason": "disabled",
                "rows": 0, "expires": None, "days_left": None}
    try:
        table = _client().get_table(_table_id())
        expires = table.expires.isoformat() if table.expires else None
        left = ((table.expires - datetime.now(timezone.utc)).days
                if table.expires else None)
        return {"available": True, "reason": None, "rows": int(table.num_rows),
                "expires": expires, "days_left": left}
    except Exception as exc:
        return {"available": False, "reason": type(exc).__name__,
                "rows": 0, "expires": None, "days_left": None}
