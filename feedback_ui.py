# Interactive Feedback MCP UI
# Developed by Fábio Ferreira (https://x.com/fabiomlferreira)
# Inspired by/related to dotcursorrules.com (https://dotcursorrules.com/)
import os
import sys
import json
import psutil
import argparse
import subprocess
import threading
import hashlib
from typing import Optional, TypedDict

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QTextEdit,
    QPlainTextEdit,
    QGroupBox,
    QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer, QSettings
from PySide6.QtGui import (
    QTextCursor,
    QIcon,
    QKeyEvent,
    QFont,
    QFontDatabase,
    QPalette,
    QColor,
)

# Auto feedback timeout configuration (now configurable via command line)

# Global window tracking for positioning multiple windows
_OPEN_WINDOWS: dict[str, "FeedbackUI"] = {}
_WINDOW_OFFSET_STEP = 30  # pixels to offset each new window

# Confirmation instructions for plan review workflow
CONFIRMATION_INSTRUCTIONS = """

请先输出你经过架构师的角度思考后的执行计划和方案，然后调用interactive_feedback工具拉起反馈窗口来询问用户对该方案是否满意，等待用户在反馈窗口输入肯定的指令后再执行该方案，如果用户不同意该方案请思考其他方案后再次请求用户确认。"""


class FeedbackResult(TypedDict):
    interactive_feedback: str


class FeedbackConfig(TypedDict):
    run_command: str
    execute_automatically: bool
    confirm_before_execute: bool


def set_dark_title_bar(widget: QWidget, dark_title_bar: bool) -> None:
    # Ensure we're on Windows
    if sys.platform != "win32":
        return

    from ctypes import windll, c_uint32, byref

    # Get Windows build number
    build_number = sys.getwindowsversion().build
    if build_number < 17763:  # Windows 10 1809 minimum
        return

    # Check if the widget's property already matches the setting
    dark_prop = widget.property("DarkTitleBar")
    if dark_prop is not None and dark_prop == dark_title_bar:
        return

    # Set the property (True if dark_title_bar != 0, False otherwise)
    widget.setProperty("DarkTitleBar", dark_title_bar)

    # Load dwmapi.dll and call DwmSetWindowAttribute
    dwmapi = windll.dwmapi
    hwnd = widget.winId()  # Get the window handle
    attribute = (
        20 if build_number >= 18985 else 19
    )  # Use newer attribute for newer builds
    c_dark_title_bar = c_uint32(dark_title_bar)  # Convert to C-compatible uint32
    dwmapi.DwmSetWindowAttribute(hwnd, attribute, byref(c_dark_title_bar), 4)

    # HACK: Create a 1x1 pixel frameless window to force redraw
    temp_widget = QWidget(None, Qt.FramelessWindowHint)
    temp_widget.resize(1, 1)
    temp_widget.move(widget.pos())
    temp_widget.show()
    temp_widget.deleteLater()  # Safe deletion in Qt event loop


def get_dark_mode_palette(app: QApplication):
    darkPalette = app.palette()
    # Elegant dark theme - minimal and sophisticated
    darkPalette.setColor(QPalette.Window, QColor(18, 18, 18))  # Deep charcoal
    darkPalette.setColor(QPalette.WindowText, QColor(224, 224, 224))  # Soft white
    darkPalette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(128, 128, 128))
    darkPalette.setColor(QPalette.Base, QColor(24, 24, 24))  # Slightly lighter charcoal
    darkPalette.setColor(QPalette.AlternateBase, QColor(32, 32, 32))
    darkPalette.setColor(QPalette.ToolTipBase, QColor(18, 18, 18))
    darkPalette.setColor(QPalette.ToolTipText, QColor(224, 224, 224))
    darkPalette.setColor(QPalette.Text, QColor(224, 224, 224))
    darkPalette.setColor(QPalette.Disabled, QPalette.Text, QColor(128, 128, 128))
    darkPalette.setColor(QPalette.Dark, QColor(45, 45, 45))
    darkPalette.setColor(QPalette.Shadow, QColor(0, 0, 0))
    darkPalette.setColor(QPalette.Button, QColor(32, 32, 32))
    darkPalette.setColor(QPalette.ButtonText, QColor(224, 224, 224))
    darkPalette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(128, 128, 128))
    darkPalette.setColor(QPalette.BrightText, QColor(255, 107, 107))
    darkPalette.setColor(QPalette.Link, QColor(100, 181, 246))
    darkPalette.setColor(QPalette.Highlight, QColor(100, 181, 246))
    darkPalette.setColor(QPalette.Disabled, QPalette.Highlight, QColor(64, 64, 64))
    darkPalette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    darkPalette.setColor(
        QPalette.Disabled, QPalette.HighlightedText, QColor(128, 128, 128)
    )
    darkPalette.setColor(QPalette.PlaceholderText, QColor(128, 128, 128))
    return darkPalette


