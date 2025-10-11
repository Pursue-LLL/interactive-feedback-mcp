# Interactive Feedback MCP
# Developed by Fábio Ferreira (https://x.com/fabiomlferreira)
# Inspired by/related to dotcursorrules.com (https://dotcursorrules.com/)
import os
import sys
import json
import tempfile
import subprocess

from typing import Annotated, Dict

from fastmcp import FastMCP
from pydantic import Field

# The log_level is necessary for Cline to work: https://github.com/jlowin/fastmcp/issues/81
mcp = FastMCP("Interactive Feedback MCP", log_level="ERROR")

# Configuration
AUTO_FEEDBACK_TIMEOUT_SECONDS = int(
    os.getenv("INTERACTIVE_FEEDBACK_TIMEOUT_SECONDS", "290")
)


def launch_feedback_ui(
    project_directory: str, summary: str, task_id: str, timeout_seconds: int = 290
) -> dict[str, str]:
    # Create a temporary file for the feedback result
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_file = tmp.name

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
        result = subprocess.run(
            args,
            check=False,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            cwd=project_directory,  # Run in project directory
        )
        if result.returncode != 0:
            raise Exception(f"Failed to launch feedback UI: {result.returncode}")

        # Read the result from the temporary file
        with open(output_file, "r") as f:
            result = json.load(f)
        os.unlink(output_file)
        return result
    except Exception as e:
        if os.path.exists(output_file):
            os.unlink(output_file)
        raise e


def first_line(text: str) -> str:
    return text.split("\n")[0].strip()


@mcp.tool()
def interactive_feedback(
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
) -> Dict[str, str]:
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
    return launch_feedback_ui(
        first_line(project_directory),
        first_line(summary),
        first_line(task_id),
        AUTO_FEEDBACK_TIMEOUT_SECONDS,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
