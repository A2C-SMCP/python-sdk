#!/usr/bin/env python3
"""
Skill discovery UAT agent driver — reusable test script.

Runs D-05 progressive disclosure test case against a running Server+Computer.
Covers the three-level progressive disclosure: get_skills → get_skill → get_blob.

Note: D-01~D-04 are CLI-only tests and don't need an Agent driver.

Usage:
    uv run python agent_skill_driver.py \
        [--port-file /tmp/a2c-uat-port] \
        [--office-id skill-uat-office] \
        [--computer-name skill-comp-001] \
        [--skill-name foo:valid-skill-pkg]

Protocol: events.md §client:get_skill[s]; skill.md §progressive-disclosure
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
    GET_SKILLS_EVENT,
    GET_SKILL_EVENT,
    GET_BLOB_EVENT,
    JOIN_OFFICE_EVENT,
    LIST_ROOM_EVENT,
)

results: dict[str, dict] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def req_id() -> str:
    return uuid.uuid4().hex[:12]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Skill discovery UAT agent driver")
    ap.add_argument("--port-file", default="/tmp/a2c-uat-port")
    ap.add_argument("--office-id", default="skill-uat-office")
    ap.add_argument("--computer-name", default=None,
                    help="Computer name (auto-detected via list_room if omitted)")
    ap.add_argument("--skill-name", default=None,
                    help="SKILL name to test (auto-detected from get_skills if omitted)")
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


def run(args: argparse.Namespace) -> int:
    port = open(args.port_file).read().strip()
    url = f"http://127.0.0.1:{port}"
    agent_name = f"skill-agent-{uuid.uuid4().hex[:6]}"

    log("=== SKILL DISCOVERY UAT AGENT ===")
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

    def skills_call() -> dict:
        return client.call(
            GET_SKILLS_EVENT,
            {"computer": computer_name, "agent": agent_name, "req_id": req_id()},
            namespace=SMCP_NAMESPACE,
            timeout=10,
        )

    def skill_call(name: str, rel_path: str | None = None) -> dict:
        req: dict = {"computer": computer_name, "name": name,
                     "agent": agent_name, "req_id": req_id()}
        if rel_path:
            req["rel_path"] = rel_path
        return client.call(GET_SKILL_EVENT, req, namespace=SMCP_NAMESPACE, timeout=10)

    def blob_call(handle: str) -> dict:
        req: dict = {"computer": computer_name, "blob_handle": handle,
                     "agent": agent_name, "req_id": req_id()}
        return client.call(GET_BLOB_EVENT, req, namespace=SMCP_NAMESPACE, timeout=15)

    # ═══════════════════════════════════════════════════════════
    # D-05-1: get_skills — discover SKILL list
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== D-05-1: get_skills ===")
    resp = skills_call()
    _check_success("D-05-1", resp, [
        ("skills non-empty", lambda r: (
            len(r.get("skills", [])) > 0,
            f"skills count={len(r.get('skills', []))}"
        )),
    ])

    # ── Auto-detect skill name ──
    skill_name = args.skill_name
    if not skill_name and isinstance(resp, dict) and not resp.get("code"):
        skills = resp.get("skills", [])
        if skills:
            skill_name = skills[0].get("name")
            log(f"  Auto-detected skill: {skill_name}")

    if not skill_name:
        log("FAIL: no skill found to test progressive disclosure")
        client.disconnect()
        return 1

    # ═══════════════════════════════════════════════════════════
    # D-05-2: get_skill — fetch entry SKILL.md (inline)
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== D-05-2: get_skill {skill_name} (SKILL.md) ===")
    resp = skill_call(skill_name)
    _check_success("D-05-2", resp, [
        ("has body", lambda r: (
            bool(r.get("body")),
            f"body length={len(r.get('body', ''))}"
        )),
        ("mime_type markdown", lambda r: (
            r.get("mime_type") == "text/markdown",
            f"mime_type={r.get('mime_type')!r}"
        )),
        ("frontmatter stripped", lambda r: (
            "name:" not in r.get("body", ""),
            "frontmatter stripped from body"
        )),
    ])

    # ═══════════════════════════════════════════════════════════
    # D-05-3: get_skill with rel_path — sub-resource
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== D-05-3: get_skill {skill_name} references/usage.md ===")
    resp = skill_call(skill_name, "references/usage.md")
    if isinstance(resp, dict) and not resp.get("code"):
        if resp.get("body"):
            # Text sub-resource, inline
            _check_success("D-05-3", resp, [
                ("body inline", lambda r: (
                    bool(r.get("body")) and not r.get("blob_handle"),
                    "text sub-resource returned inline"
                )),
            ])
        elif resp.get("blob_handle"):
            # Over budget, blob handle
            log("  Sub-resource over budget, got blob_handle — testing get_blob...")
            blob_resp = blob_call(resp["blob_handle"])
            _check_success("D-05-3", blob_resp, [
                ("blob data", lambda r: (
                    bool(r.get("blob")),
                    f"blob data present"
                )),
                ("sha256 match", lambda r: (
                    r.get("sha256") == resp.get("sha256"),
                    f"sha256 consistent"
                )),
            ])
        else:
            results["D-05-3"] = {"pass": False, "notes": [
                "FAIL: no body or blob_handle in response"
            ]}
            log("D-05-3: FAIL")
    else:
        # Sub-resource might not exist
        results["D-05-3"] = {"pass": None, "notes": [
            f"SKIP: references/usage.md not available: {resp}"
        ]}
        log("D-05-3: SKIPPED (sub-resource not available)")

    # ═══════════════════════════════════════════════════════════
    # D-05-4: A2CSkillRef required fields contract
    # ═══════════════════════════════════════════════════════════
    log(f"\n=== D-05-4: A2CSkillRef required fields ===")
    skills_resp = skills_call()
    if isinstance(skills_resp, dict) and not skills_resp.get("code"):
        skills = skills_resp.get("skills", [])
        required_fields = ("name", "source", "path", "description")
        all_ok = True
        field_notes: list[str] = []
        for skill_ref in skills:
            for f in required_fields:
                if f not in skill_ref:
                    all_ok = False
                    field_notes.append(f"FAIL: {skill_ref.get('name', '?')} missing field '{f}'")
                else:
                    field_notes.append(f"PASS: {skill_ref.get('name', '?')}.{f} present")

        results["D-05-4"] = {"pass": all_ok, "notes": field_notes}
        log(f"D-05-4: {'PASS' if all_ok else 'FAIL'}")
        for n in field_notes:
            log(f"  {n}")
    else:
        results["D-05-4"] = {"pass": None, "notes": ["SKIP: get_skills failed"]}
        log("D-05-4: SKIPPED")

    # ── Summary ──
    log("\n" + "=" * 50)
    log("SKILL DISCOVERY UAT SUMMARY")
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
    log("=== SKILL DISCOVERY UAT COMPLETE ===")

    client.disconnect()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(parse_args()))
