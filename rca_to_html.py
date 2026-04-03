"""
rca_to_html.py
--------------
Reads rca_output.json (Granite output from watsonx_inference.py)
Auto-generates a styled HTML report card for each incident.

Output: one HTML file per incident + one combined multi-incident report
  rca_report_incident-network-001.html
  rca_report_incident-auth-001.html
  rca_report_incident-payment-001.html
  rca_report_ALL_INCIDENTS.html        ← combined for demo

No human copy-paste needed.
Run: python rca_to_html.py
"""

import json
from pathlib import Path
from datetime import datetime, timezone

INPUT_FILE  = Path("rca_output.json")
OUTPUT_DIR  = Path("rca_reports")

# ---------------------------------------------------------------------------
# CSS — shared across all reports
# ---------------------------------------------------------------------------

CSS = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');
  :root {
    --navy:#0f1b2d; --blue:#0f62fe; --blue-lt:#d0e2ff;
    --teal:#009d9a; --teal-lt:#d9fbfb; --red:#da1e28;
    --red-lt:#fff1f1; --amber:#f1c21b; --amber-lt:#fdf6dd;
    --green:#24a148; --green-lt:#defbe6; --purple:#6929c4;
    --purple-lt:#f6f2ff; --gray-00:#ffffff; --gray-01:#f4f4f4;
    --gray-02:#e0e0e0; --gray-03:#c6c6c6; --gray-05:#8d8d8d;
    --gray-07:#525252; --gray-10:#161616;
    --shadow:0 2px 16px rgba(0,0,0,0.10); --radius:8px;
    --font:'IBM Plex Sans',sans-serif; --mono:'IBM Plex Mono',monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:var(--font);background:var(--gray-01);
       color:var(--gray-10);padding:32px 24px 64px;}
  .page{max-width:900px;margin:0 auto;}
  .report-header{background:var(--navy);border-radius:12px;
    padding:28px 32px;margin-bottom:20px;position:relative;overflow:hidden;}
  .report-header::before{content:'';position:absolute;top:-40px;right:-40px;
    width:200px;height:200px;border-radius:50%;
    background:rgba(15,98,254,0.15);}
  .ibm-badge{background:var(--blue);color:#fff;font-size:10px;font-weight:600;
    letter-spacing:1.5px;text-transform:uppercase;padding:4px 12px;
    border-radius:20px;margin-bottom:12px;display:inline-block;}
  .report-header h1{font-size:22px;font-weight:600;color:#fff;
    line-height:1.3;max-width:600px;}
  .conf-badge{background:var(--green);color:#fff;font-size:11px;
    font-weight:600;padding:6px 14px;border-radius:20px;white-space:nowrap;}
  .header-top{display:flex;align-items:flex-start;
    justify-content:space-between;margin-bottom:16px;
    position:relative;z-index:1;}
  .meta-row{display:flex;flex-wrap:wrap;gap:20px;position:relative;
    z-index:1;border-top:1px solid rgba(255,255,255,0.12);
    padding-top:16px;margin-top:4px;}
  .meta-item{display:flex;flex-direction:column;gap:2px;}
  .meta-label{font-size:10px;text-transform:uppercase;letter-spacing:1px;
    color:rgba(255,255,255,0.5);font-weight:500;}
  .meta-value{font-size:13px;color:rgba(255,255,255,0.9);}
  .meta-p1{color:#ff8389!important;font-weight:600;}
  .failure-pill{background:rgba(241,194,27,0.15);
    border:1px solid rgba(241,194,27,0.3);color:var(--amber);
    font-size:11px;font-weight:500;padding:3px 10px;border-radius:4px;
    font-family:var(--mono);display:inline-block;}
  .card{background:var(--gray-00);border-radius:var(--radius);
    margin-bottom:14px;box-shadow:var(--shadow);overflow:hidden;
    border:1px solid var(--gray-02);}
  .card-header{display:flex;align-items:center;gap:10px;
    padding:14px 20px;border-bottom:1px solid var(--gray-02);}
  .icon-circle{width:32px;height:32px;border-radius:50%;
    display:flex;align-items:center;justify-content:center;
    font-size:14px;flex-shrink:0;}
  .card-title{font-size:12px;font-weight:600;letter-spacing:1px;
    text-transform:uppercase;}
  .card-body{padding:18px 20px;font-size:14px;line-height:1.75;
    color:var(--gray-07);}
  .card-body strong{color:var(--gray-10);font-weight:500;}
  .card-summary .card-header{background:#f0f4ff;}
  .card-summary .icon-circle{background:var(--blue-lt);}
  .card-summary .card-title{color:var(--blue);}
  .card-root .card-header{background:var(--red-lt);}
  .card-root .icon-circle{background:#ffd7d9;}
  .card-root .card-title{color:var(--red);}
  .card-corr .card-header{background:var(--purple-lt);}
  .card-corr .icon-circle{background:#e8daff;}
  .card-corr .card-title{color:var(--purple);}
  .card-impact .card-header{background:var(--amber-lt);}
  .card-impact .icon-circle{background:#fcf4d6;}
  .card-impact .card-title{color:#8a5a00;}
  .card-steps .card-header{background:var(--teal-lt);}
  .card-steps .icon-circle{background:#a7f0ba;}
  .card-steps .card-title{color:var(--teal);}
  .card-runbook .card-header{background:#f6f2ff;}
  .card-runbook .icon-circle{background:#e8daff;}
  .card-runbook .card-title{color:var(--purple);}
  .steps-list{display:flex;flex-direction:column;gap:12px;}
  .step-item{display:flex;gap:14px;background:var(--gray-01);
    border-radius:6px;padding:14px 16px;border:1px solid var(--gray-02);}
  .step-num{width:28px;height:28px;background:var(--teal);color:#fff;
    border-radius:50%;display:flex;align-items:center;justify-content:center;
    font-size:12px;font-weight:600;flex-shrink:0;margin-top:1px;}
  .step-content{flex:1;}
  .step-action{font-size:13px;font-weight:600;color:var(--gray-10);
    margin-bottom:6px;}
  .step-row{display:flex;align-items:flex-start;gap:8px;
    margin-top:5px;font-size:12px;}
  .step-row-label{font-size:10px;font-weight:600;text-transform:uppercase;
    letter-spacing:0.5px;color:var(--gray-05);min-width:70px;padding-top:2px;}
  .cmd-pill{font-family:var(--mono);font-size:11px;background:var(--navy);
    color:#a8d1ff;padding:3px 10px;border-radius:4px;display:inline-block;}
  .target-pill{font-size:11px;background:var(--teal-lt);color:var(--teal);
    border:1px solid rgba(0,157,154,0.2);padding:2px 8px;border-radius:4px;
    font-weight:500;display:inline-block;}
  .rb-meta{display:grid;grid-template-columns:repeat(3,1fr);
    gap:12px;margin-bottom:16px;}
  .rb-meta-item{background:var(--gray-01);border:1px solid var(--gray-02);
    border-radius:6px;padding:10px 14px;}
  .rb-meta-label{font-size:10px;text-transform:uppercase;letter-spacing:0.8px;
    color:var(--gray-05);font-weight:600;margin-bottom:4px;}
  .rb-meta-val{font-size:13px;color:var(--gray-10);font-weight:500;}
  .rb-meta-val.mono{font-family:var(--mono);font-size:12px;
    color:var(--purple);}
  .sec-label{font-size:10px;font-weight:600;text-transform:uppercase;
    letter-spacing:1px;color:var(--gray-05);margin:16px 0 8px;
    padding-bottom:6px;border-bottom:1px solid var(--gray-02);}
  .prechecks{display:flex;flex-direction:column;gap:8px;}
  .precheck-item{display:flex;align-items:flex-start;gap:10px;
    background:var(--amber-lt);border:1px solid rgba(241,194,27,0.25);
    border-radius:6px;padding:10px 14px;font-size:13px;}
  .precheck-cmd{font-family:var(--mono);font-size:11px;color:#8a5a00;
    background:rgba(241,194,27,0.12);padding:2px 8px;border-radius:3px;
    display:inline-block;margin-top:4px;}
  .rb-steps{display:flex;flex-direction:column;gap:12px;}
  .rb-step{border:1px solid var(--gray-02);border-radius:6px;overflow:hidden;}
  .rb-step-hdr{background:var(--navy);color:#fff;display:flex;
    align-items:center;gap:12px;padding:10px 16px;}
  .rb-step-num{width:24px;height:24px;background:var(--blue);
    border-radius:50%;display:flex;align-items:center;
    justify-content:center;font-size:11px;font-weight:700;flex-shrink:0;}
  .rb-step-title{font-size:13px;font-weight:600;}
  .rb-step-body{padding:14px 16px;background:var(--gray-00);}
  .rb-field{display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;}
  .rb-field-label{font-size:10px;font-weight:700;text-transform:uppercase;
    letter-spacing:0.8px;color:var(--gray-05);min-width:72px;
    padding-top:3px;flex-shrink:0;}
  .rb-field-val{font-size:13px;color:var(--gray-07);line-height:1.5;flex:1;}
  .rb-cmd{font-family:var(--mono);font-size:11px;background:#1a2332;
    color:#a8d1ff;padding:6px 12px;border-radius:4px;display:block;
    margin-top:2px;}
  .rb-verify{font-family:var(--mono);font-size:11px;
    background:var(--green-lt);color:#0a6641;padding:6px 12px;
    border-radius:4px;display:block;margin-top:2px;}
  .type-pill{font-size:10px;background:var(--blue-lt);color:#003a8c;
    padding:2px 7px;border-radius:3px;font-family:var(--mono);
    font-weight:500;margin-left:auto;}
  .of-pill{font-size:10px;padding:2px 8px;border-radius:3px;font-weight:600;}
  .of-stop{background:var(--red-lt);color:var(--red);}
  .of-retry{background:var(--amber-lt);color:#8a5a00;}
  .of-cont{background:var(--green-lt);color:#0a6641;}
  .esc-grid{display:grid;grid-template-columns:repeat(3,1fr);
    gap:10px;margin-top:4px;}
  .esc-item{background:var(--gray-01);border:1px solid var(--gray-02);
    border-radius:6px;padding:10px 12px;}
  .esc-level{font-size:11px;font-weight:700;color:var(--blue);
    margin-bottom:4px;font-family:var(--mono);}
  .esc-text{font-size:12px;color:var(--gray-07);line-height:1.4;}
  .pv-list{display:flex;flex-direction:column;gap:8px;}
  .pv-item{display:flex;gap:10px;background:var(--green-lt);
    border:1px solid rgba(36,161,72,0.2);border-radius:6px;
    padding:10px 14px;}
  .pv-content{font-size:12px;color:var(--gray-07);line-height:1.5;}
  .pv-cmd{font-family:var(--mono);font-size:10px;color:#0a6641;
    background:rgba(36,161,72,0.1);padding:2px 7px;border-radius:3px;
    display:inline-block;margin-top:4px;}
  .rollback-box{background:var(--red-lt);
    border:1px solid rgba(218,30,40,0.2);border-radius:6px;
    padding:12px 16px;font-size:13px;color:var(--gray-07);line-height:1.6;}
  .rollback-cmd{font-family:var(--mono);font-size:11px;
    background:rgba(218,30,40,0.08);color:var(--red);padding:4px 10px;
    border-radius:3px;display:inline-block;margin-top:6px;}
  .snow-card{background:var(--gray-00);border-radius:var(--radius);
    margin-bottom:14px;box-shadow:var(--shadow);overflow:hidden;
    border:1px solid var(--gray-02);}
  .snow-hdr{background:#293e60;color:#fff;display:flex;align-items:center;
    gap:10px;padding:14px 20px;}
  .snow-logo{width:28px;height:28px;background:#81b5a1;border-radius:4px;
    display:flex;align-items:center;justify-content:center;
    font-size:11px;font-weight:700;color:#fff;}
  .snow-title{font-size:12px;font-weight:600;letter-spacing:1px;
    text-transform:uppercase;}
  .snow-body{padding:18px 20px;}
  .snow-grid{display:grid;grid-template-columns:1fr 1fr 1fr;
    gap:12px;margin-bottom:14px;}
  .snow-field{display:flex;flex-direction:column;gap:3px;}
  .snow-field-label{font-size:10px;text-transform:uppercase;
    letter-spacing:0.8px;color:var(--gray-05);font-weight:600;}
  .snow-field-val{font-size:13px;color:var(--gray-10);font-weight:500;}
  .p1-badge{background:var(--red);color:#fff;font-size:11px;
    font-weight:700;padding:3px 10px;border-radius:4px;display:inline-block;}
  .p2-badge{background:#ff832b;color:#fff;font-size:11px;
    font-weight:700;padding:3px 10px;border-radius:4px;display:inline-block;}
  .snow-desc{background:var(--gray-01);border:1px solid var(--gray-02);
    border-radius:6px;padding:12px 14px;font-size:13px;color:var(--gray-07);
    line-height:1.65;margin-bottom:12px;}
  .work-notes{background:#fffbf0;border:1px solid rgba(241,194,27,0.3);
    border-radius:6px;padding:12px 14px;}
  .wn-label{font-size:10px;text-transform:uppercase;letter-spacing:0.8px;
    color:#8a5a00;font-weight:700;margin-bottom:6px;}
  .wn-text{font-size:12px;color:var(--gray-07);line-height:1.6;
    font-family:var(--mono);}
  .incident-divider{border:none;border-top:3px dashed var(--gray-02);
    margin:40px 0;}
  .footer{text-align:center;font-size:11px;color:var(--gray-05);
    margin-top:24px;padding-top:16px;border-top:1px solid var(--gray-02);
    line-height:1.8;}
  @media print{body{background:#fff;padding:0;}
    .card,.snow-card{box-shadow:none;}}
</style>
"""

# ---------------------------------------------------------------------------
# Priority badge helper
# ---------------------------------------------------------------------------

def priority_badge(priority: str) -> str:
    p = str(priority)
    if p == "1":
        return '<span class="p1-badge">P1 — Critical</span>'
    if p == "2":
        return '<span class="p2-badge">P2 — Major</span>'
    return f'<span style="font-weight:600">P{p}</span>'


def on_failure_pill(val: str) -> str:
    val = str(val).lower()
    if "stop" in val:
        return '<span class="of-pill of-stop">stop_and_escalate</span>'
    if "retry" in val:
        return '<span class="of-pill of-retry">retry_once</span>'
    return '<span class="of-pill of-cont">continue</span>'


def escape(text) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")




# ---------------------------------------------------------------------------
# Dual-schema helpers — support both watsonx.ai (full) and Orchestrate (flat)
# ---------------------------------------------------------------------------

def _step_cmd(step: dict) -> tuple[str, str, str, str, str]:
    """Return (command, target, command_type, expected_output, on_failure)
    from either nested commands[0] (watsonx) or flat fields (orchestrate)."""
    cmds = step.get("commands", [])
    if cmds and isinstance(cmds[0], dict):
        c = cmds[0]
        return (c.get("command",""), c.get("target",""),
                c.get("command_type",""), c.get("expected_output",""),
                c.get("on_failure","continue"))
    # Flat schema fallback
    return (step.get("command",""), step.get("target",""),
            step.get("command_type",""), step.get("expected_output",""),
            step.get("on_failure","continue"))


def _step_verify(step: dict) -> tuple[str, str]:
    """Return (verify_command, verify_expected) from either
    nested verify{} (watsonx) or flat verify_command/verify_expected (orchestrate)."""
    v = step.get("verify", {})
    if isinstance(v, dict) and v.get("command"):
        return v.get("command",""), v.get("expected_output","")
    return step.get("verify_command",""), step.get("verify_expected","")


def _step_timeout(step: dict) -> int:
    """Return timeout_seconds from commands[0] or default 30."""
    cmds = step.get("commands", [])
    if cmds and isinstance(cmds[0], dict):
        return cmds[0].get("timeout_seconds", 30)
    return 30


def _step_type_badge(step: dict) -> str:
    """Return command_type from commands[0] or flat command_type."""
    cmds = step.get("commands", [])
    if cmds and isinstance(cmds[0], dict):
        return escape(cmds[0].get("command_type",""))
    return escape(step.get("command_type",""))


def _escalation_levels(rb: dict) -> dict:
    """Return {L1, L2, L3} from either nested escalation{} (watsonx)
    or flat escalation_l1/l2/l3 (orchestrate)."""
    esc = rb.get("escalation", {})
    if isinstance(esc, dict) and (esc.get("L1") or esc.get("L2") or esc.get("L3")):
        return esc
    # Flat schema fallback
    return {
        "L1": rb.get("escalation_l1", ""),
        "L2": rb.get("escalation_l2", ""),
        "L3": rb.get("escalation_l3", ""),
    }


def _build_description_for_snow_card(analysis: dict) -> str:
    """
    Build the full ServiceNow description for the HTML snow card.
    rca_output.json servicenow_ticket does NOT contain description —
    it is assembled by prepare_snow_fields() → build_snow_description().
    This function produces the same content for the HTML card.
    Handles both watsonx (full) and orchestrate (flat) schemas.
    """
    rb         = analysis.get("runbook", {})
    confidence = analysis.get("confidence", "").upper()
    summary    = analysis.get("summary", "")
    root_cause = analysis.get("root_cause_explanation", "")
    correlation= analysis.get("correlation_reasoning", "")
    impact     = analysis.get("impact_assessment", "")

    lines = []
    lines.append(f"[AIOps RCA — Confidence: {confidence}]")
    lines.append("SUMMARY:")
    lines.append(summary)
    lines.append("ROOT CAUSE:")
    lines.append(root_cause)
    lines.append("CORRELATION REASONING:")
    lines.append(correlation)
    lines.append("IMPACT ASSESSMENT:")
    lines.append(impact)
    lines.append("=== AIOps AUTO-GENERATED RUNBOOK ===")
    lines.append(f"Runbook ID : {rb.get('runbook_id','')}")
    lines.append(f"Title      : {rb.get('title','')}")
    lines.append(f"Pattern    : {rb.get('applies_to','')}")
    lines.append(f"Est. Time  : {rb.get('estimated_resolution_minutes','')} minutes")

    lines.append("--- RESOLUTION STEPS ---")
    for step in rb.get("steps", []):
        if not isinstance(step, dict):
            continue
        cmd, tgt, ctype, exp, onf = _step_cmd(step)
        vcmd, vexp = _step_verify(step)
        lines.append(f"  STEP {step.get('step_number','')}: {step.get('action','')}")
        lines.append(f"    What   : {step.get('what','')}")
        lines.append(f"    Command: {cmd}")
        lines.append(f"    Target : {tgt}")
        lines.append(f"    Type   : {ctype}")
        lines.append(f"    Expect : {exp}")
        lines.append(f"    OnFail : {onf}")
        if vcmd:
            lines.append(f"    Verify : {vcmd} on {tgt} -> {vexp}")

    esc = _escalation_levels(rb)
    lines.append("--- ESCALATION ---")
    lines.append(f"  L1: {esc.get('L1','')}")
    lines.append(f"  L2: {esc.get('L2','')}")
    lines.append(f"  L3: {esc.get('L3','')}")

    lines.append(
        f"=== Runbook ID: {rb.get('runbook_id','')} | "
        f"Follow: Watson Orchestrate -> AIOps_Incident Resolution Manager ==="
    )
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# HTML builders per section
# ---------------------------------------------------------------------------

def build_header(inc: dict, analysis: dict) -> str:
    snow    = analysis.get("servicenow_ticket", {})
    rb      = analysis.get("runbook", {})
    inc_id  = escape(analysis.get("incident_id", inc.get("incident_id", "")))
    title   = escape(analysis.get("title", inc.get("title", "")))
    created = escape(analysis.get("created_time", ""))
    conf    = escape(analysis.get("confidence", "").upper())
    pattern = escape(rb.get("applies_to") or analysis.get("failure_pattern", ""))
    priority = escape(snow.get("priority", "1"))
    model   = escape(inc.get("model_used", "ibm/granite-4-h-small"))

    p_label = f"P{priority} — Critical" if priority == "1" else f"P{priority} — Major"
    p_class = "meta-p1" if priority == "1" else ""

    return f"""
  <div class="report-header">
    <div class="header-top">
      <div>
        <div class="ibm-badge">IBM AIOps — RCA Report</div>
        <h1>{title}</h1>
      </div>
      <div class="conf-badge">&#10003; Confidence: {conf}</div>
    </div>
    <div class="meta-row">
      <div class="meta-item">
        <span class="meta-label">Incident ID</span>
        <span class="meta-value">{inc_id}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">Priority</span>
        <span class="meta-value {p_class}">{p_label}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">Created</span>
        <span class="meta-value">{created}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">Source</span>
        <span class="meta-value">CP4AIOps</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">Failure Pattern</span>
        <span class="failure-pill">{pattern}</span>
      </div>
    </div>
  </div>"""


def build_text_card(css_class: str, icon: str, title: str, content: str) -> str:
    return f"""
  <div class="card {css_class}">
    <div class="card-header">
      <div class="icon-circle">{icon}</div>
      <span class="card-title">{title}</span>
    </div>
    <div class="card-body">{content}</div>
  </div>"""


def build_steps_card(steps: list) -> str:
    if not steps:
        return ""
    items = ""
    for i, step in enumerate(steps, start=1):
        if isinstance(step, str):
            # Plain string step
            items += f"""
        <div class="step-item">
          <div class="step-num">{i}</div>
          <div class="step-content">
            <div class="step-action">{escape(step)}</div>
          </div>
        </div>"""
        elif isinstance(step, dict):
            action   = escape(step.get("action", f"Step {i}"))
            cmd      = escape(step.get("command_hint") or step.get("command", ""))
            target   = escape(step.get("target", ""))
            expected = escape(step.get("expected_outcome") or step.get("expected_output", ""))
            items += f"""
        <div class="step-item">
          <div class="step-num">{step.get('order', i)}</div>
          <div class="step-content">
            <div class="step-action">{action}</div>
            {"" if not cmd else f'<div class="step-row"><span class="step-row-label">Command</span><span class="step-row-val"><span class="cmd-pill">{cmd}</span></span></div>'}
            {"" if not target else f'<div class="step-row"><span class="step-row-label">Target</span><span class="step-row-val"><span class="target-pill">{target}</span></span></div>'}
            {"" if not expected else f'<div class="step-row"><span class="step-row-label">Expected</span><span class="step-row-val">{expected}</span></div>'}
          </div>
        </div>"""

    return f"""
  <div class="card card-steps">
    <div class="card-header">
      <div class="icon-circle">&#128295;</div>
      <span class="card-title">Recommended Investigation Steps</span>
    </div>
    <div class="card-body">
      <div class="steps-list">{items}
      </div>
    </div>
  </div>"""


def build_runbook_card(rb: dict) -> str:
    if not rb:
        return ""

    rb_id    = escape(rb.get("runbook_id", ""))
    applies  = escape(rb.get("applies_to", ""))
    est      = rb.get("estimated_resolution_minutes", "")

    # Pre-checks
    pre_html = ""
    for pc in rb.get("pre_checks", []):
        if isinstance(pc, str):
            pre_html += f"""
          <div class="precheck-item">
            <span>&#9889;</span>
            <div><div class="precheck-text">{escape(pc)}</div></div>
          </div>"""
        elif isinstance(pc, dict):
            desc = escape(pc.get("description", ""))
            cmd  = escape(pc.get("command", ""))
            pre_html += f"""
          <div class="precheck-item">
            <span>&#9889;</span>
            <div>
              <div class="precheck-text">{desc}</div>
              {"" if not cmd else f'<span class="precheck-cmd">{cmd}</span>'}
            </div>
          </div>"""

    # Steps — dual-schema: handles both watsonx (commands[]/verify{})
    # and orchestrate (flat command/verify_command/verify_expected)
    steps_html = ""
    for step in rb.get("steps", []):
        if not isinstance(step, dict):
            continue
        num    = step.get("step_number", "")
        action = escape(step.get("action", ""))
        what   = escape(step.get("what", ""))

        c_cmd, c_tgt, c_type, c_exp, c_of_raw = _step_cmd(step)
        c_of  = on_failure_pill(c_of_raw)
        c_to  = _step_timeout(step)
        c_type_badge = _step_type_badge(step)
        v_cmd, v_exp = _step_verify(step)

        cmds_html = f"""
              <div class="rb-field">
                <span class="rb-field-label">Command</span>
                <span class="rb-field-val"><code class="rb-cmd">{escape(c_cmd)}</code></span>
              </div>
              <div class="rb-field">
                <span class="rb-field-label">Target</span>
                <span class="rb-field-val"><span class="target-pill">{escape(c_tgt)}</span></span>
              </div>
              <div class="rb-field">
                <span class="rb-field-label">Expected</span>
                <span class="rb-field-val">{escape(c_exp)}</span>
              </div>
              <div class="rb-field">
                <span class="rb-field-label">Timeout</span>
                <span class="rb-field-val">{c_to}s</span>
              </div>
              <div class="rb-field">
                <span class="rb-field-label">On failure</span>
                <span class="rb-field-val">{c_of}</span>
              </div>""" if c_cmd else ""

        verify_cmd = f"""
              <div class="rb-field">
                <span class="rb-field-label">Verify</span>
                <span class="rb-field-val">
                  <code class="rb-verify">{escape(v_cmd)} &#8594; {escape(v_exp)}</code>
                </span>
              </div>""" if v_cmd else ""

        steps_html += f"""
        <div class="rb-step">
          <div class="rb-step-hdr">
            <div class="rb-step-num">{num}</div>
            <span class="rb-step-title">{action}</span>
            {"" if not c_type_badge else f'<span class="type-pill">{c_type_badge}</span>'}
          </div>
          <div class="rb-step-body">
            {"" if not what else f'<div class="rb-field"><span class="rb-field-label">What</span><span class="rb-field-val">{what}</span></div>'}
            {cmds_html}
            {verify_cmd}
          </div>
        </div>"""

    # Rollback
    rollback     = rb.get("rollback", {})
    rollback_html = ""
    if isinstance(rollback, dict):
        rb_desc = escape(rollback.get("description", ""))
        rb_cmds = ""
        for rc in rollback.get("commands", []):
            if isinstance(rc, dict):
                rb_cmds += f'<span class="rollback-cmd">{escape(rc.get("command",""))}</span> '
        rollback_html = f"""
        <div class="rollback-box">
          {rb_desc}
          {"" if not rb_cmds else f"<div>{rb_cmds}</div>"}
        </div>"""
    elif isinstance(rollback, str):
        rollback_html = f'<div class="rollback-box">{escape(rollback)}</div>'

    # Escalation — dual-schema: try nested escalation{} then flat escalation_l1/l2/l3
    esc      = _escalation_levels(rb)
    esc_html = ""
    for level in ["L1", "L2", "L3"]:
        val = escape(esc.get(level, ""))
        if val:
            esc_html += f"""
          <div class="esc-item">
            <div class="esc-level">{level}</div>
            <div class="esc-text">{val}</div>
          </div>"""

    # Post-validation
    pv_html = ""
    for pv in rb.get("post_validation", []):
        if isinstance(pv, str):
            pv_html += f"""
          <div class="pv-item">
            <span>&#9989;</span>
            <div class="pv-content">{escape(pv)}</div>
          </div>"""
        elif isinstance(pv, dict):
            pv_desc = escape(pv.get("description", ""))
            pv_cmd  = escape(pv.get("command", ""))
            pv_exp  = escape(pv.get("expected_output", ""))
            pv_html += f"""
          <div class="pv-item">
            <span>&#9989;</span>
            <div class="pv-content">
              {pv_desc}
              {"" if not pv_cmd else f'<div><span class="pv-cmd">{pv_cmd} &#8594; {pv_exp}</span></div>'}
            </div>
          </div>"""

    return f"""
  <div class="card card-runbook">
    <div class="card-header">
      <div class="icon-circle">&#128214;</div>
      <span class="card-title">Runbook — {applies}</span>
    </div>
    <div class="card-body">
      <div class="rb-meta">
        <div class="rb-meta-item">
          <div class="rb-meta-label">Runbook ID</div>
          <div class="rb-meta-val mono">{rb_id}</div>
        </div>
        <div class="rb-meta-item">
          <div class="rb-meta-label">Applies to</div>
          <div class="rb-meta-val">{applies}</div>
        </div>
        <div class="rb-meta-item">
          <div class="rb-meta-label">Est. resolution</div>
          <div class="rb-meta-val">{est} minutes</div>
        </div>
      </div>
      {"" if not pre_html else f'<div class="sec-label">Pre-conditions</div><div class="prechecks">{pre_html}</div>'}
      {"" if not steps_html else f'<div class="sec-label">Resolution Steps</div><div class="rb-steps">{steps_html}</div>'}
      {"" if not rollback_html else f'<div class="sec-label">Rollback Procedure</div>{rollback_html}'}
      {"" if not esc_html else f'<div class="sec-label">Escalation Path</div><div class="esc-grid">{esc_html}</div>'}
      {"" if not pv_html else f'<div class="sec-label">Post-Resolution Validation</div><div class="pv-list">{pv_html}</div>'}
    </div>
  </div>"""


def build_snow_card(snow: dict, description_override: str = "") -> str:
    if not snow:
        return ""
    priority  = str(snow.get("priority", "1"))
    p_badge   = priority_badge(priority)
    group     = escape(snow.get("assignment_group", ""))
    ci        = escape(snow.get("cmdb_ci", ""))
    category  = escape(snow.get("category", ""))
    subcat    = escape(snow.get("subcategory", ""))
    urgency   = escape(snow.get("urgency", priority))
    impact    = escape(snow.get("impact", priority))
    desc      = escape(description_override or snow.get("description", "")).replace("\\n", "<br>\n")
    wn        = escape(snow.get("work_notes", "")).replace("\\n", "<br>")

    return f"""
  <div class="snow-card">
    <div class="snow-hdr">
      <div class="snow-logo">SN</div>
      <span class="snow-title">ServiceNow Ticket — Auto-Generated</span>
    </div>
    <div class="snow-body">
      <div class="snow-grid">
        <div class="snow-field">
          <span class="snow-field-label">Priority</span>
          <span class="snow-field-val">{p_badge}</span>
        </div>
        <div class="snow-field">
          <span class="snow-field-label">Assignment Group</span>
          <span class="snow-field-val">{group}</span>
        </div>
        <div class="snow-field">
          <span class="snow-field-label">CMDB CI</span>
          <span class="snow-field-val">{ci}</span>
        </div>
        <div class="snow-field">
          <span class="snow-field-label">Category</span>
          <span class="snow-field-val">{category}</span>
        </div>
        <div class="snow-field">
          <span class="snow-field-label">Subcategory</span>
          <span class="snow-field-val">{subcat}</span>
        </div>
        <div class="snow-field">
          <span class="snow-field-label">Urgency / Impact</span>
          <span class="snow-field-val">{urgency} / {impact}</span>
        </div>
      </div>
      <div class="snow-desc">{desc}</div>
      <div class="work-notes">
        <div class="wn-label">Work Notes (Auto-populated)</div>
        <div class="wn-text">{wn}</div>
      </div>
    </div>
  </div>"""


def build_footer(inc: dict) -> str:
    model   = escape(inc.get("model_used", "ibm/granite-4-h-small"))
    inc_id  = escape(inc.get("incident_id", ""))
    gen_at  = escape(inc.get("generated_at", ""))
    return f"""
  <div class="footer">
    Generated by <strong>AIOps-RCA-Analyzer</strong> &nbsp;·&nbsp;
    Powered by <strong>IBM watsonx.ai + Watson Orchestrate</strong> &nbsp;·&nbsp;
    Source: <strong>CP4AIOps canonical payload</strong><br>
    {inc_id} &nbsp;·&nbsp; {gen_at} &nbsp;·&nbsp; Model: {model}
  </div>"""


# ---------------------------------------------------------------------------
# Main report builder
# ---------------------------------------------------------------------------

def build_report_html(inc: dict, title_override: str = None) -> str:
    """Build a complete HTML report for one incident result dict."""
    analysis = inc.get("analysis")
    if not analysis:
        return f"<p>No analysis available for {inc.get('incident_id')}</p>"

    snow = analysis.get("servicenow_ticket", {})
    rb   = analysis.get("runbook", {})

    page_title = title_override or inc.get("title", "AIOps RCA Report")

    # Recommended steps — handle both list of strings and list of objects
    steps = analysis.get("recommended_steps", [])

    body = (
        build_header(inc, analysis)
        + build_text_card("card-summary", "&#128203;", "Executive Summary",
                          analysis.get("summary", ""))
        + build_text_card("card-root", "&#128308;", "Root Cause Explanation",
                          analysis.get("root_cause_explanation", ""))
        + build_text_card("card-corr", "&#128279;", "Correlation Reasoning",
                          analysis.get("correlation_reasoning", ""))
        + build_text_card("card-impact", "&#9888;&#65039;", "Impact Assessment",
                          analysis.get("impact_assessment", ""))
        + build_steps_card(steps)
        + build_runbook_card(rb)
        + build_snow_card(snow, description_override=_build_description_for_snow_card(analysis))
        + build_footer(inc)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(page_title)}</title>
{CSS}
</head>
<body>
<div class="page">
{body}
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not INPUT_FILE.exists():
        print(f"ERROR: {INPUT_FILE} not found. Run watsonx_inference.py first.")
        return

    results: list[dict] = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    OUTPUT_DIR.mkdir(exist_ok=True)

    print(f"Loaded {len(results)} incident(s) from {INPUT_FILE}")
    print(f"Output directory: {OUTPUT_DIR.resolve()}\n")

    generated: list[Path] = []

    for inc in results:
        inc_id = inc.get("incident_id", "unknown")

        if not inc.get("analysis"):
            print(f"  SKIP {inc_id} — no analysis (inference failed)")
            continue

        # Individual report
        html      = build_report_html(inc)
        out_file  = OUTPUT_DIR / f"rca_report_{inc_id}.html"
        out_file.write_text(html, encoding="utf-8")
        generated.append(out_file)
        print(f"  ✓  {out_file.name}")

    # Combined multi-incident report
    if len(results) > 1:
        combined_parts = []
        for i, inc in enumerate(results):
            if not inc.get("analysis"):
                continue
            combined_parts.append(build_header(inc, inc.get("analysis", {})))
            analysis = inc.get("analysis", {})
            snow     = analysis.get("servicenow_ticket", {})
            rb       = analysis.get("runbook", {})
            steps    = analysis.get("recommended_steps", [])
            combined_parts.append(
                build_text_card("card-summary","&#128203;","Executive Summary",
                                analysis.get("summary",""))
                + build_text_card("card-root","&#128308;","Root Cause Explanation",
                                  analysis.get("root_cause_explanation",""))
                + build_text_card("card-corr","&#128279;","Correlation Reasoning",
                                  analysis.get("correlation_reasoning",""))
                + build_text_card("card-impact","&#9888;&#65039;","Impact Assessment",
                                  analysis.get("impact_assessment",""))
                + build_steps_card(steps)
                + build_runbook_card(rb)
                + build_snow_card(snow, description_override=_build_description_for_snow_card(analysis))
                + build_footer(inc)
            )
            if i < len(results) - 1:
                combined_parts.append('<hr class="incident-divider">')

        combined_body = "\n".join(combined_parts)
        combined_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AIOps RCA Report — All Incidents</title>
{CSS}
</head>
<body>
<div class="page">
{combined_body}
</div>
</body>
</html>"""
        combined_file = OUTPUT_DIR / "rca_report_ALL_INCIDENTS.html"
        combined_file.write_text(combined_html, encoding="utf-8")
        generated.append(combined_file)
        print(f"  ✓  {combined_file.name}  (combined — all {len(results)} incidents)")

    print(f"\n{'='*60}")
    print(f"HTML generation complete!")
    print(f"  Reports generated : {len(generated)}")
    print(f"  Output folder     : {OUTPUT_DIR.resolve()}")
    print(f"{'='*60}")
    print("\nOpen in browser:")
    for f in generated:
        print(f"  {f.resolve()}")


if __name__ == "__main__":
    main()