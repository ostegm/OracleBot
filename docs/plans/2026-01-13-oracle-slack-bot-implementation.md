# Oracle Slack Bot Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Slack bot that provides an interactive Claude agent with full access to the OracleLoop codebase.

**Architecture:** Three Modal applications - (1) Slack Bot Server handles events and creates per-thread sandboxes, (2) Anthropic API Proxy validates sandbox identity before forwarding requests, (3) Agent Sandbox runs Claude with full OracleLoop access.

**Tech Stack:** Modal, FastAPI, Slack Bolt, Claude Agent SDK, Python 3.12

**Reference:** See design at `docs/plans/2026-01-13-oracle-slack-bot-design.md`

---

## Task 1: Project Setup

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`

**Step 1: Create pyproject.toml**

```toml
[project]
name = "oracle-bot"
version = "0.1.0"
description = "Slack bot with Claude agent access to OracleLoop codebase"
requires-python = ">=3.12"
dependencies = [
    "modal>=0.65.66",
]

[tool.ruff]
line-length = 120

[tool.ruff.lint]
select = ['E', 'F', 'W', 'I']

[tool.ruff.lint.isort]
combine-as-imports = true
known-third-party = ["modal"]
```

**Step 2: Create .python-version**

```
3.12
```

**Step 3: Verify setup**

Run: `uv sync`
Expected: Dependencies installed successfully

**Step 4: Commit**

```bash
git add pyproject.toml .python-version
git commit -m "feat: initialize project with dependencies"
```

---

## Task 2: Anthropic API Proxy

**Files:**
- Create: `src/proxy.py`

**Step 1: Create proxy implementation**

```python
import os

import modal

proxy_image = modal.Image.debian_slim(python_version="3.12").pip_install("httpx", "fastapi")

anthropic_secret = modal.Secret.from_name("anthropic-secret")  # ANTHROPIC_API_KEY

app = modal.App("oracle-anthropic-proxy")


@app.function(
    secrets=[anthropic_secret],
    image=proxy_image,
    region="us-east-1",
    container_idle_timeout=300,
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def anthropic_proxy():
    import httpx
    from fastapi import FastAPI, HTTPException, Request, Response

    proxy_app = FastAPI()

    @proxy_app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]
    )
    async def proxy(request: Request, path: str):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}

        sandbox_id = headers.get("x-api-key")

        try:
            sb = await modal.Sandbox.from_id.aio(sandbox_id)
            if sb.returncode is not None:
                raise HTTPException(status_code=403, detail="Sandbox no longer running")
        except modal.exception.NotFoundError:
            raise HTTPException(status_code=403, detail="Invalid sandbox ID")

        headers["x-api-key"] = os.environ["ANTHROPIC_API_KEY"]

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.anthropic.com/{path}",
                headers=headers,
                content=await request.body(),
                timeout=300.0,
            )

        return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")

    return proxy_app
```

**Step 2: Verify syntax**

Run: `python -m py_compile src/proxy.py`
Expected: No output (success)

**Step 3: Commit**

```bash
git add src/proxy.py
git commit -m "feat: add Anthropic API proxy with sandbox validation"
```

---

## Task 3: Slack Tool Logger

**Files:**
- Create: `src/agent/slack_tool_logger.py`

**Step 1: Create directory structure**

```bash
mkdir -p src/agent
```

**Step 2: Create slack_tool_logger.py**

```python
import os
import re
from typing import Any

import slack_sdk
from claude_agent_sdk import HookContext

HEREDOC_PATTERN = re.compile(
    r"<<-?\s*(?:(?P<quote>['\"])(?P<quoted_label>[\w-]+)(?P=quote)|\\?(?P<simple_label>[\w-]+))"
)


