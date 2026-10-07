"""stdio <-> streamable HTTP bridge for MCP clients that only start local (stdio) servers,
e.g. the Claude Desktop app (claude_desktop_config.json).

Run inside the container, so the host needs neither Python nor Node:
  docker exec -i onec-code python -m onec_code.stdio_proxy
It forwards tools/list and tools/call to http://127.0.0.1:8002/mcp (env ONEC_MCP_URL).
"""
from __future__ import annotations

import os

import anyio
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def main(url: str):
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as remote:
            init = await remote.initialize()
            server = Server('onec-code', instructions=init.instructions)

            @server.list_tools()
            async def list_tools() -> list[types.Tool]:
                return (await remote.list_tools()).tools

            @server.call_tool(validate_input=False)   # the remote server validates
            async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
                return await remote.call_tool(name, arguments)

            async with stdio_server() as (sr, sw):
                await server.run(sr, sw, server.create_initialization_options())


if __name__ == '__main__':
    anyio.run(main, os.environ.get('ONEC_MCP_URL', 'http://127.0.0.1:8002/mcp'))
