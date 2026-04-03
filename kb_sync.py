"""
kb_sync.py  —  KB Resolution Sync
====================================
Agentic AI-Powered Incident Resolution System — Pellera Hackathon 2026

PURPOSE:
    Polls ServiceNow for resolved tickets and automatically marks the
    corresponding local kb_documents/ files as RESOLVED — closing the
    learning loop without any manual update_kb_resolved_by_pattern() call.

HOW IT WORKS:
    1. Reads ticket_log.json  →  incident_id → INC number mapping
    2. Reads kb_documents/*.txt  →  finds all STATUS: PENDING files
    3. Cross-references: PENDING file TICKET field → INC number
    4. Queries ServiceNow incident table for state + sys_id
    5. If state = Resolved (6) or Closed (7) → queries work_notes journal
    6. If work_notes contain "Runbook Worked" → marks KB RESOLVED
    7. If work_notes contain "Escalated" → skips KB update (not validated)
    8. If neither phrase found → retries up to MAX_RETRIES times
    9. Repeats every POLL_INTERVAL_SEC until all resolved or stopped

WORK NOTE MATCHING:
    Case-insensitive partial match. "Runbook worked as expected" matches.
    "Escalated to L2 team" matches. Exact phrase not required.

SERVICENOW AUTH:
    Uses HTTP Basic Auth with SNOW_USER / SNOW_PASS from .env.

USAGE:
    python kb_sync.py                  # polls every 60s
    python kb_sync.py --interval 30    # polls every 30s
    python kb_sync.py --once           # single check and exit (testing)

.env required:
    SNOW_INSTANCE_URL  = https://dev293798.service-now.com
    SNOW_USERNAME          = admin
    SNOW_PASSWORD          = your-snow-password
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from kb_utils import KB_DIR, update_kb_resolved_by_pattern

# ── Configuration ─────────────────────────────────────────────────────────────
SNOW_INSTANCE_URL  = os.getenv("SNOW_INSTANCE_URL", "https://dev293798.service-now.com").rstrip("/")
SNOW_USER          = os.getenv("SNOW_USERNAME", "admin")
SNOW_PASS          = os.getenv("SNOW_PASSWORD", "")
ORGANIZATION_NAME  = os.getenv("ORGANIZATION_NAME", "AIOps")

TICKET_LOG_FILE    = Path("ticket_log.json")
POLL_INTERVAL_SEC  = 60   # default — override with --interval
MAX_RETRIES        = 20   # max cycles to wait for work note after ticket resolves

# Work note phrases — case-insensitive partial match
PHRASE_RESOLVED    = "runbook worked"   # SRE writes this when runbook succeeded
PHRASE_ESCALATED   = "escalated"  # SRE writes this when escalated

# ServiceNow incident state codes
SNOW_STATE_RESOLVED = "6"
SNOW_STATE_CLOSED   = "7"


# ── ServiceNow helpers ────────────────────────────────────────────────────────

def _snow_get_incident(inc_number: str) -> dict | None:
    """
    Query ServiceNow for a single incident by INC number.
    Returns the record dict (includes sys_id) or None on error.
    """
    if not HAS_REQUESTS:
        return None
    url = (
        f"{SNOW_INSTANCE_URL}/api/now/table/incident"
        f"?sysparm_query=number={inc_number}"
        f"&sysparm_fields=number,state,close_notes,resolved_at,short_description,sys_id"
        f"&sysparm_limit=1"
    )
    try:
        resp = requests.get(
            url,
            auth=(SNOW_USER, SNOW_PASS),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            results = resp.json().get("result", [])
            return results[0] if results else None
        else:
            print(f"  [KB-SYNC] SNOW query {inc_number} — HTTP {resp.status_code}")
            return None
    except Exception as exc:
        print(f"  [KB-SYNC] SNOW query error: {exc}")
        return None


def _snow_get_work_notes(sys_id: str) -> str:
    """
    Fetch all work_notes for a ticket by sys_id from the journal field table.
    Work_notes are journal entries, not standard fields — requires separate query.
    Returns all note values joined and lowercased for case-insensitive matching.
    """
    if not HAS_REQUESTS or not sys_id:
        return ""
    url = (
        f"{SNOW_INSTANCE_URL}/api/now/table/sys_journal_field"
        f"?sysparm_query=name=incident^element=work_notes^element_id={sys_id}"
        f"&sysparm_fields=value,sys_created_on"
        f"&sysparm_order_by=sys_created_on"
    )
    try:
        resp = requests.get(
            url,
            auth=(SNOW_USER, SNOW_PASS),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            entries = resp.json().get("result", [])
            return " ".join(e.get("value", "") for e in entries).lower()
        else:
            print(f"  [KB-SYNC] work_notes query HTTP {resp.status_code}")
            return ""
    except Exception as exc:
        print(f"  [KB-SYNC] work_notes query error: {exc}")
        return ""


# ── KB file helpers ───────────────────────────────────────────────────────────

def _read_kb_field(content: str, field: str) -> str:
    """Extract a field value from KB document text. e.g. field='PATTERN'."""
    for line in content.splitlines():
        if line.startswith(f"{field}: "):
            return line[len(f"{field}: "):].strip()
    return ""


def _get_pending_kb_files() -> list[dict]:
    """
    Return list of dicts for all kb_*.txt files with STATUS: PENDING.
    Each dict has: path, failure_pattern, incident_id
    """
    pending = []
    if not KB_DIR.exists():
        return pending
    for kb_file in sorted(KB_DIR.glob("kb_*.txt")):
        try:
            content = kb_file.read_text(encoding="utf-8")
        except Exception:
            continue
        if "STATUS: PENDING" not in content:
            continue
        failure_pattern = (
            _read_kb_field(content, "FAILURE_PATTERN_LABEL")
            or _read_kb_field(content, "PATTERN")
        )
        incident_id = (
            _read_kb_field(content, "INCIDENT")
            or _read_kb_field(content, "TICKET")
        )
        if failure_pattern and incident_id:
            pending.append({
                "path":            kb_file,
                "failure_pattern": failure_pattern,
                "incident_id":     incident_id,
            })
    return pending


def _load_ticket_log() -> dict[str, str]:
    """
    Load ticket_log.json and return incident_id → INC number mapping.
    Only includes entries with status=created and a real INC number.
    """
    if not TICKET_LOG_FILE.exists():
        return {}
    try:
        entries = json.loads(TICKET_LOG_FILE.read_text(encoding="utf-8"))
        result = {}
        for e in entries:
            inc_num = e.get("inc_number", "")
            if (e.get("status") == "created"
                    and inc_num
                    and inc_num not in ("UNKNOWN", "FAILED", "PENDING-INC", "PENDING")):
                result[e["incident_id"]] = inc_num
        return result
    except Exception as exc:
        print(f"  [KB-SYNC] ticket_log.json read error: {exc}")
        return {}


# ── Core sync logic ───────────────────────────────────────────────────────────

def run_sync_cycle(verbose: bool = True,
                   retry_counts: dict | None = None) -> dict:
    """
    Run one sync cycle. Returns summary dict with counts.
    retry_counts: mutable dict tracking per-incident retry attempts.
                  Pass the same dict across cycles so retries accumulate.
    """
    if retry_counts is None:
        retry_counts = {}

    summary = {"pending": 0, "resolved": 0, "skipped": 0, "errors": 0}

    pending_files = _get_pending_kb_files()
    if not pending_files:
        if verbose:
            print(f"  [KB-SYNC] No PENDING KB files — nothing to sync")
        return summary

    ticket_map = _load_ticket_log()
    summary["pending"] = len(pending_files)

    if verbose:
        print(f"  [KB-SYNC] {len(pending_files)} PENDING file(s) to check")

    for entry in pending_files:
        fp          = entry["failure_pattern"]
        incident_id = entry["incident_id"]
        inc_number  = ticket_map.get(incident_id, "")

        if not inc_number:
            if verbose:
                print(f"  [KB-SYNC] {fp}: no INC number in ticket_log.json — skipping")
            summary["skipped"] += 1
            continue

        # Step 1: Get incident state and sys_id
        snow_record = _snow_get_incident(inc_number)
        if snow_record is None:
            if verbose:
                print(f"  [KB-SYNC] {fp} ({inc_number}): SNOW query failed — will retry")
            summary["errors"] += 1
            continue

        state = snow_record.get("state", "")

        if state not in (SNOW_STATE_RESOLVED, SNOW_STATE_CLOSED):
            state_label = {
                "1": "New", "2": "In Progress", "3": "On Hold"
            }.get(state, f"state={state}")
            if verbose:
                print(f"  [KB-SYNC] {fp} ({inc_number}): {state_label} — still open")
            continue

        # Step 2: Ticket is resolved — check work notes
        sys_id     = snow_record.get("sys_id", "")
        work_notes = _snow_get_work_notes(sys_id)

        if PHRASE_RESOLVED in work_notes:
            # SRE confirmed runbook worked — safe to mark KB RESOLVED
            close_notes = (
                snow_record.get("close_notes", "")
                or "Resolved via Watson Orchestrate APPROVE flow"
            )
            if verbose:
                print(f"  [KB-SYNC] {fp} ({inc_number}): 'Runbook Worked' found ✓ — marking KB")
            try:
                ok = update_kb_resolved_by_pattern(
                    failure_pattern=fp,
                    incident_id=incident_id,
                    feedback=close_notes[:200],
                    resolution_time=3,
                    steps_executed=["steps executed via Watson Orchestrate runbook"],
                    outcome="resolved",
                )
                if ok:
                    summary["resolved"] += 1
                    retry_counts.pop(incident_id, None)
                    if verbose:
                        print(f"  [KB-SYNC]   → kb_{fp}.txt marked STATUS: RESOLVED ✓")
                else:
                    summary["errors"] += 1
                    if verbose:
                        print(f"  [KB-SYNC]   → update_kb_resolved_by_pattern returned False")
            except Exception as exc:
                summary["errors"] += 1
                print(f"  [KB-SYNC] Error marking {fp} resolved: {exc}")

        elif PHRASE_ESCALATED in work_notes:
            # SRE escalated — runbook did not work, do not pollute KB
            if verbose:
                print(f"  [KB-SYNC] {fp} ({inc_number}): 'Escalated' found — skipping KB update")
            summary["skipped"] += 1
            retry_counts.pop(incident_id, None)

        else:
            # Ticket resolved but no matching work note yet — retry
            retries = retry_counts.get(incident_id, 0) + 1
            retry_counts[incident_id] = retries
            if retries >= MAX_RETRIES:
                print(
                    f"  [KB-SYNC] WARNING: {inc_number} resolved but no matching work note "
                    f"found after {MAX_RETRIES} retries — skipping KB update. "
                    f"Run update_kb_resolved_by_pattern() manually if needed."
                )
                summary["skipped"] += 1
                retry_counts.pop(incident_id, None)
            else:
                if verbose:
                    print(
                        f"  [KB-SYNC] {fp} ({inc_number}): resolved but no work note yet "
                        f"(retry {retries}/{MAX_RETRIES})"
                    )

    return summary


# ── Polling loop ──────────────────────────────────────────────────────────────

def run_polling_loop(interval_sec: int) -> None:
    """
    Run sync cycles on a fixed interval until no PENDING files remain
    or the user presses Enter.
    """
    stop_event = threading.Event()

    def _wait_for_enter():
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        stop_event.set()

    enter_thread = threading.Thread(target=_wait_for_enter, daemon=True)
    enter_thread.start()

    _retry_counts: dict = {}  # persists across cycles
    print(f"\n  [KB-SYNC] Polling ServiceNow every {interval_sec}s for resolved tickets")
    print(f"  [KB-SYNC] Watching for work notes: 'Runbook Worked' or 'Escalated'")
    print(f"  [KB-SYNC] Press Enter to stop\n")

    cycle = 0
    while not stop_event.is_set():
        cycle += 1
        now = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
        print(f"  [KB-SYNC] Cycle {cycle} — {now}")

        summary = run_sync_cycle(verbose=True, retry_counts=_retry_counts)

        remaining = summary["pending"] - summary["resolved"] - summary["skipped"]
        if remaining <= 0 and summary["pending"] > 0:
            print(f"\n  [KB-SYNC] All {summary['pending']} file(s) processed — stopping")
            break

        if not stop_event.is_set():
            stop_event.wait(timeout=interval_sec)

    print(f"  [KB-SYNC] Sync stopped")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="kb_sync — auto-mark KB files RESOLVED when SNOW tickets resolve"
    )
    parser.add_argument(
        "--interval", type=int, default=POLL_INTERVAL_SEC,
        help=f"Poll interval in seconds (default: {POLL_INTERVAL_SEC})"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one sync cycle and exit (useful for testing)"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("KB Sync — Auto RESOLVED marker")
    print(f"AIOps Multi-Agent System — {ORGANIZATION_NAME}")
    print("=" * 60)

    if not HAS_REQUESTS:
        print("ERROR: pip install requests")
        sys.exit(1)

    if not SNOW_USER or not SNOW_PASS:
        print("ERROR: SNOW_USERNAME and SNOW_PASSWORD must be set in .env")
        sys.exit(1)

    if not SNOW_INSTANCE_URL:
        print("ERROR: SNOW_INSTANCE_URL must be set in .env")
        sys.exit(1)

    print(f"  SNOW instance : {SNOW_INSTANCE_URL}")
    print(f"  KB directory  : {KB_DIR.resolve()}")
    print(f"  Ticket log    : {TICKET_LOG_FILE.resolve()}")
    print(f"  Match phrase  : '{PHRASE_RESOLVED}' → RESOLVED")
    print(f"  Match phrase  : '{PHRASE_ESCALATED}' → skip")

    if args.once:
        print(f"\n  Running single sync cycle...\n")
        summary = run_sync_cycle(verbose=True)
        print(
            f"\n  Summary: pending={summary['pending']} resolved={summary['resolved']} "
            f"skipped={summary['skipped']} errors={summary['errors']}"
        )
    else:
        run_polling_loop(args.interval)


if __name__ == "__main__":
    main()
