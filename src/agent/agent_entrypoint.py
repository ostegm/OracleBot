#!/usr/bin/env python3
"""Entrypoint script for Claude Agent SDK inside Modal sandbox."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage
from slack_tool_logger import SlackLogger

SESSIONS_FILE = Path("/data/sessions.json")

SYSTEM_PROMPT = """You are an assistant with full access to the OracleLoop codebase -
a prediction market trading system for Kalshi.

Your working directory is /app/OracleLoop. You can:
- Explore code with Read, Glob, Grep
- Modify files with Write, Edit
- Run OracleLoop tools: uv run python tools/<tool>.py <command>

Key tools available:
- tools/market.py - Market data and event queries
- tools/position.py - Portfolio positions
- tools/trade.py - Trading operations (when enabled)
- tools/analyze.py - Analysis utilities

Changes stay in this sandbox session. You cannot push to GitHub yet.
"""


def load_session_id(sandbox_name: str) -> str | None:
    """Load existing session ID for this sandbox/thread."""
    if not SESSIONS_FILE.exists():
        return None
    sessions = json.loads(SESSIONS_FILE.read_text())
    return sessions.get(sandbox_name)


def save_session_id(sandbox_name: str, session_id: str) -> None:
    """Save session ID for this sandbox/thread."""
    sessions = {}
    if SESSIONS_FILE.exists():
        sessions = json.loads(SESSIONS_FILE.read_text())
    sessions[sandbox_name] = session_id
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


async def main(user_msg: str, sandbox_name: str, sandbox_id: str, channel: str | None, thread_ts: str | None):
    # Use the sandbox ID as the API key for the proxy. The proxy will exchange it
    # for the real key, as long as the sandbox is still running.
    os.environ["ANTHROPIC_API_KEY"] = sandbox_id

    # Set up tool logging hooks if Slack channel info provided
    hooks = None
    if channel and thread_ts:
        logger = SlackLogger(channel, thread_ts)
        hooks = {
            "PreToolUse": [HookMatcher(hooks=[logger.log_tool_use])],
            "PostToolUse": [HookMatcher(hooks=[logger.log_tool_use])],
        }

    session_id = load_session_id(sandbox_name)

    options = ClaudeAgentOptions(
        resume=session_id,
        system_prompt=SYSTEM_PROMPT,
        cwd="/app/OracleLoop",
        allowed_tools=["Read", "Glob", "Grep", "Write", "Edit", "Bash"],
        permission_mode="acceptEdits",
        max_turns=15,
        hooks=hooks,
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_msg)

        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                save_session_id(sandbox_name, msg.session_id)
            elif hasattr(msg, "content"):
                for block in msg.content:
                    if hasattr(block, "text"):
                        print(block.text)

        print("Agent turn complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", type=str, required=True)
    parser.add_argument("--sandbox-name", type=str, required=True)
    parser.add_argument("--sandbox-id", type=str, required=True)
    parser.add_argument("--channel", type=str, required=False)
    parser.add_argument("--thread-ts", type=str, required=False)
    args = parser.parse_args()

    asyncio.run(main(args.message, args.sandbox_name, args.sandbox_id, args.channel, args.thread_ts))
