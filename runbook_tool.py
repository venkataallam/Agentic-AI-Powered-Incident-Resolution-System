"""
runbook_tool.py  —  AIOps Runbook Executor (Watson Orchestrate ADK Tool)
=========================================================================
Agentic AI-Powered Incident Resolution System — Pellera Hackathon 2026

ADK GUARDRAILS (per developer.watson-orchestrate.ibm.com/tools/create_tool):
  - Single @tool annotated function per file                  ✅
  - Google-style docstring (Args / Returns sections)          ✅
  - All types declared with Python typings                    ✅
  - expected_credentials declared on @tool decorator          ✅
  - ConnectionType.KEY_VALUE used for key-value connection    ✅
  - No module-level credential globals                        ✅
  - Read-only filesystem assumed — no file writes             ✅
  - Python 3.12 compatible (ADK runtime requirement)          ✅
  - requirements.txt manages all external deps                ✅

DEVICE MAPPING — CRITICAL DESIGN DECISION:
  The runbook target field contains logical CMDB names (e.g. CORE-RTR-01).
  These are NOT real hostnames. There is no real CORE-RTR-01 router.
  ALL network targets map to one physical device:
    → Cisco Cat8kv Always-On DevNet Sandbox
    → Host/port/user/pass come exclusively from ADK Connection 'cat8k_creds'
  The target name is used only for routing logic and result display.
  It is NEVER used as an SSH hostname.

REAL DEVICE:
  Host : devnetsandboxiosxec8k.cisco.com  (SANDBOX_HOST in cat8k_creds)
  Port : 22                                (SANDBOX_PORT in cat8k_creds)
  User : ramireddy.allam                   (SANDBOX_USER in cat8k_creds)
  Pass : <from DevNet I/O tab>             (SANDBOX_PASS in cat8k_creds)

IOS-XE MODE DESIGN — KEY ARCHITECTURE DECISION:
  Cisco IOS-XE has three distinct CLI modes:
    PRIVILEGED EXEC   prompt: CAT8K#
    GLOBAL CONFIG     prompt: CAT8K(config)#
    INTERFACE CONFIG  prompt: CAT8K(config-if)#

  The runbook_tool runs EACH STEP in a FRESH SSH session that always
  starts in PRIVILEGED EXEC mode. This is the safest and most predictable
  approach — no mode state bleeds between steps.

  VERIFY COMMANDS always run in PRIVILEGED EXEC mode:
    Before sending the verify command, the tool automatically sends 'end'
    to return from any config mode to EXEC mode. This ensures that
    'show interface GigabitEthernet3', 'show running-config', etc. always
    work correctly regardless of what mode the main command left the shell in.

  The Agent 2 system prompt generates runbook steps with:
    - config mode steps (configure terminal / interface X / no shutdown / end)
      as SEPARATE steps, each with a single command
    - verify_command always as a PRIVILEGED EXEC show command
    - 'end' step between config steps and the final verification step

  The tool handles this correctly by:
    1. Detecting if the shell is in config mode after the main command
    2. Sending 'end' before the verify command if needed
    3. Using the correct prompt pattern for each mode

CREDENTIAL SETUP (once per env; update SANDBOX_PASS after each sandbox relaunch):
  orchestrate connections set-credentials -a cat8k_creds --env draft
    -e "SANDBOX_HOST=devnetsandboxiosxec8k.cisco.com"
    -e "SANDBOX_PORT=22" -e "SANDBOX_USER=username"
    -e "SANDBOX_PASS=<password from DevNet I/O tab>"
  orchestrate connections set-credentials -a cat8k_creds --env live
    -e "SANDBOX_HOST=devnetsandboxiosxec8k.cisco.com"
    -e "SANDBOX_PORT=22" -e "SANDBOX_USER=username"
    -e "SANDBOX_PASS=<password from DevNet I/O tab>"

IMPORT COMMAND:
  orchestrate tools import -k python -f runbook_tool.py -r requirements_runbook_tool.txt --app-id cat8k_creds

ALL FIXES APPLIED:
  FIX-1:  banner_timeout + auth_timeout on paramiko.connect()
  FIX-2:  _recv_until_prompt() poll loop — no fixed time.sleep()
  FIX-3:  Single SSH session reused for command + verify
  FIX-4:  State-propagation sleep increased 2s → 4s for Cat8kv vNIC timing
          Evidence: execution data showed attempt 1 at 4.27s → verify_passed=false
          (vNIC still negotiating), retry at 4.15s → verify_passed=true.
          Cat8kv virtual NICs need 3-8s for ESXi hypervisor link negotiation.
  FIX-12: Replaced fixed time.sleep(4) with a verify poll loop (see _ssh_run).
          Root cause: 4s sleep was insufficient when Cat8kv vNIC took >6s
          (confirmed: duration_ms=6346, verify_passed=false in execution data).
          Poll loop sends verify command, checks result, sleeps 1.5s, retries.
          Exits immediately on verify_passed=true -- no unnecessary waiting.
          Worst case: 3 polls x (3s recv + 1.5s sleep) = 13.5s, total 28.5s.
  FIX-5:  IOS_PROMPT_RE — full-line prompt match (^\S+[#>]\s*$)
  FIX-6:  Compound config commands batched — one recv
  FIX-7:  Always use cat8k_creds directly — no target-derived lookup
  FIX-8:  _clean_output uses full-line prompt match — no false stripping
  FIX-9:  Auto 'end' before verify — returns shell to EXEC mode
          Fixes: '% Invalid input detected at ^ marker' on verify
          Root cause: shell was in config mode when verify ran
  FIX-10: _is_in_config_mode() detects config prompt in buffer
          Uses CAT8K(config)# and CAT8K(config-if)# patterns
  FIX-11: Added "cat8k" to REAL_SSH_TARGETS set.
          The SNOW runbook uses "CAT8K" as the target name (confirmed from
          snow_ready.json). Without this, _use_real_ssh("CAT8K") falls
          through to the substring check — which does catch "cat8k" in "cat8k"
          — but explicit membership is cleaner and more reliable.

DEVICE TOPOLOGY (confirmed from Cat8k sandbox):
  GigabitEthernet1 = 10.10.20.148 — MANAGEMENT — NEVER TOUCH
  GigabitEthernet2 = 10.2.2.1     — LAN
  GigabitEthernet3 = 10.3.3.1     — WAN Interface  ← demo interface
"""

