"""
agent2_analyst.py  —  Agent 2: Analyst
=======================================
Agentic AI-Powered Incident Resolution System — Pellera Hackathon 2026

ROLE:
  Agent 2 receives the canonical incident payload from Agent 1,
  searches the KB for similar past resolutions, calls LLM
  to generate RCA + runbook, generates HTML report cards, writes
  snow_ready.json for Agent 3, and generates KB pending documents.

FLOW:
  1. Read watsonx_payload.json  (Agent 1 output)
  2. For each incident:
     a. Search KB for similar RESOLVED past incident
     b. Inject KB context into AIOps_RCA_Agent LLM prompt if found
     c. Call Granite function — generate RCA + runbook (all 32 rules)
     d. Validate output
  3. Generate styled HTML report cards  → rca_reports/
  4. Write snow_ready.json              → Agent 3 handoff
  5. Write KB pending documents         → upload to Orchestrate KB


INPUT:
  watsonx_payload.json

OUTPUT:
  rca_output.json       — Granite RCA analysis
  snow_ready.json       — pre-validated SNOW fields for Agent 3
  rca_reports/*.html    — styled HTML report cards per incident
  kb_documents/*.txt    — KB pending docs (upload to Orchestrate)

USAGE:
  python agent2_analyst.py                                        # watsonx.ai (default)
  python agent2_analyst.py --inference-route watsonx              # explicit watsonx.ai
  python agent2_analyst.py --inference-route orchestrate          # via Watson Orchestrate
  python agent2_analyst.py --inference-route orchestrate --retry-failed
  python agent2_analyst.py --retry-failed                         # retry with default route

INFERENCE ROUTES:
  watsonx     — Direct ibm_watsonx_ai SDK → us-south.ml.cloud.ibm.com
                Requires: WATSONX_API_KEY, WATSONX_PROJECT_ID
                Fails when: token_quota_reached (free-tier monthly limit)
  orchestrate — Watson Orchestrate /v1/orchestrate/runs → AIOps_RCA_Agent (GPT OSS 120B)
                Requires: ORCHESTRATE_API_KEY, ORCHESTRATE_RCA_AGENT_ID
                Full SYSTEM_PROMPT embedded in user turn (agent has no system prompt)

CHANGE LOG:
  2026-03-27 — Added RULE 32: deterministic assignment_group lookup table
               in system prompt. Added resolve_assignment_group() Python
               override in prepare_snow_fields() as a safety net so the
               group is always canonical regardless of model output.
  2026-03-27 — Added --inference-route flag. New route: orchestrate.
               New functions: get_mcsp_token(), call_granite_via_orchestrate().
               Both routes produce identical output schema.
"""

import argparse
import json
import os
import re
import sys
import time
import requests
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Local imports ────────────────────────────────────────────────────────────
try:
    from kb_utils import search_kb_for_similar, write_kb_pending, KB_DIR
except ImportError:
    print("ERROR: kb_utils.py not found in same directory.")
    sys.exit(1)

# ── Configuration ────────────────────────────────────────────────────────────
_CONFIG_DIR = Path(__file__).parent / "config"


def _load_yaml_config(filename: str, fallback: object) -> object:
    try:
        import yaml
        p = _CONFIG_DIR / filename
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8"))
        print(f"  WARN: config/{filename} not found — using defaults")
    except ImportError:
        print("  WARN: PyYAML not installed — pip install pyyaml")
    except Exception as exc:
        print(f"  WARN: config/{filename} error: {exc}")
    return fallback


WATSONX_API_KEY    = os.getenv("WATSONX_API_KEY",    "YOUR_IBM_CLOUD_API_KEY")
WATSONX_PROJECT_ID = os.getenv("WATSONX_PROJECT_ID", "YOUR_WATSONX_PROJECT_ID")
WATSONX_URL        = os.getenv("WATSONX_URL",         "https://us-south.ml.cloud.ibm.com")
#MODEL_ID           = "ibm/granite-4-h-small"
MODEL_ID           = "GPT OSS 120B"

# ── Watson Orchestrate Route Config ──────────────────────────────────────────
# Used when --inference-route orchestrate is passed.
# Agent: AIOps_RCA_Agent | Model: GPT OSS 120B | No tools / no system prompt
# Agent has no system prompt configured — full SYSTEM_PROMPT travels in user turn.
ORCHESTRATE_INSTANCE_URL   = os.getenv(
    "ORCHESTRATE_INSTANCE_URL",
    "https://api.dl.watson-orchestrate.ibm.com/instances/20260318-0102-3826-4018-4af1cf54692e",
)
ORCHESTRATE_API_KEY        = os.getenv("ORCHESTRATE_API_KEY", "")
ORCHESTRATE_RCA_AGENT_ID   = os.getenv("ORCHESTRATE_RCA_AGENT_ID", "")
ORCHESTRATE_KB_NAME        = os.getenv(
    "ORCHESTRATE_KB_NAME", "aiops-incident-patterns-kb"
)
MODEL_DISPLAY_NAME         = os.getenv("MODEL_DISPLAY_NAME", "GPT OSS 120B")
ORGANIZATION_NAME          = os.getenv("ORGANIZATION_NAME",  "Pellera Hackathon 2026")
PROJECT_NAME               = os.getenv("PROJECT_NAME",       "Agentic AI-Powered Incident Resolution System")
# ^ Must match the KB name created by Agent 3 in Watson Orchestrate exactly.

# MCSP token endpoint — confirmed working in agent3_notify.py (March 23, 2026)
# NOTE: endpoint is /apikeys/token (confirmed working in agent3_notify.py)
MCSP_TOKEN_URL = "https://iam.platform.saas.ibm.com/siusermgr/api/1.0/apikeys/token"

# Module-level MCSP token cache — reused across all incidents in one run.
# Avoids one token exchange per incident (3 incidents = 1 exchange, not 3).
# Valid for 90 minutes; token TTL is 2 hours.
_rag_mcsp_token: dict = {"token": "", "fetched_at": 0.0}

PAYLOAD_FILE  = Path("watsonx_payload.json")
RCA_FILE      = Path("rca_output.json")
SNOW_FILE     = Path("snow_ready.json")
REPORTS_DIR   = Path("rca_reports")

MAX_RETRIES     = 3
RETRY_DELAY_SEC = 5

# ── Runbook Command Validation ───────────────────────────────────────────────
# Catches Granite token-drop corruption (e.g. "[interface_name]" → "Gignet3")
# on the Orchestrate route BEFORE the corrupted value reaches snow_ready.json.
# validate_step_commands() — per-step field checks
# validate_runbook_steps()  — gate called from prepare_snow_fields()

_IOS_XE_EXEC_VERBS = {
    "show", "ping", "traceroute", "debug", "undebug", "clear",
    "reload", "copy", "write", "dir", "more", "terminal",
}
_IOS_XE_CONFIG_VERBS = {
    "configure terminal", "conf t", "conf term",
    "interface", "no", "ip", "hostname", "router",
    "end", "exit",
}


def validate_step_commands(step: dict, step_num: int, inc_id: str) -> list[str]:
    """
    Validate that command and verify_command fields in a runbook step are not
    corrupted by Granite token-dropping on the Orchestrate route.

    Checks:
      1. command field is non-empty
      2. verify_command field is non-empty
      3. CISCO IOS XE: command starts with a known IOS XE verb or is a
         multi-line config sequence starting with 'configure terminal'
      4. CISCO IOS XE: verify_command starts with 'show' (EXEC mode only)
      5. CISCO IOS XE: interface names in commands are not truncated —
         any 'GigabitEthernet' must be followed by a digit; abbreviated
         forms like 'Gignet3' or 'GigE3' are flagged as corruption
      6. verify_expected is not empty when verify_command is set

    Returns list of error strings (empty list = all valid).
    """
    errors = []
    cmd_type = step.get("command_type", "").lower()
    command  = step.get("command", "").strip()
    vcmd     = step.get("verify_command", "").strip()
    vexp     = step.get("verify_expected", "").strip()
    action   = step.get("action", "")

    prefix = f"  [{inc_id}] Step {step_num} ({action})"

    # Check 1: command not empty
    if not command:
        errors.append(f"{prefix}: command field is EMPTY")

    # Check 2: verify_command not empty
    if not vcmd:
        errors.append(f"{prefix}: verify_command field is EMPTY")

    # Checks 3-5: IOS XE specific
    if "cisco" in cmd_type or "ios" in cmd_type:

        # Check 3: command starts with a known IOS XE verb
        first_word = command.split()[0].lower() if command.split() else ""
        is_multiline_config = command.lower().startswith("configure terminal")
        if not is_multiline_config and first_word not in _IOS_XE_EXEC_VERBS:
            if first_word not in {v.split()[0] for v in _IOS_XE_CONFIG_VERBS}:
                errors.append(
                    f"{prefix}: command starts with unexpected verb '{first_word}' "
                    f"for CISCO IOS XE step — possible token corruption"
                )

        # Check 4: verify_command must start with 'show'
        if vcmd and not vcmd.lower().startswith("show"):
            errors.append(
                f"{prefix}: verify_command '{vcmd}' does not start with 'show' "
                f"— verify must be a PRIVILEGED EXEC show command, not a config command"
            )

        # Check 5: GigabitEthernet must not be truncated or abbreviated.
        # Three sub-checks:
        #   5a — any token that contains 'gigabitethernet' (case-insensitive) but
        #        is NOT followed immediately by a digit is a truncated name
        #        (e.g. 'GigabitEtherneX', 'GigabitEthernet', 'GigabitEthernety')
        #   5b — any token that starts with 'Giga' but is not the full valid word
        #        'GigabitEthernet' followed by a digit is a partial token-drop
        #        (e.g. 'GigabitEtherneX', 'Gigabit3')
        #   5c — abbreviated forms like 'Gignet3', 'GigE3', 'Gi3' that are not
        #        valid IOS XE full interface names
        for field_name, field_val in [("command", command), ("verify_command", vcmd)]:
            # 5a: scan every whitespace-separated token that contains 'gigabitethernet'
            for token in re.findall(r'\S*gigabitethernet\S*', field_val, re.IGNORECASE):
                if not re.match(r'^GigabitEthernet\d', token, re.IGNORECASE):
                    errors.append(
                        f"{prefix}: {field_name} contains truncated interface name "
                        f"'{token}' — expected 'GigabitEthernetN' (Granite token drop)"
                    )
            # 5b: scan tokens starting with 'Giga' that are not fully spelled out
            for token in re.findall(r'\bGiga\S*', field_val, re.IGNORECASE):
                if not re.match(r'^GigabitEthernet\d', token, re.IGNORECASE):
                    errors.append(
                        f"{prefix}: {field_name} contains partial/truncated 'Giga*' token "
                        f"'{token}' — expected 'GigabitEthernetN' (Granite token drop)"
                    )
            # 5c: abbreviated forms like 'Gignet3', 'GigE3' that are NOT valid IOS XE
            if re.search(r'\bGig[a-z]*\d+\b', field_val, re.IGNORECASE):
                if not re.search(r'\bGigabitEthernet\d', field_val, re.IGNORECASE):
                    errors.append(
                        f"{prefix}: {field_name} contains abbreviated interface name "
                        f"— IOS XE requires full 'GigabitEthernetN' form, not abbreviations. "
                        f"Value: '{field_val}' (Granite token drop or hallucination)"
                    )

    # Check 6: verify_expected not empty when verify_command is set
    if vcmd and not vexp:
        errors.append(
            f"{prefix}: verify_expected is EMPTY while verify_command is set to '{vcmd}' "
            f"— cannot perform verification without expected output substring"
        )

    # Check 7: IOS-XE interface verify_expected must be a real 'show interfaces' substring.
    #
    # PRINCIPLE — not hardcoded to any specific interface name or device:
    #   IOS-XE 'show interfaces <X>' output format:
    #     Admin down: "<X> is administratively down, line protocol is down (disabled)"
    #     Up:         "<X> is up, line protocol is up (connected)"
    #
    #   The bare pattern "<anything> is down" at end of string NEVER appears
    #   in real output because IOS-XE always inserts "administratively" before
    #   "down" for admin-shutdown interfaces.
    #
    #   This check is scoped to 'show interfaces' verify_commands only — it does
    #   not fire for BGP, routing, or other show commands where "is down" could
    #   legitimately appear in a different context.
    #
    # GENERIC REGEX: catches any interface type without hardcoding:
    #   [interface_name] is down      → FLAG  (no qualifier)
    #   Ten[interface_name]/0/0 is down → FLAG (no qualifier)
    #   Serial0/0 is down             → FLAG  (no qualifier)
    #   FastEthernet0 is down         → FLAG  (no qualifier)
    #   administratively down         → PASS  (correct form)
    #   line protocol is down         → PASS  ("protocol" qualifier present)
    #   line protocol is up           → PASS  (no "is down" pattern)
    #   [interface_name] is up        → PASS  (valid substring of real up output)
    #   [interface_name] is administratively down → PASS ("administratively" present)
    if ("cisco" in cmd_type.lower() or "ios" in cmd_type.lower()):
        if vexp and "show interface" in vcmd.lower():
            # Matches: <non-whitespace> <space> "is" <space> "down" at end of string.
            # The guards prevent false positives for valid IOS-XE forms:
            #   "line protocol is down" → excluded because "protocol" is present
            #   "administratively down" → excluded because "administratively" is present
            _bare_is_down = re.search(
                r'\S+\s+is\s+down\s*$', vexp.strip(), re.IGNORECASE
            )
            _has_qualifier = (
                "administratively" in vexp.lower()
                or "protocol" in vexp.lower()
            )
            if _bare_is_down and not _has_qualifier:
                errors.append(
                    f"{prefix}: verify_expected '{vexp}' uses bare '... is down' "
                    f"which is NOT a real IOS-XE 'show interfaces' substring. "
                    f"Actual output always says 'administratively down' — "
                    f"use 'administratively down' or 'line protocol is down' "
                    f"per Rule A-10. This check is generic and applies to any "
                    f"interface type (GigabitEthernet, TenGigabitEthernet, Serial, etc.)."
                )

    return errors


def validate_runbook_steps(analysis: dict, inc_id: str) -> list[str]:
    """
    Validate ALL runbook steps in a Granite analysis dict before writing to
    snow_ready.json. Called from prepare_snow_fields() as a blocking gate.

    Returns list of all validation errors across all steps (empty = pass).
    """
    all_errors = []
    steps = analysis.get("runbook", {}).get("steps", [])

    if not steps:
        all_errors.append(
            f"  [{inc_id}]: runbook.steps is EMPTY — aborting SNOW preparation"
        )
        return all_errors

    for step in steps:
        step_num = step.get("step_number", "?")
        errors   = validate_step_commands(step, step_num, inc_id)
        all_errors.extend(errors)

    return all_errors


# ── Assignment Group Lookup Table ────────────────────────────────────────────
# Used by BOTH the system prompt (RULE 32) and the Python override function
# resolve_assignment_group() below.
#
# Structure: (dominant_layer, keyword_in_title_lower)  →  canonical_group
#   - keyword=None means "match any title for this layer"
#   - Rules are evaluated top-to-bottom; FIRST match wins
#
_AG_DEFAULTS = [
    (("network", None), "Network Operations"),
    (("infrastructure", None), "Infrastructure Team"),
    (("platform", None), "Platform Engineering"),
    (("application", "auth"), "Platform Team"),
    (("application", "ldap"), "Platform Team"),
    (("application", "login"), "Platform Team"),
    (("application", "identity"), "Platform Team"),
    (("application", "payment"), "Payments Platform Team"),
    (("application", "database"), "Payments Platform Team"),
    (("application", "db"), "Payments Platform Team"),
    (("application", "transaction"), "Payments Platform Team"),
    (("application", None), "Application Support"),
    (("security", None), "Security Operations"),
]


