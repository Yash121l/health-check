"""
telemetry.py — Anonymous PostHog events from the GitHub Actions runs.

Counts and timings only: no target names, URLs or error text. Sends only inside
GitHub Actions, honours HEALTH_CHECK_TELEMETRY=0, and never fails or slows the job
(3-second timeout, every error swallowed).
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import yaml

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "config" / "settings.yml"
INGEST_URL = "https://us.i.posthog.com/i/v0/e/"


def _key() -> Optional[str]:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            key = (yaml.safe_load(f) or {}).get("global", {}).get("posthog_key")
        return key or None
    except (OSError, yaml.YAMLError):
        return None


def capture(event: str, properties: Dict[str, Any]) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    if os.environ.get("HEALTH_CHECK_TELEMETRY", "").strip() == "0":
        return
    key = _key()
    if not key:
        return
    payload = {
        "api_key": key,
        "event": event,
        "distinct_id": "health-check-actions",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "properties": {"app": "health-check", "$process_person_profile": False, **properties},
    }
    try:
        requests.post(INGEST_URL, json=payload, timeout=3)
    except Exception:  # noqa: BLE001 — telemetry must never break a run
        pass
