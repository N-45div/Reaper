"""Render the briefing as a printable slide deck in the ledger's own hand."""

from datetime import date
from html import escape

CSS = """
:root { --paper:#fbfaf7; --ink:#191917; --muted:#6e6b63; --hair:#e4e2da;
        --wax:#8c2318; --ledger:#1e5c3a; --amber:#8a6100; }
* { box-sizing:border-box; margin:0; }
body { background:#e8e6e0; font:15px/1.6 "Segoe UI",system-ui,sans-serif; color:var(--ink); padding:1px 0; }
.slide { width:1120px; height:630px; margin:26px auto; background:var(--paper);
         border-top:7px solid var(--ink); padding:52px 64px; position:relative;
         box-shadow:0 12px 40px rgba(25,25,23,.13); page-break-after:always; overflow:hidden; }
.kicker { font-size:11px; letter-spacing:3.4px; text-transform:uppercase; color:var(--muted); font-weight:700; }
h1 { font:600 58px/1.06 Georgia,serif; letter-spacing:-1.2px; margin:22px 0 20px; }
h2 { font:600 34px/1.2 Georgia,serif; margin-bottom:18px; }
.stand { font:400 23px/1.5 Georgia,serif; color:#3a3833; max-width:820px; }
.meta { position:absolute; left:64px; bottom:34px; font:600 10.5px/1.7 Consolas,monospace;
        letter-spacing:1.6px; color:var(--muted); text-transform:uppercase; }
.pno { position:absolute; right:64px; bottom:32px; font:600 12px Consolas,monospace; color:var(--muted); }
.tiles { display:grid; grid-template-columns:repeat(5,1fr); border:1px solid var(--hair); margin:26px 0; background:#fff; }
.tile { padding:18px 14px; border-left:1px solid var(--hair); text-align:center; }
.tile:first-child { border-left:0; }
.tile b { display:block; font:600 38px/1 Georgia,serif; }
.tile span { font-size:10.5px; letter-spacing:1.5px; text-transform:uppercase; color:var(--muted); }
.tile.wax b { color:var(--wax); } .tile.green b { color:var(--ledger); } .tile.amber b { color:var(--amber); }
p.body { font:400 18px/1.62 Georgia,serif; max-width:850px; color:#3a3833; }
table { width:100%; border-collapse:collapse; margin-top:10px; }
th { text-align:left; font-size:10px; letter-spacing:2px; text-transform:uppercase; color:var(--muted);
     border-bottom:2px solid var(--ink); padding:8px 10px; }
td { padding:10px; border-bottom:1px solid var(--hair); font-size:13.5px; vertical-align:top; }
td.v { font:600 16px Georgia,serif; white-space:nowrap; }
td.verdict { font-weight:700; white-space:nowrap; }
td.note { color:var(--muted); }
td.when { font-family:Consolas,monospace; font-size:12px; white-space:nowrap; }
.w-wax { color:var(--wax); } .w-green { color:var(--ledger); } .w-amber { color:var(--amber); }
ol.risks { margin:6px 0 0 22px; } ol.risks li { font:400 18px/1.5 Georgia,serif; margin-bottom:14px; max-width:870px; }
.rec { border-left:4px solid var(--wax); padding:14px 22px; margin-top:22px; background:#fff; }
.rec p { font:400 19px/1.5 Georgia,serif; }
.stamp { position:absolute; right:64px; top:64px; transform:rotate(-7deg); border:3px double var(--ledger);
         color:var(--ledger); font:800 13px/1 "Segoe UI",sans-serif; letter-spacing:2.6px;
         padding:9px 15px; text-transform:uppercase; }
.stamp.broken { border-color:var(--wax); color:var(--wax); }
.hint { position:fixed; right:18px; bottom:16px; background:#191917; color:#fbfaf7; font-size:12px;
        padding:8px 14px; border-radius:2px; opacity:.85; }
@media print { body { background:#fff; } .slide { margin:0; box-shadow:none; } .hint { display:none; } }
"""


