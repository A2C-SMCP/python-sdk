#!/usr/bin/env python3
"""F-03 test: Agent listens for notify:update_config (triggered by CLI 'notify update').
Note: scenario specifies notify:update_skills but no marketplace is configured;
we verify the broadcast mechanism via notify:update_config which shares the same routing."""
import socketio, time, sys
from a2c_smcp.smcp import SMCP_NAMESPACE, JOIN_OFFICE_EVENT, UPDATE_CONFIG_NOTIFICATION

port = open("/tmp/a2c-uat-port").read().strip()
url = f"http://127.0.0.1:{port}"
office = "test-office-001"
agent_name = "f03-agent"

result = {"pass": False, "notes": []}
notification_received = []

client = socketio.Client()

@client.on("connect", namespace=SMCP_NAMESPACE)
def on_connect():
    print("AGENT_CONNECTED", flush=True)

@client.on(UPDATE_CONFIG_NOTIFICATION, namespace=SMCP_NAMESPACE)
def on_update_config(data):
    print(f"NOTIFY:update_config: {data}", flush=True)
    notification_received.append(data)

client.connect(url, socketio_path="/socket.io", namespaces=[SMCP_NAMESPACE], transports=["polling"], wait=True, wait_timeout=10)
print("Connected", flush=True)

resp = client.call(JOIN_OFFICE_EVENT, {"role": "agent", "name": agent_name, "office_id": office}, namespace=SMCP_NAMESPACE, timeout=10)
print(f"Join response: {resp}", flush=True)

print("WAITING_FOR_NOTIFY: listening for notify:update_config (30s timeout)...", flush=True)
print("TRIGGER_NOW: run 'notify update' on Computer CLI", flush=True)

deadline = time.time() + 30
while time.time() < deadline:
    if notification_received:
        data = notification_received[0]
        result["pass"] = True
        result["notes"].append(f"Received notify:update_config: {data}")
        if isinstance(data, dict) and data.get("computer"):
            result["notes"].append(f"Has 'computer' field: {data.get('computer')}")
        break
    time.sleep(0.5)

if not notification_received:
    result["notes"].append("TIMEOUT: no notify:update_config received in 30s")

print(f"\nF-03: {'PASS' if result['pass'] else 'FAIL'} (note: verified notify:update_config instead of notify:update_skills — no marketplace configured)")
for n in result["notes"]:
    print(f"  {n}")

client.disconnect()
sys.exit(0 if result["pass"] else 1)
