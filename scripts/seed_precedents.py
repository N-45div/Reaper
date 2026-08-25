"""Seed the BigQuery precedent store from the fixture corpus and the ledger.

Run:
  python scripts/seed_precedents.py --create --from-fixtures --replace
  python scripts/seed_precedents.py --from-ledger
  python scripts/seed_precedents.py --touch
  python scripts/seed_precedents.py --verify --query "ninety (90) days prior to expiration"
  python scripts/seed_precedents.py --from-fixtures --dry-run --out rows.ndjson

The sandbox forbids DML and streaming inserts, so every write here is a load
job. Fixture rows re-derive their gate fields by calling the REAL date engine
and delivery classifier — the corpus cannot drift from the engine, and every
row's resolution text says plainly that it is corpus, not customer history.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("REAPER_PRECEDENT", "bigquery")

from reaper import delivery, precedent  # noqa: E402
from reaper.config import REPO_ROOT  # noqa: E402
from reaper.date_engine import derive_deadline  # noqa: E402
from datetime import date  # noqa: E402

CORPUS = REPO_ROOT / "data" / "precedents" / "seed_clauses.json"


def _fixture_rows() -> list[dict]:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    rows = []
    for item in corpus:
        clause = item["clause_text"]
        term_end = date.fromisoformat(item["term_end"])
        d = derive_deadline(clause, term_end)
        ruling = delivery.classify(clause)
        verdict = "AMBIGUOUS" if d.status == "AMBIGUOUS" else "MATCH"
        rows.append(precedent.build_row(
            clause_id=precedent.clause_id(item["vendor"], clause, "fixture"),
            source="fixture",
            vendor=item["vendor"],
            clause_text=clause,
            clause_sha256=None,
            notice_period_value=d.period_value,
            notice_period_unit=d.period_unit,
            anchor=d.anchor,
            gate_verdict=verdict,
            gate_reason="; ".join(d.reasons)[:200] if d.reasons else None,
            engine_deadline=d.deadline,
            term_end=term_end,
            notice_method=ruling.method,
            email_compliant=ruling.email_compliant,
            status_final=item.get("status_final"),
            notice_served=item.get("notice_served"),
            billing_stopped=item.get("billing_stopped"),
            dispute_opened=item.get("dispute_opened"),
            resolution=item["resolution"],
        ))
    return rows


def _ledger_rows() -> list[dict]:
    from reaper import ledger  # imported late: honours REAPER_LEDGER

    terminal = {"BLOCKED", "VERIFIED", "REFUTED", "DISPUTED"}
    rows = []
    for ob in ledger.list_obligations():
        if ob.get("status") not in terminal:
            continue
        receipts = ledger.get_receipts(ob["id"])
        gated = next((r for r in receipts if r["kind"] == "GATED"), None)
        payload = {}
        if gated:
            payload = gated["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
        intact, _ = ledger.verify_chain(ob["id"])
        last = receipts[-1] if receipts else None
        rows.append(precedent.build_row(
            clause_id=precedent.clause_id(
                ob.get("vendor") or "", ob.get("clause_text") or "", "ledger"),
            source="ledger",
            obligation_id=ob["id"],
            vendor=ob.get("vendor"),
            clause_text=ob.get("clause_text"),
            gate_verdict=ob.get("gate_verdict"),
            gate_reason="; ".join(payload.get("reasons") or [])[:200] or None,
            llm_deadline=ob.get("llm_deadline"),
            engine_deadline=ob.get("engine_deadline"),
            term_end=ob.get("term_end"),
            notice_method=ob.get("notice_method"),
            email_compliant=payload.get("email_compliant"),
            notice_period_value=payload.get("notice_period_value"),
            notice_period_unit=payload.get("notice_period_unit"),
            anchor=payload.get("anchor"),
            status_final=ob.get("status"),
            notice_served=any(r["kind"] == "NOTICE_SENT" for r in receipts),
            dispute_opened=any(r["kind"] == "DISPUTE_OPENED" for r in receipts),
            billing_stopped=(ob.get("status") == "VERIFIED") or None,
            receipt_count=len(receipts),
            chain_intact=intact,
            terminal_receipt_hash=last["hash"] if last else None,
            resolution=f"ledger outcome: obligation {ob['id']} ended {ob.get('status')}",
        ))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--from-fixtures", action="store_true")
    ap.add_argument("--from-ledger", action="store_true")
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--touch", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--query", default="written notice of non-renewal ninety (90) days prior to the end of the term")
    ap.add_argument("--out", default="precedent_rows.ndjson")
    args = ap.parse_args()

    if args.create:
        from google.cloud import bigquery

        client = precedent._client()
        ds_id = f"{client.project}.{os.getenv('REAPER_BQ_DATASET', 'reaper_precedents')}"
        ds = bigquery.Dataset(ds_id)
        ds.location = os.getenv("REAPER_BQ_LOCATION", "US")
        client.create_dataset(ds, exists_ok=True)
        schema = [bigquery.SchemaField(c["name"], c["type"], mode=c["mode"])
                  for c in json.loads((REPO_ROOT / "data" / "precedents" / "schema.json").read_text())]
        table = bigquery.Table(precedent._table_id(), schema=schema)
        client.create_table(table, exists_ok=True)
        print(f"dataset+table ready: {precedent._table_id()}")

    rows: list[dict] = []
    if args.from_fixtures or (not args.from_ledger and not args.touch and not args.verify):
        rows += _fixture_rows()
    if args.from_ledger:
        rows += _ledger_rows()

    # dedupe on clause_id, last write wins
    dedup: dict[str, dict] = {}
    for r in rows:
        dedup[r["clause_id"]] = r
    rows = list(dedup.values())

    if rows and args.dry_run:
        out = Path(args.out)
        out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        print(f"dry-run: wrote {len(rows)} rows to {out} (no embeddings, no upload)")
    elif rows:
        print(f"embedding {len(rows)} clauses with {precedent.EMBED_MODEL} "
              f"(dim {precedent.EMBED_DIM})...")
        vecs = precedent.embed_batch([r["clause_text"] or "" for r in rows])
        for r, v in zip(rows, vecs):
            r["embedding"] = v
        res = precedent.load_rows(rows, replace=args.replace)
        print(f"loaded {res['rows']} rows (job {res['job_id']}, "
              f"{'replace' if args.replace else 'append'})")
        print("expiry:", precedent.touch_expiry())

    if args.touch and not rows:
        print("expiry:", precedent.touch_expiry())

    if args.verify:
        result = precedent.recall(vendor="verify", clause_text=args.query)
        print(f"available={result['available']} rows_scanned={result.get('rows_scanned')} "
              f"latency={result.get('latency_ms')}ms")
        for m in result["matches"]:
            print(f"  {m['similarity']:.3f}  {m['vendor']:<22} gate={m['gate_verdict']:<9} "
                  f"{m.get('notice_period') or '-':<9} {m['clause_id']}")
        if result.get("warning"):
            print("  warning:", result["warning"])
        print("  summary:", result.get("summary"))

    status = precedent.table_status()
    print(f"table: rows={status['rows']} days_left={status['days_left']} "
          f"expires={status['expires']}")


if __name__ == "__main__":
    main()