def _build_ag_rules(yaml_data: object) -> list:
    """Convert assignment_groups.yaml to runtime tuples."""
    if not isinstance(yaml_data, dict):
        return _AG_DEFAULTS
    rules = []
    for entry in yaml_data.get("rules", []):
        layer = entry.get("layer", "")
        keyword = entry.get("keyword")
        group = entry.get("assignment_group", "")
        if layer and group:
            rules.append(((layer, keyword), group))
    return rules or _AG_DEFAULTS


ASSIGNMENT_GROUP_RULES = _build_ag_rules(
    _load_yaml_config("assignment_groups.yaml", None)
)

# ── System Prompt — All 32 Rules ─────────────────────────────────────────────
# R1  Return ONLY the JSON object — no text before or after
# R2  Do not use markdown anywhere
# R3  NEVER use **text** or *text* — strictly forbidden
# R4  No code fences or backticks around output
# R5  Base analysis ONLY on payload + KB context provided
# R6  Never greet the user
# R7  Never explain — start directly with {
# R8  Alert IDs and resource names copied exactly from payload
# R9  Each recommended_step must have all 6 fields
# R10 Never truncate output
# R11 Replace every placeholder — never leave null or empty
# R12 Runbook specific to failure pattern — never generic
# R13 Every runbook step: action, what, commands array, verify object
# R14 estimated_resolution_minutes realistic per failure type
# R15 signal_type: latency|errors|traffic|saturation|availability
# R16 severity: Critical|Major|Minor|Warning
# R17 layer: network|infrastructure|platform|application
# R18 incident_type classification per domain
# R19 failure_pattern from allowed values only
# R20 Never invent alert IDs or resource names
# R21 recommended_steps array of objects not strings
# R22 runbook.steps min 3 max 7
# R23 servicenow priority mapping
# R24 confidence: high|medium|low based on evidence
# R25 what, commands, verify separate distinct fields
# R26 Commands fully executable — no placeholders
# R27 command_type: cisco_ios_cli|linux_shell|kubectl|rest_api|manual
# R28 target must be exact resource name from topology
# R29 expected_output specific matchable string
# R30 on_failure: stop_and_escalate|retry_once|continue
# R31 pre_checks and post_validation must have executable commands
# R32 assignment_group MUST come from the fixed lookup table — no other values

# ── System Prompt — watsonx.ai route (FULL schema) ───────────────────────────
# Used by call_granite() only. DO NOT modify for Orchestrate concerns.
# Contains complete schema: recommended_steps, pre_checks, post_validation,
# rollback, nested escalation{}, nested commands[]/verify{} per step.

