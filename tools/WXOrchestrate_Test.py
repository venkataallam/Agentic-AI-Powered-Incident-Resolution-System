"""
orchestrate_fix.py
==================
Watson Orchestrate API connection test.

CONFIRMED working configuration:
  Token URL  : https://iam.platform.saas.ibm.com/siusermgr/api/1.0/apikeys/token
  Body       : {"apikey": "..."}  → returns JWT Bearer token

  Endpoint   : https://api.dl.watson-orchestrate.ibm.com/instances/{id}/v1/orchestrate/runs
  Body fix   : content item uses "response_type" not "type" (422 error confirmed this)

.env required:
  ORCHESTRATE_INSTANCE_URL=https://api.dl.watson-orchestrate.ibm.com/instances/20260318-0102-3826-4018-4af1cf54692e
  ORCHESTRATE_AGENT_ID=70e7cd1f-cb5e-4f71-ac13-0a516aab01fb
  ORCHESTRATE_API_KEY=<Watson Orchestrate Settings → API details → Generate API key>

USAGE:
  python orchestrate_fix.py
"""

import os
import time

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ORCHESTRATE_INSTANCE_URL = os.getenv("ORCHESTRATE_INSTANCE_URL", "")
ORCHESTRATE_AGENT_ID     = os.getenv("ORCHESTRATE_AGENT_ID",     "")
ORCHESTRATE_API_KEY      = os.getenv("ORCHESTRATE_API_KEY",      "")

# Confirmed working token endpoint
MCSP_TOKEN_URL = "https://iam.platform.saas.ibm.com/siusermgr/api/1.0/apikeys/token"


