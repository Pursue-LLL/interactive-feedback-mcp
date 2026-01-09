# Interactive Feedback MCP
# Developed by Fábio Ferreira (https://x.com/fabiomlferreira)
# Inspired by/related to dotcursorrules.com (https://dotcursorrules.com/)
import os
import sys
import json
import base64
import asyncio
import tempfile
import subprocess

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.utilities.types import Image as MCPImage
from mcp.types import TextContent
from pydantic import Field

# Version identifier for debugging
SERVER_VERSION = "v0.1.3-image-support"

# The log_level is necessary for Cline to work: https://github.com/jlowin/fastmcp/issues/81
mcp = FastMCP("Interactive Feedback MCP")

# Configuration
AUTO_FEEDBACK_TIMEOUT_SECONDS = int(
    os.getenv("INTERACTIVE_FEEDBACK_TIMEOUT_SECONDS", "290")
)

# Log version on startup
print(f"[INFO] Interactive Feedback MCP {SERVER_VERSION} starting...", file=sys.stderr)


def _cleanup_process(proc: subprocess.Popen | None) -> None:
    """安全地终止子进程"""
    if proc is None:
        return
    if proc.poll() is None:  # 进程仍在运行
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _cleanup_file(file_path: str) -> None:
    """安全地删除临时文件"""
    try:
        if os.path.exists(file_path):
            os.unlink(file_path)
    except OSError:
        pass  # 忽略删除失败的情况


async def launch_feedback_ui_async(
    project_directory: str, summary: str, task_id: str, timeout_seconds: int = 290
) -> dict[str, Any]:
    """异步启动反馈UI并等待结果，不阻塞MCP服务器的其他请求

    正确处理请求取消：当 MCP 客户端取消请求时，会抛出 asyncio.CancelledError，
    我们需要捕获它，清理资源，然后重新抛出让 FastMCP 正确处理。
    """
    # Create a temporary file for the feedback result
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_file = tmp.name

    proc: subprocess.Popen | None = None

    try:
        # Get the absolute path to feedback_ui.py
        script_dir = os.path.dirname(os.path.abspath(__file__))
        feedback_ui_path = os.path.abspath(os.path.join(script_dir, "feedback_ui.py"))

        # Ensure the path exists
        if not os.path.exists(feedback_ui_path):
            raise Exception(f"feedback_ui.py not found at: {feedback_ui_path}")

        # Run feedback_ui.py as a separate process
        # NOTE: There appears to be a bug in uv, so we need
        # to pass a bunch of special flags to make this work
        # Try to find the correct python executable
        python_exe = sys.executable

        # Check if we're in a virtual environment and if so, use the venv python
        if hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix:
            venv_python = os.path.join(sys.prefix, "bin", "python")
            if os.path.exists(venv_python):
                python_exe = venv_python

        args = [
            python_exe,
            "-u",
            feedback_ui_path,
            "--project-directory",
            project_directory,
            "--prompt",
            summary,
            "--output-file",
            output_file,
            "--timeout-seconds",
            str(timeout_seconds),
            "--task-id",
            task_id,
        ]
        # Start the subprocess
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            cwd=project_directory,  # Run in project directory
        )

        # 异步非阻塞等待：在等待UI响应期间，事件循环可以处理其他MCP请求
        while proc.poll() is None:
            # Check if output file exists and has content
            if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
                try:
                    with open(output_file, "r") as f:
                        result = json.load(f)
                    _cleanup_process(proc)
                    _cleanup_file(output_file)
                    return result
                except (json.JSONDecodeError, OSError):
                    # File exists but not ready yet, continue waiting
                    pass

            # 关键：使用异步sleep，这里可能抛出 CancelledError
            await asyncio.sleep(0.1)

        # Process completed, read the result
        if proc.returncode != 0:
            raise Exception(f"Failed to launch feedback UI: {proc.returncode}")

        # Read the result from the temporary file
        with open(output_file, "r") as f:
            result = json.load(f)
        _cleanup_file(output_file)
        return result

    except asyncio.CancelledError:
        # MCP 请求被取消（用户在 Cursor 中取消、超时等）
        # 必须清理资源，然后重新抛出让 FastMCP 正确处理
        # 不要返回任何响应，否则会导致 "unknown message ID" 错误
        print(
            "[INFO] Request cancelled, cleaning up subprocess and temp file",
            file=sys.stderr,
        )
        _cleanup_process(proc)
        _cleanup_file(output_file)
        raise  # 重新抛出 CancelledError，让 FastMCP 处理

    except Exception as e:
        _cleanup_process(proc)
        _cleanup_file(output_file)
        raise e