# ── SYSTEM_PROMPT_WATSONX — Regenerated for agent2_analyst.py ────────────────
#
# PASTE THIS to replace SYSTEM_PROMPT_WATSONX in agent2_analyst.py
#
# Schema: FULL watsonx.ai schema — nested commands[], nested verify{},
#         pre_checks[], post_validation[], rollback{}, nested escalation{}.
#         This is the HTML report card / KB document quality schema.
#         The SNOW description is built by build_snow_description() which
#         reads the flat runbook.steps fields — those fields MUST also be
#         present in every step even in this nested schema.
#
# Used by: call_granite() — direct ibm_watsonx_ai SDK, <|system|> turn
# Token limit: 4096 max_new_tokens — no truncation concern for this route
#
# CHANGES vs previous version:
#
#   IOS-XE MODE FIX (most critical — root cause of all verify failures):
#     Previous: "use separate steps for each mode change: configure terminal /
#               interface X / no shutdown / end"
#     Problem:  This generated 4 separate steps. Each intermediate config step
#               had a verify command (show running-config etc.) that ran while
#               the shell was still in config mode → % Invalid input detected.
#     New rule: ALL config-mode commands for ONE logical action go in ONE step,
#               ending with 'end'. verify_command ALWAYS runs in EXEC mode.
#
#   VERIFY_COMMAND MODE RULE (new):
#     verify_command in both nested verify{} and flat verify_command field must
#     ALWAYS be a PRIVILEGED EXEC show command. Never a config-mode command.
#
#   IOS-XE COMMAND SYNTAX (strengthened):
#     Added plural 'show interfaces' (not 'show interface') rule.
#     Added exact interface name rule ([interface_name], not Gi3).
#     Added no-pipe-on-interface-show rule.
#
#   NETWORK STEP COUNT (fixed):
#     Interface down remediation = exactly 4 steps. Previous prompt produced
#     6 steps by splitting config sequence into individual steps.
#
#   APPLICATION USE CASE (new explicit rules):
#     Linux systemctl, ldapsearch, curl syntax rules with exact verify strings.
#
#   DATABASE USE CASE (new explicit rules):
#     mysqladmin, kubectl rules with exact verify strings.
#
#   FLAT FIELDS IN NESTED SCHEMA (clarified):
#     Every step MUST have both the nested commands[]/verify{} AND the flat
#     command/verify_command/verify_expected fields because build_snow_description()
#     reads the flat fields to build the SNOW ticket description.
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_WATSONX = """KNOWLEDGE BASE — SEARCH FIRST:
Before analyzing the incident payload, search the knowledge base for any RESOLVED past incidents with the same failure pattern. Use the failure_pattern value from the incident payload as your search query. If RESOLVED entries are found, use their validated runbook steps as the primary basis for your runbook — do not deviate from steps that have been confirmed to work. If a KNOWLEDGE BASE CONTEXT section is provided above the incident payload in this message, treat it as pre-retrieved KB context and use it exactly as if you had searched and found it yourself. State in correlation_reasoning ONLY IF KB context was found and used: KB reference: incident [incident_id from KB entry] — failure pattern '[failure_pattern]' — steps previously validated. If no KB results are found and no KNOWLEDGE BASE CONTEXT section is present, generate the runbook from scratch based on the incident evidence.

You are an expert SRE and AIOps analyst with 25 years of \
experience in IBM Cloud Pak for AIOps, Cisco IOS XE network operations on Catalyst \
8000 series devices, Linux system administration, Kubernetes, MySQL/MariaDB databases, \
and IT service management. You generate precise, validated, executable runbooks based \
strictly on observed incident evidence.

Analyze the AIOps incident payload and return ONLY a valid JSON object with the exact \
structure below. Fill every field with real values from the payload. Never leave any \
field empty, null, or as placeholder text. Never truncate output.

If a KNOWLEDGE BASE CONTEXT section is provided above the incident payload, \
use it as reference for runbook steps that have been previously validated. \
Prioritize those steps. Note in correlation_reasoning if KB context was used.

==========================================================================
SECTION A — CISCO IOS XE RULES
Applies when: incident.layer = "network" OR target is a router, switch,
or firewall. These rules OVERRIDE all other instructions for network steps.
==========================================================================

A-1  COMMAND VALIDITY
     Generate only raw Cisco IOS XE CLI commands that can be typed directly
     into a Cisco IOS XE privileged EXEC or configuration mode shell.
     Never output the string "cisco_ios_cli" as a command value.
     Never invent CLI aliases, automation wrappers, or YANG paths.
     Never use placeholders like <interface-id> — use the exact interface
     name from the incident payload or topology (e.g. [interface_name]).

A-2  CLI MODE AWARENESS — CRITICAL
     IOS XE has three distinct CLI modes. Know exactly which mode each
     command requires and never mix modes in a single command string:

     PRIVILEGED EXEC  (prompt: HOSTNAME#)
       Commands: show interfaces X, show ip bgp summary, show ip route,
                 show running-config, show running-config interface X,
                 show ip interface brief, ping X, traceroute X

     GLOBAL CONFIG    (prompt: HOSTNAME(config)#)
       Entered via: configure terminal
       Commands:    hostname X, ip route X X X, no ip route X X X

     INTERFACE CONFIG (prompt: HOSTNAME(config-if)#)
       Entered via:  interface [interface_name]  (from global config)
       Commands:     no shutdown, shutdown, ip address X X, description X

A-3  CONFIG MODE STEP RULE — THE MOST CRITICAL RULE IN THIS ENTIRE PROMPT
     The execute_step tool runs verify_command in the SAME SSH session
     immediately after the main command. If the main command leaves the
     shell in config mode, the verify command will fail with:
       "% Invalid input detected at '^' marker."
     because show commands are not valid in config mode.

     THEREFORE: Every step whose commands enter config mode MUST end with
     the 'end' command as the last command in the commands[] array AND as
     the last line in the flat command field.

     CORRECT — config step ending with end:
       commands: [
         {"seq":1,"command":"configure terminal",...},
         {"seq":2,"command":"interface [interface_name]",...},
         {"seq":3,"command":"no shutdown",...},
         {"seq":4,"command":"end",...}
       ]
       command (flat): "configure terminal\ninterface [interface_name]\nno shutdown\nend"
       verify.command: "show interfaces [interface_name]"
       verify_command (flat): "show interfaces [interface_name]"

     WRONG — do NOT generate these patterns:
       Step: command="configure terminal", verify.command="show running-config | include hostname"
       (FAILS: shell is in HOSTNAME(config)# when verify runs)

       Step: command="interface [interface_name]", verify.command="show running-config interface [interface_name]"
       (FAILS: shell is in HOSTNAME(config-if)# when verify runs)

       Step: command="no shutdown", verify.command="show interfaces [interface_name]"
       (FAILS: shell is still in HOSTNAME(config-if)# when verify runs)

A-4  VERIFY COMMAND MUST ALWAYS BE A PRIVILEGED EXEC SHOW COMMAND
     verify.command and verify_command (flat) must ALWAYS be a PRIVILEGED
     EXEC command. Never use a config-mode command as a verify command.
     The verify runs after 'end' has returned the shell to EXEC mode.

     Valid verify commands:
       show interfaces [interface_name]
       show ip bgp summary
       show ip interface brief
       show running-config interface [interface_name]  (valid in EXEC mode)
       show ip route

     Invalid verify commands (config-mode, will fail):
       interface [interface_name]
       no shutdown
       configure terminal

A-5  PLURAL INTERFACES — USE EXACT IOS XE SYNTAX
     Use PLURAL "show interfaces" (not "show interface") for interface status.
     Valid:   show interfaces [interface_name]
     Invalid: show interface [interface_name] status   (not valid IOS XE)
     Invalid: show interface [interface_name]           (missing 's', unreliable)

A-6  NO PIPE ON INTERFACE SHOW COMMANDS
     Pipe filtering on invoke_shell buffers unreliably for interface commands.
     Use the full show interfaces command and let substring matching detect state.
     Valid:   show interfaces [interface_name]      (returns full interface block)
     Invalid: show interfaces [interface_name] | include line protocol
     Pipe IS acceptable on BGP: show ip bgp summary | include 10.x.x.x

A-7  EXACT INTERFACE NAMES — NO ABBREVIATIONS
     Always use the full interface name from the payload or topology.
     Valid:   [interface_name]
     Invalid: Gi3, Gi0/3, Gig3, GE3, Ethernet3

A-8  COMPOUND CONFIG STEPS — USE NEWLINE SEPARATOR
     In the flat command field, separate multi-line config sequences with \n:
       "configure terminal\ninterface [interface_name]\nno shutdown\nend"
     The tool splits on \n and ;. Newline is the canonical IOS XE format.

A-9  NETWORK RUNBOOK STRUCTURE FOR INTERFACE DOWN — EXACTLY 4 STEPS
     Do NOT generate intermediate steps like "Enter Config Mode", "Select
     Interface", or "Exit Config" as separate steps. These cause verify
     failures because each intermediate step has a verify that runs in
     config mode. The entire config sequence belongs in ONE step.

     STEP 1 — Check Interface (DIAGNOSTIC)
       commands seq 1: show interfaces [interface_name]
       expected_output: administratively down
       verify.command: show interfaces [interface_name]
       verify.expected_output: line protocol is down
       on_failure: retry_once

     STEP 2 — Enable Interface (REMEDIATION — ends with end)
       commands seq 1: configure terminal
       commands seq 2: interface [interface_name]
       commands seq 3: no shutdown
       commands seq 4: end
       flat command: "configure terminal\ninterface [interface_name]\nno shutdown\nend"
       expected_output: [command accepted — no output returned]
       verify.command: show interfaces [interface_name]
       verify.expected_output: line protocol is up
       on_failure: stop_and_escalate

     STEP 3 — Verify Interface Up (CONFIRMATION)
       commands seq 1: show interfaces [interface_name]
       expected_output: line protocol is up
       verify.command: show ip interface brief
       verify.expected_output: [interface_name]
       on_failure: stop_and_escalate

     STEP 4 — Verify Routing Restored (CONFIRMATION — if BGP/routes in incident)
       commands seq 1: show ip bgp summary
       expected_output: Established
       verify.command: show ip bgp summary
       verify.expected_output: Established
       on_failure: continue

     CRITICAL — do NOT add a Verify SNMP step for link_down incidents.
     SNMP polling failure is a SYMPTOM of the interface being down, not a root cause.
     Once Step 2 restores the interface, SNMP polling recovers automatically.
     show snmp only confirms the SNMP agent is configured — it was never disabled.
     A show snmp step is only valid when SNMP agent misconfiguration is the root cause.

A-10 IOS XE VERIFY_EXPECTED SUBSTRINGS — USE THESE EXACT PATTERNS
     These are real substrings from actual IOS XE show command output.
     Use them exactly. Do not invent other substrings.

     Interface administratively down: "administratively down"
     Interface line protocol down:    "line protocol is down"
     Interface line protocol up:      "line protocol is up"
     Interface up/up state:           "[interface_name] is up"
     BGP peer established:            "Established"
     BGP peer not established:        "Active" or "Idle"
     Config hostname line:            "hostname" (appears in running-config)
     Interface in running-config:     "interface [interface_name]"
     IP brief up:                     "[interface_name]" (present in output line)

A-11 DIAGNOSTIC vs REMEDIATION VERIFY RULE
     DIAGNOSTIC step (action verb: check, show, verify, confirm, inspect,
       diagnose, validate, monitor, query):
       verify.expected_output must confirm the FAULT EXISTS (problem is present).
       Example: checking interface is down → verify_expected = "line protocol is down"

     REMEDIATION step (action verb: enable, bring, configure, apply, fix,
       restore, disable, restart, reset, clear, deploy, rollout, update):
       verify.expected_output must confirm the FIX SUCCEEDED (healthy state).
       Example: bringing interface up → verify_expected = "line protocol is up"

     CRITICAL: verify.expected_output must NEVER contradict expected_output
     of the same step. Down state must be confirmed as down. Up as up.

A-12 PRE-CHECKS FOR NETWORK INCIDENTS
     pre_checks must use PRIVILEGED EXEC show commands only.
     Standard network pre-checks:
       PC-1: show interfaces [interface_name] → confirms interface down before attempting fix
       PC-2: show ip route → confirms routing table state before remediation

A-13 ROLLBACK FOR NETWORK INCIDENTS
     If no shutdown brings the interface up but causes instability, rollback is:
       seq 1: configure terminal
       seq 2: interface [interface_name]
       seq 3: shutdown
       seq 4: end
     rollback target: exact device name from topology

A-14 SNMP VERIFICATION — IOS-XE ONLY
     NEVER generate a linux_shell snmpwalk command targeting an external
     monitoring server (e.g. NOC-MONITOR, monitoring-host, nms-server).
     The execute_step tool connects ONLY to Cisco IOS-XE devices via SSH.
     It cannot reach Linux monitoring servers.
     For SNMP verification after interface recovery, use show snmp on the
     router itself.
       VALID:   show snmp
                verify.expected_output: "SNMP agent enabled"
                command_type: CISCO IOS XE  target: exact device name
       INVALID: snmpwalk -v2c -c public <device>  (linux_shell, external host)
       INVALID: snmpwalk targeting NOC-MONITOR or any non-IOS-XE host

==========================================================================
SECTION B — LINUX SHELL RULES
Applies when: incident.layer = "application" or "infrastructure" AND
target is a Linux service (auth-service, [ldap_host_from_topology], etc.)
==========================================================================

B-1  Use systemctl for all Linux service management.
     Status check:  systemctl status <service-name>
     Restart:       systemctl restart <service-name>
     Start:         systemctl start <service-name>
     Stop:          systemctl stop <service-name>

B-2  Linux verify_expected substrings — use these exact patterns:
     Service is failed/stopped (diagnostic): "inactive (dead)" or "failed"
     Service is running (remediation):       "active (running)"

B-3  For LDAP connectivity checks use ldapsearch:
     Command: ldapsearch -x -H ldap://<ldap-host> -b dc=example,dc=com -s base
     verify_expected (healthy): "numResponses: 1"
     verify_expected (failed):  "Can't contact LDAP server"

B-4  For HTTP health checks use curl:
     Command: curl -s -o /dev/null -w "%{http_code}" http://<host>:<port>/health
     verify_expected (healthy): "200"
     verify_expected (failed):  "000" or "503" or "500"

B-5  CPU/memory checks use standard Linux tools:
     CPU:    top -bn1 | grep "Cpu(s)"
     Memory: free -m
     verify_expected: actual measured value substring from the command output

==========================================================================
SECTION C — KUBERNETES RULES
Applies when: incident.layer = "platform" AND target is a Kubernetes pod
==========================================================================

C-1  Always specify namespace in every kubectl command.
     List pods:      kubectl get pods -n <namespace> | grep <service>
     Restart:        kubectl rollout restart deployment/<name> -n <namespace>
     Check rollout:  kubectl rollout status deployment/<name> -n <namespace>
     Describe pod:   kubectl describe pod <pod-name> -n <namespace>

C-2  Kubernetes verify_expected substrings:
     Pod not running (diagnostic): "CrashLoopBackOff" or "Error" or "0/1"
     Pod running (remediation):    "1/1" or "Running"
     Rollout complete:             "successfully rolled out"

==========================================================================
SECTION D — DATABASE RULES
Applies when: incident involves payment-db, mysql, mariadb, or database latency
==========================================================================

D-1  Use mysqladmin for MySQL/MariaDB health and status:
     Status:  mysqladmin -h <host> -u root status
     Ping:    mysqladmin -h <host> -u root ping
     verify_expected (up):   "Uptime:"
     verify_expected (down): "Can't connect"

D-2  Latency verification:
     verify_expected when latency is high:    "Latency:" (shows measured value)
     verify_expected when latency is normal:  "Latency: 1" (under 2s threshold)

==========================================================================
SECTION E — OUTPUT SCHEMA
==========================================================================

IMPORTANT: The runbook.steps array uses the FULL nested schema below.
ALSO IMPORTANT: Every step must ADDITIONALLY include these flat string fields
at the step level (NOT inside commands[]) because build_snow_description()
reads them to generate the ServiceNow ticket description:
  "command"         — same as commands[0].command for single-command steps,
                      or newline-joined sequence for multi-command steps
  "verify_command"  — same as verify.command
  "verify_expected" — same as verify.expected_output
  "on_failure"      — same as commands[0].on_failure

{
  "incident_id": "exact value from payload incident.incident_id",
  "title": "exact value from payload incident.title",
  "created_time": "exact value from payload incident.created_time",
  "source": "cp4aiops",
  "confidence": "high or medium or low",
  "kb_used": true or false,

  "summary": "2-3 sentence executive summary. State what failed, when it started, and what was impacted. Reference all affected downstream services from topology.",

  "root_cause_explanation": "Precise technical explanation citing the probable_cause alert ID, resource names, and timestamps from event_timeline. Name the failure chain pattern explicitly.",

  "correlation_reasoning": "REQUIRED — always fill this field. Write the causal chain using exact timestamps from event_timeline and exact resource names from topology. Show how each alert caused the next. Example: [alert_id_1] at [timestamp_1] caused interface down which triggered [alert_id_N] (SNMP failure) at 09:00:10. OPTIONAL ADDITION — append this sentence ONLY IF the KNOWLEDGE BASE CONTEXT section appeared above the incident payload in this prompt: KB reference: incident [incident_id from KB entry] — failure pattern [failure_pattern] — steps previously validated. If NO Knowledge Base Context section was present, write only the causal chain.",

  "impact_assessment": "Affected downstream services from topology.downstream, active golden signals, overall severity HIGH/MEDIUM/LOW, and business impact statement.",

  "recommended_steps": [
    {
      "order": 1,
      "action": "Short action title 5 words max",
      "description": "What this step does and why in one sentence",
      "command_hint": "exact CLI or shell command string",
      "target": "exact resource name from topology",
      "expected_outcome": "one sentence describing success"
    }
  ],

  "runbook": {
    "runbook_id": "RB-[incident_id]-001",
    "title": "Short descriptive title 5-8 words",
    "applies_to": "MUST be one of these exact values — no other text permitted: polling_failure_cascade | link_down_cascade | routing_protocol_failure | dependency_failure | latency_cascade | resource_exhaustion | hardware_failure_cascade | storage_degradation | service_degradation | unauthorized_access_attempt",
    "estimated_resolution_minutes": 30,

    "pre_checks": [
      {
        "check_id": "PC-1",
        "description": "What to verify before starting remediation",
        "command": "exact PRIVILEGED EXEC show command",
        "command_type": "CISCO IOS XE or linux_shell or kubectl or rest_api or manual",
        "target": "exact resource name from topology",
        "pass_condition": "exact substring from real command output confirming safe to proceed"
      }
    ],

    "steps": [
      {
        "step_number": 1,
        "action": "Action name 3-5 words",
        "what": "What this step does and why in one sentence",

        "commands": [
          {
            "seq": 1,
            "command_type": "CISCO IOS XE or linux_shell or kubectl or rest_api or manual",
            "target": "exact hostname or resource name from topology",
            "command": "exact executable IOS XE or shell command with no placeholders",
            "expected_output": "exact substring expected in command output",
            "timeout_seconds": 30,
            "on_failure": "MUST match the parent step on_failure value. stop_and_escalate for remediation steps. retry_once or continue for diagnostic steps. All seq commands in one step share the same on_failure policy."
          }
        ],

        "verify": {
          "command": "exact PRIVILEGED EXEC show command — NEVER a config-mode command",
          "command_type": "CISCO IOS XE or linux_shell or kubectl or rest_api or manual",
          "target": "exact resource name from topology",
          "expected_output": "exact substring confirming step outcome — see A-10 for IOS XE patterns",
          "timeout_seconds": 30
        },

        "command": "flat field: for single-command steps same as commands[0].command. For multi-command config steps use newline separator: configure terminal\\ninterface [interface_name]\\nno shutdown\\nend",
        "command_type": "CISCO IOS XE or linux_shell or kubectl or rest_api or manual",
        "expected_output": "same as commands[0].expected_output",
        "on_failure": "same as commands[0].on_failure",
        "verify_command": "same as verify.command — must be PRIVILEGED EXEC show command",
        "verify_expected": "same as verify.expected_output"
      }
    ],

    "rollback": {
      "description": "What to do if steps cause further instability or unexpected side effects",
      "commands": [
        {
          "seq": 1,
          "command_type": "CISCO IOS XE or linux_shell or kubectl or rest_api or manual",
          "target": "exact resource name from topology",
          "command": "exact rollback command — for IOS XE must end with end if it enters config mode",
          "timeout_seconds": 30
        }
      ]
    },

    "escalation": {
      "L1": "Who to contact first, at what time threshold, and via what channel",
      "L2": "Who to escalate to if L1 cannot resolve, and at what time threshold",
      "L3": "Vendor or IBM support contact, when, and what artifacts to provide (logs, config, pcap)"
    },

    "post_validation": [
      {
        "check_id": "PV-1",
        "description": "What to confirm after all steps complete",
        "command": "exact PRIVILEGED EXEC show or monitoring command",
        "command_type": "CISCO IOS XE or linux_shell or kubectl or rest_api or manual",
        "target": "exact resource name from topology",
        "expected_output": "exact substring confirming full resolution",
        "timeout_seconds": 30
      }
    ],

    "escalation_l1": "same as escalation.L1 — flat field required for SNOW description builder",
    "escalation_l2": "same as escalation.L2 — flat field required for SNOW description builder",
    "escalation_l3": "same as escalation.L3 — flat field required for SNOW description builder"
  },

  "servicenow_ticket": {
    "short_description": "one line under 160 chars — exact resource name + failure type",
    "category": "Network or Application or Infrastructure or Security",
    "subcategory": "specific failure subcategory matching SNOW OOTB values",
    "priority": "1",
    "urgency": "1",
    "impact": "1",
    "assignment_group": "exact group name from RULE 32 lookup table — no other values",
    "cmdb_ci": "exact root_node value from topology"
  }
}

==========================================================================
SECTION F — STRICT RULES 1-32
All rules must be followed. No exceptions.
==========================================================================

RULE 1:  Return ONLY the JSON object. Start with { and end with }. No text before or after.
RULE 2:  Do not use markdown anywhere in the response.
RULE 3:  NEVER use **text** or *text*. Bold and italic markdown are strictly forbidden.
RULE 4:  No code fences or backticks anywhere in the output.
RULE 5:  Base all analysis ONLY on payload fields and KB context provided. Do not invent facts.
RULE 6:  Never greet. Never say hello, thank you, or any pleasantry.
RULE 7:  Never explain your reasoning. Start the response directly with {.
RULE 8:  Copy alert IDs, resource names, timestamps, and IP addresses exactly from the payload.
RULE 9:  Each recommended_step must have ALL SIX fields: order, action, description,
         command_hint, target, expected_outcome. Each runbook step must have ALL of:
         step_number, action, what, commands[], verify{}, command, command_type,
         expected_output, on_failure, verify_command, verify_expected.
RULE 10: Never truncate output. Complete the entire JSON regardless of length.
         Never use "...", "etc.", or omit steps.
RULE 11: Replace every placeholder with real values from the payload.
RULE 12: Runbook must be specific to this exact failure pattern and these exact resources.
         Never generate a generic runbook.
RULE 13: Every step must have both the nested commands[]/verify{} schema AND the flat
         command/verify_command/verify_expected fields. Both are required.
RULE 14: estimated_resolution_minutes must match the failure type:
           SNMP or polling failure:       15-30 minutes
           Interface or link down:        20-45 minutes
           BGP or routing failure:        30-60 minutes
           Auth or LDAP failure:          20-45 minutes
           Database latency:              30-90 minutes
           CPU or memory exhaustion:      15-30 minutes
           Kubernetes pod failure:        15-30 minutes
RULE 15: signal_type values: latency | errors | traffic | saturation | availability
RULE 16: severity values: Critical | Major | Minor | Warning
RULE 17: layer values: network | infrastructure | platform | application
RULE 18: incident_type values: network_failure | application_failure |
         application_performance_degradation | infrastructure_failure | platform_failure
RULE 19: failure_pattern values: polling_failure_cascade | link_down_cascade |
         routing_protocol_failure | dependency_failure | latency_cascade |
         resource_exhaustion | hardware_failure_cascade | storage_degradation |
         service_degradation | unauthorized_access_attempt
RULE 20: Never invent alert IDs, IP addresses, resource names, hostnames, or service names
         that are not in the payload or topology.
RULE 21: recommended_steps must be an array of objects — not strings.
         runbook.steps must be an array of step objects — not strings.
RULE 22: runbook.steps minimum 3 steps, maximum 7 steps.
RULE 23: servicenow priority: high confidence or P1 = "1", medium = "2", low = "3"
RULE 24: confidence = "high" when probable_cause is present AND event_timeline has evidence.
         confidence = "medium" when probable_cause present but limited timeline evidence.
         confidence = "low" when only inferred from alert correlation.
RULE 25: commands[], verify{}, command, and verify_command are separate distinct fields.
         Never merge them or omit any.
RULE 26: Every command must be fully executable with no angle-bracket placeholders.
         The string "cisco_ios_cli" must never appear as a command value.
RULE 27: command_type values: CISCO IOS XE | linux_shell | kubectl | rest_api | manual
RULE 28: target must be the exact resource name from topology — never a generic string.
RULE 29: expected_output and verify_expected must be specific substrings that actually
         appear in real command output. See Section A-10 for IOS XE patterns.
         See Section B, C, D for Linux, Kubernetes, and database patterns.
RULE 30: on_failure values: stop_and_escalate | retry_once | continue
RULE 31: pre_checks and post_validation must contain executable commands, not descriptions.
         pre_checks commands must be PRIVILEGED EXEC show commands for IOS XE.
         post_validation commands must be PRIVILEGED EXEC show commands for IOS XE.
RULE 32: assignment_group MUST be selected from this exact lookup table.
         Determine the dominant layer from the majority of alert layers in the payload.
         Match dominant layer first, then keyword in incident title (lowercase).
         Use the FIRST matching rule. Never invent or paraphrase a group name.

  | dominant_layer | keyword in title (lowercase) | assignment_group        |
  |----------------|------------------------------|-------------------------|
  | network        | (any)                        | Network Operations      |
  | infrastructure | (any)                        | Infrastructure Team     |
  | platform       | (any)                        | Platform Engineering    |
  | application    | auth                         | Platform Team           |
  | application    | ldap                         | Platform Team           |
  | application    | login                        | Platform Team           |
  | application    | identity                     | Platform Team           |
  | application    | payment                      | Payments Platform Team  |
  | application    | database                     | Payments Platform Team  |
  | application    | db                           | Payments Platform Team  |
  | application    | transaction                  | Payments Platform Team  |
  | application    | (any)                        | Application Support     |
  | security       | (any)                        | Security Operations     |

  Only these exact strings are valid assignment_group values:
    Network Operations | Infrastructure Team | Platform Engineering |
    Platform Team | Payments Platform Team | Application Support |
    Security Operations"""


# ── System Prompt — Watson Orchestrate route (FLAT/SIMPLIFIED schema) ─────────
# Used ONLY by call_granite_via_orchestrate(). Orchestrate truncates at ~4,100
# chars so schema is simplified: flat steps, flat escalation, no pre_checks,
# no post_validation, no rollback, no recommended_steps.
# rca_to_html.py and kb_utils.py handle both schemas via fallback field reading.

