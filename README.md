# Agentic AI-Powered Incident Resolution System

> **Pellera Hackathon 2026 — Powered by IBM**  
> *45 minutes manual → 5 minutes automated. Every resolution makes the next smarter.*

---

## What This Does

Agentic AI Powered Incident Resolution System

This solution delivers an Agentic AI-powered AIOps platform that automates and governs the entire incident lifecycle—from detection and root cause analysis to remediation, validation, and continuous learning—while maintaining strict human-in-the-loop controls for enterprise trust and safety.
The platform integrates IBM Cloud Pak for AIOps (CP4AIOps), large language models (LLMs), Watson Orchestrate, ServiceNow, and Microsoft Teams, and Cisco catalyst 8000 into a cohesive, production grade system capable of resolving complex, multi domain incidents across network, infrastructure, platform, and application layers.

Key Characteristics

1. Agentic architecture with specialized AI agents operating under clear responsibility boundaries
2. LLM powered RCA and runbook generation, grounded in real operational data
3. Human approved autonomous execution on live infrastructure
4. Closed loop learning system that improves accuracy with every validated resolution
5. Enterprise ITSM and collaboration integration with full auditability


Architecture Overview
Agent 1 — Correlator (CP4AIOps Ingestion & Context Builder)

1. Consumes CP4AIOps incidents and alerts (real-time or export)
2. Normalize incident & alerts schema into a single canonical incident
Infers:
	a. Topology relationships
	b. Device OS and interfaces
	c. Golden signals (latency, availability, saturation, errors)
3. Produces a normalized, structured incident payload

Agent 2 — Analyst (LLM‑Driven RCA & Runbook Generator)

1. Retrieves validated RESOLVED knowledge base patterns (RAG)
2. Calls an enterprise LLM with strict system rules to produce:

	a. Root Cause Analysis (RCA), Correlation Reasoning, Impact Assessment, and recommended steps.
	b. Executable, domain-specific runbooks

3. Applies deterministic safeguards:

	a. Command validation
	b Assignment group resolution
	c. CMDB integrity

4. Generates:

	a. Machine readable RCA output
	b. ServiceNow ready incident artifacts
	c. RCA HTML reports
	d. PENDING KB entries

Agent 3 — Notifier & Orchestrator (Human Approval & Execution)

	a. Creates ServiceNow incidents
	b. Notifies SREs via Microsoft Teams and email
	c. Presents RCA and runbook context for review
	d. Waits for explicit human approval
	e. Executes runbooks via controlled SSH / automation only after approval
	f. Captures execution outcome and SRE feedback

Continuous Learning Loop

	a. Successfully resolved incidents automatically promote KB entries from PENDING → RESOLVED
	b. Only human validated resolutions are reused as LLM context
	c. The system becomes faster, safer, and more accurate over time


Business Outcomes

	a. 60–80% reduction in MTTR for recurring incidents
	b. Elimination of inconsistent manual triage
	c. Operational knowledge retained despite team turnover
	d. Safe autonomy with full governance
	e. Scalable across domains, platforms, and teams


```
Hackathon.txt
    │
    ▼
Agent 1 — Correlator       reads CP4AIOps export → watsonx_payload.json
    │
    ▼
Agent 2 — Analyst          AIOps_RCA_Agent: LLM RCA via Watson Orchestrate → rca_output.json
    │                       snow_ready.json, HTML report cards, KB pattern files
    ▼
Agent 3 — Notifier         AIOps_Incident_Resolution_Manager: ServiceNow ticket (via Orchestrate) → Teams + Email → KB upload
    │
    ▼
SRE: opens Orchestrate → types INC number → reviews runbook → types APPROVE
    │
    ▼
Watson Orchestrate          executes runbook steps on live Cisco Cat8k via SSH
    │
    ▼
Resolution recorded         KB marked RESOLVED — next identical incident resolved faster
```

---

## Prerequisites

- Python 3.11+
- IBM Watson Orchestrate instance with:
  - `AIOps_RCA_Agent` deployed (LLM inference)
  - `AIOps_Incident_Resolution_Manager` deployed (ticket creation + runbook execution)
  - `aiops-incident-patterns-kb` knowledge base created
  - `runbook_tool.py` and `mark_resolved_tool.py` registered as ADK Python tools