class SlackLogger:
    """Logs Claude agent tool use to Slack threads."""

    def __init__(self, channel: str, thread_ts: str):
        self.slack_client = slack_sdk.WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        self.channel = channel
        self.thread_ts = thread_ts

    async def log_tool_use(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        if "tool_response" in input_data:
            self._log_tool_response(input_data)
        elif "tool_input" in input_data:
            self._log_tool_input(input_data)
        return {}

    def _log_tool_response(self, input_data: dict[str, Any]) -> None:
        """Log tool response to Slack."""
        response_data = input_data["tool_response"].copy()
        # Remove verbose fields
        response_data.pop("command", None)
        response_data.pop("content", None)
        response_data.pop("file_path", None)

        response_str = str(response_data)
        if len(response_str) > 500:
            response_str = response_str[:500] + "..."

        message = f"🔧 *Tool Response:*\n```\n{response_str}\n```"
        self.slack_client.chat_postMessage(
            channel=self.channel,
            text=message,
            thread_ts=self.thread_ts,
            mrkdwn=True,
        )

    def _log_tool_input(self, input_data: dict[str, Any]) -> None:
        """Log tool input to Slack."""
        tool_name = input_data.get("tool_name", "Unknown Tool")
        tool_input = input_data["tool_input"]

        # Try to extract file write content for better display
        content, filename = self._extract_file_content(tool_input)
        if content and filename:
            try:
                self.slack_client.files_upload_v2(
                    channel=self.channel,
                    content=content,
                    filename=filename,
                    title=f"Generated {filename}",
                    thread_ts=self.thread_ts,
                    initial_comment=f"⚙️ *Using Tool:* `{tool_name}`",
                )
                return
            except Exception as e:
                print(f"Error uploading file content: {e}")

        input_str = str(tool_input)
        if len(input_str) > 500:
            input_str = input_str[:500] + "..."

        message = f"⚙️ *Using Tool:* `{tool_name}`\n```\n{input_str}\n```"
        self.slack_client.chat_postMessage(
            channel=self.channel,
            text=message,
            thread_ts=self.thread_ts,
            mrkdwn=True,
        )

    def _extract_file_content(self, tool_input: dict) -> tuple[str | None, str | None]:
        """Extract file content from tool input for cleaner display."""
        if "command" in tool_input:
            return self._extract_heredoc_content(tool_input["command"])
        elif "content" in tool_input and "file_path" in tool_input:
            return tool_input["content"], os.path.basename(tool_input["file_path"])
        return None, None

    def _extract_heredoc_content(self, command: str) -> tuple[str | None, str | None]:
        """Extract content from heredoc in bash command."""
        match = HEREDOC_PATTERN.search(command)
        if not match:
            return None, None

        label = match.group("quoted_label") or match.group("simple_label")

        newline_after_marker = command.find("\n", match.end())
        if newline_after_marker == -1:
            return None, None

        content_start = newline_after_marker + 1
        closing_pattern = re.compile(rf"(?m)^\s*{re.escape(label)}\s*$")
        closing_match = closing_pattern.search(command, pos=content_start)
        if not closing_match:
            return None, None

        content = command[content_start : closing_match.start()]

        # Try to infer filename from redirect
        redirect_match = re.search(r">\s*([^\s]+)", command)
        if redirect_match:
            filename = os.path.basename(redirect_match.group(1).strip("'\""))
        else:
            filename = f"{label.lower()}_heredoc.txt"

        return content, filename
```

**Step 3: Verify syntax**

Run: `python -m py_compile src/agent/slack_tool_logger.py`
Expected: No output (success)

**Step 4: Commit**

```bash
git add src/agent/slack_tool_logger.py
git commit -m "feat: add Slack tool logger for real-time tool use display"
```

---

## Task 4: Agent Entrypoint

**Files:**
- Create: `src/agent/agent_entrypoint.py`

**Step 1: Create agent_entrypoint.py**

```python
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


async def main(user_msg: str, sandbox_name: str, channel: str | None, thread_ts: str | None):
    # Use the sandbox ID as the API key for the proxy. The proxy will exchange it
    # for the real key, as long as the sandbox is still running.
    os.environ["ANTHROPIC_API_KEY"] = os.environ.get("MODAL_SANDBOX_ID", "")

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
    parser.add_argument("--channel", type=str, required=False)
    parser.add_argument("--thread-ts", type=str, required=False)
    args = parser.parse_args()

    asyncio.run(main(args.message, args.sandbox_name, args.channel, args.thread_ts))
```

**Step 2: Verify syntax**

Run: `python -m py_compile src/agent/agent_entrypoint.py`
Expected: No output (success)

**Step 3: Commit**

```bash
git add src/agent/agent_entrypoint.py
git commit -m "feat: add agent entrypoint with session persistence and tool hooks"
```

---

## Task 5: Slack Bot Server

**Files:**
- Create: `src/main.py`

**Step 1: Create main.py**

```python
import os
import re
from pathlib import Path

import modal

from .proxy import anthropic_proxy, app as proxy_app

app = modal.App("oracle-slack-bot")
app.include(proxy_app)

slack_secret = modal.Secret.from_name("slack-bot-secret")  # SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
github_deploy_key = modal.Secret.from_name("github-deploy-key")  # GITHUB_DEPLOY_KEY

vol = modal.Volume.from_name("oracle-workspace", create_if_missing=True)

AGENT_ENTRYPOINT = Path(__file__).parent / "agent"
VOL_MOUNT_PATH = Path("/workspace")
DEBUG_TOOL_USE = True

sandbox_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git", "openssh-client", "curl")
    # Install Node.js (required for Claude CLI)
    .run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
        "apt-get install -y nodejs",
    )
    # Install Claude CLI globally
    .run_commands("npm install -g @anthropic-ai/claude-code")
    # Install uv (for OracleLoop tools)
    .run_commands("curl -LsSf https://astral.sh/uv/install.sh | sh")
    .env({"PATH": "/root/.local/bin:$PATH"})
    # Python dependencies
    .pip_install("claude-agent-sdk", "slack-sdk")
    # Configure SSH for GitHub
    .run_commands(
        "mkdir -p /root/.ssh",
        "ssh-keyscan github.com >> /root/.ssh/known_hosts",
    )
    # Add entrypoint script
    .add_local_dir(AGENT_ENTRYPOINT, "/agent")
)