def _timeline(facts: dict) -> str:
    rows = [r for r in facts["obligations"] if r.get("notice_deadline")]
    if not rows:
        return '<p class="body">No verified deadlines yet — nothing has passed the gate.</p>'
    today = date.fromisoformat(facts["as_of"])
    ends = [date.fromisoformat(r["term_end"]) for r in rows if r.get("term_end")]
    last = max(ends) if ends else today
    span = max((last - today).days, 1)
    width, left, right = 980, 170, 60
    axis = width - left - right
    height = 74 + len(rows) * 58

    def x(iso: str) -> float:
        frac = (date.fromisoformat(iso) - today).days / span
        return left + axis * max(0.0, min(1.0, frac))

    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{min(height, 380)}">',
           f'<line x1="{left}" y1="36" x2="{width - right}" y2="36" stroke="#e4e2da" stroke-width="2"/>',
           f'<line x1="{left}" y1="26" x2="{left}" y2="{height - 14}" stroke="#191917" stroke-width="1.5"/>',
           f'<text x="{left}" y="20" font-size="11" font-family="Consolas" fill="#6e6b63">TODAY · {facts["as_of"]}</text>',
           f'<text x="{width - right}" y="20" font-size="11" font-family="Consolas" fill="#6e6b63" text-anchor="end">{last.isoformat()}</text>']
    for i, r in enumerate(rows):
        y = 66 + i * 58
        nx = x(r["notice_deadline"])
        tx = x(r["term_end"]) if r.get("term_end") else nx
        colour = ("#8c2318" if r["status"] in ("BLOCKED", "DISPUTED", "REFUTED")
                  else "#1e5c3a" if r["status"] == "VERIFIED" else "#191917")
        days = r.get("days_to_notice")
        suffix = "" if days is None else (f" · {days}d" if days >= 0 else " · passed")
        label = f'notice {r["notice_deadline"]}' + suffix
        out += [
            f'<text x="{left - 14}" y="{y + 5}" font-size="13" font-family="Georgia" text-anchor="end" fill="#191917">{escape(r["vendor"][:24])}</text>',
            f'<line x1="{nx}" y1="{y}" x2="{tx}" y2="{y}" stroke="{colour}" stroke-width="2" opacity=".33"/>',
            f'<circle cx="{nx}" cy="{y}" r="6" fill="{colour}"/>',
            f'<rect x="{tx - 3}" y="{y - 8}" width="6" height="16" fill="{colour}" opacity=".5"/>',
            f'<text x="{nx}" y="{y - 14}" font-size="10.5" font-family="Consolas" fill="{colour}" text-anchor="middle">{label}</text>',
        ]
    out.append("</svg>")
    return "".join(out)


def _verdict_class(status: str) -> str:
    if status in ("BLOCKED", "DISPUTED", "REFUTED"):
        return "w-wax"
    if status == "VERIFIED":
        return "w-green"
    if status == "AWAITING_APPROVAL":
        return "w-amber"
    return ""


def render(facts: dict, story: dict) -> str:
    notes = {o["id"]: o for o in story.get("obligations", [])}
    counter = {"n": 0}

    def foot() -> str:
        counter["n"] += 1
        return (f'<div class="meta">Reaper · renewal exposure briefing · {facts["as_of"]}</div>'
                f'<div class="pno">{counter["n"]:02d}</div>')

    row_html = []
    for r in facts["obligations"]:
        note = notes.get(r["id"], {})
        verdict = note.get("verdict") or r["status"].replace("_", " ").lower()
        when = r.get("notice_deadline") or "unverified"
        days = r.get("days_to_notice")
        if days is None:
            when_html = escape(when)
        elif days >= 0:
            when_html = escape(when) + f"<br>in {days} days"
        else:
            when_html = escape(when) + f"<br>{abs(days)} days ago"
        row_html.append(
            f'<tr><td class="v">{escape(r["vendor"])}</td>'
            f'<td class="verdict {_verdict_class(r["status"])}">{escape(verdict)}</td>'
            f'<td class="when">{when_html}</td>'
            f'<td class="note">{escape(note.get("note", ""))}</td></tr>')

    risks = "".join(f"<li>{escape(x)}</li>" for x in story.get("risks", []))
    chain_ok = facts["all_chains_intact"]
    stamp_word = "Chain intact" if chain_ok else "Chain broken"
    chain_tail = ("" if chain_ok else
                  " — except one, which is why this deck is stamped broken")

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Reaper — renewal exposure briefing</title><style>{CSS}</style></head><body>

