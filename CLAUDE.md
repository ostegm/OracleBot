# OracleBot

Slack bot that runs Claude agents in Modal sandboxes to interact with the OracleLoop codebase.

## Development

```bash
# Install dependencies
uv sync

# Deploy to Modal
uv run modal deploy -m src.main

# Tail logs
uv run modal app logs oracle-slack-bot

# List running containers
uv run modal container list
```

## Architecture

- `src/main.py` - Slack bot handler, sandbox orchestration
- `src/proxy.py` - Anthropic API proxy for sandbox auth
- `src/agent/` - Code that runs inside Modal sandboxes
  - `agent_entrypoint.py` - Claude Agent SDK wrapper
  - `slack_tool_logger.py` - Block Kit tool activity display

## Modal Secrets

Required secrets (set via `modal secret create`):
- `slack-bot-secret`: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
- `github-deploy-key`: GITHUB_DEPLOY_KEY (SSH key for OracleLoop repo)
- `github-token`: GITHUB_TOKEN (PAT for gh CLI)
- `anthropic-secret`: ANTHROPIC_API_KEY

## Sandbox Behavior

- Sandboxes are named by Slack thread: `oracle-{team_id}-{thread_ts}`
- 5 min idle timeout, 5 hour max lifetime
- Session state persists on Modal Volume even if sandbox times out
- Agent has access to: Read, Write, Edit, Bash, Glob, Grep tools