def get_mcsp_token(api_key: str) -> str | None:
    """
    Exchange Orchestrate API key for MCSP Bearer token.
    Endpoint confirmed working from manual test.
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
                print(f"  ✓  MCSP token obtained (expires_in: {data.get('expires_in', '?')}s)")
                return token
            else:
                print(f"  ✗  Token field not found in response: {data}")
                return None
        else:
            print(f"  ✗  MCSP token failed {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as exc:
        print(f"  ✗  MCSP token error: {exc}")
        return None


def poll_run(instance_url: str, run_id: str, headers: dict,
             max_wait: int = 120) -> dict | None:
    """
    Poll GET {instance_url}/v1/orchestrate/runs/{run_id}
    until status = completed / failed / cancelled / expired.
    """
    poll_url = f"{instance_url}/v1/orchestrate/runs/{run_id}"
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


def send_to_orchestrate(snow_tickets: list[dict]) -> bool:
    """
    Send AIOps incidents to Watson Orchestrate agent.

    Step 1: Get MCSP token
    Step 2: POST to {instance_url}/v1/orchestrate/runs
            content item uses "response_type" (confirmed from 422 error)
    Step 3: Poll until completed
    """

    missing = []
    if not ORCHESTRATE_INSTANCE_URL: missing.append("ORCHESTRATE_INSTANCE_URL")
    if not ORCHESTRATE_AGENT_ID:     missing.append("ORCHESTRATE_AGENT_ID")
    if not ORCHESTRATE_API_KEY:      missing.append("ORCHESTRATE_API_KEY")

    if missing:
        print(f"  SKIP: Missing .env variables: {', '.join(missing)}")
        return False

    if not HAS_REQUESTS:
        print("  ERROR: pip install requests")
        return False

    # ── Step 1: Get MCSP Bearer token ─────────────────────────────────────────
    print(f"\n[STEP 1] Getting MCSP token")
    print(f"  URL: {MCSP_TOKEN_URL}")
    bearer_token = get_mcsp_token(ORCHESTRATE_API_KEY)
    if not bearer_token:
        print("  ✗  Cannot proceed without token")
        return False

    # ── Step 2: Build confirmed endpoint ──────────────────────────────────────
    # Keep /instances/{id} — confirmed working from 422 response
    # Use /v1/ not /api/v1/ — confirmed from working test
    runs_endpoint = f"{ORCHESTRATE_INSTANCE_URL.rstrip('/')}/v1/orchestrate/runs"

    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type":  "application/json",
    }

    print(f"\n[STEP 2] Sending to Watson Orchestrate")
    print(f"  Endpoint : {runs_endpoint}")
    print(f"  Agent ID : {ORCHESTRATE_AGENT_ID}")

    success_count = 0

    for ticket in snow_tickets:
        inc_id = ticket.get("_incident_id", "unknown")
        title  = ticket.get("_title",       "Unknown Incident")

        instruction = (
            f"Create a ServiceNow incident ticket for this AIOps incident "
            f"and notify the SRE team with the ticket number.\n\n"
            f"Incident: {title}\n"
            f"short_description: {ticket.get('short_description', '')}\n"
            f"description: {ticket.get('description', '')}\n"
            f"priority: {ticket.get('priority', '1')}\n"
            f"urgency: {ticket.get('urgency', '1')}\n"
            f"impact: {ticket.get('impact', '1')}\n"
            f"assignment_group: {ticket.get('assignment_group', '')}\n"
            f"category: {ticket.get('category', '')}\n"
            f"subcategory: {ticket.get('subcategory', '')}\n"
            f"cmdb_ci: {ticket.get('cmdb_ci', '')}\n"
            f"caller_id: {ticket.get('caller_id', 'admin')}\n"
            f"work_notes: {ticket.get('work_notes', '')}\n"
        )

        # FIXED: "response_type" not "type" — confirmed from 422 validation error
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
                        try:
                            content = (
                                result.get("result", {})
                                      .get("data", {})
                                      .get("message", {})
                                      .get("content", [])
                            )
                            if isinstance(content, list) and content:
                                text = content[0].get("text", "")
                                print(f"  [{inc_id}] Agent reply: {text[:200]}")
                            elif isinstance(content, str):
                                print(f"  [{inc_id}] Agent reply: {content[:200]}")
                        except Exception:
                            pass
                        print(f"  [{inc_id}] SUCCESS")
                        success_count += 1
                    else:
                        print(f"  [{inc_id}] Run did not complete")
                else:
                    print(f"  [{inc_id}] No run_id in response: {data}")

            elif resp.status_code == 401:
                print(f"  [{inc_id}] 401 — token rejected")
                print(f"    Response: {resp.text[:300]}")
                return False

            elif resp.status_code == 422:
                print(f"  [{inc_id}] 422 — validation error")
                print(f"    Response: {resp.text[:500]}")
                return False

            elif resp.status_code == 500:
                print(f"  [{inc_id}] 500 — server error")
                print(f"    Response: {resp.text[:500]}")
                return False

            else:
                print(f"  [{inc_id}] {resp.status_code}: {resp.text[:300]}")

        except Exception as exc:
            print(f"  [{inc_id}] Connection error: {exc}")

    total = len(snow_tickets)
    print(f"\n  Result: {success_count}/{total} sent to Orchestrate")
    return success_count == total


# ── Standalone connection test ─────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("Watson Orchestrate — Connection Test")
    print("=" * 55)
    print(f"Instance URL : {ORCHESTRATE_INSTANCE_URL or 'NOT SET'}")
    print(f"Agent ID     : {ORCHESTRATE_AGENT_ID     or 'NOT SET'}")
    print(f"API Key      : {'SET' if ORCHESTRATE_API_KEY else 'NOT SET'}")
    print(f"Token URL    : {MCSP_TOKEN_URL}")
    print()

    if not all([ORCHESTRATE_INSTANCE_URL, ORCHESTRATE_AGENT_ID, ORCHESTRATE_API_KEY]):
        print("ERROR: Set all 3 in .env:")
        print("  ORCHESTRATE_INSTANCE_URL=https://api.dl.watson-orchestrate.ibm.com/instances/20260318-0102-3826-4018-4af1cf54692e")
        print("  ORCHESTRATE_AGENT_ID=70e7cd1f-cb5e-4f71-ac13-0a516aab01fb")
        print("  ORCHESTRATE_API_KEY=<Watson Orchestrate Settings → API details → Generate API key>")
        exit(1)

    test_tickets = [{
        "_incident_id":      "test-connection-001-Venkat",
        "_title":            "Venkat: Test1: Orchestrate API Connection Test",
        "short_description": "Venkat: Test1: AIOps pipeline connectivity test — safe to close",
        "description":       "Venkat: Test1: Test to verify Watson Orchestrate API connectivity.",
        "priority":          "3",
        "urgency":           "3",
        "impact":            "3",
        "assignment_group":  "Network Operations",
        "category":          "Network",
        "subcategory":       "Test",
        "cmdb_ci":           "TEST-NODE-02",
        "caller_id":         "admin",
        "work_notes":        "Auto-generated connection test. Safe to close.",
    }]

    ok = send_to_orchestrate(test_tickets)
    print()
    print("=" * 55)
    if ok:
        print("SUCCESS — ready to run: python agent3_notify.py")
    else:
        print("FAILED — fix errors above first")
    print("=" * 55)