- ServiceNow developer instance
- IBM watsonx.ai project (optional — only for `--inference-route watsonx`)
- Gmail App Password or other SMTP credentials
- Microsoft Teams Incoming Webhook URL

---
## AIOps_RCA_Agent 

	#Behaviour Instructions
		- Please read Orchestrate_rca_agent_behaviour_after update.txt

	#Knowledgebase
		- Please add aiops-incident-pattern-kb

## AIOps_Incident_Resolution_Manager 

	## Behavior Instructions
		- Please read Orchestrate_Incident Resolution Manager behavior updated instructions.txt
	## Knowledgebase
		- Please add aiops-incident-pattern-kb
	## Tools
		- Please add execute_step

## Built With

- IBM Watson Orchestrate (ticket creation, runbook execution, knowledge base)
- IBM watsonx.ai (optional LLM inference route)
- IBM CP4AIOps (alert correlation and incident data)
- IBM SevOne (network monitoring — source of device metadata: Hackathon.txt)
- Cisco Catalyst 8000 (Always-On DevNet Sandbox — live runbook execution)
- ServiceNow (incident management)
- Microsoft Teams + Gmail (SRE notifications)

----

## Repository Structure

FILES IN THIS PACKAGE
├── agent1_correlator.py        Agent 1 — CP4AIOps export parser
├── agent2_analyst.py           Agent 2 — AIOps_RCA_Agent : LLM RCA + runbook generator
├── agent3_notify.py            Agent 3 — AIOps_Incident_Resolution_Manager ticket creation + notifications
├── kb_utils.py                 Knowledge base read/write utilities
|── kb_sync.py					        KB Resolution Sync
├── runbook_tool.py             Watson Orchestrate ADK tool — SSH execute
|── requirements_runbook_tool.txt Requirements for libraries runbook_tool 
├── config/
│   ├── assignment_groups.yaml  SNOW group mapping (edit for your environment)
│   ├── snow_category_map.yaml  SNOW category mapping
│   └── device_os_map.yaml      Device model → OS class mapping
├── docs/
│   ├── Agentic AI Incident Resolution Manager.png   Architecture
│   └── Agent AI Powered Incident Resolution System Component Flow          Architecture Component flow
|   └── Orchestrate_Incident Resolution Manager behavior updated instructions.txt          Agent behavior instructions update
|   └── Orchestrate_rca_agent_behavior_after update.txt          Agent behavior instructions update
|
├── Hackathon.txt               CP4AIOps export — demo input for Agent 1
├── .env.example                Environment variable template
├── requirements.txt            Python dependencies
└── README.md                   README file
└── .gitignore                  

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/venkataallam/Agentic-AI-Powered-Incident-Resolution-System.git
cd Agentic-AI-Powered-Incident-Resolution-System
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your API keys, SNOW URL, SMTP credentials, etc.
```

If you need to customise ServiceNow assignment groups or category mappings
for your environment, edit the YAML files in `config/` — no code changes required:

```bash
config/assignment_groups.yaml   # Maps alert layer → SNOW group name
config/snow_category_map.yaml   # Maps model category → SNOW OOTB category
config/device_os_map.yaml       # Maps device model string → OS class
```

### 3. Activate Watson Orchestrate ADK

```bash
orchestrate env activate hackathon_vmaa --api-key $ORCHESTRATE_API_KEY
```

```
## Inference Routes

| Flag | Route | Model | Use case |
|---|---|---|---|
| `--inference-route orchestrate` | Watson Orchestrate | GPT OSS 120B | Demo (recommended) |
| `--inference-route watsonx` | IBM watsonx.ai | ibm/granite-3-8b-instruct | Fully IBM stack |



AGENTS IN YOUR ORCHESTRATE INSTANCE
  Agent A (ORCHESTRATE_AGENT_ID)      — AIOps_Incident_Resolution_Manager
                                        
  Agent B (ORCHESTRATE_RCA_AGENT_ID)  — AIOps_RCA_Agent
 

================================================================
STEP 1 — Add 4 lines to your existing .env
================================================================

Open your .env file and add at the bottom:

Update all the environment variables mentioned in .env file


================================================================
STEP 2 — Install ADK and dependencies
================================================================
```bash
  pip install ibm-watsonx-orchestrate
  pip install paramiko python-dotenv
  orchestrate --version    ← confirm install
```
================================================================
STEP 2A. - Create Servicenow connections
================================================================
  Watsonx Orchestrate UI console-> Manage->connections
  Search for ServiceNow in the search behavior (servicenow_ibm_184bdbd3)
  Select servicenow_ibm_184bdbd3
  Provide all the connection details
    Authentication Type->Oauth2Password
    Server Url, Token URL, ClientID, ClientSecret, GrantType->password, CredentialType->Team Members

