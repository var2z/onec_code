"""Call MCP tools over streamable HTTP, like an agent would.

Usage: python tests/mcp_probe.py http://localhost:8002/mcp tool '{"arg": "value"}' [tool '{...}' ...]
       python tests/mcp_probe.py http://localhost:8002/mcp --list
"""
import asyncio
import json
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(url, args):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if args == ['--list']:
                tools = (await s.list_tools()).tools
                print(f'{len(tools)} tools:', ', '.join(t.name for t in tools))
                return
            for name, a in zip(args[::2], args[1::2]):
                t = time.time()
                res = await s.call_tool(name, json.loads(a))
                text = ''.join(c.text for c in res.content if hasattr(c, 'text'))
                print(f'===== {name} {a}  ({time.time() - t:.2f}s, {len(text)} chars)')
                print(text)


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
