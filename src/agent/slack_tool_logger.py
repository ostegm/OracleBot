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
