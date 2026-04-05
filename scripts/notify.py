#!/usr/bin/env python3
"""
notify.py — Alert dispatcher for health-check results.

Reads data/latest.json, compares with the previous run in today's history,
and sends Email + Telegram alerts only when target status changes.
Idempotency is enforced via data/last_notified_run_id.txt.
"""

import html
import json
import os
import smtplib
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ── Path constants ────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
LATEST_JSON = DATA_DIR / "latest.json"
LAST_NOTIFIED_FILE = DATA_DIR / "last_notified_run_id.txt"

IST = timezone(timedelta(hours=5, minutes=30))

STATUS_EMOJI = {"UP": "🟢", "DEGRADED": "🟡", "DOWN": "🔴"}
STATUS_SUBJECT_TAG = {"DOWN": "DOWN", "DEGRADED": "DEGRADED", "UP": "RECOVERED"}


# ── Env var readers (never log values) ───────────────────────────────────────

def _env(key: str, required: bool = True) -> Optional[str]:
    val = os.environ.get(key, "").strip()
    if required and not val:
        print(f"[notify.py] WARNING: env var {key} is not set — skipping related alert")
        return None
    return val or None


# ── Atomic write ──────────────────────────────────────────────────────────────

def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(content)
        tmp_path = tf.name
    os.replace(tmp_path, path)


# ── History helpers ───────────────────────────────────────────────────────────

