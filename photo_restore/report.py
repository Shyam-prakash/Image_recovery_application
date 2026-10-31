"""report.csv / report.json / index.html from the per-image result records."""
import csv
import html
import json
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from .config import Paths

ORDER = ["needs_review", "failed", "repaired_cropped", "repaired", "unsupported", "clean", "other"]
COLORS = {"needs_review": "#d97706", "failed": "#dc2626", "repaired_cropped": "#7c3aed",
          "repaired": "#16a34a", "unsupported": "#6b7280", "clean": "#0284c7", "other": "#6b7280"}
CSV_FIELDS = ["path", "status", "message", "orig_size", "out_size", "orig_bytes", "damage_points",
              "segments", "filled_pct", "rows_cropped", "confidence", "false_positives",
              "sampling", "time_s", "out_path"]


def load_results(paths: Paths):
    recs = []
    for f in sorted(paths.results.rglob("*.json")):
        try:
            recs.append(json.loads(f.read_text()))
        except Exception:
            continue
    # drop records whose source no longer exists
    recs = [r for r in recs if (paths.extracted / r["path"]).exists()]
    recs.sort(key=lambda r: (ORDER.index(r["status"]) if r.get("status") in ORDER else 99,
                             r.get("confidence") if r.get("confidence") is not None else 1, r["path"]))
    return recs


def _seg_str(segs):
    return " | ".join(f"[{s['decoded_start']},{s['decoded_end']}) s={s['shift']:+d} off=({','.join(str(x) for x in s['offsets'])})"
                      for s in segs)


def write_csv(recs, dest: Path):
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in recs:
            row = dict(r)
            row["segments"] = _seg_str(r.get("segments") or [])
            for k in ("orig_size", "out_size"):
                if row.get(k):
                    row[k] = "x".join(str(x) for x in row[k])
            w.writerow(row)


def _u(p):
    return quote(str(p).replace("\\", "/"))