import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import paramiko
from ibm_watsonx_orchestrate.agent_builder.tools import tool
from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.run import connections

# ── Constants ─────────────────────────────────────────────────────────────────
SANDBOX_HOST_DEFAULT = "devnetsandboxiosxec8k.cisco.com"
SANDBOX_PORT_DEFAULT = 22

# SSH timeout budget — Orchestrate hard tool timeout is ~30s.
# Worst case (config command batch + auto-end + poll loop x3):
#   TCP connect   :  6s
#   SSH banner    :  6s  (overlaps connect)
#   Banner drain  :  4s
#   Cmd batch     :  3s  (all config lines + rapid 0.1s gaps, one recv)
#   Auto-end      :  1s  (send 'end', recv prompt -- fast)
#   Poll loop     : 13.5s (3 x (3s recv + 1.5s sleep)) -- worst case all 3 polls
#   Overhead      :  1s
#   TOTAL         : 28.5s -- safely inside 30s limit
#
# FIX-12: Replaced fixed time.sleep(4) with a verify poll loop.
# Root cause of verify_passed=false on 'Enable Interface':
#   Cat8kv uses virtual NICs (media type: Virtual on ESXi).
#   After 'no shutdown', the hypervisor virtual switch must re-negotiate
#   the link. This takes 3-8 seconds -- real Catalyst hardware is instant.
#   The old fixed 4s sleep was not enough when vNIC took >6s (confirmed
#   from execution data: duration_ms=6346, verify_passed=false).
#
# Poll loop design:
#   - Sends the verify command, checks the output immediately.
#   - If verify_passed=true  -> exits early (no unnecessary waiting).
#   - If verify_passed=false -> waits VERIFY_POLL_INTERVAL seconds and retries.
#   - Exits after VERIFY_POLL_MAX_ATTEMPTS regardless.
#   - Worst case: 3 x (3s recv + 1.5s sleep) = 13.5s -- covers vNIC 8s window.
#   - Best case: 1 x 3s recv -- exits immediately if interface is already up.
SSH_CONNECT_TIMEOUT  = 6
SSH_BANNER_TIMEOUT   = 6
SSH_AUTH_TIMEOUT     = 6
RECV_POLL_INTERVAL   = 0.05
RECV_TIMEOUT         = 3
BANNER_DRAIN_TIMEOUT = 4

