"""
agent3_notify.py  —  Agent 3: Handoff Notifier
================================================
Agentic AI-Powered Incident Resolution System — Pellera Hackathon 2026

ROLE:
  This script is the BRIDGE between Agent 2 (Python) and
  Agent 3 (Watson Orchestrate). It runs automatically at the
  end of run_demo.py after Agent 2 completes.

  It sends the snow_ready.json content to Watson Orchestrate
  via the Orchestrate API. The AIOps_Incident Resolution Manager
  agent receives it and then:
    1. Calls ServiceNow skill to create ticket → gets INC number
    2. Sends email notification to SRE
    3. Sends Teams notification to SRE
    4. Waits for SRE to type ticket number

  If Orchestrate API is not available, this script falls back to:
    - Sending email directly (SMTP)
    - Sending Teams message directly (webhook)
    - Printing instructions for manual paste

INPUT:
  snow_ready.json   — written by Agent 2
  rca_output.json   — written by Agent 2

OUTPUT:
  Notifications via Watson Orchestrate OR email + Teams directly

CONFIRMED WORKING CONFIGURATION:
  Token URL  : https://iam.platform.saas.ibm.com/siusermgr/api/1.0/apikeys/token
  Endpoint   : {ORCHESTRATE_INSTANCE_URL}/v1/orchestrate/runs
  Body       : {"message": {"role": "user", "content": [{"response_type": "text", "text": "..."}]}, "agent_id": "..."}
  Poll       : {ORCHESTRATE_INSTANCE_URL}/v1/orchestrate/runs/{run_id}

.env required:
  ORCHESTRATE_INSTANCE_URL = https://api.dl.watson-orchestrate.ibm.com/instances/20260318-0102-3826-4018-4af1cf54692e
  ORCHESTRATE_API_KEY      = your Orchestrate API key (Settings → API details → Generate API key)
  ORCHESTRATE_AGENT_ID     = 70e7cd1f-cb5e-4f71-ac13-0a516aab01fb
  SMTP_HOST                = smtp.gmail.com
  SMTP_PORT                = 587
  SMTP_USER                = your-gmail@gmail.com
  SMTP_PASS                = your-16-char-app-password
  TEAMS_WEBHOOK_URL        = https://your-org.webhook.office.com/...

USAGE:
  python agent3_notify.py
  python agent3_notify.py --skip-orchestrate   (email+teams only)
  python agent3_notify.py --skip-email         (orchestrate+teams only)
  python agent3_notify.py --skip-teams         (orchestrate+email only)
"""

import argparse
import functools
import http.server
import json
import os
import re
import smtplib
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

# Optional — used to read STATUS from local KB files for upload log
try:
    from kb_utils import KB_DIR as _KB_DIR
    HAS_KB_UTILS = True
except ImportError:
    HAS_KB_UTILS = False
    _KB_DIR = None

# ── Configuration ─────────────────────────────────────────────────────────────
ORCHESTRATE_INSTANCE_URL = os.getenv("ORCHESTRATE_INSTANCE_URL", "")
ORCHESTRATE_API_KEY      = os.getenv("ORCHESTRATE_API_KEY",      "")
ORCHESTRATE_AGENT_ID     = os.getenv("ORCHESTRATE_AGENT_ID",     "")

SMTP_HOST  = os.getenv("SMTP_HOST",  "smtp.gmail.com")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER",  "")
SMTP_PASS  = os.getenv("SMTP_PASS",  "")
EMAIL_TO   = os.getenv("EMAIL_TO", "venkata.allam@pellera.com")

TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")
SNOW_INSTANCE_URL = os.getenv("SNOW_INSTANCE_URL", "https://dev293798.service-now.com").rstrip("/")
WO_UI_URL         = os.getenv("WO_UI_URL",         "https://dl.watson-orchestrate.ibm.com").rstrip("/")
ORGANIZATION_NAME = os.getenv("ORGANIZATION_NAME", "AIOps")
PROJECT_NAME      = os.getenv("PROJECT_NAME",      "AIOps Multi-Agent System")
ORCHESTRATE_ADK_ENV = os.getenv("ORCHESTRATE_ADK_ENV", "hackathon_vmaa")
# ── Config directory ─────────────────────────────────────────────────────────
_CONFIG_DIR = Path(__file__).parent / "config"


def _load_yaml_config(filename: str, fallback: object) -> object:
    """Load YAML config from config/. Returns fallback on any error."""
    try:
        import yaml
        path = _CONFIG_DIR / filename
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        print(f"  WARN: config/{filename} not found — using defaults")
    except Exception as exc:
        print(f"  WARN: config/{filename} error: {exc}")
    return fallback

SNOW_FILE   = Path("snow_ready.json")
RCA_FILE    = Path("rca_output.json")
REPORTS_DIR = Path("rca_reports")
MODEL_ID    = os.getenv("MODEL_ID", "ibm/granite-4-h-small")

# Confirmed working MCSP token endpoint
MCSP_TOKEN_URL = "https://iam.platform.saas.ibm.com/siusermgr/api/1.0/apikeys/token"

# Local HTTP server — serves rca_reports/ so Teams "View RCA Report" button
# opens the actual IBM-styled HTML report card in the browser.
RCA_SERVER_PORT = int(os.getenv("RCA_SERVER_PORT", "8099"))
_RCA_BASE_URL: str | None = None   # set by start_rca_server(), read globally


# ── MCSP Token Exchange ───────────────────────────────────────────────────────

