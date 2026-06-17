"""Live test harness for the browser MCP server (Phase 4 Stage 2).

Connects to a RUNNING Symmetria IDE's browser MCP server as an external
MCP client — exactly the path a spawned agent takes — and drives the
`browser_*` tools, printing each result. This is the way to exercise the
runJavaScript-based tools (`eval_js`/`snapshot`/`click`/`fill`): they only
fire their callback on a compositor that drives frames, so the headless
pytest suite can't cover them, but this can.

Watch the IDE's browser surface (Ctrl+Shift+B) change live as it runs.

Usage:
    # 1. Launch the IDE normally (its MCP server starts in start()):
    PYTHONPATH=src python -m symmetria_ide
    # 2. In another terminal, drive it (auto-discovers the running IDE):
    python bench/browser_mcp_live.py
    # variants:
    python bench/browser_mcp_live.py --open https://news.ycombinator.com
    python bench/browser_mcp_live.py --url http://127.0.0.1:PORT/mcp

Needs `python-mcp` (the same dep the IDE's server needs). If no IDE is
running (no $TMPDIR/symmetria-browser-mcp-*.json), it says so and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import tempfile


def discover_url() -> str | None:
    """The newest LIVE IDE's MCP URL, from the per-launch config file.

    The config filename encodes the IDE's pid (`symmetria-browser-mcp-<pid>.json`),
    so we skip configs left behind by dead instances (hard-killed IDEs / test
    harnesses) rather than trying to connect to a port nobody's listening on."""
    prefix = "symmetria-browser-mcp-"
    pattern = os.path.join(tempfile.gettempdir(), f"{prefix}*.json")
    live: list[tuple[float, str]] = []
    for path in glob.glob(pattern):
        try:
            pid = int(os.path.basename(path)[len(prefix) : -len(".json")])
        except ValueError:
            continue
        if os.path.exists(f"/proc/{pid}"):  # the IDE that wrote it is still alive
            live.append((os.path.getmtime(path), path))
    if not live:
        return None
    live.sort()
    with open(live[-1][1]) as handle:
        config = json.load(handle)
    return config["mcpServers"]["symmetria-browser"]["url"]


def result_payload(res) -> object:
    """FastMCP returns the tool's dict in `structuredContent` OR as JSON in
    `content[].text` — try both so the output is always readable."""
    if getattr(res, "structuredContent", None):
        return res.structuredContent
    return [getattr(c, "text", str(c)) for c in res.content]


async def run(url: str, open_url: str) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            async def call(name: str, **args) -> object:
                res = await session.call_tool(name, args)
                print(f"\n{name}({args}) ->")
                print("   ", result_payload(res))
                return res

            # The autonomous loop: open a window, look at it, read + snapshot.
            await call("browser_open", url=open_url)
            await asyncio.sleep(1.5)  # let the page load (real frames driving)
            await call("browser_list_windows")
            await call("browser_eval_js", code="document.title", window=1)
            await call("browser_snapshot", window=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", help="MCP server URL (default: auto-discover the running IDE)"
    )
    parser.add_argument(
        "--open",
        default="https://example.com",
        help="URL to open (default: example.com)",
    )
    args = parser.parse_args()

    url = args.url or discover_url()
    if not url:
        print(
            "No running IDE browser MCP server found "
            "($TMPDIR/symmetria-browser-mcp-*.json missing). "
            "Launch the IDE first: PYTHONPATH=src python -m symmetria_ide",
            file=sys.stderr,
        )
        return 1
    print("connecting to", url)
    try:
        asyncio.run(run(url, args.open))
    except Exception as exc:  # noqa: BLE001 — surface any connect/transport failure cleanly
        print(
            f"\nfailed to drive the MCP server at {url}: {exc!r}\n"
            "Is the IDE still running? (a stale config can linger after a hard kill — "
            "rm $TMPDIR/symmetria-browser-mcp-*.json and relaunch the IDE).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
