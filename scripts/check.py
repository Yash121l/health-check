#!/usr/bin/env python3
"""
check.py — Core health-check runner.

Loads targets from config/targets.yml and global settings from config/settings.yml,
runs all checks concurrently, writes data/latest.json and appends to the daily
history file under data/history/YYYY-MM-DD.json (IST date).
"""

import json
import os
import socket
import ssl
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
import yaml
from OpenSSL import crypto

# ── Path constants ────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
LATEST_JSON = DATA_DIR / "latest.json"

IST = timezone(timedelta(hours=5, minutes=30))
LOCK = threading.Lock()


# ── Config loaders ────────────────────────────────────────────────────────────

def load_config() -> tuple:
    with open(CONFIG_DIR / "targets.yml") as f:
        targets_cfg = yaml.safe_load(f)
    with open(CONFIG_DIR / "settings.yml") as f:
        settings_cfg = yaml.safe_load(f)
    return targets_cfg["targets"], settings_cfg["global"]


# ── SSL helper ────────────────────────────────────────────────────────────────

def get_ssl_info(hostname: str, port: int = 443,
                 timeout: int = 10) -> Dict[str, Any]:
    """Return SSL expiry info for a hostname."""
    result = {"days_remaining": None, "expiry_date": None, "error": None}
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(
            socket.create_connection((hostname, port), timeout=timeout),
            server_hostname=hostname,
        ) as ssock:
            der = ssock.getpeercert(binary_form=True)
            cert = crypto.load_certificate(crypto.FILETYPE_ASN1, der)
            expiry_bytes = cert.get_notAfter()
            expiry_str = expiry_bytes.decode("utf-8")  # e.g. 20250630120000Z
            expiry_dt = datetime.strptime(expiry_str, "%Y%m%d%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
            now_utc = datetime.now(timezone.utc)
            days_remaining = (expiry_dt - now_utc).days
            result["days_remaining"] = days_remaining
            result["expiry_date"] = expiry_dt.strftime("%Y-%m-%d")
    except ssl.SSLCertVerificationError as e:
        result["error"] = f"SSL verification error: {e}"
    except ssl.SSLError as e:
        result["error"] = f"SSL error: {e}"
    except socket.timeout:
        result["error"] = "SSL connection timed out"
    except OSError as e:
        result["error"] = f"Connection error: {e}"
    except Exception as e:  # noqa: BLE001
        result["error"] = f"Unexpected SSL error: {e}"
    return result


def check_ssl_self_signed(
    hostname: str, port: int = 443, timeout: int = 10
) -> Dict[str, Any]:
    """Check SSL without verifying chain (for allow_self_signed targets)."""
    result = {"days_remaining": None, "expiry_date": None, "error": None}
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ctx.wrap_socket(
            socket.create_connection((hostname, port), timeout=timeout),
            server_hostname=hostname,
        ) as ssock:
            der = ssock.getpeercert(binary_form=True)
            if not der:
                result["error"] = "No certificate returned"
                return result
            cert = crypto.load_certificate(crypto.FILETYPE_ASN1, der)
            expiry_bytes = cert.get_notAfter()
            expiry_str = expiry_bytes.decode("utf-8")
            expiry_dt = datetime.strptime(expiry_str, "%Y%m%d%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
            now_utc = datetime.now(timezone.utc)
            result["days_remaining"] = (expiry_dt - now_utc).days
            result["expiry_date"] = expiry_dt.strftime("%Y-%m-%d")
    except Exception as e:  # noqa: BLE001
        result["error"] = f"SSL error: {e}"
    return result


# ── Dot-notation JSON key checker ─────────────────────────────────────────────

def resolve_json_key(data: Any, path: str) -> bool:
    """Return True if dot-notation path exists in nested dict/list."""
    parts = path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
                current = current[idx]
            except (ValueError, IndexError):
                return False
        else:
            return False
    return True


# ── Per-target checker ────────────────────────────────────────────────────────

def check_target(target: Dict, settings: Dict) -> Dict[str, Any]:
    """Run all checks for a single target and return a result dict."""
    name = target["name"]
    url = target["url"]
    method = target.get("method", "GET").upper()
    headers = target.get("headers", {}) or {}
    expected_status = target.get("expected_status")
    expected_keyword = target.get("expected_keyword", "") or ""
    expected_final_url = target.get("expected_final_url", "") or ""
    expected_json_keys = target.get("expected_json_keys", []) or []
    allow_self_signed = target.get("allow_self_signed", False)

    timeout_s = settings.get("timeout_s", 10)
    latency_warn = settings.get("latency_warn_ms", 1000)
    latency_fail = settings.get("latency_fail_ms", 2000)
    ssl_warn_days = settings.get("ssl_warn_days", 14)

    now_utc = datetime.now(timezone.utc)

    result: Dict[str, Any] = {
        "name": name,
        "url": url,
        "status": "UP",
        "timestamp_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestamp_ist": now_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "latency_ms": None,
        "http_status": None,
        "ssl_days_remaining": None,
        "ssl_expiry_date": None,
        "redirect_chain": [],
        "final_url": None,
        "keyword_found": None,
        "json_keys_missing": None,
        "checks": {
            "http_status": {"status": "N/A", "detail": ""},
            "latency": {"status": "N/A", "detail": ""},
            "keyword": {"status": "N/A", "detail": ""},
            "ssl": {"status": "N/A", "detail": ""},
            "redirect": {"status": "N/A", "detail": ""},
            "json_schema": {"status": "N/A", "detail": ""},
        },
        "error": None,
        "tags": target.get("tags", []) or [],
    }

    # ── HTTP request ──────────────────────────────────────────────────────────
    response = None
    latency_ms = None
    redirect_chain: List[str] = []
    final_url: Optional[str] = None

    try:
        req_start = datetime.now(timezone.utc)
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=timeout_s,
            allow_redirects=True,
            verify=not allow_self_signed,
            stream=False,
        )
        req_end = datetime.now(timezone.utc)
        latency_ms = int((req_end - req_start).total_seconds() * 1000)

        # Collect redirect chain
        for hist in response.history:
            redirect_chain.append(hist.url)
        final_url = response.url

        result["latency_ms"] = latency_ms
        result["http_status"] = response.status_code
        result["redirect_chain"] = redirect_chain
        result["final_url"] = final_url

    except requests.exceptions.SSLError as e:
        result["error"] = f"SSL error: {e}"
        result["checks"]["http_status"] = {"status": "FAIL", "detail": f"SSL error: {e}"}
        result["checks"]["ssl"] = {"status": "FAIL", "detail": f"SSL error: {e}"}
        result["status"] = "DOWN"
        return result
    except requests.exceptions.ConnectionError as e:
        result["error"] = f"Connection refused: {e}"
        result["checks"]["http_status"] = {
            "status": "FAIL", "detail": "Connection refused"
        }
        result["status"] = "DOWN"
        return result
    except requests.exceptions.Timeout:
        result["error"] = f"Request timed out after {timeout_s}s"
        result["checks"]["http_status"] = {
            "status": "FAIL", "detail": f"Timeout after {timeout_s}s"
        }
        result["checks"]["latency"] = {
            "status": "FAIL", "detail": f"Timeout after {timeout_s}s"
        }
        result["status"] = "DOWN"
        return result
    except requests.exceptions.RequestException as e:
        result["error"] = f"Request error: {e}"
        result["checks"]["http_status"] = {"status": "FAIL", "detail": str(e)}
        result["status"] = "DOWN"
        return result

    check_statuses: List[str] = []

    # ── 2a. HTTP Status ───────────────────────────────────────────────────────
    code = response.status_code
    if expected_status:
        if code == expected_status:
            http_chk = "PASS"
            http_detail = f"HTTP {code} (expected {expected_status})"
        elif 200 <= code < 300:
            http_chk = "WARN"
            http_detail = f"HTTP {code} (expected {expected_status})"
        else:
            http_chk = "FAIL"
            http_detail = f"HTTP {code} (expected {expected_status})"
    else:
        if 200 <= code < 300:
            http_chk = "PASS"
            http_detail = f"HTTP {code}"
        elif 300 <= code < 400:
            http_chk = "WARN"
            http_detail = f"HTTP {code} (unexpected redirect)"
        else:
            http_chk = "FAIL"
            http_detail = f"HTTP {code}"
    result["checks"]["http_status"] = {"status": http_chk, "detail": http_detail}
    check_statuses.append(http_chk)

    # ── 2b. Latency ───────────────────────────────────────────────────────────
    if latency_ms is not None:
        if latency_ms < latency_warn:
            lat_chk = "PASS"
            lat_detail = f"{latency_ms}ms"
        elif latency_ms <= latency_fail:
            lat_chk = "WARN"
            lat_detail = f"{latency_ms}ms (warn threshold {latency_warn}ms)"
        else:
            lat_chk = "FAIL"
            lat_detail = f"{latency_ms}ms (fail threshold {latency_fail}ms)"
        result["checks"]["latency"] = {"status": lat_chk, "detail": lat_detail}
        check_statuses.append(lat_chk)

    # ── 2c. Keyword ───────────────────────────────────────────────────────────
    if expected_keyword:
        try:
            body = response.text
            if expected_keyword in body:
                result["keyword_found"] = True
                result["checks"]["keyword"] = {
                    "status": "PASS",
                    "detail": f'Found "{expected_keyword}"',
                }
            else:
                result["keyword_found"] = False
                result["checks"]["keyword"] = {
                    "status": "FAIL",
                    "detail": f'Missing "{expected_keyword}"',
                }
                check_statuses.append("FAIL")
        except Exception as e:  # noqa: BLE001
            result["checks"]["keyword"] = {
                "status": "FAIL",
                "detail": f"Error reading body: {e}",
            }
            check_statuses.append("FAIL")
    else:
        result["checks"]["keyword"] = {"status": "N/A", "detail": "No keyword configured"}

    # ── 2d. SSL ───────────────────────────────────────────────────────────────
    parsed = urlparse(url)
    if parsed.scheme == "https":
        hostname = parsed.hostname
        port = parsed.port or 443
        if allow_self_signed:
            ssl_info = check_ssl_self_signed(hostname, port, timeout_s)
        else:
            ssl_info = get_ssl_info(hostname, port, timeout_s)

        if ssl_info["error"]:
            ssl_chk = "FAIL"
            ssl_detail = ssl_info["error"]
            check_statuses.append("FAIL")
        else:
            days = ssl_info["days_remaining"]
            result["ssl_days_remaining"] = days
            result["ssl_expiry_date"] = ssl_info["expiry_date"]
            if days is not None and days < 0:
                ssl_chk = "FAIL"
                ssl_detail = f"Certificate EXPIRED {abs(days)} days ago"
                check_statuses.append("FAIL")
            elif days is not None and days <= ssl_warn_days:
                ssl_chk = "WARN"
                ssl_detail = f"Expires in {days} days ({ssl_info['expiry_date']})"
                check_statuses.append("WARN")
            else:
                ssl_chk = "PASS"
                ssl_detail = f"Valid, expires in {days} days ({ssl_info['expiry_date']})"
        result["checks"]["ssl"] = {"status": ssl_chk, "detail": ssl_detail}
    else:
        result["checks"]["ssl"] = {"status": "N/A", "detail": "Not HTTPS"}

    # ── 2e. Redirect ──────────────────────────────────────────────────────────
    if expected_final_url:
        if final_url and final_url.rstrip("/") == expected_final_url.rstrip("/"):
            redir_chk = "PASS"
            redir_detail = f"Final URL matches: {final_url}"
        else:
            redir_chk = "FAIL"
            redir_detail = f"Expected {expected_final_url}, got {final_url}"
            check_statuses.append("FAIL")
    else:
        redirected = len(redirect_chain) > 0
        redir_chk = "PASS"
        redir_detail = (
            f"Redirected via {len(redirect_chain)} hop(s) to {final_url}"
            if redirected
            else "No redirect"
        )
    result["checks"]["redirect"] = {"status": redir_chk, "detail": redir_detail}

    # ── 2f. JSON schema ───────────────────────────────────────────────────────
    content_type = response.headers.get("Content-Type", "")
    if expected_json_keys and "application/json" in content_type:
        try:
            body_json = response.json()
            missing = [k for k in expected_json_keys if not resolve_json_key(body_json, k)]
            result["json_keys_missing"] = missing
            if missing:
                result["checks"]["json_schema"] = {
                    "status": "FAIL",
                    "detail": f"Missing keys: {missing}",
                }
                check_statuses.append("FAIL")
            else:
                result["checks"]["json_schema"] = {
                    "status": "PASS",
                    "detail": f"All keys present: {expected_json_keys}",
                }
        except ValueError:
            result["checks"]["json_schema"] = {
                "status": "FAIL",
                "detail": "Response is not valid JSON",
            }
            check_statuses.append("FAIL")
    elif expected_json_keys and "application/json" not in content_type:
        result["checks"]["json_schema"] = {
            "status": "FAIL",
            "detail": f"Expected JSON but Content-Type is '{content_type}'",
        }
        check_statuses.append("FAIL")
    else:
        result["checks"]["json_schema"] = {
            "status": "N/A",
            "detail": "No JSON schema check configured",
        }

    # ── Overall target status ─────────────────────────────────────────────────
    if "FAIL" in check_statuses:
        result["status"] = "DOWN"
    elif "WARN" in check_statuses:
        result["status"] = "DEGRADED"
    else:
        result["status"] = "UP"

    return result


# ── Atomic file write ─────────────────────────────────────────────────────────

def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically via a temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as tf:
        json.dump(data, tf, indent=2, ensure_ascii=False)
        tmp_path = tf.name
    os.replace(tmp_path, path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    targets, settings = load_config()

    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)
    run_id = now_ist.strftime("%Y%m%d-%H%M%S-IST")

    print(f"[check.py] Starting run {run_id} — {len(targets)} targets")

    target_results: List[Dict] = [None] * len(targets)  # type: ignore[list-item]

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_idx = {
            executor.submit(check_target, t, settings): i
            for i, t in enumerate(targets)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                target_results[idx] = future.result()
            except Exception as e:  # noqa: BLE001
                t = targets[idx]
                target_results[idx] = {
                    "name": t.get("name", "Unknown"),
                    "url": t.get("url", ""),
                    "status": "DOWN",
                    "timestamp_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "timestamp_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
                    "latency_ms": None,
                    "http_status": None,
                    "ssl_days_remaining": None,
                    "ssl_expiry_date": None,
                    "redirect_chain": [],
                    "final_url": None,
                    "keyword_found": None,
                    "json_keys_missing": None,
                    "checks": {
                        k: {"status": "FAIL", "detail": f"Internal error: {e}"}
                        for k in ("http_status", "latency", "keyword",
                                  "ssl", "redirect", "json_schema")
                    },
                    "error": str(e),
                    "tags": t.get("tags", []) or [],
                }

    # Overall status
    statuses = [r["status"] for r in target_results]
    if "DOWN" in statuses:
        overall = "SOME_DOWN"
    elif "DEGRADED" in statuses:
        overall = "SOME_DEGRADED"
    else:
        overall = "ALL_UP"

    envelope = {
        "run_id": run_id,
        "run_timestamp_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_timestamp_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "overall_status": overall,
        "targets": target_results,
    }

    # Write latest.json
    atomic_write_json(LATEST_JSON, envelope)
    print(f"[check.py] Wrote {LATEST_JSON}")

    # Append to today's history file
    today_str = now_ist.strftime("%Y-%m-%d")
    history_file = HISTORY_DIR / f"{today_str}.json"
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    with LOCK:
        if history_file.exists():
            try:
                with open(history_file, encoding="utf-8") as f:
                    history_data = json.load(f)
                if not isinstance(history_data, list):
                    history_data = []
            except (json.JSONDecodeError, OSError):
                history_data = []
        else:
            history_data = []

        history_data.append(envelope)
        atomic_write_json(history_file, history_data)
    print(f"[check.py] Appended to {history_file}")

    # Purge old history files
    retention_days = settings.get("retention_days", 90)
    cutoff = now_ist.date() - timedelta(days=retention_days)
    purged = 0
    for f in HISTORY_DIR.glob("*.json"):
        try:
            file_date_str = f.stem  # YYYY-MM-DD
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d").date()
            if file_date < cutoff:
                f.unlink()
                purged += 1
        except ValueError:
            pass
    if purged:
        print(f"[check.py] Purged {purged} old history file(s) (older than {cutoff})")

    # Print summary
    for r in target_results:
        icon = {"UP": "✓", "DEGRADED": "~", "DOWN": "✗"}.get(r["status"], "?")
        lat = f"{r['latency_ms']}ms" if r["latency_ms"] is not None else "N/A"
        print(f"  {icon} {r['name']:<35} {r['status']:<10} {lat}")

    print(f"[check.py] Overall: {overall}")
    sys.exit(0)


if __name__ == "__main__":
    main()
