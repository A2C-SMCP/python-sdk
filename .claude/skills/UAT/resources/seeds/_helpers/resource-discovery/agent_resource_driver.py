#!/usr/bin/env python3
"""
Resource discovery UAT agent driver — reusable test script.

Runs R-01 through R-05 resource-discovery test cases against a running Server+Computer.
Verifies client:get_resources protocol: successful listing, annotations, error codes
4014 (MCP_SERVER_NOT_FOUND) and 4015 (MCP_CAPABILITY_NOT_SUPPORTED).

Usage:
    uv run python agent_resource_driver.py \
        [--port-file /tmp/a2c-uat-port] \
        [--office-id res-uat-office] \
        [--computer-name res-comp-001] \
        [--window-server window-resource-server] \
        [--no-resources-server no-resources-server]

Protocol: events.md §client:get_resources; error-handling.md §4014 / §4015
"""
from __future__ import annotations

import argparse
import sys
import uuid

import socketio
from a2c_smcp.smcp import (
    SMCP_NAMESPACE,
    GET_RESOURCES_EVENT,
    JOIN_OFFICE_EVENT,
    LIST_ROOM_EVENT,
)

results: dict[str, dict] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def req_id() -> str:
    return uuid.uuid4().hex[:12]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Resource discovery UAT agent driver")
    ap.add_argument("--port-file", default="/tmp/a2c-uat-port")
    ap.add_argument("--office-id", default="res-uat-office")
    ap.add_argument("--computer-name", default=None,
                    help="Computer name (auto-detected via list_room if omitted)")
    ap.add_argument("--window-server", default="window-resource-server",
                    help="Name of MCP server with resources capability")
    ap.add_argument("--no-resources-server", default="no-resources-server",
                    help="Name of MCP server without resources capability")
    return ap.parse_args()


def _check_success(case_id: str, resp, extra_checks=None) -> None:
    ok = True
    notes: list[str] = []

    if not isinstance(resp, dict):
        ok = False
        notes.append(f"FAIL: unexpected type {type(resp)}: {resp}")
    elif resp.get("code") and resp.get("code") != 0:
        ok = False
        notes.append(f"FAIL: error code={resp.get('code')} msg={resp.get('message')}")
    else:
        notes.append("PASS: no error code")

    if extra_checks:
        for label, check_fn in (extra_checks or []):
            try:
                detail_ok, detail_note = check_fn(resp if isinstance(resp, dict) else {})
                if detail_ok:
                    notes.append(f"PASS: {label} — {detail_note}")
                else:
                    ok = False
                    notes.append(f"FAIL: {label} — {detail_note}")
            except Exception as e:
                ok = False
                notes.append(f"FAIL: {label} — exception: {e}")

    results[case_id] = {"pass": ok, "notes": notes}
    log(f"{case_id}: {'PASS' if ok else 'FAIL'}")
    for n in notes:
        log(f"  {n}")


def _check_error(case_id: str, resp, expected_code: int, extra_checks=None) -> None:
    ok = True
    notes: list[str] = []

    if not isinstance(resp, dict):
        ok = False
        notes.append(f"FAIL: unexpected type {type(resp)}: {resp}")
    else:
        code = resp.get("code")
        if code != expected_code:
            ok = False
            notes.append(f"FAIL: code={code}, expected={expected_code}")
        else:
            notes.append(f"PASS: code={code}")

        msg = resp.get("message", "")
        if msg:
            notes.append("PASS: message non-empty")
        else:
            ok = False
            notes.append("FAIL: message empty")

    if extra_checks:
        for label, check_fn in (extra_checks or []):
            try:
                detail_ok, detail_note = check_fn(resp if isinstance(resp, dict) else {})
                if detail_ok:
                    notes.append(f"PASS: {label} — {detail_note}")
                else:
                    ok = False
                    notes.append(f"FAIL: {label} — {detail_note}")
            except Exception as e:
                ok = False
                notes.append(f"FAIL: {label} — exception: {e}")

    results[case_id] = {"pass": ok, "notes": notes}
    log(f"{case_id}: {'PASS' if ok else 'FAIL'}")
    for n in notes:
        log(f"  {n}")