def load_latest() -> Optional[Dict]:
    if not LATEST_JSON.exists():
        return None
    try:
        with open(LATEST_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def get_previous_run(current_run_id: str, today_ist: str) -> Optional[Dict]:
    """Return the run envelope immediately before the current run in today's history."""
    history_file = HISTORY_DIR / f"{today_ist}.json"
    if not history_file.exists():
        return None
    try:
        with open(history_file, encoding="utf-8") as f:
            runs: List[Dict] = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # Runs are appended in order; find current and return the one before it
    for i, run in enumerate(runs):
        if run.get("run_id") == current_run_id and i > 0:
            return runs[i - 1]

    # If current run isn't in history yet (written after notify), take last entry
    if runs and runs[-1].get("run_id") != current_run_id:
        return runs[-1]
    if len(runs) >= 2:
        return runs[-2]
    return None


def build_status_map(envelope: Dict) -> Dict[str, str]:
    """Return {target_name: status} from a run envelope."""
    return {t["name"]: t["status"] for t in envelope.get("targets", [])}


def detect_changes(
    current: Dict, previous: Optional[Dict]
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """
    Return (down_targets, degraded_targets, recovered_targets).
    Each item is the full target result dict from the current run.
    """
    if previous is None:
        # First run — alert on anything not UP
        down = [t for t in current["targets"] if t["status"] == "DOWN"]
        degraded = [t for t in current["targets"] if t["status"] == "DEGRADED"]
        return down, degraded, []

    prev_map = build_status_map(previous)
    curr_targets = current["targets"]

    down_list, degraded_list, recovered_list = [], [], []
    for t in curr_targets:
        name = t["name"]
        curr_status = t["status"]
        prev_status = prev_map.get(name, "UP")

        if curr_status == "DOWN" and prev_status != "DOWN":
            down_list.append(t)
        elif curr_status == "DEGRADED" and prev_status not in ("DOWN", "DEGRADED"):
            degraded_list.append(t)
        elif curr_status == "UP" and prev_status in ("DOWN", "DEGRADED"):
            recovered_list.append(t)

    return down_list, degraded_list, recovered_list


# ── Email ─────────────────────────────────────────────────────────────────────

def _check_row_html(check_name: str, chk: Dict) -> str:
    colors = {"PASS": "#22c55e", "WARN": "#f59e0b", "FAIL": "#ef4444", "N/A": "#6b7280"}
    icons = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "N/A": "–"}
    status = chk.get("status", "N/A")
    detail = html.escape(chk.get("detail", ""))
    color = colors.get(status, "#6b7280")
    icon = icons.get(status, "–")
    return (
        f'<tr><td style="padding:2px 8px;color:#9ca3af">{check_name}</td>'
        f'<td style="padding:2px 8px;color:{color};font-weight:bold">'
        f'{icon} {status}</td>'
        f'<td style="padding:2px 8px;color:#d1d5db">{detail}</td></tr>'
    )


def _target_card_html(t: Dict) -> str:
    status = t["status"]
    colors = {"UP": "#22c55e", "DEGRADED": "#f59e0b", "DOWN": "#ef4444"}
    color = colors.get(status, "#6b7280")
    name = html.escape(t["name"])
    url = html.escape(t["url"])
    lat = f"{t['latency_ms']}ms" if t["latency_ms"] is not None else "N/A"
    ssl = (
        f"{t['ssl_days_remaining']}d" if t["ssl_days_remaining"] is not None else "N/A"
    )
    checks_html = ""
    for k, label in [
        ("http_status", "HTTP"),
        ("latency", "Latency"),
        ("keyword", "Keyword"),
        ("ssl", "SSL"),
        ("redirect", "Redirect"),
        ("json_schema", "JSON Schema"),
    ]:
        checks_html += _check_row_html(label, t["checks"].get(k, {}))

    return f"""
<div style="background:#1f2937;border-radius:8px;padding:16px;margin:12px 0;
            border-left:4px solid {color}">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <span style="font-size:16px;font-weight:bold;color:#f9fafb">{name}</span>
      <a href="{url}" style="color:#60a5fa;font-size:12px;margin-left:8px">{url}</a>
    </div>
    <span style="background:{color};color:#fff;padding:4px 12px;
                 border-radius:4px;font-weight:bold">{status}</span>
  </div>
  <div style="color:#9ca3af;font-size:13px;margin-top:4px">
    Latency: <b style="color:#f9fafb">{lat}</b> &nbsp;|&nbsp;
    SSL: <b style="color:#f9fafb">{ssl}</b>
  </div>
  <table style="margin-top:8px;font-size:13px;width:100%">{checks_html}</table>
</div>"""


def build_email_body(
    current: Dict,
    down_targets: List[Dict],
    degraded_targets: List[Dict],
    recovered_targets: List[Dict],
) -> Tuple[str, str]:
    """Return (plain_text, html_body)."""
    ts = current.get("run_timestamp_ist", "")
    overall = current.get("overall_status", "")
    all_targets = current.get("targets", [])

    changed = down_targets + degraded_targets + recovered_targets
    subject_parts = []
    if down_targets:
        names = ", ".join(t["name"] for t in down_targets)
        subject_parts.append(f"DOWN: {names}")
    if degraded_targets:
        names = ", ".join(t["name"] for t in degraded_targets)
        subject_parts.append(f"DEGRADED: {names}")
    if recovered_targets:
        names = ", ".join(t["name"] for t in recovered_targets)
        subject_parts.append(f"RECOVERED: {names}")

    subject = f"[Health Check] {' | '.join(subject_parts)} | {ts}"

    # ── Plain text ────────────────────────────────────────────────────────────
    plain_lines = [
        f"Health Check Alert — {ts}",
        f"Overall status: {overall}",
        "",
        "STATUS CHANGES:",
    ]
    for t in changed:
        emoji = STATUS_EMOJI.get(t["status"], "?")
        plain_lines.append(f"  {emoji} {t['name']} → {t['status']} ({t['url']})")

    plain_lines += ["", "ALL TARGETS:"]
    for t in all_targets:
        emoji = STATUS_EMOJI.get(t["status"], "?")
        lat = f"{t['latency_ms']}ms" if t["latency_ms"] is not None else "N/A"
        plain_lines.append(f"  {emoji} {t['name']:<35} {t['status']:<10} {lat}")

    plain_text = "\n".join(plain_lines)

    # ── HTML ──────────────────────────────────────────────────────────────────
    overall_colors = {
        "ALL_UP": "#22c55e",
        "SOME_DEGRADED": "#f59e0b",
        "SOME_DOWN": "#ef4444",
    }
    ov_color = overall_colors.get(overall, "#6b7280")
    ov_label = overall.replace("_", " ")

    changed_cards = "".join(_target_card_html(t) for t in changed)
    all_rows = ""
    for t in all_targets:
        color = {"UP": "#22c55e", "DEGRADED": "#f59e0b", "DOWN": "#ef4444"}.get(
            t["status"], "#6b7280"
        )
        lat = f"{t['latency_ms']}ms" if t["latency_ms"] is not None else "N/A"
        ssl = (
            f"{t['ssl_days_remaining']}d"
            if t["ssl_days_remaining"] is not None
            else "N/A"
        )
        name = html.escape(t["name"])
        all_rows += (
            f'<tr><td style="padding:6px 12px">{name}</td>'
            f'<td style="padding:6px 12px;color:{color};font-weight:bold">'
            f'{t["status"]}</td>'
            f'<td style="padding:6px 12px">{lat}</td>'
            f'<td style="padding:6px 12px">{ssl}</td></tr>'
        )

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;background:#111827;color:#f9fafb;
             padding:24px;margin:0">
  <div style="max-width:700px;margin:0 auto">
    <h1 style="font-size:22px;margin-bottom:4px">Health Check Alert</h1>
    <p style="color:#9ca3af;margin-top:0">{ts}</p>
    <div style="background:{ov_color};color:#fff;display:inline-block;
                padding:6px 16px;border-radius:6px;font-weight:bold;
                margin-bottom:20px">{ov_label}</div>

    <h2 style="font-size:16px;color:#d1d5db;border-bottom:1px solid #374151;
               padding-bottom:8px">Status Changes</h2>
    {changed_cards}

    <h2 style="font-size:16px;color:#d1d5db;border-bottom:1px solid #374151;
               padding-bottom:8px;margin-top:24px">All Targets</h2>
    <table style="width:100%;border-collapse:collapse;font-size:14px">
      <thead>
        <tr style="background:#1f2937;color:#9ca3af;text-align:left">
          <th style="padding:8px 12px">Target</th>
          <th style="padding:8px 12px">Status</th>
          <th style="padding:8px 12px">Latency</th>
          <th style="padding:8px 12px">SSL</th>
        </tr>
      </thead>
      <tbody>{all_rows}</tbody>
    </table>

    <p style="color:#6b7280;font-size:12px;margin-top:24px">
      Powered by GitHub Actions · health-check repo
    </p>
  </div>
</body>
</html>"""

    return subject, plain_text, html_body


def send_email(
    subject: str,
    plain_text: str,
    html_body: str,
    gmail_user: str,
    gmail_password: str,
    recipients: List[str],
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, recipients, msg.as_string())
        print(f"[notify.py] Email sent to {recipients}")
    except smtplib.SMTPAuthenticationError:
        print("[notify.py] ERROR: Gmail authentication failed — check GMAIL_APP_PASSWORD")
    except smtplib.SMTPException as e:
        print(f"[notify.py] ERROR: SMTP error: {e}")
    except OSError as e:
        print(f"[notify.py] ERROR: Network error sending email: {e}")


# ── Telegram ──────────────────────────────────────────────────────────────────

def _escape_md2(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text


def build_telegram_message(
    current: Dict,
    down_targets: List[Dict],
    degraded_targets: List[Dict],
    recovered_targets: List[Dict],
) -> str:
    ts = _escape_md2(current.get("run_timestamp_ist", ""))
    overall = current.get("overall_status", "")
    overall_labels = {
        "ALL_UP": "✅ ALL SYSTEMS UP",
        "SOME_DEGRADED": "⚠️ SOME DEGRADED",
        "SOME_DOWN": "🔴 OUTAGE DETECTED",
    }
    ov_label = overall_labels.get(overall, overall)

    lines = [
        "*Health Check Alert*",
        f"_{ts}_",
        f"Overall: *{_escape_md2(ov_label)}*",
        "",
        "*Changes:*",
    ]

    check_keys = [
        ("http_status", "HTTP"), ("latency", "Latency"),
        ("ssl", "SSL"), ("keyword", "Keyword"),
    ]

    for t in down_targets:
        name = _escape_md2(t["name"])
        lines.append(f"🔴 *{name}* → DOWN")
        for k, label in check_keys:
            chk = t["checks"].get(k, {})
            if chk.get("status") == "FAIL":
                detail = _escape_md2(chk.get("detail", ""))
                lines.append(f"  ✗ {_escape_md2(label)}: {detail}")

    for t in degraded_targets:
        name = _escape_md2(t["name"])
        lines.append(f"🟡 *{name}* → DEGRADED")
        for k, label in check_keys:
            chk = t["checks"].get(k, {})
            if chk.get("status") in ("FAIL", "WARN"):
                detail = _escape_md2(chk.get("detail", ""))
                lines.append(f"  ⚠ {_escape_md2(label)}: {detail}")

    for t in recovered_targets:
        name = _escape_md2(t["name"])
        lat = f"{t['latency_ms']}ms" if t["latency_ms"] is not None else "N/A"
        lines.append(f"🟢 *{name}* → RECOVERED \\({_escape_md2(lat)}\\)")

    lines += ["", "*All targets:*"]
    for t in current.get("targets", []):
        emoji = STATUS_EMOJI.get(t["status"], "?")
        name = _escape_md2(t["name"])
        lat = f"{t['latency_ms']}ms" if t["latency_ms"] is not None else "N/A"
        lines.append(f"{emoji} {name} — {_escape_md2(lat)}")

    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            print("[notify.py] Telegram message sent")
        else:
            print(f"[notify.py] Telegram API error {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as e:
        print(f"[notify.py] ERROR: Telegram request failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    current = load_latest()
    if current is None:
        print("[notify.py] No latest.json found — nothing to notify")
        sys.exit(0)

    run_id = current.get("run_id", "")

    # Idempotency check
    if LAST_NOTIFIED_FILE.exists():
        try:
            last_id = LAST_NOTIFIED_FILE.read_text(encoding="utf-8").strip()
            if last_id == run_id:
                print(f"[notify.py] Already notified for run {run_id} — skipping")
                sys.exit(0)
        except OSError:
            pass

    # Determine today IST date for history lookup
    run_ts = current.get("run_timestamp_ist", "")
    try:
        today_ist = run_ts[:10]  # YYYY-MM-DD
    except (ValueError, IndexError):
        today_ist = datetime.now(IST).strftime("%Y-%m-%d")

    previous = get_previous_run(run_id, today_ist)
    down_targets, degraded_targets, recovered_targets = detect_changes(current, previous)

    if not (down_targets or degraded_targets or recovered_targets):
        print("[notify.py] No status changes detected — no alerts to send")
        atomic_write_text(LAST_NOTIFIED_FILE, run_id)
        sys.exit(0)

    print(
        f"[notify.py] Changes detected: {len(down_targets)} DOWN, "
        f"{len(degraded_targets)} DEGRADED, {len(recovered_targets)} RECOVERED"
    )

    # ── Email ─────────────────────────────────────────────────────────────────
    gmail_user = _env("GMAIL_USER", required=False)
    gmail_password = _env("GMAIL_APP_PASSWORD", required=False)
    alert_email_to = _env("ALERT_EMAIL_TO", required=False)

    if gmail_user and gmail_password and alert_email_to:
        recipients = [e.strip() for e in alert_email_to.split(",") if e.strip()]
        subject, plain_text, html_body = build_email_body(
            current, down_targets, degraded_targets, recovered_targets
        )
        send_email(subject, plain_text, html_body, gmail_user, gmail_password, recipients)
    else:
        print("[notify.py] Email credentials not fully configured — skipping email")

    # ── Telegram ──────────────────────────────────────────────────────────────
    tg_token = _env("TELEGRAM_BOT_TOKEN", required=False)
    tg_chat_id = _env("TELEGRAM_CHAT_ID", required=False)

    if tg_token and tg_chat_id:
        message = build_telegram_message(
            current, down_targets, degraded_targets, recovered_targets
        )
        send_telegram(tg_token, tg_chat_id, message)
    else:
        print("[notify.py] Telegram credentials not configured — skipping Telegram")

    # Mark as notified
    atomic_write_text(LAST_NOTIFIED_FILE, run_id)
    print(f"[notify.py] Notification complete for run {run_id}")
    sys.exit(0)


if __name__ == "__main__":
    main()
