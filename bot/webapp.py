"""Lightweight public leaderboard dashboard served by the bot process.

No extra web-framework dependency is required. A standard-library HTTP server
runs in a daemon thread while an asyncio task refreshes a read-only in-memory
snapshot from PostgreSQL.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import os
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from bot import db

logger = logging.getLogger(__name__)
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
PERIODS = ("day", "month", "all")

_snapshot_lock = threading.RLock()
_snapshot: dict = {
    "updated_at": None,
    "periods": {"day": [], "month": [], "all": []},
    "summary": {"total_calls": 0, "cumulative_ath_pnl_percent": 0.0, "average_ath_profit_percent": 0.0},
    "ready": False,
    "error": None,
}
_http_server: ThreadingHTTPServer | None = None


def public_base_url() -> str | None:
    raw = os.getenv("DASHBOARD_BASE_URL", "").strip()
    if not raw:
        raw = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw.rstrip("/")


def dashboard_url(period: str = "all") -> str | None:
    if period not in PERIODS:
        period = "all"
    base = public_base_url()
    return f"{base}/leaders/{period}" if base else None


def export_url(period: str = "all") -> str | None:
    if period not in PERIODS:
        period = "all"
    base = public_base_url()
    return f"{base}/export/{period}.csv" if base else None


def _channel_username() -> str | None:
    raw = os.getenv("CHANNEL_USERNAME", "").strip()
    if raw:
        return raw.replace("https://t.me/", "").replace("t.me/", "").lstrip("@").strip("/")
    channel = os.getenv("CHANNEL_ID", "").strip()
    return channel[1:] if channel.startswith("@") else None


def _fmt_usd(value) -> str:
    if value is None:
        return "לא זמין"
    value = float(value)
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.10f}".rstrip("0").rstrip(".")


def _format_time(value) -> str:
    if not value:
        return "לא זמין"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ISRAEL_TZ).strftime("%d/%m/%Y • %H:%M")


def _profit(row: dict) -> tuple[float, float, float]:
    entry = float(row.get("entry_valuation") or 0.0)
    ath = float(row.get("highest_valuation") or entry or 0.0)
    multiple = ath / entry if entry > 0 else 0.0
    computed = ((multiple - 1.0) * 100.0) if entry > 0 else 0.0
    stored = float(row.get("highest_profit_percent") or 0.0)
    return ath, max(computed, stored), multiple


def _badge(profit: float) -> str:
    if profit >= 1000:
        return "💎"
    if profit >= 500:
        return "🟣"
    if profit >= 300:
        return "🔥"
    if profit >= 100:
        return "🟢"
    if profit >= 50:
        return "🟡"
    return "🔵"


def _serializable_row(row) -> dict:
    d = dict(row)
    for key, value in list(d.items()):
        if isinstance(value, datetime):
            d[key] = value.isoformat()
    return d


async def refresh_snapshot(pool) -> None:
    periods = {}
    for period in PERIODS:
        rows = await db.get_calls_for_period(pool, period)
        periods[period] = [_serializable_row(row) for row in rows]
    summary_record = await db.get_all_time_performance_summary(pool)
    summary = dict(summary_record) if summary_record else {}
    snap = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "periods": periods,
        "summary": {
            "total_calls": int(summary.get("total_calls") or 0),
            "cumulative_ath_pnl_percent": float(summary.get("cumulative_ath_pnl_percent") or 0.0),
            "average_ath_profit_percent": float(summary.get("average_ath_profit_percent") or 0.0),
        },
        "ready": True,
        "error": None,
    }
    with _snapshot_lock:
        global _snapshot
        _snapshot = snap


async def dashboard_snapshot_loop(application) -> None:
    interval = max(5, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "15")))
    await asyncio.sleep(3)
    while True:
        try:
            pool = application.bot_data.get("db")
            if pool is not None:
                await refresh_snapshot(pool)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Dashboard snapshot refresh failed")
            with _snapshot_lock:
                _snapshot["error"] = str(exc)
        await asyncio.sleep(interval)


def _get_snapshot() -> dict:
    with _snapshot_lock:
        return {
            "updated_at": _snapshot.get("updated_at"),
            "periods": {k: list(v) for k, v in _snapshot.get("periods", {}).items()},
            "summary": dict(_snapshot.get("summary", {})),
            "ready": bool(_snapshot.get("ready")),
            "error": _snapshot.get("error"),
        }


def _period_label(period: str) -> tuple[str, str]:
    return {
        "day": ("🏆 מובילים יומיים", "24 השעות האחרונות"),
        "month": ("🗓 מובילים חודשיים", "30 הימים האחרונים"),
        "all": ("🌟 כל הזמנים", "כל ההמלצות מאז תחילת המעקב"),
    }[period]


def _original_call_url(row: dict) -> str | None:
    username = _channel_username()
    message_id = row.get("telegram_message_id")
    if username and message_id:
        return f"https://t.me/{username}/{int(message_id)}"
    return None


def _card_html(index: int, row: dict) -> str:
    symbol = html.escape(str(row.get("symbol") or "?"))
    name = html.escape(str(row.get("token_name") or ""))
    ca = html.escape(str(row.get("token_address") or ""))
    entry = float(row.get("entry_valuation") or 0.0)
    ath, profit, multiple = _profit(row)
    metric = "MC" if row.get("valuation_type") == "marketCap" else "FDV"
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"#{index}")
    badge = _badge(profit)
    original = _original_call_url(row)
    dex = row.get("current_dexscreener_url") or row.get("dexscreener_url")

    actions = []
    if original:
        actions.append(f'<a class="btn secondary" href="{html.escape(original, quote=True)}">↩️ ל־Call המקורי</a>')
    if dex:
        actions.append(f'<a class="btn" href="{html.escape(str(dex), quote=True)}">📈 DexScreener</a>')

    return f"""
    <article class="call-card" data-search="{symbol.lower()} {name.lower()} {ca.lower()}">
      <div class="rank">{medal}</div>
      <div class="card-main">
        <div class="token-line"><span class="symbol">${symbol}</span><span class="name">{name}</span></div>
        <div class="performance"><span class="badge">{badge}</span><strong>{profit:+,.1f}%</strong><span>•</span><strong>{multiple:.2f}X</strong></div>
        <div class="metrics">
          <div><span>🏆 ATH</span><strong>{_fmt_usd(ath)}</strong></div>
          <div><span>🎯 {metric} בזמן ההמלצה</span><strong>{_fmt_usd(entry)}</strong></div>
          <div><span>🕒 פורסם</span><strong>{_format_time(row.get('call_time'))}</strong></div>
        </div>
        <div class="ca">CA: <code>{ca}</code></div>
        <div class="actions">{''.join(actions)}</div>
      </div>
    </article>
    """


def _dashboard_html(period: str) -> bytes:
    snap = _get_snapshot()
    title, subtitle = _period_label(period)
    rows = snap.get("periods", {}).get(period, [])
    updated = _format_time(snap.get("updated_at")) if snap.get("updated_at") else "ממתין לנתונים"
    summary = snap.get("summary", {})

    tabs = "".join(
        f'<a class="tab {"active" if p == period else ""}" href="/leaders/{p}">{_period_label(p)[0]}</a>'
        for p in PERIODS
    )

    if period == "all":
        summary_html = f"""
        <section class="summary-grid">
          <div class="summary-box"><span>סה״כ המלצות</span><strong>{int(summary.get('total_calls') or 0):,}</strong></div>
          <div class="summary-box"><span>PNL מצטבר לפי ATH</span><strong>{float(summary.get('cumulative_ath_pnl_percent') or 0):+,.1f}%</strong></div>
          <div class="summary-box"><span>רווח ATH ממוצע</span><strong>{float(summary.get('average_ath_profit_percent') or 0):+,.1f}%</strong></div>
        </section>
        <p class="small-note">ℹ️ PNL מצטבר הוא סכום אחוזי ה־ATH של ה־Calls ואינו תשואת תיק השקעות בפועל.</p>
        """
    else:
        summary_html = f'<section class="summary-grid one"><div class="summary-box"><span>מספר המלצות בתקופה</span><strong>{len(rows):,}</strong></div></section>'

    cards = "".join(_card_html(i, row) for i, row in enumerate(rows, start=1))
    if not cards:
        cards = '<div class="empty">עדיין אין המלצות בתקופה הזאת.</div>'

    export = f"/export/{period}.csv"
    ready_text = "🟢 LIVE" if snap.get("ready") else "🟡 מתחבר לנתונים"

    page = f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1220">
<title>{html.escape(title)} – Crypto Calls</title>
<style>
:root{{--bg:#07111f;--panel:#0d1b2d;--panel2:#12243a;--text:#f8fafc;--muted:#9fb0c5;--line:#203750;--accent:#38bdf8;--good:#22c55e;--gold:#fbbf24;}}
*{{box-sizing:border-box}} body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;background:linear-gradient(180deg,#07111f,#0a1626 45%,#07111f);color:var(--text)}}
.wrap{{max-width:920px;margin:auto;padding:18px 14px 56px}} .hero{{padding:22px 18px 14px;text-align:center}} .hero h1{{margin:0;font-size:28px}} .subtitle{{color:var(--muted);margin-top:7px}} .live{{display:inline-block;margin-top:12px;padding:6px 11px;border-radius:999px;background:#0f2f23;color:#8ef0b0;font-weight:800;font-size:13px}}
.tabs{{display:flex;gap:8px;overflow:auto;padding:6px 0 14px;position:sticky;top:0;background:rgba(7,17,31,.94);backdrop-filter:blur(10px);z-index:5}} .tab{{flex:1;min-width:145px;text-align:center;text-decoration:none;color:var(--muted);background:var(--panel);border:1px solid var(--line);padding:11px;border-radius:14px;font-weight:700}} .tab.active{{color:white;border-color:var(--accent);box-shadow:0 0 0 1px rgba(56,189,248,.25) inset}}
.tools{{display:flex;gap:8px;margin:4px 0 14px;flex-wrap:wrap}} .search{{flex:1;min-width:220px;background:var(--panel);border:1px solid var(--line);color:white;padding:12px 14px;border-radius:12px;font-size:15px}} .download{{text-decoration:none;background:#12324b;color:#bfe7ff;border:1px solid #245a7d;padding:12px 14px;border-radius:12px;font-weight:700}}
.summary-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px}} .summary-grid.one{{grid-template-columns:1fr}} .summary-box{{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:16px;padding:14px;text-align:center}} .summary-box span{{display:block;color:var(--muted);font-size:13px;margin-bottom:5px}} .summary-box strong{{font-size:21px}} .small-note{{color:var(--muted);font-size:12px;line-height:1.5;margin:7px 2px 14px}}
.call-card{{display:flex;gap:12px;background:linear-gradient(180deg,#102238,#0c1a2b);border:1px solid var(--line);border-radius:18px;padding:15px;margin:11px 0;box-shadow:0 8px 28px rgba(0,0,0,.18)}} .rank{{font-size:26px;min-width:42px;text-align:center;padding-top:3px}} .card-main{{flex:1;min-width:0}} .token-line{{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}} .symbol{{font-size:22px;font-weight:900;color:#e2f5ff}} .name{{color:var(--muted)}} .performance{{margin:8px 0 12px;font-size:21px;display:flex;gap:8px;align-items:center;color:#7ef29e}} .performance .badge{{font-size:20px}} .metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}} .metrics div{{background:#081522;border:1px solid #1d3349;border-radius:12px;padding:10px}} .metrics span{{display:block;color:var(--muted);font-size:12px;margin-bottom:3px}} .metrics strong{{font-size:15px}} .ca{{margin-top:9px;color:#7890aa;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}} code{{direction:ltr;unicode-bidi:embed}} .actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:11px}} .btn{{text-decoration:none;background:#0e7490;color:white;padding:9px 12px;border-radius:10px;font-size:13px;font-weight:800}} .btn.secondary{{background:#24364a;color:#dce9f7}}
.footer{{margin-top:22px;text-align:center;color:var(--muted);font-size:12px;line-height:1.6}} .empty{{background:var(--panel);padding:30px;text-align:center;border-radius:16px;color:var(--muted)}}
@media(max-width:650px){{.hero h1{{font-size:24px}} .summary-grid{{grid-template-columns:1fr}} .metrics{{grid-template-columns:1fr}} .call-card{{padding:13px 11px}} .rank{{min-width:32px;font-size:22px}} .performance{{font-size:19px}}}}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero"><h1>{html.escape(title)}</h1><div class="subtitle">{html.escape(subtitle)}</div><div class="live">{ready_text} • עודכן {html.escape(updated)}</div></header>
  <nav class="tabs">{tabs}</nav>
  <div class="tools"><input id="search" class="search" placeholder="🔎 חיפוש לפי מטבע, סימול או CA"><a class="download" href="{export}">📥 הורדת CSV</a></div>
  {summary_html}
  <main id="cards">{cards}</main>
  <footer class="footer">🔄 הנתונים משתנים בזמן אמת ומתעדכנים לפי נתוני שוק אמיתיים.<br>ATH משקף את השיא שנשמר במערכת מאז פרסום ה־Call.</footer>
</div>
<script>
const q=document.getElementById('search');
q.addEventListener('input',()=>{{const s=q.value.trim().toLowerCase();document.querySelectorAll('.call-card').forEach(c=>{{c.style.display=!s||c.dataset.search.includes(s)?'flex':'none'}})}});
setTimeout(()=>location.reload(),30000);
</script>
</body></html>"""
    return page.encode("utf-8")