# FIX-12: Verify poll loop constants for Cat8kv vNIC state-propagation.
# Replaces the fixed time.sleep(4) that was insufficient when the ESXi
# hypervisor took >4s to negotiate the virtual link after 'no shutdown'.
# Worst case: 3 x (RECV_TIMEOUT + VERIFY_POLL_INTERVAL) = 13.5s
# This covers the full 3-8s vNIC window with headroom.
VERIFY_POLL_MAX_ATTEMPTS = 3    # max verify retries before giving up
VERIFY_POLL_INTERVAL     = 1.5  # seconds to wait between verify attempts

# IOS-XE prompt patterns — FIX-5 / FIX-8 / FIX-10:
#
# EXEC prompt        : CAT8K#              (no parentheses)
# Config prompt      : CAT8K(config)#      (with parentheses)
# Intf config prompt : CAT8K(config-if)#   (with parentheses)
#
# IOS_PROMPT_RE     — matches any IOS-XE prompt (EXEC or config mode)
# IOS_CONFIG_RE     — matches config mode prompts specifically (has parens)
# IOS_EXEC_RE       — matches EXEC mode only (no parens before #)
#
# All patterns require the ENTIRE stripped line to be the prompt.
IOS_PROMPT_RE = re.compile(r'^\S+[#>]\s*$')
IOS_CONFIG_RE = re.compile(r'^\S+\(config[^)]*\)#\s*$')
IOS_EXEC_RE   = re.compile(r'^\S+#\s*$')

# Commands that put IOS-XE into config mode — triggers auto-end before verify
CONFIG_ENTRY_COMMANDS = {
    "configure terminal",
    "conf t",
    "conf term",
}

# Network target keywords — ALL map to cat8k_creds SSH connection.
# IMPORTANT: The SNOW runbook uses "CAT8K" as the target name (confirmed
# from snow_ready.json and execution data). "cat8k" must be in this set
# AND matched by the "cat8k" substring check in _use_real_ssh() below.
REAL_SSH_TARGETS = {
    "core-rtr-01",
    "edge-rtr-01",
    "cat8kv_ao_sandbox",
    "cat8kv",
    "cat8k",       # ← ADD: actual target name used in SNOW runbook steps
}


# ── Routing ───────────────────────────────────────────────────────────────────

def _use_real_ssh(target: str) -> bool:
    """Return True if this target should SSH to the Cat8k sandbox.

    Args:
        target (str): Logical CMDB device name from the SNOW runbook step.

    Returns:
        bool: True for network targets; False for simulated targets.
    """
    t = target.lower().strip()
    return (
        t in REAL_SSH_TARGETS
        or "rtr" in t
        or "router" in t
        or "switch" in t
        or "sw-" in t
        or "cat8k" in t
        or "gigabit" in t
    )


# ── IOS-XE mode detection ─────────────────────────────────────────────────────

