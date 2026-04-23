"""
Diagnostic: list the tools exposed by the HubSpot MCP server.

Run once after installing @hubspot/mcp-server to discover the exact tool
names your version exposes, then update agent/integrations/hubspot_mcp.py
to call those names.

Usage:
    uv run python -m scripts.list_hubspot_mcp_tools
"""

from __future__ import annotations

import asyncio
import json

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.config import settings


async def main() -> None:
    if not settings.hubspot_access_token:
        print("HUBSPOT_ACCESS_TOKEN is empty — set it in .env first.")
        return

    args_list = [a.strip() for a in settings.hubspot_mcp_args.split(",") if a.strip()]
    params = StdioServerParameters(
        command=settings.hubspot_mcp_command,
        args=args_list,
        env={"PRIVATE_APP_ACCESS_TOKEN": settings.hubspot_access_token},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_resp = await session.list_tools()
            tools = tools_resp.tools

            # Write to JSON to avoid Windows console encoding issues with emoji
            import pathlib
            out_path = pathlib.Path("outputs/hubspot_mcp_tools.json")
            out_path.parent.mkdir(exist_ok=True)
            dump = [
                {
                    "name": t.name,
                    "description": (t.description or "").strip(),
                    "args": list((t.inputSchema or {}).get("properties", {}).keys()),
                    "inputSchema": t.inputSchema,
                }
                for t in tools
            ]
            out_path.write_text(
                json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            # Print names only (ASCII-safe)
            print(f"\n=== HubSpot MCP server exposes {len(tools)} tools ===\n")
            for t in tools:
                args = list((t.inputSchema or {}).get("properties", {}).keys())
                print(f"  {t.name}  ({', '.join(args[:6])})")
            print(f"\nFull tool catalog written to: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
