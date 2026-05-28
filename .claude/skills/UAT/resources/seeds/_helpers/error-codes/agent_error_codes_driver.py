#!/usr/bin/env python3
"""
Error codes UAT agent driver — reusable test script.

Runs E-01 through E-16 error-code test cases against a running Server+Computer.
Verifies protocol error codes 4016 (SKILL_NAME_INVALID), 4014 (MCP_SERVER_NOT_FOUND /
SKILL not found), 4017 (SKILL_RESOURCE_NOT_ACCESSIBLE), 4018 (BLOB_NOT_ACCESSIBLE),
and computer-not-found routing errors (issue #92 regression guard).

Usage:
    uv run python agent_error_codes_driver.py \
        [--port-file /tmp/a2c-uat-port] \
        [--office-id err-uat-office] \
        [--computer-name err-comp-001] \
        [--skill-with-env env-skill]

Protocol: error-handling.md §4014 / §4016 / §4017 / §4018 + issue #92
"""
from __future__ import annotations

import argparse
import sys
import uuid

import socketio
from a2c_smcp.smcp import (
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    GET_TOOLS_EVENT,
    GET_DESKTOP_EVENT,
    GET_RESOURCES_EVENT,
    GET_SKILLS_EVENT,
    GET_SKILL_EVENT,
    GET_BLOB_EVENT,
    JOIN_OFFICE_EVENT,
    LIST_ROOM_EVENT,
)

results: dict[str, dict] = {}

# Issue #92 fix: computer-not-found error code. Until the fix lands, we accept
# either 404 (NOT_FOUND, expected new code) or 4014 (MCP_SERVER_NOT_FOUND, reused).
# After the fix merges, narrow this to the single actual code.
COMPUTER_NOT_FOUND_CODES = (404, 4014)
GHOST_COMPUTER = "ghost-computer-999"


def log(msg: str) -> None:
    print(msg, flush=True)


def req_id() -> str:
    return uuid.uuid4().hex[:12]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Error codes UAT agent driver")
    ap.add_argument("--port-file", default="/tmp/a2c-uat-port")
    ap.add_argument("--office-id", default="err-uat-office")
    ap.add_argument("--computer-name", default=None,
                    help="Computer name (auto-detected via list_room if omitted)")
    ap.add_argument("--skill-with-env", default="env-skill",
                    help="Name of a registered SKILL that has a .skillenv file (for E-06)")
    return ap.parse_args()


