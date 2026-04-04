# Production Roadmap

## Agentic AI-Powered Incident Resolution System — Pellera Hackathon 2026

This document describes the known architectural decisions made for the hackathon
submission and the concrete production evolution path for each.

---

## Decision Log

### D1 — Failure Pattern Taxonomy (Tier E — deferred)

**Current state:**  
`_PATTERN_KEYWORDS` in `agent2_analyst.py` maps keyword combinations to Rule 19
failure pattern enums (e.g. `link_down_cascade`, `dependency_failure`). The same
10 enums are listed in the system prompt `applies_to` instruction as a hardcoded
string. These two representations exist separately and must stay manually in sync.

**Why deferred:**  
Tier E requires generating the `applies_to` instruction text dynamically from the
same source file that drives `_PATTERN_KEYWORDS`. Any bug in the dynamic assembly
produces malformed JSON from the LLM — a high-risk change close to the demo date.
The current system works and the two are currently in sync.

**Production path:**  
1. Create `config/failure_patterns.yaml` with each pattern's name, description,
   and keyword vocabulary.
2. At agent2 startup, load the YAML and build both `_PATTERN_KEYWORDS` and the
   `applies_to` instruction text from the same source.
3. The system prompt `applies_to` field becomes dynamic:  
   `f"MUST be one of: {' | '.join(patterns.keys())}"`
4. Adding a new failure pattern requires only a YAML entry — no code change.

---

### D2 — CMDB Integration for Topology (upstream/downstream)

**Current state:**  
`TOPO_RULES` in `agent1_correlator.py` infers upstream and downstream service
names from keyword matching on the incident text. Service names like `EDGE-RTR-01`,
`api-gateway`, `user-session-service` are hardcoded in the rules.

**Why this is correct for the hackathon:**  
The CP4AIOps topology insights in Hackathon.txt contain internal group IDs
(`CORE_NETWORK_GROUP`, policy IDs) not human-readable service names. The rules
generate meaningful names the SRE can act on. This is accurate for the demo topology.

**Production path:**  
1. Integrate with CMDB (ServiceNow CMDB or IBM Instana) to fetch real service
   dependency graphs at incident time.
2. Agent 1 queries `GET /api/now/table/cmdb_rel_ci?parent={ci_name}` to retrieve
   actual upstream and downstream CIs.
3. `TOPO_RULES` is retired. Topology comes entirely from CMDB data.
4. For environments without CMDB integration, `TOPO_RULES` remains as a fallback
   loaded from `config/topology_rules.yaml`.

---

### D3 — Device OS Detection: Layer 2 (payload-based, per-incident)

**Current state (Layer 1):**  
Agent 1 reads `details.Device Model` from CP4AIOps/SevOne alert details, normalises
it through `config/device_os_map.yaml`, and writes `topology.device_os` into the
payload. Agent 2 reads this and conditionally includes IOS XE CLI rules.

**Production path (Layer 2):**  
The `details.Device Model` field is SevOne-specific. Other monitoring platforms
(Dynatrace, Datadog, Prometheus) provide device metadata in different fields.
Production Agent 1 should query the CMDB device record for the primary CI and
retrieve `hardware_model` and `software_version` directly, making the OS detection
platform-agnostic.

**Production path (Layer 3 — multi-OS runbook templates):**  
Device-OS-specific runbook rule blocks (currently only IOS XE exists) are managed
as YAML template files: `config/device_rules_cisco_iosxe.yaml`,
`config/device_rules_junos.yaml`, etc. Agent 2 loads the appropriate template at
runtime based on `topology.device_os`. Adding Juniper support requires only a new
YAML file — no Python changes.

---

### D4 — ServiceNow Category Runtime Lookup

**Current state:**  
`config/snow_category_map.yaml` maps model-generated category names to SNOW OOTB
category values. This map is correct for `dev293798` (standard OOTB instance).
Different SNOW instances may have different category names.

**Production path:**  
At Agent 3 startup, query `GET /api/now/table/sys_choice?name=incident&element=category`
to retrieve the live list of valid categories from the target SNOW instance.
Build the `SNOW_CATEGORY_MAP` dynamically from this response. The YAML file becomes
a fallback for environments where the SNOW API is not accessible from the agent host.

---

### D5 — Assignment Group Runtime Lookup

**Current state:**  
`config/assignment_groups.yaml` maps alert layer + keyword to SNOW group names.
These are correct for the demo environment but are static configuration.

**Production path:**  
Query `GET /api/now/table/sys_user_group?active=true` at startup to get the live
list of SNOW groups. Use LLM semantic matching (or exact name matching after the
model output is normalised) against the live list. This ensures that when an
operations team renames or restructures their SNOW groups, Agent 2 adapts without
a configuration file update.

---

### D6 — Multi-Tenant Support

**Current state:**  
All configuration (SNOW instance, Orchestrate instance, KB name, email recipient)
is per-deployment via `.env`. Running two customers requires two separate deployments.

**Production path:**  
Introduce a `tenant_id` field in the CP4AIOps payload (from the alert's `tenantId`
field, which is already present in Hackathon.txt as a UUID). Agent 1 writes this
to the payload. Agents 2 and 3 route API calls to the correct SNOW instance and
Orchestrate instance based on tenant config loaded from a `config/tenants.yaml`
lookup. One deployment serves multiple customers.

---

### D7 — Automated KB Resolved Marking (closing the learning loop)

**Current state:**  
After an SRE approves a runbook in Watson Orchestrate and the `execute_step` tool
runs successfully, `mark_resolved_tool.py` marks the local KB file as RESOLVED.
This step is triggered by the SRE typing a manual command after the demo.

**Production path:**  
`mark_resolved_tool.py` is registered as a Watson Orchestrate ADK Python tool
and is called automatically by `AIOps_Incident_Resolution_Manager` at the end
of a successful runbook execution — no manual SRE step required. The learning
loop closes without human intervention.

---

## Summary Timeline

| Phase | Scope | Effort |
|---|---|---|
| Hackathon (current) | 3 demo incidents, 1 device OS, manual KB marking | Done |
| v1.0 | D7 automated loop + D3 Layer 2 CMDB device query | 1 sprint |
| v1.1 | D4 + D5 SNOW runtime lookup, D1 Tier E pattern YAML | 1 sprint |
| v2.0 | D2 full CMDB topology integration | 2 sprints |
| v2.1 | D6 multi-tenant, D3 Layer 3 multi-OS templates | 2 sprints |
