"""MCP transport and separate preparation-worker entry points."""

import argparse
import asyncio
from ipaddress import ip_address
from pathlib import Path
import sys


def _loopback_host(value: str) -> str:
    if value == "localhost":
        return value
    try:
        if ip_address(value).is_loopback:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("host must be a loopback address (127.0.0.1, ::1 or localhost)")


def _port(value: str) -> int:
    try:
        port = int(value)
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("port must be an integer between 1 and 65535")


def _task_timeout(value: str) -> float:
    try:
        timeout = float(value)
        if 1 <= timeout <= 900:
            return timeout
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("task timeout must be between 1 and 900 seconds")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Serve saved jobs and manual application tracking over MCP.")
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run one MCP server until stopped.")
    serve.add_argument("--db", type=Path, required=True, help="Existing job database; never created or migrated.")
    serve.add_argument("--preferences", type=Path, help="Optional non-secret country preferences JSON.")
    serve.add_argument("--preparation-dir", type=Path,
                       help="Opt in to fictional Ashby preparation tools using this separate ledger directory.")
    serve.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    serve.add_argument("--host", type=_loopback_host, default="127.0.0.1",
                       help="HTTP loopback bind address (default: 127.0.0.1).")
    serve.add_argument("--port", type=_port, default=8767, help="HTTP port (default: 8767); endpoint is /mcp.")
    worker = commands.add_parser("prepare-worker", help="Run the exclusive fictional Ashby preparation worker.")
    worker.add_argument("--preparation-dir", type=Path, required=True, help="Preparation ledger and artifacts directory.")
    worker.add_argument("--once", action="store_true", help="Process at most one queued task, then exit.")
    worker.add_argument("--driver", choices=("rules", "claude"), default="rules",
                        help="Use deterministic fields, or explicitly enable the constrained Claude driver.")
    worker.add_argument("--browser-channel", help="Optional installed Playwright browser channel, such as chrome.")
    worker.add_argument("--task-timeout", type=_task_timeout, default=120.0,
                        help="Maximum task duration in seconds, 1–900 (default: 120).")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare-worker":
            from auto_app.prepare.worker import run_worker

            asyncio.run(run_worker(args.preparation_dir, once=args.once, driver=args.driver,
                                   browser_channel=args.browser_channel, task_timeout=args.task_timeout))
            return 0

        from auto_app.agent.mcp_server import create_mcp_server
        from auto_app.agent.service import AgentService

        # Validate the selected database before starting either transport.
        AgentService(args.db, preferences_path=args.preferences).context()
        server = create_mcp_server(args.db, preferences_path=args.preferences,
                                   preparation_dir=args.preparation_dir)
        if args.transport == "stdio":
            server.run(transport="stdio")
        else:
            server.run(transport="streamable-http", host=args.host, port=args.port, json_response=True)
        return 0
    except (OSError, ValueError) as exc:
        print(f"Agent {args.command} failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