# ── SYSTEM_PROMPT_ORCHESTRATE — Drop-in replacement for agent2_analyst.py ─────
#
# PASTE THIS ENTIRE STRING to replace SYSTEM_PROMPT_ORCHESTRATE in agent2_analyst.py
#
# What changed vs the previous version and WHY:
#
# CISCO IOS XE MODE RULES (new, most critical):
#   The previous prompt said "use separate steps, each with a single command"
#   but generated steps like Step 2 = configure terminal with verify_command =
#   'show running-config | include hostname'. This fails because after sending
#   'configure terminal' the shell is in CAT8K(config)# mode. Any 'show' command
#   sent next returns '% Invalid input detected at ^ marker'.
#
#   The new rules enforce:
#     1. Each IOS-XE config-mode step ends with 'end' in the command itself
#        so the shell always returns to EXEC before the verify runs.
#        'configure terminal' alone is no longer a valid step — it must end with
#        'end' OR the next step must continue from config mode with no verify.
#     2. verify_command MUST ALWAYS be a PRIVILEGED EXEC show command.
#        Never a config-mode command. Never 'show running-config' while in config mode.
#     3. Steps that stay entirely in config mode (interface X / no shutdown)
#        must include 'end' as the last command so the verify runs in EXEC mode.
#     4. The tool handles the mode automatically but the prompt must generate
#        correct step structures so the logic is predictable.
#
# VERIFY_EXPECTED RULES (strengthened):
#   Previous prompt allowed verify_expected to be the CURRENT state at step time.
#   This was correct but weakly enforced. The new rules add concrete IOS-XE
#   output examples so Granite knows exactly what strings to use.
#
# APPLICATION + DATABASE RULES (new section):
#   Added explicit rules for linux_shell and kubectl command generation with
#   the same rigour as IOS-XE rules — service names, systemctl syntax,
#   kubectl namespace handling, database check commands.
#
# STEP COUNT (corrected):
#   Network interface remediation requires exactly 4 steps to be executable:
#     1. Check (show) — diagnostic
#     2. Remediate (conf t + iface + no shut + end, as ONE step command)
#     3. Verify interface up (show)
#     4. Verify BGP/routing restored (show)
#   The previous prompt generated 6 steps by splitting conf t, interface X,
#   no shutdown, end into separate steps. This created 4 verify failures because
#   each intermediate config step had a verify that ran in config mode.
#
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_ORCHESTRATE = """KNOWLEDGE BASE — SEARCH FIRST:
Before analyzing the incident payload, search the knowledge base for any RESOLVED past incidents with the same failure pattern. Use the failure_pattern value from the incident payload as your search query. If RESOLVED entries are found, use their validated runbook steps as the primary basis for your runbook — do not deviate from steps that have been confirmed to work. If a KNOWLEDGE BASE CONTEXT section is provided above the incident payload in this message, treat it as pre-retrieved KB context and use it exactly as if you had searched and found it yourself. State in correlation_reasoning ONLY IF KB context was found and used: KB reference: incident [incident_id from KB entry] — failure pattern '[failure_pattern]' — steps previously validated. If no KB results are found and no KNOWLEDGE BASE CONTEXT section is present, generate the runbook from scratch based on the incident evidence.

You are an expert SRE and AIOps analyst with 25 years of experience in IBM Cloud Pak for AIOps, Cisco IOS XE network operations, Linux administration, Kubernetes, and IT service management. You generate precise, executable runbooks validated against real device behaviour.

Analyze the AIOps incident payload and return ONLY a valid JSON object with the exact structure below. Fill every field with real values from the payload. Never leave any field empty, null, or as placeholder text.

If a KNOWLEDGE BASE CONTEXT section is provided above the incident payload, use it as reference for runbook steps that have been previously validated. Prioritize those steps. Note in correlation_reasoning if KB context was used.

=== CISCO IOS XE RULES — MANDATORY FOR ALL NETWORK STEPS ===

These rules apply when incident.layer is "network" or the target is a network device (router, switch, firewall). These override all other instructions.

DEVICE RULES APPLICABILITY:
Apply the CISCO IOS XE rules below (IOS-1 through IOS-13) ONLY when:
  topology.device_os = "cisco_iosxe"  OR  incident.layer = "network"
For application, database, Kubernetes, or Linux incidents where
topology.device_os is not "cisco_iosxe", IGNORE all IOS-N rules.
Use linux_shell or kubectl command types for those incidents instead.

RULE IOS-1: Generate only raw Cisco IOS XE CLI commands. No wrapper syntax. No automation abstractions. No placeholders.

RULE IOS-2: Never output cisco_ios_cli as a command value. The command field must contain the actual IOS XE CLI string.

RULE IOS-3: IOS XE has three CLI modes. Know which mode each command belongs to:
  PRIVILEGED EXEC (prompt ends with #, no parens): show commands, ping, traceroute, debug
  GLOBAL CONFIG   (prompt: hostname(config)#):      configure terminal, hostname, ip route
  INTERFACE CONFIG (prompt: hostname(config-if)#):  interface X, no shutdown, shutdown, ip address

RULE IOS-4: CRITICAL — EVERY config-mode step MUST end with 'end'.
  The execute_step tool runs verify_command AFTER the main command in the SAME SSH session.
  If the main command leaves the shell in config mode, the verify command will fail with
  '% Invalid input detected at ^ marker' because show commands are not valid in config mode.
  THEREFORE: Every step whose command enters config mode MUST include 'end' at the end.

  CORRECT step structure for interface remediation:
    command: "configure terminal\ninterface [interface_name]\nno shutdown\nend"
    verify_command: "show interfaces [interface_name]"
    verify_expected: "line protocol is up"

  WRONG — do NOT generate this:
    command: "configure terminal"
    verify_command: "show running-config | include hostname"
    (FAILS: shell is in config mode when verify runs)

  WRONG — do NOT generate this:
    command: "interface [interface_name]"
    verify_command: "show running-config interface [interface_name]"
    (FAILS: shell is in config-if mode when verify runs)

RULE IOS-5: verify_command MUST ALWAYS be a PRIVILEGED EXEC show command.
  Valid: show interfaces [interface_name]
  Valid: show ip bgp summary
  Valid: show ip interface brief
  Valid: show running-config interface [interface_name]
  Invalid: any command that requires config mode
  Invalid: any command with 'configure terminal' or 'interface X' prefix

RULE IOS-6: Use PLURAL 'show interfaces' (not 'show interface') for interface status.
  Valid:   show interfaces [interface_name]
  Invalid: show interface [interface_name] status    (not valid IOS XE)
  Invalid: show interface [interface_name] | include (pipe unreliable on invoke_shell)

RULE IOS-7: Use exact interface names from the payload or topology. Never use abbreviations.
  Valid:   [interface_name]
  Invalid: Gi3, Gi0/3, GE3

RULE IOS-8: Compound config commands in ONE step use newline separator:
  "configure terminal\ninterface [interface_name]\nno shutdown\nend"
  NOT semicolons: "configure terminal ; interface [interface_name] ; no shutdown ; end"
  The tool splits on both, but newline is canonical IOS XE multi-line format.

RULE IOS-9: Network runbook step structure for interface down — EXACTLY 4 steps:

  STEP 1 — Check Interface (DIAGNOSTIC)
    command:         show interfaces [interface_name]
    expected_output: administratively down
    verify_command:  show interfaces [interface_name]
    verify_expected: line protocol is down
    on_failure:      retry_once

  STEP 2 — Enable Interface (REMEDIATION — config mode, ends with 'end')
    command:         configure terminal\ninterface [interface_name]\nno shutdown\nend
    expected_output: [command accepted — no output returned]
    verify_command:  show interfaces [interface_name]
    verify_expected: line protocol is up
    on_failure:      stop_and_escalate

  STEP 3 — Verify Interface Up (CONFIRMATION)
    command:         show interfaces [interface_name]
    expected_output: line protocol is up
    verify_command:  show ip interface brief
    verify_expected: [interface_name]
    on_failure:      stop_and_escalate

  STEP 4 — Verify Routing/BGP (CONFIRMATION — if BGP present in incident)
    command:         show ip bgp summary
    expected_output: Established
    verify_command:  show ip bgp summary
    verify_expected: Established
    on_failure:      continue

  Do NOT generate intermediate steps like 'Enter Config Mode' or 'Select Interface'
  or 'Exit Config' as separate steps. These cause verify failures.
  All config mode activity MUST be in a single step that ends with 'end'.

RULE IOS-10: verify_expected must be a substring that appears in real IOS XE show command output:
  Interface down state:  "administratively down" or "line protocol is down"
  Interface up state:    "line protocol is up" or "[interface_name] is up"
  BGP established:       "Established"
  BGP not established:   "Active" or "Idle"
  Config hostname:       "hostname CAT8K" (use actual device hostname, not CMDB name)
  Running config iface:  "interface [interface_name]"
  IP brief up:           "[interface_name]" (appears in the output line regardless of state)

RULE IOS-11: For DIAGNOSTIC steps (action contains: check, show, verify, confirm, inspect):
  verify_expected must confirm the FAULT EXISTS (the problem is present).
  Example: checking if interface is down → verify_expected: "line protocol is down"

  For REMEDIATION steps (action contains: enable, bring, configure, apply, fix, restore):
  verify_expected must confirm the FIX SUCCEEDED (the device is in healthy state).
  Example: bringing interface up → verify_expected: "line protocol is up"

  CRITICAL: verify_expected must NEVER contradict expected_output of the same step.

RULE IOS-12: Do not generate verify commands that use pipe (|) on interface commands.
  'show interfaces [interface_name] | include line protocol' may buffer incorrectly.
  Use 'show interfaces [interface_name]' and let substring matching handle it.
  Pipe is acceptable on BGP commands: 'show ip bgp summary | include 10.x.x.x'

RULE IOS-13: For link_down incidents, do NOT add a Verify SNMP runbook step.
  SNMP polling failure is a DOWNSTREAM SYMPTOM of the interface being down.
  Once Step 2 restores the interface, SNMP polling recovers automatically.
  The SNMP agent was never disabled — the route to the NMS was simply unreachable.
  "show snmp" only confirms the agent is configured, not that polling works end-to-end.
  Do NOT generate "show snmp" as a runbook step for link_down incidents.
  ALSO NEVER generate snmpwalk targeting an external monitoring host (e.g. NOC-MONITOR).
  The execute_step tool connects ONLY to Cisco IOS-XE devices — external hosts unreachable.
  If post_validation requires SNMP confirmation: show snmp → "SNMP agent enabled"
  but only as a post_validation check, never as a primary runbook step.

=== LINUX SHELL RULES — FOR APPLICATION AND INFRASTRUCTURE STEPS ===

RULE LNX-1: Use systemctl for service management on Linux targets.
  Check:    systemctl status auth-service
  Start:    systemctl start auth-service
  Restart:  systemctl restart auth-service
  Enable:   systemctl enable auth-service

RULE LNX-2: verify_expected for Linux service states:
  Service down (diagnostic): "inactive (dead)" or "failed"
  Service up (remediation):  "active (running)"

RULE LNX-3: Use ldapsearch for LDAP connectivity checks.
  Command: ldapsearch -x -H ldap://[ldap_host_from_topology] -b dc=example,dc=com -s base
  verify_expected: "numResponses: 1"

RULE LNX-4: Use curl for HTTP health checks.
  Command: curl -s -o /dev/null -w "%{http_code}" http://auth-service:8080/health
  verify_expected (healthy): "200"
  verify_expected (failed):  "000" or "503"

=== KUBERNETES RULES — FOR PLATFORM STEPS ===

RULE K8S-1: Use kubectl for Kubernetes targets. Always specify namespace.
  Check pods:    kubectl get pods -n <namespace> | grep <service-name>
  Restart:       kubectl rollout restart deployment/<service-name> -n <namespace>
  Check rollout: kubectl rollout status deployment/<service-name> -n <namespace>

RULE K8S-2: verify_expected for pod states:
  Pod not running (diagnostic): "CrashLoopBackOff" or "Error" or "0/1"
  Pod running (remediation):    "1/1" or "Running"

=== DATABASE RULES — FOR DATABASE STEPS ===

RULE DB-1: Use mysqladmin for MySQL/MariaDB health checks.
  Command: mysqladmin -h payment-db -u root -p status
  verify_expected: "Uptime:"

RULE DB-2: verify_expected for database latency:
  High latency (diagnostic): "Latency:" with the measured value
  Resolved (remediation):    "Latency: 1" (under threshold)

=== OUTPUT SCHEMA ===

{
  "incident_id": "exact value from payload incident.incident_id",
  "title": "exact value from payload incident.title",
  "created_time": "exact value from payload incident.created_time",
  "source": "cp4aiops",
  "confidence": "high or medium or low",
  "kb_used": true or false,

  "summary": "2-3 sentence executive summary. State what failed, when it started, and what was impacted. Reference all affected downstream services from topology.",

  "root_cause_explanation": "Precise technical explanation citing probable_cause alert ID, resource names, and timestamps from event_timeline. Name the failure chain pattern explicitly.",

  "correlation_reasoning": "REQUIRED — always fill this field. Write the causal chain using exact timestamps from event_timeline and exact resource names from topology. Show how each event caused the next. Example: [alert_id_1] at [timestamp_1] caused interface down which triggered [alert_id_N] (SNMP failure) at 09:00:10. OPTIONAL ADDITION — append this sentence ONLY IF the KNOWLEDGE BASE CONTEXT section appeared above the incident payload in this prompt: KB reference: incident [incident_id from KB entry] — failure pattern [failure_pattern] — steps previously validated. If NO Knowledge Base Context section was present, write only the causal chain.",

  "impact_assessment": "Affected downstream services from topology.downstream, active golden signals, overall severity HIGH/MEDIUM/LOW, business impact.",

  "recommended_steps": [
    {
      "order": 1,
      "action": "Short action title — 5 words max",
      "description": "What this step does and why — one sentence",
      "command_hint": "exact CLI or shell command to run",
      "target": "exact resource name from topology",
      "expected_outcome": "what success looks like in one sentence"
    }
  ],

  "runbook": {
    "runbook_id": "RB-[incident_id]-001",
    "title": "Short descriptive runbook title — 5 to 8 words",
    "applies_to": "MUST be one of these exact values — no other text permitted: polling_failure_cascade | link_down_cascade | routing_protocol_failure | dependency_failure | latency_cascade | resource_exhaustion | hardware_failure_cascade | storage_degradation | service_degradation | unauthorized_access_attempt",
    "estimated_resolution_minutes": 30,
    "escalation_l1": "First escalation — who, condition, and how",
    "escalation_l2": "Second escalation — who and when",
    "escalation_l3": "Vendor or IBM support — when and what artifacts to provide",
    "steps": [
      {
        "step_number": 1,
        "action": "Action name in 3-5 words",
        "what": "What this step does and why — one sentence",
        "command": "exact executable command with no placeholders. For IOS XE config steps use newline separator: configure terminal\\ninterface [interface_name]\\nno shutdown\\nend",
        "target": "exact hostname or resource name from topology",
        "command_type": "CISCO IOS XE or linux_shell or kubectl or rest_api or manual",
        "expected_output": "exact string or pattern expected in output",
        "on_failure": "stop_and_escalate or retry_once or continue",
        "verify_command": "exact PRIVILEGED EXEC show command — NEVER a config-mode command",
        "verify_expected": "exact substring from real show command output confirming step outcome"
      }
    ]
  },

  "servicenow_ticket": {
    "short_description": "one line under 160 chars — resource name + failure type",
    "category": "Network or Application or Infrastructure or Security",
    "subcategory": "specific failure subcategory",
    "priority": "1",
    "urgency": "1",
    "impact": "1",
    "assignment_group": "exact group name from RULE 32 lookup table — no other values allowed",
    "cmdb_ci": "exact root_node value from topology"
  }
}

=== STRICT RULES — ALL MUST BE FOLLOWED ===

RULE 1:  Return ONLY the JSON object. Start with { end with }.
RULE 2:  Do not use markdown anywhere.
RULE 3:  NEVER use **text** or *text*. Strictly forbidden.
RULE 4:  No code fences or backticks around output.
RULE 5:  Base analysis ONLY on payload fields and KB context provided.
RULE 6:  Never greet. Never say hello or thank you.
RULE 7:  Never explain. Start directly with {.
RULE 8:  Alert IDs and resource names copied exactly from payload.
RULE 9:  Each recommended_step must have ALL SIX fields. Each runbook step must have ALL TEN fields.
RULE 10: Never truncate. Output complete JSON. Never use "..." or skip steps.
RULE 11: Replace every placeholder with real payload values.
RULE 12: Runbook must be specific to this failure pattern and resources.
RULE 13: Every runbook step must have all ten flat fields — no nested commands array, no nested verify object.
RULE 14: estimated_resolution_minutes must be realistic:
  SNMP or polling failure: 15-30 min
  Interface or link down: 20-45 min
  BGP or routing failure: 30-60 min
  Auth or LDAP failure: 20-45 min
  Database latency: 30-90 min
  CPU or memory exhaustion: 15-30 min
RULE 15: signal_type: latency|errors|traffic|saturation|availability only.
RULE 16: severity: Critical|Major|Minor|Warning only.
RULE 17: layer: network|infrastructure|platform|application only.
RULE 18: incident_type: network_failure|application_failure|application_performance_degradation|infrastructure_failure|platform_failure.
RULE 19: failure_pattern: polling_failure_cascade|link_down_cascade|routing_protocol_failure|dependency_failure|latency_cascade|resource_exhaustion|hardware_failure_cascade|storage_degradation|service_degradation|unauthorized_access_attempt.
RULE 20: Never invent alert IDs, IPs, resource names, or service names.
RULE 21: recommended_steps must be array of objects not strings. runbook.steps must be array of step objects with flat fields only.
RULE 22: runbook.steps minimum 3, maximum 7.
RULE 23: servicenow priority: confidence high = "1", medium = "2", low = "3".
RULE 24: confidence high = probable_cause present + timeline evidence.
RULE 25: command and verify_command are separate flat string fields per step.
RULE 26: Every command fully executable. No <placeholder> values. No cisco_ios_cli string as command value.
RULE 27: command_type: CISCO IOS XE|linux_shell|kubectl|rest_api|manual.
RULE 28: target must be exact resource name from topology.
RULE 29: expected_output and verify_expected must be specific matchable substrings of actual command output. See IOS-10 for IOS XE examples.
RULE 30: on_failure: stop_and_escalate|retry_once|continue.
RULE 31: escalation_l1, escalation_l2, escalation_l3 are flat string fields in runbook — not a nested object.
RULE 32: assignment_group MUST be selected from this exact lookup table.
  Determine the dominant layer from the majority of alert layers in the payload.
  Match on dominant layer first, then keyword in incident title.
  Use the FIRST matching rule. Never invent or paraphrase a group name.

  | dominant_layer | keyword in title (lowercase) | assignment_group        |
  |----------------|------------------------------|-------------------------|
  | network        | (any)                        | Network Operations      |
  | infrastructure | (any)                        | Infrastructure Team     |
  | platform       | (any)                        | Platform Engineering    |
  | application    | auth                         | Platform Team           |
  | application    | ldap                         | Platform Team           |
  | application    | login                        | Platform Team           |
  | application    | identity                     | Platform Team           |
  | application    | payment                      | Payments Platform Team  |
  | application    | database                     | Payments Platform Team  |
  | application    | db                           | Payments Platform Team  |
  | application    | transaction                  | Payments Platform Team  |
  | application    | (any)                        | Application Support     |
  | security       | (any)                        | Security Operations     |

  Only these exact strings are valid assignment_group values:
    Network Operations | Infrastructure Team | Platform Engineering |
    Platform Team | Payments Platform Team | Application Support |
    Security Operations"""


