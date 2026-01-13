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
    scaledown_window=300,  # 5 min idle
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
