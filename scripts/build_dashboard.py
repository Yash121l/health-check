#!/usr/bin/env python3
"""
build_dashboard.py — Generates docs/index.html from latest.json + history files.

All data is baked into the HTML at build time; no runtime JS fetch calls.
"""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ── Path constants ────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
LATEST_JSON = DATA_DIR / "latest.json"
DOCS_DIR = REPO_ROOT / "docs"
INDEX_HTML = DOCS_DIR / "index.html"
CONFIG_DIR = REPO_ROOT / "config"

IST = timezone(timedelta(hours=5, minutes=30))


# ── Helpers ───────────────────────────────────────────────────────────────────

def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(content)
        tmp_path = tf.name
    os.replace(tmp_path, path)


def load_latest() -> Optional[Dict]:
    if not LATEST_JSON.exists():
        return None
    try:
        with open(LATEST_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def load_settings() -> Dict:
    settings_file = CONFIG_DIR / "settings.yml"
    if settings_file.exists():
        with open(settings_file, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            return cfg.get("global", {})
    return {}


def load_history(days: int = 90) -> Dict[str, List[Dict]]:
    """Return {YYYY-MM-DD: [run_envelope, ...]} for the last `days` days."""
    history: Dict[str, List[Dict]] = {}
    now_ist = datetime.now(IST)
    for i in range(days):
        date_str = (now_ist - timedelta(days=i)).strftime("%Y-%m-%d")
        history_file = HISTORY_DIR / f"{date_str}.json"
        if history_file.exists():
            try:
                with open(history_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    history[date_str] = data
            except (json.JSONDecodeError, OSError):
                pass
    return history


def compute_uptime_table(
    history: Dict[str, List[Dict]], target_names: List[str], days: int = 90
) -> Dict[str, Dict[str, Optional[float]]]:
    """
    Return {target_name: {YYYY-MM-DD: uptime_pct}} for the last `days` days.
    uptime_pct = % of runs where status == 'UP'.
    None means no data for that day.
    """
    now_ist = datetime.now(IST)
    result: Dict[str, Dict[str, Optional[float]]] = {n: {} for n in target_names}

    for i in range(days):
        date_str = (now_ist - timedelta(days=i)).strftime("%Y-%m-%d")
        runs = history.get(date_str, [])
        for name in target_names:
            if not runs:
                result[name][date_str] = None
                continue
            total = 0
            up_count = 0
            for run in runs:
                for t in run.get("targets", []):
                    if t["name"] == name:
                        total += 1
                        if t["status"] == "UP":
                            up_count += 1
                        break
            result[name][date_str] = (up_count / total * 100) if total > 0 else None

    return result


def get_24h_latency(
    history: Dict[str, List[Dict]], target_name: str
) -> List[Dict[str, Any]]:
    """Return [{timestamp_ist, latency_ms}, ...] for the last 24 hours."""
    now_ist = datetime.now(IST)
    cutoff = now_ist - timedelta(hours=24)
    points: List[Dict[str, Any]] = []

    today_str = now_ist.strftime("%Y-%m-%d")
    yesterday_str = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")

    for date_str in [yesterday_str, today_str]:
        runs = history.get(date_str, [])
        for run in runs:
            ts_str = run.get("run_timestamp_ist", "")
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S IST").replace(tzinfo=IST)
            except ValueError:
                continue
            if ts < cutoff:
                continue
            for t in run.get("targets", []):
                if t["name"] == target_name:
                    points.append({
                        "ts": ts_str,
                        "latency_ms": t.get("latency_ms"),
                    })
                    break

    return points


def e(text: Any) -> str:
    """HTML-escape a value for safe embedding."""
    import html
    return html.escape(str(text) if text is not None else "")


# ── Dashboard HTML builder ────────────────────────────────────────────────────

def status_badge(status: str) -> str:
    classes = {"UP": "badge-up", "DEGRADED": "badge-degraded", "DOWN": "badge-down"}
    cls = classes.get(status, "badge-unknown")
    return f'<span class="badge {cls}">{e(status)}</span>'


def check_icon(status: str) -> str:
    icons = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "N/A": "–"}
    css = {"PASS": "chk-pass", "WARN": "chk-warn", "FAIL": "chk-fail", "N/A": "chk-na"}
    icon = icons.get(status, "?")
    cls = css.get(status, "chk-na")
    return f'<span class="{cls}">{icon}</span>'


def render_target_card(
    t: Dict,
    latency_points: List[Dict[str, Any]],
    card_index: int,
) -> str:
    name = e(t["name"])
    url = e(t["url"])
    status = t["status"]
    lat = f"{t['latency_ms']}ms" if t["latency_ms"] is not None else "N/A"
    ssl_days = (
        f"{t['ssl_days_remaining']}d"
        if t["ssl_days_remaining"] is not None
        else "N/A"
    )
    ssl_expiry = e(t.get("ssl_expiry_date") or "")
    error_html = ""
    if t.get("error"):
        error_html = f'<div class="card-error">{e(t["error"])}</div>'

    tags_html = ""
    for tag in t.get("tags", []):
        tags_html += f'<span class="tag">{e(tag)}</span>'

    # Check rows
    check_rows = ""
    for key, label in [
        ("http_status", "HTTP"),
        ("latency", "Latency"),
        ("keyword", "Keyword"),
        ("ssl", "SSL"),
        ("redirect", "Redirect"),
        ("json_schema", "JSON Schema"),
    ]:
        chk = t["checks"].get(key, {"status": "N/A", "detail": ""})
        s = chk.get("status", "N/A")
        detail = e(chk.get("detail", ""))
        check_rows += f"""
        <div class="check-row">
          {check_icon(s)}
          <span class="check-label">{label}</span>
          <span class="check-detail">{detail}</span>
        </div>"""

    # Sparkline chart data
    chart_labels = json.dumps([p["ts"][-8:-3] for p in latency_points])  # HH:MM
    chart_data = json.dumps([p["latency_ms"] for p in latency_points])
    chart_id = f"chart-{card_index}"

    sparkline_html = ""
    if latency_points:
        sparkline_html = f"""
      <div class="chart-container">
        <canvas id="{chart_id}" height="60"></canvas>
      </div>
      <script>
      (function() {{
        var ctx = document.getElementById('{chart_id}');
        if (!ctx) return;
        new Chart(ctx, {{
          type: 'line',
          data: {{
            labels: {chart_labels},
            datasets: [{{
              label: 'Latency (ms)',
              data: {chart_data},
              borderColor: '#60a5fa',
              backgroundColor: 'rgba(96,165,250,0.1)',
              borderWidth: 1.5,
              pointRadius: 2,
              tension: 0.3,
              fill: true,
              spanGaps: true
            }}]
          }},
          options: {{
            responsive: true,
            plugins: {{ legend: {{ display: false }} }},
            scales: {{
              x: {{
                ticks: {{ color: '#9ca3af', font: {{ size: 10 }} }},
                grid: {{ color: '#1f2937' }}
              }},
              y: {{
                ticks: {{ color: '#9ca3af', font: {{ size: 10 }} }},
                grid: {{ color: '#1f2937' }},
                beginAtZero: true
              }}
            }}
          }}
        }});
      }})();
      </script>"""

    status_class = f"card-{status.lower()}"

    return f"""
  <div class="target-card {status_class}">
    <div class="card-header">
      <div class="card-title">
        <a href="{url}" target="_blank" rel="noopener">{name}</a>
        <div class="card-url">{url}</div>
        <div class="card-tags">{tags_html}</div>
      </div>
      {status_badge(status)}
    </div>
    <div class="card-meta">
      <span>Latency: <strong>{lat}</strong></span>
      <span>SSL: <strong>{ssl_days}</strong>
        {f'<span class="ssl-expiry">exp {ssl_expiry}</span>' if ssl_expiry else ''}
      </span>
    </div>
    {error_html}
    <div class="checks">{check_rows}
    </div>
    {sparkline_html}
  </div>"""


def render_uptime_table(
    uptime_data: Dict[str, Dict[str, Optional[float]]],
    target_names: List[str],
    days: int = 90,
) -> str:
    now_ist = datetime.now(IST)
    date_cols = [
        (now_ist - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days - 1, -1, -1)
    ]

    # Show last 30 days in the table for readability
    show_cols = date_cols[-30:]

    header_cells = "<th>Target</th>"
    for d in show_cols:
        short = d[5:]  # MM-DD
        header_cells += f'<th title="{d}">{short}</th>'
    header_cells += "<th>30d Avg</th>"

    rows = ""
    for name in target_names:
        row = f"<td>{e(name)}</td>"
        pcts = []
        for d in show_cols:
            pct = uptime_data.get(name, {}).get(d)
            if pct is None:
                row += '<td class="uptime-nodata" title="No data">–</td>'
            elif pct >= 99:
                row += f'<td class="uptime-ok" title="{pct:.1f}%">●</td>'
            elif pct >= 90:
                row += f'<td class="uptime-warn" title="{pct:.1f}%">●</td>'
            else:
                row += f'<td class="uptime-down" title="{pct:.1f}%">●</td>'
            if pct is not None:
                pcts.append(pct)

        avg = sum(pcts) / len(pcts) if pcts else None
        if avg is None:
            row += '<td class="uptime-nodata">–</td>'
        elif avg >= 99:
            row += f'<td class="uptime-avg uptime-ok">{avg:.1f}%</td>'
        elif avg >= 90:
            row += f'<td class="uptime-avg uptime-warn">{avg:.1f}%</td>'
        else:
            row += f'<td class="uptime-avg uptime-down">{avg:.1f}%</td>'

        rows += f"<tr>{row}</tr>"

    return f"""
  <table class="uptime-table">
    <thead><tr>{header_cells}</tr></thead>
    <tbody>{rows}</tbody>
  </table>"""


def build_html(latest: Dict, history: Dict[str, List[Dict]], settings: Dict) -> str:
    title = settings.get("dashboard_title", "Service Status")
    overall = latest.get("overall_status", "ALL_UP")
    ts_ist = latest.get("run_timestamp_ist", "")
    targets = latest.get("targets", [])
    target_names = [t["name"] for t in targets]
    retention = settings.get("retention_days", 90)

    overall_labels = {
        "ALL_UP": "ALL SYSTEMS OPERATIONAL",
        "SOME_DEGRADED": "SOME SYSTEMS DEGRADED",
        "SOME_DOWN": "OUTAGE DETECTED",
    }
    overall_classes = {
        "ALL_UP": "overall-up",
        "SOME_DEGRADED": "overall-degraded",
        "SOME_DOWN": "overall-down",
    }
    ov_label = overall_labels.get(overall, overall)
    ov_class = overall_classes.get(overall, "overall-up")

    uptime_data = compute_uptime_table(history, target_names, days=retention)

    cards_html = ""
    for i, t in enumerate(targets):
        latency_points = get_24h_latency(history, t["name"])
        cards_html += render_target_card(t, latency_points, i)

    uptime_html = render_uptime_table(uptime_data, target_names, days=retention)

    # Count statuses
    up_count = sum(1 for t in targets if t["status"] == "UP")
    deg_count = sum(1 for t in targets if t["status"] == "DEGRADED")
    down_count = sum(1 for t in targets if t["status"] == "DOWN")
    total_count = len(targets)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{e(title)}</title>
  <link rel="stylesheet" href="assets/style.css">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
</head>
<body>
  <header class="site-header">
    <div class="header-inner">
      <div class="header-left">
        <h1 class="site-title">{e(title)}</h1>
        <div class="last-updated">Last updated: {e(ts_ist)}</div>
      </div>
      <div class="overall-badge {ov_class}">{ov_label}</div>
    </div>
    <div class="status-summary">
      <span class="summary-item summary-up">{up_count} UP</span>
      <span class="summary-item summary-degraded">{deg_count} DEGRADED</span>
      <span class="summary-item summary-down">{down_count} DOWN</span>
      <span class="summary-item summary-total">of {total_count} targets</span>
    </div>
  </header>

  <main class="main-content">
    <section class="targets-section">
      <h2 class="section-title">Service Health</h2>
      <div class="card-grid">
        {cards_html}
      </div>
    </section>

    <section class="uptime-section">
      <h2 class="section-title">30-Day Uptime History</h2>
      <div class="uptime-legend">
        <span class="uptime-ok">● ≥99%</span>
        <span class="uptime-warn">● 90–99%</span>
        <span class="uptime-down">● &lt;90%</span>
        <span class="uptime-nodata">– No data</span>
      </div>
      <div class="uptime-scroll">
        {uptime_html}
      </div>
    </section>
  </main>

  <footer class="site-footer">
    Powered by <a href="https://github.com/features/actions" target="_blank"
      rel="noopener">GitHub Actions</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/Yash121l/health-check" target="_blank"
      rel="noopener">health-check repo</a>
  </footer>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    settings = load_settings()
    latest = load_latest()

    if latest is None:
        print("[build_dashboard.py] No latest.json found — generating placeholder page")
        latest = {
            "run_id": "none",
            "run_timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_timestamp_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
            "overall_status": "ALL_UP",
            "targets": [],
        }

    retention = settings.get("retention_days", 90)
    history = load_history(days=retention)

    html_content = build_html(latest, history, settings)
    atomic_write(INDEX_HTML, html_content)
    print(f"[build_dashboard.py] Wrote {INDEX_HTML}")

    # Ensure assets exist
    assets_dir = DOCS_DIR / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    print("[build_dashboard.py] Done")


if __name__ == "__main__":
    main()
