# cat8k_ping_tool.py
# update SANDBOX_USER and SANDBOX_PASS
import paramiko
import time
import re
from ibm_watsonx_orchestrate.agent_builder.tools import tool
from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.run import connections

@tool(expected_credentials=[{"app_id": "cat8k_creds", "type": ConnectionType.KEY_VALUE}])
def cat8k_ping(command: str = "show clock") -> str:
    """Test SSH connectivity to the Cat8k sandbox from inside the Orchestrate runtime.

    Args:
        command (str): IOS-XE command to run. Defaults to 'show clock'.

    Returns:
        str: Diagnostic result showing each connection stage and command output.
    """
    log = []
    creds = connections.key_value("cat8k_creds")
    host  = creds.get("SANDBOX_HOST", "devnetsandboxiosxec8k.cisco.com")
    port  = int(creds.get("SANDBOX_PORT", 22))
    user  = creds.get("SANDBOX_USER", "XXXXX")
    pwd   = creds.get("SANDBOX_PASS", "XXXXX")

    log.append(f"HOST={host} PORT={port} USER={'SET' if user else 'MISSING'} PASS={'SET' if pwd else 'MISSING'}")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host, port=port, username=user, password=pwd,
            timeout=10, banner_timeout=12, auth_timeout=10,
            look_for_keys=False, allow_agent=False,
        )
        log.append("TCP+AUTH: OK")

        shell = client.invoke_shell(width=220, height=50)
        buf = ""
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if shell.recv_ready():
                buf += shell.recv(4096).decode("utf-8", errors="replace")
                if re.search(r'[#>]\s*$', buf, re.MULTILINE):
                    break
            else:
                time.sleep(0.1)
        log.append(f"BANNER: {len(buf)} bytes, prompt={'YES' if buf else 'NO'}")

        shell.send(command + "\n")
        out = ""
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if shell.recv_ready():
                out += shell.recv(4096).decode("utf-8", errors="replace")
                if re.search(r'[#>]\s*$', out, re.MULTILINE):
                    break
            else:
                time.sleep(0.1)
        log.append(f"COMMAND OUTPUT:\n{out.strip()}")
        log.append("RESULT: CONNECTIVITY OK")

    except paramiko.AuthenticationException:
        log.append("RESULT: AUTH FAILED — update SANDBOX_PASS in cat8k_creds connection")
    except Exception as e:
        log.append(f"RESULT: FAILED — {type(e).__name__}: {e}")
    finally:
        client.close()

    return "\n".join(log)