# ── Backward-compatible alias ─────────────────────────────────────────────────
# call_granite() uses SYSTEM_PROMPT_WATSONX; call_granite_via_orchestrate() uses
# SYSTEM_PROMPT_ORCHESTRATE. SYSTEM_PROMPT alias kept for any external imports.
SYSTEM_PROMPT = SYSTEM_PROMPT_WATSONX


# ── JSON cleaner ─────────────────────────────────────────────────────────────

def _repair_json_strings(text: str) -> str:
    """
    Fix unescaped control characters and invalid escape sequences inside
    JSON string values.

    Handles two classes of model output errors:
    1. Literal control characters (newline, CR, tab) inside string values
       → escaped to \\n, \\r, \\t
    2. Invalid backslash escape sequences e.g. \\' from shell commands
       → JSON only permits: \\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX
       → Any other \\X sequence: strip the backslash, keep the char

    State machine tracks in_string position to avoid modifying JSON structure.
    """
    # Valid single-char JSON escape followers
    VALID_ESCAPES = set('"' + '\\' + '/bfnrtu')

    result    = []
    in_string = False
    i         = 0
    n         = len(text)

    while i < n:
        ch = text[i]

        if in_string:
            if ch == '\\' and i + 1 < n:
                next_ch = text[i + 1]
                if next_ch in VALID_ESCAPES:
                    # Valid escape sequence — copy both chars verbatim
                    result.append(ch)
                    result.append(next_ch)
                    i += 2
                    continue
                else:
                    # Invalid escape e.g. \' from shell commands — drop backslash
                    result.append(next_ch)
                    i += 2
                    continue
            elif ch == '"':
                in_string = False
                result.append(ch)
            elif ch == '\n':
                result.append('\\n')
            elif ch == '\r':
                result.append('\\r')
            elif ch == '\t':
                result.append('\\t')
            elif ord(ch) < 0x20:
                result.append(f'\\u{ord(ch):04x}')
            else:
                result.append(ch)
        else:
            if ch == '"':
                in_string = True
                result.append(ch)
            else:
                result.append(ch)

        i += 1

    return ''.join(result)


def clean_json(raw: str) -> str:
    """
    Clean and repair raw LLM output into valid JSON.

    Step 1: Strip markdown code fences (``` json / ```)
    Step 2: Trim any trailing text after the closing }
    Step 3: Repair unescaped control characters inside string values
    Step 4: Final strip
    """
    text = raw.strip()

    # Step 1 — strip code fences
    for fence in ["```json", "```JSON", "```"]:
        if text.startswith(fence):
            text = text[len(fence):]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # Step 2 — trim trailing content after last closing brace
    if not text.endswith("}"):
        lb = text.rfind("}")
        if lb != -1:
            text = text[:lb + 1]

    # Step 3 — repair unescaped control characters inside string values
    text = _repair_json_strings(text)

    return text.strip()


# ── Smart JSON parser with progressive repair ─────────────────────────────────

def smart_json_loads(text: str) -> dict:
    """
    Parse JSON with progressive, position-targeted repair.

    Root cause (confirmed): granite-4-h-small via Watson Orchestrate drops
    commas between object members in long JSON outputs, producing:
        "Expecting ',' delimiter"
    This is a DIFFERENT error from unescaped newlines ("Invalid control character").
    The existing _repair_json_strings() only fixes control characters — it cannot
    fix missing commas because it has no way to know a comma is structurally required.

    This function uses json.JSONDecodeError.pos (the exact byte position in the
    input string) to make a minimal, targeted fix at each failure point, then
    retries. Each pass fixes exactly ONE problem. Up to MAX_PASSES repairs are
    attempted, covering both missing commas and any control characters that
    _repair_json_strings may have missed due to in_string state drift.

    Handles:
      "Expecting ',' delimiter"  → insert comma after last non-whitespace before pos
      "Invalid control character" → escape the control char at pos
      "Expecting value"           → remove trailing comma before pos
    """
    MAX_PASSES = 20
    for attempt in range(MAX_PASSES):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            pos = exc.pos
            if pos > len(text):
                raise

            ch = text[pos] if pos < len(text) else ''

            if exc.msg.startswith("Expecting ','"):
                # Missing comma between JSON values.
                # pos is where the next value starts (e.g. '"' of next key).
                # Insert comma immediately after the last non-whitespace char
                # before pos so we get: "val",\n  "key" instead of "val"\n  "key"
                insert_at = pos
                while insert_at > 0 and text[insert_at - 1] in ' \t\n\r':
                    insert_at -= 1
                if insert_at == 0:
                    raise  # nothing before pos, cannot fix
                text = text[:insert_at] + ',' + text[insert_at:]

            elif exc.msg.startswith("Invalid control character") or (
                    pos < len(text) and ord(ch) < 0x20):
                # Unescaped control character not caught by _repair_json_strings.
                fixes = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
                fix = fixes.get(ch, f'\\u{ord(ch):04x}')
                text = text[:pos] + fix + text[pos + 1:]

            elif exc.msg.startswith("Expecting value"):
                # Trailing comma — remove the comma immediately before pos
                j = pos - 1
                while j >= 0 and text[j] in ' \t\n\r':
                    j -= 1
                if j >= 0 and text[j] == ',':
                    text = text[:j] + text[j + 1:]
                else:
                    raise

            else:
                raise  # Unknown error type — surface to caller

    raise json.JSONDecodeError(
        f"smart_json_loads: max repair passes ({MAX_PASSES}) exceeded", text, 0)

def resolve_assignment_group(incident_payload: dict, granite_group: str) -> str:
    """
    Return the canonical ServiceNow assignment group for an incident.

    Logic:
      1. Determine the dominant alert layer (majority vote across all alerts).
      2. Walk ASSIGNMENT_GROUP_RULES top-to-bottom.
      3. Return the group for the FIRST rule whose (layer, keyword) matches.
      4. Fall back to granite_group ONLY if no rule matched (should never
         happen for supported layer values).

    This is a deterministic safety net applied AFTER Granite inference so
    the group is always canonical even when the model drifts across runs.
    """
    title  = incident_payload.get("incident", {}).get("title", "").lower()
    alerts = incident_payload.get("alerts", [])
    layers = [a.get("layer", "").lower() for a in alerts if a.get("layer")]

    # Dominant layer = most frequent; empty payload → empty string
    dominant_layer = max(set(layers), key=layers.count) if layers else ""

    for (rule_layer, keyword), group in ASSIGNMENT_GROUP_RULES:
        if dominant_layer == rule_layer:
            if keyword is None or keyword in title:
                return group

    # Safety fallback — log so it's visible during development
    print(f"  WARN resolve_assignment_group: no rule matched "
          f"(layer='{dominant_layer}', title='{title[:60]}') "
          f"— keeping Granite value '{granite_group}'")
    return granite_group


# ── ServiceNow field validation ───────────────────────────────────────────────

# Valid assignment groups — used in validation to catch stale Granite values
# that slipped past the override (e.g. if incident_payload was unavailable).
VALID_ASSIGNMENT_GROUPS = {
    "Network Operations",
    "Infrastructure Team",
    "Platform Engineering",
    "Platform Team",
    "Payments Platform Team",
    "Application Support",
    "Security Operations",
}

def validate_snow_ticket(ticket: dict) -> list[str]:
    warnings = []
    required = ["short_description", "description", "category",
                "assignment_group", "cmdb_ci", "urgency", "impact"]
    for f in required:
        if not ticket.get(f, "").strip():
            warnings.append(f"EMPTY: {f}")
    sd = ticket.get("short_description", "")
    if len(sd) > 160:
        warnings.append(f"TOO LONG: short_description {len(sd)} chars")
    for f in ["priority", "urgency", "impact"]:
        if ticket.get(f) not in {"1", "2", "3", "4"}:
            warnings.append(f"INVALID {f}: '{ticket.get(f)}'")
    ag = ticket.get("assignment_group", "")
    if ag not in VALID_ASSIGNMENT_GROUPS:
        warnings.append(f"INVALID assignment_group: '{ag}' — not in approved list")
    return warnings


# ── Build ServiceNow description from structured RCA fields ───────────────────

def build_snow_description(analysis: dict) -> str:
    """
    Assemble the ServiceNow description from structured Granite RCA fields.

    STRICT RULES — DO NOT MODIFY THIS FUNCTION:
    1. Command fields are copied VERBATIM from Granite's runbook.steps.
       No summarization, no rephrasing, no shortening.
    2. verify_command is copied VERBATIM — never abbreviated or paraphrased.
    3. Multi-line commands (configure terminal\\ninterface X\\nno shutdown\\nend)
       are written as-is. Newline characters are preserved in the description.
    4. This function has no intelligence — it is a mechanical formatter.
       Intelligence belongs in Granite. This function only formats.
    5. If any step field is empty or suspicious, the field is written as-is
       with no substitution — validate_step_commands() flags it upstream.

    The output format is the CONTRACT between Agent 2 (Python) and the
    AIOps Resolution Manager agent (Watson Orchestrate). Any change to
    the format requires updating the agent's behaviour instructions too.
    """
    confidence  = analysis.get("confidence", "").upper()
    summary     = analysis.get("summary", "")
    root_cause  = analysis.get("root_cause_explanation", "")
    correlation = analysis.get("correlation_reasoning", "")
    impact      = analysis.get("impact_assessment", "")
    rb          = analysis.get("runbook", {})

    lines = []

    # ── RCA Analysis sections ────────────────────────────────────────────────
    lines.append(f"[AIOps RCA — Confidence: {confidence}]")
    lines.append("SUMMARY:")
    lines.append(summary)
    lines.append("ROOT CAUSE:")
    lines.append(root_cause)
    lines.append("CORRELATION REASONING:")
    lines.append(correlation)
    lines.append("IMPACT ASSESSMENT:")
    lines.append(impact)

    # ── Runbook header ───────────────────────────────────────────────────────
    lines.append("=== AIOps AUTO-GENERATED RUNBOOK ===")
    lines.append(f"Runbook ID : {rb.get('runbook_id', '')}")
    lines.append(f"Title      : {rb.get('title', '')}")
    lines.append(f"Pattern    : {rb.get('applies_to', '')}")
    lines.append(f"Est. Time  : {rb.get('estimated_resolution_minutes', '')} minutes")

    # ── Resolution steps — VERBATIM flat fields ──────────────────────────────
    # STRICT: use exact field values — no transformation, no strip, no rephrase.
    # The AIOps Resolution Manager agent parses this exact format.
    # Do NOT change field labels, spacing, or the ' -> ' arrow in Verify lines.
    lines.append("--- RESOLUTION STEPS ---")
    for step in rb.get("steps", []):
        command    = step.get("command", "")
        verify_cmd = step.get("verify_command", "")
        verify_exp = step.get("verify_expected", "")
        # Flat target field often missing from model output — fall back to
        # nested commands[0].target or verify.target in that order.
        target = (
            step.get("target", "")
            or (step.get("commands", [{}])[0].get("target", "")
                if step.get("commands") else "")
            or step.get("verify", {}).get("target", "")
        )

        lines.append(f"  STEP {step.get('step_number', '')}: {step.get('action', '')}")
        lines.append(f"    What   : {step.get('what', '')}")
        lines.append(f"    Command: {command}")
        lines.append(f"    Target : {target}")
        lines.append(f"    Type   : {step.get('command_type', '')}")
        lines.append(f"    Expect : {step.get('expected_output', '')}")
        lines.append(f"    OnFail : {step.get('on_failure', '')}")
        # Verify line format: "command on target -> expected"
        # The AIOps agent parses this exact format — do not change spacing or arrows.
        lines.append(
            f"    Verify : {verify_cmd} on {target} -> {verify_exp}"
        )

    # ── Escalation — flat fields ─────────────────────────────────────────────
    lines.append("--- ESCALATION ---")
    lines.append(f"  L1: {rb.get('escalation_l1', '')}")
    lines.append(f"  L2: {rb.get('escalation_l2', '')}")
    lines.append(f"  L3: {rb.get('escalation_l3', '')}")

    # ── Closing Orchestrate marker ───────────────────────────────────────────
    lines.append(
        f"=== Runbook ID: {rb.get('runbook_id', '')} | "
        f"Follow: Watson Orchestrate -> AIOps_Incident Resolution Manager ==="
    )

    return "\n".join(lines)


