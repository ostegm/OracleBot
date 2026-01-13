import os
from typing import Any

import slack_sdk
from claude_agent_sdk import HookContext

# Tool icons for compact display
TOOL_ICONS = {
    "Read": "📖",
    "Write": "✏️",
    "Edit": "🔧",
    "Bash": "💻",
    "Glob": "🔍",
    "Grep": "🔎",
    "Task": "📋",
    "WebFetch": "🌐",
}


class SlackLogger:
    """Logs Claude agent tool use to Slack threads with compact Block Kit display."""

    def __init__(self, channel: str, thread_ts: str):
        self.slack_client = slack_sdk.WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        self.channel = channel
        self.thread_ts = thread_ts
        self.status_ts: str | None = None  # Track the status message to update
        self.tools_used: list[str] = []

    async def log_tool_use(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        """Log tool use with compact Block Kit format."""
        if "tool_input" in input_data:
            tool_name = input_data.get("tool_name", "Unknown")
            tool_input = input_data["tool_input"]
            self._update_status(tool_name, tool_input, is_response=False)
        elif "tool_response" in input_data:
            # Check for errors in response
            response = input_data.get("tool_response", {})
            if response.get("is_error"):
                self._post_error(response)
        return {}

    def _get_tool_summary(self, tool_name: str, tool_input: dict) -> str:
        """Get a one-line summary of the tool use."""
        icon = TOOL_ICONS.get(tool_name, "⚙️")

        if tool_name == "Read":
            path = tool_input.get("file_path", "")
            filename = os.path.basename(path) if path else "file"
            return f"{icon} Reading `{filename}`"
        elif tool_name == "Write":
            path = tool_input.get("file_path", "")
            filename = os.path.basename(path) if path else "file"
            return f"{icon} Writing `{filename}`"
        elif tool_name == "Edit":
            path = tool_input.get("file_path", "")
            filename = os.path.basename(path) if path else "file"
            return f"{icon} Editing `{filename}`"
        elif tool_name == "Bash":
            cmd = tool_input.get("command", "")
            # Truncate long commands
            if len(cmd) > 40:
                cmd = cmd[:37] + "..."
            return f"{icon} `{cmd}`"
        elif tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            return f"{icon} Searching `{pattern}`"
        else:
            return f"{icon} {tool_name}"

    def _update_status(self, tool_name: str, tool_input: dict, is_response: bool) -> None:
        """Update or create the status message showing tool activity."""
        summary = self._get_tool_summary(tool_name, tool_input)
        self.tools_used.append(summary)

        # Keep only last 5 tools to avoid message getting too long
        display_tools = self.tools_used[-5:]

        blocks = [
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "🤖 *Working...*"}
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(display_tools)
                }
            }
        ]

        if self.status_ts:
            # Update existing message
            try:
                self.slack_client.chat_update(
                    channel=self.channel,
                    ts=self.status_ts,
                    blocks=blocks,
                    text="Working...",
                )
            except Exception:
                # If update fails, post new message
                self._post_new_status(blocks)
        else:
            self._post_new_status(blocks)

    def _post_new_status(self, blocks: list) -> None:
        """Post a new status message and track its timestamp."""
        response = self.slack_client.chat_postMessage(
            channel=self.channel,
            thread_ts=self.thread_ts,
            blocks=blocks,
            text="Working...",
        )
        self.status_ts = response["ts"]

    def _post_error(self, response: dict) -> None:
        """Post error details when a tool fails."""
        error_msg = str(response)
        if len(error_msg) > 500:
            error_msg = error_msg[:500] + "..."

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"❌ *Tool Error:*\n```{error_msg}```"
                }
            }
        ]

        self.slack_client.chat_postMessage(
            channel=self.channel,
            thread_ts=self.thread_ts,
            blocks=blocks,
            text=f"Tool Error: {error_msg}",
        )
