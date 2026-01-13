import logging
import os
import re
from pathlib import Path

import modal

# Configure logging for Modal
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from .proxy import anthropic_proxy, app as proxy_app

app = modal.App("oracle-slack-bot")
app.include(proxy_app)

slack_secret = modal.Secret.from_name("slack-bot-secret")  # SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
github_deploy_key = modal.Secret.from_name("github-deploy-key")  # GITHUB_DEPLOY_KEY
github_token = modal.Secret.from_name("github-token")  # GITHUB_TOKEN for gh CLI

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
    # Install GitHub CLI
    .run_commands(
        "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg",
        "chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg",
        'echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null',
        "apt-get update && apt-get install -y gh",
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
    logger.info(f"SSH key present: {bool(deploy_key)}, length: {len(deploy_key)}")

    if deploy_key:
        sb.exec(
            "bash",
            "-c",
            f'echo "{deploy_key}" > /root/.ssh/id_ed25519 && chmod 600 /root/.ssh/id_ed25519',
        ).wait()
        logger.info("SSH key written to sandbox")
    else:
        logger.warning("No SSH key found in environment!")


def clone_or_update_repo(sb: modal.Sandbox) -> None:
    """Clone OracleLoop if missing, otherwise pull latest."""
    # Check if repo exists
    check = sb.exec("test", "-d", "/app/OracleLoop/.git")
    if check.wait() == 0:
        logger.info("Repo exists, pulling latest")
        sb.exec("bash", "-c", "cd /app/OracleLoop && git pull").wait()
        return

    logger.info("Cloning OracleLoop repo...")
    clone = sb.exec("git", "clone", "git@github.com:ostegm/OracleLoop.git", "/app/OracleLoop")
    stdout_lines = list(clone.stdout)
    exit_code = clone.wait()
    stderr = clone.stderr.read()

    if exit_code != 0:
        logger.error(f"Clone FAILED (exit {exit_code}): {stderr}")
        return

    logger.info("Clone successful, installing dependencies...")
    sb.exec("bash", "-c", "cd /app/OracleLoop && uv sync").wait()
    logger.info("Dependencies installed")


def run_agent_turn(
    sb: modal.Sandbox, user_message: str, channel: str, thread_ts: str, sandbox_name: str
):
    """Execute one turn of Claude conversation in sandbox."""
    args = [
        "python", "-u",  # Unbuffered output for real-time logging
        "/agent/agent_entrypoint.py",
        "--message", user_message,
        "--sandbox-name", sandbox_name,
        "--sandbox-id", sb.object_id,
    ]

    if DEBUG_TOOL_USE:
        args.extend(["--channel", channel, "--thread-ts", thread_ts])

    logger.info(f"[{sandbox_name}] Starting agent turn")
    process = sb.exec(*args)

    # Stream stdout - log everything, yield non-log lines as responses
    for line in process.stdout:
        line = line.strip()
        if line.startswith("[LOG]"):
            # Internal log line from agent - just log it
            logger.info(f"[{sandbox_name}] {line}")
        elif line:
            # Response line - log and yield
            logger.info(f"[{sandbox_name}] Response: {line[:100]}...")
            yield {"response": line}

    exit_code = process.wait()
    logger.info(f"[{sandbox_name}] Agent exited with status {exit_code}")

    # Capture and log stderr
    stderr = process.stderr.read()
    if stderr:
        for line in stderr.strip().split("\n"):
            logger.error(f"[{sandbox_name}] STDERR: {line}")
        yield {"response": f"*** ERROR ***\n{stderr}"}


def post_status(client, channel: str, thread_ts: str, text: str, emoji: str = "⏳") -> None:
    """Post a status update to the Slack thread."""
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f"{emoji} {text}",
    )


def process_message(body, client, user_message):
    """Process incoming Slack message and run agent."""
    channel = body["event"]["channel"]
    thread_ts = body["event"].get("thread_ts", body["event"]["ts"])

    sandbox_name = f"oracle-{body['team_id']}-{thread_ts}".replace(".", "-")

    try:
        sb = modal.Sandbox.from_name(app_name=app.name, name=sandbox_name)
        logger.info(f"Reusing existing sandbox: {sandbox_name}")
        post_status(client, channel, thread_ts, "Resuming session...", "🔄")
    except modal.exception.NotFoundError:
        logger.info(f"Creating new sandbox: {sandbox_name}")
        post_status(client, channel, thread_ts, "Starting new session...", "🚀")
        try:
            sb = modal.Sandbox.create(
                app=app,
                image=sandbox_image,
                secrets=[slack_secret, github_deploy_key, github_token] if DEBUG_TOOL_USE else [github_deploy_key, github_token],
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
        except modal.exception.AlreadyExistsError:
            # Race condition: another request created it first, just use it
            logger.info(f"Sandbox created by concurrent request, reusing: {sandbox_name}")
            sb = modal.Sandbox.from_name(app_name=app.name, name=sandbox_name)

    # Always ensure SSH and repo are set up (idempotent operations)
    setup_github_ssh(sb)
    clone_or_update_repo(sb)

    # Set up /data symlink for session persistence
    data_dir = (VOL_MOUNT_PATH / sandbox_name).as_posix()
    sb.exec("bash", "-c", f"mkdir -p {data_dir} && ln -sf {data_dir} /data").wait()

    for result in run_agent_turn(sb, user_message, channel, thread_ts, sandbox_name):
        if result.get("response"):
            client.chat_postMessage(channel=channel, text=result["response"], thread_ts=thread_ts)


@app.function(
    secrets=[slack_secret, github_deploy_key, github_token],
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