def get_mcsp_token(api_key: str) -> str | None:
    """
    Exchange Orchestrate API key for MCSP Bearer token.
    Confirmed working endpoint and body format.
    """
    try:
        resp = requests.post(
            MCSP_TOKEN_URL,
            json={"apikey": api_key},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            data  = resp.json()
            token = data.get("token") or data.get("access_token")
            if token:
                print(f"  ✓  MCSP token obtained (expires_in: {data.get('expires_in','?')}s)")
                return token
            else:
                print(f"  ✗  Token field not found: {data}")
                return None
        else:
            print(f"  ✗  MCSP token failed {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as exc:
        print(f"  ✗  MCSP token error: {exc}")
        return None


# ── Poll Run ──────────────────────────────────────────────────────────────────

def poll_run(instance_url: str, run_id: str, headers: dict,
             max_wait: int = 120) -> dict | None:
    """
    Poll GET {instance_url}/v1/orchestrate/runs/{run_id}
    until status = completed / failed / cancelled / expired.
    """
    poll_url = f"{instance_url.rstrip('/')}/v1/orchestrate/runs/{run_id}"
    elapsed  = 0
    interval = 3

    print(f"    Polling run {run_id[:8]}... ", end="", flush=True)

    while elapsed < max_wait:
        try:
            resp = requests.get(poll_url, headers=headers, timeout=15)

            if resp.status_code == 200:
                data   = resp.json()
                status = data.get("status", "")

                if status == "completed":
                    print(f"completed ({elapsed}s)")
                    return data
                elif status in ("failed", "cancelled", "expired"):
                    print(f"{status}")
                    print(f"    Error: {data.get('last_error', 'no details')}")
                    return None
                else:
                    print(".", end="", flush=True)
                    time.sleep(interval)
                    elapsed += interval

            elif resp.status_code == 401:
                print()
                print(f"    401 — token expired")
                return None
            else:
                print()
                print(f"    Poll error {resp.status_code}: {resp.text[:150]}")
                return None

        except Exception as exc:
            print()
            print(f"    Poll error: {exc}")
            return None

    print(f"\n    Timed out after {max_wait}s")
    return None


# ── Option A: Send to Watson Orchestrate API ──────────────────────────────────

def send_to_orchestrate(snow_tickets: list[dict]) -> bool:
    """
    Send snow_ready.json to Watson Orchestrate AIOps_Incident Resolution Manager.

    CONFIRMED WORKING (tested March 23, 2026):
      Step 1: GET MCSP token from MCSP_TOKEN_URL using ORCHESTRATE_API_KEY
      Step 2: POST {ORCHESTRATE_INSTANCE_URL}/v1/orchestrate/runs
              Body: {"message": {"role": "user", "content": [{"response_type": "text", "text": "..."}]}, "agent_id": "..."}
      Step 3: Poll {ORCHESTRATE_INSTANCE_URL}/v1/orchestrate/runs/{run_id}
              Until status = completed

    Returns True if all incidents sent successfully.
    """
    missing = []
    if not ORCHESTRATE_INSTANCE_URL: missing.append("ORCHESTRATE_INSTANCE_URL")
    if not ORCHESTRATE_API_KEY:      missing.append("ORCHESTRATE_API_KEY")
    if not ORCHESTRATE_AGENT_ID:     missing.append("ORCHESTRATE_AGENT_ID")

    if missing:
        print(f"  SKIP: Orchestrate not configured — missing: {', '.join(missing)}")
        return False

    if not HAS_REQUESTS:
        print("  ERROR: pip install requests")
        return False

    # ── Step 1: Get MCSP Bearer token ─────────────────────────────────────────
    print(f"\n[AGENT 3] Getting MCSP token...")
    bearer_token = get_mcsp_token(ORCHESTRATE_API_KEY)
    if not bearer_token:
        print("  ✗  Cannot proceed without token")
        return False

    # ── Step 2: Build confirmed endpoint ──────────────────────────────────────
    runs_endpoint = f"{ORCHESTRATE_INSTANCE_URL.rstrip('/')}/v1/orchestrate/runs"

    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type":  "application/json",
    }

    print(f"[AGENT 3] Sending to Watson Orchestrate")
    print(f"  Endpoint : {runs_endpoint}")
    print(f"  Agent ID : {ORCHESTRATE_AGENT_ID}")

    success_count = 0
    ticket_log    = []  # Tracks INC numbers for final summary

    for ticket in snow_tickets:
        inc_id = ticket.get("_incident_id", "unknown")
        title  = ticket.get("_title",       "Unknown Incident")

        def _sanitize(text: str) -> str:
            """Remove chars that break Orchestrate instruction parsing."""
            if not text:
                return ""
            return (text
                    .replace("{{", "{").replace("}}", "}")
                    .replace("%{", "pct{"))

        # ── DESCRIPTION: complete RCA + complete runbook — no truncation ──────
        # agent2_analyst.py already builds the full combined description and
        # writes it into snow_ready.json["description"].
        # It contains: RCA analysis + remediation steps + runbook + escalation.
        # This is the authoritative backup copy in case work notes differ.
        combined_desc = _sanitize(ticket.get("description", ""))

        # ── CATEGORY MAP: Granite value → confirmed SNOW dev293798 OOTB value ──
        # Problem: Orchestrate's ServiceNow agent runs 'Get categories' to
        # validate the category before creating the ticket. Dev instance
        # dev293798 uses OOTB categories (Network, Software, Hardware,
        # Inquiry / Help). If Granite produces a value the lookup doesn't
        # match exactly, Orchestrate defaults to 'Inquiry / Help'.
        # This map translates Granite output to the exact OOTB string.
        _cat_cfg = _load_yaml_config("snow_category_map.yaml", {})
        _cat_map = _cat_cfg.get("mappings", {}) if isinstance(_cat_cfg, dict) else {}
        _cat_def = _cat_cfg.get("default_category", "Inquiry / Help") if isinstance(_cat_cfg, dict) else "Inquiry / Help"
        SNOW_CATEGORY_MAP = _cat_map or {
            "Network": "Network", "network": "Network",
            "Application": "Software", "application": "Software",
            "Infrastructure": "Hardware", "infrastructure": "Hardware",
            "Security": "Network", "security": "Network",
            "Platform": "Software", "platform": "Software",
        }
        raw_category    = ticket.get("category", "")
        mapped_category = SNOW_CATEGORY_MAP.get(raw_category, _cat_def)

        # ── INSTRUCTION: single-step, minimal fields, confirmed values ─────────
        # The Orchestrate agent behavior rule: validate EVERY parameter via
        # helper tools before calling Create Incident.  Each value below is
        # guaranteed to return exactly ONE row from the helper tool:
        #
        #   urgency          "1 - High"     → Get urgency of case → 1 row ✓
        #   impact           "1 - High"     → Get Impact details  → 1 row ✓
        #   category         mapped value   → Get categories      → 1 row ✓
        #   assignment_group exact name     → get assignment group → 1 row ✓
        #   caller_id        "admin"        → Get system users    → 1 row ✓
        #
        # Fields deliberately EXCLUDED from instruction:
        #   priority   — SNOW auto-calculates from urgency+impact; sending
        #                it separately causes helper tool conflict
        #   subcategory — custom values do not exist in dev293798; helper
        #                returns zero rows → agent stops and asks SRE
        #   state      — defaults to "New" in SNOW automatically
        #
        # Work notes STEP 2 removed — Update Incident tool writes to standard
        # fields only, NOT the SNOW activity journal.  "Updated with work notes"
        # was agent hallucination; Activities: 1 in screenshots proved it failed.
        # Description already holds the full content as the reliable record.
        instruction = (
            f"Create a ServiceNow incident with these exact pre-validated values. "
            f"All values are confirmed against ServiceNow — use them exactly as "
            f"provided without additional lookup:\n\n"
            f"short_description: {ticket.get('short_description', '')}\n"
            f"description: {combined_desc}\n"
            f"caller_id: admin\n"
            f"category: {mapped_category}\n"
            f"urgency: {ticket.get('urgency', '1 - High')}\n"
            f"impact: {ticket.get('impact', '1 - High')}\n"
            f"assignment_group: {ticket.get('assignment_group', '')}\n"
            f"cmdb_ci: {ticket.get('cmdb_ci', '')}\n\n"
            f"Return ONLY the INC number from the created ticket.\n"
        )

        # CONFIRMED body format — response_type not type
        body = {
            "message": {
                "role":    "user",
                "content": [
                    {
                        "response_type": "text",
                        "text":          instruction,
                    }
                ],
            },
            "agent_id": ORCHESTRATE_AGENT_ID,
        }

        try:
            print(f"\n  [{inc_id}] Submitting run ...")
            resp = requests.post(
                runs_endpoint,
                json=body,
                headers=headers,
                timeout=30,
            )

            if resp.status_code == 200:
                data   = resp.json()
                run_id = data.get("run_id")
                print(f"  [{inc_id}] Run submitted — run_id={run_id}")

                if run_id:
                    result = poll_run(ORCHESTRATE_INSTANCE_URL, run_id, headers)
                    if result:
                        # ── Extract INC ticket number from agent reply ─────
                        inc_number = "UNKNOWN"
                        try:
                            content = (
                                result.get("result", {})
                                      .get("data", {})
                                      .get("message", {})
                                      .get("content", [])
                            )
                            reply_text = ""
                            if isinstance(content, list) and content:
                                reply_text = content[0].get("text", "")
                            elif isinstance(content, str):
                                reply_text = content

                            # Print reply for debugging
                            if reply_text:
                                print(f"  [{inc_id}] Agent reply: {reply_text[:300]}")

                            # Parse INC number — widened to 5-10 digits
                            match = re.search(r'\bINC\d{5,10}\b', reply_text)
                            if match:
                                inc_number = match.group(0)
                            else:
                                # Try alternate format without word boundary
                                match2 = re.search(r'INC\d+', reply_text)
                                if match2:
                                    inc_number = match2.group(0)

                        except Exception as e:
                            print(f"  [{inc_id}] INC parse error: {e}")

                        # ── Ticket creation log ───────────────────────────
                        print(f"\n  {'─'*50}")
                        print(f"  ✅ TICKET CREATED")
                        print(f"  {'─'*50}")
                        print(f"  Incident ID   : {inc_id}")
                        print(f"  Title         : {title}")
                        print(f"  INC Number    : {inc_number}")
                        print(f"  Priority      : P{ticket.get('priority','1')}")
                        print(f"  Assignment    : {ticket.get('assignment_group','')}")
                        print(f"  CMDB CI       : {ticket.get('cmdb_ci','')}")
                        print(f"  Run ID        : {run_id}")
                        print(f"  Created At    : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
                        print(f"  {'─'*50}")

                        # Store for final summary
                        ticket_log.append({
                            "incident_id":   inc_id,
                            "title":         title,
                            "inc_number":    inc_number,
                            "priority":      ticket.get("priority", "1"),
                            "assignment":    ticket.get("assignment_group", ""),
                            "cmdb_ci":       ticket.get("cmdb_ci", ""),
                            "run_id":        run_id,
                            "status":        "created",
                        })
                        success_count += 1
                    else:
                        print(f"  [{inc_id}] Run did not complete")
                        ticket_log.append({
                            "incident_id": inc_id,
                            "title":       title,
                            "inc_number":  "FAILED",
                            "status":      "failed",
                        })
                else:
                    print(f"  [{inc_id}] No run_id in response: {data}")

            elif resp.status_code == 401:
                print(f"  [{inc_id}] 401 — token expired or rejected")
                print(f"    Response: {resp.text[:300]}")
                return False

            elif resp.status_code == 422:
                print(f"  [{inc_id}] 422 — validation error")
                print(f"    Response: {resp.text[:500]}")
                return False

            elif resp.status_code == 500:
                print(f"  [{inc_id}] 500 — server error")
                print(f"    Response: {resp.text[:300]}")
                return False

            else:
                print(f"  [{inc_id}] {resp.status_code}: {resp.text[:300]}")

        except Exception as exc:
            print(f"  [{inc_id}] Connection error: {exc}")

    total = len(snow_tickets)
    print(f"\n  Orchestrate: {success_count}/{total} incidents sent successfully")

    # ── Final ticket summary table ─────────────────────────────────────────────
    if ticket_log:
        print(f"\n  {'='*60}")
        print(f"  TICKET CREATION SUMMARY")
        print(f"  {'='*60}")
        print(f"  {'Incident ID':<30} {'INC Number':<15} {'Status'}")
        print(f"  {'─'*30} {'─'*15} {'─'*10}")
        for entry in ticket_log:
            status_icon = "✅" if entry["status"] == "created" else "❌"
            print(f"  {entry['incident_id']:<30} {entry['inc_number']:<15} {status_icon} {entry['status']}")
        print(f"  {'='*60}")
        print(f"  Total: {success_count}/{total} tickets created")

        # Write ticket log to file for reference
        log_file = Path("ticket_log.json")
        log_file.write_text(
            json.dumps(ticket_log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  Log saved → {log_file.resolve()}")

    return success_count == total


# ── Option B: Send email directly (fallback) ──────────────────────────────────

def send_email_direct(inc: dict, snow_num: str = "PENDING") -> None:
    """
    Send one email per incident via SMTP.
    Uses full HTML RCA report card from rca_reports/ as email body.
    Falls back to inline HTML if report file not found.
    """
    if not SMTP_USER or not SMTP_PASS:
        return

    analysis = inc.get("analysis", {})
    snow     = analysis.get("servicenow_ticket", {})
    inc_id   = inc.get("incident_id", "")
    title    = inc.get("title", "")
    priority = snow.get("priority", "1")
    p_label  = f"P{priority} — Critical" if priority == "1" else f"P{priority} — Major"
    p_color  = "#da1e28" if priority == "1" else "#ff832b"
    ci       = snow.get("cmdb_ci", "")
    group    = snow.get("assignment_group", "")
    pattern  = analysis.get("runbook", {}).get("applies_to", "")
    summary  = analysis.get("summary", "")
    rb       = analysis.get("runbook", {})

    subject = f"[AIOps {p_label}] {snow_num} — {title} — ACTION REQUIRED"

    # ── Try to use full HTML RCA report card ──────────────────────────────────
    html_file = REPORTS_DIR / f"rca_report_{inc_id}.html"
    if html_file.exists():
        # Use the full RCA report card — inject INC number into it
        html_body = html_file.read_text(encoding="utf-8")
        # Inject INC number banner at top of report
        inc_banner = f"""
<div style="font-family:'IBM Plex Sans',sans-serif;background:#0f1b2d;
     padding:16px 32px;margin-bottom:0;">
  <div style="display:flex;align-items:center;justify-content:space-between;">
    <div>
      <div style="color:rgba(255,255,255,0.6);font-size:11px;
           text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">
        ServiceNow Ticket Created
      </div>
      <div style="color:#fff;font-size:28px;font-weight:700;
           font-family:monospace;letter-spacing:3px">{snow_num}</div>
    </div>
    <div style="background:#da1e28;color:#fff;font-size:11px;font-weight:700;
         padding:6px 16px;border-radius:20px;letter-spacing:1px">
      {p_label} — ACTION REQUIRED
    </div>
  </div>
  <div style="color:rgba(255,255,255,0.6);font-size:12px;margin-top:8px;">
    Open Watson Orchestrate → type <strong style="color:#fff">{snow_num}</strong>
    → review runbook → type APPROVE
  </div>
</div>"""
        # Insert banner after <body> tag
        if "<body" in html_body:
            insert_pos = html_body.find(">", html_body.find("<body")) + 1
            html_body  = html_body[:insert_pos] + inc_banner + html_body[insert_pos:]
        print(f"    📄 Using full RCA report card for email body")
    else:
        # Fallback inline HTML if report file not found
        steps_text = ""
        for step in rb.get("steps", [])[:3]:
            if not isinstance(step, dict):
                continue
            cmds = step.get("commands", [{}])
            fc   = cmds[0] if cmds and isinstance(cmds[0], dict) else {}
            steps_text += (
                f'<div style="background:#f4f4f4;border-radius:6px;'
                f'padding:10px 14px;margin-bottom:8px;">'
                f'<div style="font-size:12px;font-weight:600;color:#161616">'
                f'Step {step.get("step_number","")} — {step.get("action","")}</div>'
                f'<code style="font-size:11px;background:#0f1b2d;color:#a8d1ff;'
                f'padding:2px 8px;border-radius:3px;display:inline-block;margin-top:4px">'
                f'{fc.get("command","")}</code>'
                f'</div>'
            )

        html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:0;">
<div style="max-width:700px;margin:0 auto;padding:24px 16px 48px;">
  <div style="background:#0f1b2d;border-radius:12px;padding:24px 28px;margin-bottom:16px;">
    <div style="background:#da1e28;color:#fff;font-size:10px;font-weight:600;
         letter-spacing:1.5px;text-transform:uppercase;padding:3px 10px;
         border-radius:20px;margin-bottom:10px;display:inline-block">
      IBM AIOps — {p_label} Alert
    </div>
    <h1 style="color:#fff;font-size:20px;font-weight:600;margin:0 0 6px">{title}</h1>
    <p style="color:rgba(255,255,255,0.7);font-size:12px;margin:0">
      {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} &nbsp;|&nbsp; Model: {MODEL_ID}
    </p>
  </div>
  <div style="background:#fff;border:2px solid {p_color};border-radius:8px;
       margin-bottom:16px;overflow:hidden;">
    <div style="background:{p_color};padding:14px 20px;">
      <div style="font-size:11px;color:rgba(255,255,255,0.8);margin-bottom:4px">
        ServiceNow Ticket
      </div>
      <div style="font-size:32px;font-weight:700;color:#fff;
           font-family:monospace;letter-spacing:2px">{snow_num}</div>
    </div>
    <div style="padding:14px 20px;background:#fff8f8;">
      <div style="font-size:13px;color:#161616;margin-bottom:4px">
        <strong>{p_label}</strong> &nbsp;|&nbsp; CI: {ci} &nbsp;|&nbsp; {group}
      </div>
      <div style="font-size:13px;color:#525252">Pattern: {pattern}</div>
      <div style="margin-top:12px;padding:10px 14px;background:#e8daff;border-radius:6px;">
        <strong style="font-size:13px;color:#3c3489">ACTION REQUIRED:</strong>
        <span style="font-size:13px;color:#3c3489">
          Open Watson Orchestrate → AIOps_Incident Resolution Manager →
          type <strong>{snow_num}</strong> → review runbook → type APPROVE
        </span>
        <div style="margin-top:8px;">
          <a href=f"{SNOW_INSTANCE_URL}/nav_to.do?uri=incident.do?sysparm_query=number%3D{snow_num}"
             style="background:#0f62fe;color:#fff;font-size:12px;font-weight:600;
             padding:6px 16px;border-radius:4px;text-decoration:none;
             display:inline-block;margin-right:8px">
             View Ticket {snow_num}
          </a>
          <a href=WO_UI_URL
             style="background:#6929c4;color:#fff;font-size:12px;font-weight:600;
             padding:6px 16px;border-radius:4px;text-decoration:none;
             display:inline-block">
             Open Watson Orchestrate
          </a>
        </div>
      </div>
    </div>
  </div>
  <div style="background:#fff;border-radius:8px;border:1px solid #e0e0e0;
       margin-bottom:16px;overflow:hidden;">
    <div style="padding:12px 18px;border-bottom:1px solid #e0e0e0;background:#f0f4ff;">
      <span style="font-size:12px;font-weight:600;text-transform:uppercase;
            letter-spacing:1px;color:#0f62fe">Summary</span>
    </div>
    <div style="padding:14px 18px;font-size:13px;color:#525252;line-height:1.7">
      {summary}
    </div>
  </div>
  <div style="background:#fff;border-radius:8px;border:1px solid #e0e0e0;
       margin-bottom:16px;overflow:hidden;">
    <div style="padding:12px 18px;border-bottom:1px solid #e0e0e0;background:#d9fbfb;">
      <span style="font-size:12px;font-weight:600;text-transform:uppercase;
            letter-spacing:1px;color:#009d9a">Runbook Preview</span>
    </div>
    <div style="padding:14px 18px;">{steps_text}</div>
  </div>
  <div style="text-align:center;font-size:11px;color:#8d8d8d;
       padding-top:16px;border-top:1px solid #e0e0e0;">
    Generated by AIOps-Multi-Agent-System &nbsp;&#183;&nbsp;
    Powered by IBM watsonx.ai + Watson Orchestrate &nbsp;&#183;&nbsp;
    Model: {MODEL_ID}
  </div>
</div></body></html>"""
        print(f"    📄 RCA report file not found — using inline HTML")

    # ── Build email ───────────────────────────────────────────────────────────
    msg            = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    body_part      = MIMEMultipart("related")
    body_part.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(body_part)

    # Attach HTML report card as file attachment
    if html_file.exists():
        with open(html_file, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="RCA_Report_{inc_id}_{snow_num}.html"',
            )
            msg.attach(part)
        print(f"    📎 Attached: RCA_Report_{inc_id}_{snow_num}.html")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        print(f"    ✓  Email → {EMAIL_TO}  [{snow_num}]")
    except smtplib.SMTPAuthenticationError:
        print("    ✗  SMTP auth failed — check SMTP_USER / SMTP_PASS in .env")
    except Exception as exc:
        print(f"    ✗  Email error: {exc}")


# ── Option C: Send Teams message directly (fallback) ─────────────────────────

def _is_power_automate_url(url: str) -> bool:
    return False  # All modern Microsoft webhook URLs accept Adaptive Cards


def _build_plain_text_payload(inc: dict, snow_num: str) -> dict:
    analysis = inc.get("analysis", {})
    snow     = analysis.get("servicenow_ticket", {})
    rb       = analysis.get("runbook", {})
    title    = inc.get("title", "")
    inc_id   = inc.get("incident_id", "")
    priority = snow.get("priority", "1")
    p_label  = f"P{priority} — Critical" if priority == "1" else f"P{priority} — Major"
    ci       = snow.get("cmdb_ci", "")
    group    = snow.get("assignment_group", "")
    pattern  = rb.get("applies_to", "")
    summary  = analysis.get("summary", "")[:250]

    lines = [
        f"🚨 IBM AIOps — {p_label} Incident Alert", "",
        f"📋 Title      : {title}",
        f"🎫 Ticket     : {snow_num}",
        f"⚡ Priority   : {p_label}",
        f"🖥  CI         : {ci}",
        f"👥 Team       : {group}",
        f"🔗 Pattern    : {pattern}", "",
        f"📝 Summary:", f"{summary}", "",
        f"🔧 Runbook Steps:",
    ]
    for step in rb.get("steps", [])[:3]:
        if not isinstance(step, dict):
            continue
        cmds = step.get("commands", [{}])
        fc   = cmds[0] if cmds and isinstance(cmds[0], dict) else {}
        lines.append(
            f"  Step {step.get('step_number','')} — {step.get('action','')}: "
            f"{fc.get('command','')}"
        )
    lines += [
        "",
        f"✅ ACTION: Open Watson Orchestrate → AIOps_Incident Resolution Manager "
        f"→ type {snow_num} → review runbook → type APPROVE",
    ]
    return {"text": "\n".join(lines)}


def _build_adaptive_card_payload(inc: dict, snow_num: str) -> dict:
    analysis    = inc.get("analysis", {})
    snow        = analysis.get("servicenow_ticket", {})
    rb          = analysis.get("runbook", {})
    title       = inc.get("title", "")
    inc_id      = inc.get("incident_id", "")
    priority    = snow.get("priority", "1")
    p_label     = f"P{priority} — Critical" if priority == "1" else f"P{priority} — Major"
    ci          = snow.get("cmdb_ci", "")
    group       = snow.get("assignment_group", "")
    pattern     = rb.get("applies_to", "")
    summary     = analysis.get("summary", "")[:300]

    steps_facts = []
    for step in rb.get("steps", [])[:3]:
        if not isinstance(step, dict):
            continue
        cmds = step.get("commands", [{}])
        fc   = cmds[0] if cmds and isinstance(cmds[0], dict) else {}
        steps_facts.append({
            "title": f"Step {step.get('step_number','')} — {step.get('action','')}",
            "value": fc.get("command", step.get("what", "")),
        })

    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl":  None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type":    "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type":  "ColumnSet",
                        "style": "emphasis",
                        "columns": [
                            {
                                "type":  "Column",
                                "width": "stretch",
                                "items": [
                                    {"type":"TextBlock",
                                     "text":"IBM AIOps — Incident Alert",
                                     "size":"Small","weight":"Bolder",
                                     "color":"Accent"},
                                    {"type":"TextBlock","text": title,
                                     "size":"Medium","weight":"Bolder",
                                     "wrap":True,"spacing":"None"},
                                ]
                            },
                            {
                                "type":  "Column",
                                "width": "auto",
                                "items": [
                                    {"type":"TextBlock","text": p_label,
                                     "size":"Small","weight":"Bolder",
                                     "color":"Attention",
                                     "horizontalAlignment":"Right"},
                                ]
                            }
                        ]
                    },
                    {
                        "type":    "Container",
                        "style":   "attention",
                        "spacing": "Medium",
                        "items": [{
                            "type": "ColumnSet",
                            "columns": [
                                {
                                    "type":  "Column",
                                    "width": "auto",
                                    "items": [
                                        {"type":"TextBlock",
                                         "text":"ServiceNow Ticket",
                                         "size":"Small","isSubtle":True},
                                        {"type":"TextBlock","text": snow_num,
                                         "size":"ExtraLarge","weight":"Bolder",
                                         "spacing":"None","fontType":"Monospace"},
                                    ]
                                },
                                {
                                    "type":  "Column",
                                    "width": "stretch",
                                    "items": [
                                        {"type":"TextBlock",
                                         "text": f"CI: {ci}",
                                         "size":"Small",
                                         "horizontalAlignment":"Right"},
                                        {"type":"TextBlock","text": group,
                                         "size":"Small","isSubtle":True,
                                         "horizontalAlignment":"Right",
                                         "spacing":"None"},
                                    ]
                                }
                            ]
                        }]
                    },
                    {"type":"FactSet","spacing":"Medium","facts":[
                        {"title":"Incident",   "value": inc_id},
                        {"title":"CI / Asset", "value": ci},
                        {"title":"Pattern",    "value": pattern},
                        {"title":"Category",   "value": snow.get("category","")},
                        {"title":"Confidence", "value": analysis.get("confidence","").upper()},
                    ]},
                    {"type":"TextBlock","text":"Summary",
                     "weight":"Bolder","spacing":"Medium"},
                    {"type":"TextBlock","text": summary,
                     "wrap":True,"size":"Small"},
                    {"type":"TextBlock","text":"Runbook Steps",
                     "weight":"Bolder","spacing":"Medium"},
                    {"type":"FactSet","facts": steps_facts},
                    {"type":"TextBlock",
                     "text": (
                         f"Open Watson Orchestrate and type **{snow_num}** "
                         f"to review and approve the runbook."
                     ),
                     "wrap":True,"size":"Small",
                     "spacing":"Medium","color":"Accent"},
                ],
                "actions": [
                    {"type":"Action.OpenUrl",
                     "title": f"View Ticket {snow_num}",
                     "url":   (
                         f"{SNOW_INSTANCE_URL}/nav_to.do?uri=incident.do?sysparm_query=number%3D{snow_num}"
                         if snow_num not in ("PENDING-INC", "UNKNOWN", "PENDING")
                         else SNOW_INSTANCE_URL
                     ),
                     "style":"positive"},
                    {"type":"Action.OpenUrl",
                     # View RCA Report — opens the IBM-styled HTML report card
                     # served by the local HTTP server (start_rca_server()).
                     # Falls back to the SNOW incident if server is not running.
                     "title": "View RCA Report",
                     "url":   (
                         f"{_RCA_BASE_URL}/rca_report_{inc_id}.html"
                         if _RCA_BASE_URL
                         else (
                             f"{SNOW_INSTANCE_URL}/"
                             f"incident.do?sysparm_query=number%3D{snow_num}"
                             if snow_num not in ("PENDING-INC", "UNKNOWN", "PENDING")
                             else SNOW_INSTANCE_URL
                         )
                     ),
                     "style":"default"},
                    {"type":"Action.OpenUrl",
                     "title":"Open Watson Orchestrate",
                     "url":   WO_UI_URL,
                     "style":"default"},
                ]
            }
        }]
    }


def send_teams_direct(inc: dict, snow_num: str = "PENDING") -> None:
    """Send one Teams message per incident (fallback)."""
    if not HAS_REQUESTS or not TEAMS_WEBHOOK_URL:
        return

    if _is_power_automate_url(TEAMS_WEBHOOK_URL):
        payload  = _build_plain_text_payload(inc, snow_num)
        url_type = "Power Automate workflow"
    else:
        payload  = _build_adaptive_card_payload(inc, snow_num)
        url_type = "Incoming Webhook (Adaptive Card)"

    try:
        resp = requests.post(
            TEAMS_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code in (200, 202):
            print(f"    ✓  Teams message sent  [{snow_num}]  ({url_type})")
        else:
            print(f"    ✗  Teams error {resp.status_code}: {resp.text[:100]}")
    except Exception as exc:
        print(f"    ✗  Teams error: {exc}")



# ── KB auto-upload to Watson Orchestrate ──────────────────────────────────────

# ── Local RCA Report HTTP server ─────────────────────────────────────────────

def start_rca_server(reports_dir: Path) -> str | None:
    """
    Start a local HTTP server on RCA_SERVER_PORT serving rca_reports/.

    The Teams 'View RCA Report' button links to:
      http://localhost:{RCA_SERVER_PORT}/rca_report_{inc_id}.html

    The server runs in a daemon thread — it stays alive as long as the
    Python process is running. The main() function blocks with 'Press Enter'
    after notifications are sent so the presenter can click the button
    during the demo.

    Returns the base URL string if the server started, None if it failed.
    """
    global _RCA_BASE_URL

    if not reports_dir.exists():
        print(f"  WARN: {reports_dir} not found — RCA server not started")
        return None

    # Check if port is already in use
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("localhost", RCA_SERVER_PORT))
    except OSError:
        # Port busy — another server may already be running
        base = f"http://localhost:{RCA_SERVER_PORT}"
        print(f"  INFO: Port {RCA_SERVER_PORT} already in use — assuming RCA server running")
        _RCA_BASE_URL = base
        return base

    try:
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler,
            directory=str(reports_dir.resolve()),
        )
        # Silence request log lines in the console
        handler.log_message = lambda *a: None

        server = http.server.HTTPServer(("localhost", RCA_SERVER_PORT), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://localhost:{RCA_SERVER_PORT}"
        _RCA_BASE_URL = base
        print(f"  RCA Report Server : {base}/")
        return base
    except Exception as exc:
        print(f"  WARN: RCA server failed to start: {exc}")
        return None


# KB name must match what the AIOps_Incident_Resolution_Manager agent
# references in its Knowledge tab in Watson Orchestrate.
# This name was set when the KB was first created via the UI.
KB_NAME = os.getenv("ORCHESTRATE_KB_NAME", "aiops-incident-patterns-kb")


def upload_kb_docs(kb_dir: Path) -> int:
    """
    Auto-upload KB pattern documents to Watson Orchestrate Knowledge Base.

    IBM ADK docs (developer.watson-orchestrate.ibm.com/knowledge_base/deploy_kb):
      orchestrate knowledge-bases import -f <knowledge-base-file-path>

    The -f flag requires a YAML CONFIGURATION FILE, NOT the .txt document
    directly. The YAML declares spec_version, kind, name, description, and
    lists the document paths under the `documents:` key.

    This function:
      1. Scans kb_documents/ for kb_*.txt pattern files (one per failure pattern)
      2. Generates a single YAML config file referencing all of them
      3. Runs orchestrate knowledge-bases import -f <yaml-config>

    The YAML config (kb_documents/kb_config.yaml) is regenerated on every run
    so newly added patterns are always included. Importing a KB with the same
    name as an existing one updates it — idempotent and safe to re-run.

    ADK environment must already be activated:
      orchestrate env activate hackathon_vmaa --api-key <key>

    Returns 1 if import succeeded, 0 if failed or skipped.
    """
    if not kb_dir.exists():
        print(f"  SKIP KB upload — {kb_dir} not found")
        return 0

    kb_files = sorted(kb_dir.glob("kb_*.txt"))
    if not kb_files:
        print(f"  SKIP KB upload — no kb_*.txt files in {kb_dir}")
        return 0

    print(f"\n[AGENT 3] Auto-uploading KB to Orchestrate ({len(kb_files)} pattern(s))")
    print(f"  KB name : {KB_NAME}")
    print(f"  Docs    : {[f.name for f in kb_files]}")

    # ── Step 1: Generate YAML config file ────────────────────────────────────
    # Confirmed format from IBM ADK docs:
    #   spec_version: v1
    #   kind: knowledge_base
    #   name: <name>
    #   description: |
    #     <text>
    #   documents:
    #     - "path/to/doc.txt"
    #
    # .txt files max 5 MB per IBM ADK docs. Our KB files are well under 1 KB.
    # Each file must have a unique name — our pattern-based naming ensures this.

    doc_lines = "\n".join(
        f'  - "{kb_file.resolve().as_posix()}"' for kb_file in kb_files
    )
    yaml_content = (
        "spec_version: v1\n"
        "kind: knowledge_base\n"
        f"name: {KB_NAME}\n"
        "description: |\n"
        "  AIOps incident resolution knowledge base.\n"
        "  Indexed by failure pattern — one document per pattern.\n"
        "  Auto-generated by AIOps Multi-Agent System.\n"
        f"documents:\n{doc_lines}\n"
    )

    yaml_path = kb_dir / "kb_config.yaml"
    try:
        yaml_path.write_text(yaml_content, encoding="utf-8")
        print(f"  ✓  KB config written : {yaml_path.name}")
        print(f"  Config preview:\n{yaml_content}")
    except Exception as exc:
        print(f"  ✗  Failed to write KB config: {exc}")
        return 0

    # ── Step 2: Import via ADK CLI ────────────────────────────────────────────
    try:
        # PYTHONUTF8=1 ensures CLI stdout/stderr uses UTF-8 on Windows
        # (default Windows pipe encoding cp1252 cannot handle ✓ characters).
        cli_env = os.environ.copy()
        cli_env["PYTHONUTF8"] = "1"
        cli_env["PYTHONIOENCODING"] = "utf-8"

       # Auto-activate ADK environment — pipes API key to the interactive prompt
       # subprocess.run(
       #     ["orchestrate", "env", "activate", ORCHESTRATE_ADK_ENV],
       #     input=ORCHESTRATE_API_KEY,
       #     capture_output=True,
       #     text=True,
       #     encoding="utf-8",
       #    errors="replace",
       #    timeout=30,
       #     env=cli_env,
       # ) */

        result = subprocess.run(
            ["orchestrate", "knowledge-bases", "import", "-f", str(yaml_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=cli_env,
        )

        # ── Full CLI output — print every line, no truncation ────────────
        stdout_lines = (result.stdout or "").strip().splitlines()
        stderr_lines = (result.stderr or "").strip().splitlines()

        if result.returncode == 0:
            print(f"  ✓  KB import succeeded: {KB_NAME} (exit 0)")
        else:
            print(f"  ✗  KB import FAILED (exit {result.returncode})")

        # Print all stdout lines — INFO/WARNING messages from CLI
        for line in stdout_lines:
            if line.strip():
                tag = "WARN" if "WARNING" in line.upper() else "INFO"
                print(f"     [{tag}] {line}")

        # Print stderr lines only if non-empty (usually empty on success)
        for line in stderr_lines:
            if line.strip():
                print(f"     [ERR] {line}")

        # ── Per-document upload summary ───────────────────────────────────
        print(f"\n  KB Document Upload Summary:")
        print(f"  {'─'*56}")
        print(f"  {'File':<40} {'Status':<10} {'KB Status'}")
        print(f"  {'─'*40} {'─'*10} {'─'*12}")
        for kb_file in kb_files:
            # Read STATUS from local file to show what was uploaded
            file_status = "PENDING"
            try:
                content = kb_file.read_text(encoding="utf-8")
                if "STATUS: RESOLVED" in content:
                    file_status = "RESOLVED"
            except Exception:
                pass
            upload_ok = result.returncode == 0
            icon = "✓" if upload_ok else "✗"
            print(f"  {icon} {kb_file.name:<38} {'uploaded' if upload_ok else 'FAILED':<10} {file_status}")
        print(f"  {'─'*56}")

        print(f"     orchestrate env activate {ORCHESTRATE_ADK_ENV} --api-key <key>")
        if result.returncode != 0:
            return 0

        return 1

    except FileNotFoundError:
        print(f"  ✗  'orchestrate' CLI not found — is ADK installed?")
        print(f"     pip install ibm-watsonx-orchestrate")
        print(f"     orchestrate env activate hackathon_vmaa --api-key <key>")
        return 0
    except subprocess.TimeoutExpired:
        print(f"  ✗  KB import timed out after 120s")
        return 0
    except Exception as exc:
        print(f"  ✗  KB import error: {exc}")
        return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent 3 Notifier — bridges Agent 2 to Watson Orchestrate"
    )
    parser.add_argument("--skip-orchestrate", action="store_true",
                        help="Skip Watson Orchestrate API call")
    parser.add_argument("--skip-email",       action="store_true",
                        help="Skip email notifications")
    parser.add_argument("--skip-teams",       action="store_true",
                        help="Skip Teams notifications")
    args = parser.parse_args()

    print("=" * 60)
    print("Agent 3 — Notifier (Agent 2 → Agent 3 Bridge)")
    print(f"{PROJECT_NAME} — {ORGANIZATION_NAME}")
    print("=" * 60)

    if not SNOW_FILE.exists():
        print(f"ERROR: {SNOW_FILE} not found — run agent2_analyst.py first")
        sys.exit(1)
    if not RCA_FILE.exists():
        print(f"ERROR: {RCA_FILE} not found — run agent2_analyst.py first")
        sys.exit(1)

    snow_tickets = json.loads(SNOW_FILE.read_text(encoding="utf-8"))
    results      = json.loads(RCA_FILE.read_text(encoding="utf-8"))
    results_map  = {r["incident_id"]: r for r in results}

    print(f"  Loaded {len(snow_tickets)} ticket(s) from {SNOW_FILE}")

    # ── Step 1: Watson Orchestrate creates SNOW tickets ───────────────────────
    orchestrate_ok = False
    if not args.skip_orchestrate:
        orchestrate_ok = send_to_orchestrate(snow_tickets)

    # ── Step 1b: Auto-upload KB documents to Orchestrate ─────────────────────
    if orchestrate_ok:
        upload_kb_docs(Path("kb_documents"))

    # ── Step 2: Build INC number map from ticket_log.json ────────────────────
    # Read ALL entries from ticket_log.json — accumulates across runs
    # This handles cases where only some tickets were created in this run
    inc_number_map = {}  # inc_id → INC number
    log_file = Path("ticket_log.json")

    if log_file.exists():
        try:
            ticket_log_data = json.loads(log_file.read_text(encoding="utf-8"))
            for entry in ticket_log_data:
                inc_num = entry.get("inc_number", "")
                # Only use real INC numbers — skip UNKNOWN, FAILED, PENDING
                if (entry.get("status") == "created"
                        and inc_num
                        and inc_num not in ("UNKNOWN", "FAILED", "PENDING-INC", "PENDING")):
                    inc_number_map[entry["incident_id"]] = inc_num
            if inc_number_map:
                print(f"\n  INC numbers loaded from {log_file}:")
                for inc_id, inc_num in inc_number_map.items():
                    print(f"    {inc_id:<35} → {inc_num}")
        except Exception as exc:
            print(f"  WARN: Could not read ticket_log.json — {exc}")

    # ── Step 3: Always send email + Teams directly with real INC numbers ───────
    # Direct SMTP + webhook is used regardless of Orchestrate status
    # This ensures reliable notifications for every demo run

    # Start local RCA Report server BEFORE sending Teams so button URLs are live
    print(f"\n[AGENT 3] Starting local RCA Report server...")
    start_rca_server(REPORTS_DIR)

    print(f"\n{'─'*60}")
    print(f"[AGENT 3] Sending notifications (SMTP + Teams webhook)")
    print(f"{'─'*60}")

    email_count = 0
    teams_count = 0

    for ticket in snow_tickets:
        inc_id   = ticket.get("_incident_id", "")
        inc      = results_map.get(inc_id, {})
        snow_num = inc_number_map.get(inc_id, "PENDING-INC")

        if not inc.get("analysis"):
            print(f"  SKIP {inc_id} — no RCA analysis found")
            continue

        print(f"\n  [{inc_id}] → {snow_num}")

        # ── Email via SMTP with HTML RCA card ──────────────────────────────
        if not args.skip_email:
            if SMTP_USER and SMTP_PASS:
                send_email_direct(inc, snow_num)
                email_count += 1
            else:
                print(f"    SKIP email — SMTP_USER/SMTP_PASS not set in .env")

        # ── Teams via webhook ──────────────────────────────────────────────
        if not args.skip_teams:
            if TEAMS_WEBHOOK_URL:
                send_teams_direct(inc, snow_num)
                teams_count += 1
            else:
                print(f"    SKIP Teams — TEAMS_WEBHOOK_URL not set in .env")

    # ── Final Summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Agent 3 Complete — {ORGANIZATION_NAME}")
    print(f"{'='*60}")

    snow_count = len(inc_number_map) if orchestrate_ok else 0

    print(f"\n  Pipeline Results:")
    print(f"  {'─'*40}")
    print(f"  SNOW Tickets (Orchestrate) : {snow_count}/{len(snow_tickets)}")
    print(f"  Emails sent (SMTP)         : {email_count}/{len(snow_tickets)}")
    print(f"  Teams messages (webhook)   : {teams_count}/{len(snow_tickets)}")
    print(f"  {'─'*40}")

    if inc_number_map:
        print(f"\n  Ticket Numbers:")
        for inc_id, inc_num in inc_number_map.items():
            print(f"    {inc_id:<35} → {inc_num}")

    if not orchestrate_ok:
        print(f"\n  ⚠️  Orchestrate did not create tickets.")
        print(f"  SRE received PENDING-INC notifications.")
        print(f"  Manual step: Open Watson Orchestrate → paste snow_ready.json")
    else:
        print(f"\n  ✓  All steps completed successfully")

    print(f"\n  SRE: Open Watson Orchestrate → type INC number → APPROVE runbook")

    # Keep the RCA Report server alive so the Teams button works during the demo
    if _RCA_BASE_URL:
        print(f"\n{'='*60}")
        print(f"  RCA REPORT SERVER RUNNING — DO NOT CLOSE")
        print(f"{'='*60}")
        print(f"  URL     : {_RCA_BASE_URL}/")
        print(f"  Reports : rca_report_{{incident_id}}.html")
        print(f"\n  Click 'View RCA Report' in Teams to open the HTML card.")
        print(f"  Press Enter when demo is complete to stop the server...")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass
        print(f"  RCA server stopped.")


if __name__ == "__main__":
    main()