def kill_tree(process: subprocess.Popen):
    killed: list[psutil.Process] = []
    parent = psutil.Process(process.pid)
    for proc in parent.children(recursive=True):
        try:
            proc.kill()
            killed.append(proc)
        except psutil.Error:
            pass
    try:
        parent.kill()
    except psutil.Error:
        pass
    killed.append(parent)

    # Terminate any remaining processes
    for proc in killed:
        try:
            if proc.is_running():
                proc.terminate()
        except psutil.Error:
            pass


def get_user_environment() -> dict[str, str]:
    if sys.platform != "win32":
        return os.environ.copy()

    import ctypes
    from ctypes import wintypes

    # Load required DLLs
    advapi32 = ctypes.WinDLL("advapi32")
    userenv = ctypes.WinDLL("userenv")
    kernel32 = ctypes.WinDLL("kernel32")

    # Constants
    TOKEN_QUERY = 0x0008

    # Function prototypes
    OpenProcessToken = advapi32.OpenProcessToken
    OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    OpenProcessToken.restype = wintypes.BOOL

    CreateEnvironmentBlock = userenv.CreateEnvironmentBlock
    CreateEnvironmentBlock.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.BOOL,
    ]
    CreateEnvironmentBlock.restype = wintypes.BOOL

    DestroyEnvironmentBlock = userenv.DestroyEnvironmentBlock
    DestroyEnvironmentBlock.argtypes = [wintypes.LPVOID]
    DestroyEnvironmentBlock.restype = wintypes.BOOL

    GetCurrentProcess = kernel32.GetCurrentProcess
    GetCurrentProcess.argtypes = []
    GetCurrentProcess.restype = wintypes.HANDLE

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    # Get process token
    token = wintypes.HANDLE()
    if not OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise RuntimeError("Failed to open process token")

    try:
        # Create environment block
        environment = ctypes.c_void_p()
        if not CreateEnvironmentBlock(ctypes.byref(environment), token, False):
            raise RuntimeError("Failed to create environment block")

        try:
            # Convert environment block to list of strings
            result = {}
            env_ptr = ctypes.cast(environment, ctypes.POINTER(ctypes.c_wchar))
            offset = 0

            while True:
                # Get string at current offset
                current_string = ""
                while env_ptr[offset] != "\0":
                    current_string += env_ptr[offset]
                    offset += 1

                # Skip null terminator
                offset += 1

                # Break if we hit double null terminator
                if not current_string:
                    break

                equal_index = current_string.index("=")
                if equal_index == -1:
                    continue

                key = current_string[:equal_index]
                value = current_string[equal_index + 1 :]
                result[key] = value

            return result

        finally:
            DestroyEnvironmentBlock(environment)

    finally:
        CloseHandle(token)


