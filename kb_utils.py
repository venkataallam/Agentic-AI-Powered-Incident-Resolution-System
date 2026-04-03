"""
kb_utils.py  —  Knowledge Base Utilities
=========================================
Agentic AI-Powered Incident Resolution System — Pellera Hackathon 2026

Shared utilities for reading and writing the Watson Orchestrate
Knowledge Base documents.

RULES (validated before writing any KB entry):
  - write_kb_pending : called by Agent 2 BEFORE execution
                       STATUS = PENDING
                       Contains: runbook steps (planned, not yet validated)
                       Purpose : uploaded to Orchestrate KB so SRE can
                                 look up by ticket number

  - update_kb_resolved : called by Agent 3 AFTER SRE confirms resolution
                         STATUS = RESOLVED
                         Contains: executed steps + outcome + feedback
                         Purpose : used by Agent 2 next time to reference
                                   validated past resolutions

  - search_kb_for_similar : called by Agent 2 BEFORE Granite function call
                             Finds similar past RESOLVED incidents
                             Returns context string for Granite prompt

IMPORTANT: KB entries should contain VALIDATED data only.
  PENDING entries are for SRE lookup (ticket number → runbook).
  RESOLVED entries are for Agent 2 learning (past resolutions).
  Agent 2 only uses RESOLVED entries as Granite context.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

KB_DIR = Path("kb_documents")





# ── Search — used by Agent 2 before Granite call ────────────────────────────

def search_kb_for_similar(failure_pattern: str,
                           incident_type:   str) -> str:
    """
    Search kb_documents/ for a RESOLVED past incident that matches
    the same failure_pattern or incident_type.

    Returns plain text context string to inject into Granite prompt.
    Returns empty string if:
      - KB is empty (first time running)
      - No RESOLVED entries match
      - Only PENDING entries found (not validated — not used)

    Called by: agent2_analyst.py before building Granite prompt.
    """
    if not KB_DIR.exists() or not any(KB_DIR.glob("kb_*.txt")):
        return ""   # First run — no KB yet

    matches = []

    # Pre-compute lowercased search keys once outside the loop
    fp_lower  = failure_pattern.lower().strip() if failure_pattern else ""
    inc_lower = incident_type.lower().strip()   if incident_type   else ""

    for kb_file in sorted(KB_DIR.glob("kb_*.txt"),
                           key=lambda f: f.stat().st_mtime,
                           reverse=True):   # Most recent first
        try:
            content = kb_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # Only use RESOLVED entries — not PENDING (unvalidated)
        if "STATUS: RESOLVED" not in content:
            continue

        # Score 5: exact filename match — kb_{fp}.txt
        # Most reliable: written by generate_kb_pending_docs() using fp_label
        if fp_lower and kb_file.stem.lower() == f"kb_{fp_lower}":
            matches.append((5, content, kb_file.name))
            continue

        # Match on FAILURE_PATTERN_LABEL line — exact structured enum match.
        # This is the most reliable signal: kb_link_down_cascade.txt always
        # contains "FAILURE_PATTERN_LABEL: link_down_cascade" regardless of
        # how CP4AIOps phrased the probable_cause.summary.
        label_line = f"failure_pattern_label: {fp_lower}"
        if fp_lower and label_line in content.lower():
            matches.append((4, content, kb_file.name))  # highest score
            continue

        # Match on failure_pattern anywhere in content (secondary)
        if fp_lower and fp_lower in content.lower():
            matches.append((3, content, kb_file.name))
            continue

        # Match on incident_type as fallback
        if inc_lower and inc_lower in content.lower():
            matches.append((1, content, kb_file.name))

    if not matches:
        return ""

    # Use the highest-scored match
    matches.sort(key=lambda x: x[0], reverse=True)
    _, best_content, best_file = matches[0]

    return (
        f"\n\nKNOWLEDGE BASE — SIMILAR RESOLVED INCIDENT:\n"
        f"Source: {best_file}\n"
        f"{best_content}\n"
        f"END KNOWLEDGE BASE CONTEXT\n\n"
        f"Use the above validated resolution as reference when "
        f"generating runbook steps. Prioritise steps that previously "
        f"worked. Adjust for any differences in the current incident.\n"
    )


# ── Write PENDING — called by Agent 2 after Granite, before execution ────────

def write_kb_pending(inc: dict,
                     snow_result: dict,
                     model_id: str = "ibm/granite-4-h-small",
                     failure_pattern_label: str = "") -> Path | None:
    """
    Write a KB document marked PENDING for one incident.
    Called by Agent 2 immediately after Granite generates the RCA.

    STATUS = PENDING means:
      - Runbook has been generated but NOT yet executed
      - NOT used by Agent 2 as Granite context (only RESOLVED are used)
      - Used by Agent 3 for SRE ticket-number lookup in Orchestrate KB

    Args:
      failure_pattern_label : structured Rule 19 enum derived by
          _derive_failure_pattern() in agent2_analyst.py and passed here.
          Written as FAILURE_PATTERN_LABEL in the KB document so the RAG
          filter can locate it by exact label match.
          If empty, the FAILURE_PATTERN_LABEL line is omitted.

    Returns: Path to written file, or None on error.
    """
    KB_DIR.mkdir(exist_ok=True)

    analysis = inc.get("analysis")
    if not analysis:
        return None

    inc_id      = inc.get("incident_id", "unknown")
    snow_num    = snow_result.get("snow_number", "INC-PENDING")
    snow_url    = snow_result.get("snow_url", "")
    rb          = analysis.get("runbook", {})
    snow_fields = analysis.get("servicenow_ticket", {})

    lines = []
    lines.append(f"TICKET: {snow_num}")
    lines.append(f"STATUS: PENDING")
    lines.append(f"INCIDENT: {inc_id}")
    lines.append(f"TITLE: {inc.get('title','')}")
    lines.append(f"PATTERN: {rb.get('applies_to','')}")
    # FAILURE_PATTERN_LABEL: written using the label derived by
    # _derive_failure_pattern() in agent2_analyst.py and passed here.
    # Single source of truth — no duplicate mapping table in this file.
    if failure_pattern_label:
        lines.append(f"FAILURE_PATTERN_LABEL: {failure_pattern_label}")
    lines.append(f"PRIORITY: P{snow_fields.get('priority','1')}")
    lines.append(f"CI: {snow_fields.get('cmdb_ci','')}")
    lines.append(f"TEAM: {snow_fields.get('assignment_group','')}")
    lines.append(f"CONFIDENCE: {analysis.get('confidence','').upper()}")
    lines.append(f"CREATED: {analysis.get('created_time','')}")
    lines.append(f"GENERATED: {datetime.now(tz=timezone.utc).isoformat()}")
    lines.append(f"MODEL: {model_id}")
    lines.append("")

    lines.append("SUMMARY:")
    lines.append(analysis.get("summary", ""))
    lines.append("")

    lines.append("ROOT CAUSE:")
    lines.append(analysis.get("root_cause_explanation", ""))
    lines.append("")

    lines.append("CORRELATION:")
    lines.append(analysis.get("correlation_reasoning", ""))
    lines.append("")

    lines.append("IMPACT:")
    lines.append(analysis.get("impact_assessment", ""))
    lines.append("")

    lines.append(f"RUNBOOK: {rb.get('title','')}")
    lines.append(f"EST RESOLUTION: {rb.get('estimated_resolution_minutes','')} minutes")
    lines.append("")

    # Pre-checks
    for pc in rb.get("pre_checks", []):
        if isinstance(pc, dict):
            lines.append(
                f"PRE-CHECK {pc.get('check_id','')}: "
                f"{pc.get('description','')} "
                f"— {pc.get('command','')}"
            )
    lines.append("")

    # Runbook steps — dual-schema: handles both watsonx (commands[]/verify{})
    # and orchestrate (flat command/verify_command/verify_expected)
    for step in rb.get("steps", []):
        if not isinstance(step, dict):
            continue
        # Command: try nested commands[0] first, then flat fields
        cmds = step.get("commands", [])
        fc   = cmds[0] if cmds and isinstance(cmds[0], dict) else {}
        # cmd: flat field first — contains the COMPLETE compound command
        # (e.g. "configure terminal\ninterface X\nno shutdown\nend").
        # commands[0].command only has the FIRST sub-command ("configure terminal").
        cmd  = step.get("command","")      or fc.get("command","")
        # tgt: nested first — flat target often missing from model output
        tgt  = fc.get("target","")         or step.get("target","")
        exp  = fc.get("expected_output","") or step.get("expected_output","")
        # err: flat field first — flat on_failure has the correct step-level policy.
        # commands[0].on_failure is "continue" for config sub-steps but the flat
        # step on_failure correctly says "stop_and_escalate" for remediation.
        err  = step.get("on_failure","")   or fc.get("on_failure","")
        # Verify: try nested verify{} first, then flat fields
        v    = step.get("verify", {})
        if isinstance(v, dict) and v.get("command"):
            vcmd = v.get("command","")
            vexp = v.get("expected_output","")
        else:
            vcmd = step.get("verify_command","")
            vexp = step.get("verify_expected","")
        lines.append(f"STEP {step.get('step_number','')} — {step.get('action','')}")
        lines.append(f"  WHAT: {step.get('what','')}")
        lines.append(f"  CMD:  {cmd}")
        lines.append(f"  ON:   {tgt}")
        lines.append(f"  EXP:  {exp}")
        lines.append(f"  ERR:  {err}")
        if vcmd:
            lines.append(f"  VFY:  {vcmd} → {vexp}")
        lines.append("")

    # Rollback — only in full schema (watsonx); absent in flat (orchestrate)
    rb_roll = rb.get("rollback", {})
    if isinstance(rb_roll, dict) and rb_roll:
        rb_cmds = [c.get("command", "")
                   for c in rb_roll.get("commands", [])
                   if isinstance(c, dict)]
        lines.append(
            f"ROLLBACK: {rb_roll.get('description','')} "
            f"— {' | '.join(filter(None, rb_cmds))}"
        )
    elif isinstance(rb_roll, str) and rb_roll:
        lines.append(f"ROLLBACK: {rb_roll}")
    lines.append("")

    # Escalation — dual-schema: try nested escalation{L1,L2,L3} first,
    # then flat escalation_l1/l2/l3 (orchestrate schema)
    esc = rb.get("escalation", {})
    if isinstance(esc, dict) and (esc.get("L1") or esc.get("L2") or esc.get("L3")):
        for lvl in ["L1", "L2", "L3"]:
            if esc.get(lvl):
                lines.append(f"{lvl}: {esc[lvl]}")
    else:
        for lvl, key in [("L1","escalation_l1"),("L2","escalation_l2"),("L3","escalation_l3")]:
            val = rb.get(key,"")
            if val:
                lines.append(f"{lvl}: {val}")
    lines.append("")

    # Post-validation
    for pv in rb.get("post_validation", []):
        if isinstance(pv, dict):
            lines.append(
                f"VALIDATE {pv.get('check_id','')}: "
                f"{pv.get('description','')} "
                f"— {pv.get('command','')} "
                f"→ {pv.get('expected_output','')}"
            )
    lines.append("")

    if snow_url:
        lines.append(f"SNOW URL: {snow_url}")

    # Safe filename
    safe_num = re.sub(r"[^A-Za-z0-9_-]", "_", snow_num)
    safe_id  = re.sub(r"[^A-Za-z0-9_-]", "_", inc_id)
    out_file = KB_DIR / f"kb_{safe_num}_{safe_id}.txt"

    try:
        out_file.write_text("\n".join(lines), encoding="utf-8")
        return out_file
    except Exception as exc:
        print(f"  KB write error: {exc}")
        return None


# ── Update RESOLVED — called by Agent 3 after SRE confirms ──────────────────

def update_kb_resolved(snow_number:       str,
                        feedback:          str,
                        resolution_time:   int,
                        steps_executed:    list[str],
                        outcome:           str = "resolved") -> bool:
    """
    Update an existing PENDING KB document to RESOLVED status.

    Called by: Agent 3 (Watson Orchestrate) AFTER:
      1. Runbook steps have been executed
      2. SRE has confirmed resolution
      3. Feedback has been collected

    Only RESOLVED entries are used by Agent 2 as Granite context.

    Parameters:
      snow_number      : INC0001234
      feedback         : SRE feedback text
      resolution_time  : actual minutes taken to resolve
      steps_executed   : list of commands that were actually run
      outcome          : "resolved" or "unresolved"

    Returns True if file found and updated, False otherwise.
    """
    if not KB_DIR.exists():
        return False

    # Find the KB file for this ticket number
    target_file = None
    for f in KB_DIR.glob("kb_*.txt"):
        try:
            content = f.read_text(encoding="utf-8")
            if f"TICKET: {snow_number}" in content:
                target_file = f
                break
        except Exception:
            continue

    if not target_file:
        print(f"  KB update: no file found for {snow_number}")
        return False

    # Read existing content
    existing = target_file.read_text(encoding="utf-8")

    # Replace PENDING with RESOLVED and append outcome block
    updated = existing.replace("STATUS: PENDING", "STATUS: RESOLVED", 1)

    outcome_block = "\n".join([
        "",
        "=" * 50,
        "RESOLUTION OUTCOME",
        "=" * 50,
        f"OUTCOME:           {outcome.upper()}",
        f"ACTUAL TIME:       {resolution_time} minutes",
        f"RESOLVED AT:       {datetime.now(tz=timezone.utc).isoformat()}",
        f"SRE FEEDBACK:      {feedback}",
        "",
        "STEPS ACTUALLY EXECUTED:",
    ])
    for i, step in enumerate(steps_executed, start=1):
        outcome_block += f"\n  {i}. {step}"
    outcome_block += "\n"

    if outcome == "resolved":
        outcome_block += "\nVALIDATED: YES — these steps confirmed to resolve this failure pattern.\n"
        outcome_block += "Agent 2 will use these steps as reference for similar future incidents.\n"
    else:
        outcome_block += "\nVALIDATED: NO — incident not resolved. Do not use as reference.\n"

    updated += outcome_block

    try:
        target_file.write_text(updated, encoding="utf-8")
        print(f"  KB updated → {target_file.name}  [STATUS: RESOLVED]")
        return True
    except Exception as exc:
        print(f"  KB update error: {exc}")
        return False




# ── Update RESOLVED by pattern — called by mark_incident_resolved ADK tool ──

def update_kb_resolved_by_pattern(
    failure_pattern:      str,
    incident_id:          str,
    feedback:             str,
    resolution_time:      int,
    steps_executed:       list[str],
    outcome:              str = "resolved",
) -> bool:
    """
    Update a KB document to RESOLVED status using failure_pattern as the key.

    PATTERN-BASED — one KB doc per failure pattern, never per ticket.
    The same pattern's doc is overwritten on each resolution so the KB
    always holds the MOST RECENT validated steps for each pattern.

    Lookup priority:
      1. Filename: kb_{failure_pattern}.txt  (primary — deterministic)
      2. FAILURE_PATTERN_LABEL line in content (secondary — for edge cases)
      3. PATTERN line in content (tertiary — for older docs)

    Called by: mark_resolved_tool.py (Watson Orchestrate ADK Python tool)
    Also callable directly from Python for demo/testing.
    """
    if not KB_DIR.exists():
        print(f"  KB update: kb_documents/ directory not found")
        return False

    # Priority 1: filename-based lookup (most reliable)
    safe_pattern = (
        failure_pattern.lower()
        .strip()
        .replace(" ", "_")
        .replace("-", "_")
    )
    target_file = KB_DIR / f"kb_{safe_pattern}.txt"

    if not target_file.exists():
        # Priority 2+3: scan file contents
        target_file = None
        for f in KB_DIR.glob("kb_*.txt"):
            try:
                content = f.read_text(encoding="utf-8")
                if (
                    f"FAILURE_PATTERN_LABEL: {failure_pattern}" in content
                    or f"PATTERN: {failure_pattern}" in content
                ):
                    target_file = f
                    break
            except Exception:
                continue

    if not target_file or not target_file.exists():
        print(f"  KB update: no file found for pattern '{failure_pattern}'")
        print(f"  Expected: {KB_DIR}/kb_{safe_pattern}.txt")
        print(f"  Available: {[f.name for f in KB_DIR.glob('kb_*.txt')]}")
        return False

    existing = target_file.read_text(encoding="utf-8")

    # Replace first PENDING — guard against double-marking
    if "STATUS: RESOLVED" in existing:
        print(f"  KB {target_file.name}: already RESOLVED — not overwriting")
        return True  # treat as success

    updated = existing.replace("STATUS: PENDING", "STATUS: RESOLVED", 1)

    outcome_block = "\n".join([
        "",
        "=" * 50,
        "RESOLUTION OUTCOME",
        "=" * 50,
        f"OUTCOME:           {outcome.upper()}",
        f"ACTUAL TIME:       {resolution_time} minutes",
        f"RESOLVED AT:       {datetime.now(tz=timezone.utc).isoformat()}",
        f"INCIDENT ID:       {incident_id}",
        f"SRE FEEDBACK:      {feedback}",
        "",
        "STEPS ACTUALLY EXECUTED:",
    ])
    for i, step in enumerate(steps_executed, start=1):
        outcome_block += f"\n  {i}. {step}"
    outcome_block += "\n"

    if outcome == "resolved":
        outcome_block += (
            "\nVALIDATED: YES — these steps confirmed to resolve "
            "this failure pattern.\n"
            "Agent 2 will use these validated steps as reference "
            "for similar future incidents.\n"
        )
    else:
        outcome_block += (
            "\nVALIDATED: NO — incident was escalated, not resolved. "
            "Do not use as Agent 2 reference.\n"
        )

    updated += outcome_block

    try:
        target_file.write_text(updated, encoding="utf-8")
        print(f"  ✓ KB updated → {target_file.name}  [STATUS: {outcome.upper()}]")
        return True
    except Exception as exc:
        print(f"  KB update error: {exc}")
        return False
# ── List KB entries — utility for debugging ──────────────────────────────────

def list_kb_entries() -> None:
    """Print a summary of all KB entries. Useful for debugging."""
    if not KB_DIR.exists() or not any(KB_DIR.glob("kb_*.txt")):
        print("  Knowledge base is empty — no entries yet")
        return

    print(f"\n  Knowledge Base — {KB_DIR}/")
    print(f"  {'File':<45} {'Ticket':<14} {'Status':<10} {'Pattern'}")
    print(f"  {'-'*45} {'-'*14} {'-'*10} {'-'*30}")

    for f in sorted(KB_DIR.glob("kb_*.txt")):
        try:
            content = f.read_text(encoding="utf-8")
            ticket  = next((l.split(": ",1)[1] for l in content.splitlines()
                            if l.startswith("TICKET: ")), "?")
            status  = next((l.split(": ",1)[1] for l in content.splitlines()
                            if l.startswith("STATUS: ")), "?")
            pattern = next((l.split(": ",1)[1] for l in content.splitlines()
                            if l.startswith("PATTERN: ")), "?")
            print(f"  {f.name:<45} {ticket:<14} {status:<10} {pattern}")
        except Exception:
            print(f"  {f.name:<45} (unreadable)")


if __name__ == "__main__":
    print("KB Utilities — listing current knowledge base:")
    list_kb_entries()