# ── Prepare snow_ready.json fields ────────────────────────────────────────────

def prepare_snow_fields(results: list[dict],
                        payload: list[dict]) -> list[dict]:
    """
    Build snow_ready.json from Granite RCA results.

    The `payload` argument (Agent 1 output) is used by
    resolve_assignment_group() to deterministically override whatever
    Granite returned for assignment_group.
    """
    print(f"\n[AGENT 2] Preparing ServiceNow fields → {SNOW_FILE}")

    # Build a quick lookup: incident_id → original payload item
    payload_map: dict[str, dict] = {
        p.get("incident", {}).get("incident_id", ""): p
        for p in payload
    }

    snow_tickets: list[dict] = []
    total_warnings = 0

    for inc in results:
        inc_id   = inc.get("incident_id", "")
        analysis = inc.get("analysis")
        if not analysis:
            continue

        # ── STRICT VALIDATION — abort if Granite output is corrupted ──────────
        # validate_runbook_steps() catches Granite token-drop corruption
        # (e.g. "[interface_name]" → "Gignet3") before it propagates into
        # the SNOW description and the AIOps Resolution Manager agent.
        step_errors = validate_runbook_steps(analysis, inc_id)
        if step_errors:
            print(f"\n  ✗  [{inc_id}] RUNBOOK VALIDATION FAILED — "
                  f"refusing to write corrupted data to snow_ready.json")
            for err in step_errors:
                print(f"     ERROR: {err}")
            print(f"  ACTION REQUIRED: re-run agent2_analyst.py to regenerate this incident")
            # Skip this incident — do not write a corrupted ticket
            continue

        snow = analysis.get("servicenow_ticket", {})
        rb   = analysis.get("runbook", {})
        if not snow:
            continue

        # ── Deterministic assignment group override ──────────────────────────
        # Use the original payload for the layer/title decision.
        # Fall back to Granite's value only if the incident isn't in the map.
        original_payload_item = payload_map.get(inc_id, {})
        resolved_group = resolve_assignment_group(
            original_payload_item,
            snow.get("assignment_group", ""),
        )

        # ── Deterministic CI name override ───────────────────────────────────
        # Always source cmdb_ci from topology.root_node in the original payload.
        # Granite can mis-copy or hallucinate CI names under load; the payload
        # topology is the single source of truth for the affected CMDB item.
        resolved_ci = (
            original_payload_item
            .get("topology", {})
            .get("root_node", "")
            .strip()
        ) or snow.get("cmdb_ci", "").strip()

        ticket = {
            # Reference context — NOT sent to ServiceNow API
            "_incident_id":  inc_id,
            "_title":        inc.get("title", ""),
            "_runbook_id":   rb.get("runbook_id", ""),
            "_generated_at": inc.get("generated_at", ""),
            "_kb_used":      analysis.get("kb_used", False),

            # ServiceNow Create Incident API fields
            "short_description": snow.get("short_description", ""),
            # Description is assembled by Python from structured runbook fields.
            # Granite's description field only contains the 4 RCA analysis sections
            # (~300 chars). build_snow_description() adds the full runbook block
            # from the structured runbook object — zero token cost, always complete.
            "description":       build_snow_description(analysis),
            "state":             "1",       # 1 = New
            "caller_id":         "admin",   # UPDATE to your SNOW username

            "category":          snow.get("category", ""),
            "subcategory":       snow.get("subcategory", ""),
            "urgency":           snow.get("urgency", "1"),
            "impact":            snow.get("impact", "1"),
            "priority":          snow.get("priority", "1"),
            "assignment_group":  resolved_group,   # ← always canonical
            "cmdb_ci":           resolved_ci,       # ← always from topology
        }

        warnings = validate_snow_ticket(ticket)
        total_warnings += len(warnings)
        flag = "✓" if not warnings else "⚠"
        print(f"  {flag}  {inc_id} → "
              f"P{ticket['priority']} | "
              f"CI={ticket['cmdb_ci']} | "
              f"Group={ticket['assignment_group']}")
        for w in warnings:
            print(f"     WARN: {w}")

        snow_tickets.append(ticket)

    SNOW_FILE.write_text(
        json.dumps(snow_tickets, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Individual per-incident files
    snow_dir = Path("snow_tickets")
    snow_dir.mkdir(exist_ok=True)
    for ticket in snow_tickets:
        inc_id   = ticket.get("_incident_id", "unknown")
        safe_id  = inc_id.replace("/", "-").replace(" ", "_")
        ind_file = snow_dir / f"snow_{safe_id}.json"
        ind_file.write_text(
            json.dumps(ticket, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(f"\n  snow_ready.json        → {SNOW_FILE}")
    print(f"  snow_tickets/          → {snow_dir.resolve()}")
    print(f"  Tickets prepared   : {len(snow_tickets)}")
    print(f"  Validation warnings: {total_warnings}")
    return snow_tickets


# ── Granite call ──────────────────────────────────────────────────────────────

def call_granite(payload: list[dict]) -> list[dict]:
    print(f"\n[AGENT 2] Calling Granite — {MODEL_ID}")

    try:
        from ibm_watsonx_ai import Credentials
        from ibm_watsonx_ai.foundation_models import ModelInference
    except ImportError:
        print("  ERROR: pip install ibm-watsonx-ai")
        sys.exit(1)

    model = ModelInference(
        model_id   = MODEL_ID,
        credentials= Credentials(url=WATSONX_URL, api_key=WATSONX_API_KEY),
        project_id = WATSONX_PROJECT_ID,
        params     = {
            "max_new_tokens":     4096,
            "temperature":        0.0,
            "repetition_penalty": 1.05,
        },
    )

    results: list[dict] = []

    for i, incident in enumerate(payload, start=1):
        inc_id = incident.get("incident", {}).get("incident_id", f"incident-{i:03d}")
        title  = incident.get("incident", {}).get("title", "")

        # ── RAG retrieval BEFORE Granite call ───────────────────────────────
        # _derive_failure_pattern() maps payload text → structured KB label.
        # query_orchestrate_kb_for_rag() queries Watson Orchestrate KB for
        # RESOLVED documents matching this pattern and injects them into the
        # LLM prompt. Falls back to local kb_documents/ if Orchestrate is
        # unreachable (first run or API unavailable).
        probable        = incident.get("probable_cause") or {}
        incident_type   = incident.get("incident", {}).get("title", "")
        _inc_id         = incident.get("incident", {}).get("incident_id", "")
        _prior = _load_prior_applies_to()
        failure_pattern = _prior.get(_inc_id) or _derive_failure_pattern(probable, incident_type)
        kb_context = query_orchestrate_kb_for_rag(failure_pattern, incident_type)
        kb_used    = bool(kb_context)

        if kb_used:
            print(f"  [{i}/{len(payload)}] {inc_id} "
                  f"... KB match found — enriching prompt", end=" ", flush=True)
        else:
            print(f"  [{i}/{len(payload)}] {inc_id} "
                  f"... no KB match — generating from scratch", end=" ", flush=True)

        # ── Build prompt with optional KB context ────────────────────────────
        user_content = "Analyze this AIOps incident and return the JSON:\n\n"
        if kb_context:
            user_content += kb_context
        user_content += json.dumps(incident, indent=2)

        prompt = (
            f"<|system|>\n{SYSTEM_PROMPT_WATSONX}\n"
            f"<|user|>\n{user_content}\n"
            f"<|assistant|>\n"
        )

        rca = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                raw   = model.generate_text(prompt=prompt)
                clean = clean_json(raw)
                rca   = smart_json_loads(clean)

                required = {
                    "incident_id", "summary", "root_cause_explanation",
                    "correlation_reasoning", "impact_assessment",
                    "runbook", "servicenow_ticket",
                }
                missing = required - set(rca.keys())

                if missing:
                    print(f"WARN missing {missing}", end=" ", flush=True)
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY_SEC)
                        rca = None
                        continue

                steps = rca.get("runbook", {}).get("steps", [])
                if len(steps) < 3 and attempt < MAX_RETRIES:
                    print(f"WARN only {len(steps)} steps", end=" ", flush=True)
                    time.sleep(RETRY_DELAY_SEC)
                    rca = None
                    continue

                rca["kb_used"] = kb_used
                break

            except json.JSONDecodeError as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SEC)
                    rca = None
                else:
                    print(f"JSON error: {exc}")
            except Exception as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SEC)
                    rca = None
                else:
                    print(f"API error: {exc}")

        if rca:
            print("OK")
            results.append({
                "incident_id":  inc_id,
                "title":        title,
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "model_used":   MODEL_ID,
                "analysis":     rca,
            })
        else:
            print("FAILED")
            results.append({
                "incident_id":  inc_id,
                "title":        title,
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "model_used":   MODEL_ID,
                "analysis":     None,
                "error":        "Granite inference failed after max retries",
            })

    RCA_FILE.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n  rca_output.json → {RCA_FILE}")
    return results


# ── MCSP token fetch ──────────────────────────────────────────────────────────

def get_mcsp_token() -> str:
    """
    Exchange ORCHESTRATE_API_KEY for a short-lived MCSP bearer token.

    Confirmed working endpoint (same as agent3_notify.py, tested March 23 2026):
      POST https://iam.platform.saas.ibm.com/siusermgr/api/1.0/apikeys/token
      Body: {"apikey": "<key>"}
      Response field: "token"  (fallback: "access_token")

    NOTE: endpoint is /apikeys/token — confirmed working in agent3_notify.py.

    Raises RuntimeError if the exchange fails so the caller can abort cleanly.
    """
    try:
        resp = requests.post(
            MCSP_TOKEN_URL,
            json={"apikey": ORCHESTRATE_API_KEY},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"MCSP token request failed (network): {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"MCSP token exchange failed — HTTP {resp.status_code}: {resp.text[:300]}"
        )

    data  = resp.json()
    # agent3 confirmed: try "token" first, fall back to "access_token"
    token = data.get("token") or data.get("access_token", "")
    if not token:
        raise RuntimeError(
            f"MCSP token exchange succeeded but neither 'token' nor "
            f"'access_token' found. Raw response: {resp.text[:300]}"
        )
    print(f"  ✓  MCSP token obtained (expires_in: {data.get('expires_in', '?')}s)")
    return token



# ── Failure pattern derivation — maps payload text to KB enum labels ──────────
#
# WHY THIS EXISTS:
#   probable_cause.summary is free text: "SNMP polling failure on CORE-RTR-01..."
#   KB documents store structured labels:  "polling_failure_cascade"
#   Without this mapping, the RAG filter "polling_failure_cascade" in content
#   will never match the free-text summary. This function bridges the two.
#
# SINGLE SOURCE OF TRUTH:
#   This is the ONLY place _PATTERN_KEYWORDS lives. kb_utils.py previously
#   had a duplicate _KB_PATTERN_MAP — that has been removed (Step 4 refactor).
#   generate_kb_pending_docs() calls _derive_failure_pattern() and passes the
#   derived label to write_kb_pending() as failure_pattern_label=.

_PATTERN_KEYWORDS: list[tuple[list[str], str]] = [
    # ([all_keywords_must_appear_in_text], "rule19_enum_label")
    # Evaluated top-to-bottom; first full match wins.
    (["snmp", "polling"],               "polling_failure_cascade"),
    (["polling", "failure"],            "polling_failure_cascade"),
    (["interface", "down"],             "link_down_cascade"),
    (["link", "down"],                  "link_down_cascade"),
    (["bgp", "reset"],                  "routing_protocol_failure"),
    (["bgp", "routing"],                "routing_protocol_failure"),
    (["routing", "protocol"],           "routing_protocol_failure"),
    (["payment", "latency"],            "latency_cascade"),
    (["database", "latency"],           "latency_cascade"),
    (["latency", "cascade"],            "latency_cascade"),
    (["ldap", "auth"],                  "dependency_failure"),
    (["ldap", "failure"],               "dependency_failure"),
    (["authentication", "failure"],     "dependency_failure"),
    (["auth", "service"],               "dependency_failure"),
    (["dependency", "failure"],         "dependency_failure"),
    (["cpu", "exhaustion"],             "resource_exhaustion"),
    (["memory", "exhaustion"],          "resource_exhaustion"),
    (["resource", "exhaustion"],        "resource_exhaustion"),
    (["hardware", "failure"],           "hardware_failure_cascade"),
    (["storage", "degradation"],        "storage_degradation"),
    (["service", "degradation"],        "service_degradation"),
    (["unauthorized", "access"],        "unauthorized_access_attempt"),
]

_RULE19_ENUMS: frozenset[str] = frozenset({
    "polling_failure_cascade", "link_down_cascade", "routing_protocol_failure",
    "dependency_failure", "latency_cascade", "resource_exhaustion",
    "hardware_failure_cascade", "storage_degradation",
    "service_degradation", "unauthorized_access_attempt",
})



def _load_prior_applies_to() -> dict[str, str]:
    """
    Load applies_to values from the previous rca_output.json run.

    Returns a dict mapping incident_id → applies_to (Rule 19 enum).
    Used by call_granite() and call_granite_via_orchestrate() to look up
    the model-verified failure pattern for KB pre-retrieval, bypassing
    the keyword scan on raw probable_cause.summary which may describe
    symptoms (e.g. "SNMP polling failure") rather than root cause
    (e.g. "link_down_cascade").

    Returns empty dict on first run (no prior rca_output.json yet).
    """
    if not RCA_FILE.exists():
        return {}
    try:
        data = json.loads(RCA_FILE.read_text(encoding="utf-8-sig"))
        result = {}
        for entry in (data if isinstance(data, list) else []):
            inc_id    = entry.get("incident_id", "")
            applies   = (
                entry.get("analysis", {})
                     .get("runbook", {})
                     .get("applies_to", "")
            )
            if inc_id and applies and applies in _RULE19_ENUMS:
                result[inc_id] = applies
        return result
    except Exception:
        return {}


def _derive_failure_pattern(probable_cause: dict, incident_title: str) -> str:
    """
    Map probable_cause payload fields to a structured Rule 19 KB pattern label.

    Priority:
      1. probable_cause["failure_pattern"] or ["pattern"] — explicit structured field
      2. Keyword vocabulary scan of combined summary + incident title text
      3. probable_cause["summary"] raw text — broad fallback (title_words in RAG
         filter is the secondary safety net)

    This is the single source of truth for pattern derivation.
    Used by:
      - call_granite()                  → labels the RAG query key
      - call_granite_via_orchestrate()  → same, passes pre-fetched token
      - generate_kb_pending_docs()      → passes label to write_kb_pending()
    """
    # Priority 1: explicit structured field in the payload
    explicit = (
        probable_cause.get("failure_pattern", "")
        or probable_cause.get("pattern", "")
    ).strip()
    if explicit:
        # Accept only if it is a known Rule 19 enum; reject hallucinated values
        return explicit if explicit in _RULE19_ENUMS else ""

    # Priority 2: keyword vocabulary scan
    combined = (
        probable_cause.get("summary", "") + " " + incident_title
    ).lower()
    for keywords, label in _PATTERN_KEYWORDS:
        if all(kw in combined for kw in keywords):
            return label

    # Priority 3: raw summary — RAG title_words filter is the safety net
    return probable_cause.get("summary", "").strip()


# ── RAG: Query Watson Orchestrate KB before LLM inference ────────────────────
#
# WHAT THIS DOES:
#   Agent 2 calls this BEFORE every LLM inference call.
#   It queries the Watson Orchestrate KB for RESOLVED documents matching the
#   current incident failure_pattern.  When found, the document is injected
#   into the LLM prompt so the model references previously validated steps.
#
# WHY THIS IS RAG:
#   Retrieval  = this function (Orchestrate KB REST API)
#   Augmented  = the prompt injection block in call_granite / call_granite_via_orchestrate
#   Generation = the existing LLM call
#
# FAILURE MODES — all handled gracefully:
#   Orchestrate not configured  → fall back to search_kb_for_similar() (local files)
#   Token fetch fails           → fall back to local
#   KB API returns 404          → first run, KB not yet created — run cold, correct
#   No RESOLVED docs found      → run cold — correct for first occurrence
#   Empty failure_pattern       → skip — no useful search key available

def query_orchestrate_kb_for_rag(
    failure_pattern: str,
    incident_title: str,
    mcsp_token: str = "",
) -> str:
    """
    RAG retrieval: query Watson Orchestrate KB for RESOLVED pattern context.

    Returns a formatted context string ready for LLM prompt injection, or ""
    if no match is found or the API is unreachable.  Never raises.

    Args:
        failure_pattern : structured Rule 19 enum from _derive_failure_pattern()
                          e.g. "polling_failure_cascade"
        incident_title  : incident.title from the payload
        mcsp_token      : pre-fetched MCSP token. Pass from
                          call_granite_via_orchestrate() to avoid a second
                          exchange. Leave blank in call_granite() — this
                          function fetches / reuses the cached token.
    """
    global _rag_mcsp_token

    # Guard: Orchestrate must be configured
    if not ORCHESTRATE_API_KEY or not ORCHESTRATE_INSTANCE_URL:
        return search_kb_for_similar(failure_pattern, incident_title)

    # Guard: empty pattern — empty string matches everything, skip
    fp_lower = failure_pattern.strip().lower()
    if not fp_lower:
        print(f"  [KB-RAG] No failure_pattern derived from payload — skipping KB search")
        return ""

    # ── Token: provided > cached > fresh fetch ────────────────────────────────
    token = mcsp_token.strip()
    if not token:
        now = time.time()
        # Reuse cached token if under 90 minutes old (TTL is 2 hours)
        if _rag_mcsp_token["token"] and (now - _rag_mcsp_token["fetched_at"]) < 5400:
            token = _rag_mcsp_token["token"]
        else:
            try:
                token = get_mcsp_token()
                _rag_mcsp_token = {"token": token, "fetched_at": now}
            except RuntimeError as exc:
                print(f"  [KB-RAG] Token fetch failed: {exc} — running cold")
                return search_kb_for_similar(failure_pattern, incident_title)

    headers  = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base_url = ORCHESTRATE_INSTANCE_URL.rstrip("/")

    # ── Step 1: Resolve KB name → ID ─────────────────────────────────────────
    # GET /api/v1/orchestrate/knowledge-bases
    # Confirmed endpoint from IBM API Hub (Watson Orchestrate API).
    # Uses the same /api/v1/orchestrate/ prefix as the KB upload endpoint
    # in agent3_notify.py. The earlier /v1/knowledge_bases path was wrong.
    # If the KB does not exist yet (Agent 3 has not created it), the list
    # returns empty — kb_id stays None, Step 2 uses the name as identifier.
    kb_id = None
    try:
        # Confirmed endpoint from IBM API Hub:
        # GET /api/v1/orchestrate/knowledge-bases — same /api/v1/orchestrate/
        # prefix pattern as the confirmed /v1/orchestrate/runs endpoint.
        resp = requests.get(
            f"{base_url}/api/v1/orchestrate/knowledge-bases",
            headers=headers, timeout=15,
        )
        if resp.status_code == 200:
            data    = resp.json()
            kb_list = data.get("knowledge_bases") or data.get("results") or []
            for kb in (kb_list if isinstance(kb_list, list) else []):
                if isinstance(kb, dict):
                    if (kb.get("name") == ORCHESTRATE_KB_NAME
                            or kb.get("id") == ORCHESTRATE_KB_NAME):
                        kb_id = kb.get("id") or kb.get("knowledge_base_id", "")
                        break
        elif resp.status_code == 403:
            print(f"  [KB-RAG] 403 on KB list — Orchestrate KB REST API "
                  f"access denied. Proceeding with name-based addressing.")
    except Exception as exc:
        print(f"  [KB-RAG] KB list error: {exc}")

    kb_identifier = kb_id if kb_id else ORCHESTRATE_KB_NAME

    # ── Step 2: Retrieve documents ────────────────────────────────────────────
    # GET /v1/knowledge_bases/{id_or_name}/documents
    documents: list = []
    try:
        # Confirmed endpoint pattern from IBM API Hub:
        # GET /api/v1/orchestrate/knowledge-bases/{KB_ID}/documents
        resp = requests.get(
            f"{base_url}/api/v1/orchestrate/knowledge-bases/{kb_identifier}/documents",
            headers=headers, timeout=20,
        )
        if resp.status_code == 200:
            data      = resp.json()
            documents = (
                data.get("documents")
                or data.get("results")
                or (data if isinstance(data, list) else [])
            )
            print(f"  [KB-RAG] Retrieved {len(documents)} document(s) "
                  f"from KB '{ORCHESTRATE_KB_NAME}'")
        elif resp.status_code == 403:
            # 403 on this endpoint = KB has not been created yet in this instance,
            # OR the API key lacks KB read permission.
            # Previous 403 errors were due to using the wrong URL path (/v1/knowledge_bases
            # instead of /api/v1/orchestrate/knowledge-bases).
            # With the correct URL, 403 most likely means: no KB exists yet.
            # Fall back to local kb_documents/ — Agent 3 creates the KB on first run.
            print(f"  [KB-RAG] 403 on /api/v1/orchestrate/knowledge-bases/{kb_identifier}/documents "
                  f"— KB may not exist yet (run Agent 3 first) or API key lacks KB read permission. "
                  f"Falling back to local kb_documents/ search.")
            return search_kb_for_similar(failure_pattern, incident_title)
        elif resp.status_code == 404:
            # 404 means the document-listing REST endpoint is not supported
            # by this Watson Orchestrate instance — NOT that the KB is empty.
            # The KB exists and is uploaded by Agent 3 via CLI successfully.
            # Fall back to local kb_documents/ which is always in sync.
            print(f"  [KB-RAG] Orchestrate KB document listing returned 404 "
                  f"(REST endpoint not exposed) — searching local kb_documents/")
            return search_kb_for_similar(failure_pattern, incident_title)
        elif resp.status_code == 401:
                print(f"  [KB-RAG] 401 — MCSP token expired — searching local kb_documents/")
                return search_kb_for_similar(failure_pattern, incident_title)
        else:
            print(f"  [KB-RAG] Document list HTTP {resp.status_code}: "
                  f"{resp.text[:150]}")
    except Exception as exc:
        print(f"  [KB-RAG] Document retrieval error: {exc} — local fallback")
        return search_kb_for_similar(failure_pattern, incident_title)

    if not documents:
        print(f"  [KB-RAG] Orchestrate KB has no documents — "
              f"searching local kb_documents/ for '{failure_pattern}'")
        return search_kb_for_similar(failure_pattern, incident_title)

    # ── Step 3: Filter RESOLVED documents matching failure_pattern ────────────
    # Only RESOLVED entries feed the LLM. PENDING entries are for SRE lookup.
    # Match on FAILURE_PATTERN_LABEL (written by write_kb_pending via Step 4
    # refactor) OR on the PATTERN free-text line as fallback.
    # Guard: empty fp_lower already blocked above — no risk of "" matching all.
    matched: list[str] = []
    for doc in documents:
        # Extract content — handle multiple Orchestrate response shapes
        if isinstance(doc, dict):
            content = (
                doc.get("content", "")
                or doc.get("text", "")
                or doc.get("body", "")
                or doc.get("page_content", "")
                or ""
            )
            if not content and "document" in doc:
                inner   = doc["document"]
                content = (
                    (inner.get("content", "") or inner.get("text", ""))
                    if isinstance(inner, dict) else str(inner)
                )
        elif isinstance(doc, str):
            content = doc
        else:
            content = str(doc)

        if not content:
            continue

        content_lower = content.lower()

        # Must carry explicit RESOLVED marker written by Agent 3.
        # PENDING docs contain "steps to resolve" — must not pass this gate.
        is_resolved = (
            "status: resolved" in content_lower
            or '"status": "resolved"' in content_lower
            or "status:resolved" in content_lower
        )
        if not is_resolved:
            continue

        # Exact label match on FAILURE_PATTERN_LABEL line (primary — precise)
        # OR on PATTERN free-text line (secondary — for older KB docs without label)
        if fp_lower in content_lower:
            matched.append(content)

    # ── Step 4: Build context injection block ─────────────────────────────────
    if matched:
        # Deduplicate by first 200 chars
        seen: set[str] = set()
        unique: list[str] = []
        for doc in matched:
            key = doc[:200]
            if key not in seen:
                seen.add(key)
                unique.append(doc)

        context_lines = [
            "",
            "KNOWLEDGE BASE CONTEXT — PREVIOUSLY RESOLVED INCIDENTS:",
            "The steps below were validated in a real resolution of the same "
            "failure pattern.",
            "Prioritise these steps in your runbook. Note in "
            "correlation_reasoning: KB context used.",
            "=" * 60,
        ]
        for idx, doc_content in enumerate(unique[:2], start=1):
            context_lines.append(f"[KB RESOLVED ENTRY {idx}]")
            context_lines.append(doc_content[:1800])  # token budget cap
            context_lines.append("=" * 60)
        context_lines += ["END OF KB CONTEXT", ""]

        print(f"  [KB-RAG] ✓  {len(unique)} RESOLVED match(es) for "
              f"'{failure_pattern}' — injecting into LLM prompt")
        return "\n".join(context_lines)

    print(f"  [KB-RAG] No RESOLVED docs matching '{failure_pattern}' "
          f"({len(documents)} total in Orchestrate KB) — searching local kb_documents/")
    return search_kb_for_similar(failure_pattern, incident_title)


# ── Orchestrate run poller ────────────────────────────────────────────────────

def _poll_orchestrate_run(run_id: str, headers: dict,
                          max_wait: int = 180) -> str:
    """
    Poll GET {ORCHESTRATE_INSTANCE_URL}/v1/orchestrate/runs/{run_id}
    until status = completed / failed / cancelled / expired.

    Mirrors agent3_notify.py poll_run() exactly — same endpoint, same
    status values, same interval.  Timeout extended to 180s because RCA
    inference takes longer than ServiceNow ticket creation.

    Returns the plain-text reply from the agent on success, or "" on failure.
    """
    poll_url = f"{ORCHESTRATE_INSTANCE_URL.rstrip('/')}/v1/orchestrate/runs/{run_id}"
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
                    # Extract text — same path as agent3_notify.py
                    # result.data.message.content — join ALL items in case
                    # Orchestrate splits a long response across multiple content objects
                    try:
                        content = (
                            data.get("result", {})
                                .get("data", {})
                                .get("message", {})
                                .get("content", [])
                        )
                        raw_text = ""
                        if isinstance(content, list) and content:
                            # Join all text blocks — model may split across items
                            raw_text = "".join(
                                item.get("text", "")
                                for item in content
                                if isinstance(item, dict)
                            )
                        elif isinstance(content, str):
                            raw_text = content

                        if not raw_text:
                            print(f"    WARN: completed but text is empty")
                            print(f"    Raw result keys: {list(data.get('result', {}).keys())}")
                            print(f"    Content type: {type(content)}, len: {len(content) if content else 0}")

                        return raw_text

                    except Exception as exc:
                        print(f"\n    WARN content extraction error: {exc}")
                    return ""

                elif status in ("failed", "cancelled", "expired"):
                    print(f"{status}")
                    print(f"    Error: {data.get('last_error', 'no details')}")
                    return ""
                else:
                    print(".", end="", flush=True)
                    time.sleep(interval)
                    elapsed += interval

            elif resp.status_code == 401:
                print(f"\n    401 — token expired during poll")
                return ""
            else:
                print(f"\n    Poll error {resp.status_code}: {resp.text[:150]}")
                return ""

        except Exception as exc:
            print(f"\n    Poll error: {exc}")
            return ""

    print(f"\n    Timed out after {max_wait}s")
    return ""


# ── Granite call via Watson Orchestrate ───────────────────────────────────────

def call_granite_via_orchestrate(payload: list[dict]) -> list[dict]:
    """
    Alternative inference route: POST to Watson Orchestrate /v1/orchestrate/runs
    then poll until completed — exactly the same flow as agent3_notify.py.

    Agent  : AIOps_RCA_Agent (ORCHESTRATE_RCA_AGENT_ID from .env)
    Model  : granite-4-h-small
    Auth   : MCSP bearer token fetched once via /apikeys/token

    Confirmed working request body (same format as agent3_notify.py):
      {
        "message": {
          "role": "user",
          "content": [{"response_type": "text", "text": "<prompt>"}]
        },
        "agent_id": "<ORCHESTRATE_RCA_AGENT_ID>"
      }

    Response extraction path (same as agent3_notify.py):
      result.data.message.content[0].text

    The Orchestrate agent has NO system prompt configured, so the full
    SYSTEM_PROMPT is prepended inside the user turn content.

    Output schema is identical to call_granite() — downstream functions
    receive the same list[dict] regardless of which route ran.
    """
    print(f"\n[AGENT 2] Calling Granite via Watson Orchestrate — {MODEL_ID}")
    print(f"  Agent  : AIOps_RCA_Agent ({ORCHESTRATE_RCA_AGENT_ID})")
    print(f"  URL    : {ORCHESTRATE_INSTANCE_URL}/v1/orchestrate/runs")

    # ── Fetch MCSP token once for all incidents ──────────────────────────────
    print(f"  Fetching MCSP token ...", end=" ", flush=True)
    try:
        token = get_mcsp_token()
    except RuntimeError as exc:
        print(f"FAILED\n  ERROR: {exc}")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    # Confirmed working endpoint — same as agent3_notify.py
    runs_url = f"{ORCHESTRATE_INSTANCE_URL.rstrip('/')}/v1/orchestrate/runs"

    results: list[dict] = []

    for i, incident in enumerate(payload, start=1):
        inc_id = incident.get("incident", {}).get("incident_id", f"incident-{i:03d}")
        title  = incident.get("incident", {}).get("title", "")

        # ── RAG retrieval BEFORE Orchestrate call ───────────────────────────
        # Pass the already-fetched `token` to avoid a second MCSP exchange.
        # Same function as the watsonx route — identical output schema.
        probable        = incident.get("probable_cause") or {}
        incident_type   = incident.get("incident", {}).get("title", "")
        _inc_id         = incident.get("incident", {}).get("incident_id", "")
        # device_os from topology — set by Agent 1 from Device Model field
        # Used to add topology context to the prompt so IOS-XE rules
        # are applied only when appropriate.
        device_os       = incident.get("topology", {}).get("device_os", "")
        iface_names     = incident.get("topology", {}).get("interface_names", [])
        _prior = _load_prior_applies_to()
        failure_pattern = _prior.get(_inc_id) or _derive_failure_pattern(probable, incident_type)
        kb_context      = query_orchestrate_kb_for_rag(
            failure_pattern, incident_type, mcsp_token=token)
        kb_used         = bool(kb_context)

        if kb_used:
            print(f"  [{i}/{len(payload)}] {inc_id} "
                  f"... KB match found — enriching prompt")
        else:
            print(f"  [{i}/{len(payload)}] {inc_id} "
                  f"... no KB match — generating from scratch")

        # ── Build user message ───────────────────────────────────────────────
        # Agent has no system prompt — embed SYSTEM_PROMPT inside user turn.
        user_content = (
            f"<INSTRUCTIONS>\n{SYSTEM_PROMPT_ORCHESTRATE}\n\n"
            f"<INCIDENT>\n"
            f"Analyze this AIOps incident and return the JSON:\n\n"
        )
        if kb_context:
            user_content += kb_context
        user_content += json.dumps(incident, indent=2)

        # Confirmed working body format — same as agent3_notify.py
        body = {
            "message": {
                "role":    "user",
                "content": [
                    {
                        "response_type": "text",
                        "text":          user_content,
                    }
                ],
            },
            "agent_id": ORCHESTRATE_RCA_AGENT_ID,
        }

        rca = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print(f"  [{inc_id}] Submitting run (attempt {attempt}) ...",
                      end=" ", flush=True)
                resp = requests.post(
                    runs_url,
                    json=body,
                    headers=headers,
                    timeout=30,
                )

                if resp.status_code == 401:
                    # Token expired — refresh once and retry
                    print(f"401 — token expired, refreshing ...", end=" ", flush=True)
                    token  = get_mcsp_token()
                    headers["Authorization"] = f"Bearer {token}"
                    time.sleep(2)
                    continue

                if resp.status_code != 200:
                    raise Exception(
                        f"Orchestrate HTTP {resp.status_code}: {resp.text[:300]}"
                    )

                data   = resp.json()
                run_id = data.get("run_id")
                if not run_id:
                    raise Exception(
                        f"No run_id in response: {json.dumps(data)[:200]}"
                    )
                print(f"run_id={run_id[:8]}...")

                # ── Poll until completed — same logic as agent3_notify.py ────
                raw_text = _poll_orchestrate_run(run_id, headers)
                if not raw_text:
                    raise Exception("Orchestrate returned empty text after polling")

                clean = clean_json(raw_text)
                # Use smart_json_loads — fixes missing commas AND control chars
                # via progressive position-targeted repair (up to 20 passes).
                # Confirmed root cause: granite-4-h-small via Orchestrate drops
                # commas between object members in long JSON, causing
                # "Expecting ',' delimiter". json.loads() cannot self-heal this.
                rca   = smart_json_loads(clean)

                required = {
                    "incident_id", "summary", "root_cause_explanation",
                    "correlation_reasoning", "impact_assessment",
                    "runbook", "servicenow_ticket",
                }
                missing = required - set(rca.keys())
                if missing:
                    print(f"  WARN missing fields: {missing}", end=" ", flush=True)
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY_SEC)
                        rca = None
                        continue

                steps = rca.get("runbook", {}).get("steps", [])
                if len(steps) < 3 and attempt < MAX_RETRIES:
                    print(f"  WARN only {len(steps)} runbook steps",
                          end=" ", flush=True)
                    time.sleep(RETRY_DELAY_SEC)
                    rca = None
                    continue

                rca["kb_used"] = kb_used
                break

            except json.JSONDecodeError as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SEC)
                    rca = None
                else:
                    # Final failure — show full diagnostic context
                    print(f"  JSON error: {exc.msg} at char {exc.pos}")
                    try:
                        ctx_start = max(0, exc.pos - 80)
                        ctx_end   = min(len(clean), exc.pos + 80)
                        print(f"  Context in cleaned text (chars {ctx_start}-{ctx_end}):")
                        print(f"  {repr(clean[ctx_start:ctx_end])}")
                        print(f"  RAW (first 1200 chars):\n{raw_text[:1200]}")
                    except Exception:
                        pass
            except Exception as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SEC)
                    rca = None
                else:
                    print(f"  API error: {exc}")

        model_label = f"{MODEL_ID} (via orchestrate)"
        if rca:
            print(f"  [{inc_id}] OK")
            results.append({
                "incident_id":  inc_id,
                "title":        title,
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "model_used":   model_label,
                "analysis":     rca,
            })
        else:
            print(f"  [{inc_id}] FAILED")
            results.append({
                "incident_id":  inc_id,
                "title":        title,
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "model_used":   model_label,
                "analysis":     None,
                "error":        "Granite inference via Orchestrate failed after max retries",
            })

    RCA_FILE.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n  rca_output.json → {RCA_FILE}")
    return results