def first_line(text: str) -> str:
    return text.split("\n")[0].strip()


def _process_images(images_data: Any) -> list[MCPImage]:
    if not isinstance(images_data, list):
        return []

    mcp_images: list[MCPImage] = []

    for img in images_data:
        if not isinstance(img, dict):
            continue

        data_b64 = img.get("data")
        if not isinstance(data_b64, str) or not data_b64:
            continue

        try:
            image_bytes = base64.b64decode(data_b64)
        except Exception:
            continue

        if not image_bytes:
            continue

        file_name = img.get("name") if isinstance(img.get("name"), str) else ""
        mime_type = img.get("mime_type") if isinstance(img.get("mime_type"), str) else ""

        image_format = "png"
        if mime_type == "image/jpeg" or file_name.lower().endswith((".jpg", ".jpeg")):
            image_format = "jpeg"
        elif mime_type == "image/gif" or file_name.lower().endswith(".gif"):
            image_format = "gif"
        elif mime_type == "image/webp" or file_name.lower().endswith(".webp"):
            image_format = "webp"

        mcp_images.append(MCPImage(data=image_bytes, format=image_format))

    return mcp_images


@mcp.tool(output_schema=None)
async def interactive_feedback(
    project_directory: Annotated[
        str, Field(description="Full path to the project directory")
    ],
    summary: Annotated[
        str,
        Field(
            description="Brief one-line summary of changes or question to ask the user"
        ),
    ],
    task_id: Annotated[
        str,
        Field(description="Task identifier to distinguish different tasks (required)"),
    ],
) -> list[Any]:
    """Interactive Feedback Tool for MCP (Model Context Protocol)

    This tool enables AI assistants to request real-time feedback from users during coding sessions.
    It opens an interactive feedback window where users can provide input, ask questions, or give directions.

    Parameters:
    - project_directory: Full path to the project directory being worked on
    - summary: Brief one-line summary of changes made, or a specific question to ask the user
    - task_id: Task identifier to distinguish different tasks (required)

    Usage:
    - Use this tool whenever you need user input, clarification, or approval for your work
    - The tool opens a GUI window for user interaction and returns their response
    - Keep calling this tool to maintain continuous dialogue until user says "end conversation"

    Important Rules:
    - Always call this tool after completing any work or response
    - Never end conversation unless user explicitly says "end conversation"
    - Use summary parameter for brief updates or specific questions to the user
    - Use task_id parameter to help users distinguish between different tasks
    - Maintain continuous dialogue by repeatedly calling this tool

    """
    result = await launch_feedback_ui_async(
        first_line(project_directory),
        first_line(summary),
        first_line(task_id),
        AUTO_FEEDBACK_TIMEOUT_SECONDS,
    )

    feedback_items: list[Any] = []

    feedback_text = result.get("interactive_feedback")
    feedback_text_value = (
        feedback_text.strip()
        if isinstance(feedback_text, str) and feedback_text.strip()
        else ""
    )

    images = _process_images(result.get("images"))

    if images:
        # Important: include at least one MCP ContentBlock to avoid FastMCP aggregating
        # the whole list into a single TextContent (which would try to serialize Image).
        text_for_images = feedback_text_value or "User provided images."
        feedback_items.append(TextContent(type="text", text=text_for_images))
        feedback_items.extend(images)
        return feedback_items

    if feedback_text_value:
        return [TextContent(type="text", text=feedback_text_value)]

    return [TextContent(type="text", text="User did not provide any feedback.")]


if __name__ == "__main__":
    mcp.run(transport="stdio")