def _csv_bytes(period: str) -> bytes:
    snap = _get_snapshot()
    rows = snap.get("periods", {}).get(period, [])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Rank", "Symbol", "Token Name", "CA", "Call Date/Time (Israel)",
        "Entry Metric", "Entry MC/FDV", "ATH", "ATH Profit %", "ATH X",
        "DexScreener", "Original Call",
    ])
    for index, row in enumerate(rows, start=1):
        ath, profit, multiple = _profit(row)
        writer.writerow([
            index,
            row.get("symbol") or "",
            row.get("token_name") or "",
            row.get("token_address") or "",
            _format_time(row.get("call_time")),
            "MC" if row.get("valuation_type") == "marketCap" else "FDV",
            float(row.get("entry_valuation") or 0.0),
            ath,
            round(profit, 4),
            round(multiple, 6),
            row.get("current_dexscreener_url") or row.get("dexscreener_url") or "",
            _original_call_url(row) or "",
        ])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CallsDashboard/1.0"

    def log_message(self, fmt, *args):
        logger.info("dashboard: " + fmt, *args)

    def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._send(b"ok", "text/plain; charset=utf-8")
            return
        if path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/leaders/all")
            self.end_headers()
            return
        if path.startswith("/leaders/"):
            period = path.split("/")[-1]
            if period in PERIODS:
                self._send(_dashboard_html(period), "text/html; charset=utf-8")
                return
        if path.startswith("/export/") and path.endswith(".csv"):
            period = path.split("/")[-1][:-4]
            if period in PERIODS:
                body = _csv_bytes(period)
                self._send(
                    body,
                    "text/csv; charset=utf-8",
                    extra_headers={"Content-Disposition": f'attachment; filename="calls_{period}.csv"'},
                )
                return
        if path == "/api/snapshot":
            body = json.dumps(_get_snapshot(), ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
            return
        self._send(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)


def start_http_server() -> ThreadingHTTPServer:
    global _http_server
    if _http_server is not None:
        return _http_server
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, name="dashboard-http", daemon=True)
    thread.start()
    _http_server = server
    logger.info("Dashboard HTTP server listening on port %s", port)
    return server