slack_bot_image = modal.Image.debian_slim(python_version="3.12").pip_install("slack-bolt", "fastapi")


def setup_github_ssh(sb: modal.Sandbox) -> None:
    """Write GitHub deploy key from environment to SSH config."""
    deploy_key = os.environ.get("GITHUB_DEPLOY_KEY", "")
    if deploy_key:
        sb.exec(
            "bash",
            "-c",
            f'echo "{deploy_key}" > /root/.ssh/id_ed25519 && chmod 600 /root/.ssh/id_ed25519',
        )


def clone_or_update_repo(sb: modal.Sandbox) -> None:
    """Clone OracleLoop if missing, otherwise pull latest."""
    # Check if repo exists
    check = sb.exec("test", "-d", "/app/OracleLoop/.git")
    if check.wait() == 0:
        # Repo exists, pull latest
        sb.exec("bash", "-c", "cd /app/OracleLoop && git pull")
    else:
        # Clone fresh
        sb.exec("git", "clone", "git@github.com:ostegm/OracleLoop.git", "/app/OracleLoop")
        # Install dependencies
        sb.exec("bash", "-c", "cd /app/OracleLoop && uv sync")


def run_agent_turn(
    sb: modal.Sandbox, user_message: str, channel: str, thread_ts: str, sandbox_name: str
):
    """Execute one turn of Claude conversation in sandbox."""
    args = ["python", "/agent/agent_entrypoint.py", "--message", user_message, "--sandbox-name", sandbox_name]

    if DEBUG_TOOL_USE:
        args.extend(["--channel", channel, "--thread-ts", thread_ts])

    process = sb.exec(*args)

    for line in process.stdout:
        yield {"response": line}

    exit_code = process.wait()
    print(f"Agent process exited with status {exit_code}")

    stderr = process.stderr.read()
    if stderr:
        yield {"response": f"*** ERROR ***\n{stderr}"}


def process_message(body, client, user_message):
    """Process incoming Slack message and run agent."""
    channel = body["event"]["channel"]
    thread_ts = body["event"].get("thread_ts", body["event"]["ts"])

    sandbox_name = f"oracle-{body['team_id']}-{thread_ts}".replace(".", "-")

    try:
        sb = modal.Sandbox.from_name(app_name=app.name, name=sandbox_name)
    except modal.exception.NotFoundError:
        sb = modal.Sandbox.create(
            app=app,
            image=sandbox_image,
            secrets=[slack_secret, github_deploy_key] if DEBUG_TOOL_USE else [github_deploy_key],
            volumes={VOL_MOUNT_PATH: vol},
            workdir="/app",
            env={
                "CLAUDE_CONFIG_DIR": (VOL_MOUNT_PATH / "claude-config").as_posix(),
                "ANTHROPIC_BASE_URL": anthropic_proxy.get_web_url(),
            },
            idle_timeout=5 * 60,  # 5 min idle
            timeout=5 * 60 * 60,  # 5 hour max
            name=sandbox_name,
        )
        # Set up GitHub SSH for private repo access
        setup_github_ssh(sb)
        # Clone/update OracleLoop
        clone_or_update_repo(sb)

    # Set up /data symlink for session persistence
    data_dir = (VOL_MOUNT_PATH / sandbox_name).as_posix()
    sb.exec("bash", "-c", f"mkdir -p {data_dir} && ln -sf {data_dir} /data")

    for result in run_agent_turn(sb, user_message, channel, thread_ts, sandbox_name):
        if result.get("response"):
            client.chat_postMessage(channel=channel, text=result["response"], thread_ts=thread_ts)


