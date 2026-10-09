"""Exportação do diagnóstico de saúde para um relatório HTML."""
from __future__ import annotations

from html import escape
from pathlib import Path

from . import __version__
from .health import CRIT, INFO, OK, UNKNOWN, WARN, HealthReport
from .utils import app_data_dir

STATUS_CLASS = {OK: "ok", WARN: "warn", CRIT: "crit", INFO: "info", UNKNOWN: "unknown"}

CSS = """
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#5f6b7a; --line:#e3e6ea;
  --ok:#1e8e3e; --warn:#b26a00; --crit:#c5221f; --info:#1a6dd4; --unknown:#6b7280; }
@media (prefers-color-scheme: dark) { :root { --bg:#14171c; --card:#1d2128; --text:#e6e9ee; --muted:#9aa4b2;
  --line:#2c323b; --ok:#5bc27a; --warn:#f0a43a; --crit:#f2716c; --info:#6aa7f5; --unknown:#9aa4b2; } }
* { box-sizing:border-box; }
body { margin:0; font-family:"Segoe UI",system-ui,sans-serif; background:var(--bg); color:var(--text); }
main { max-width:1000px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 10px; }
.muted { color:var(--muted); font-size:13px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }
.score { display:flex; align-items:center; gap:20px; margin-top:16px; }
.score b { font-size:44px; line-height:1; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:6px 16px; margin:0; font-size:14px; }
dt { color:var(--muted); }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; }
.badge { font-weight:600; white-space:nowrap; }
.ok { color:var(--ok); } .warn { color:var(--warn); } .crit { color:var(--crit); }
.info { color:var(--info); } .unknown { color:var(--unknown); }
@media (max-width:640px) { td:nth-child(1), th:nth-child(1) { display:none; } }
"""


def render_html(report: HealthReport) -> str:
    score_class = "ok" if report.score >= 80 else "warn" if report.score >= 50 else "crit"
    summary = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in report.summary.items())
    rows = "".join(
        f"<tr><td>{escape(i.component)}</td><td>{escape(i.name)}</td>"
        f"<td class='badge {STATUS_CLASS.get(i.status, 'unknown')}'>{escape(i.status)}</td>"
        f"<td>{escape(i.detail)}</td></tr>"
        for i in report.items
    )
    admin_note = "" if report.admin else (
        "<p class='muted'>Executado sem privilégios de administrador: alguns sensores (temperatura, desgaste "
        "do SSD, capacidade da bateria) não puderam ser lidos.</p>")
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Relatório DiagCompleto</title><style>{CSS}</style></head>
<body><main>
<h1>Relatório de saúde do computador</h1>
<div class="muted">Gerado em {report.collected_at:%d/%m/%Y %H:%M} · DiagCompleto {__version__}</div>
<div class="card score"><b class="{score_class}">{report.score}</b>
<div>Pontuação de saúde (0–100)<br><span class="muted">{report.count(CRIT)} crítico(s) · {report.count(WARN)} alerta(s)</span></div></div>
{admin_note}
<h2>Sistema</h2><div class="card"><dl>{summary}</dl></div>
<h2>Componentes</h2><div class="card"><table>
<thead><tr><th>Componente</th><th>Item</th><th>Status</th><th>Detalhes</th></tr></thead>
<tbody>{rows}</tbody></table></div>
</main></body></html>"""


def save_report(report: HealthReport, folder: Path | None = None) -> Path:
    folder = folder or app_data_dir() / "relatorios"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"relatorio_{report.collected_at:%Y%m%d_%H%M%S}.html"
    path.write_text(render_html(report), encoding="utf-8")
    return path
