#!/usr/bin/env python3
"""F-12 test: Agent sends tool_call then immediately cancels it."""
import socketio, time, sys, threading
from a2c_smcp.smcp import (
    SMCP_NAMESPACE, JOIN_OFFICE_EVENT, TOOL_CALL_EVENT,
    CANCEL_TOOL_CALL_EVENT, UPDATE_SKILLS_NOTIFICATION,
)

port = open("/tmp/a2c-uat-port").read().strip()
url = f"http://127.0.0.1:{port}"
office = "test-office-001"
comp = "proto-comp-001"
agent_name = "f12-agent"

result = {"pass": False, "notes": []}

client = socketio.Client()

@client.on("connect", namespace=SMCP_NAMESPACE)
def on_connect():
    print("AGENT_CONNECTED", flush=True)

@client.on(UPDATE_SKILLS_NOTIFICATION, namespace=SMCP_NAMESPACE)
def on_update_skills(data):
    print(f"NOTIFY:update_skills: {data}", flush=True)

client.connect(url, socketio_path="/socket.io", namespaces=[SMCP_NAMESPACE], transports=["polling"], wait=True, wait_timeout=10)

# Join office
resp = client.call(JOIN_OFFICE_EVENT, {"role": "agent", "name": agent_name, "office_id": office}, namespace=SMCP_NAMESPACE, timeout=10)
print(f"Join: {resp}", flush=True)

# Send slow tool_call, then cancel
print("\n=== F-12: tool_call_cancel ===", flush=True)

# Send tool_call in a thread so we can cancel immediately
call_result = {"done": False, "resp": None}

def do_call():
    r = client.call(
        TOOL_CALL_EVENT,
        {"computer": comp, "tool_name": "slow_echo", "agent": agent_name,
         "req_id": "F-12-call", "params": {"msg": "hello"}, "timeout": 30000},
        namespace=SMCP_NAMESPACE, timeout=35,
    )
    call_result["done"] = True
    call_result["resp"] = r
    print(f"TOOL_CALL response: {r}", flush=True)

t = threading.Thread(target=do_call, daemon=True)
t.start()

# Wait a moment for the call to be sent, then cancel
time.sleep(1)

print("Sending cancel...", flush=True)
cancel_resp = client.call(
    CANCEL_TOOL_CALL_EVENT,
    {"computer": comp, "req_id": "F-12-cancel", "agent": agent_name},
    namespace=SMCP_NAMESPACE, timeout=10,
)
print(f"Cancel response: {cancel_resp}", flush=True)

# Wait for the original call to complete
t.join(timeout=15)

if call_result["done"]:
    resp = call_result["resp"]
    if resp is None:
        result["notes"].append("tool_call returned None (cancelled)")
        result["pass"] = True
    elif isinstance(resp, dict) and resp.get("code"):
        result["notes"].append(f"tool_call returned error: code={resp.get('code')}, msg={resp.get('message')}")
        result["pass"] = True
    elif isinstance(resp, dict) and resp.get("isError"):
        result["notes"].append(f"tool_call returned isError=True")
        result["pass"] = True
    else:
        result["notes"].append(f"tool_call returned unexpected: {type(resp)} {resp}")
else:
    result["notes"].append("tool_call timed out (15s) — may indicate cancel did not work")

# Check if cancel response is meaningful
if cancel_resp:
    result["notes"].append(f"cancel_resp: {cancel_resp}")
else:
    result["notes"].append("cancel_resp was None/empty")

print(f"\nF-12: {'PASS' if result['pass'] else 'FAIL'}")
for n in result["notes"]:
    print(f"  {n}")

client.disconnect()
sys.exit(0 if result["pass"] else 1)
