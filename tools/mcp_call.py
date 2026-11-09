"""Call pads-mcp tools over the real MCP stdio protocol.

usage: mcp_call.py '<json list of [tool, args]>' [png_out_dir]
Images returned by tools are written to png_out_dir.
"""
import asyncio, base64, json, os, sys, time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")


async def main(calls, out_dir):
    params = StdioServerParameters(command=PY, args=["-m", "pads_mcp.server"], cwd=ROOT)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for i, (tool, args) in enumerate(calls):
                t = time.time()
                res = await s.call_tool(tool, args)
                print(f"=== {tool} ({time.time()-t:.1f}s){' ERROR' if res.isError else ''}")
                for c in res.content:
                    if c.type == "text":
                        txt = c.text
                        print(txt if len(txt) < 6000 else txt[:6000] + "\n...[truncated]")
                    elif c.type == "image":
                        p = os.path.join(out_dir, f"shot_{i}_{tool}.png")
                        open(p, "wb").write(base64.b64decode(c.data))
                        print("image ->", p)
                sys.stdout.flush()


calls = json.loads(sys.argv[1])
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "work")
asyncio.run(main(calls, out))