@app.function(
    secrets=[slack_secret],
    image=slack_bot_image,
    container_idle_timeout=300,  # 5 min idle
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def slack_bot():
    from fastapi import FastAPI, Request
    from slack_bolt import App as SlackApp
    from slack_bolt.adapter.fastapi import SlackRequestHandler

    slack_app = SlackApp(
        token=os.environ["SLACK_BOT_TOKEN"],
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    )

    fastapi_app = FastAPI()
    handler = SlackRequestHandler(slack_app)

    @slack_app.event("app_mention")
    def handle_mention(body, client, context, logger):
        user_message = body["event"]["text"]
        # Remove bot mention from message
        user_message = re.sub(r"<@[A-Z0-9]+>", "", user_message).strip()
        process_message(body, client, user_message)

    @slack_app.event("message")
    def handle_message(body, client, context, logger):
        event = body["event"]
        # Skip bot messages
        if event.get("subtype") == "bot_message" or event.get("bot_id"):
            return
        # Only process thread replies
        if "thread_ts" not in event:
            return
        # Skip if this message mentions the bot (handled by app_mention)
        if f"<@{context.bot_user_id}>" in event.get("text", ""):
            return

        # Check if bot was mentioned in thread root
        try:
            history = client.conversations_replies(
                channel=event["channel"],
                ts=event["thread_ts"],
                limit=1,
            )
            if (
                not history.get("messages")
                or f"<@{context.bot_user_id}>" not in history["messages"][0].get("text", "")
            ):
                return
        except Exception:
            return

        user_message = event["text"]
        process_message(body, client, user_message)

    @fastapi_app.post("/")
    async def root(request: Request):
        return await handler.handle(request)

    return fastapi_app
```

**Step 2: Verify syntax**

Run: `python -m py_compile src/main.py`
Expected: No output (success)

**Step 3: Commit**

```bash
git add src/main.py
git commit -m "feat: add Slack bot server with sandbox management"
```

---

## Task 6: Create src/__init__.py

**Files:**
- Create: `src/__init__.py`

**Step 1: Create empty __init__.py**

```python
# Oracle Slack Bot
```

**Step 2: Commit**

```bash
git add src/__init__.py
git commit -m "feat: add src package init"
```

---

## Task 7: Verify Full Implementation

**Step 1: Run full syntax check**

```bash
python -m py_compile src/main.py src/proxy.py src/agent/agent_entrypoint.py src/agent/slack_tool_logger.py
```

Expected: No output (success)

**Step 2: Verify Modal can parse the apps**

```bash
modal app list
```

Expected: Shows existing apps (no syntax errors)

**Step 3: Final commit with all files**

If any uncommitted changes remain:
```bash
git status
git add -A
git commit -m "feat: complete Oracle Slack Bot implementation"
```

---

## Post-Implementation: Manual Setup Required

After code is complete, these manual steps are needed:

### 1. Create Modal Secrets

```bash
# Slack credentials
modal secret create slack-bot-secret \
  SLACK_BOT_TOKEN=xoxb-your-token \
  SLACK_SIGNING_SECRET=your-secret

# Anthropic API key
modal secret create anthropic-secret \
  ANTHROPIC_API_KEY=sk-ant-your-key

# GitHub deploy key (generate with: ssh-keygen -t ed25519 -f oracleloop-deploy-key)
modal secret create github-deploy-key \
  GITHUB_DEPLOY_KEY="$(cat oracleloop-deploy-key)"
```

### 2. Add Deploy Key to OracleLoop

1. Go to OracleLoop repo → Settings → Deploy Keys
2. Add the public key (`oracleloop-deploy-key.pub`)
3. Enable "Allow write access" for future PR support

### 3. Deploy to Modal

```bash
modal deploy src/main.py
```

### 4. Configure Slack App

1. Get the Modal webhook URL from deploy output
2. Go to Slack App → Event Subscriptions
3. Set Request URL to the Modal webhook URL
4. Subscribe to events: `app_mention`, `message.channels`

---

## Summary

| Task | Description | Files |
|------|-------------|-------|
| 1 | Project Setup | pyproject.toml, .python-version |
| 2 | Anthropic API Proxy | src/proxy.py |
| 3 | Slack Tool Logger | src/agent/slack_tool_logger.py |
| 4 | Agent Entrypoint | src/agent/agent_entrypoint.py |
| 5 | Slack Bot Server | src/main.py |
| 6 | Package Init | src/__init__.py |
| 7 | Verification | (syntax checks) |
