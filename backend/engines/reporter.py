"""
Transfera v2 — Session Report Generator
Produces a JSON data file and a standalone HTML analytics report
for completed or failed transfer sessions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.config import EXPORT_DIR
from backend.database.manager import session_scope
from backend.database.models import MediaItem, TransferSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
async def _load_session_data(session_id: int) -> dict[str, Any]:
    """Load the session and all associated media items from the database."""
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            raise ValueError(f"Session {session_id} not found")

        result = await session.execute(
            select(MediaItem)
            .where(MediaItem.session_id == session_id)
            .order_by(MediaItem.id)
        )
        items = list(result.scalars().all())

    return {"session": ts, "items": items}


def _build_status_matrix(items: list[MediaItem]) -> dict[str, int]:
    """Count items per final_status value."""
    matrix: dict[str, int] = {}
    for item in items:
        matrix[item.final_status] = matrix.get(item.final_status, 0) + 1
    return matrix


def _compute_throughput(session: TransferSession) -> dict[str, Any]:
    """Compute bytes/sec and files/sec throughput metrics."""
    started = session.started_at
    completed = session.completed_at
    total_bytes = session.total_bytes_volume or 0

    if started and completed:
        elapsed = (completed - started).total_seconds()
    else:
        elapsed = 0.0

    bytes_per_sec = total_bytes / elapsed if elapsed > 0 else 0.0
    files_per_sec = session.completed_items / elapsed if elapsed > 0 else 0.0

    return {
        "elapsed_seconds": round(elapsed, 2),
        "bytes_per_second": round(bytes_per_sec, 2),
        "files_per_second": round(files_per_sec, 4),
    }


# ---------------------------------------------------------------------------
# JSON report
# ---------------------------------------------------------------------------
def _build_json_payload(session: TransferSession, items: list[MediaItem]) -> dict[str, Any]:
    """Assemble the structured JSON report payload."""
    status_matrix = _build_status_matrix(items)
    throughput = _compute_throughput(session)

    file_records: list[dict[str, Any]] = []
    for item in items:
        record: dict[str, Any] = {
            "id": item.id,
            "file_name": item.file_name,
            "source_path": item.source_path,
            "file_size": item.file_size,
            "mime_type": item.mime_type,
            "extension": item.extension,
            "hop1_status": item.hop1_status,
            "hop2_status": item.hop2_status,
            "final_status": item.final_status,
        }
        if item.source_hash:
            record["source_hash"] = item.source_hash
        if item.date_taken:
            record["date_taken"] = item.date_taken.isoformat()
        if item.date_source:
            record["date_source"] = item.date_source
        if item.error_message:
            record["error_message"] = item.error_message
        file_records.append(record)

    return {
        "report_generated_at": datetime.now(timezone.utc).isoformat(),
        "session": {
            "id": session.id,
            "session_name": session.session_name,
            "source_root": session.source_root,
            "dest_root": session.dest_root,
            "transfer_mode": session.transfer_mode,
            "status": session.status,
            "total_items": session.total_items,
            "completed_items": session.completed_items,
            "failed_items": session.failed_items,
            "total_bytes_volume": session.total_bytes_volume,
            "error_message": session.error_message,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "started_at": session.started_at.isoformat() if session.started_at else None,
            "completed_at": session.completed_at.isoformat() if session.completed_at else None,
        },
        "throughput": throughput,
        "status_matrix": status_matrix,
        "items": file_records,
    }


def _write_json(payload: dict[str, Any], output_dir: Path, session_id: int) -> Path:
    """Write the JSON report to disk and return its path."""
    json_path = output_dir / f"session-{session_id}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("JSON report written -> %s", json_path)
    return json_path


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Transfera Report — Session {session_id}</title>
<style>
  :root {{
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface-alt: #222632;
    --border: #2e3345;
    --text: #e4e6ed;
    --text-muted: #8b8fa3;
    --accent: #6c8cff;
    --success: #34d399;
    --warning: #fbbf24;
    --error: #f87171;
    --radius: 8px;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 2rem;
  }}
  .container {{ max-width: 960px; margin: 0 auto; }}
  h1 {{
    font-size: 1.5rem;
    font-weight: 600;
    margin-bottom: 0.25rem;
  }}
  .subtitle {{ color: var(--text-muted); font-size: 0.875rem; margin-bottom: 2rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem;
  }}
  .card-label {{ font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.25rem; }}
  .card-value {{ font-size: 1.5rem; font-weight: 700; }}
  .card-value.success {{ color: var(--success); }}
  .card-value.warning {{ color: var(--warning); }}
  .card-value.error {{ color: var(--error); }}
  .section {{ margin-bottom: 2rem; }}
  .section-title {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; }}
  .bar-chart {{ display: flex; gap: 2px; height: 32px; border-radius: var(--radius); overflow: hidden; margin-bottom: 0.5rem; }}
  .bar-segment {{ display: flex; align-items: center; justify-content: center; font-size: 0.7rem; font-weight: 600; color: #fff; min-width: 24px; }}
  .bar-completed {{ background: var(--success); }}
  .bar-failed {{ background: var(--error); }}
  .bar-other {{ background: var(--text-muted); }}
  .legend {{ display: flex; gap: 1.5rem; font-size: 0.8rem; color: var(--text-muted); }}
  .legend-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 0.35rem; vertical-align: middle; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 0.6rem 0.75rem; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--text-muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; }}
  tr:hover td {{ background: var(--surface-alt); }}
  .status-badge {{
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
  }}
  .status-completed {{ background: rgba(52,211,153,0.15); color: var(--success); }}
  .status-failed {{ background: rgba(248,113,113,0.15); color: var(--error); }}
  .status-other {{ background: rgba(139,143,163,0.15); color: var(--text-muted); }}
  .error-log {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1rem;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.8rem;
    color: var(--error);
    max-height: 260px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
  }}
  .error-log-empty {{ color: var(--text-muted); font-style: italic; font-family: inherit; }}
  .footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--border); font-size: 0.75rem; color: var(--text-muted); text-align: center; }}
</style>
</head>
<body>
<div class="container">
  <h1>Transfera Backup Report</h1>
  <p class="subtitle">Session {session_name} &middot; Generated {generated_at}</p>

  <div class="grid">
    <div class="card">
      <div class="card-label">Total Files</div>
      <div class="card-value">{total_items}</div>
    </div>
    <div class="card">
      <div class="card-label">Completed</div>
      <div class="card-value success">{completed_items}</div>
    </div>
    <div class="card">
      <div class="card-label">Failed</div>
      <div class="card-value {failed_class}">{failed_items}</div>
    </div>
    <div class="card">
      <div class="card-label">Data Volume</div>
      <div class="card-value">{total_volume}</div>
    </div>
    <div class="card">
      <div class="card-label">Elapsed</div>
      <div class="card-value">{elapsed}</div>
    </div>
    <div class="card">
      <div class="card-label">Throughput</div>
      <div class="card-value">{throughput}</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">File Status Distribution</div>
    <div class="bar-chart">{bar_chart}</div>
    <div class="legend">{legend}</div>
  </div>

  <div class="section">
    <div class="section-title">Individual File Manifest</div>
    <div style="overflow-x:auto;">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>File Name</th>
          <th>Size</th>
          <th>Type</th>
          <th>Status</th>
          <th>Hash (BLAKE3)</th>
        </tr>
      </thead>
      <tbody>
{file_rows}
      </tbody>
    </table>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Error Log</div>
{error_section}
  </div>

  <div class="footer">
    Transfera v2 &middot; Session {session_id} &middot; Source: {source_root} &rarr; {dest_root}
  </div>
</div>
</body>
</html>
"""