class FeedbackTextEdit(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Return and event.modifiers() == Qt.ControlModifier:
            # Find the parent FeedbackUI instance and trigger submit click
            parent = self.parent()
            while parent and not isinstance(parent, FeedbackUI):
                parent = parent.parent()
            if parent:
                # Use _on_submit_clicked to respect multi-window confirmation
                parent._on_submit_clicked()
        else:
            super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        # Override to strip formatting when pasting
        if source.hasText():
            # Insert only plain text, stripping all formatting
            plain_text = source.text()
            self.insertPlainText(plain_text)
        else:
            # For non-text data, use default behavior
            super().insertFromMimeData(source)


class LogSignals(QObject):
    append_log = Signal(str)


class FeedbackSignals(QObject):
    feedback_ready = Signal(dict)  # Emits FeedbackResult when window is done


class FeedbackUI(QMainWindow):
    def __init__(
        self,
        project_directory: str,
        prompt: str,
        task_id: str,
        timeout_seconds: int = 290,
    ):
        # Check if window with this task_id already exists
        if task_id in _OPEN_WINDOWS:
            raise ValueError(f"Window with task_id '{task_id}' already exists")

        super().__init__()
        self.project_directory = project_directory
        self.prompt = prompt
        self.timeout_seconds = timeout_seconds
        self.task_id = task_id

        # Register this window globally
        _OPEN_WINDOWS[task_id] = self

        self.process: Optional[subprocess.Popen] = None
        self.log_buffer: list[str] = []
        self.feedback_result: FeedbackResult | None = None
        self.log_signals = LogSignals()
        self.log_signals.append_log.connect(self._append_log)
        self.feedback_signals = FeedbackSignals()

        # Multi-window confirmation state
        self._pending_confirm = False  # Whether waiting for second click to confirm

        # Auto feedback timer
        self.auto_feedback_timer = QTimer()
        self.auto_feedback_timer.setSingleShot(True)
        self.auto_feedback_timer.timeout.connect(self._auto_submit_feedback)

        # Countdown display timer (updates every second)
        self.countdown_timer = QTimer()
        self.countdown_timer.timeout.connect(self._update_countdown_display)
        self.remaining_seconds = self.timeout_seconds

        # Set window title with project name for identification
        project_name = os.path.basename(os.path.normpath(self.project_directory))
        self.setWindowTitle(f"Interactive Feedback MCP - {project_name}")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "images", "feedback.png")
        self.setWindowIcon(QIcon(icon_path))
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        self.settings = QSettings("InteractiveFeedbackMCP", "InteractiveFeedbackMCP")

        # Load general UI settings for the main window (geometry, state)
        self.settings.beginGroup("MainWindow_General")
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            # Calculate offset position for multiple windows
            screen = QApplication.primaryScreen().geometry()
            base_x = (screen.width() - 800) // 2
            base_y = (screen.height() - 600) // 2

            # Count windows before this one to calculate offset
            window_count = len([w for w in _OPEN_WINDOWS.values() if w != self])
            offset = window_count * _WINDOW_OFFSET_STEP

            x = base_x + offset
            y = base_y + offset

            # Ensure window stays within screen bounds
            x = max(0, min(x, screen.width() - 800))
            y = max(0, min(y, screen.height() - 600))

            self.resize(800, 600)
            self.move(x, y)
        state = self.settings.value("windowState")
        if state:
            self.restoreState(state)
        self.settings.endGroup()  # End "MainWindow_General" group

        # Load task-specific settings (command, auto-execute, command section visibility, confirm before execute)
        # Use task_id for primary grouping, with project as fallback for compatibility
        self.task_group_name = f"Task_{self.task_id}"
        self.project_group_name = get_project_settings_group(self.project_directory)

        # Try task-specific settings first, fallback to project settings
        self.settings.beginGroup(self.task_group_name)
        loaded_run_command = self.settings.value("run_command", "", type=str)
        loaded_execute_auto = self.settings.value(
            "execute_automatically", False, type=bool
        )
        loaded_confirm_before_execute = self.settings.value(
            "confirm_before_execute", False, type=bool
        )
        command_section_visible = self.settings.value(
            "commandSectionVisible", False, type=bool
        )
        self.settings.endGroup()

        # If no task-specific settings found, try project settings for backward compatibility
        if (
            not loaded_run_command
            and not loaded_execute_auto
            and not loaded_confirm_before_execute
            and not command_section_visible
        ):
            self.settings.beginGroup(self.project_group_name)
            loaded_run_command = self.settings.value("run_command", "", type=str)
            loaded_execute_auto = self.settings.value(
                "execute_automatically", False, type=bool
            )
            loaded_confirm_before_execute = self.settings.value(
                "confirm_before_execute", False, type=bool
            )
            command_section_visible = self.settings.value(
                "commandSectionVisible", False, type=bool
            )
            self.settings.endGroup()

        self.config: FeedbackConfig = {
            "run_command": loaded_run_command or "",
            "execute_automatically": loaded_execute_auto or False,
            "confirm_before_execute": loaded_confirm_before_execute or False,
        }

        self._create_ui()  # self.config is used here to set initial values

        # Command section is permanently hidden
        self.command_group.setVisible(False)

        set_dark_title_bar(self, True)

        if self.config.get("execute_automatically", False):
            self._run_command()

    def _update_countdown_display(self):
        """Update the countdown display every second"""
        self.remaining_seconds -= 1
        if self.remaining_seconds > 0:
            self.countdown_label.setText(f"⏰ {self.remaining_seconds}秒后即将重新询问")
        else:
            self.countdown_label.setText("⏰ 重新询问中...")
            self.countdown_timer.stop()

    def _adjust_description_height(self):
        """Adjust description label height based on content length"""
        if not hasattr(self, "description_label") or not self.description_label:
            return

        # Get text content
        text = self.description_label.toPlainText()
        if not text.strip():
            self.description_label.setFixedHeight(80)
            return

        # Calculate lines (rough estimation)
        font_metrics = self.description_label.fontMetrics()
        line_height = font_metrics.lineSpacing()

        # Use estimated width since actual width may not be available during initialization
        # Assume typical width for the widget area (accounting for margins and other UI elements)
        estimated_width = 700  # Rough estimate based on window width minus margins
        text_width = estimated_width - 40  # Account for padding and margins

        # Count actual lines by splitting and measuring
        lines = text.split("\n")
        total_lines = 0
        for line in lines:
            if not line.strip():
                total_lines += 1
            else:
                # Estimate wrapped lines
                line_width = font_metrics.horizontalAdvance(line)
                if line_width <= text_width:
                    total_lines += 1
                else:
                    wrapped_lines = (line_width // text_width) + 1
                    total_lines += wrapped_lines

        # Calculate target height with padding
        content_height = (
            total_lines * line_height + 24
        )  # Add padding for top/bottom margins
        target_height = min(200, max(80, content_height))

        self.description_label.setFixedHeight(target_height)

    def _auto_submit_feedback(self):
        """Auto submit feedback with default message after timeout"""
        user_input = self.feedback_text.toPlainText().strip()
        if user_input:
            # Backup user input to console log
            self._append_log(f"[备份] 用户输入内容: {user_input}\n")
            # Copy user input to system clipboard
            clipboard = QApplication.clipboard()
            clipboard.setText(user_input)
            self._append_log("[信息] 用户输入已复制到剪切板\n")
        # Always submit default feedback after timeout
        self.feedback_text.setPlainText(
            "好的，代码先保持现状，然后重新调用 interactive_feedback 工具，调用interactive_feedback后会有一个窗口，我将在窗口中告诉你下一步指令，请先调用interactive_feedback工具"
        )
        self._submit_feedback()

    def _format_windows_path(self, path: str) -> str:
        if sys.platform == "win32":
            # Convert forward slashes to backslashes
            path = path.replace("/", "\\")
            # Capitalize drive letter if path starts with x:\
            if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
                path = path[0].upper() + path[1:]
        return path

    def _create_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Set main background - elegant dark
        central_widget.setStyleSheet("""
            QWidget {
                background-color: #121212;
            }
        """)

        layout = QVBoxLayout(central_widget)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        # Command section - Elegant dark design
        self.command_group = QGroupBox("🖥️ 命令控制台")
        self.command_group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: 600;
                border: 1px solid #404040;
                border-radius: 12px;
                margin-top: 8px;
                padding-top: 16px;
                background-color: #1e1e1e;
            }
            QGroupBox::title {
                color: #c0c0c0;
                font-size: 13px;
                font-weight: 600;
                padding: 2px 8px;
                margin-top: -10px;
                margin-left: 16px;
            }
        """)
        command_layout = QVBoxLayout(self.command_group)
        command_layout.setContentsMargins(20, 20, 20, 20)
        command_layout.setSpacing(12)

        # Working directory label - Simple dark style
        formatted_path = self._format_windows_path(self.project_directory)
        working_dir_label = QLabel(f"📁 工作目录: {formatted_path}")
        working_dir_label.setStyleSheet("""
            QLabel {
                color: #a0a0a0;
                font-size: 12px;
                padding: 8px 12px;
                background-color: #252525;
                border-radius: 6px;
            }
        """)
        command_layout.addWidget(working_dir_label)

        # Command input row - Clean dark design
        command_input_layout = QHBoxLayout()
        command_input_layout.setSpacing(12)

        self.command_entry = QLineEdit()
        self.command_entry.setText(self.config["run_command"])
        self.command_entry.setStyleSheet("""
            QLineEdit {
                background-color: #252525;
                color: #e0e0e0;
                border: 1px solid #404040;
                border-radius: 6px;
                padding: 10px 14px;
                font-size: 13px;
            }
            QLineEdit:focus {
                border: 1px solid #606060;
                background-color: #2a2a2a;
            }
            QLineEdit::placeholder {
                color: #808080;
            }
        """)
        self.command_entry.returnPressed.connect(self._run_command)
        self.command_entry.textChanged.connect(self._update_config)

        self.run_button = QPushButton("▶️ 运行")
        self.run_button.setStyleSheet("""
            QPushButton {
                background-color: #007bff;
                color: #ffffff;
                border: 1px solid #007bff;
                border-radius: 6px;
                padding: 10px 20px;
                font-size: 13px;
                font-weight: 500;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #0056b3;
                border-color: #0056b3;
            }
            QPushButton:pressed {
                background-color: #004085;
            }
        """)
        self.run_button.clicked.connect(self._run_command)

        command_input_layout.addWidget(self.command_entry)
        command_input_layout.addWidget(self.run_button)
        command_layout.addLayout(command_input_layout)

        # Auto-execute and save config row - Simple dark style
        auto_layout = QHBoxLayout()
        auto_layout.setSpacing(12)

        self.auto_check = QCheckBox("🔄 下次运行时自动执行")
        self.auto_check.setStyleSheet("""
            QCheckBox {
                color: #a0a0a0;
                font-size: 12px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #606060;
                border-radius: 3px;
                background-color: #252525;
            }
            QCheckBox::indicator:checked {
                background-color: #007bff;
                border: 1px solid #007bff;
            }
            QCheckBox::indicator:hover {
                border: 1px solid #007bff;
            }
        """)

        save_button = QPushButton("💾 保存配置")
        save_button.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: #ffffff;
                border: 1px solid #28a745;
                border-radius: 6px;
                padding: 8px 16px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #218838;
                border-color: #218838;
            }
            QPushButton:pressed {
                background-color: #1e7e34;
            }
        """)
        save_button.clicked.connect(self._save_config)

        auto_layout.addWidget(self.auto_check)
        auto_layout.addStretch()
        auto_layout.addWidget(save_button)
        command_layout.addLayout(auto_layout)

        # Console section (now part of command_group) - Dark terminal style
        console_group = QGroupBox("📜 控制台输出")
        console_group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: 600;
                border: 1px solid #404040;
                border-radius: 8px;
                margin-top: 8px;
                padding-top: 16px;
                background-color: #1e1e1e;
            }
            QGroupBox::title {
                color: #c0c0c0;
                font-size: 13px;
                font-weight: 600;
                padding: 2px 8px;
                margin-top: -10px;
                margin-left: 16px;
            }
        """)
        console_group.setMinimumHeight(200)
        console_layout_internal = QVBoxLayout(console_group)
        console_layout_internal.setContentsMargins(16, 16, 16, 16)

        # Log text area - Light terminal style
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        font = QFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        font.setPointSize(10)
        self.log_text.setFont(font)
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #0a0a0a;
                color: #c0c0c0;
                border: 1px solid #333333;
                border-radius: 4px;
                padding: 8px;
                font-family: monospace;
                selection-background-color: #404040;
            }
        """)
        console_layout_internal.addWidget(self.log_text)

        # Clear button - Simple dark style
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 8, 0, 0)

        self.clear_button = QPushButton("🗑️ 清空控制台")
        self.clear_button.setStyleSheet("""
            QPushButton {
                background-color: #dc3545;
                color: #ffffff;
                border: 1px solid #dc3545;
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #c82333;
                border-color: #c82333;
            }
            QPushButton:pressed {
                background-color: #bd2130;
            }
        """)
        self.clear_button.clicked.connect(self.clear_logs)
        button_layout.addStretch()
        button_layout.addWidget(self.clear_button)
        console_layout_internal.addLayout(button_layout)

        command_layout.addWidget(console_group)

        self.command_group.setVisible(False)
        layout.addWidget(self.command_group)

        # Feedback section - Light design
        self.feedback_group = QGroupBox("💬 用户反馈")
        self.feedback_group.setStyleSheet("""
            QGroupBox {
                font-size: 16px;
                font-weight: 700;
                border: 2px solid #404040;
                border-radius: 12px;
                margin-top: 8px;
                padding-top: 20px;
                background-color: #1a1a1a;
            }
            QGroupBox::title {
                color: #e0e0e0;
                font-size: 14px;
                font-weight: 700;
                padding: 2px 8px;
                margin-top: -12px;
                margin-left: 20px;
            }
        """)
        feedback_layout = QVBoxLayout(self.feedback_group)
        feedback_layout.setContentsMargins(24, 24, 24, 24)
        feedback_layout.setSpacing(16)

        # Header section - Compact layout
        header_layout = QHBoxLayout()

        # Project identification label - Compact style
        project_path = os.path.abspath(self.project_directory)
        project_name = os.path.basename(project_path)
        # 如果是当前目录且basename返回"."，则使用绝对路径的basename
        if project_name == "." or project_name == "":
            project_name = os.path.basename(os.path.dirname(project_path))
        project_path_label = QLabel(f"🎯 {project_name}")
        project_path_label.setStyleSheet("""
            QLabel {
                color: #e0e0e0;
                font-size: 14px;
                font-weight: 600;
                padding: 4px 8px;
                background-color: #404040;
                border-radius: 4px;
            }
        """)
        header_layout.addWidget(project_path_label)

        # Task ID label - Only show if task_id is provided
        if self.task_id:
            task_id_label = QLabel(f"📋 {self.task_id}")
            task_id_label.setStyleSheet("""
                QLabel {
                    color: #a0d8ff;
                    font-size: 12px;
                    font-weight: 500;
                    padding: 4px 8px;
                    background-color: #2a4a6b;
                    border-radius: 4px;
                }
            """)
            header_layout.addWidget(task_id_label)

        header_layout.addStretch()

        # Countdown display label - Compact style
        self.countdown_label = QLabel(f"⏰ {self.timeout_seconds}秒")
        self.countdown_label.setStyleSheet("""
            QLabel {
                color: #ff6b6b;
                font-size: 12px;
                padding: 4px 8px;
                background-color: #404040;
                border-radius: 4px;
            }
        """)
        header_layout.addWidget(self.countdown_label)

        feedback_layout.addLayout(header_layout)

        # Short description text edit - Simple styling
        self.description_label = QPlainTextEdit(self.prompt)
        self.description_label.setReadOnly(True)
        # Set fixed height for short content, expandable for longer content
        self._adjust_description_height()
        self.description_label.setStyleSheet("""
            QPlainTextEdit {
                color: #c0c0c0;
                font-size: 13px;
                line-height: 1.4;
                padding: 8px 12px;
                background-color: #404040;
                border-radius: 6px;
                border: none;
                selection-background-color: #606060;
            }
            QPlainTextEdit:focus {
                border: 1px solid #606060;
            }
        """)
        feedback_layout.addWidget(self.description_label)

        # Feedback text input - Clean dark design with more space
        self.feedback_text = FeedbackTextEdit()
        font_metrics = self.feedback_text.fontMetrics()
        row_height = font_metrics.height()
        # Calculate height for 3 lines to give compact input area
        padding = (
            self.feedback_text.contentsMargins().top()
            + self.feedback_text.contentsMargins().bottom()
            + 10
        )
        self.feedback_text.setMinimumHeight(3 * row_height + padding)
        self.feedback_text.setStyleSheet("""
            QTextEdit {
                background-color: #252525;
                color: #e0e0e0;
                border: 1px solid #404040;
                border-radius: 8px;
                padding: 12px;
                font-size: 14px;
                line-height: 1.6;
                selection-background-color: #404040;
            }
            QTextEdit:focus {
                border: 1px solid #606060;
                background-color: #2a2a2a;
            }
            QTextEdit::placeholder {
                color: #808080;
                font-style: italic;
            }
        """)

        self.feedback_text.setPlaceholderText(
            "请在此输入您的反馈和指示... (按 Ctrl+Enter 发送)"
        )

        # Submit button - Clean dark style
        self.submit_button = QPushButton("🚀 发送反馈 (Ctrl+Enter)")
        self._submit_button_default_style = """
            QPushButton {
                background-color: #2a2a2a;
                color: #e0e0e0;
                border: 1px solid #404040;
                border-radius: 8px;
                padding: 12px 20px;
                font-size: 14px;
                font-weight: 600;
                min-height: 20px;
            }
            QPushButton:hover {
                background-color: #333333;
                border-color: #555555;
            }
            QPushButton:pressed {
                background-color: #1a1a1a;
            }
            QPushButton:focus {
                border: 1px solid #606060;
            }
        """
        self._submit_button_confirm_style = """
            QPushButton {
                background-color: #28a745;
                color: #ffffff;
                border: 2px solid #28a745;
                border-radius: 8px;
                padding: 12px 20px;
                font-size: 14px;
                font-weight: 700;
                min-height: 20px;
            }
            QPushButton:hover {
                background-color: #218838;
                border-color: #218838;
            }
            QPushButton:pressed {
                background-color: #1e7e34;
            }
            QPushButton:focus {
                border: 2px solid #1e7e34;
            }
        """
        self.submit_button.setStyleSheet(self._submit_button_default_style)
        self.submit_button.clicked.connect(self._on_submit_clicked)

        # Confirmation checkbox - Clean dark style
        self.confirm_before_execute_check = QCheckBox("🔍 需要先确认方案后再执行")
        self.confirm_before_execute_check.setStyleSheet("""
            QCheckBox {
                color: #a0a0a0;
                font-size: 12px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #606060;
                border-radius: 3px;
                background-color: #252525;
            }
            QCheckBox::indicator:checked {
                background-color: #007bff;
                border: 1px solid #007bff;
            }
            QCheckBox::indicator:hover {
                border: 1px solid #007bff;
            }
        """)

        feedback_layout.addWidget(self.feedback_text)
        feedback_layout.addWidget(self.confirm_before_execute_check)
        feedback_layout.addWidget(self.submit_button)

        # Set minimum height for feedback_group to accommodate its contents
        # This will be based on the description label and the expanded feedback_text
        self.feedback_group.setMinimumHeight(
            self.description_label.sizeHint().height()
            + self.feedback_text.minimumHeight()
            + self.submit_button.sizeHint().height()
            + feedback_layout.spacing() * 3  # More spacing for header layout
            + feedback_layout.contentsMargins().top()
            + feedback_layout.contentsMargins().bottom()
            + 10
        )  # 10 for extra padding

        # Add widgets in a specific order
        layout.addWidget(self.feedback_group)

        self.command_group.setVisible(False)

        # Set initial states for checkboxes after all UI elements are created
        self.auto_check.setChecked(self.config.get("execute_automatically", False))
        self.confirm_before_execute_check.setChecked(
            self.config.get("confirm_before_execute", False)
        )
        self.auto_check.stateChanged.connect(self._update_config)
        self.confirm_before_execute_check.stateChanged.connect(self._update_config)

    def _update_config(self):
        self.config["run_command"] = self.command_entry.text()
        self.config["execute_automatically"] = self.auto_check.isChecked()
        self.config["confirm_before_execute"] = (
            self.confirm_before_execute_check.isChecked()
        )

    def _append_log(self, text: str):
        self.log_buffer.append(text)
        self.log_text.append(text.rstrip())
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_text.setTextCursor(cursor)

    def _check_process_status(self):
        if self.process and self.process.poll() is not None:
            # Process has terminated
            exit_code = self.process.poll()
            self._append_log(f"\nProcess exited with code {exit_code}\n")
            self.run_button.setText("&Run")
            self.process = None
            self.activateWindow()
            self.feedback_text.setFocus()

    def _run_command(self):
        if self.process:
            kill_tree(self.process)
            self.process = None
            self.run_button.setText("&Run")
            return

        # Clear the log buffer but keep UI logs visible
        self.log_buffer = []

        command = self.command_entry.text()
        if not command:
            self._append_log("请输入要运行的命令\n")
            return

        self._append_log(f"$ {command}\n")
        self.run_button.setText("Sto&p")

        try:
            self.process = subprocess.Popen(
                command,
                shell=True,
                cwd=self.project_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=get_user_environment(),
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="ignore",
                close_fds=True,
            )

            def read_output(pipe):
                for line in iter(pipe.readline, ""):
                    self.log_signals.append_log.emit(line)

            threading.Thread(
                target=read_output, args=(self.process.stdout,), daemon=True
            ).start()

            threading.Thread(
                target=read_output, args=(self.process.stderr,), daemon=True
            ).start()

            # Start process status checking
            self.status_timer = QTimer()
            self.status_timer.timeout.connect(self._check_process_status)
            self.status_timer.start(100)  # Check every 100ms

        except Exception as e:
            self._append_log(f"Error running command: {str(e)}\n")
            self.run_button.setText("&Run")

    def _get_project_display_name(self) -> str:
        """Get a short display name for the project"""
        project_path = os.path.abspath(self.project_directory)
        project_name = os.path.basename(project_path)
        if project_name == "." or project_name == "":
            project_name = os.path.basename(os.path.dirname(project_path))
        return project_name

    def _has_multiple_windows(self) -> bool:
        """Check if there are multiple feedback windows open"""
        return len(_OPEN_WINDOWS) > 1

    def _on_submit_clicked(self):
        """Handle submit button click with multi-window confirmation"""
        if self._has_multiple_windows() and not self._pending_confirm:
            # First click: enter confirmation mode
            self._enter_confirm_mode()
        else:
            # Single window or already confirmed: submit directly
            self._submit_feedback()

    def _enter_confirm_mode(self):
        """Enter confirmation mode - change button to show project/task info"""
        self._pending_confirm = True
        project_name = self._get_project_display_name()
        task_display = self.task_id if self.task_id else "未知任务"

        # Update button text and style
        self.submit_button.setText(f"✅ 确认发送到 [{project_name} - {task_display}]")
        self.submit_button.setStyleSheet(self._submit_button_confirm_style)

        # Set a timer to reset confirmation state after 5 seconds if no action
        self._confirm_reset_timer = QTimer()
        self._confirm_reset_timer.setSingleShot(True)
        self._confirm_reset_timer.timeout.connect(self._reset_confirm_mode)
        self._confirm_reset_timer.start(5000)  # 5 seconds to confirm

    def _reset_confirm_mode(self):
        """Reset from confirmation mode back to normal"""
        self._pending_confirm = False
        self.submit_button.setText("🚀 发送反馈 (Ctrl+Enter)")
        self.submit_button.setStyleSheet(self._submit_button_default_style)
        if (
            hasattr(self, "_confirm_reset_timer")
            and self._confirm_reset_timer.isActive()
        ):
            self._confirm_reset_timer.stop()

    def _submit_feedback(self):
        # Reset confirmation mode if active
        if self._pending_confirm:
            self._reset_confirm_mode()

        # Stop both timers when submitting
        if self.auto_feedback_timer.isActive():
            self.auto_feedback_timer.stop()
        if self.countdown_timer.isActive():
            self.countdown_timer.stop()

        user_input = self.feedback_text.toPlainText().strip()

        # Add additional instruction text to user input
        additional_text = "\n 请回答以上问题或要求。\n并且注意，除非我主动让你结束，否则不要结束对话，必须回复。回复或反思优化步骤完成后必须重新调用interactive_feedback工具拉起反馈窗口来询问我的反馈，因为反馈窗口仅对当前轮次对话有效，旧的窗口已无效，所以每一轮新对话必须重新调用，只有这样才能保证会话连续性，达到完成多轮对话的任务目的！"
        user_input_with_suffix = user_input + additional_text

        # Check if confirm_before_execute is enabled
        if self.confirm_before_execute_check.isChecked():
            # Add confirmation instructions to user input
            modified_input = user_input_with_suffix + CONFIRMATION_INSTRUCTIONS

            # Set the modified input as the feedback result
            self.feedback_result = FeedbackResult(interactive_feedback=modified_input)
        else:
            # Normal submission without confirmation
            self.feedback_result = FeedbackResult(
                interactive_feedback=user_input_with_suffix
            )

        # Emit signal before closing
        self.feedback_signals.feedback_ready.emit(self.feedback_result)

        self.close()

    def clear_logs(self):
        self.log_buffer = []
        self.log_text.clear()

    def _save_config(self):
        # Save run_command, execute_automatically, and confirm_before_execute to QSettings under task group
        self.settings.beginGroup(self.task_group_name)
        self.settings.setValue("run_command", self.config["run_command"])
        self.settings.setValue(
            "execute_automatically", self.config["execute_automatically"]
        )
        self.settings.setValue(
            "confirm_before_execute", self.config["confirm_before_execute"]
        )
        self.settings.endGroup()
        self._append_log(f"Configuration saved for task '{self.task_id}'.\n")

    def closeEvent(self, event):
        # Stop both timers when closing
        if self.auto_feedback_timer.isActive():
            self.auto_feedback_timer.stop()
        if self.countdown_timer.isActive():
            self.countdown_timer.stop()

        # 当用户主动关闭窗口时，设置反馈结果为"会话可以结束了"
        if not self.feedback_result:
            self.feedback_result = FeedbackResult(interactive_feedback="请结束会话！")

        # Emit signal when window is closed (if not already emitted)
        if self.feedback_result:
            self.feedback_signals.feedback_ready.emit(self.feedback_result)

        # Save general UI settings for the main window (geometry, state)
        self.settings.beginGroup("MainWindow_General")
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        self.settings.endGroup()

        # Save task-specific command section visibility (this is now slightly redundant due to immediate save in toggle, but harmless)
        self.settings.beginGroup(self.task_group_name)
        self.settings.setValue("commandSectionVisible", self.command_group.isVisible())
        self.settings.endGroup()

        # Remove this window from global tracking
        if self.task_id in _OPEN_WINDOWS:
            del _OPEN_WINDOWS[self.task_id]

        if self.process:
            kill_tree(self.process)
        super().closeEvent(event)

    def run(self) -> None:
        """Show the window and start timers. Results are emitted via signals."""
        self.show()
        # Adjust description height after window is shown and has proper dimensions
        QTimer.singleShot(100, self._adjust_description_height)
        # Start both timers after showing the window
        self.auto_feedback_timer.start(
            self.timeout_seconds * 1000
        )  # Convert to milliseconds
        self.countdown_timer.start(1000)  # Update every second