def write_html(recs, paths: Paths, dest: Path):
    counts = Counter(r.get("status") for r in recs)
    cards = []
    for r in recs:
        st = r.get("status") or "failed"
        rel = r["path"]
        before = f"thumbs/{_u(rel)}.before.jpg"
        after = f"thumbs/{_u(rel)}.after.jpg"
        dbg = f"thumbs/{_u(rel)}.debug.jpg"
        has_dbg = (paths.thumbs / (rel + ".debug.jpg")).exists()
        has_before = (paths.thumbs / (rel + ".before.jpg")).exists()
        out_link = f"../../{_u(r['out_path'])}" if r.get("out_path") else ""
        dbg_link = f"../../{_u(r['debug_path'])}" if r.get("debug_path") else ""
        size = ""
        if r.get("orig_size"):
            size = "×".join(map(str, r["orig_size"]))
            if r.get("out_size") and r["out_size"] != r["orig_size"]:
                size += " → " + "×".join(map(str, r["out_size"]))
        segs = r.get("segments") or []
        seg_rows = "".join(
            f"<tr><td>{s['decoded_start']}–{s['decoded_end']}</td><td>{s['shift']:+d}</td>"
            f"<td>{', '.join(str(x) for x in s['offsets'])}</td><td>{html.escape(s['method'])}</td></tr>"
            for s in segs)
        seg_tbl = (f"<details><summary>{len(segs)} segments</summary><table><tr><th>decoded</th><th>shift</th>"
                   f"<th>DC offsets</th><th>method</th></tr>{seg_rows}</table></details>") if len(segs) > 1 else ""
        imgs = ""
        if has_before:
            imgs = (f'<div class="pair"><figure><img loading="lazy" src="{before}"><figcaption>before</figcaption></figure>'
                    f'<figure><img loading="lazy" src="{after}" data-a="{after}" data-d="{dbg if has_dbg else after}" '
                    f'class="after"><figcaption>after{" (click: show filled areas)" if has_dbg else ""}</figcaption></figure></div>')
        conf = r.get("confidence")
        conf_s = f"confidence {conf:.2f} · " if conf is not None else ""
        links = []
        if out_link:
            links.append(f'<a href="{out_link}">output</a>')
        if dbg_link:
            links.append(f'<a href="{dbg_link}">debug PNG</a>')
        cards.append(f"""
<div class="card" data-status="{st}" data-name="{html.escape(rel.lower())}">
 <div class="hd"><span class="badge" style="background:{COLORS.get(st, '#444')}">{st}</span>
 <span class="name" title="{html.escape(rel)}">{html.escape(Path(rel).name)}</span></div>
 {imgs}
 <div class="meta">{html.escape(size)} · {conf_s}filled {r.get('filled_pct', 0)}% · damage pts {r.get('damage_points', 0)} · {r.get('time_s', '')}s</div>
 <div class="msg">{html.escape(r.get('message') or '')}</div>
 <div class="dir">{html.escape(str(Path(rel).parent))}</div>
 {seg_tbl}
 <div class="links">{' · '.join(links)}</div>
</div>""")
    buttons = "".join(
        f'<button data-f="{s}" style="border-color:{COLORS[s]}">{s} ({counts.get(s, 0)})</button>'
        for s in ORDER if counts.get(s))
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Photo restore report</title>
<style>
body{{font:14px system-ui,sans-serif;margin:16px;background:#f4f4f5;color:#18181b}}
h1{{font-size:20px;margin:0 0 8px}}
.bar{{position:sticky;top:0;background:#f4f4f5;padding:8px 0;z-index:2;display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
button{{border:2px solid #999;background:#fff;border-radius:14px;padding:4px 10px;cursor:pointer}}
button.on{{background:#18181b;color:#fff}}
input{{padding:5px 8px;border-radius:6px;border:1px solid #bbb;min-width:200px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:12px}}
.card{{background:#fff;border-radius:8px;padding:10px;box-shadow:0 1px 3px #0002;min-width:0}}
.hd{{display:flex;gap:8px;align-items:center;margin-bottom:6px}}
.badge{{color:#fff;border-radius:4px;padding:2px 6px;font-size:12px;white-space:nowrap}}
.name{{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.pair{{display:grid;grid-template-columns:1fr 1fr;gap:6px}}
figure{{margin:0}} figure img{{width:100%;height:200px;object-fit:contain;background:#222;border-radius:4px}}
figcaption{{font-size:11px;color:#666}}
.after{{cursor:pointer}}
.meta,.dir{{font-size:12px;color:#555;margin-top:4px}}
.msg{{font-size:12px;color:#b45309;margin-top:2px}}
table{{font-size:11px;border-collapse:collapse}} td,th{{padding:1px 6px;border-bottom:1px solid #eee;text-align:left}}
@media (max-width:520px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>Photo restore report — {len(recs)} files</h1>
<div class="bar"><button data-f="all" class="on">all</button>{buttons}
<input id="q" placeholder="filter by name…"></div>
<div class="grid">{''.join(cards)}</div>
<script>
let f='all';const q=document.getElementById('q');
function apply(){{const s=q.value.toLowerCase();document.querySelectorAll('.card').forEach(c=>{{
 c.style.display=((f==='all'||c.dataset.status===f)&&c.dataset.name.includes(s))?'':'none'}})}}
document.querySelectorAll('button').forEach(b=>b.onclick=()=>{{f=b.dataset.f;
 document.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));apply()}});
q.oninput=apply;
document.querySelectorAll('img.after').forEach(i=>i.onclick=()=>{{i.src=(i.getAttribute('src')===i.dataset.a)?i.dataset.d:i.dataset.a}});
</script></body></html>"""
    dest.write_text(page, encoding="utf-8")


def build(paths: Paths):
    paths.ensure()
    recs = load_results(paths)
    write_csv(recs, paths.report / "report.csv")
    (paths.report / "report.json").write_text(json.dumps(recs, indent=1, ensure_ascii=False), encoding="utf-8")
    write_html(recs, paths, paths.report / "index.html")
    return recs, Counter(r.get("status") for r in recs)