================================================================
STEP 2B. - Create AIOps_RCA_Agent
================================================================
  Watsonx Orchestrate UI console->Build->Create Agent->Create from scratch
  Name: AIOps_RCA_Agent, description
  Read Orchestrate_rca_agent_behavior_after update.txt file and update behavior instruction in the agent
  Deploy

================================================================
STEP 2C. - Create AIOps_Incident Resolution Manager Agent
================================================================
  Watsonx Orchestrate UI console->Discover->Ticket Manager->use template
  Name: AIOps_Incident Resolution Manager, Update description  
  Deploy

================================================================
STEP 3 — Authenticate ADK to your Orchestrate instance
================================================================

  orchestrate env activate -e .env

Verify it connected:
  orchestrate agents list
  ← should show your agents including AIOps_Incident_Resolution_Agent, AIOps_RCA_Agent


================================================================
STEP 4 — Import the runbook executor tool
================================================================

From your Hackathon project folder:

  orchestrate tools import \
    -k python \
    -f runbook_tool.py \
    -r requirements_runbook_tool.txt

Verify:
  orchestrate tools list
  ← should show execute_step in the list


================================================================
STEP 5 — Add execute_step tool to Agent B
================================================================

OPTION A — Orchestrate UI (simpler):
  Watson Orchestrate → Agents
  → click AIOps_Incident_Resolution_Manager
    (check it matches ORCHESTRATE_RCA_AGENT_ID — not the ticket creation agent)
  → click Tools tab
  → click Add tool
  → find execute_step
  → click Add → Save

OPTION B — ADK CLI:
  orchestrate agents update \
    --name AIOps_Incident_Resolution_Agent \
    --tools execute_step


================================================================
STEP 6 — Create Knowledgebase and Update both AIOps_Incident_Resolution_Manager, AIOps_RCA_Agent with Knowledgebase
================================================================

These 3 files must be uploaded to Agent B's Knowledge Sources.
Agent B searches them by pattern name after fetching the SNOW ticket.

  Watson Orchestrate → Agents
  → click AIOps_Incident_Resolution_Agent
  → click Knowledge tab
  → click Add source → Upload file
  → upload these 3 files one at a time: (You will get these kb files after inital run of agent2_analyst.py. You can find in kb_documents directory)
      kb_link_down_cascade.txt
      kb_auth_failure_cascade.txt
      kb_latency_cascade.txt
  → after all 3 are uploaded, confirm "Chat with documents" toggle is ON
  → Save

  Update agent knowledge base Agent->Knowledgebase->Addsources->add aiops-incident-pattern-kb

================================================================
STEP 7 — Configure Agent : AIOps_Incident_Resolution_Manager behavior instructions
================================================================

WHERE:
  Watson Orchestrate → Agents
  → click AIOps_Incident_Resolution_Agent
  → click Behavior tab
  → Instructions text box
  ->Read Orchestrate_Incident Resolution Manager behavior updated instructions.txt file and update behavior instruction in the agent

WHAT TO DO:
  The text box currently contains the ServiceNow behavior rules
  (from ServicenowBehaviour.txt — governing how SNOW tools are called).

  DO NOT delete those rules.

  APPEND the full contents of orchestrate_rca_agent_behavior.txt
  at the END of the existing instructions, after the last line.

  Final structure:
  ┌──────────────────────────────────────────────┐
  │ [Existing ServiceNow behavior rules]          │ ← keep as-is
  │  ## Role                                      │
  │  You handle requests related to managing...   │
  │  ## Rules for Collecting Required Tool Values │
  │  ...                                          │
  │  ## Scope Control                             │
  │  - Respond only to requests...                │
  │                                               │
  │ [Append below this line]                      │
  │                                               │
  │  ## Role                                      │
  │  You are the AIOps Incident Resolution...     │
  │  ## PHASE 1 — Retrieve the incident           │
  │  ...all 5 phases...                           │
  │  ## Critical rules                            │
  └──────────────────────────────────────────────┘

  Save the agent.
  Click Deploy (or Publish) if shown.


