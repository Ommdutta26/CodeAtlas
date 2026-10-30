"""Start the CodeAtlas MCP server over stdio and call it like Claude Desktop does.

    python examples/stdio_smoke_test.py /path/to/repo [rev_range]
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(repo: str, rev_range: str | None):
    env = {**os.environ, "CODEATLAS_ROOTS": os.path.dirname(os.path.abspath(repo))}
    params = StdioServerParameters(command=sys.executable, args=["-m", "codeatlas"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", ", ".join(sorted(t.name for t in tools.tools)))
            args = {"repo_path": os.path.abspath(repo)}
            if rev_range:
                args["rev_range"] = rev_range
            result = await session.call_tool("review_and_test", args)
            data = json.loads(result.content[0].text)
            print("verdict:", data["verdict"])
            print("summary:", data["summary"])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
