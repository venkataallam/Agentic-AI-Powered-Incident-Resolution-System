"""
agent1_correlator.py  —  Agent 1: Correlator
=============================================
Agentic AI-Powered Incident Resolution System — Pellera Hackathon 2026

ROLE:
  Agent 1 reads the CP4AIOps incident export (Hackathon.txt),
  extracts and correlates alerts into incidents, builds the
  canonical payload schema, and hands off to Agent 2.

IN PRODUCTION:
  CP4AIOps would trigger this via webhook when a P1 incident
  is detected. For this prototype we read the static export file.

INPUT:
  Hackathon.txt  — CP4AIOps raw export (mixed JSON)

OUTPUT:
  watsonx_payload.json  — canonical incident+alert payload for Agent 2

USAGE:
  python agent1_correlator.py
  python agent1_correlator.py --input MyExport.txt
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ORGANIZATION_NAME = os.getenv("ORGANIZATION_NAME", "Pellera Hackathon 2026")
PROJECT_NAME      = os.getenv("PROJECT_NAME",      "Agentic AI-Powered Incident Resolution System")

# ── Output ──────────────────────────────────────────────────────────────────
PAYLOAD_FILE  = Path("watsonx_payload.json")
DEFAULT_INPUT = Path("Hackathon.txt")
CONFIG_DIR    = Path(__file__).parent / "config"

# ── Severity mapping (CP4AIOps numeric → label) ─────────────────────────────
SEVERITY_MAP = {
    6: "Critical", 5: "Major", 4: "Minor",
    3: "Warning",  2: "Warning", 1: "Warning",
}

# ── Domain patterns for incident_id normalisation ───────────────────────────
DOMAIN_PATTERNS = [
    (re.compile(r"router|network|bgp|ospf|snmp|interface|link|wan", re.I), "network"),
    (re.compile(r"auth|ldap|login|session|sso|token|identity",       re.I), "auth"),
    (re.compile(r"payment|transaction|checkout|billing|invoice",      re.I), "payment"),
    (re.compile(r"storage|disk|volume|san|nas|iops",                  re.I), "storage"),
    (re.compile(r"kubernetes|k8s|container|pod|openshift",            re.I), "platform"),
    (re.compile(r"app|service|api|http|deploy",                       re.I), "app"),
    (re.compile(r"cpu|memory|host|server|hardware|blade",             re.I), "infra"),
]

# ── Topology rules (inferred from incident content) ──────────────────────────
TOPO_RULES = [
    (re.compile(r"snmp|router|bgp|ospf|interface|network|link|wan", re.I), {
        "upstream":   ["NOC-MONITOR"],
        "downstream": ["EDGE-RTR-01", "Application Services", "Monitoring Platform"],
    }),
    (re.compile(r"auth|ldap|login|session|sso|identity",            re.I), {
        "upstream":   ["ldap-server", "identity-provider"],
        "downstream": ["api-gateway", "user-session-service"],
    }),
    (re.compile(r"payment|transaction|checkout|billing",             re.I), {
        "upstream":   ["payment-db", "fraud-detection-service"],
        "downstream": ["payment-api", "api-gateway", "notification-service"],
    }),
    (re.compile(r"kubernetes|k8s|container|pod|openshift",           re.I), {
        "upstream":   ["control-plane"],
        "downstream": ["hosted-pods", "services"],
    }),
    (re.compile(r"cpu|memory|host|server|hardware",                  re.I), {
        "upstream":   [],
        "downstream": ["virtual-machines", "hosted-applications"],
    }),
]

# ── Layer + signal inference ─────────────────────────────────────────────────
LAYER_KW = [
    (re.compile(r"interface|bgp|ospf|snmp|routing|router|switch|link|wan|lan", re.I), "network"),
    (re.compile(r"cpu|memory|disk|power|hardware|fan|host|server|blade",        re.I), "infrastructure"),
    (re.compile(r"kubernetes|k8s|container|pod|openshift|cluster",              re.I), "platform"),
    (re.compile(r"application|service|api|http|auth|ldap|payment|transaction",  re.I), "application"),
]
SIGNAL_KW = [
    (re.compile(r"latency|slow|delay|response.?time|rtt",               re.I), "latency"),
    (re.compile(r"outage|unavailable|unreachable|offline|polling.?fail", re.I), "availability"),
    (re.compile(r"utiliz|saturat|capacity|overload|queue|exhaust",       re.I), "saturation"),
    (re.compile(r"traffic|throughput|bandwidth|packet|flow",             re.I), "traffic"),
    (re.compile(r"error|fail|down|lost|drop|exception|crash|reset",      re.I), "errors"),
]
CAUSE_KW = re.compile(
    r"snmp.?poll|polling.?fail|device.?unreachable|hardware.?fail"
    r"|power.?fail|link.?down|interface.?err|db.?connect|ldap.?timeout",
    re.I,
)
COMP_PATTERNS = [
    re.compile(r"\b(GigabitEthernet[\w/\.]+)\b",  re.I),
    re.compile(r"\b(FastEthernet[\w/\.]+)\b",      re.I),
    re.compile(r"\b([A-Z][A-Z0-9]{1,}-[A-Z0-9][-A-Z0-9]*(?:-\d+)?)\b"),
    re.compile(r"\b([a-z][a-z0-9]+-[a-z][a-z0-9]+(?:-[a-z0-9]+)*)\b"),
    re.compile(r"\b(BGP|OSPF|SNMP(?:-Agent)?|LDAP|DNS|NTP)\b", re.I),
]
DENY_WORDS = {
    "for","the","and","on","in","is","to","of","at","by","from","with",
    "was","are","has","not","failed","down","error","alert","polling",
    "check","monitor","status","event","incident","warning","critical",
}


# ── Device OS map ─────────────────────────────────────────────────────────────

def _load_device_os_map() -> tuple[list[tuple[str, str]], str]:
    """Load config/device_os_map.yaml. Returns (mappings, default_os_class)."""
    try:
        import yaml
        path = CONFIG_DIR / "device_os_map.yaml"
        if path.exists():
            data    = yaml.safe_load(path.read_text(encoding="utf-8"))
            entries = data.get("mappings", [])
            mapping = [(e["match"], e["os_class"]) for e in entries if "match" in e]
            default = data.get("default_os_class", "linux")
            return mapping, default
    except Exception:
        pass
    return [], "linux"


_DEVICE_OS_MAP, _DEFAULT_OS_CLASS = _load_device_os_map()


def normalize_device_os(device_model: str) -> str:
    """Map a Device Model string to a normalized OS class."""
    if not device_model:
        return _DEFAULT_OS_CLASS
    for model_substr, os_class in sorted(
        _DEVICE_OS_MAP, key=lambda x: len(x[0]), reverse=True
    ):
        if model_substr.lower() in device_model.lower():
            return os_class
    return _DEFAULT_OS_CLASS


# ── Helpers ──────────────────────────────────────────────────────────────────

def epoch_iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return str(ts)


def infer_layer(text: str) -> str:
    for pat, label in LAYER_KW:
        if pat.search(text):
            return label
    return "infrastructure"


def infer_signal(text: str) -> str:
    for pat, label in SIGNAL_KW:
        if pat.search(text):
            return label
    return "errors"


def extract_alert_details(alert_obj: dict) -> dict:
    """Extract device metadata from alert.details (SevOne/CP4AIOps fields)."""
    raw = alert_obj.get("details", {})
    if isinstance(raw, str):
        try:
            import ast as _ast
            raw = _ast.literal_eval(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "device_model": str(raw.get("Device Model", "") or ""),
        "device_name":  str(raw.get("Device Name",  "") or ""),
        "device_ip":    str(raw.get("Device IP",    "") or ""),
        "object_name":  str(raw.get("Object Name",  "") or ""),
        "sevone_type":  str(raw.get("SevOne Alert Type", "") or ""),
    }


def extract_components(alerts: list[dict]) -> list[str]:
    components: list[str] = []
    seen: set[str] = set()
    for a in alerts:
        text = f"{a.get('summary','')} {a.get('resource','')}"
        for pat in COMP_PATTERNS:
            for m in pat.findall(text):
                e = m.strip()
                if e and e.lower() not in DENY_WORDS and len(e) > 2 and e not in seen:
                    components.append(e)
                    seen.add(e)
    return components[:15]


# ── Core extraction ──────────────────────────────────────────────────────────

def extract_payload(input_file: Path) -> list[dict]:
    print(f"\n[AGENT 1] Reading CP4AIOps export: {input_file.name}")

    # Read with encoding fallback
    content = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            content = input_file.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        content = input_file.read_text(encoding="utf-8", errors="replace")
        print("  Warning: unrecognised bytes replaced with ?")

    # Brace-depth JSON scanner
    raw_objects: list[dict] = []
    depth, start = 0, None
    for i, ch in enumerate(content):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    raw_objects.append(json.loads(content[start : i + 1]))
                except json.JSONDecodeError:
                    pass
                start = None

    print(f"  Parsed {len(raw_objects)} JSON objects from export")

    # Separate alerts from incidents
    alerts_map:  dict[str, dict] = {}
    incidents:   list[dict]     = []

    for obj in raw_objects:
        if "summary" in obj and "severity" in obj and "resource" in obj:
            aid = obj.get("id")
            if aid:
                sev  = obj.get("severity")
                blob = obj.get("summary", "")
                _det = extract_alert_details(obj)
                alerts_map[aid] = {
                    "alert_id":       aid,
                    "summary":        obj.get("summary"),
                    "severity":       SEVERITY_MAP.get(int(sev) if sev else 0, "Warning"),
                    "resource":       obj.get("resource", {}).get("name"),
                    "timestamp":      epoch_iso(obj.get("firstOccurrenceTime")),
                    "classification": obj.get("type", {}).get("classification"),
                    "layer":          infer_layer(blob),
                    "signal_type":    infer_signal(blob),
                    "symptom":        not bool(CAUSE_KW.search(blob)),
                    "device_model":   _det["device_model"],
                    "object_name":    _det["object_name"],
                }
        elif "alertIds" in obj:
            incidents.append(obj)

    print(f"  Found {len(alerts_map)} alerts, {len(incidents)} incidents")

    # Build canonical payload per incident
    _counters: dict[str, int] = {}

    def norm_id(blob: str) -> str:
        domain = "general"
        for pat, label in DOMAIN_PATTERNS:
            if pat.search(blob):
                domain = label
                break
        _counters[domain] = _counters.get(domain, 0) + 1
        return f"incident-{domain}-{_counters[domain]:03d}"

    payload: list[dict] = []

    for inc in incidents:
        blob = " ".join([
            inc.get("title", ""),
            inc.get("description", ""),
            *[alerts_map.get(aid, {}).get("summary", "")
              for aid in inc.get("alertIds", [])],
        ])

        # Probable cause from CP4AIOps insights
        pc_id = None
        for ins in inc.get("insights", []):
            if ins.get("type") == "aiops.ibm.com/insight-type/probable-cause":
                if ins.get("details", {}).get("rank") == 1:
                    pc_id = ins["details"].get("id")
                    break

        # Topology inference
        upstream: list[str]   = []
        downstream: list[str] = []
        for pat, rules in TOPO_RULES:
            if pat.search(blob):
                upstream   = list(rules["upstream"])
                downstream = list(rules["downstream"])
                break


        root_node = (alerts_map.get(pc_id, {}).get("resource", "unknown")
                     if pc_id else "unknown")


        # Related alerts sorted by timestamp
        related    = [alerts_map[aid]
                      for aid in inc.get("alertIds", [])
                      if aid in alerts_map]
        with_ts    = sorted([a for a in related if a.get("timestamp")],
                            key=lambda a: a["timestamp"])
        without_ts = [a for a in related if not a.get("timestamp")]
        ordered    = with_ts + without_ts

        # Device OS — from Device Model field in alert details
        # Drives conditional IOS-XE rule selection in Agent 2.
        all_device_models = [
            a.get("device_model", "") for a in related
            if a.get("device_model")
        ]
        device_os = normalize_device_os(
            all_device_models[0] if all_device_models else ""
        )

        # Interface names — from SevOne Object Name field for network alerts
        _iface_pat = re.compile(
            r"^(GigabitEthernet|TenGigabitEthernet|FastEthernet|HundredGigE"
            r"|FortyGigabitEthernet|Bundle-Ether|Loopback|Tunnel|Serial|Vlan)",
            re.IGNORECASE,
        )
        interface_names = list({
            a["object_name"] for a in related
            if a.get("object_name") and _iface_pat.match(a["object_name"])
        })

        # Event timeline
        timeline = [
            {
                "step":      i + 1,
                "alert_id":  a["alert_id"],
                "event":     a["summary"],
                "timestamp": a["timestamp"],
                "severity":  a["severity"],
                "layer":     a["layer"],
                "resource":  a.get("resource"),
                "symptom":   a["alert_id"] != pc_id,
            }
            for i, a in enumerate(ordered)
        ]

        # Golden signals
        all_text = " ".join(a.get("summary", "") for a in related)
        golden   = {
            "latency":    bool(re.search(r"latency|slow|delay|rtt",     all_text, re.I)),
            "errors":     bool(re.search(r"error|fail|down|crash|reset", all_text, re.I)),
            "saturation": bool(re.search(r"cpu|memory|utiliz|exhaust",   all_text, re.I)),
            "traffic":    bool(re.search(r"traffic|packet|throughput",   all_text, re.I)),
        }

        incident_id = norm_id(blob)
        payload.append({
            "incident": {
                "incident_id":  incident_id,
                "title":        inc.get("title"),
                "description":  inc.get("description"),
                "priority":     inc.get("priority", 1),
                "created_time": epoch_iso(inc.get("createdTime")),
                "source":       "cp4aiops",
            },
            "probable_cause": {
                "alert_id":   pc_id,
                "summary":    alerts_map.get(pc_id, {}).get("summary", ""),
                "confidence": "high" if pc_id else "medium",
            } if pc_id else None,
            "event_timeline": timeline,
            "topology": {
                "root_node":           root_node,
                "device_os":           device_os,
                "interface_names":     interface_names,
                "upstream":            upstream,
                "downstream":          downstream,
                "affected_components": extract_components(related),
            },
            "alerts":         related,
            "golden_signals": golden,
        })

        priority = inc.get("priority", 1)
        print(f"  ✓  {incident_id}  "
              f"P{priority}  |  "
              f"alerts={len(related)}  |  "
              f"root={root_node}")

    PAYLOAD_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n  Output → {PAYLOAD_FILE}  ({len(payload)} incident(s))")
    return payload


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent 1 — Correlator: CP4AIOps payload extraction"
    )
    parser.add_argument(
        "--input", default=str(DEFAULT_INPUT),
        help=f"CP4AIOps export file (default: {DEFAULT_INPUT})"
    )
    args = parser.parse_args()

    input_file = Path(args.input)
    if not input_file.exists():
        print(f"ERROR: Input file not found: {input_file}")
        return

    print("=" * 60)
    print("Agent 1 — Correlator")
    print(f"{PROJECT_NAME} — {ORGANIZATION_NAME}")
    print("=" * 60)

    payload = extract_payload(input_file)

    print("\n" + "=" * 60)
    print(f"Agent 1 complete — {len(payload)} incident(s) extracted")
    print(f"Handoff to Agent 2: {PAYLOAD_FILE.resolve()}")
    print("=" * 60)


if __name__ == "__main__":
    main()