================================================================
STEP 8 — Running Agentic AI Agents
================================================================

8a. Relaunch Catalyst 8000 sandbox:
    DevNet → Catalyst 8000 Always-On → Launch → select 3 days → Launch
    Open I/O tab → copy Cat8k Password
    Update SANDBOX_PASS in .env
  
	  orchestrate connections set-credentials -a cat8k_creds --env draft -e "SANDBOX_HOST=devnetsandboxiosxec8k.cisco.com" -e "SANDBOX_PORT=22" -e "SANDBOX_USER=username" -e "SANDBOX_PASS=password"
	  orchestrate connections set-credentials -a cat8k_creds --env live -e "SANDBOX_HOST=devnetsandboxiosxec8k.cisco.com" -e "SANDBOX_PORT=22" -e "SANDBOX_USER=username" -e "SANDBOX_PASS=password"

	
8b. Create the fault on the device (simulates the P1 incident):
    ssh <SANDBOX_USER>@devnetsandboxiosxec8k.cisco.com
	  Cat8kv_AO_Sandbox#show ip interface brief
    Cat8kv_AO_Sandbox# conf t
    Cat8kv_AO_Sandbox(config)# interface GigabitEthernet3
    Cat8kv_AO_Sandbox(config-if)# shutdown
    Cat8kv_AO_Sandbox(config-if)# end
    Cat8kv_AO_Sandbox# show interface GigabitEthernet3
    ← confirm output shows: "GigabitEthernet3 is down, line protocol is down"

====================================================================
## DEMO STEP 1 — Run the pipeline (Terminal 1)
====================================================================
```powershell
python run_demo.py
```

**Narrate while running:**
- "One command runs all three agents"
- Point to `Agent 1 complete — 3 incident(s)` → "CP4AIOps data extracted, device OS detected as cisco_iosxe"
- Point to `KB context used: 0 incident(s)` → "First run — cold, no prior knowledge"
- Point to ticket creation → "Three P1 tickets created in ServiceNow via Watson Orchestrate"
- Point to email/Teams confirmation → "SRE notified on all channels simultaneously"
- Point to `KB Document Upload Summary: PENDING` → "Knowledge base seeded, waiting for validation"

**Expected output to highlight:**
```
Agent 2 complete
  KB context used      : 0 incident(s)   ← point to this

TICKET CREATION SUMMARY
  incident-network-001  INC0010xxx  ✅ created   ← point to INC numbers
  incident-auth-001     INC0010xxx  ✅ created
  incident-payment-001  INC0010xxx  ✅ created

KB Document Upload Summary:
  ✓ kb_link_down_cascade.txt    uploaded   PENDING   ← explain PENDING
```

---
==================================================================
## DEMO STEP 2 — Teams Adaptive notification card
==================================================================
Open Microsoft Teams, find the notification channel.

**Narrate:**
- "The SRE receives this adaptive card instantly"
- Show the three action buttons: View Ticket, View RCA Report, Open Watson Orchestrate
- Click View RCA Report → shows IBM-styled HTML report card-"Full root cause analysis, correlation reasoning, runbook steps — all generated by GPT-OSS 120B"
- Click on View Ticket, will open a ServiceNow incident
      - "ServiceNow ticket created with P1 priority, correct assignment group, full RCA in description"
      - Show the description field → "The entire runbook is embedded — no manual entry"
      - "Assignment group was determined by the alert layer — Network Operations for network incidents"
- Click on Open Watson Orchestrator, it will launch AIOps_Incident_Resolution_Manager Agent chat

================================================================
FULL END-TO-END FLOW (what happens when SRE clicks "Open Orchestrate")
================================================================

  SRE opens Watson Orchestrate → AIOps_Incident_Resolution_Agent

  SRE: INC0010045
  Agent: calls Get Incidents → description returned
         extracts Pattern: link_down_cascade
         searches KB → finds kb_link_down_cascade.txt
         presents:
           INC0010045 — CORE-RTR-01 Network Service Degradation
           Pattern: link_down_cascade | Est: 30 min
           KB history: Seen 1 time before. Avg 30 min.
           Root cause: [2 sentences]
           4 runbook steps ready
           Type APPROVE / STEPS / REJECT

  SRE: APPROVE
  Agent: calls execute_step step 1 → real Cat8k SSH → real IOS-XE output
         calls execute_step step 2 → no shutdown → GigabitEthernet3 is up
         calls execute_step step 3 → show ip route → route confirmed
         calls execute_step step 4 → interface health → up/up
         "All 4 steps complete. Is CORE-RTR-01 operating normally?"

  SRE: YES
  Agent: calls Get State → Resolved sys_id
         calls Update Incident → INC0010045 = Resolved
         searches KB → pattern exists → no update needed
         "INC0010045 Resolved. Resolution complete."
          Type in the chat Update Incident work notes as Runbook Worked
          If Runbook steps did not work SRE can escaled to L1/L2 team
          Type in the chat Update Incident work notes as Escalated
      

