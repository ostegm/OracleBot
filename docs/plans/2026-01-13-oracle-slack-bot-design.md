# Oracle Slack Bot Design

**Date:** 2026-01-13
**Status:** Approved

## Overview

A Slack bot that provides an interactive Claude agent with full access to the OracleLoop codebase (a Kalshi prediction market trading system). Users can chat with the agent in Slack threads to explore code, run tools, and make edits within an isolated sandbox environment.

## Goals

**MVP:**
- Chat with a Claude agent aware of OracleLoop's code, tools, and skills
- Full read-write access to a cloned copy of the repo (changes stay in sandbox)
- Run OracleLoop CLI tools via Bash (`uv run python tools/...`)
- Real-time tool call logging in Slack threads
- Secure credential handling via proxy pattern
- Scale to zero when idle

**Future:**
- PR pushing capability
- Trading operations (Kalshi credentials)
- Multiple repo support

## Architecture

### Three Modal Applications

1. **Slack Bot Server** (`src/main.py`)
   - FastAPI + Slack Bolt handling events
   - Creates/resumes sandboxes per Slack thread
   - Streams agent responses back to Slack

2. **Anthropic API Proxy** (`src/proxy.py`)
   - Validates sandbox identity before injecting real API key
   - Sandboxes send Modal sandbox ID as "API key"
   - Proxy verifies sandbox is running, swaps in real credentials

3. **Agent Sandbox** (created dynamically)
   - Modal sandbox with OracleLoop pre-cloned
   - Runs `git pull` at startup for latest code
   - Claude Agent SDK with full tool access

### Data Flow

```
Slack message → Bot Server → Create/Resume Sandbox
                                    ↓
                            Agent runs in sandbox
                                    ↓
                            API calls → Proxy → Anthropic
                                    ↓
                            Response streams back
                                    ↓
                          Bot Server posts to Slack
```

## Sandbox Image

```python
sandbox_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git", "openssh-client", "curl")
    # Install Node.js (required for Claude CLI)
    .run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
        "apt-get install -y nodejs"
    )
    # Install Claude CLI globally
    .run_commands("npm install -g @anthropic-ai/claude-code")
    # Install uv (for OracleLoop tools)
    .run_commands("curl -LsSf https://astral.sh/uv/install.sh | sh")
    .env({"PATH": "/root/.local/bin:$PATH"})
    # Python dependencies
    .pip_install("claude-agent-sdk")
    # Configure SSH for GitHub
    .run_commands(
        "mkdir -p /root/.ssh",
        "ssh-keyscan github.com >> /root/.ssh/known_hosts"
    )
    # Clone OracleLoop at build time (warm cache)
    .run_commands(
        "git clone git@github.com:ostegm/OracleLoop.git /app/OracleLoop"
    )
    # Pre-install OracleLoop dependencies
    .run_commands("cd /app/OracleLoop && uv sync")
    # Add entrypoint script
    .add_local_dir("src/agent", "/agent")
)
```

### GitHub Authentication

Deploy key approach for private repo access:

1. Generate SSH keypair: `ssh-keygen -t ed25519 -f oracleloop-deploy-key`
2. Add public key to OracleLoop repo → Settings → Deploy Keys (read-write for future PR support)
3. Store private key in Modal secret: `github-deploy-key`

At sandbox start, the deploy key is written from environment to `/root/.ssh/id_ed25519` before running `git pull`.

## Agent Configuration

```python
SYSTEM_PROMPT = """
You are an assistant with full access to the OracleLoop codebase -
a prediction market trading system for Kalshi.

Your working directory is /app/OracleLoop. You can:
- Explore code with Read, Glob, Grep
- Modify files with Write, Edit
- Run OracleLoop tools: uv run python tools/<tool>.py <command>

Changes stay in this sandbox session. You cannot push to GitHub yet.
"""

ClaudeAgentOptions(
    resume=session_id,
    system_prompt=SYSTEM_PROMPT,
    cwd="/app/OracleLoop",
    allowed_tools=["Read", "Glob", "Grep", "Write", "Edit", "Bash"],
    permission_mode="acceptEdits",
    max_turns=15,
)
```

## Slack Event Handling

**Events:**
- `app_mention` - Start new conversation
- `message` (in thread) - Continue conversation if bot was mentioned in thread root

**Sandbox Naming:**
```python
sandbox_name = f"oracle-{team_id}-{thread_ts}"
```

**Sandbox Creation:**
```python
sandbox = modal.Sandbox.create(
    image=sandbox_image,
    secrets=[anthropic_proxy_secret, github_deploy_key],
    volumes={"/data": workspace_volume},
    timeout=5 * 60 * 60,      # 5 hour max
    idle_timeout=5 * 60,       # 5 min idle
)
```

## Tool Logging

Real-time Slack messages for tool calls:

```
⚙️ *Using Tool:* Read
   tools/market.py

🔧 *Tool Response:*
   [truncated content...]
```

Implemented via `PreToolUse` and `PostToolUse` hooks in the Claude Agent SDK.

## Anthropic API Proxy

Security model:
1. Sandbox sets `ANTHROPIC_API_KEY=<modal_sandbox_id>`
2. Claude CLI sends requests to proxy URL
3. Proxy validates sandbox exists and is running via `modal.Sandbox.from_id()`
4. Proxy swaps in real API key, forwards to api.anthropic.com
5. Response flows back transparently

Real credentials never enter the sandbox.

## Modal App Configuration

Scale to zero when idle:

```python
@app.function(
    secrets=[...],
    allow_concurrent_inputs=10,
    container_idle_timeout=300,  # 5 min idle
)
@modal.asgi_app()
def slack_bot():
    return fastapi_app
```

## Project Structure

```
OracleBot/
├── src/
│   ├── main.py                   # Slack bot server (Modal app)
│   ├── proxy.py                  # Anthropic API proxy (Modal app)
│   └── agent/
│       ├── agent_entrypoint.py   # Runs inside sandbox
│       └── slack_tool_logger.py  # Hook for tool logging
├── pyproject.toml                # Dependencies
├── .python-version               # 3.12
└── README.md
```

## Modal Resources

- **Volume:** `oracle-workspace` - Persists `/data` (session IDs)
- **Secrets:**
  - `slack-bot-secret` (SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET)
  - `anthropic-secret` (ANTHROPIC_API_KEY)
  - `github-deploy-key` (GITHUB_DEPLOY_KEY)

## Deployment

```bash
modal deploy src/main.py
modal deploy src/proxy.py
```

Set Slack app Event Subscription URL to the Modal webhook URL.

## Cost Model

- No messages → apps scale to 0 → $0
- First message → ~1-2s cold start → warm for 5 min
- Sandbox active during conversation (5 min idle timeout)
- Only pay for actual usage