def _check_error(case_id: str, resp, expected_code: int, extra_checks=None) -> None:
    """Verify an error response matches the expected error code."""
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
            notes.append(f"PASS: message non-empty")
        else:
            ok = False
            notes.append(f"FAIL: message empty or missing")

        if extra_checks:
            for label, check_fn in extra_checks:
                try:
                    detail_ok, detail_note = check_fn(resp)
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
    agent_name = f"err-agent-{uuid.uuid4().hex[:6]}"

    log("=== ERROR CODES UAT AGENT ===")
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

    # ── Helper call builders ──
    def skill_call(name: str, rel_path: str | None = None) -> dict:
        req: dict = {"computer": computer_name, "name": name,
                     "agent": agent_name, "req_id": req_id()}
        if rel_path:
            req["rel_path"] = rel_path
        return client.call(GET_SKILL_EVENT, req, namespace=SMCP_NAMESPACE, timeout=10)

    def blob_call(blob_handle: str) -> dict:
        req: dict = {"computer": computer_name, "blob_handle": blob_handle,
                     "agent": agent_name, "req_id": req_id()}
        return client.call(GET_BLOB_EVENT, req, namespace=SMCP_NAMESPACE, timeout=10)

    # ═══════════════════════════════════════════════════════════
    # E-01: SKILL name path traversal → 4016
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== E-01: get_skill name='../etc/passwd' → 4016 ===")
    resp = skill_call("../etc/passwd")
    _check_error("E-01", resp, 4016, [
        ("details.name", lambda r: (
            r.get("details", {}).get("name") == "../etc/passwd",
            f"details.name={r.get('details', {}).get('name')!r}"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # E-02: SKILL name too many colons → 4016
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== E-02: get_skill name='foo:bar:baz:qux' → 4016 ===")
    resp = skill_call("foo:bar:baz:qux")
    _check_error("E-02", resp, 4016, [
        ("details.name", lambda r: (
            r.get("details", {}).get("name") == "foo:bar:baz:qux",
            f"details.name={r.get('details', {}).get('name')!r}"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # E-03: SKILL name legal but not found → 4014
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== E-03: get_skill name='nonexistent-skill' → 4014 ===")
    resp = skill_call("nonexistent-skill")
    _check_error("E-03", resp, 4014, [
        ("mcp_server_name", lambda r: (
            "mcp_server_name" in r or "details" in r,
            f"has mcp_server_name or details field"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # E-04: rel_path traversal → 4017
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== E-04: get_skill rel_path='../../etc/passwd' → 4017 ===")
    resp = skill_call(args.skill_with_env, "../../etc/passwd")
    _check_error("E-04", resp, 4017, [
        ("details.reason", lambda r: (
            r.get("details", {}).get("reason") == "traversal",
            f"details.reason={r.get('details', {}).get('reason')!r}"
        )),
        ("details.rel_path", lambda r: (
            r.get("details", {}).get("rel_path") == "../../etc/passwd",
            f"details.rel_path={r.get('details', {}).get('rel_path')!r}"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # E-05: rel_path absolute path → 4017
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== E-05: get_skill rel_path='/etc/shadow' → 4017 ===")
    resp = skill_call(args.skill_with_env, "/etc/shadow")
    _check_error("E-05", resp, 4017, [
        ("details.reason", lambda r: (
            r.get("details", {}).get("reason") in ("traversal",),
            f"details.reason={r.get('details', {}).get('reason')!r}"
        )),
        ("details.rel_path", lambda r: (
            r.get("details", {}).get("rel_path") == "/etc/shadow",
            f"details.rel_path={r.get('details', {}).get('rel_path')!r}"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # E-06: rel_path=.skillenv → 4017 forbidden
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== E-06: get_skill rel_path='.skillenv' → 4017 forbidden ===")
    resp = skill_call(args.skill_with_env, ".skillenv")
    _check_error("E-06", resp, 4017, [
        ("details.reason", lambda r: (
            r.get("details", {}).get("reason") == "forbidden",
            f"details.reason={r.get('details', {}).get('reason')!r}"
        )),
        ("details.rel_path", lambda r: (
            r.get("details", {}).get("rel_path") == ".skillenv",
            f"details.rel_path={r.get('details', {}).get('rel_path')!r}"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # E-07: rel_path nonexistent file → 4017 not_found
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== E-07: get_skill rel_path='nonexistent.md' → 4017 not_found ===")
    resp = skill_call(args.skill_with_env, "nonexistent.md")
    _check_error("E-07", resp, 4017, [
        ("details.reason", lambda r: (
            r.get("details", {}).get("reason") == "not_found",
            f"details.reason={r.get('details', {}).get('reason')!r}"
        )),
        ("details.rel_path", lambda r: (
            r.get("details", {}).get("rel_path") == "nonexistent.md",
            f"details.rel_path={r.get('details', {}).get('rel_path')!r}"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # E-08: Blob invalid handle → 4018
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== E-08: get_blob handle='a2c:invalid:totally-fake-handle' → 4018 ===")
    resp = blob_call("a2c:invalid:totally-fake-handle")
    _check_error("E-08", resp, 4018, [
        ("details.reason", lambda r: (
            r.get("details", {}).get("reason") in ("invalid_handle", "gone"),
            f"details.reason={r.get('details', {}).get('reason')!r}"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # E-09: Blob empty handle → 4018
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== E-09: get_blob handle='' → 4018 ===")
    resp = blob_call("")
    _check_error("E-09", resp, 4018, [
        ("details.reason", lambda r: (
            r.get("details", {}).get("reason") == "invalid_handle",
            f"details.reason={r.get('details', {}).get('reason')!r}"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # E-10: Blob cross-computer reuse → 4018 (or computer-not-found)
    # ═══════════════════════════════════════════════════════════
    # Note: "other-computer-not-exist" doesn't exist. After issue #92 fix,
    # Server may return computer-not-found error before blob validation.
    # We accept either error code.
    log(f"\n=== E-10: get_blob with wrong computer → 4018 or computer-not-found ===")
    fake_computer_req: dict = {
        "computer": "other-computer-not-exist",
        "blob_handle": "a2c:blob:some-handle",
        "agent": agent_name,
        "req_id": req_id(),
    }
    resp = client.call(GET_BLOB_EVENT, fake_computer_req, namespace=SMCP_NAMESPACE, timeout=10)
    # Accept either computer-not-found or blob-not-accessible
    if isinstance(resp, dict) and resp.get("code") in COMPUTER_NOT_FOUND_CODES:
        _check_error("E-10", resp, resp["code"], [
            ("computer-not-found", lambda r: (
                r.get("code") in COMPUTER_NOT_FOUND_CODES,
                f"code={r.get('code')} (computer-not-found, acceptable for non-existent computer)"
            )),
        ])
    else:
        _check_error("E-10", resp, 4018, [
            ("details.reason", lambda r: (
                r.get("details", {}).get("reason") in ("invalid_handle", "forbidden"),
                f"details.reason={r.get('details', {}).get('reason')!r}"
            )),
        ])

    # ═══════════════════════════════════════════════════════════
    # E-11~E-16: Issue #92 regression — computer-not-found for all client:* events
    # ═══════════════════════════════════════════════════════════
    # All client:* events routed via _relay_client_call must return flat ErrorPayload
    # (not raise ValueError / cause Agent timeout) when target computer doesn't exist.

    def _check_computer_not_found(case_id: str, resp) -> None:
        """Verify computer-not-found error response for issue #92 regression."""
        if isinstance(resp, dict) and resp.get("code") in COMPUTER_NOT_FOUND_CODES:
            notes_extra = [
                ("code", lambda r: (
                    r.get("code") in COMPUTER_NOT_FOUND_CODES,
                    f"code={r.get('code')} (computer-not-found)"
                )),
                ("message", lambda r: (
                    bool(r.get("message")),
                    f"message non-empty"
                )),
            ]
            _check_error(case_id, resp, resp["code"], notes_extra)
        else:
            # Not a computer-not-found error — check if it's any ErrorPayload at all
            ok = isinstance(resp, dict) and "code" in resp and "message" in resp
            results[case_id] = {
                "pass": ok,
                "notes": [
                    f"{'PASS' if ok else 'FAIL'}: response is ErrorPayload" if ok else
                    f"FAIL: expected computer-not-found ErrorPayload, got: {resp!r}",
                ],
            }
            log(f"{case_id}: {'PASS' if ok else 'FAIL'}")
            for n in results[case_id]["notes"]:
                log(f"  {n}")

    # E-11: get_skill with non-existent computer
    log(f"\n=== E-11: get_skill computer='{GHOST_COMPUTER}' → computer-not-found ===")
    ghost_skill_req = {
        "computer": GHOST_COMPUTER, "name": "any-skill",
        "agent": agent_name, "req_id": req_id(),
    }
    resp = client.call(GET_SKILL_EVENT, ghost_skill_req, namespace=SMCP_NAMESPACE, timeout=10)
    _check_computer_not_found("E-11", resp)

    # E-12: get_blob with non-existent computer
    log(f"\n=== E-12: get_blob computer='{GHOST_COMPUTER}' → computer-not-found ===")
    ghost_blob_req = {
        "computer": GHOST_COMPUTER, "blob_handle": "a2c:blob:some-handle",
        "agent": agent_name, "req_id": req_id(),
    }
    resp = client.call(GET_BLOB_EVENT, ghost_blob_req, namespace=SMCP_NAMESPACE, timeout=10)
    _check_computer_not_found("E-12", resp)

    # E-13: get_resources with non-existent computer
    log(f"\n=== E-13: get_resources computer='{GHOST_COMPUTER}' → computer-not-found ===")
    ghost_resources_req = {
        "computer": GHOST_COMPUTER, "mcp_server_name": "some-server",
        "agent": agent_name, "req_id": req_id(),
    }
    resp = client.call(GET_RESOURCES_EVENT, ghost_resources_req, namespace=SMCP_NAMESPACE, timeout=10)
    _check_computer_not_found("E-13", resp)

    # E-14: get_tools with non-existent computer
    log(f"\n=== E-14: get_tools computer='{GHOST_COMPUTER}' → computer-not-found ===")
    ghost_tools_req = {
        "computer": GHOST_COMPUTER,
        "agent": agent_name, "req_id": req_id(),
    }
    resp = client.call(GET_TOOLS_EVENT, ghost_tools_req, namespace=SMCP_NAMESPACE, timeout=10)
    _check_computer_not_found("E-14", resp)

    # E-15: get_skills with non-existent computer
    log(f"\n=== E-15: get_skills computer='{GHOST_COMPUTER}' → computer-not-found ===")
    ghost_skills_req = {
        "computer": GHOST_COMPUTER,
        "agent": agent_name, "req_id": req_id(),
    }
    resp = client.call(GET_SKILLS_EVENT, ghost_skills_req, namespace=SMCP_NAMESPACE, timeout=10)
    _check_computer_not_found("E-15", resp)

    # E-16: tool_call with non-existent computer
    log(f"\n=== E-16: tool_call computer='{GHOST_COMPUTER}' → computer-not-found ===")
    ghost_tool_call_req = {
        "computer": GHOST_COMPUTER, "tool_name": "some_tool", "arguments": {},
        "agent": agent_name, "req_id": req_id(),
    }
    resp = client.call(TOOL_CALL_EVENT, ghost_tool_call_req, namespace=SMCP_NAMESPACE, timeout=10)
    _check_computer_not_found("E-16", resp)

    # ── Summary ──
    log("\n" + "=" * 50)
    log("ERROR CODES UAT SUMMARY")
    passed = failed = 0
    for cid, r in results.items():
        if r["pass"]:
            passed += 1
            log(f"  {cid}: PASS")
        else:
            failed += 1
            log(f"  {cid}: FAIL")
            for n in r["notes"]:
                if "FAIL" in n:
                    log(f"    {n}")

    log(f"\nTotal: {passed + failed}  Passed: {passed}  Failed: {failed}")
    log("=== ERROR CODES UAT COMPLETE ===")

    client.disconnect()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(parse_args()))