def _format_bytes(size: int) -> str:
    """Convert byte count to human-readable string."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:
        return f"{size / (1024 ** 2):.1f} MB"
    return f"{size / (1024 ** 3):.2f} GB"


def _format_elapsed(seconds: float) -> str:
    """Format seconds into a compact human string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs:.0f}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"


def _build_bar_chart(status_matrix: dict[str, int], total: int) -> tuple[str, str]:
    """Build the bar-chart HTML and legend."""
    if total == 0:
        return '<div class="bar-segment" style="width:100%;background:var(--text-muted);">No data</div>', ""

    segments: list[str] = []
    legend_parts: list[str] = []
    colors = {
        "completed": ("bar-completed", "var(--success)"),
        "failed": ("bar-failed", "var(--error)"),
    }

    for status, count in sorted(status_matrix.items(), key=lambda x: -x[1]):
        pct = (count / total) * 100
        css_class, color = colors.get(status, ("bar-other", "var(--text-muted)"))
        if pct >= 5:
            label = f"{count}"
        else:
            label = ""
        segments.append(
            f'<div class="bar-segment {css_class}" style="width:{pct:.1f}%">{label}</div>'
        )
        legend_parts.append(
            f'<span><span class="legend-dot" style="background:{color}"></span>{status}: {count}</span>'
        )

    return "\n".join(segments), "\n".join(legend_parts)


def _build_file_rows(items: list[MediaItem]) -> str:
    """Build the HTML table rows for each media item."""
    rows: list[str] = []
    for idx, item in enumerate(items, 1):
        status_cls = {
            "completed": "status-completed",
            "failed": "status-failed",
        }.get(item.final_status, "status-other")

        hash_display = item.source_hash if item.source_hash else "—"

        rows.append(
            f'        <tr>\n'
            f'          <td>{idx}</td>\n'
            f'          <td>{_html_escape(item.file_name)}</td>\n'
            f'          <td>{_format_bytes(item.file_size)}</td>\n'
            f'          <td>{_html_escape(item.extension or "—")}</td>\n'
            f'          <td><span class="status-badge {status_cls}">{item.final_status}</span></td>\n'
            f'          <td style="font-family:monospace;font-size:0.75rem;color:var(--text-muted)">{_html_escape(hash_display[:16])}{"…" if hash_display != "—" else ""}</td>\n'
            f'        </tr>'
        )
    return "\n".join(rows)