def get_project_settings_group(project_dir: str) -> str:
    # Create a safe, unique group name from the project directory path
    # Using only the last component + hash of full path to keep it somewhat readable but unique
    basename = os.path.basename(os.path.normpath(project_dir))
    full_hash = hashlib.md5(project_dir.encode("utf-8")).hexdigest()[:8]
    return f"{basename}_{full_hash}"


def feedback_ui(
    project_directory: str,
    prompt: str,
    task_id: str,
    output_file: Optional[str] = None,
    timeout_seconds: int = 290,
) -> tuple[Optional[FeedbackResult], str]:
    app = QApplication.instance() or QApplication()
    app.setPalette(get_dark_mode_palette(app))
    app.setStyle("Fusion")

    result = None

    def on_feedback_ready(feedback_result):
        nonlocal result
        result = feedback_result

        if output_file:
            # Ensure the directory exists
            os.makedirs(
                os.path.dirname(output_file) if os.path.dirname(output_file) else ".",
                exist_ok=True,
            )
            # Save the result to the output file
            with open(output_file, "w") as f:
                json.dump(result, f)

        # Quit the application when result is ready
        app.quit()

    ui = FeedbackUI(project_directory, prompt, task_id, timeout_seconds)
    ui.feedback_signals.feedback_ready.connect(on_feedback_ready)
    ui.run()

    # Run the event loop - this is required for Qt to work
    app.exec()

    logs = "".join(ui.log_buffer)
    return result, logs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the feedback UI")
    parser.add_argument(
        "--project-directory",
        default=os.getcwd(),
        help="The project directory to run the command in",
    )
    parser.add_argument(
        "--prompt",
        default="I implemented the changes you requested.",
        help="The prompt to show to the user",
    )
    parser.add_argument(
        "--output-file", help="Path to save the feedback result as JSON"
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=290,
        help="Timeout in seconds for auto-feedback (default: 290)",
    )
    parser.add_argument(
        "--task-id",
        required=True,
        help="Task identifier to distinguish different tasks (required)",
    )
    args = parser.parse_args()

    result, logs = feedback_ui(
        args.project_directory,
        args.prompt,
        args.task_id,
        args.output_file,
        args.timeout_seconds,
    )
    if logs:
        print(f"\nLogs collected: \n{logs}")
    if result:
        print(f"\nFeedback received:\n{result['interactive_feedback']}")
    sys.exit(0)