<section class="slide">
  <div class="kicker">Reaper · autonomous renewal agent · standing briefing</div>
  <h1>{escape(story.get("headline", "Portfolio briefing"))}</h1>
  <p class="stand">{escape(story.get("standfirst", ""))}</p>
  {foot()}
</section>

<section class="slide">
  <div class="kicker">Where the portfolio stands</div>
  <h2>Situation</h2>
  <p class="body">{escape(story.get("situation", ""))}</p>
  <div class="tiles">
    <div class="tile"><b>{facts["count"]}</b><span>under watch</span></div>
    <div class="tile amber"><b>{facts["awaiting"]}</b><span>awaiting signature</span></div>
    <div class="tile wax"><b>{facts["blocked"]}</b><span>blocked by the gate</span></div>
    <div class="tile wax"><b>{facts["disputed"]}</b><span>in dispute</span></div>
    <div class="tile green"><b>{facts["verified"]}</b><span>verified closed</span></div>
  </div>
  <p class="body" style="font-size:15.5px;color:#6e6b63">Every figure above is counted from the
  evidence chain, not written by a model.</p>
  {foot()}
</section>

<section class="slide">
  <div class="kicker">Deadlines ahead</div>
  <h2>What falls due, and when</h2>
  {_timeline(facts)}
  {foot()}
</section>

<section class="slide">
  <div class="kicker">Obligation by obligation</div>
  <h2>The register</h2>
  <table><tr><th>Vendor</th><th>Verdict</th><th>Notice due</th><th>What it means</th></tr>
  {"".join(row_html) or '<tr><td colspan="4">Nothing filed yet.</td></tr>'}</table>
  {foot()}
</section>

<section class="slide">
  <div class="kicker">Exposure</div>
  <h2>Risks</h2>
  <ol class="risks">{risks or "<li>No material risks identified this period.</li>"}</ol>
  <div class="rec"><p>{escape(story.get("recommendation", ""))}</p></div>
  {foot()}
</section>

<section class="slide">
  <div class="kicker">Provenance</div>
  <h2>This briefing can be audited</h2>
  <div class="stamp {"" if chain_ok else "broken"}">{stamp_word}</div>
  <p class="body">Every statement in this deck traces to a hash-chained record in the obligations
  ledger: the clause as it was read, both derivations of each deadline, the gate verdict, the notice
  delivered, and the invoice document checked against it. Recomputing the chain from its genesis
  record reproduces every hash{chain_tail}.</p>
  <p class="body" style="font-size:15.5px;color:#6e6b63;margin-top:22px">Narrative written by Gemini
  from the ledger. Counts, dates and chain verification computed deterministically in code.</p>
  {foot()}
</section>

<div class="hint">← → to page · Ctrl/Cmd-P to print</div>
<script>
const slides = [...document.querySelectorAll(".slide")];
let idx = 0;
function go(n) {{
  idx = Math.max(0, Math.min(slides.length - 1, n));
  slides[idx].scrollIntoView({{ behavior: "smooth", block: "center" }});
}}
addEventListener("keydown", e => {{
  if (["ArrowRight", "PageDown", " "].includes(e.key)) {{ e.preventDefault(); go(idx + 1); }}
  if (["ArrowLeft", "PageUp"].includes(e.key)) {{ e.preventDefault(); go(idx - 1); }}
}});
</script>
</body></html>"""