def _build_error_section(session: TransferSession, items: list[MediaItem]) -> str:
    """Build the error-log section HTML."""
    errors: list[str] = []
    if session.error_message:
        errors.append(f"[SESSION] {session.error_message}")
    for item in items:
        if item.error_message:
            errors.append(f"[{item.file_name}] {item.error_message}")

    if not errors:
        return '    <div class="error-log"><span class="error-log-empty">No errors recorded — all files processed successfully.</span></div>'

    return f'    <div class="error-log">{_html_escape(chr(10).join(errors))}</div>'


def _html_escape(text: str) -> str:
    """Minimal HTML entity escaping."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _build_html(payload: dict[str, Any]) -> str:
    """Render the standalone HTML report from the JSON payload."""
    session = payload["session"]
    throughput = payload["throughput"]
    status_matrix = payload["status_matrix"]
    items = payload["items"]

    total = session["total_items"]
    completed = session["completed_items"]
    failed = session["failed_items"]
    failed_class = "error" if failed > 0 else ""

    bar_chart, legend = _build_bar_chart(status_matrix, total)
    file_rows = _build_file_rows_html(items)

    # Volume display
    total_bytes = session.get("total_bytes_volume") or 0
    volume_str = _format_bytes(total_bytes) if total_bytes else "—"

    # Elapsed
    elapsed_str = _format_elapsed(throughput["elapsed_seconds"])

    # Throughput
    bps = throughput["bytes_per_second"]
    if bps > 0:
        throughput_str = f"{bps / (1024*1024):.2f} MB/s"
    else:
        throughput_str = "—"

    return _HTML_TEMPLATE.format(
        session_id=session["id"],
        session_name=_html_escape(session["session_name"]),
        generated_at=payload["report_generated_at"],
        total_items=total,
        completed_items=completed,
        failed_items=failed,
        failed_class=failed_class,
        total_volume=volume_str,
        elapsed=elapsed_str,
        throughput=throughput_str,
        bar_chart=bar_chart,
        legend=legend,
        file_rows=file_rows,
        error_section=_build_error_section_html(session, items),
        source_root=_html_escape(session["source_root"]),
        dest_root=_html_escape(session["dest_root"]),
    )


def _build_file_rows_html(items: list[dict[str, Any]]) -> str:
    """Build table rows from dict items (for HTML rendering)."""
    rows: list[str] = []
    for idx, item in enumerate(items, 1):
        status_cls = {
            "completed": "status-completed",
            "failed": "status-failed",
        }.get(item["final_status"], "status-other")

        hash_val = item.get("source_hash", "")
        hash_display = hash_val[:16] if hash_val else "—"
        ellipsis = "…" if hash_val else ""

        rows.append(
            f'        <tr>\n'
            f'          <td>{idx}</td>\n'
            f'          <td>{_html_escape(item["file_name"])}</td>\n'
            f'          <td>{_format_bytes(item["file_size"])}</td>\n'
            f'          <td>{_html_escape(item.get("extension") or "—")}</td>\n'
            f'          <td><span class="status-badge {status_cls}">{item["final_status"]}</span></td>\n'
            f'          <td style="font-family:monospace;font-size:0.75rem;color:var(--text-muted)">{_html_escape(hash_display)}{ellipsis}</td>\n'
            f'        </tr>'
        )
    return "\n".join(rows)


def _build_error_section_html(session: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """Build the error-log section from dict data."""
    errors: list[str] = []
    if session.get("error_message"):
        errors.append(f"[SESSION] {session['error_message']}")
    for item in items:
        if item.get("error_message"):
            errors.append(f"[{item['file_name']}] {item['error_message']}")

    if not errors:
        return '    <div class="error-log"><span class="error-log-empty">No errors recorded — all files processed successfully.</span></div>'

    return f'    <div class="error-log">{_html_escape(chr(10).join(errors))}</div>'


def _write_html(html_content: str, output_dir: Path, session_id: int) -> Path:
    """Write the HTML report to disk and return its path."""
    html_path = output_dir / f"session-{session_id}.html"
    html_path.write_text(html_content, encoding="utf-8")
    logger.info("HTML report written -> %s", html_path)
    return html_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def generate_session_report(session_id: int) -> Path:
    """
    Generate a JSON + HTML report for the given session.
    Returns the path to the JSON report file.
    """
    data = await _load_session_data(session_id)
    session: TransferSession = data["session"]
    items: list[MediaItem] = data["items"]

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    payload = _build_json_payload(session, items)

    json_path = _write_json(payload, EXPORT_DIR, session_id)

    html_content = _build_html(payload)
    _write_html(html_content, EXPORT_DIR, session_id)

    logger.info(
        "Session %d report generated: %d items, status=%s",
        session_id, len(items), session.status,
    )
    return json_path
