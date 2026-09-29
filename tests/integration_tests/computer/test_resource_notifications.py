"""#227: real stdio notifications must not block responses or Computer shutdown."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig
from a2c_smcp.computer.socketio.client import SMCPComputerClient


class _ObservedClient(SMCPComputerClient):
    """Observe outgoing refreshes without replacing MCP transport or resource handling."""

    def __init__(self, computer: Computer) -> None:
        super().__init__(computer=computer)
        self.skills_updated = asyncio.Event()
        self.desktop_updated = asyncio.Event()

    async def emit_update_skills(self) -> None:
        await super().emit_update_skills()
        self.skills_updated.set()

    async def emit_refresh_desktop(self) -> None:
        await super().emit_refresh_desktop()
        self.desktop_updated.set()


async def _scenario(root: Path, mode: str) -> None:
    server = Path(__file__).parent / "mcp_servers" / "mutable_tools_stdio_server.py"
    config = StdioServerConfig(
        name="mutable-srv",
        default_tool_meta={"auto_apply": True},
        server_parameters=StdioServerParameters(
            command=sys.executable,
            args=[str(server), "--resource-notifications", mode, "--skill-dir", str(root / "source")],
        ),
    )
    computer = Computer(
        name="notification-regression",
        mcp_servers={config},
        skill_home=root / "skills",
        blob_cache_root=root / "blobs",
        project_root=root,
        env={"XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data")},
    )
    async with computer:
        await computer.aget_available_tools()
        client = _ObservedClient(computer) if mode != "unbound" else None
        result = await computer.aexecute_tool("change", "mutable-srv__set_phase", {"phase": 1}, timeout=2)
        # Print before teardown so deadlocked cleanup cannot hide the triggering failure.
        print(f"tool_is_error={result.isError}", flush=True)
        assert not result.isError
        async with asyncio.timeout(5):
            # develop serves the committed tool projection: await the real notification-triggered
            # refresh, rather than assuming its RPC completes before the original tool response.
            refresh = computer._tool_refresh_task
            assert refresh is not None
            await refresh
            names = {tool["name"] for tool in await computer.aget_available_tools()}
            assert "mutable-srv__dynamic_tool" in names
            result = await computer.aexecute_tool("dynamic", "mutable-srv__dynamic_tool", {}, timeout=2)
            assert not result.isError
            assert result.content[0].text == "dynamic phase=1"
            if client is not None:
                await client.skills_updated.wait()
                ref = computer.get_skill_ref("mcp:mutable-srv:demo")
                assert ref is not None
                assert "phase 1" in ref["description"]
                if mode == "list":
                    await client.desktop_updated.wait()
                    assert computer._windows_cache == {"window://dynamic"}
        print("refresh_and_dynamic_call_ok", flush=True)
    print("shutdown_ok", flush=True)


@pytest.mark.parametrize("mode", ["unbound", "list", "content"])
def test_notifications_allow_tool_response_refresh_and_shutdown(tmp_path: Path, mode: str) -> None:
    """Process isolation bounds even a deadlocked MCP receive loop during cleanup."""
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.integration_tests.computer.test_resource_notifications", str(tmp_path), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        pytest.fail(f"MCP scenario exceeded 15s (including shutdown):\n{stdout}\n{stderr[-4000:]}")
    assert process.returncode == 0, f"{stdout}\n{stderr[-4000:]}"
    assert "refresh_and_dynamic_call_ok" in stdout
    assert "shutdown_ok" in stdout


if __name__ == "__main__":
    asyncio.run(_scenario(Path(sys.argv[1]), sys.argv[2]))
