#!/usr/bin/env bash
# Acceptance for seeds/mcp/binary_image_tool_server.py
# Axis: MC-BIN happy
# Expected: MCP server starts, lists big_image + small_image tools, returns deterministic PNG bytes.
set -Eeuo pipefail

SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
SEED_NAME="binary_image_tool_server"
TMPDIR="$(mktemp -d -t "a2c-seed-${SEED_NAME}.XXXXXX")"
LOG="$TMPDIR/run.log"

cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT INT TERM

fail() {
  echo "FAIL: $*" >&2
  echo "---- last 60 log lines ----" >&2
  tail -60 "$LOG" >&2 || true
  exit 1
}

# 1. Verify the server can start and respond to list_tools via stdio
#    We use python -c to drive a minimal MCP handshake
cd /Users/liulonggang/PycharmProjects/python-sdk

uv run python -c "
import asyncio, json, sys

async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(
        command=sys.executable,
        args=['${SEED_DIR}/${SEED_NAME}.py'],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f'TOOLS={names}', flush=True)

            # Call big_image and verify deterministic output
            import base64, hashlib
            result = await session.call_tool('big_image', {})
            assert len(result.content) == 1
            item = result.content[0]
            assert item.type == 'image'
            got = base64.b64decode(item.data)
            assert len(got) == 32768, f'expected 32768 bytes, got {len(got)}'

            # Verify deterministic bytes
            expected = bytes((i * 37 + 11) % 256 for i in range(32768))
            assert got == expected, 'deterministic bytes mismatch'

            sha = hashlib.sha256(got).hexdigest()
            assert sha == 'a06fa47c2671def27679fe048a287aeb2823c07a1e15d6395e02b3cec681c73d', f'sha256 mismatch: {sha}'
            print(f'BIG_IMAGE_OK len={len(got)} sha256={sha}', flush=True)

            # Call small_image
            result2 = await session.call_tool('small_image', {})
            item2 = result2.content[0]
            got2 = base64.b64decode(item2.data)
            assert len(got2) == 64, f'expected 64 bytes, got {len(got2)}'
            print(f'SMALL_IMAGE_OK len={len(got2)}', flush=True)

asyncio.run(main())
" > "$LOG" 2>&1

# 2. Verify output
grep -q "TOOLS=\['big_image', 'small_image'\]" "$LOG" || fail "tool list missing or wrong: $(grep TOOLS "$LOG" || true)"
grep -q "BIG_IMAGE_OK len=32768" "$LOG" || fail "big_image call failed"
grep -q "SMALL_IMAGE_OK len=64" "$LOG" || fail "small_image call failed"

echo "PASS: seed ${SEED_NAME}"