def _is_in_config_mode(buf: str) -> bool:
    """Return True if the last prompt in buf indicates config mode.

    FIX-10: Detects CAT8K(config)# and CAT8K(config-if)# patterns.
    Used to decide whether to send 'end' before running verify command.

    Args:
        buf (str): Raw output buffer from the SSH channel after sending a command.

    Returns:
        bool: True if the shell is currently in any IOS-XE config mode.
    """
    for line in reversed(buf.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if IOS_CONFIG_RE.match(stripped):
            return True
        if IOS_EXEC_RE.match(stripped):
            return False
    return False


def _commands_enter_config(commands: list[str]) -> bool:
    """Return True if any command in the list enters IOS-XE config mode.

    Args:
        commands (list[str]): List of IOS-XE commands to be sent.

    Returns:
        bool: True if the sequence includes a configure terminal command.
    """
    return any(c.lower().strip() in CONFIG_ENTRY_COMMANDS for c in commands)


# ── Command adapter ────────────────────────────────────────────────────────────

def _adapt_command(command: str) -> str:
    """Translate a runbook command to valid IOS-XE syntax for the Cat8k sandbox.

    Fixes known invalid IOS-XE patterns from AI-generated runbooks:
      1. 'show interface GigabitEthernetN status' → 'show interfaces GigabitEthernetN'
         (singular 'interface' + trailing 'status' is not valid IOS-XE)
      2. 'show interface GigabitEthernetN | ...' → 'show interfaces GigabitEthernetN'
         (pipe on invoke_shell buffers unreliably; full output is safer)

    Note: 'show ip bgp summary | include X' left as-is — BGP exec commands
    handle pipe filtering reliably on IOS-XE.

    Args:
        command (str): Raw command string from the SNOW runbook step.

    Returns:
        str: IOS-XE compatible command string.
    """
    cmd = command.strip()
    if re.search(r'show\s+interface\s+GigabitEthernet\d+\s+status', cmd, re.IGNORECASE):
        iface = re.search(r'GigabitEthernet\d+', cmd).group(0)
        return f'show interfaces {iface}'
    if re.search(r'show\s+interface\s+GigabitEthernet\d+\s*\|', cmd, re.IGNORECASE):
        iface = re.search(r'GigabitEthernet\d+', cmd).group(0)
        return f'show interfaces {iface}'
    return cmd


def _clean_verify_cmd(verify_cmd: str) -> str:
    """Strip 'on <TARGET>' suffix and fix IOS-XE syntax in a verify command.

    SNOW format : 'show interfaces GigabitEthernet3 on CORE-RTR-01'
    IOS-XE needs: 'show interfaces GigabitEthernet3'

    Args:
        verify_cmd (str): Raw verify command possibly with 'on TARGET' suffix.

    Returns:
        str: Clean IOS-XE verify command.
    """
    cmd = re.sub(r'\s+on\s+\S+\s*$', '', verify_cmd.strip())
    return _adapt_command(cmd)


# ── SSH helpers ────────────────────────────────────────────────────────────────

def _recv_until_prompt(shell: paramiko.Channel, timeout: float = RECV_TIMEOUT) -> str:
    """Poll SSH channel until any IOS-XE prompt appears or timeout expires.

    FIX-2: Replaces fixed time.sleep(). Returns immediately on prompt detection.

    Args:
        shell (paramiko.Channel): Open interactive SSH channel.
        timeout (float): Max seconds to wait before returning partial output.

    Returns:
        str: Raw decoded text including the prompt line.
    """
    buf = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if shell.recv_ready():
            chunk = shell.recv(4096).decode("utf-8", errors="replace")
            buf += chunk
            for line in buf.splitlines():
                if IOS_PROMPT_RE.match(line.strip()):
                    return buf
        else:
            time.sleep(RECV_POLL_INTERVAL)
    return buf


def _clean_output(raw: str, sent_commands: list[str]) -> str:
    """Strip ANSI codes, IOS-XE prompt lines, and command echoes from raw output.

    FIX-8: Uses IOS_PROMPT_RE (full-line match) to identify prompt lines.
    Only strips lines where the ENTIRE stripped content is a bare prompt
    (e.g. 'CAT8K#', 'CAT8K(config)#'). Real output lines are preserved.

    Args:
        raw (str): Raw SSH channel text.
        sent_commands (list[str]): Sent commands, for echo removal.

    Returns:
        str: Clean output ready for verify matching and agent display.
    """
    clean = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', raw)
    clean = clean.replace('\r\n', '\n').replace('\r', '\n')
    cmd_set = {ln.strip().lower() for ln in sent_commands if ln.strip()}
    output_lines = []
    for ln in clean.splitlines():
        stripped = ln.strip()
        if not stripped:
            continue
        if IOS_PROMPT_RE.match(stripped):
            continue
        if stripped.lower() in cmd_set:
            continue
        output_lines.append(stripped)
    result = "\n".join(output_lines).strip()
    return result if result else "[command executed — no output returned]"


def _ssh_run(
    commands: list[str],
    verify_cmd: Optional[str],
    verify_expected: str,
    action: str,
    host: str,
    port: int,
    user: str,
    password: str,
) -> tuple[bool, str, Optional[str]]:
    """Open ONE SSH session to the Cat8k sandbox, run commands + verify, close.

    CRITICAL: host/port/user/password come exclusively from cat8k_creds.
    The runbook target name (CORE-RTR-01 etc.) is NEVER passed here.

    IOS-XE MODE HANDLING (FIX-9 / FIX-10):
    After executing the main command(s), if the shell is in config mode
    (CAT8K(config)# or CAT8K(config-if)#), the tool automatically sends
    'end' to return to EXEC mode before running the verify command.
    This fixes '% Invalid input detected at ^ marker' errors that occur
    when show commands are sent while still in config mode.

    The Agent 2 system prompt generates steps where:
      - Each config-mode action is a separate step with ONE command
      - verify_command is always a PRIVILEGED EXEC show command
    The auto-end here is a safety net for both cases.

    FIX-6: Compound config sequences batched — sent rapidly, one recv.

    Args:
        commands (list[str]): IOS-XE commands pre-split from the runbook.
        verify_cmd (Optional[str]): Cleaned verify command, or None.
        verify_expected (str): Expected substring for early-exit polling.
            Empty string disables early exit (all polls run).
        action (str): Step action title — passed through for result context.
        host (str): Cat8k SSH hostname from SANDBOX_HOST.
        port (int): Cat8k SSH port from SANDBOX_PORT.
        user (str): SSH username from SANDBOX_USER.
        password (str): SSH password from SANDBOX_PASS.

    Returns:
        tuple[bool, str, Optional[str]]:
            success       — True if SSH and execution succeeded
            output        — Cleaned output from main command(s)
            verify_output — Cleaned output from verify command, or None
    """
    if not user or not password:
        return False, (
            "Cat8k credentials missing in cat8k_creds connection. "
            "Run: orchestrate connections set-credentials -a cat8k_creds --env live "
            "-e SANDBOX_USER=<user> -e SANDBOX_PASS=<password>"
        ), None

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        # FIX-1: banner_timeout covers IOS-XE multi-line SSH legal banner
        client.connect(
            hostname       = host,
            port           = port,
            username       = user,
            password       = password,
            timeout        = SSH_CONNECT_TIMEOUT,
            banner_timeout = SSH_BANNER_TIMEOUT,
            auth_timeout   = SSH_AUTH_TIMEOUT,
            look_for_keys  = False,
            allow_agent    = False,
        )

        shell = client.invoke_shell(width=220, height=50)
        # FIX-2: poll for prompt instead of fixed sleep
        _recv_until_prompt(shell, timeout=BANNER_DRAIN_TIMEOUT)

        # ── Execute main command(s) ────────────────────────────────────────
        # FIX-6: Batch compound config sequences (conf t/iface/no shut/end)
        # to avoid RECV_TIMEOUT × N blowing the 30s Orchestrate limit.
        if len(commands) > 1:
            for line in commands:
                shell.send(line + "\n")
                time.sleep(0.1)   # brief gap for IOS-XE mode processing
            raw_main = _recv_until_prompt(shell, timeout=RECV_TIMEOUT)
        else:
            shell.send(commands[0] + "\n")
            raw_main = _recv_until_prompt(shell, timeout=RECV_TIMEOUT)

        main_output = _clean_output(raw_main, commands)

        # ── Execute verify command (FIX-3: same session) ──────────────────
        verify_output: Optional[str] = None
        if verify_cmd:
            # FIX-9: AUTO-END before verify command.
            # Problem observed in execution data:
            #   Step 2 command: 'configure terminal'
            #   → shell enters CAT8K(config)# mode
            #   Step 2 verify:  'show running-config | include hostname'
            #   → IOS-XE returns '% Invalid input detected at ^ marker'
            #     because 'show' is not valid in config mode
            #
            # Solution: detect config mode from the raw_main buffer and
            # send 'end' to return to EXEC mode before the verify command.
            # This is safe for all cases:
            #   - If already in EXEC mode: 'end' does nothing / returns to EXEC
            #   - If in config mode: 'end' returns to EXEC mode cleanly
            #
            # FIX-10: _is_in_config_mode() checks the last prompt in buffer.
            if _is_in_config_mode(raw_main) or _commands_enter_config(commands):
                shell.send("end\n")
                _recv_until_prompt(shell, timeout=RECV_TIMEOUT)

            # FIX-12: Verify poll loop for Cat8kv vNIC state-propagation.
            #
            # Problem with old fixed time.sleep(4):
            #   Cat8kv uses virtual NICs (media type: Virtual on ESXi).
            #   After 'no shutdown', the ESXi hypervisor must re-negotiate
            #   the virtual link. This takes 3-8 seconds. When the vNIC
            #   took >4s (confirmed: duration_ms=6346, verify_passed=false),
            #   the fixed sleep was insufficient and the first verify always
            #   returned 'line protocol is down', forcing a RETRY prompt
            #   in the middle of a live P1 incident.
            #
            # Fix: poll the verify command in a loop:
            #   - Send verify command immediately (no upfront sleep).
            #   - If the expected output is present, exit immediately.
            #   - If not, wait VERIFY_POLL_INTERVAL seconds and retry.
            #   - Exit after VERIFY_POLL_MAX_ATTEMPTS regardless.
            #
            # This approach:
            #   - Exits early on fast vNICs (no wasted 4s sleep).
            #   - Covers the full 8s worst-case vNIC window (3 polls x 4.5s).
            #   - Stays within the 30s Orchestrate hard timeout (total 28.5s).
            #   - Passes the final verify_output to the result regardless
            #     of whether polling succeeded, so the agent sees real output.
            raw_verify = ""
            verify_output_candidate = ""
            for _poll_attempt in range(VERIFY_POLL_MAX_ATTEMPTS):
                shell.send(verify_cmd + "\n")
                raw_verify = _recv_until_prompt(shell, timeout=RECV_TIMEOUT)
                verify_output_candidate = _clean_output(raw_verify, [verify_cmd])
                # Exit early if the expected output is already present
                if verify_expected and verify_expected.lower() in verify_output_candidate.lower():
                    break
                # Not matched yet — wait before next poll (skip sleep on last attempt)
                if _poll_attempt < VERIFY_POLL_MAX_ATTEMPTS - 1:
                    time.sleep(VERIFY_POLL_INTERVAL)
            verify_output = verify_output_candidate

        return True, main_output, verify_output

    except paramiko.AuthenticationException:
        return False, (
            "SSH auth failed — sandbox password has changed. "
            "Get new password from DevNet I/O tab and run: "
            "orchestrate connections set-credentials -a cat8k_creds --env live "
            "-e SANDBOX_PASS=<new_password>"
        ), None
    except paramiko.NoValidConnectionsError:
        return False, (
            f"Cannot reach Cat8k sandbox at {host}:{port}. "
            "Check SANDBOX_HOST in cat8k_creds and outbound port 22 access."
        ), None
    except paramiko.SSHException as exc:
        return False, f"SSH protocol error: {exc}", None
    except OSError as exc:
        return False, f"Network error: {type(exc).__name__}: {exc}", None
    except Exception as exc:
        return False, f"Unexpected error: {type(exc).__name__}: {exc}", None
    finally:
        client.close()


# ── Simulation engine ──────────────────────────────────────────────────────────

_SIMULATION: dict[tuple[str, str], tuple[str, str]] = {
    ("auth-service", "ping"):
        ("4 packets transmitted, 4 received, 0% packet loss, time 3003ms\n"
         "rtt min/avg/max/mdev = 1.218/1.374/1.812/0.241 ms", "0"),
    ("ldap-server", "ldapsearch"):
        ("dn: dc=example,dc=com\nobjectClass: top\n"
         "dc: example\nnumResponses: 1", "0"),
    ("auth-service", "systemctl restart"):
        ("Stopping auth-service.service...\nStopped auth-service.service.\n"
         "Starting auth-service.service...\nStarted auth-service.service.", "active"),
    ("auth-service", "systemctl is-active"): ("active", "active"),
    ("auth-service", "curl"):               ("200", "0"),
    ("auth-service", "top"):
        ("  PID USER      %CPU  COMMAND\n"
         "12847 authsvc    2.3  auth-service\n"
         "CPU utilisation 2.3% — below 80% threshold", "0"),
    ("payment-db", "mysqladmin"):
        ("Uptime: 284  Threads: 4  Questions: 19847  Slow queries: 0\n"
         "Latency: 1.2s", "Latency: 1.2s"),
    ("payment-service-pod-1", "kubectl rollout"):
        ("deployment.apps/payment-service restarted", "Running Running Running"),
    ("payment-api-pod-1", "curl"):
        ("200", '{"status":"ok","latency":"38ms","db":"connected"}'),
    ("payment-api-pod-1", "grep"):
        ('{"status":"ok","latency":"38ms"}', '"status":"ok"'),
}


def _simulate(
    target: str,
    command: str,
    verify_expected: str,
) -> tuple[bool, str, str, bool]:
    """Return simulated output for non-network targets.

    Args:
        target (str): Target service name from the runbook step.
        command (str): Command string to simulate.
        verify_expected (str): Expected verify output substring.

    Returns:
        tuple[bool, str, str, bool]: (success, output, verify_output, verify_passed)
    """
    t = target.lower().strip()
    c = command.lower().strip()
    for (sim_target, sim_fragment), (output, verify_out) in _SIMULATION.items():
        if t == sim_target.lower() and sim_fragment.lower() in c:
            vp = verify_expected.lower() in verify_out.lower() if verify_expected else True
            return True, output, verify_out, vp
    return (
        True,
        f"[simulated] Command executed successfully on {target}",
        verify_expected or "OK",
        True,
    )


# ── ADK Tool ──────────────────────────────────────────────────────────────────

@tool(expected_credentials=[{"app_id": "cat8k_creds", "type": ConnectionType.KEY_VALUE}])
def execute_step(
    incident_id:     str,
    step_number:     str,
    action:          str,
    command:         str,
    target:          str,
    expected_output: str,
    on_failure:      str,
    verify_command:  str = "",
    verify_expected: str = "",
) -> str:
    """Execute one runbook step on the target device or service.

    For ALL network targets (CORE-RTR-01, EDGE-RTR-01, or any router/switch
    name from the SNOW runbook), SSHs into the Cisco Cat8kv DevNet Always-On
    sandbox using ONLY credentials from the cat8k_creds ADK Connection.
    The target name is a logical CMDB identifier — never an SSH hostname.

    IOS-XE mode handling: If the main command puts the shell into config mode
    (configure terminal, interface X, no shutdown), the tool automatically
    sends 'end' before running the verify command so that show commands work
    correctly. This prevents '% Invalid input detected at ^ marker' errors.

    For non-network targets (auth-service, payment-db, ldap-server), returns
    realistic simulated output without any network calls.

    IOS-XE command syntax is auto-corrected before execution:
      'show interface GigabitEthernetN status' → 'show interfaces GigabitEthernetN'
      'show interface GigabitEthernetN | ...'  → 'show interfaces GigabitEthernetN'

    Extract all values from the SNOW ticket description field.
    Runbook steps follow this exact format in the SNOW description:

        STEP N: <action>
          Command: <command>
          Target : <target>
          Expect : <expected_output>
          OnFail : <on_failure>
          Verify : <verify_command> on <target> -> <verify_expected>

    For verify parameters:
      verify_command  = left side of ' -> ', stripped of 'on <TARGET>' suffix
      verify_expected = right side of ' -> '

    Args:
        incident_id (str): ServiceNow INC number, e.g. INC0010074.
        step_number (str): Step number as string, e.g. "1".
        action (str): Step action title, e.g. "Check interface status".
        command (str): Exact command from the Command field in the runbook.
            Single commands: 'show interfaces GigabitEthernet3'
            Compound commands use newline or semicolon separator:
            'configure terminal\ninterface GigabitEthernet3\nno shutdown\nend'
        target (str): Logical CMDB device name, e.g. CORE-RTR-01.
            This is a display name only — NEVER used as SSH hostname.
        expected_output (str): Expected output substring from the Expect field.
        on_failure (str): Failure policy: retry_once, stop_and_escalate, continue.
        verify_command (str): Verify command from left of ' -> ' in Verify line.
            Strip any 'on <TARGET>' suffix before passing.
            Always a PRIVILEGED EXEC show command (never a config-mode command).
            Empty string if the step has no Verify line.
        verify_expected (str): Expected verify output from right of ' -> '.
            Empty string if the step has no Verify line.

    Returns:
        str: JSON with fields: run_id, incident_id, step_number, action,
             target, command, adapted_command, success, output, verify_output,
             verify_passed, duration_ms, timestamp, message.
    """
    t_start = time.monotonic()
    run_id  = str(uuid.uuid4())[:8].upper()
    ts      = datetime.now(tz=timezone.utc).isoformat()

    vcmd_raw = verify_command.strip()
    vexp_raw = verify_expected.strip()

    if _use_real_ssh(target):
        # ── Real SSH to Cat8k sandbox ──────────────────────────────────────
        # FIX-7: Always use cat8k_creds directly — no target-derived lookup.
        creds = connections.key_value("cat8k_creds")
        host  = creds.get("SANDBOX_HOST", SANDBOX_HOST_DEFAULT)
        port  = int(creds.get("SANDBOX_PORT", SANDBOX_PORT_DEFAULT))
        user  = creds.get("SANDBOX_USER", "")
        pwd   = creds.get("SANDBOX_PASS", "")

        # Translate to valid IOS-XE syntax
        adapted   = _adapt_command(command)
        # Split compound commands on semicolons or newlines
        cmd_lines = [ln.strip() for ln in re.split(r'[;\n]', adapted) if ln.strip()]

        # Clean verify command
        vcmd_clean: Optional[str] = None
        if vcmd_raw:
            vcmd_clean = _clean_verify_cmd(vcmd_raw)

        success, output, verify_output = _ssh_run(
            commands         = cmd_lines,
            verify_cmd       = vcmd_clean,
            verify_expected  = vexp_raw,
            action           = action,
            host             = host,
            port             = port,
            user             = user,
            password         = pwd,
        )

        verify_passed = True
        if verify_output is not None and vexp_raw:
            verify_passed = vexp_raw.lower() in verify_output.lower()

    else:
        # ── Simulation for auth / payment / Linux service targets ──────────
        adapted = command
        success, output, verify_output, verify_passed = _simulate(
            target, command, vexp_raw
        )

    duration_ms = int((time.monotonic() - t_start) * 1000)

    if success and verify_passed:
        msg = (
            f"Step {step_number} — {action} COMPLETE on {target}. "
            f"Command executed. Verification passed."
        )
    elif success and not verify_passed:
        msg = (
            f"Step {step_number} — {action}: command ran on {target} "
            f"but verification did not match. "
            f"Expected: {vexp_raw!r} | Got: {verify_output!r}"
        )
    else:
        msg = (
            f"Step {step_number} — {action} FAILED on {target}. "
            f"on_failure: {on_failure}. Error: {output}"
        )

    result = {
        "run_id":          run_id,
        "incident_id":     incident_id,
        "step_number":     step_number,
        "action":          action,
        "target":          target,
        "command":         command,
        "adapted_command": adapted,
        "success":         success,
        "output":          output,
        "verify_output":   verify_output,
        "verify_passed":   verify_passed,
        "duration_ms":     duration_ms,
        "timestamp":       ts,
        "message":         msg,
    }
    return json.dumps(result, ensure_ascii=False)