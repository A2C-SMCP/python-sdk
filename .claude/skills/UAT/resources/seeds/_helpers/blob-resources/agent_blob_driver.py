#!/usr/bin/env python3
"""
Blob transfer UAT agent driver — reusable test script.

Runs B-01 through B-04 blob transfer test cases against a running Server+Computer.
Designed to be invoked from the blob-transfer UAT scenario.

Usage:
    uv run python agent_blob_driver.py [--port-file /tmp/a2c-uat-port] \
        [--office-id blob-uat-office] [--computer-name blob-comp-001] \
        [--skip-b04]

Protocol: blob-transfer.md §2-§5; events.md §client:get_skill / §client:get_blob
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import uuid

import socketio
from a2c_smcp.smcp import (
    SMCP_NAMESPACE,
    GET_SKILL_EVENT,
    GET_BLOB_EVENT,
    JOIN_OFFICE_EVENT,
    LIST_ROOM_EVENT,
    TOOL_CALL_EVENT,
)

EXPECTED_SMALL_SHA = "d82c6aa133a0fc25b087f46ad7ed2a3042772e612e015571e61753ff55ba6da8"
EXPECTED_LARGE_SHA = "fee47b1f0d7685a226fd5f2b9dd8f525038bbb05fe9d89a5d75c249edac868e3"

# B-04 deterministic bytes: det_bytes(32768) base64-encoded PNG
def _det_bytes(n: int) -> bytes:
    return bytes((i * 37 + 11) % 256 for i in range(n))

EXPECTED_BIG_IMAGE_SHA = "a06fa47c2671def27679fe048a287aeb2823c07a1e15d6395e02b3cec681c73d"
EXPECTED_BIG_IMAGE_SIZE = 32768

results: dict[str, dict] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def req_id() -> str:
    return uuid.uuid4().hex[:12]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Blob transfer UAT agent driver")
    ap.add_argument("--port-file", default="/tmp/a2c-uat-port")
    ap.add_argument("--office-id", default="blob-uat-office")
    ap.add_argument("--computer-name", default=None,
                    help="Computer name (auto-detected from list_room if omitted)")
    ap.add_argument("--skip-b04", action="store_true",
                    help="Skip B-04 tool_call binary test")
    return ap.parse_args()


def run(args: argparse.Namespace) -> int:
    port = open(args.port_file).read().strip()
    url = f"http://127.0.0.1:{port}"
    agent_name = f"blob-agent-{uuid.uuid4().hex[:6]}"

    log("=== BLOB TRANSFER UAT AGENT ===")
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

    def skill_call(name: str, rel_path: str | None = None) -> dict:
        req: dict = {"computer": computer_name, "name": name,
                     "agent": agent_name, "req_id": req_id()}
        if rel_path:
            req["rel_path"] = rel_path
        return client.call(GET_SKILL_EVENT, req, namespace=SMCP_NAMESPACE, timeout=10)

    def blob_call(handle: str, offset: int | None = None) -> dict:
        req: dict = {"computer": computer_name, "blob_handle": handle,
                     "agent": agent_name, "req_id": req_id()}
        if offset is not None:
            req["chunk_offset"] = offset
        return client.call(GET_BLOB_EVENT, req, namespace=SMCP_NAMESPACE, timeout=15)

    def tool_call(tool_name: str, params: dict | None = None, timeout: int = 30) -> dict:
        req: dict = {"computer": computer_name, "tool_name": tool_name,
                     "agent": agent_name, "req_id": req_id(),
                     "params": params or {}, "timeout": timeout}
        return client.call(TOOL_CALL_EVENT, req, namespace=SMCP_NAMESPACE, timeout=timeout)

    # ═══════════════════════════════════════
    # B-01: SKILL.md inline
    # ═══════════════════════════════════════
    _run_inline_test("B-01", "blob-test", None, None, skill_call)

    # ═══════════════════════════════════════
    # B-01b: small.txt inline (100 bytes)
    # ═══════════════════════════════════════
    _run_inline_test("B-01b", "blob-test", "small.txt", EXPECTED_SMALL_SHA, skill_call)

    # ═══════════════════════════════════════
    # B-02: large.txt blob handle (65536 bytes)
    # ═══════════════════════════════════════
    _run_blob_handle_test("B-02", "blob-test", "large.txt",
                          EXPECTED_LARGE_SHA, 65536, skill_call, blob_call)

    # ═══════════════════════════════════════
    # B-04: tool_call binary sideband blob
    # ═══════════════════════════════════════
    if args.skip_b04:
        results["B-04"] = {"pass": None, "notes": ["SKIP: --skip-b04"],
                           "skipped": True}
        log("B-04: SKIPPED (--skip-b04)")
    else:
        _run_tool_call_blob_test("B-04", "big_image", EXPECTED_BIG_IMAGE_SHA,
                                 EXPECTED_BIG_IMAGE_SIZE, tool_call, blob_call)

    # ── Summary ──
    log("\n" + "=" * 50)
    log("BLOB TRANSFER UAT SUMMARY")
    passed = failed = skipped = 0
    for cid, r in results.items():
        if r.get("skipped"):
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
    log("=== BLOB TRANSFER UAT COMPLETE ===")

    client.disconnect()
    return 1 if failed else 0


def _run_inline_test(
    case_id: str,
    skill_name: str,
    rel_path: str | None,
    expected_sha: str | None,
    skill_call,
) -> None:
    log(f"\n=== {case_id}: get_skill {skill_name} {rel_path or 'SKILL.md'} ===")
    resp = skill_call(skill_name, rel_path)

    ok = True
    notes: list[str] = []

    if not isinstance(resp, dict):
        ok = False
        notes.append(f"FAIL: unexpected type {type(resp)}")
    elif resp.get("code") and resp.get("code") != 0:
        ok = False
        notes.append(f"FAIL: error code={resp.get('code')} msg={resp.get('message')}")
    else:
        body = resp.get("body", "")
        bh = resp.get("blob_handle", "")
        ts = resp.get("total_size", 0)
        rs = resp.get("sha256", "")

        if body and not bh:
            notes.append("PASS: inline (body, no blob_handle)")
        else:
            ok = False
            notes.append(f"FAIL: body={bool(body)} blob_handle={bool(bh)}")

        if ts <= 32768:
            notes.append(f"PASS: total_size={ts} <= 32768")
        else:
            ok = False
            notes.append(f"FAIL: total_size={ts} > 32768")

        if expected_sha:
            if rs == expected_sha:
                notes.append("PASS: sha256 matches expected")
            else:
                ok = False
                notes.append(f"FAIL: sha256 got={rs} expected={expected_sha}")

        if body:
            local = sha256_bytes(body.encode("utf-8"))
            if local == rs:
                notes.append("PASS: local sha256 matches remote")
            else:
                ok = False
                notes.append("FAIL: local sha256 mismatch")

    results[case_id] = {"pass": ok, "notes": notes}
    log(f"{case_id}: {'PASS' if ok else 'FAIL'}")
    for n in notes:
        log(f"  {n}")


def _run_blob_handle_test(
    case_id: str,
    skill_name: str,
    rel_path: str,
    expected_sha: str,
    expected_size: int,
    skill_call,
    blob_call,
) -> None:
    log(f"\n=== {case_id}: get_skill {skill_name} {rel_path} (blob handle) ===")
    resp = skill_call(skill_name, rel_path)

    ok = True
    notes: list[str] = []
    handle = None

    if not isinstance(resp, dict):
        ok = False
        notes.append(f"FAIL: unexpected type {type(resp)}")
    elif resp.get("code") and resp.get("code") != 0:
        ok = False
        notes.append(f"FAIL: error code={resp.get('code')}")
    else:
        body = resp.get("body", "")
        bh = resp.get("blob_handle", "")
        ts = resp.get("total_size", 0)
        rs = resp.get("sha256", "")

        if bh and not body:
            handle = bh
            notes.append("PASS: blob_handle returned (not inline)")
        else:
            ok = False
            notes.append(f"FAIL: body={bool(body)} blob_handle={bool(bh)}")

        if ts == expected_size:
            notes.append(f"PASS: total_size={ts} == {expected_size}")
        else:
            ok = False
            notes.append(f"FAIL: total_size={ts} != {expected_size}")

        if rs == expected_sha:
            notes.append("PASS: sha256 matches expected")
        else:
            ok = False
            notes.append(f"FAIL: sha256 mismatch")

    # get_blob round-trip
    if handle:
        log(f"  Fetching blob via get_blob...")
        br = blob_call(handle)
        if not isinstance(br, dict) or br.get("code") and br.get("code") != 0:
            ok = False
            notes.append(f"FAIL: get_blob error: {br}")
        else:
            blob_bytes = base64.b64decode(br.get("blob", ""))
            local_sha = sha256_bytes(blob_bytes)
            remote_sha = br.get("sha256", "")
            eof = br.get("eof", False)
            total = br.get("total_size", 0)
            offset = br.get("chunk_offset", 0)

            if local_sha == remote_sha:
                notes.append("PASS: blob local sha256 == remote sha256")
            else:
                ok = False
                notes.append("FAIL: blob sha256 mismatch")

            if remote_sha == expected_sha:
                notes.append("PASS: blob sha256 matches expected")
            else:
                ok = False
                notes.append("FAIL: blob sha256 mismatch with expected")

            if total == expected_size:
                notes.append(f"PASS: blob total_size={total}")
            else:
                ok = False
                notes.append(f"FAIL: blob total_size={total} != {expected_size}")

            if not eof:
                # multi-chunk — reassemble
                all_bytes = blob_bytes
                next_off = offset + len(blob_bytes)
                while not eof and next_off < total:
                    cr = blob_call(handle, next_off)
                    if isinstance(cr, dict) and "blob" in cr:
                        cb = base64.b64decode(cr["blob"])
                        all_bytes += cb
                        eof = cr.get("eof", False)
                        next_off += len(cb)
                    else:
                        ok = False
                        notes.append(f"FAIL: chunk error at offset {next_off}")
                        break
                full_sha = sha256_bytes(all_bytes)
                if full_sha == expected_sha and len(all_bytes) == total:
                    notes.append("PASS: reassembled blob matches expected")
                else:
                    ok = False
                    notes.append("FAIL: reassembled blob mismatch")

    results[case_id] = {"pass": ok, "notes": notes}
    log(f"{case_id}: {'PASS' if ok else 'FAIL'}")
    for n in notes:
        log(f"  {n}")


def _run_tool_call_blob_test(
    case_id: str,
    tool_name: str,
    expected_sha: str,
    expected_size: int,
    tool_call,
    blob_call,
) -> None:
    log(f"\n=== {case_id}: call_tool {tool_name} (binary sideband) ===")
    resp = tool_call(tool_name)

    ok = True
    notes: list[str] = []

    if not isinstance(resp, dict):
        ok = False
        notes.append(f"FAIL: unexpected type {type(resp)}")
    elif resp.get("isError"):
        content = resp.get("content", [])
        err_text = ""
        for c in (content or []):
            if isinstance(c, dict) and c.get("text"):
                err_text = c["text"]
        ok = False
        notes.append(f"FAIL: tool call error: {err_text or resp.get('structuredContent')}")
    else:
        meta = resp.get("meta") or {}
        content = resp.get("content", [])
        blob_handle = meta.get("a2c_blob_handle")

        if blob_handle:
            notes.append("PASS: blob_handle found in _meta (sideband blob)")
            # Fetch blob
            br = blob_call(blob_handle)
            if not isinstance(br, dict) or (br.get("code") and br.get("code") != 0):
                ok = False
                notes.append(f"FAIL: get_blob error: {br}")
            else:
                blob_bytes = base64.b64decode(br.get("blob", ""))
                local_sha = sha256_bytes(blob_bytes)
                remote_sha = br.get("sha256", "")
                total_size = br.get("total_size", 0)

                if local_sha == remote_sha:
                    notes.append("PASS: blob local sha256 == remote sha256")
                else:
                    ok = False
                    notes.append("FAIL: blob sha256 mismatch")

                if local_sha == expected_sha:
                    notes.append("PASS: blob sha256 matches expected")
                else:
                    ok = False
                    notes.append(f"FAIL: blob sha256 mismatch with expected")

                if total_size == expected_size:
                    notes.append(f"PASS: blob total_size={total_size}")
                else:
                    ok = False
                    notes.append(f"FAIL: blob total_size={total_size} != {expected_size}")

                if len(blob_bytes) == expected_size:
                    notes.append("PASS: no data truncation")
                else:
                    ok = False
                    notes.append(f"FAIL: got {len(blob_bytes)} bytes, expected {expected_size}")
        else:
            # Fallback: check inline content
            for item in (content or []):
                if isinstance(item, dict) and item.get("type") == "image":
                    decoded = base64.b64decode(item.get("data", ""))
                    local_sha = sha256_bytes(decoded)
                    if local_sha == expected_sha and len(decoded) == expected_size:
                        notes.append(f"PASS: inline (within budget), sha256 ok, size={len(decoded)}")
                    else:
                        ok = False
                        notes.append(f"FAIL: inline sha256 or size mismatch")
                    break
            else:
                ok = False
                notes.append("FAIL: no blob_handle and no image content")

    results[case_id] = {"pass": ok, "notes": notes}
    log(f"{case_id}: {'PASS' if ok else 'FAIL'}")
    for n in notes:
        log(f"  {n}")


if __name__ == "__main__":
    sys.exit(run(parse_args()))