---
===================================================================
## DEMO STEP 5 — Start kb_sync (Terminal 2)
===================================================================
```powershell
python kb_sync.py --interval 30
```

**While showing Watson Orchestrate:**
- "In the background, kb_sync is polling ServiceNow every 30 seconds"

**In ServiceNow:**
- Open the ticket and make sure it is in resolved state

**Switch to Terminal 2 — narrate:**
```
[KB-SYNC] 'Runbook Worked' found ✓ — marking KB
[KB-SYNC] kb_link_down_cascade.txt marked STATUS: RESOLVED ✓
```
- "The learning loop just closed. Automatically. No manual step."

---
==================================================================
## DEMO STEP 6 — The warm run (Terminal 1)
==================================================================
```powershell
python run_demo.py --skip-agent1
```

**Narrate while running:**
- Point to `KB context used: 3 incident(s)` → "All three incidents now have KB context"
- Point to `KB match found — enriching prompt` → "Validated steps from the last resolution injected"
- Compare response time → "3 seconds vs 9 seconds on the first run"
- Point to `kb_link_down_cascade.txt already RESOLVED — skipping PENDING overwrite` → "The system protects validated knowledge"

**Key line to highlight:**
```
[KB-RAG] ✓ 1 RESOLVED match(es) for 'link_down_cascade' — injecting into LLM prompt
[1/3] incident-network-001 ... KB match found — enriching prompt

Agent 2 complete
  KB context used      : 3 incident(s)   ← THIS IS THE LEARNING LOOP
```
## The Learning Loop

```
Run 1 (cold):   LLM generates RCA from scratch
                KB file written as PENDING

SRE approves → runbook executes → steps validated
                KB file marked RESOLVED

Run 2 (warm):   Agent 2 finds RESOLVED KB entry
                Injects validated steps into LLM prompt
                RCA quality improves · resolution time drops
```

---

---
=================================================================
## DEMO STEP 7 — Closing statement
=================================================================
Point to the two run summaries side by side.

"Run 1: 0 KB context, 9 seconds per incident.
Run 2: 3 KB context, 3 seconds per incident.
Same input. Better output. The system learned."

---


================================================================
TROUBLESHOOTING
================================================================

"execute_step returns SSH auth failed":
  → Sandbox was relaunched — password changed
  → DevNet → I/O tab → copy new Cat8k Password
  → Update SANDBOX_PASS in .env
  → orchestrate tools import -k python -f runbook_tool.py -r requirements_runbook_tool.txt

"Agent says tool execute_step not found":
  → orchestrate tools list — check it appears
  → If missing: reimport with the command in Step 4
  → If present but agent not calling it: check Tools tab — add it to Agent B

"KB articles not being found":
  → Confirm files uploaded to Agent B 
  → Confirm "Chat with documents" toggle is ON
  → Wait 2-3 minutes after upload for indexing
  → Try searching manually in the Knowledge tab

"GigabitEthernet3 comes back up on its own":
  → Shared device — another user may have run no shutdown
  → Run shutdown again in conf t, verify with show ip interface brief

"orchestrate env activate fails":
  → Check WATSONX_ORCHESTRATE_URL is set in .env
    (same value as ORCHESTRATE_INSTANCE_URL)
  → Check WATSONX_ORCHESTRATE_API_KEY is set
    (same value as ORCHESTRATE_API_KEY)

"Agent not presenting the runbook steps":
  → The description field format must match exactly
  → Run: python agent2_analyst.py (regenerates snow_ready.json)
  → Then: python agent3_notify.py (creates fresh tickets with correct format)
  → Use the new INC numbers for the demo


## Architecture

See `docs/production_roadmap.md` for the full production evolution plan including
CMDB integration, multi-tenant support, multi-OS runbook templates, and automated
KB resolved marking.

---