def generate_html(results: list[dict]) -> list[Path]:
    print(f"\n[AGENT 2] Generating HTML report cards → {REPORTS_DIR}/")
    REPORTS_DIR.mkdir(exist_ok=True)

    rca_to_html = Path("rca_to_html.py")
    if not rca_to_html.exists():
        print("  WARNING: rca_to_html.py not found — skipping HTML generation")
        return []

    import subprocess
    subprocess.run(
        [sys.executable, str(rca_to_html)],
        check=True,
    )
    files = list(REPORTS_DIR.glob("*.html"))
    print(f"  Generated {len(files)} HTML file(s)")
    return files


# ── KB pending documents ──────────────────────────────────────────────────────

def generate_kb_pending_docs(results: list[dict],
                              snow_tickets: list[dict]) -> list[Path]:
    """
    Write PENDING KB documents — one per incident.
    These are uploaded to Watson Orchestrate Knowledge Base
    so SRE can type ticket number and Agent 3 finds the runbook.
    STATUS = PENDING until Agent 3 updates to RESOLVED.
    """
    print(f"\n[AGENT 2] Writing KB pending documents → kb_documents/")

    snow_map = {t["_incident_id"]: {"snow_number": t.get("_incident_id",""),
                                     "snow_url": ""}
                for t in snow_tickets}

    # Use placeholder ticket numbers — Agent 3 will have real numbers
    # We write these now for KB upload; Agent 3 updates with real INC number
    files: list[Path] = []
    for inc in results:
        if not inc.get("analysis"):
            continue
        inc_id      = inc.get("incident_id","")
        snow_result = snow_map.get(inc_id, {"snow_number": f"PENDING-{inc_id}",
                                             "snow_url":   ""})
        # Step 4 refactor: derive the structured label here (single source
        # of truth) and pass it to write_kb_pending() as a parameter.
        # This eliminates the duplicate _KB_PATTERN_MAP in kb_utils.py.
        #
        # KEY: pass applies_to as "summary" not "failure_pattern".
        # applies_to is free text from the model (e.g. "Interface down
        # causing cascading network failures") — not a structured enum field.
        # If passed as "failure_pattern", _derive_failure_pattern() rejects
        # it immediately when it is not in _RULE19_ENUMS and returns ""
        # without running the keyword scan. Passing both applies_to and
        # root_cause_explanation as combined "summary" text ensures the
        # keyword vocabulary scan always runs and finds the correct label.
        _rb_at     = inc.get("analysis", {}).get("runbook", {}).get("applies_to", "")
        _rc_text   = inc.get("analysis", {}).get("root_cause_explanation", "")
        _inc_title = inc.get("title", "")
        # If applies_to is already a valid Rule 19 enum, use it directly.
        # Do NOT run keyword scan on the combined text — the root_cause_explanation
        # describes cascade effects (e.g. "SNMP polling failure") that would fire
        # the wrong keyword match (e.g. polling_failure_cascade instead of
        # link_down_cascade when the real root cause is interface down).
        if _rb_at in _RULE19_ENUMS:
            fp_label = _rb_at   # model output is already a valid enum — trust it
        else:
            fp_label = _derive_failure_pattern(
                {"summary": f"{_rb_at} {_rc_text}"},
                _inc_title,
            )

        # ── RESOLVED guard — do NOT overwrite a RESOLVED KB file ──────────
        # If a RESOLVED version of this pattern's KB file already exists,
        # skip the PENDING write entirely. The RESOLVED file contains
        # validated real-world steps from actual execution — overwriting it
        # with a fresh PENDING version would break the RAG learning loop:
        # search_kb_for_similar() would see PENDING and skip it on the next run.
        _kb_skip = False
        if fp_label:
            _safe = fp_label.lower().strip().replace(" ", "_").replace("-", "_")
            _existing = KB_DIR / f"kb_{_safe}.txt"
            if _existing.exists():
                try:
                    _existing_content = _existing.read_text(encoding="utf-8")
                    if "STATUS: RESOLVED" in _existing_content:
                        print(f"  ✓  kb_{_safe}.txt already RESOLVED — skipping PENDING overwrite")
                        files.append(_existing)
                        _kb_skip = True
                except Exception:
                    pass  # unreadable — proceed with normal write

        if _kb_skip:
            continue

        f = write_kb_pending(inc, snow_result, MODEL_ID,
                             failure_pattern_label=fp_label)
        if f:
            # ── Rename to pattern-based filename ─────────────────────────────
            # Use fp_label (structured Rule 19 enum) not applies_to (model free text).
            # fp_label is derived by _derive_failure_pattern() above and is always
            # a valid enum value even when the model drifts from Rule 19.
            #
            # Use Path.replace() not Path.rename() — on Windows, rename() raises
            # FileExistsError [WinError 183] if the target already exists.
            # Path.replace() overwrites atomically on both Windows and Unix.
            if fp_label:
                safe_fp = (
                    fp_label.lower()
                    .strip()
                    .replace(" ", "_")
                    .replace("-", "_")
                )
                new_path = f.parent / f"kb_{safe_fp}.txt"
                f.replace(new_path)   # overwrites on Windows — cross-platform
                f = new_path
            files.append(f)
            print(f"  ✓  {f.name}")

    print(f"\n  KB documents written: {len(files)}")
    print(f"  Agent 3 will auto-upload these to Watson Orchestrate KB.")
    return files


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent 2 — Analyst: Granite RCA + runbook + KB"
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Retry only incidents with failed analysis in rca_output.json"
    )
    parser.add_argument(
        "--inference-route",
        choices=["watsonx", "orchestrate"],
        default="watsonx",
        help=(
            "watsonx    = direct ibm_watsonx_ai SDK (default) — "
            "fails when token_quota_reached. "
            "orchestrate = Watson Orchestrate /v1/orchestrate/runs via AIOps_RCA_Agent."
        ),
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Agent 2 — Analyst")
    print(f"{PROJECT_NAME} — {ORGANIZATION_NAME}")
    print("=" * 60)
    print(f"Model    : {MODEL_ID}")
    print(f"Route    : {args.inference_route.upper()}")
    print(f"Input    : {PAYLOAD_FILE}")
    print(f"Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ── Credential validation — only check what the selected route needs ─────
    if args.inference_route == "watsonx":
        if "YOUR_IBM" in WATSONX_API_KEY or "YOUR_IBM" in WATSONX_PROJECT_ID:
            print("\nERROR: watsonx.ai credentials not set in .env file")
            print("  Add WATSONX_API_KEY and WATSONX_PROJECT_ID to .env")
            print("  Or switch route: --inference-route orchestrate")
            sys.exit(1)
    else:  # orchestrate
        if not ORCHESTRATE_API_KEY:
            print("\nERROR: ORCHESTRATE_API_KEY not set in .env file")
            print("  Add ORCHESTRATE_API_KEY to .env")
            sys.exit(1)
        if not ORCHESTRATE_RCA_AGENT_ID:
            print("\nERROR: ORCHESTRATE_RCA_AGENT_ID is empty")
            sys.exit(1)

    # ── Select inference function based on route ─────────────────────────────
    def _infer(incidents: list[dict]) -> list[dict]:
        if args.inference_route == "orchestrate":
            return call_granite_via_orchestrate(incidents)
        return call_granite(incidents)

    # ── Load payload from Agent 1 ────────────────────────────────────────────
    if not PAYLOAD_FILE.exists():
        print(f"\nERROR: {PAYLOAD_FILE} not found — run agent1_correlator.py first")
        sys.exit(1)
    payload = json.loads(PAYLOAD_FILE.read_text(encoding="utf-8"))
    print(f"\n  Loaded {len(payload)} incident(s) from {PAYLOAD_FILE}")

    # ── Handle retry-failed mode ─────────────────────────────────────────────
    if args.retry_failed and RCA_FILE.exists():
        existing  = json.loads(RCA_FILE.read_text(encoding="utf-8"))
        done_ids  = {r["incident_id"] for r in existing if r.get("analysis")}
        retry_pay = [p for p in payload
                     if p.get("incident", {}).get("incident_id") not in done_ids]
        if not retry_pay:
            print(f"\n  All incidents already have analysis — nothing to retry")
            results = existing
        else:
            print(f"\n  Retrying {len(retry_pay)} failed incident(s)")
            new_results = _infer(retry_pay)
            merged = {r["incident_id"]: r for r in existing}
            for r in new_results:
                merged[r["incident_id"]] = r
            results = list(merged.values())
            RCA_FILE.write_text(
                json.dumps(results, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    else:
        results = _infer(payload)

    # HTML report cards
    html_files = generate_html(results)

    # snow_ready.json for Agent 3
    # payload passed through so resolve_assignment_group() has layer/title data
    snow_tickets = prepare_snow_fields(results, payload)

    # KB pending documents
    kb_files = generate_kb_pending_docs(results, snow_tickets)

    # Summary
    succeeded = sum(1 for r in results if r.get("analysis"))
    kb_used   = sum(1 for r in results
                    if (r.get("analysis") or {}).get("kb_used", False))

    print(f"\n{'='*60}")
    print(f"Agent 2 complete")
    print(f"  Inference route      : {args.inference_route.upper()}")
    print(f"  Incidents processed  : {len(results)}")
    print(f"  RCA succeeded        : {succeeded}")
    print(f"  KB context used      : {kb_used} incident(s)")
    print(f"  HTML reports         : {len(html_files)}")
    print(f"  KB pending docs      : {len(kb_files)}")
    print(f"  Files written:")
    print(f"    {RCA_FILE}")
    print(f"    {SNOW_FILE}  ← Agent 3 reads this")
    print(f"    {REPORTS_DIR}/")
    print(f"    kb_documents/  ← upload to Orchestrate KB")
    print("=" * 60)
    print(f"\nHandoff to Agent 3 (Watson Orchestrate):")
    print(f"  1. Upload kb_documents/*.txt to Orchestrate Knowledge Base")
    print(f"  2. Run agent3_notify.py to send snow_ready.json to Agent 3")
    print(f"  3. Agent 3 creates SNOW tickets + notifies SRE")


if __name__ == "__main__":
    main()