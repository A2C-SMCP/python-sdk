#!/usr/bin/env python3
"""F-06 test: Agent sends tool_call, Computer disconnects, then reconnects."""
import socketio, time, sys, threading
from a2c_smcp.smcp import SMCP_NAMESPACE, JOIN_OFFICE_EVENT, TOOL_CALL_EVENT

port = open("/tmp/a2c-uat-port").read().strip()
url = f"http://127.0.0.1:{port}"
office = "test-office-001"
comp = "proto-comp-001"
agent_name = "f06-agent"

phase1 = {"pass": False, "notes": []}
phase2 = {"pass": False, "notes": []}

client = socketio.Client()

@client.on("connect", namespace=SMCP_NAMESPACE)
def on_connect():
    print("AGENT_CONNECTED", flush=True)

client.connect(url, socketio_path="/socket.io", namespaces=[SMCP_NAMESPACE], transports=["polling"], wait=True, wait_timeout=10)

# Join office
resp = client.call(JOIN_OFFICE_EVENT, {"role": "agent", "name": agent_name, "office_id": office}, namespace=SMCP_NAMESPACE, timeout=10)
print(f"Join: {resp}", flush=True)

# Phase 1: Send tool_call, Computer will be killed during execution
print("\n=== F-06 Phase 1: tool_call during disconnect ===", flush=True)
print("WAITING_FOR_KILL: Computer will be killed now...", flush=True)

call_result = {"done": False, "resp": None}

def do_slow_call():
    r = client.call(
        TOOL_CALL_EVENT,
        {"computer": comp, "tool_name": "slow_echo", "agent": agent_name,
         "req_id": "F-06-p1", "params": {"msg": "before-kill"}, "timeout": 30000},
        namespace=SMCP_NAMESPACE, timeout=35,
    )
    call_result["done"] = True
    call_result["resp"] = r
    print(f"PHASE1_TOOL_CALL response: {r}", flush=True)

t = threading.Thread(target=do_slow_call, daemon=True)
t.start()

# Wait for the call to be in-flight, signal that Computer should be killed
time.sleep(1)
print("KILL_SIGNAL_SENT: kill Computer now!", flush=True)

# Wait for the call to finish (should get error since Computer is dead)
t.join(timeout=20)

if call_result["done"]:
    resp = call_result["resp"]
    if resp is None:
        phase1["pass"] = True
        phase1["notes"].append("tool_call returned None (Computer disconnected)")
    elif isinstance(resp, dict) and resp.get("code"):
        phase1["pass"] = True
        phase1["notes"].append(f"tool_call returned error: code={resp.get('code')}")
    elif isinstance(resp, dict) and resp.get("isError"):
        phase1["pass"] = True
        phase1["notes"].append("tool_call returned isError=True")
    else:
        phase1["notes"].append(f"tool_call returned unexpected: {type(resp)} {resp}")
else:
    phase1["notes"].append("tool_call timed out (20s) without response")

# Phase 2: Wait for Computer to reconnect, then try tool_call again
print("\n=== F-06 Phase 2: tool_call after reconnect ===", flush=True)
print("WAITING_FOR_RECONNECT: restart Computer now!", flush=True)

# Wait for user to restart Computer
time.sleep(15)
print("Attempting tool_call after reconnect wait...", flush=True)

try:
    resp2 = client.call(
        TOOL_CALL_EVENT,
        {"computer": comp, "tool_name": "big_image", "agent": agent_name,
         "req_id": "F-06-p2", "params": {}, "timeout": 15000},
        namespace=SMCP_NAMESPACE, timeout=20,
    )
    print(f"PHASE2_TOOL_CALL response: {resp2}", flush=True)
    if isinstance(resp2, dict) and not resp2.get("code"):
        phase2["pass"] = True
        phase2["notes"].append("tool_call succeeded after reconnect")
    elif isinstance(resp2, dict) and resp2.get("code"):
        phase2["notes"].append(f"tool_call error after reconnect: code={resp2.get('code')}")
    else:
        phase2["notes"].append(f"tool_call returned: {type(resp2)} {resp2}")
except Exception as e:
    phase2["notes"].append(f"tool_call exception: {e}")

overall = phase1["pass"] and phase2["pass"]
print(f"\nF-06 Phase1: {'PASS' if phase1['pass'] else 'FAIL'}")
for n in phase1["notes"]:
    print(f"  {n}")
print(f"F-06 Phase2: {'PASS' if phase2['pass'] else 'FAIL'}")
for n in phase2["notes"]:
    print(f"  {n}")
print(f"\nF-06 Overall: {'PASS' if overall else 'FAIL'}")

client.disconnect()
sys.exit(0 if overall else 1)