def run(args: argparse.Namespace) -> int:
    port = open(args.port_file).read().strip()
    url = f"http://127.0.0.1:{port}"
    agent_name = f"res-agent-{uuid.uuid4().hex[:6]}"

    log("=== RESOURCE DISCOVERY UAT AGENT ===")
    log(f"URL: {url}  Office: {args.office_id}  Agent: {agent_name}")

    client = socketio.Client()

    @client.on("connect", namespace=SMCP_NAMESPACE)
    def on_connect():
        log("AGENT_CONNECTED")

    client.connect(
        url,
        socketio_path="/socket.io",
        namespaces=[SMCP_NAMESPACE],
        transports=["polling"],
        wait=True,
        wait_timeout=10,
    )
    log("Connected to server")

    # ── Join office ──
    join_resp = client.call(
        JOIN_OFFICE_EVENT,
        {"role": "agent", "name": agent_name, "office_id": args.office_id},
        namespace=SMCP_NAMESPACE,
        timeout=10,
    )
    if not (isinstance(join_resp, tuple) and join_resp[0]):
        log(f"FAIL: join_office rejected: {join_resp}")
        client.disconnect()
        return 1

    # ── Discover computer ──
    computer_name = args.computer_name
    if not computer_name:
        room = client.call(
            LIST_ROOM_EVENT,
            {"agent": agent_name, "req_id": req_id(), "office_id": args.office_id},
            namespace=SMCP_NAMESPACE,
            timeout=10,
        )
        if not isinstance(room, dict):
            log(f"FAIL: list_room unexpected: {room}")
            client.disconnect()
            return 1
        for s in room.get("sessions", []):
            if s.get("role") == "computer":
                computer_name = s.get("name")
                break
        if not computer_name:
            log(f"FAIL: no computer in office {args.office_id}")
            client.disconnect()
            return 1

    log(f"Computer: {computer_name}")

    def get_resources(mcp_server: str, computer: str | None = None) -> dict:
        req: dict = {
            "computer": computer or computer_name,
            "mcp_server": mcp_server,
            "agent": agent_name,
            "req_id": req_id(),
        }
        return client.call(GET_RESOURCES_EVENT, req, namespace=SMCP_NAMESPACE, timeout=10)

    # ═══════════════════════════════════════════════════════════
    # R-01: get_resources success — 3 resources
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== R-01: get_resources from {args.window_server} ===")
    resp = get_resources(args.window_server)
    _check_success("R-01", resp, [
        ("resources count", lambda r: (
            len(r.get("resources", [])) == 3,
            f"resources count={len(r.get('resources', []))}"
        )),
        ("window://main-editor", lambda r: (
            any(res.get("uri") == "window://main-editor" for res in r.get("resources", [])),
            "found window://main-editor"
        )),
        ("window://terminal", lambda r: (
            any(res.get("uri") == "window://terminal" for res in r.get("resources", [])),
            "found window://terminal"
        )),
        ("config://app-settings", lambda r: (
            any(res.get("uri") == "config://app-settings" for res in r.get("resources", [])),
            "found config://app-settings"
        )),
        ("snake_case mime_type", lambda r: (
            all("mime_type" in res for res in r.get("resources", [])),
            "all resources have mime_type (snake_case)"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # R-02: window:// resources have annotations
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== R-02: annotations on window:// resources ===")
    if isinstance(resp, dict) and not resp.get("code"):
        resources = resp.get("resources", [])
        main_editor = next((r for r in resources if r.get("uri") == "window://main-editor"), None)
        terminal = next((r for r in resources if r.get("uri") == "window://terminal"), None)

        checks = []
        if main_editor:
            checks.extend([
                ("main-editor annotations.priority", lambda r: (
                    main_editor.get("annotations", {}).get("priority") is not None,
                    f"priority={main_editor.get('annotations', {}).get('priority')}"
                )),
                ("main-editor _meta.fullscreen", lambda r: (
                    "fullscreen" in main_editor.get("_meta", {}),
                    f"fullscreen={main_editor.get('_meta', {}).get('fullscreen')}"
                )),
            ])
        if terminal:
            checks.extend([
                ("terminal annotations.priority", lambda r: (
                    terminal.get("annotations", {}).get("priority") is not None,
                    f"priority={terminal.get('annotations', {}).get('priority')}"
                )),
            ])
        _check_success("R-02", resp, checks if checks else None)
    else:
        results["R-02"] = {"pass": None, "notes": ["SKIP: R-01 did not return resources"]}
        log("R-02: SKIPPED (R-01 failed)")

    # ═══════════════════════════════════════════════════════════
    # R-03: nonexistent MCP server → 4014
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== R-03: get_resources nonexistent MCP server → 4014 ===")
    resp = get_resources("nonexistent-server")
    _check_error("R-03", resp, 4014, [
        ("mcp_server_name", lambda r: (
            r.get("mcp_server_name") == "nonexistent-server",
            f"mcp_server_name={r.get('mcp_server_name')!r}"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # R-04: no-resources server → 4015
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== R-04: get_resources no-resources server → 4015 ===")
    resp = get_resources(args.no_resources_server)
    _check_error("R-04", resp, 4015, [
        ("mcp_server_name", lambda r: (
            r.get("mcp_server_name") == args.no_resources_server,
            f"mcp_server_name={r.get('mcp_server_name')!r}"
        )),
        ("capability", lambda r: (
            r.get("capability") == "resources",
            f"capability={r.get('capability')!r}"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # R-05: ghost computer → routing failure
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== R-05: get_resources ghost computer → error ===")
    resp = get_resources(args.window_server, computer="ghost-computer")
    # Expect error (may be 4014 or timeout)
    ok = isinstance(resp, dict) and resp.get("code") is not None
    if ok:
        results["R-05"] = {"pass": True, "notes": [
            f"PASS: error returned code={resp.get('code')}",
        ]}
    else:
        results["R-05"] = {"pass": False, "notes": [
            f"FAIL: expected error, got: {resp}",
        ]}
    log(f"R-05: {'PASS' if ok else 'FAIL'}")

    # ── Summary ──
    log("\n" + "=" * 50)
    log("RESOURCE DISCOVERY UAT SUMMARY")
    passed = failed = skipped = 0
    for cid, r in results.items():
        if r["pass"] is None:
            skipped += 1
            log(f"  {cid}: SKIPPED")
        elif r["pass"]:
            passed += 1
            log(f"  {cid}: PASS")
        else:
            failed += 1
            log(f"  {cid}: FAIL")
            for n in r["notes"]:
                if "FAIL" in n:
                    log(f"    {n}")

    log(f"\nTotal: {passed + failed + skipped}  "
        f"Passed: {passed}  Failed: {failed}  Skipped: {skipped}")
    log("=== RESOURCE DISCOVERY UAT COMPLETE ===")

    client.disconnect()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(parse_args()))
