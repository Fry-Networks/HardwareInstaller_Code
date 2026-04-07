"""
FryNetworks Installer GUI Window

Main graphical interface for the FryNetworks miner installer with:
- Corporate branding and theme
- Automatic miner type detection
- Real-time conflict checking
- Installation progress tracking (concise, non-redundant output)
"""

import sys
import os
import subprocess
import html
import time
import threading
import json
import logging
from pathlib import Path
from typing import Any, Dict, cast, Optional, List, Set, Callable

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    print("PySide6 not available - GUI mode disabled")
    sys.exit(1)

# Import installer components
from core.key_parser import MinerKeyParser
from core.conflict_detector import ConflictDetector
from core.service_manager import ServiceManager
from core.config_manager import ConfigManager
from core import naming

# Import FryNetworks branding
from tools.theme import Theme
from tools.banner import TopBanner

# Import external API client from tools package
from tools.external_api import get_external_api_client, ExternalApiClient, _BUILD_CONFIG
from version import __version__ as version_str


class _WelcomeDataWorker(QtCore.QThread):
    """Fetches installer availability data from the API in a background thread."""
    finished = QtCore.Signal(object)

    def __init__(self, api_base_url: str, api_token, use_test: bool, parent=None):
        super().__init__(parent)
        self._api_base_url = api_base_url
        self._api_token = api_token
        self._use_test = use_test

    def run(self):
        slog = logging.getLogger("startup")
        slog.info("_WelcomeDataWorker: thread started")
        t0 = time.monotonic()
        result = {
            'supported_windows': {}, 'supported_linux': {},
            'test_windows_set': set(), 'test_linux_set': set(),
        }
        try:
            client = ExternalApiClient(self._api_base_url, token=self._api_token, timeout=5.0)
            client._RETRY_DELAYS = []

            slog.info("_WelcomeDataWorker: fetching windows installers")
            result['supported_windows'] = client.get_supported_installers('windows', use_test=False) or {}
            slog.info("_WelcomeDataWorker: fetching linux installers")
            result['supported_linux'] = client.get_supported_installers('linux', use_test=False) or {}

            if self._use_test:
                slog.info("_WelcomeDataWorker: fetching test installers")
                tw = client.get_supported_installers('windows', use_test=True) or {}
                tl = client.get_supported_installers('linux', use_test=True) or {}
                result['test_windows_set'] = set(str(x).upper() for x in tw.get('miner_codes', []) if x)
                result['test_linux_set'] = set(str(x).upper() for x in tl.get('miner_codes', []) if x)

            slog.info(f"_WelcomeDataWorker: completed in {time.monotonic() - t0:.3f}s")
            self.finished.emit(result)
        except Exception as e:
            slog.warning(f"_WelcomeDataWorker: failed after {time.monotonic() - t0:.3f}s — {e}")
            self.finished.emit(None)


class FryNetworksInstallerWindow(QtWidgets.QMainWindow):
    """Main installer window with FryNetworks branding."""
    # Signals for thread-safe UI invocation from worker threads
    _invoke_log = QtCore.Signal(str)
    _invoke_update = QtCore.Signal(int, str)
    _invoke_step6_update = QtCore.Signal(int, str)
    _invoke_step6_log = QtCore.Signal(str)
    _invoke_log_update = QtCore.Signal(str)  # For updating log lines in place (checkmarks)
    _invoke_installation_completed = QtCore.Signal(dict)
    _invoke_installation_failed = QtCore.Signal(str, list)
    _invoke_validation_done = QtCore.Signal(dict)  # Validation result from worker thread
    
    def __init__(self):
        super().__init__()
        self._slog = logging.getLogger("startup")
        self._slog.info("FryNetworksInstallerWindow.__init__() started")

        # ---- Concise logging mode (ON by default) ----
        # When True, the UI log shows ONLY a short, structured sequence of steps
        # with checkmarks and a final summary. No "planned steps", no streaming
        # of inner debug logs from ServiceManager or external tools.
        self.concise_log: bool = True
        
        # Initialize debug log FIRST before any logging calls
        try:
            # Preferred: per-user app-local path so bundled exe writes to a persistent location
            import os, tempfile

            local_app = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
            if local_app:
                p = Path(local_app) / "FryNetworks" / "install_debug.log"
            else:
                # Fallback to system temp dir
                p = Path(tempfile.gettempdir()) / "FryNetworks" / "install_debug.log"

            # When running from source, also keep the repo-root location as a secondary option
            try:
                repo_root_log = Path(__file__).parent.parent / "install_debug.log"
            except Exception:
                repo_root_log = None

            # Prefer local_app path (persistent); ensure parent dir exists
            p.parent.mkdir(parents=True, exist_ok=True)
            self._debug_log_path = p
            # Also expose repo_root_log for convenience if running from source
            self._repo_debug_log = repo_root_log
        except Exception:
            self._debug_log_path = None
            self._repo_debug_log = None
        
        # Load custom API configuration from .env if present
        self._load_api_config()

        # Initialize API client — pass the env var explicitly since _BUILD_CONFIG
        # was cached at import time (before .env was loaded)
        api_url = os.environ.get('EXTERNAL_API_BASE_URL') or None
        self.api_client = get_external_api_client(base_url=api_url, use_optimized=False)
        self._partner_secret_flags = self._detect_partner_secret_availability()
        
        # Check for test mode configuration (.env file next to installer.exe)
        self._use_test_versions = self._load_test_mode_config()
        
        # Initialize components
        self.parser = MinerKeyParser()
        self.detector = ConflictDetector(api_client=self.api_client, use_test=self._use_test_versions)
        self.current_miner_info = None
        self.installation_thread = None
        self._validation_thread = None  # Background thread for key validation
        # Cancellation flag for in-progress installation
        self._cancel_requested = False
        self.is_key_validated = False  # Track if key has been validated
        self._last_conflicts = None  # Cached conflict result from last validation
        # Track when an installation has completed so Finish button closes instead of reinstalling
        self._post_install_mode: bool = False
        self._last_install_ctx: Optional[Dict[str, Any]] = None

        # Track whether the progress log has been seeded for the current run (kept for safety)
        self._progress_seeded = False
        # Remember last main progress value to avoid regressions from async callbacks
        self._last_progress_value = 0

        # Tray integration
        self._tray_icon: Optional[QtWidgets.QSystemTrayIcon] = None
        self._allow_close = False
        self._tray_message_shown = False
        self._reset_on_restore = False
        # Remember the status text before entering the manage panel so Back can restore it
        self._status_before_manage: Optional[str] = None

        # Periodic version warning timer (runs every 10 minutes when installations exist)
        self._version_warning_timer = QtCore.QTimer(self)
        try:
            self._version_warning_timer.setTimerType(QtCore.Qt.TimerType.VeryCoarseTimer)
        except Exception:
            pass
        self._version_warning_timer.setInterval(10 * 60 * 1000)
        self._version_warning_timer.timeout.connect(self._run_version_status_timer)
        self._cached_version_warnings: List[str] = []
        self._version_pair_cache: Dict[tuple[str, str], bool] = {}
        
        self._is_windows = sys.platform.startswith("win")

        # Load FryNetworks icon early so tray icon setup can use it
        if getattr(sys, 'frozen', False):
            base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
            icon_path = base_path / "resources" / "frynetworks_logo.ico"
        else:
            icon_path = Path(__file__).parent.parent / "resources" / "frynetworks_logo.ico"

        if icon_path.exists():
            self._app_icon = QtGui.QIcon(str(icon_path))
            self.setWindowIcon(self._app_icon)
        else:
            self._app_icon = QtGui.QIcon()

        self.setup_ui()
        self._slog.info("setup_ui() completed (tray icon now visible)")
        self._welcome_shown = False
        self._welcome_closed_by_user = False  # Track if user clicked "Get Started"
        self._welcome_worker = None
        self._welcome_data_loaded = False
        self._welcome_table_label = None
        self.apply_theme()
        QtCore.QTimer.singleShot(0, self._maybe_show_welcome)
        self._slog.info("_maybe_show_welcome() deferred via QTimer.singleShot(0)")

        # Connect invocation signals to main-thread slots (queued)
        try:
            self._invoke_log.connect(self._log_progress_main_thread, QtCore.Qt.ConnectionType.QueuedConnection)
            self._invoke_update.connect(self._update_progress_main_thread, QtCore.Qt.ConnectionType.QueuedConnection)
            # Step6-specific signals
            self._invoke_step6_update.connect(self._update_step6_main_thread, QtCore.Qt.ConnectionType.QueuedConnection)
            self._invoke_step6_log.connect(self._log_progress_main_thread, QtCore.Qt.ConnectionType.QueuedConnection)
            self._invoke_log_update.connect(self._update_log_line_main_thread, QtCore.Qt.ConnectionType.QueuedConnection)
            self._invoke_installation_completed.connect(self._installation_completed_main_thread, QtCore.Qt.ConnectionType.QueuedConnection)
            self._invoke_installation_failed.connect(self._installation_failed_main_thread, QtCore.Qt.ConnectionType.QueuedConnection)
            self._invoke_validation_done.connect(self._on_validation_done, QtCore.Qt.ConnectionType.QueuedConnection)
        except Exception:
            # Best-effort; UI may still work without explicit connections in unusual environments
            pass
        self._slog.info("Signal connections completed")

        # Debug log file already initialized at the start of __init__

        platform_suffix = " (linux)" if sys.platform.startswith("linux") else ""

        # Normalize version for display (strip linux- prefix if present)
        display_version = version_str or ""
        if display_version:
            lower = display_version.lower()
            if lower.startswith("linux-"):
                display_version = display_version[len("linux-"):]
            elif lower.startswith("linux_"):
                display_version = display_version[len("linux_"):]

        if display_version:
            # Avoid double "v" when version already includes it (e.g., v1.1.1)
            prefix = "" if display_version.lstrip().lower().startswith("v") else "v"
            window_title = f"FryNetworks Miners and Nodes Installer {prefix}{display_version}{platform_suffix}"
        else:
            window_title = f"FryNetworks Miners and Nodes Installer{platform_suffix}"
        
        # Add TEST VERSIONS suffix when test mode is enabled
        if self._use_test_versions:
            window_title += " [TEST VERSIONS]"
        
        self.setWindowTitle(window_title)
        self.setMinimumSize(800, 700)
        self.resize(900, 800)
        self._slog.info("FryNetworksInstallerWindow.__init__() finished")

    def _debug_log(self, message: str) -> None:
        """Best-effort append-only debug logger for installer events."""
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{ts}] {message}\n"
            path = getattr(self, "_debug_log_path", None)
            repo_path = getattr(self, "_repo_debug_log", None)
            if path:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write(line)
                except Exception:
                    pass
            if repo_path and repo_path != path:
                try:
                    repo_path.parent.mkdir(parents=True, exist_ok=True)
                    with repo_path.open("a", encoding="utf-8") as fh:
                        fh.write(line)
                except Exception:
                    pass
        except Exception:
            pass
    
    def _load_api_config(self) -> None:
        """Load API configuration from .env file next to installer executable.
        
        Sets EXTERNAL_API_BASE_URL environment variable if found in .env file.
        This allows developers to point the installer to a local API server during testing.
        """
        try:
            # Try multiple locations to find .env file
            candidate_dirs = []
            
            # 1. Check sys.executable parent (PyInstaller frozen)
            if getattr(sys, 'frozen', False):
                exe_dir = Path(sys.executable).parent
                candidate_dirs.append(exe_dir)
            
            # 2. Check script parent (dev environment)
            script_dir = Path(__file__).parent.parent
            candidate_dirs.append(script_dir)
            
            # 3. Check current working directory
            candidate_dirs.append(Path.cwd())
            
            # Try each location
            for exe_dir in candidate_dirs:
                env_path = exe_dir / ".env"
                
                if env_path.exists():
                    self._debug_log(f"[_load_api_config] Found .env at: {env_path}")
                    
                    # Read and parse .env file
                    with open(env_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            # Skip comments and empty lines
                            if not line or line.startswith('#'):
                                continue
                            
                            # Parse KEY=VALUE format
                            if '=' in line:
                                key, value = line.split('=', 1)
                                key = key.strip()
                                value = value.strip().rstrip('/')  # Remove trailing slash for consistency
                                
                                if key == 'EXTERNAL_API_BASE_URL' and value:
                                    os.environ['EXTERNAL_API_BASE_URL'] = value
                                    self._debug_log(f"[_load_api_config] Set EXTERNAL_API_BASE_URL to: {value}")
                                    self._debug_log(f"API CONFIG LOADED: Using custom API endpoint: {value}")
                                    return
            
            self._debug_log(f"[_load_api_config] .env file not found in any candidate directory")
        except Exception as exc:
            # If we can't read the .env file, silently continue with defaults
            self._debug_log(f"[_load_api_config] Error: {exc}")
            self._debug_log(f"Error loading API config: {exc}")
    
    def _load_test_mode_config(self) -> bool:
        """Load test mode configuration from .env file next to installer executable.
        
        Returns True if ENABLE_TEST_VERSIONS=true is found in .env file.
        This allows QA testers to use test-windows/test-linux platforms for version checks
        without requiring a separate build.
        """
        try:
            # Determine the directory where the installer executable is located
            if getattr(sys, 'frozen', False):
                # Running from PyInstaller bundle
                exe_dir = Path(sys.executable).parent
            else:
                # Running from source (dev environment)
                exe_dir = Path(__file__).parent.parent
            
            env_path = exe_dir / ".env"
            
            if not env_path.exists():
                return False
            
            # Read and parse .env file
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue
                    
                    # Parse KEY=VALUE format
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        
                        if key == 'ENABLE_TEST_VERSIONS':
                            enabled = value.lower() in ('true', '1', 'yes', 'on')
                            if enabled:
                                self._debug_log("TEST MODE ENABLED: Using test-windows/test-linux platforms for version checks")
                            return enabled
            
            return False
        except Exception as exc:
            # If we can't read the .env file, default to normal mode
            self._debug_log(f"Error loading test mode config: {exc}")
            return False
    
    def create_menu_bar(self):
        """Hide the top menu bar; File/Settings menus are unused."""
        try:
            mb = self.menuBar()
            if mb:
                mb.setVisible(False)
        except Exception:
            pass
    
    def change_ui_scale(self, scale: int):
        """Change UI scaling percentage."""
        # Calculate font scale factor
        base_font = QtWidgets.QApplication.font()
        new_size = int(base_font.pointSize() * (scale / 100.0))
        base_font.setPointSize(new_size)
        QtWidgets.QApplication.setFont(base_font)
        
        # Show confirmation
        QtWidgets.QMessageBox.information(
            self, "UI Scale Changed",
            f"UI scale set to {scale}%\n\nSome changes may require restarting the installer."
        )
    
    def setup_ui(self):
        """Create the user interface."""
        # Main layout
        main_widget = QtWidgets.QWidget()
        self.setCentralWidget(main_widget)
        layout = QtWidgets.QVBoxLayout(main_widget)
        # Keep a reference to the main layout so we can swap the wizard
        # widget for a full-size manage panel when requested.
        self.main_layout = layout
        layout.setSpacing(15)
        
        # Menu bar
        self.create_menu_bar()
        
        # FryNetworks banner
        self.create_banner(layout)

        # Prominent Manage Installed Miners and Nodes button under the banner with warning indicator
        try:
            manage_row = QtWidgets.QHBoxLayout()
            manage_btn = QtWidgets.QPushButton("Manage Installed Miners and Nodes")
            manage_btn.setToolTip("Open the Manage Installed Miners and Nodes panel (Ctrl+M)")
            manage_btn.setFixedHeight(36)
            manage_btn.clicked.connect(self.show_manage_panel)
            try:
                manage_btn.setShortcut("Ctrl+M")
            except Exception:
                pass
            manage_row.addWidget(manage_btn, 0)

            warning_label = QtWidgets.QLabel("")
            warning_label.setWordWrap(True)
            warning_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
            warning_label.setObjectName("versionWarningLabel")
            warning_label.setStyleSheet("color: #f97316; font-weight: 600;")
            warning_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
            warning_label.setVisible(False)
            self.version_warning_label = warning_label
            manage_row.addSpacing(12)
            manage_row.addWidget(warning_label, 1)
            layout.addLayout(manage_row)
        except Exception:
            self.version_warning_label = None

        # Kick off an initial version status check after UI loads
        try:
            QtCore.QTimer.singleShot(0, self._run_version_status_timer)
        except Exception:
            pass

        # Initialize tray icon/menu
        try:
            self._setup_tray_icon()
        except Exception:
            pass

        # Create wizard for installation process
        self.wizard = QtWidgets.QWizard()        
        # Set size policy to expand and use available space
        self.wizard.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self.wizard)
        # Allow wizard to expand vertically
        try:
            wiz_idx = layout.indexOf(self.wizard)
            if wiz_idx is not None and wiz_idx >= 0:
                try:
                    layout.setStretch(wiz_idx, 1)
                except Exception:
                    pass
        except Exception:
            pass
        
        # Configure wizard
        self.wizard.setWindowTitle("FryNetworks Miners and Nodes Installer")
        self.wizard.setWizardStyle(QtWidgets.QWizard.WizardStyle.ModernStyle)
        self.wizard.setOptions(
            QtWidgets.QWizard.WizardOption.NoBackButtonOnStartPage |
            QtWidgets.QWizard.WizardOption.HaveHelpButton |
            QtWidgets.QWizard.WizardOption.HelpButtonOnRight |
            QtWidgets.QWizard.WizardOption.HaveCustomButton1
        )

        # Typed aliases for wizard buttons
        try:
            self.NEXT_BUTTON = cast(QtWidgets.QWizard.WizardButton, getattr(QtWidgets.QWizard, 'NextButton', 0))
            self.FINISH_BUTTON = cast(QtWidgets.QWizard.WizardButton, getattr(QtWidgets.QWizard, 'FinishButton', 0))
            self.BACK_BUTTON = cast(QtWidgets.QWizard.WizardButton, getattr(QtWidgets.QWizard, 'BackButton', 0))
            self.CUSTOM_BUTTON_1 = cast(QtWidgets.QWizard.WizardButton, getattr(QtWidgets.QWizard, 'CustomButton1', 6))
            self.CUSTOM_BUTTON_2 = cast(QtWidgets.QWizard.WizardButton, getattr(QtWidgets.QWizard, 'CustomButton2', 7))
        except Exception:
            self.NEXT_BUTTON = cast(QtWidgets.QWizard.WizardButton, 0)
            self.FINISH_BUTTON = cast(QtWidgets.QWizard.WizardButton, 0)
            self.BACK_BUTTON = cast(QtWidgets.QWizard.WizardButton, 0)
            self.CUSTOM_BUTTON_1 = cast(QtWidgets.QWizard.WizardButton, 6)
            self.CUSTOM_BUTTON_2 = cast(QtWidgets.QWizard.WizardButton, 7)
        
        # Set up Clear button (Custom Button 1)
        try:
            self.wizard.setButtonText(self.CUSTOM_BUTTON_1, "Clear")
            self.clear_button = self.wizard.button(self.CUSTOM_BUTTON_1)
            if self.clear_button:
                self.clear_button.setToolTip("Clear the form and start over")
                self.clear_button.clicked.connect(self.clear_form)
                self.clear_button.setVisible(False)  # Hidden by default
                
            # Set up Install Another Miner button (Custom Button 2)
            self.wizard.setButtonText(self.CUSTOM_BUTTON_2, "Install Another Miner")
            self.install_another_button = self.wizard.button(self.CUSTOM_BUTTON_2)
            if self.install_another_button:
                self.install_another_button.setToolTip("Go back to install another miner")
                self.install_another_button.clicked.connect(self.install_another_miner)
                self.install_another_button.setVisible(False)  # Hidden by default
                
                # Set button layout to include both custom buttons
                self.wizard.setButtonLayout([
                    QtWidgets.QWizard.WizardButton.CustomButton1,
                    QtWidgets.QWizard.WizardButton.Stretch,
                    QtWidgets.QWizard.WizardButton.CustomButton2,
                    QtWidgets.QWizard.WizardButton.BackButton,
                    QtWidgets.QWizard.WizardButton.NextButton,
                    QtWidgets.QWizard.WizardButton.FinishButton,
                    QtWidgets.QWizard.WizardButton.CancelButton,
                    QtWidgets.QWizard.WizardButton.HelpButton
                ])
        except Exception:
            pass
        
        # Create wizard pages
        self.create_wizard_pages()
        
        # Connect wizard signals
        self.wizard.currentIdChanged.connect(self.on_wizard_page_changed)
        self.wizard.helpRequested.connect(self.show_help)
        # Ensure clicking the Finish button closes the installer
        try:
            self.wizard.finished.connect(lambda _id: self.close())
        except Exception:
            try:
                fb_enum = getattr(QtWidgets.QWizard, 'FinishButton', None)
                if fb_enum is not None:
                    finish_btn_widget = self.wizard.button(fb_enum)
                    if finish_btn_widget is not None:
                        finish_btn_widget.clicked.connect(self.close)
            except Exception:
                pass
        
        # Create status section
        self.create_status_section(layout)
        
        # Manage section placeholders
        self.manage_dialog = None
        self.manage_panel = None
        self._wizard_index = None
    
    def create_banner(self, layout):
        """Create FryNetworks banner."""
        # Check if running from PyInstaller bundle
        if getattr(sys, 'frozen', False):
            base_path = Path(sys._MEIPASS)  # type: ignore
            background_path = base_path / "resources" / "background.png"
        else:
            background_path = Path(__file__).parent.parent / "resources" / "background.png"
        
        self.banner = TopBanner(
            "FryNetworks Miners and Nodes Installer",
            str(background_path) if background_path.exists() else None,
            height=120
        )
        layout.addWidget(self.banner)
    
    def create_key_section(self, layout):
        """Create miner key input section."""
        key_group = QtWidgets.QGroupBox("Miner Key")
        key_group.setObjectName("layerBox")
        key_layout = QtWidgets.QVBoxLayout(key_group)
        
        # Key input field
        self.key_input = QtWidgets.QLineEdit()
        self.key_input.setPlaceholderText("Enter your FryNetworks miner key (e.g., BM-ABC123XYZ...)")
        # Listen to text changes (typing) and normalize pasted content via eventFilter
        self.key_input.textChanged.connect(self.on_key_changed)
        try:
            # Ensure we catch paste events from context menu or shortcuts
            self.key_input.installEventFilter(self)
        except Exception:
            pass
        key_layout.addWidget(self.key_input)
        
        # Key validation status
        self.key_status = QtWidgets.QLabel("")
        self.key_status.setObjectName("hint")
        key_layout.addWidget(self.key_status)
        
        self.key_group = key_group
        layout.addWidget(key_group)
    
    def create_miner_info_section(self, layout):
        """Create auto-detected miner information section."""
        self.miner_group = QtWidgets.QGroupBox("Detected Miner")
        self.miner_group.setObjectName("layerBox")
        self.miner_group.setVisible(False)
        miner_layout = QtWidgets.QVBoxLayout(self.miner_group)
        
        self.miner_info_label = QtWidgets.QLabel("")
        miner_layout.addWidget(self.miner_info_label)
        
        layout.addWidget(self.miner_group)
    
    def create_options_section(self, layout):
        """Create installation options section."""
        options_group = QtWidgets.QGroupBox("Installation Options")
        options_group.setObjectName("layerBox")
        options_layout = QtWidgets.QVBoxLayout(options_group)
        
        # Replace optional tools with desktop/taskbar options; other options are mandatory and hidden
        self.desktop_shortcut = QtWidgets.QCheckBox("Create desktop shortcut for miner GUI")
        self.desktop_shortcut.setChecked(True)
        self.pin_start_checkbox = QtWidgets.QCheckBox("Pin miner GUI to Start menu")
        if os.name != 'nt':
            self.pin_start_checkbox.setEnabled(False)
            self.pin_start_checkbox.setToolTip("Start menu pinning is only available on Windows")
        self.auto_start = QtWidgets.QCheckBox("Start miner service and GUI automatically on boot/login")
        self.auto_start.setChecked(True)

        options_layout.addWidget(self.desktop_shortcut)
        options_layout.addWidget(self.pin_start_checkbox)
        options_layout.addWidget(self.auto_start)

        layout.addWidget(options_group)
        self._create_bm_rewards_info(layout)

    def _detect_partner_secret_availability(self) -> Dict[str, bool]:
        """Check build configuration for embedded partner secrets."""
        try:
            cfg = _BUILD_CONFIG if isinstance(_BUILD_CONFIG, dict) else {}
        except Exception:
            cfg = {}
        partners = cfg.get("partner_integrations", {}) or {}

        def _has_secret(name: str, field: str) -> bool:
            data = partners.get(name, {}) or {}
            secret = data.get(field)
            enabled = data.get("enabled", bool(secret))
            if not enabled:
                return False
            return bool(isinstance(secret, str) and secret.strip())

        return {
            "honeygain": _has_secret("honeygain", "api_key"),
            "bright": _has_secret("bright", "app_id"),
        }

    def _create_bm_rewards_info(self, layout: QtWidgets.QVBoxLayout):
        """Create BM-specific rewards information (initially hidden)."""
        self.bm_rewards_group = QtWidgets.QGroupBox("fVPN Rewards Information")
        self.bm_rewards_group.setObjectName("layerBox")
        rewards_layout = QtWidgets.QVBoxLayout(self.bm_rewards_group)

        rewards_info = QtWidgets.QLabel(
            "<b>Bandwidth Miner (BM) rewards are earned in fVPN tokens.</b><br><br>"
            "To unlock the full reward potential, enable bandwidth sharing tools in the <b>miner GUI</b> after installation. "
            "All sharing tools will be available in the miner GUI with dedicated tabs for consent and activation."
        )
        rewards_info.setWordWrap(True)
        rewards_info.setTextFormat(QtCore.Qt.TextFormat.RichText)
        rewards_info.setObjectName("hint")
        rewards_layout.addWidget(rewards_info)

        self.bm_rewards_group.setVisible(False)
        layout.addWidget(self.bm_rewards_group)

    def _reset_partner_section(self, hide_group: bool = True):
        """Optionally hide the BM rewards info section."""
        try:
            if hide_group and hasattr(self, "bm_rewards_group"):
                self.bm_rewards_group.setVisible(False)
        except Exception:
            pass

    def _get_screen_size_pref(self) -> str:
        """Best-effort screen size preference used for dialog sizing."""
        try:
            pref = getattr(self, "_screen_size_pref", None)
            if pref:
                return pref
            if hasattr(self, "screen_size_combo") and hasattr(self, "_screen_size_choices"):
                idx = self.screen_size_combo.currentIndex()
                if 0 <= idx < len(self._screen_size_choices):
                    return self._screen_size_choices[idx][1]
        except Exception:
            pass
        return "auto"
    def _maybe_show_welcome(self) -> None:
        """Show welcome screen immediately with a loading placeholder, then
        fetch miner availability data from the API in a background thread."""
        try:
            self._slog.info("_maybe_show_welcome() executing")
            if getattr(self, '_welcome_shown', False):
                self._slog.info("Welcome already shown, skipping")
                return

            welcome_widget = QtWidgets.QWidget()
            main_layout = QtWidgets.QVBoxLayout(welcome_widget)
            main_layout.setContentsMargins(0, 0, 0, 0)
            main_layout.setSpacing(0)

            try:
                if getattr(sys, 'frozen', False):
                    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
                    banner_path = base_path / "resources" / "background.png"
                else:
                    banner_path = Path(__file__).parent.parent / "resources" / "background.png"
                banner = TopBanner(
                    "FryNetworks Miners and Nodes Installer",
                    str(banner_path) if banner_path.exists() else None,
                    height=140
                )
                banner.setFixedHeight(140)
                main_layout.addWidget(banner)
            except Exception:
                pass

            try:
                intro_label = QtWidgets.QLabel(
                    "<div style='font-size:20px; font-weight:bold; color:#ffffff;'>Welcome</div>"
                    "<div style='margin-top:8px; font-size:15px;'>"
                    "This wizard installs and manages supported FryNetworks miners and nodes on your system."
                    "</div>"
                )
                intro_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
                intro_label.setWordWrap(True)
                try:
                    table_container = QtWidgets.QWidget()
                    tc_layout = QtWidgets.QVBoxLayout(table_container)
                    tc_layout.setContentsMargins(48, 8, 0, 0)
                    tc_layout.setSpacing(6)
                    tc_layout.addWidget(intro_label)

                    table_label = QtWidgets.QLabel(
                        "<div style='margin-top:16px; font-size:14px; color:#aaaaaa;'>"
                        "Loading available miners...</div>"
                    )
                    table_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
                    table_label.setWordWrap(True)
                    tc_layout.addWidget(table_label, alignment=QtCore.Qt.AlignmentFlag.AlignHCenter)
                    self._welcome_table_label = table_label
                    main_layout.addWidget(table_container)
                except Exception:
                    try:
                        fallback_container = QtWidgets.QWidget()
                        fc_layout = QtWidgets.QVBoxLayout(fallback_container)
                        fc_layout.setContentsMargins(48, 8, 0, 0)
                        fc_layout.addWidget(intro_label)
                        lbl = QtWidgets.QLabel("Loading available miners...")
                        lbl.setWordWrap(True)
                        fc_layout.addWidget(lbl)
                        self._welcome_table_label = lbl
                        main_layout.addWidget(fallback_container)
                    except Exception:
                        main_layout.addWidget(intro_label)
            except Exception:
                pass

            footer = QtWidgets.QFrame()
            footer_layout = QtWidgets.QHBoxLayout(footer)
            footer_layout.addStretch()
            continue_btn = QtWidgets.QPushButton("Get Started")
            continue_btn.setFixedSize(140, 36)
            try:
                continue_btn.clicked.connect(lambda: self._on_welcome_continue(welcome_widget))
            except Exception:
                pass
            footer_layout.addWidget(continue_btn)
            footer_layout.addStretch()
            main_layout.addWidget(footer)

            try:
                self.setCentralWidget(welcome_widget)
                self._welcome_shown = True
                self._slog.info("Welcome screen set as central widget (loading state)")
            except Exception:
                self._slog.warning("Failed to set welcome screen as central widget")

            self._start_welcome_data_fetch()

        except Exception as e:
            self._debug_log(f"[_maybe_show_welcome] OUTER EXCEPTION: {e}")
            self._slog.error(f"_maybe_show_welcome() OUTER EXCEPTION: {e}")

    def _start_welcome_data_fetch(self) -> None:
        """Launch the background worker to fetch miner availability data."""
        try:
            api_base = getattr(self.api_client, 'base_url', None) if hasattr(self, 'api_client') else None
            api_token = getattr(self.api_client, 'token', None) if hasattr(self, 'api_client') else None
            if not api_base:
                self._slog.warning("_start_welcome_data_fetch: no api_client or base_url, skipping")
                return
            use_test = getattr(self, '_use_test_versions', False)
            self._welcome_worker = _WelcomeDataWorker(api_base, api_token, use_test, parent=self)
            self._welcome_worker.finished.connect(self._on_welcome_data_loaded)
            self._welcome_worker.start()
            self._slog.info("_start_welcome_data_fetch: worker thread started")
        except Exception as e:
            self._slog.warning(f"_start_welcome_data_fetch: failed to start worker — {e}")

    def _on_welcome_data_loaded(self, results) -> None:
        """Slot called on the main thread when the API worker finishes."""
        try:
            self._slog.info(f"_on_welcome_data_loaded: received results={results is not None}")
            self._welcome_data_loaded = True

            if getattr(self, '_welcome_closed_by_user', False):
                self._slog.info("_on_welcome_data_loaded: user already clicked Get Started, ignoring")
                return

            label = getattr(self, '_welcome_table_label', None)
            if label is None:
                return

            if results is None:
                label.setText(
                    "<div style='margin-top:16px; font-size:14px; color:#f97316;'>"
                    "Could not load miner availability. Check your connection and try again.</div>"
                )
                try:
                    retry_btn = QtWidgets.QPushButton("Retry")
                    retry_btn.setFixedSize(80, 28)
                    retry_btn.clicked.connect(self._retry_welcome_data)
                    parent_layout = label.parentWidget().layout() if label.parentWidget() else None
                    if parent_layout:
                        parent_layout.addWidget(retry_btn, alignment=QtCore.Qt.AlignmentFlag.AlignHCenter)
                except Exception:
                    pass
                self._slog.info("_on_welcome_data_loaded: showing error with retry button")
                return

            miners = sorted({info['name'] for info in self.parser.MINER_TYPES.values()})
            welcome_html = self.generate_welcome_message(
                miners,
                supported_windows=results.get('supported_windows'),
                supported_linux=results.get('supported_linux'),
                test_windows_codes=results.get('test_windows_set'),
                test_linux_codes=results.get('test_linux_set'),
            )
            label.setText(welcome_html)
            self._slog.info("_on_welcome_data_loaded: welcome table updated with API data")

            try:
                base_platform = 'windows' if self._is_windows else 'linux'
                version_platform = f"test-{base_platform}" if getattr(self, '_use_test_versions', False) else base_platform
                ctx = getattr(self, '_last_install_ctx', None)
                install_dir = None
                if isinstance(ctx, dict):
                    opts = ctx.get('options') or {}
                    install_dir = opts.get('_resolved_install_dir') or opts.get('install_dir')
                if install_dir:
                    try:
                        cfg_dir = Path(install_dir) / 'config'
                        cfg_dir.mkdir(exist_ok=True)
                        installer_cfg = {
                            'version_platform': version_platform,
                            'installer_version': version_str or "",
                        }
                        with (cfg_dir / 'installer_config.json').open('w', encoding='utf-8') as jf:
                            json.dump(installer_cfg, jf, indent=2)
                    except Exception:
                        pass
            except Exception:
                pass

        except Exception as e:
            self._slog.error(f"_on_welcome_data_loaded: EXCEPTION: {e}")

    def _retry_welcome_data(self) -> None:
        """Retry fetching welcome data after a failure."""
        try:
            label = getattr(self, '_welcome_table_label', None)
            if label:
                label.setText(
                    "<div style='margin-top:16px; font-size:14px; color:#aaaaaa;'>"
                    "Loading available miners...</div>"
                )
            if label and label.parentWidget():
                layout = label.parentWidget().layout()
                if layout:
                    for i in range(layout.count() - 1, -1, -1):
                        item = layout.itemAt(i)
                        w = item.widget() if item else None
                        if isinstance(w, QtWidgets.QPushButton) and w.text() == "Retry":
                            w.deleteLater()
            self._start_welcome_data_fetch()
            self._slog.info("_retry_welcome_data: retry started")
        except Exception as e:
            self._slog.error(f"_retry_welcome_data: EXCEPTION: {e}")

    def generate_welcome_message(
        self,
        available_miners: list[str],
        supported_windows: list[Any] | dict[str, Any] | None = None,
        supported_linux: list[Any] | dict[str, Any] | None = None,
        test_windows_codes: Optional[Set[str]] = None,
        test_linux_codes: Optional[Set[str]] = None,
    ) -> str:
        """Return a 4-column HTML table: Miner Code, Miner Name, Windows, Linux.

        Rows are ordered as: AEM, BM, IDM, IRM, ISM, ODM, OSM, RDN, SDN, SVN.
        `supported_windows` and `supported_linux` may be lists of codes or
        lists of dicts; we normalize them to sets of upper-case miner codes.
        `test_windows_codes` and `test_linux_codes` are raw sets of codes available in test environments.
        """
        from html import escape

        def _normalize_devices(devices: Any) -> Set[str]:
            codes: Set[str] = set()
            try:
                if devices is None:
                    return codes
                if isinstance(devices, dict):
                    candidates = devices.get("miner_codes") or devices.get("supported_devices") or devices.get("devices")
                    devices = candidates if isinstance(candidates, list) else []
                for item in devices:
                    if isinstance(item, dict):
                        code_field = item.get("code") or item.get("miner_code") or item.get("miner")
                        if isinstance(code_field, (list, tuple)):
                            for c in code_field:
                                if c:
                                    codes.add(str(c).upper())
                        elif isinstance(code_field, str) and code_field.strip():
                            codes.add(code_field.strip().upper())
                    elif isinstance(item, str):
                        if item.strip():
                            codes.add(item.strip().upper())
            except Exception:
                return set()
            return codes

        win_codes = _normalize_devices(supported_windows)
        lin_codes = _normalize_devices(supported_linux)
        test_win_codes = test_windows_codes or set()
        test_lin_codes = test_linux_codes or set()

        # Miners with BOTH production and test versions
        both_win = test_win_codes & win_codes
        both_lin = test_lin_codes & lin_codes
        # Test-only miners (in test but not in production)
        test_only_win = test_win_codes - win_codes
        test_only_lin = test_lin_codes - lin_codes

        # Fixed row order requested by user
        row_order = ["AEM", "BM", "IDM", "IRM", "ISM", "ODM", "OSM", "RDN", "SDN", "SVN"]

        # Helper to get a display name from parser.MINER_TYPES
        def miner_name_for(code: str) -> str:
            try:
                info = getattr(self, 'parser').MINER_TYPES.get(code, {}) if getattr(self, 'parser', None) else {}
                return info.get('name') or code
            except Exception:
                return code

        # Build HTML table (4 columns)
        # Column widths: Miner Code (12%), Miner Name (56%), Windows (16%), Linux (16%)
        header = (
            "<colgroup>"
            "<col style='width:12%;'>"
            "<col style='width:56%;'>"
            "<col style='width:16%;'>"
            "<col style='width:16%;'>"
            "</colgroup>"
            "<tr style='text-transform:uppercase; letter-spacing:0.5px;'>"
            "<th style='padding:8px 10px; text-align:left; border-bottom:1px solid rgba(255,255,255,0.12); font-size:12px;'>Miner Code</th>"
            "<th style='padding:8px 10px; text-align:left; border-bottom:1px solid rgba(255,255,255,0.12); font-size:12px;'>Miner Name</th>"
            "<th style='padding:8px 10px; text-align:center; border-bottom:1px solid rgba(255,255,255,0.12); font-size:12px;'>Windows</th>"
            "<th style='padding:8px 10px; text-align:center; border-bottom:1px solid rgba(255,255,255,0.12); font-size:12px;'>Linux</th>"
            "</tr>"
        )

        body_rows: List[str] = []
        for code in row_order:
            display_name = miner_name_for(code)
            
            # Windows column: show ✓ - TEST for both, TEST for test-only, ✓ for prod only, ✗ for unavailable
            if code in both_win:
                # Both prod and test: checkmark for prod, separator, TEST for test
                win_avail = "<span style='color:#10b981; font-weight:600;'>✓</span> - <span style='color:#3b82f6; font-weight:600; font-size:11px; letter-spacing:0.5px;'>TEST</span>"
                win_color = "inherit"
                win_style = "font-size:11px; line-height:1.2;"
            elif code in test_only_win:
                # Test-only
                win_avail = "TEST"
                win_color = "#3b82f6"  # Blue for test versions
                win_style = "font-weight:600; font-size:11px; letter-spacing:0.5px;"
            elif code in win_codes:
                # Production only
                win_avail = "✓"
                win_color = "#10b981"  # Green for production
                win_style = ""
            else:
                # Unavailable
                win_avail = "✗"
                win_color = "#ef4444"  # Red for unavailable
                win_style = ""
            
            # Linux column: show ✓ - TEST for both, TEST for test-only, ✓ for prod only, ✗ for unavailable
            if code in both_lin:
                # Both prod and test: checkmark for prod, separator, TEST for test
                lin_avail = "<span style='color:#10b981; font-weight:600;'>✓</span> - <span style='color:#3b82f6; font-weight:600; font-size:11px; letter-spacing:0.5px;'>TEST</span>"
                lin_color = "inherit"
                lin_style = "font-size:11px; line-height:1.2;"
            elif code in test_only_lin:
                # Test-only
                lin_avail = "TEST"
                lin_color = "#3b82f6"  # Blue for test versions
                lin_style = "font-weight:600; font-size:11px; letter-spacing:0.5px;"
            elif code in lin_codes:
                # Production only
                lin_avail = "✓"
                lin_color = "#10b981"  # Green for production
                lin_style = ""
            else:
                # Unavailable
                lin_avail = "✗"
                lin_color = "#ef4444"  # Red for unavailable
                lin_style = ""
            
            body_rows.append(
                "<tr>"
                f"<td style='padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.06);'>{escape(code)}</td>"
                f"<td style='padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.06); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:13px; line-height:1.1;'>{escape(display_name)}</td>"
                f"<td style='text-align:center; padding:8px 10px; color:{win_color}; {win_style} border-bottom:1px solid rgba(255,255,255,0.06);'>{win_avail}</td>"
                f"<td style='text-align:center; padding:8px 10px; color:{lin_color}; {lin_style} border-bottom:1px solid rgba(255,255,255,0.06);'>{lin_avail}</td>"
                "</tr>"
            )

        table_html = (
            "<div style='display:flex; justify-content:center; margin-top:12px;'>"
            "<table style='width:86%; max-width:1400px; min-width:760px; border-collapse:collapse; font-size:13px; background:rgba(0,0,0,0.35);"
            "box-shadow:0 4px 12px rgba(0,0,0,0.35); border:1px solid rgba(255,255,255,0.12);'>"
            f"{header}"
            f"{''.join(body_rows)}"
            "</table>"
            "</div>"
        )

        return table_html

    def _get_best_version(self, miner_code: str, platform: str = 'windows') -> Dict[str, Any]:
        """Get the best available version for a miner.
        
        In test mode: compares production and test versions, returns whichever is most recent.
        In production mode: returns only production version.
        
        Returns the version dict with gui_version, poc_version, etc.
        """
        use_test = getattr(self, '_use_test_versions', False)
        
        if not use_test:
            # Production mode - only get production version
            try:
                return self.api_client.get_required_version(miner_code, platform=platform, use_test=False) or {}
            except Exception:
                return {}
        
        # Test mode - get both and compare
        prod_version = None
        test_version = None
        
        try:
            prod_version = self.api_client.get_required_version(miner_code, platform=platform, use_test=False) or {}
        except Exception:
            prod_version = {}
        
        try:
            test_version = self.api_client.get_required_version(miner_code, platform=platform, use_test=True) or {}
        except Exception:
            test_version = {}
        
        # If only one exists, use it
        if not prod_version and test_version:
            return test_version
        if prod_version and not test_version:
            return prod_version
        if not prod_version and not test_version:
            return {}
        
        # Both exist - compare versions and return most recent
        try:
            from packaging import version
            prod_gui = prod_version.get('software_version', '0.0.0')
            test_gui = test_version.get('software_version', '0.0.0')
            
            if version.parse(test_gui) > version.parse(prod_gui):
                return test_version
            else:
                return prod_version
        except Exception:
            # If comparison fails, prefer test version in test mode
            return test_version

    def _has_complete_version_pair(self, code: str, platform: str, use_test: bool = False) -> bool:
        """Check if a miner code has versions available for the given platform.
        
        Now that the API returns filtered lists per OS type, this checks if the code
        appears in the supported list for that OS. Still called during key validation.
        """
        try:
            code_upper = str(code).upper()
            use_test_mode = use_test or getattr(self, "_use_test_versions", False)
            
            # Determine which platform strings to check
            if platform.startswith('linux'):
                if use_test_mode:
                    # Check both test and production Linux
                    test_supported = getattr(self, "_supported_linux_codes", set()) or set()
                    return code_upper in test_supported
                else:
                    # Check production Linux only
                    prod_supported = getattr(self, "_supported_linux_codes", set()) or set()
                    return code_upper in prod_supported
            else:  # Windows
                if use_test_mode:
                    # Check both test and production Windows
                    test_supported = getattr(self, "_supported_windows_codes", set()) or set()
                    return code_upper in test_supported
                else:
                    # Check production Windows only
                    prod_supported = getattr(self, "_supported_windows_codes", set()) or set()
                    return code_upper in prod_supported
        except Exception:
            # If check fails, assume available (don't block)
            return True

    def _ensure_supported_offerings_loaded(self) -> None:
        """Ensure `self._supported_windows_codes` and `self._supported_linux_codes` exist.

        This loads supported miner codes from the API client on first use and
        stores them as upper-case sets for availability checks.
        Checks BOTH production and test endpoints, merging results so miners work
        whether they exist in production or test versions.
        """
        if getattr(self, '_supported_windows_codes', None) is not None and getattr(self, '_supported_linux_codes', None) is not None:
            return
        supported_windows = []
        supported_linux = []
        try:
            if hasattr(self, 'api_client') and hasattr(self.api_client, 'get_supported_installers'):
                def _extract(s):
                    if isinstance(s, dict):
                        c = s.get('miner_codes') or s.get('supported_devices') or s.get('devices')
                        return c if isinstance(c, list) else []
                    return []

                # Always fetch production versions
                try:
                    prod_win = self.api_client.get_supported_installers('windows', use_test=False) or {}
                    supported_windows.extend(_extract(prod_win))
                except Exception:
                    pass
                try:
                    prod_lin = self.api_client.get_supported_installers('linux', use_test=False) or {}
                    supported_linux.extend(_extract(prod_lin))
                except Exception:
                    pass

                # Also fetch test versions (so IDM works whether in prod or test)
                try:
                    test_win = self.api_client.get_supported_installers('windows', use_test=True) or {}
                    supported_windows.extend(_extract(test_win))
                except Exception:
                    pass
                try:
                    test_lin = self.api_client.get_supported_installers('linux', use_test=True) or {}
                    supported_linux.extend(_extract(test_lin))
                except Exception:
                    pass
        except Exception:
            supported_windows = supported_windows or []
            supported_linux = supported_linux or []

        try:
            self._supported_windows_codes = set(str(x).upper() for x in (supported_windows or []) if x)
            self._supported_linux_codes = set(str(x).upper() for x in (supported_linux or []) if x)
        except Exception:
            self._supported_windows_codes = set()
            self._supported_linux_codes = set()

    def _clean_key_text(self, text: str) -> str:
        """Return a normalized miner key string suitable for parsing.

        Removes leading/trailing whitespace, newlines and common invisible
        characters that often appear when copying from other applications.
        """
        try:
            if not isinstance(text, str):
                return ''
            # Trim common whitespace and remove line breaks / tabs
            cleaned = text.strip().replace('\r', '').replace('\n', '').replace('\t', '')
            # Remove zero-width / BOM characters often introduced by copy/paste
            cleaned = cleaned.replace('\ufeff', '').replace('\u200b', '')
            return cleaned
        except Exception:
            return text or ''

    def _normalize_key_input(self) -> None:
        """Normalize the current QLineEdit value in-place without causing recursion.

        If the displayed text contains stray whitespace/newlines from paste,
        this will replace it with a cleaned value while blocking signals so
        we don't re-enter the change handler unexpectedly.
        """
        try:
            if not hasattr(self, 'key_input') or self.key_input is None:
                return
            orig = self.key_input.text()
            cleaned = self._clean_key_text(orig)
            if cleaned != orig:
                try:
                    self.key_input.blockSignals(True)
                    self.key_input.setText(cleaned)
                    # Move cursor to end for better UX
                    try:
                        self.key_input.setCursorPosition(len(cleaned))
                    except Exception:
                        pass
                finally:
                    try:
                        self.key_input.blockSignals(False)
                    except Exception:
                        pass
                    # Manually invoke change handler since we blocked signals
                    try:
                        self.on_key_changed(cleaned)
                    except Exception:
                        pass
        except Exception:
            pass

    def eventFilter(self, obj, event):
        """Catch paste events on the key input so pasted content is normalized.

        This ensures copy/paste behaves the same as manual typing for validation.
        """
        try:
            if obj is getattr(self, 'key_input', None):
                # Schedule normalization for any event on the key input. This
                # is a lightweight no-op for most typed input but guarantees we
                # catch pasted content from context menus, shortcuts or drag/drop.
                try:
                    QtCore.QTimer.singleShot(0, self._normalize_key_input)
                except Exception:
                    try:
                        self._normalize_key_input()
                    except Exception:
                        pass
        except Exception:
            pass

        # Default processing
        try:
            return super().eventFilter(obj, event)
        except Exception:
            return False

    def _set_validate_button_enabled(self, enabled: bool) -> None:
        """Enable/disable the footer 'Validate Key' (Next) button robustly.

        Some environments may expose the wizard Next button under different
        enums or a separate QPushButton instance. This helper tries multiple
        strategies to locate the button and update its enabled state and
        appearance.
        """
        try:
            # Strategy 1: use stored enum reference (preferred)
            try:
                nb = self.wizard.button(self.NEXT_BUTTON)
            except Exception:
                nb = None

            # Strategy 2: use Qt enum directly
            if nb is None:
                try:
                    next_enum = getattr(QtWidgets.QWizard, 'NextButton', None)
                    if next_enum is not None:
                        nb = self.wizard.button(cast(Any, next_enum))
                except Exception:
                    nb = None

            # Strategy 3: find any QPushButton child whose text contains 'Validate' or 'Next'
            if nb is None:
                try:
                    # Search both the main window and the wizard for QPushButton children
                    candidates = []
                    try:
                        candidates.extend(self.findChildren(QtWidgets.QPushButton))
                    except Exception:
                        pass
                    try:
                        if hasattr(self, 'wizard') and self.wizard is not None:
                            candidates.extend(self.wizard.findChildren(QtWidgets.QPushButton))
                    except Exception:
                        pass

                    for child in candidates:
                        try:
                            txt = (child.text() or "").strip()
                            if 'validate' in txt.lower() or 'next' in txt.lower():
                                nb = child
                                break
                        except Exception:
                            continue
                except Exception:
                    nb = None

            if nb is not None:
                try:
                    nb.setEnabled(bool(enabled))
                    # Force a repaint so style (disabled look) updates immediately
                    try:
                        nb.repaint()
                        nb.update()
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass

    def _sync_validate_button_state(self, enabled: bool) -> None:
        """Ensure both the wizard Next button and the visible footer Validate
        button reflect the same enabled state and label.
        """
        try:
            # First, toggle any visible footer buttons
            try:
                self._set_validate_button_enabled(enabled)
            except Exception:
                pass
            # Then, ensure the wizard's NextButton is enabled/disabled.
            # Only update the button text when disabling (set to 'Validate Key'),
            # or when enabling and the key is validated and the current label
            # is still the default 'Validate Key' (so we don't overwrite explicit
            # labels like '< Try Another Key').
            try:
                next_enum = getattr(QtWidgets.QWizard, 'NextButton', None)
                if next_enum is not None:
                    nb_widget = None
                    try:
                        nb_widget = self.wizard.button(cast(Any, next_enum))
                    except Exception:
                        nb_widget = None

                    # Set enabled/disabled state
                    try:
                        if nb_widget is not None:
                            nb_widget.setEnabled(bool(enabled))
                    except Exception:
                        pass

                    # Decide label behavior
                    try:
                        current_text = ''
                        if nb_widget is not None:
                            try:
                                current_text = (nb_widget.text() or '').strip()
                            except Exception:
                                current_text = ''

                        # When disabling, always show 'Validate Key'
                        if not bool(enabled):
                            try:
                                self.wizard.setButtonText(cast(Any, next_enum), "Validate Key")
                            except Exception:
                                pass
                        else:
                            # Enabling: if key has been validated, and the button
                            # still shows 'Validate Key', change it to 'Next >'. If
                            # it already has an explicit label like '< Try Another Key',
                            # preserve it.
                            try:
                                if getattr(self, 'is_key_validated', False):
                                    if current_text.lower() in ("validate key", ""):
                                        try:
                                            self.wizard.setButtonText(cast(Any, next_enum), "Next >")
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

        # Schedule a reinforced update after the event loop so QWizard's
        # internal enabling logic doesn't immediately overwrite our state.
        try:
            def _apply_later():
                try:
                    try:
                        self._set_validate_button_enabled(enabled)
                    except Exception:
                        pass
                    try:
                        next_enum = getattr(QtWidgets.QWizard, 'NextButton', None)
                        if next_enum is not None:
                            try:
                                nbw = self.wizard.button(cast(Any, next_enum))
                                if nbw is not None:
                                    nbw.setEnabled(bool(enabled))
                            except Exception:
                                pass
                    except Exception:
                        pass
                except Exception:
                    pass

            try:
                QtCore.QTimer.singleShot(0, _apply_later)
            except Exception:
                pass
        except Exception:
            pass

    def _on_welcome_continue(self, splash_widget: QtWidgets.QWidget):
        # Mark that user has progressed past welcome screen
        # This prevents minimize-to-tray behavior if they close the app later
        self._welcome_closed_by_user = True
        splash_widget.setParent(None)
        self.setup_ui()

    def show_welcome_page(self) -> None:
        """Show the welcome page with the list of available miners and nodes.
        
        This is called when the user clicks the "Show Available Miners" button in the footer.
        Replaces the current UI with the full welcome page.
        """
        try:
            use_test = getattr(self, '_use_test_versions', False)
            
            # Fetch supported miners from API
            supported_windows = {}
            supported_linux = {}
            test_windows_set = set()
            test_linux_set = set()

            if hasattr(self, 'api_client'):
                try:
                    # Get production versions
                    win_data = self.api_client.get_supported_installers('windows', use_test=False) or {}
                    lin_data = self.api_client.get_supported_installers('linux', use_test=False) or {}
                    supported_windows = win_data
                    supported_linux = lin_data
                except Exception:
                    pass
                
                # If test mode, also get test versions
                if use_test:
                    try:
                        test_win_data = self.api_client.get_supported_installers('windows', use_test=True) or {}
                        test_lin_data = self.api_client.get_supported_installers('linux', use_test=True) or {}
                        test_windows_set = set(str(x).upper() for x in test_win_data.get('miner_codes', []) if x)
                        test_linux_set = set(str(x).upper() for x in test_lin_data.get('miner_codes', []) if x)
                    except Exception:
                        pass

            miners = sorted({info['name'] for info in self.parser.MINER_TYPES.values()})
            welcome_html = self.generate_welcome_message(
                miners,
                supported_windows=supported_windows,
                supported_linux=supported_linux,
                test_windows_codes=test_windows_set,
                test_linux_codes=test_linux_set,
            )

            # Build welcome widget (similar to _maybe_show_welcome)
            welcome_widget = QtWidgets.QWidget()
            main_layout = QtWidgets.QVBoxLayout(welcome_widget)
            main_layout.setContentsMargins(0, 0, 0, 0)
            main_layout.setSpacing(0)

            # Top banner
            try:
                if getattr(sys, 'frozen', False):
                    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
                    banner_path = base_path / "resources" / "background.png"
                else:
                    banner_path = Path(__file__).parent.parent / "resources" / "background.png"
                banner = TopBanner(
                    "FryNetworks Miners and Nodes Installer",
                    str(banner_path) if banner_path.exists() else None,
                    height=140
                )
                banner.setFixedHeight(140)
                main_layout.addWidget(banner)
            except Exception:
                pass

            # Intro label
            try:
                intro_label = QtWidgets.QLabel(
                    "<div style='font-size:20px; font-weight:bold; color:#ffffff;'>Welcome</div>"
                    "<div style='margin-top:8px; font-size:15px;'>"
                    "This wizard installs and manages supported FryNetworks miners and nodes on your system."
                    "</div>"
                )
                intro_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
                intro_label.setWordWrap(True)
                
                # Create a left-margined container for intro + table
                table_container = QtWidgets.QWidget()
                tc_layout = QtWidgets.QVBoxLayout(table_container)
                tc_layout.setContentsMargins(48, 8, 0, 0)
                tc_layout.setSpacing(6)
                tc_layout.addWidget(intro_label)

                # Table / welcome HTML
                table_label = QtWidgets.QLabel(welcome_html)
                table_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
                table_label.setWordWrap(True)
                tc_layout.addWidget(table_label, alignment=QtCore.Qt.AlignmentFlag.AlignHCenter)

                main_layout.addWidget(table_container)
            except Exception:
                pass

            # Footer with buttons
            footer = QtWidgets.QFrame()
            footer_layout = QtWidgets.QHBoxLayout(footer)
            footer_layout.addStretch()
            
            back_btn = QtWidgets.QPushButton("Back to Installer")
            back_btn.setFixedSize(140, 36)
            back_btn.clicked.connect(self._restore_installer_ui)
            footer_layout.addWidget(back_btn)
            
            footer_layout.addStretch()
            main_layout.addWidget(footer)

            self.setCentralWidget(welcome_widget)
        except Exception as e:
            self._debug_log(f"[show_welcome_page] Error: {e}")
            QtWidgets.QMessageBox.warning(
                self,
                "Error",
                f"Could not display available miners: {e}"
            )

    def _restore_installer_ui(self) -> None:
        """Restore the main installer UI after showing the welcome page."""
        try:
            # Clear the current central widget
            old_widget = self.centralWidget()
            if old_widget:
                old_widget.setParent(None)
                old_widget.deleteLater()
            
            # Recreate the installer UI
            self.setup_ui()
        except Exception as e:
            self._debug_log(f"[_restore_installer_ui] Error: {e}")

    def _update_partner_section_visibility(self):
        """Show or hide BM-only rewards information."""
        group = getattr(self, "bm_rewards_group", None)
        if group is None:
            return
        is_bm = bool(self.current_miner_info and self.current_miner_info.get("code") == "BM")
        if is_bm:
            group.setVisible(True)
        else:
            self._reset_partner_section(hide_group=True)

    def create_additional_settings(self, layout):
        """Create additional settings such as install path and screen size."""
        settings_group = QtWidgets.QGroupBox("Advanced Settings")
        settings_group.setObjectName("layerBox")
        form = QtWidgets.QFormLayout(settings_group)

        # Install path selector
        path_row = QtWidgets.QHBoxLayout()
        self.install_path_edit = QtWidgets.QLineEdit()
        self.install_path_edit.setPlaceholderText("Default based on install scope")
        browse_btn = QtWidgets.QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_install_path)
        path_row.addWidget(self.install_path_edit)
        path_row.addWidget(browse_btn)
        form.addRow("Install path (optional):", self._wrap_layout(path_row))

        # Initialize default path placeholder after widget is created
        self._update_install_path_placeholder()

        # Screen size option consistent with miner_GUI responsive system
        self.screen_size_combo = QtWidgets.QComboBox()
        # Display label -> stored value
        self._screen_size_choices = [
            ("Auto Detect", "auto"),
            ("Mobile", "mobile"),
            ("Tablet", "tablet"),
            ("Laptop", "laptop"),
            ("Desktop", "desktop"),
            ("Ultrawide", "ultrawide"),
        ]
        for label, _val in self._screen_size_choices:
            self.screen_size_combo.addItem(label)
        self.screen_size_combo.setCurrentIndex(0)
        form.addRow("Miner GUI screen size:", self.screen_size_combo)

        layout.addWidget(settings_group)

        # Initialize placeholder now
        self._update_install_path_placeholder()

    def _wrap_layout(self, inner_layout: QtWidgets.QLayout) -> QtWidgets.QWidget:
        """Wrap a layout into a QWidget for use in FormLayout rows."""
        w = QtWidgets.QWidget()
        w.setLayout(inner_layout)
        return w

    def _browse_install_path(self):
        """Open a directory chooser for the custom install path."""
        start_dir = self.install_path_edit.text().strip() or str(Path.home())
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Install Directory", start_dir)
        if chosen:
            self.install_path_edit.setText(chosen)

    def _update_install_path_placeholder(self):
        """Update install path placeholder based on default system-wide install."""
        try:
            miner_code = self.current_miner_info.get("code") if self.current_miner_info else None
            cfg = ConfigManager(miner_code)
            # System-wide installation is mandatory
            default_path = cfg.get_installation_directory(True)
            self.install_path_edit.setPlaceholderText(str(default_path))
        except Exception:
            self.install_path_edit.setPlaceholderText("Default based on install scope")

    def refresh_conflicts(self):
        """Recheck device compatibility for the current miner key (background thread)."""
        try:
            key = self.key_input.text().strip() if hasattr(self, 'key_input') else ''
            if not key:
                try:
                    self.conflict_status.setText("Enter a miner key to check compatibility")
                    self.status_bar.setText("Ready - Enter a miner key to begin")
                except Exception:
                    pass
                return

            # Prevent double-click while already running
            if self._validation_thread is not None and self._validation_thread.is_alive():
                return

            self.status_bar.setText("Checking for conflicts...")
            try:
                if getattr(self, 'conflict_refresh_btn', None) is not None:
                    self.conflict_refresh_btn.setEnabled(False)
            except Exception:
                pass

            def _worker():
                try:
                    conflicts = self.detector.check_device_conflicts(key)
                except Exception as e:
                    conflicts = {"error": str(e)}
                try:
                    self._invoke_validation_done.emit({"conflicts": conflicts, "refresh_only": True})
                except Exception:
                    QtCore.QTimer.singleShot(0, lambda c=conflicts: self._on_validation_done({"conflicts": c, "refresh_only": True}))

            self._validation_thread = threading.Thread(target=_worker, daemon=True)
            self._validation_thread.start()

        except Exception:
            pass
    
    def create_manage_section(self, layout, persistent: bool = True, dialog: Optional[QtWidgets.QDialog] = None):
        """Create manage installed miners and nodes section with table view.

        If persistent is True then widgets are stored on the instance (so the
        in-window manage panel reuses them). If persistent is False a set of
        local widgets is created for use in a transient dialog so we don't
        reparent the persistent widgets.
        """
        # Title and refresh button
        title_layout = QtWidgets.QHBoxLayout()
        title_label = QtWidgets.QLabel("Installed Miners and Nodes")
        title_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: #ef4444;")
        refresh_btn = QtWidgets.QPushButton("Refresh")
        refresh_btn.setFixedWidth(90)
        title_layout.addWidget(title_label)
        title_layout.addStretch()
        title_layout.addWidget(refresh_btn)
        title_layout.setContentsMargins(0, 0, 8, 0)  # 8px right margin to align with table buttons
        layout.addLayout(title_layout)
        
        # Installations table (persistent or local)
        table = QtWidgets.QTableWidget()
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels([
            "Miner Key", "GUI\nVersion", "GUI\nStatus", 
            "PoC\nVersion", "PoC\nStatus", "Actions"
        ])
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setWordWrap(False)  # Prevent word wrapping in cells
        table.horizontalHeader().setStretchLastSection(False)
        # Allow header text to wrap for multi-line titles
        table.horizontalHeader().setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        # Set Miner Key column to fixed width so it doesn't get squeezed
        table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)  # Miner Key
        table.setColumnWidth(0, 330)  # Miner Key column - reduced slightly to avoid overlap
        table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Fixed)  # GUI Version
        table.setColumnWidth(1, 60)
        table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)  # GUI Status
        table.setColumnWidth(2, 60)
        table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Fixed)  # PoC Version
        table.setColumnWidth(3, 60)
        table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Fixed)  # PoC Status
        table.setColumnWidth(4, 60)
        table.horizontalHeader().setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeMode.Stretch)  # Actions fill remaining space
        table.verticalHeader().setVisible(False)
        table.setShowGrid(True)
        table.setAlternatingRowColors(True)
        # Disable text eliding so full content shows (or scrolls horizontally within cell)
        table.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
        # Set horizontal scroll mode to per pixel for smoother scrolling if needed
        table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        # Dark theme styling for readability
        table.setStyleSheet(
            "QTableWidget {"
            "  background-color: #0b0f16;"
            "  color: #e5e7eb;"
            "  gridline-color: #1f2937;"
            "  alternate-background-color: #0f172a;"
            "  selection-background-color: #374151;"
            "  selection-color: #ffffff;"
            "}"
            "QHeaderView::section {"
            "  background-color: #111827;"
            "  color: #d1d5db;"
            "  padding: 6px;"
            "  border: 0px;"
            "  font-size: 9pt;"
            "}"
            "QTableWidget::item {"
            "  padding: 6px;"
            "}"
        )
        
        if persistent:
            self.installations_table = table
        else:
            self._dialog_installations_table = table
        
        layout.addWidget(table)
        
        # Action buttons (Back aligned to the right)
        action_layout = QtWidgets.QHBoxLayout()
        action_layout.setContentsMargins(0, 0, 8, 0)  # 8px right margin to align with table buttons
        action_layout.addStretch()

        if persistent:
            if getattr(self, 'manage_back_btn', None) is None:
                back_btn = QtWidgets.QPushButton("Back")
                back_btn.setFixedHeight(32)
                back_btn.setToolTip("Back to installer (Alt+Left)")
                try:
                    back_btn.setShortcut("Alt+Left")
                except Exception:
                    pass
                back_btn.setObjectName("manageBackButton")
                back_btn.setAccessibleName("manageBackButton")
                try:
                    back_btn.clicked.connect(self.hide_manage_panel)
                except Exception:
                    try:
                        back_btn.clicked.connect(lambda: self.manage_dialog.close() if self.manage_dialog else None)
                    except Exception:
                        pass
                self.manage_back_btn = back_btn
            else:
                back_btn = self.manage_back_btn
            back_btn.setFixedWidth(100)
            action_layout.addWidget(back_btn)
        else:
            local_back_btn = QtWidgets.QPushButton("Back")
            local_back_btn.setFixedHeight(32)
            local_back_btn.setToolTip("Back to installer (Alt+Left)")
            try:
                local_back_btn.setShortcut("Alt+Left")
            except Exception:
                pass
            local_back_btn.setObjectName("manageBackButton")
            try:
                if dialog is not None:
                    local_back_btn.clicked.connect(dialog.close)
                else:
                    local_back_btn.clicked.connect(lambda: None)
            except Exception:
                pass
            action_layout.addWidget(local_back_btn)

        layout.addLayout(action_layout)
        
        # Connect refresh button (after all widgets are created)
        if persistent:
            refresh_btn.clicked.connect(self._refresh_installations)
        else:
            refresh_btn.clicked.connect(lambda: self._refresh_installations(use_dialog=True))
        
        # Initial load of installations
        try:
            self._refresh_installations(use_dialog=not persistent)
        except Exception:
            pass

    def _refresh_installations(self, use_dialog: bool = False):
        """Refresh the table of installed miners and nodes with version checking.
        
        Args:
            use_dialog: If True, use dialog widget references instead of persistent ones.
        """
        # Get table reference based on mode
        if use_dialog:
            table = getattr(self, '_dialog_installations_table', None)
        else:
            table = getattr(self, 'installations_table', None)
        
        # Return early if widget doesn't exist
        if not table:
            return
        
        table.setRowCount(0)
        
        try:
            config_manager = ConfigManager()
            installations = config_manager.detect_existing_installations()
            
            # Filter out incomplete/failed installations (missing miner_key or required versions)
            valid_installations = []
            for install in installations:
                miner_key = install.get('config', {}).get('miner_key', '').strip()
                gui_version = install.get('config', {}).get('gui_version', '').strip()
                poc_version = install.get('config', {}).get('poc_version', '').strip()
                
                # Only include installations that have a valid miner key and both versions
                if miner_key and gui_version and poc_version and gui_version != 'Unknown' and poc_version != 'Unknown':
                    valid_installations.append(install)
            
            installations = valid_installations
            self._ensure_version_timer_state(bool(installations))
            
            if not installations:
                table.setRowCount(1)
                no_install_item = QtWidgets.QTableWidgetItem("No installations found")
                # Keep item enabled so text isn't greyed out, but it's not interactive
                no_install_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                no_install_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                table.setItem(0, 0, no_install_item)
                table.setSpan(0, 0, 1, 7)  # Span across all columns
                self._cached_version_warnings = []
                self._update_version_warning_label(None)
                return
            
            # Get latest versions from API for comparison
            latest_versions = self._fetch_latest_versions_for_installations(installations)
            
            # Populate table
            for row_idx, install in enumerate(installations):
                table.insertRow(row_idx)
                
                # Column 0: Miner Key
                miner_key = install.get('config', {}).get('miner_key', 'N/A')
                key_item = QtWidgets.QTableWidgetItem(miner_key)
                key_item.setData(QtCore.Qt.ItemDataRole.UserRole, install)  # Store full install data
                key_item.setToolTip(miner_key)  # Show full key on hover
                table.setItem(row_idx, 0, key_item)
                
                # Column 1: GUI Version
                gui_version = install.get('config', {}).get('gui_version', 'Unknown')
                gui_version_item = QtWidgets.QTableWidgetItem(gui_version)
                gui_version_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                table.setItem(row_idx, 1, gui_version_item)
                
                # Column 2: GUI Status
                miner_code = str(install.get('miner_code') or '')
                latest = latest_versions.get(miner_code, {})
                latest_gui = latest.get('software_version', '')
                gui_uptodate = False
                gui_symbol = "?"
                gui_tooltip = "Version status unknown"
                if latest_gui and gui_version != 'Unknown':
                    gui_uptodate = (gui_version == latest_gui)
                    if gui_uptodate:
                        gui_symbol = "✓"
                        gui_tooltip = "Up-to-date"
                    else:
                        gui_symbol = "⚠"
                        gui_tooltip = f"Update available: {latest_gui}"
                elif latest_gui:
                    gui_symbol = "⚠"
                    gui_tooltip = f"Installed version unknown. Latest: {latest_gui}"
                gui_status_item = QtWidgets.QTableWidgetItem(gui_symbol)
                gui_status_item.setToolTip(gui_tooltip)
                gui_status_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                if gui_uptodate:
                    gui_status_item.setForeground(QtGui.QColor("#10b981"))  # Green
                elif latest_gui:
                    gui_status_item.setForeground(QtGui.QColor("#f59e0b"))  # Amber when update needed/unknown
                else:
                    gui_status_item.setForeground(QtGui.QColor("#9ca3af"))  # Neutral gray
                table.setItem(row_idx, 2, gui_status_item)
                
                # Column 3: PoC Version
                poc_version = install.get('config', {}).get('poc_version', 'Unknown')
                poc_version_item = QtWidgets.QTableWidgetItem(poc_version)
                poc_version_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                table.setItem(row_idx, 3, poc_version_item)
                
                # Column 4: PoC Status
                latest_poc = latest.get('poc_version', '')
                poc_uptodate = False
                poc_symbol = "?"
                poc_tooltip = "Version status unknown"
                if latest_poc and poc_version != 'Unknown':
                    poc_uptodate = (poc_version == latest_poc)
                    if poc_uptodate:
                        poc_symbol = "✓"
                        poc_tooltip = "Up-to-date"
                    else:
                        poc_symbol = "⚠"
                        poc_tooltip = f"Update available: {latest_poc}"
                elif latest_poc:
                    poc_symbol = "⚠"
                    poc_tooltip = f"Installed version unknown. Latest: {latest_poc}"
                poc_status_item = QtWidgets.QTableWidgetItem(poc_symbol)
                poc_status_item.setToolTip(poc_tooltip)
                poc_status_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                if poc_uptodate:
                    poc_status_item.setForeground(QtGui.QColor("#10b981"))  # Green
                elif latest_poc:
                    poc_status_item.setForeground(QtGui.QColor("#f59e0b"))  # Amber when update needed/unknown
                else:
                    poc_status_item.setForeground(QtGui.QColor("#9ca3af"))  # Neutral gray
                table.setItem(row_idx, 4, poc_status_item)
                
                # Column 5: Action buttons
                action_widget = QtWidgets.QWidget()
                action_layout = QtWidgets.QHBoxLayout(action_widget)
                action_layout.setContentsMargins(4, 2, 4, 2)  # Small margins on all sides
                action_layout.setSpacing(4)
                
                # Combined Start button
                action_layout.addStretch()
                start_btn = QtWidgets.QPushButton("Start")
                start_btn.setFixedSize(60, 24)
                start_btn.setStyleSheet("min-width: 60px; max-width: 60px; padding-top: 0px; padding-bottom: 4px;")
                start_btn.clicked.connect(lambda checked, r=row_idx: self._start_miner(r, use_dialog))
                action_layout.addWidget(start_btn)
                
                needs_update = (latest_gui and not gui_uptodate) or (latest_poc and not poc_uptodate)
                if needs_update:
                    update_btn = QtWidgets.QPushButton("Update")
                    update_btn.setFixedSize(60, 24)
                    update_btn.setStyleSheet("min-width: 60px; max-width: 60px; padding-top: 0px; padding-bottom: 4px;")
                    update_btn.clicked.connect(lambda checked, r=row_idx: self._update_installation(r, use_dialog))
                    action_layout.addWidget(update_btn)
                    
                uninstall_btn = QtWidgets.QPushButton("Uninstall")
                uninstall_btn.setFixedSize(70, 24)
                uninstall_btn.setStyleSheet("min-width: 70px; max-width: 70px; padding-top: 0px; padding-bottom: 4px;")
                uninstall_btn.clicked.connect(lambda checked, r=row_idx: self._uninstall_installation(r, use_dialog))
                action_layout.addWidget(uninstall_btn)
                action_layout.addStretch()
                
                table.setCellWidget(row_idx, 5, action_widget)
            
            # Adjust row heights to fully show buttons
            for row_idx in range(table.rowCount()):
                table.setRowHeight(row_idx, 40)

            warnings = self._build_version_warning_entries(installations, latest_versions)
            self._handle_version_warning_update(warnings)
    
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            QtWidgets.QMessageBox.critical(
                self,
                "Error",
                f"Failed to detect installations:\n{str(e)}\n\nSee console for details."
            )

    def _refresh_manage_views_once(self) -> None:
        """Refresh manage tables (dialog and panel) once if they exist."""
        def _safe_refresh(use_dialog: bool) -> None:
            try:
                self._refresh_installations(use_dialog=use_dialog)
            except Exception:
                pass

        try:
            QtCore.QTimer.singleShot(0, lambda: _safe_refresh(False))
        except Exception:
            _safe_refresh(False)
        try:
            QtCore.QTimer.singleShot(0, lambda: _safe_refresh(True))
        except Exception:
            _safe_refresh(True)

    def _launch_miner_gui(self, miner_code: str, install_dir: Path, gui_version: Optional[str]) -> bool:
        """Launch miner GUI executable; return True on success."""
        try:
            install_dir = Path(install_dir)
            if not install_dir.exists():
                try:
                    self.log_progress(f"[warning] GUI launch skipped - install path missing: {install_dir}")
                except Exception:
                    pass
                self._debug_log(f"GUI launch skipped - install path missing: {install_dir}")
                return False
            try:
                self.log_progress(f"Launching GUI from {install_dir}")
            except Exception:
                pass
            self._debug_log(f"Launching GUI from {install_dir}")
            gui_filename = naming.gui_asset(miner_code, gui_version or "unknown", windows=os.name == 'nt')
            gui_exe = install_dir / gui_filename
            if not gui_exe.exists():
                patterns: List[str] = []
                if os.name == 'nt':
                    patterns.append(f"FRY_{miner_code}_v*.exe")
                else:
                    patterns.append(f"{naming.gui_prefix(miner_code)}*")
                for pattern in patterns:
                    matches = list(install_dir.glob(pattern))
                    if matches:
                        gui_exe = matches[0]
                        break
            if not gui_exe.exists():
                try:
                    # Final fallback: recursive search for a GUI binary under install_dir
                    candidate = next(install_dir.rglob("FRY_*_v*.exe"))
                    gui_exe = candidate
                except Exception:
                    try:
                        self.log_progress("[warning] Could not locate miner GUI executable to launch")
                    except Exception:
                        pass
                    self._debug_log("GUI executable not found during recursive search")
                    return False
            if not gui_exe.exists():
                try:
                    self.log_progress(f"[warning] GUI executable missing: {gui_exe}")
                except Exception:
                    pass
                self._debug_log(f"GUI executable missing: {gui_exe}")
                return False
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == 'nt' else 0
            try:
                subprocess.Popen([str(gui_exe)], cwd=str(install_dir), creationflags=creation_flags)
                self._debug_log(f"GUI launch attempted via Popen: {gui_exe}")
            except Exception:
                if os.name == 'nt':
                    try:
                        os.startfile(str(gui_exe))
                        self._debug_log(f"GUI launch attempted via os.startfile: {gui_exe}")
                    except Exception as e2:
                        try:
                            self.log_progress(f"[warning] Failed to launch GUI: {e2}")
                        except Exception:
                            pass
                        self._debug_log(f"GUI launch failed via os.startfile: {e2}")
                        return False
                else:
                    raise
            return True
        except Exception as e:
            try:
                self.log_progress(f"[warning] Could not launch miner GUI automatically: {e}")
            except Exception:
                pass
            self._debug_log(f"GUI launch exception: {e}")
            return False

    def _maybe_launch_gui_post_install(
        self,
        miner_code: str,
        install_result: Dict[str, Any],
        install_dir_hint: Optional[str] = None,
        gui_version_hint: Optional[str] = None,
        extra_dir_hints: Optional[List[Optional[str]]] = None,
    ) -> None:
        """Best-effort GUI launch after install/update completes."""
        try:
            raw_candidates: List[Optional[str]] = [
                install_result.get("install_dir"),
                install_result.get("install_path"),
                install_dir_hint,
            ]
            if extra_dir_hints:
                raw_candidates.extend(extra_dir_hints)
            # Use the first existing directory
            chosen_dir: Optional[Path] = None
            for raw in raw_candidates:
                if not raw:
                    continue
                candidate = Path(raw)
                if candidate.exists():
                    chosen_dir = candidate
                    break
            if not chosen_dir:
                self._debug_log("GUI launch skipped - no install directory candidates available")
                return
            gui_version = install_result.get("gui_version") or gui_version_hint
            self._debug_log(f"Scheduling GUI launch. dir={chosen_dir}, version={gui_version or 'unknown'}")
            # Use a short delay to ensure binaries are fully written before launching
            # Use QTimer.singleShot with a slightly longer delay to ensure event loop is ready
            QtCore.QTimer.singleShot(1000, lambda: self._launch_miner_gui(miner_code, chosen_dir, gui_version))
        except Exception as e:
            self._debug_log(f"GUI launch scheduling failed: {e}")
            import traceback
            self._debug_log(traceback.format_exc())
            pass

    def _fetch_latest_versions_for_installations(self, installations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fetch latest GUI/PoC versions for the installed miner codes."""
        latest_versions: Dict[str, Any] = {}
        seen_codes = set()
        for install in installations:
            code = install.get('miner_code')
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            try:
                platform = "windows" if sys.platform.startswith('win') else "linux"
                ver_dict = self._get_best_version(code, platform=platform)
                if isinstance(ver_dict, dict) and (not ver_dict or "detail" in ver_dict):
                    # Empty dict or API-provided 'detail' means no versions available for this platform
                    detail_msg = ver_dict.get("detail") if isinstance(ver_dict.get("detail"), str) else None
                    continue
                if not (ver_dict.get("software_version") and ver_dict.get("poc_version")):
                    continue
                latest_versions[code] = ver_dict
            except Exception as e:
                pass
        return latest_versions

    def _build_version_warning_entries(
        self, installations: List[Dict[str, Any]], latest_versions: Dict[str, Any]
    ) -> List[str]:
        """Return warning strings for miners that are not up to date."""
        warnings: List[str] = []
        for install in installations:
            code = install.get('miner_code')
            code_key = str(code or '')
            miner_name = install.get('miner_name') or code_key
            config = install.get('config') or {}
            latest = latest_versions.get(code_key) or {}
            current_gui = config.get('gui_version')
            required_gui = latest.get('software_version')
            current_poc = config.get('poc_version')
            required_poc = latest.get('poc_version')

            if required_gui and current_gui and current_gui != "Unknown" and current_gui != required_gui:
                warnings.append(f"{miner_name}: GUI {current_gui} \u2192 {required_gui}")
            if required_poc and current_poc and current_poc != "Unknown" and current_poc != required_poc:
                warnings.append(f"{miner_name}: PoC {current_poc} \u2192 {required_poc}")
        return warnings
    
    def _get_warning_icon_data_url(self) -> str:
        """Return a cached data URL for the standard warning icon."""
        cached = getattr(self, "_warning_icon_data_url", None)
        if cached is not None:
            return cached
        try:
            icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxWarning)
            pixmap = icon.pixmap(52, 52)
            buffer = QtCore.QBuffer()
            buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
            pixmap.save(buffer, "PNG")
            # PySide6 returns a QByteArray; convert via .data() for type safety
            encoded_bytes = bytes(buffer.data().toBase64().data())
            encoded = encoded_bytes.decode("ascii")
            data_url = f"data:image/png;base64,{encoded}"
            self._warning_icon_data_url = data_url
            return data_url
        except Exception:
            self._warning_icon_data_url = ""
            return ""

    def _update_version_warning_label(self, warnings: Optional[List[str]]) -> None:
        """Update warning label text near the Manage Installed Miners and Nodes button."""
        label = getattr(self, 'version_warning_label', None)
        if not label:
            return

        if not warnings:
            label.clear()
            label.setVisible(False)
            return

        # Show a simple, non-detailed warning to avoid clutter on the main page
        icon_src = self._get_warning_icon_data_url()
        if icon_src:
            label.setText(f'<img src="{icon_src}" width="20" height="20" style="vertical-align:middle; margin-right:6px;"> <b>Updates available</b>')
        else:
            label.setText("<b>Updates available</b>")
        label.setVisible(True)

    def _handle_version_warning_update(self, warnings: Optional[List[str]]) -> None:
        """Update cached warnings, label, and tray notifications."""
        new_warnings = warnings or []
        old_warnings = self._cached_version_warnings or []
        self._cached_version_warnings = new_warnings
        self._update_version_warning_label(new_warnings if new_warnings else None)

        try:
            if not self._tray_icon or not self._tray_icon.isVisible():
                return
        except Exception:
            return

        if new_warnings and new_warnings != old_warnings:
            try:
                summary = "\n".join(new_warnings[:3])
                if len(new_warnings) > 3:
                    summary += f"\n(+{len(new_warnings) - 3} more)"
                self._tray_icon.showMessage(
                    "FryNetworks Installer",
                    f"Updates required:\n{summary}",
                    QtWidgets.QSystemTrayIcon.MessageIcon.Warning,
                    5000,
                )
            except Exception:
                pass

    def _ensure_version_timer_state(self, has_installations: bool) -> None:
        """Start or stop the periodic version check timer based on installation count."""
        if has_installations:
            if not self._version_warning_timer.isActive():
                self._version_warning_timer.start()
        else:
            if self._version_warning_timer.isActive():
                self._version_warning_timer.stop()

    def _run_version_status_timer(self) -> None:
        """Periodic timer callback to refresh version warnings."""
        try:
            config_manager = ConfigManager()
            installations = config_manager.detect_existing_installations()
            self._ensure_version_timer_state(bool(installations))
            if not installations:
                self._handle_version_warning_update([])
                return
            latest_versions = self._fetch_latest_versions_for_installations(installations)
            warnings = self._build_version_warning_entries(installations, latest_versions)
            self._handle_version_warning_update(warnings)
        except Exception as exc:
            self._update_version_warning_label([f"Unable to check versions: {exc}"])
    
    def _uninstall_installation(self, row_idx: int, use_dialog: bool = False):
        """Uninstall a miner from the table."""
        # Get table reference
        if use_dialog:
            table = getattr(self, '_dialog_installations_table', None)
        else:
            table = getattr(self, 'installations_table', None)
        
        if not table or row_idx >= table.rowCount():
            return
        
        # Get installation data from first column
        key_item = table.item(row_idx, 0)
        if not key_item:
            return
        
        install_data = key_item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not install_data:
            return
        
        # Confirm uninstallation
        reply = QtWidgets.QMessageBox.question(
            self,
            "Confirm Uninstall",
            f"Are you sure you want to uninstall {install_data['miner_name']} ({install_data['miner_code']})?\n\n"
            f"Installation directory: {install_data['install_dir']}\n\n"
            "This will:\n"
            "• Stop the miner service (if running)\n"
            "• Remove the service registration\n"
            "• Delete all miner files and configuration\n\n"
            "This action cannot be undone.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No
        )
        
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        
        # Perform uninstallation
        self._perform_uninstall(install_data, use_dialog)
    
    def _start_miner(self, row_idx: int, use_dialog: bool = False):
        """Start/restart both the miner service and GUI."""
        # Get table reference
        if use_dialog:
            table = getattr(self, '_dialog_installations_table', None)
        else:
            table = getattr(self, 'installations_table', None)
        
        if not table or row_idx >= table.rowCount():
            return
        
        # Get installation data
        key_item = table.item(row_idx, 0)
        if not key_item:
            return
        
        install_data = key_item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not install_data:
            return
        
        try:
            miner_code = install_data['miner_code']
            miner_name = install_data['miner_name']
            install_dir = Path(install_data['install_dir'])
            gui_version = install_data.get('config', {}).get('gui_version')
            
            service_manager = ServiceManager(miner_code)
            
            # Detect actual service name from install directory
            try:
                impl = getattr(service_manager, "service_manager", None)
                if impl and install_dir.exists():
                    detected_service_name = impl._detect_actual_service_name(install_dir)
                    if detected_service_name:
                        impl.service_name = detected_service_name
                        self._debug_log(f"Detected service name: {detected_service_name}")
            except Exception as detect_err:
                self._debug_log(f"Could not detect service name: {detect_err}")
            
            # Check and start service if not running
            service_status = service_manager.get_service_status()
            if service_status != "RUNNING":
                self.status_bar.setText(f"Starting {miner_name} service...")
                self._debug_log(f"[START] Service not running, starting...")
                QtWidgets.QApplication.processEvents()
                
                start_result = service_manager.start_service()
                if not start_result.get('success'):
                    self._debug_log(f"[START] Service start failed: {start_result.get('message')}")
                    QtWidgets.QMessageBox.warning(
                        self,
                        "Service Start Failed",
                        f"Failed to start service:\n\n{start_result.get('message', 'Unknown error')}"
                    )
                    return
                self._debug_log(f"[START] Service started successfully")
                self.status_bar.setText(f"Started {miner_name} service")
            else:
                self._debug_log(f"[START] Service already running")
            
            # Check if GUI is already running
            gui_exe_name = naming.gui_asset(miner_code, gui_version, windows=True) if gui_version else f"{naming.gui_prefix(miner_code)}.exe"
            gui_running = False
            try:
                import psutil
                for proc in psutil.process_iter(['name', 'exe', 'cmdline']):
                    try:
                        names = []
                        if proc.info.get('name'):
                            names.append(proc.info.get('name'))
                        if proc.info.get('exe'):
                            exe_path = str(proc.info.get('exe')).lower()
                            names.append(exe_path)
                        for n in names:
                            if gui_exe_name.lower() in str(n).lower():
                                gui_running = True
                                break
                        if gui_running:
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except Exception as psutil_err:
                self._debug_log(f"[START] Could not check GUI process: {psutil_err}")
            
            if gui_running:
                self._debug_log(f"[START] GUI {gui_exe_name} already running, skipping launch")
                self.status_bar.setText(f"{miner_name} GUI already running")
                QtWidgets.QMessageBox.information(
                    self,
                    "Already Running",
                    f"{miner_name} GUI is already running."
                )
                return
            
            # Always launch/show GUI if not running
            self.status_bar.setText(f"Launching {miner_name} GUI...")
            self._debug_log(f"[START] Launching GUI {gui_exe_name}...")
            QtWidgets.QApplication.processEvents()
            
            success = self._launch_miner_gui(miner_code, install_dir, gui_version)
            if success:
                self._debug_log(f"[START] GUI launched successfully")
                self.status_bar.setText(f"Started {miner_name}")
                # Refresh table to update status
                self._refresh_manage_views_once()
            else:
                self._debug_log(f"[START] GUI launch failed")
                QtWidgets.QMessageBox.warning(
                    self,
                    "GUI Launch Failed",
                    f"Service is running but could not launch GUI.\nThe GUI may already be open."
                )
                self.status_bar.setText(f"Service running, GUI launch failed")
                
        except Exception as e:
            import traceback
            self._debug_log(f"[START] Exception: {traceback.format_exc()}")
            QtWidgets.QMessageBox.critical(
                self,
                "Error",
                f"Failed to start miner:\n\n{str(e)}"
            )
            self.status_bar.setText("Start failed")
    
    def _update_installation(self, row_idx: int, use_dialog: bool = False):
        """Update a miner's GUI and/or PoC versions."""
        # Get table reference
        if use_dialog:
            table = getattr(self, '_dialog_installations_table', None)
        else:
            table = getattr(self, 'installations_table', None)
        
        if not table or row_idx >= table.rowCount():
            return
        
        # Get installation data
        key_item = table.item(row_idx, 0)
        if not key_item:
            return
        
        install_data = key_item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not install_data:
            return
        
        miner_key = install_data.get('config', {}).get('miner_key')
        if not miner_key:
            QtWidgets.QMessageBox.warning(
                self,
                "Update Failed",
                "Cannot update: miner key not found in installation config."
            )
            return
        
        # Confirm update
        reply = QtWidgets.QMessageBox.question(
            self,
            "Confirm Update",
            f"Update {install_data['miner_name']} ({install_data['miner_code']})?\n\n"
            "This will:\n"
            "• Stop the miner service\n"
            "• Remove the old service\n"
            "• Download and install latest versions\n"
            "• Start the updated service\n\n"
            "Do you want to continue?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No
        )
        
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        
        # Perform update
        self._perform_update(install_data, miner_key, use_dialog)
    
    def _perform_uninstall(self, install_data: Dict[str, Any], use_dialog: bool = False):
        """Perform the actual uninstallation."""
        try:
            # Create service manager for this miner
            service_manager = ServiceManager(install_data['miner_code'])
            
            # Stop and remove service
            self.status_bar.setText(f"Stopping {install_data['miner_name']} service...")
            QtWidgets.QApplication.processEvents()
            
            uninstall_result = service_manager.uninstall_service(
                install_dir=install_data['install_dir']
            )

            # Remove shortcuts/pins if present (Windows only)
            if os.name == 'nt':
                try:
                    shortcuts = self._detect_existing_shortcuts(install_data['miner_code'])
                except Exception:
                    shortcuts = {}
                for path in shortcuts.values():
                    try:
                        if path and Path(path).exists():
                            Path(path).unlink()
                            self.log_progress(f"Removed shortcut: {path}")
                    except Exception as shortcut_err:
                        self.log_progress(f"[warning] Could not remove shortcut {path}: {shortcut_err}")
            
            if not uninstall_result.get('success'):
                errors = uninstall_result.get('errors', ['Unknown error'])
                QtWidgets.QMessageBox.warning(
                    self,
                    "Uninstall Warning",
                    f"Service removal completed with warnings:\n\n" + "\n".join(errors)
                )
            
            # Remove configuration files
            config_manager = ConfigManager(install_data['miner_code'])
            config_result = config_manager.remove_configuration(
                system_wide=install_data['system_wide'],
                install_dir=install_data['install_dir']
            )
            
            if config_result.get('success'):
                QtWidgets.QMessageBox.information(
                    self,
                    "Uninstall Complete",
                    f"{install_data['miner_name']} has been successfully uninstalled."
                )
                self.status_bar.setText(f"Successfully uninstalled {install_data['miner_name']}")
            else:
                errors = config_result.get('errors', ['Unknown error'])
                QtWidgets.QMessageBox.critical(
                    self,
                    "Uninstall Failed",
                    f"Failed to remove configuration:\n\n" + "\n".join(errors)
                )
                self.status_bar.setText("Uninstall failed")
                return
            
            # Refresh the table
            self._refresh_manage_views_once()
        
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self,
                "Uninstall Error",
                f"An error occurred during uninstallation:\n\n{str(e)}"
            )
            self.status_bar.setText("Uninstall failed")
    
    def _perform_update(self, install_data: Dict[str, Any], miner_key: str, use_dialog: bool = False):
        """Perform the actual update of a miner installation."""
        try:
            self.status_bar.setText(f"Updating {install_data['miner_name']}...")
            self._debug_log(f"[UPDATE] Starting update with test_mode={self._use_test_versions}")
            QtWidgets.QApplication.processEvents()
            
            # Create service manager
            service_manager = ServiceManager(install_data['miner_code'])
            install_dir_path: Optional[Path] = None
            try:
                install_dir_path = Path(install_data['install_dir'])
            except Exception:
                install_dir_path = None

            # Snapshot existing shortcuts/pins so we can recreate them after the update
            existing_shortcuts: Dict[str, Path] = {}
            if os.name == 'nt':
                try:
                    existing_shortcuts = self._detect_existing_shortcuts(install_data['miner_code'])
                except Exception:
                    existing_shortcuts = {}

            # Align service name with on-disk installation so stop/start actions target the right service
            try:
                impl = getattr(service_manager, "service_manager", None)
                if impl and hasattr(impl, "_detect_actual_service_name") and install_dir_path:
                    detected = impl._detect_actual_service_name(install_dir_path)
                    if detected:
                        impl.service_name = detected
            except Exception:
                pass

            miner_code = install_data['miner_code']
            config_block = install_data.get('config', {}) or {}
            gui_version = config_block.get('gui_version')
            poc_version = config_block.get('poc_version')

            # Restore saved installation options if present (e.g., auto_start preference)
            options = install_data.get("options")
            if not isinstance(options, dict):
                options = install_data.get("config", {}).get("options") if isinstance(install_data.get("config", {}).get("options"), dict) else {}
            options.setdefault("auto_start", True)

            # Figure out which components actually need an update so we avoid duplicate launches/restarts
            latest_versions: Dict[str, Any] = {}
            try:
                platform = "windows" if sys.platform.startswith("win") else "linux"
                latest_versions = self._get_best_version(miner_code, platform=platform) or {}
            except Exception as version_err:
                self._debug_log(f"Version lookup failed for {miner_code}: {version_err}")

            def _needs_update(current: Optional[str], target: Optional[str]) -> bool:
                if target is None:
                    # If we don't know the target and also don't know the current version, treat as update
                    return not current or str(current).lower() == "unknown"
                if not current or str(current).lower() == "unknown":
                    return True
                return str(current).strip() != str(target).strip()

            target_gui_version = latest_versions.get("software_version")
            target_poc_version = latest_versions.get("poc_version")
            gui_update_required = _needs_update(gui_version, target_gui_version)
            poc_update_required = _needs_update(poc_version, target_poc_version)

            if not gui_update_required and not poc_update_required:
                QtWidgets.QMessageBox.information(
                    self,
                    "No Update Needed",
                    f"{install_data['miner_name']} is already on the required versions."
                )
                self.status_bar.setText("Already up to date")
                return

            if gui_version and gui_version != "Unknown":
                gui_pattern = naming.gui_asset(miner_code, gui_version)
            else:
                gui_pattern = f"{naming.gui_prefix(miner_code)}*.exe" if os.name == 'nt' else naming.gui_prefix(miner_code)
            if poc_version and poc_version != "Unknown":
                poc_pattern = naming.poc_asset(miner_code, poc_version)
            else:
                poc_pattern = f"{naming.poc_prefix(miner_code)}*.exe" if os.name == 'nt' else naming.poc_prefix(miner_code)

            # If only the GUI needs an update, handle it without disturbing the running PoC
            if gui_update_required and not poc_update_required:
                self._perform_gui_only_update(
                    miner_code=miner_code,
                    miner_key=miner_key,
                    install_data=install_data,
                    install_dir_path=install_dir_path,
                    gui_pattern=gui_pattern,
                    target_gui_version=target_gui_version or gui_version,
                    current_poc_version=poc_version,
                    existing_shortcuts=existing_shortcuts,
                    options=options,
                    service_manager=service_manager,
                )
                # Refresh the table even for GUI-only updates
                self._refresh_manage_views_once()
                return

            # Step 0: Stop the service only when the PoC is actually changing
            if poc_update_required:
                self.status_bar.setText("Stopping service...")
                QtWidgets.QApplication.processEvents()
                stop_result = service_manager.stop_service()
                if not stop_result.get("success"):
                    self.log_progress(f"[warning] Could not stop service cleanly: {stop_result.get('message', 'Unknown error')}")

                # Ensure the old PoC process is not running before we replace binaries
                self.status_bar.setText("Closing running miner components...")
                QtWidgets.QApplication.processEvents()
                self._terminate_processes_for_pattern(poc_pattern, f"{install_data['miner_name']} service")

            # Close GUI only when it will be replaced to avoid duplicate instances
            if gui_update_required:
                self._terminate_processes_for_pattern(gui_pattern, f"{install_data['miner_name']} GUI")

            # Step 1: Remove service registration and old binaries (only necessary for PoC updates)
            if poc_update_required:
                self.status_bar.setText("Removing old installation files...")
                QtWidgets.QApplication.processEvents()
                uninstall_result = service_manager.uninstall_service(
                    install_dir=install_data['install_dir'],
                    preserve_data=True,
                    preserve_gui_processes=not gui_update_required,
                )

                if not uninstall_result.get('success'):
                    errors = uninstall_result.get('errors', ['Unknown error'])
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Update Failed",
                        f"Failed to remove existing installation:\n\n" + "\n".join(errors)
                    )
                    self.status_bar.setText("Update failed")
                    return

            # Remove legacy GUI/PoC binaries now that the service is unregistered, but keep data directories
            if install_dir_path and install_dir_path.exists():
                self._remove_existing_binaries(
                    install_dir_path,
                    gui_pattern if gui_update_required else None,
                    poc_pattern if poc_update_required else None
                )

            # Step 2: Reinstall with latest versions
            self.status_bar.setText("Downloading and installing updated version...")
            QtWidgets.QApplication.processEvents()

            # Determine the platform to use for version resolution (test or production)
            platform_for_version = "test-windows" if (self._use_test_versions and sys.platform.startswith("win")) else "test-linux" if self._use_test_versions else ("windows" if sys.platform.startswith("win") else "linux")

            install_result = service_manager.install_service(
                miner_key=miner_key,
                system_wide=install_data['system_wide'],
                auto_start=True,
                install_path=install_data['install_dir'],
                version_platform=platform_for_version
            )
            gui_version_resolved = install_result.get("gui_version") or install_data.get("config", {}).get("gui_version")

            if install_result.get('success'):
                QtWidgets.QMessageBox.information(
                    self,
                    "Update Complete",
                    f"{install_data['miner_name']} has been successfully updated."
                )
                self.status_bar.setText(f"Successfully updated {install_data['miner_name']}")

                # Recreate shortcuts/pins that previously existed
                try:
                    if os.name == 'nt' and existing_shortcuts:
                        from pathlib import Path as _P
                        raw_path = (
                            install_result.get("install_dir")
                            or install_result.get("install_path")
                            or install_data.get("install_dir")
                            or ""
                        )
                        shortcut_install_path = _P(raw_path) if raw_path else None
                        gui_version_resolved = install_result.get("gui_version") or install_data.get("config", {}).get("gui_version")

                        new_desktop = None
                        new_start = None
                        if "desktop" in existing_shortcuts and shortcut_install_path:
                            try:
                                new_desktop = self._create_desktop_shortcut_for_miner(
                                    miner_code=miner_code,
                                    install_path=shortcut_install_path,
                                    gui_version=gui_version_resolved,
                                )
                                self.log_progress("Desktop shortcut refreshed")
                            except Exception as desktop_err:
                                self.log_progress(f"[warning] Could not refresh desktop shortcut: {desktop_err}")

                        if "start_menu" in existing_shortcuts and shortcut_install_path:
                            try:
                                new_start = self._create_start_menu_shortcut_for_miner(
                                    miner_code=miner_code,
                                    install_path=shortcut_install_path,
                                    gui_version=gui_version_resolved,
                                )
                                self.log_progress("Start menu shortcut refreshed")
                                self._pin_gui_to_start(
                                    miner_code=miner_code,
                                    install_path=shortcut_install_path,
                                    gui_version=gui_version_resolved,
                                    existing_shortcut=new_start,
                                )
                                self.log_progress("Start menu pin refreshed")
                            except Exception as start_err:
                                self.log_progress(f"[warning] Could not refresh Start menu shortcut/pin: {start_err}")
                except Exception as shortcut_err:
                    self.log_progress(f"[warning] Could not refresh shortcuts: {shortcut_err}")

                # Step 4: Ensure the refreshed service is running (only when PoC changed)
                if poc_update_required:
                    try:
                        impl = getattr(service_manager, "service_manager", None)
                        if impl and install_result.get("poc_version"):
                            impl.service_name = naming.poc_windows_service_name(miner_code, str(install_result.get("poc_version")))
                    except Exception:
                        pass
                    start_result = service_manager.start_service()
                    if not start_result.get("success"):
                        self.log_progress(f"[warning] Updated service did not start automatically: {start_result.get('message', 'Unknown error')}")

                # Rewrite installer configuration so Manage view retains miner key/version info
                try:
                    config_manager = ConfigManager(install_data['miner_code'])
                    write_result = config_manager.write_miner_key(
                        miner_key,
                        system_wide=install_data['system_wide'],
                        install_path=install_data['install_dir'],
                        gui_version=install_result.get('gui_version'),
                        poc_version=install_result.get('poc_version')
                    )
                    if not write_result.get("success"):
                        self.log_progress(f"[warning] Could not update installer configuration: {write_result.get('errors', ['Unknown error'])}")
                except Exception as cfg_err:
                    self.log_progress(f"[warning] Exception while updating installer configuration: {cfg_err}")

                try:
                    if gui_update_required:
                        self._maybe_launch_gui_post_install(
                            miner_code=miner_code,
                            install_result=install_result,
                            install_dir_hint=install_data.get("install_dir"),
                            gui_version_hint=gui_version_resolved,
                            extra_dir_hints=[
                                install_data.get("install_path"),
                                install_data.get("config", {}).get("install_dir"),
                                install_data.get("_resolved_install_dir"),
                            ],
                        )
                except Exception:
                    pass
            else:
                errors = install_result.get('errors') or [install_result.get('message') or 'Unknown error']
                QtWidgets.QMessageBox.critical(
                    self,
                    "Update Failed",
                    f"Failed to install updated version:\n\n" + "\n".join(errors)
                )
                self.status_bar.setText("Update failed")
                return
            
            # Refresh the table
            self._refresh_manage_views_once()
        
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            QtWidgets.QMessageBox.critical(
                self,
                "Update Error",
                f"An error occurred during update:\n\n{str(e)}"
            )
            self.status_bar.setText("Update failed")

    def _perform_gui_only_update(
        self,
        miner_code: str,
        miner_key: str,
        install_data: Dict[str, Any],
        install_dir_path: Optional[Path],
        gui_pattern: Optional[str],
        target_gui_version: Optional[str],
        current_poc_version: Optional[str],
        existing_shortcuts: Dict[str, Path],
        options: Dict[str, Any],
        service_manager: ServiceManager,
    ) -> None:
        """
        Update only the miner GUI without touching the running PoC/service.

        This avoids spawning duplicate GUI instances when the PoC is unchanged.
        """
        try:
            self.status_bar.setText(f"Updating {install_data['miner_name']} GUI...")
            QtWidgets.QApplication.processEvents()

            if gui_pattern:
                self._terminate_processes_for_pattern(gui_pattern, f"{install_data['miner_name']} GUI")

            impl = getattr(service_manager, "service_manager", None)
            if impl and install_dir_path:
                try:
                    impl.base_dir = install_dir_path
                except Exception:
                    pass

            try:
                # Prepare options with version platform for test mode support
                base_platform = "windows" if sys.platform.startswith("win") else "linux"
                version_platform = f"test-{base_platform}" if self._use_test_versions else base_platform
                copy_opts = {
                    "install_dir": str(install_dir_path) if install_dir_path else None,
                    "version_platform": version_platform
                }
                copy_ok, attempts, new_gui_version, new_poc_version = (
                    impl._copy_service_files(copy_opts)
                    if impl
                    else (False, [], None, None)
                )
            except Exception as copy_exc:
                QtWidgets.QMessageBox.critical(
                    self,
                    "Update Failed",
                    f"Failed to download GUI update:\n\n{copy_exc}"
                )
                self.status_bar.setText("Update failed")
                return

            if not copy_ok:
                QtWidgets.QMessageBox.critical(
                    self,
                    "Update Failed",
                    "Failed to download or place the updated GUI binary."
                )
                self.status_bar.setText("Update failed")
                return

            resolved_gui_version = new_gui_version or target_gui_version or install_data.get("config", {}).get("gui_version")

            # Clean up old GUI binaries now that the new one is present
            if install_dir_path and install_dir_path.exists():
                self._remove_existing_binaries(install_dir_path, gui_pattern, None)

            # Refresh shortcuts/pins that previously existed
            try:
                if os.name == 'nt' and existing_shortcuts:
                    from pathlib import Path as _P
                    raw_path = install_data.get("install_dir") or ""
                    shortcut_install_path = _P(raw_path) if raw_path else None

                    new_desktop = None
                    if "desktop" in existing_shortcuts and shortcut_install_path:
                        try:
                            new_desktop = self._create_desktop_shortcut_for_miner(
                                miner_code=miner_code,
                                install_path=shortcut_install_path,
                                gui_version=resolved_gui_version,
                            )
                            self.log_progress("Desktop shortcut refreshed")
                        except Exception as desktop_err:
                            self.log_progress(f"[warning] Could not refresh desktop shortcut: {desktop_err}")

                    new_start = None
                    if "start_menu" in existing_shortcuts and shortcut_install_path:
                        try:
                            new_start = self._create_start_menu_shortcut_for_miner(
                                miner_code=miner_code,
                                install_path=shortcut_install_path,
                                gui_version=resolved_gui_version,
                            )
                            self.log_progress("Start menu shortcut refreshed")
                            self._pin_gui_to_start(
                                miner_code=miner_code,
                                install_path=shortcut_install_path,
                                gui_version=resolved_gui_version,
                                existing_shortcut=new_start,
                            )
                            self.log_progress("Start menu pin refreshed")
                        except Exception as start_err:
                            self.log_progress(f"[warning] Could not refresh Start menu shortcut/pin: {start_err}")

                    if "taskbar" in existing_shortcuts and shortcut_install_path:
                        try:
                            pin_shortcut = new_desktop or new_start
                            if pin_shortcut is None:
                                pin_shortcut = self._create_start_menu_shortcut_for_miner(
                                    miner_code=miner_code,
                                    install_path=shortcut_install_path,
                                    gui_version=resolved_gui_version,
                                )
                            self._pin_gui_to_taskbar(
                                miner_code=miner_code,
                                install_path=shortcut_install_path,
                                gui_version=resolved_gui_version,
                                existing_shortcut=pin_shortcut,
                            )
                            self.log_progress("Taskbar pin refreshed")
                        except Exception as pin_err:
                            self.log_progress(f"[warning] Could not refresh taskbar pin: {pin_err}")
            except Exception:
                pass

            if options.get("auto_start", True):
                try:
                    from pathlib import Path as _P
                    raw_path = install_data.get("install_dir") or ""
                    startup_install_path = _P(raw_path) if raw_path else None
                    if startup_install_path:
                        startup_shortcut = self._create_startup_shortcut_for_miner(
                            miner_code=miner_code,
                            install_path=startup_install_path,
                            gui_version=resolved_gui_version,
                        )
                        self.log_progress("Configured miner GUI to launch automatically after reboot/login")
                        self._debug_log(f"Startup shortcut refreshed: {startup_shortcut}")
                    else:
                        self.log_progress("[warning] Could not determine install path for GUI autostart shortcut")
                except Exception as autostart_err:
                    self.log_progress(f"[warning] Could not configure GUI autostart: {autostart_err}")

            # Persist updated GUI version without touching the PoC entry
            try:
                config_manager = ConfigManager(miner_code)
                write_result = config_manager.write_miner_key(
                    miner_key,
                    system_wide=install_data['system_wide'],
                    install_path=install_data['install_dir'],
                    gui_version=resolved_gui_version,
                    poc_version=current_poc_version or new_poc_version,
                )
                if not write_result.get("success"):
                    self.log_progress(f"[warning] Could not update installer configuration: {write_result.get('errors', ['Unknown error'])}")
            except Exception as cfg_err:
                self.log_progress(f"[warning] Exception while updating installer configuration: {cfg_err}")

            try:
                pseudo_result = {
                    "install_dir": install_data.get("install_dir"),
                    "install_path": install_data.get("install_path"),
                    "gui_version": resolved_gui_version,
                }
                self._maybe_launch_gui_post_install(
                    miner_code=miner_code,
                    install_result=pseudo_result,
                    install_dir_hint=install_data.get("install_dir"),
                    gui_version_hint=resolved_gui_version,
                    extra_dir_hints=[
                        install_data.get("install_path"),
                        install_data.get("config", {}).get("install_dir"),
                        install_data.get("_resolved_install_dir"),
                    ],
                )
            except Exception:
                pass

            QtWidgets.QMessageBox.information(
                self,
                "Update Complete",
                f"{install_data['miner_name']} GUI has been successfully updated."
            )
            self.status_bar.setText(f"Successfully updated {install_data['miner_name']} GUI")

        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self,
                "Update Error",
                f"An error occurred during GUI update:\n\n{str(e)}"
            )
            self.status_bar.setText("Update failed")

    def _remove_existing_binaries(self, install_dir: Path, gui_pattern: Optional[str], poc_pattern: Optional[str]) -> None:
        """Remove old GUI/PoC executables so updates don't leave stale binaries."""
        try:
            patterns = [gui_pattern, poc_pattern]
            for pattern in patterns:
                if not pattern:
                    continue
                try:
                    for file_path in install_dir.glob(pattern):
                        if file_path.is_file():
                            try:
                                file_path.unlink()
                                self.log_progress(f"Removed old binary: {file_path.name}")
                            except Exception as unlink_err:
                                self.log_progress(f"[warning] Could not remove {file_path.name}: {unlink_err}")
                except Exception as glob_err:
                    self.log_progress(f"[warning] Could not enumerate files for pattern {pattern}: {glob_err}")
        except Exception:
            pass

    def _terminate_processes_for_pattern(self, image_pattern: Optional[str], friendly_name: str) -> None:
        """Force terminate processes that match the given executable pattern."""
        if not image_pattern:
            return
        try:
            if os.name == 'nt':
                cmd = ["taskkill", "/F", "/IM", image_pattern, "/T"]
            else:
                cmd = ["pkill", "-f", image_pattern]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                self.log_progress(f"{friendly_name} processes terminated")
            else:
                # Non-zero return code is fine when process wasn't running
                if proc.stderr:
                    self.log_progress(f"[info] {friendly_name}: {proc.stderr.strip()}")
        except FileNotFoundError:
            # taskkill/pkill not available - ignore
            pass
        except Exception as exc:
            self.log_progress(f"[warning] Could not terminate {friendly_name}: {exc}")
    
    def create_conflict_section(self, layout):
        """Create conflict detection display section."""
        self.conflict_group = QtWidgets.QGroupBox("Device Compatibility")
        self.conflict_group.setObjectName("layerBox")
        self.conflict_layout = QtWidgets.QVBoxLayout(self.conflict_group)
        
        # Top row: status label and Refresh button on the right
        top_row = QtWidgets.QHBoxLayout()
        self.conflict_status = QtWidgets.QLabel("Enter a miner key to check compatibility")
        top_row.addWidget(self.conflict_status)
        top_row.addStretch()
        try:
            self.conflict_refresh_btn = QtWidgets.QPushButton("Refresh")
            self.conflict_refresh_btn.setToolTip("Re-run device compatibility check for the current miner key")
            self.conflict_refresh_btn.setFixedHeight(26)
            self.conflict_refresh_btn.clicked.connect(self.refresh_conflicts)
            # Hidden initially; only show after a key has been validated or a check has been run
            try:
                self.conflict_refresh_btn.setVisible(False)
            except Exception:
                pass
            top_row.addWidget(self.conflict_refresh_btn)
        except Exception:
            # Fallback: if we cannot create the button for some environments, continue without it
            pass
        self.conflict_layout.addLayout(top_row)

        
        layout.addWidget(self.conflict_group)
    
    def create_progress_section(self, layout):
        """Create installation progress section."""
        self.progress_group = QtWidgets.QGroupBox("Installation Progress")
        self.progress_group.setObjectName("layerBox")
        self.progress_group.setVisible(False)
        progress_layout = QtWidgets.QVBoxLayout(self.progress_group)
        
        self.progress_label = QtWidgets.QLabel("")
        progress_layout.addWidget(self.progress_label)
        
        self.progress_bar = QtWidgets.QProgressBar()
        progress_layout.addWidget(self.progress_bar)
        # Step 6 specific progress (separate, shown only during downloads)
        self.step6_label = QtWidgets.QLabel("")
        self.step6_label.setVisible(False)
        progress_layout.addWidget(self.step6_label)

        self.step6_progress = QtWidgets.QProgressBar()
        self.step6_progress.setVisible(False)
        progress_layout.addWidget(self.step6_progress)
        
        # Progress log
        self.progress_log = QtWidgets.QTextEdit()
        self.progress_log.setMinimumHeight(220)
        try:
            self.progress_log.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        except Exception:
            pass
        self.progress_log.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.progress_log.setReadOnly(True)
        # Prefer monospace for aligned steps/checkmarks
        try:
            font = self.progress_log.font()
            font.setFamily("Consolas, Menlo, Monaco, 'Courier New', monospace")
            self.progress_log.setFont(font)
        except Exception:
            pass
        progress_layout.addWidget(self.progress_log)
        try:
            pl_idx = progress_layout.indexOf(self.progress_log)
            if pl_idx is not None and pl_idx >= 0:
                try:
                    progress_layout.setStretch(pl_idx, 1)
                except Exception:
                    pass
        except Exception:
            pass
        
        # Make the whole progress section expandable
        self.progress_group.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        
        layout.addWidget(self.progress_group)
        try:
            pg_idx = layout.indexOf(self.progress_group)
            if pg_idx is not None and pg_idx >= 0:
                try:
                    layout.setStretch(pg_idx, 1)
                except Exception:
                    pass
        except Exception:
            pass
    

    
    def create_status_section(self, layout):
        """Create status bar section."""
        footer_layout = QtWidgets.QVBoxLayout()
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(0)

        # Status bar with button footer
        status_container = QtWidgets.QHBoxLayout()
        status_container.setContentsMargins(0, 0, 0, 0)
        status_container.setSpacing(12)

        self.status_bar = QtWidgets.QLabel("Ready - Enter a miner key to begin")
        self.status_bar.setStyleSheet("padding: 8px;")
        status_container.addWidget(self.status_bar, 1)

        # Show Miners button in footer
        show_miners_btn = QtWidgets.QPushButton("Show Available Miners")
        show_miners_btn.setToolTip("View the list of available miners and nodes")
        show_miners_btn.setFixedHeight(32)
        show_miners_btn.setMaximumWidth(200)
        show_miners_btn.clicked.connect(self.show_welcome_page)
        status_container.addWidget(show_miners_btn, 0)

        footer_frame = QtWidgets.QFrame()
        footer_frame.setLayout(status_container)
        footer_frame.setStyleSheet("border-top: 1px solid #787a7e;")
        footer_layout.addWidget(footer_frame)

        layout.addLayout(footer_layout)
    
    def create_wizard_pages(self):
        """Create the wizard pages for the installation process."""
        # Small page subclasses that control validation via validatePage
        class KeyEntryPage(QtWidgets.QWizardPage):
            def __init__(self, parent_win: "FryNetworksInstallerWindow"):
                super().__init__()
                self.parent_win = parent_win

            def validatePage(self) -> bool:
                if getattr(self.parent_win, 'is_key_validated', False):
                    return True
                try:
                    self.parent_win.validate_key()
                except Exception:
                    pass
                return False

        class ReviewPage(QtWidgets.QWizardPage):
            def __init__(self, parent_win: "FryNetworksInstallerWindow"):
                super().__init__()
                self.parent_win = parent_win

            def validatePage(self) -> bool:
                try:
                    # If a prior installation just finished, allow Finish to close the wizard
                    if getattr(self.parent_win, "_post_install_mode", False):
                        return True
                    if getattr(self.parent_win, 'installation_thread', None):
                        return False
                    self.parent_win.install_miner()
                except Exception:
                    pass
                return False

        # Page 1: Key Entry
        key_page = KeyEntryPage(self)
        key_page.setTitle("Enter Miner Key")
        key_page.setSubTitle("Enter your FryNetworks miner key to begin installation")
        
        key_layout = QtWidgets.QVBoxLayout(key_page)
        self.create_key_section(key_layout)
        self.create_miner_info_section(key_layout)
        self.create_conflict_section(key_layout)
        key_layout.addStretch()
        self.wizard.addPage(key_page)
        
        # Page 2: Settings
        class SettingsPage(QtWidgets.QWizardPage):
            def __init__(self, parent_win: "FryNetworksInstallerWindow"):
                super().__init__()
                self.parent_win = parent_win

            def validatePage(self) -> bool:
                try:
                    self.parent_win._confirm_settings()
                except Exception:
                    pass
                return True

        settings_page = SettingsPage(self)
        settings_page.setTitle("Installation Settings")
        settings_page.setSubTitle("Configure installation options")
        
        settings_layout = QtWidgets.QVBoxLayout(settings_page)
        self.create_options_section(settings_layout)
        self.create_additional_settings(settings_layout)
        self.wizard.addPage(settings_page)
        
        # Page 3: Review & Install
        review_page = ReviewPage(self)
        # Keep a reference so we can modify the page header at runtime (e.g., hide title during install)
        self.review_page = review_page
        review_page.setTitle("Review & Install")
        review_page.setSubTitle("Review your configuration and start installation")

        review_layout = QtWidgets.QVBoxLayout(review_page)
        self.create_review_section(review_layout)
        self.create_progress_section(review_layout)

        review_layout.addStretch()
        self.wizard.addPage(review_page)

        # Initial footer button state
        try:
            nb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'NextButton', 0)))
            if nb is not None:
                nb.setEnabled(False)
                try:
                    nb.setText("Validate Key")
                except Exception:
                    pass
            fb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)))
            if fb is not None:
                fb.setEnabled(False)
            # Hook Cancel button for rollback-enabled cancellation
            cb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'CancelButton', 0)))
            if cb is not None:
                try:
                    cb.clicked.connect(self._on_cancel_clicked)
                except Exception:
                    pass
        except Exception:
            pass

        # Set page validation
        key_page.registerField("key*", self.key_input)
    
    def create_review_section(self, layout):
        """Create review section for the final page."""
        review_group = QtWidgets.QGroupBox("Installation Summary")
        review_group.setObjectName("layerBox")
        review_layout = QtWidgets.QVBoxLayout(review_group)
        
        self.review_label = QtWidgets.QLabel("Review your settings before proceeding...")
        self.review_label.setWordWrap(True)
        review_layout.addWidget(self.review_label)
        review_layout.addStretch()
        layout.addWidget(review_group)
    
    def on_wizard_page_changed(self, page_id: int):
        """Handle wizard page changes."""
        # Clear progress indicators when navigating away from install page
        if page_id != 2:  # Not on the Review & Install page
            self._clear_installation_progress()
        
        if page_id == 0:  # Key entry page
            self.status_bar.setText("Enter a miner key and validate it")
            
            # Hide "Install Another Miner" button on key entry page
            try:
                if hasattr(self, 'install_another_button') and self.install_another_button is not None:
                    self.install_another_button.setVisible(False)
            except Exception:
                pass
                
        elif page_id == 1:  # Settings page
            # Check cached conflicts - if present, user clicked "Try Another Key >"
            # Go back to page 0 and clear the form
            try:
                cached = getattr(self, '_last_conflicts', None)
                if cached and cached.get("has_conflicts"):
                    self.wizard.back()
                    self.clear_form()
                    self.status_bar.setText("Enter a new miner key and validate it")
                    return
            except Exception:
                pass
            
            self.status_bar.setText("Configure installation settings")
            self.update_review_summary()
            
            # Hide "Install Another Miner" button on settings page
            try:
                if hasattr(self, 'install_another_button') and self.install_another_button is not None:
                    self.install_another_button.setVisible(False)
            except Exception:
                pass
                
        elif page_id == 2:  # Review & Install page
            self.status_bar.setText("Review configuration and start installation")
            
            # Hide "Install Another Miner" button when entering the page
            try:
                if hasattr(self, 'install_another_button') and self.install_another_button is not None:
                    self.install_another_button.setVisible(False)
            except Exception:
                pass
            
            # Show Back button (may have been hidden after successful install)
            try:
                bb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'BackButton', 0)))
                if bb is not None:
                    bb.setVisible(True)
            except Exception:
                pass
            
            # Set the Finish button to "Install" when entering the page
            try:
                fb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)))
                if fb is not None:
                    # Ensure Finish button is restored to "Install" behavior (remove any prior close binding)
                    try:
                        fb.clicked.disconnect(self.close)
                    except Exception:
                        pass
                    self.wizard.setButtonText(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)), "Install")
                    # Don't disconnect - let the wizard handle the click automatically
                    # The wizard will call ReviewPage.validatePage() which triggers install_miner()
                    fb.setEnabled(True)
            except Exception:
                pass
            
            # Restore page titles if they were hidden during installation
            try:
                if getattr(self, 'review_page', None) is not None:
                    if getattr(self, '_review_orig_title', None) is not None:
                        self.review_page.setTitle(str(self._review_orig_title))
                    if getattr(self, '_review_orig_subtitle', None) is not None:
                        self.review_page.setSubTitle(str(self._review_orig_subtitle))
            except Exception:
                pass
            
            # Restore original wizard stylesheet if it was modified
            try:
                if getattr(self, '_wizard_orig_qss', None) is not None:
                    try:
                        self.wizard.setStyleSheet(str(self._wizard_orig_qss))
                    except Exception:
                        pass
            except Exception:
                pass
            
            # Restore review summary visibility (may have been hidden during previous install)
            try:
                if hasattr(self, "review_label"):
                    parent_group = self.review_label.parentWidget()
                    if parent_group is not None:
                        parent_group.setVisible(True)
            except Exception:
                pass
            
            # Hide progress group when entering the page (show only after installation starts)
            try:
                if hasattr(self, "progress_group"):
                    self.progress_group.setVisible(False)
            except Exception:
                pass
            
            try:
                self.update_review_summary()
            except Exception:
                pass
            try:
                fb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)))
                if fb is not None:
                    fb.setEnabled(bool(getattr(self, 'is_key_validated', False)))
            except Exception:
                pass
    
    def update_review_summary(self):
        """Update the review summary on the final page."""
        if not hasattr(self, 'review_label'):
            return
        
        # The QGroupBox already shows the 'Installation Summary' title,
        # so keep the review text concise and avoid repeating the header.
        summary = ""

        if self.current_miner_info:
            summary += f"Miner: {self.current_miner_info.get('name', '')} ({self.current_miner_info.get('code', '')})<br>"
        
        key_text = self.key_input.text().strip()
        if key_text:
            from html import escape
            summary += f"Key: {escape(key_text)}<br>"
        
        summary += f"Auto-start (service + GUI): {'Yes' if self.auto_start.isChecked() else 'No'}<br>"
        if hasattr(self, 'desktop_shortcut'):
            summary += f"Desktop shortcut: {'Yes' if self.desktop_shortcut.isChecked() else 'No'}<br>"
        if hasattr(self, 'pin_start_checkbox'):
            summary += f"Start menu pin: {'Yes' if self.pin_start_checkbox.isChecked() else 'No'}<br>"

        # Partner integrations status summaries - only shown for Bandwidth Miner (BM)
        def _partner_status(label: str, available: bool, checkbox: Optional[QtWidgets.QCheckBox]) -> str:
            if not checkbox:
                return ""
            if not available:
                return f"{label}: Disabled (credentials unavailable)<br>"
            return f"{label}: {'Enabled' if checkbox.isChecked() else 'Disabled'}<br>"

        try:
            miner_code = str(self.current_miner_info.get('code') or '').upper() if self.current_miner_info else ''
        except Exception:
            miner_code = ''

        if miner_code == 'BM':
            # Bandwidth sharing tools info is now shown in a simple info box
            # All SDKs are staged by default, none activated (GUI handles activation)
            pass
        
        self.review_label.setText(summary)
    
    def show_help(self):
        """Show help for the current wizard page."""
        current_page = self.wizard.currentPage()
        if current_page:
            title = current_page.title()
            if "Key" in title:
                QtWidgets.QMessageBox.information(
                    self, "Help - Enter Miner Key",
                    "Enter your FryNetworks miner key in the format BM-ABC123XYZ.\n\n"
                    "The key will be validated automatically and checked for conflicts."
                )
            elif "Settings" in title:
                QtWidgets.QMessageBox.information(
                    self, "Help - Installation Settings",
                    "Configure how the miner should be installed:\n\n"
                    "• Auto-start: Start miner service and GUI automatically on boot/login\n"
                    "• Desktop shortcut: Create a shortcut to the miner GUI\n"
                    "• Screen size: GUI display size preference"
                )
            elif "Review" in title:
                QtWidgets.QMessageBox.information(
                    self, "Help - Review & Install",
                    "Review your configuration and click 'Install Miner' to begin.\n\n"
                    "The installation will download required files and set up the service."
                )
    
    def apply_theme(self):
        """Apply FryNetworks theme."""
        theme = Theme()
        extra_qss = """
        QWizard {
            background: #1f2937;
        }
        QWizardPage {
            background: #1f2937;
            color: #e5e7eb;
        }
        QGroupBox {
            border: 1px solid #3a3a3a;
            border-radius: 8px;
            margin-top: 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
            color: #cbd5e1;
        }
        /* Global QPushButton styling to match manage action buttons */
        QPushButton {
            background: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, stop:0 #ef4444, stop:1 #dc2626);
            color: white;
            border: none;
            border-radius: 8px;
            padding: 6px 12px;
            min-width: 120px;
            font-weight: 600;
        }
        QPushButton:hover {
            background: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, stop:0 #f87171, stop:1 #ef4444);
        }
        QPushButton:disabled {
            background: #6b7280;
            color: #e5e7eb;
        }

        /* Manage panel action buttons (Back and Uninstall) - preserve identical look */
        QPushButton#manageBackButton, QPushButton#manageUninstallButton {
            background: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, stop:0 #ef4444, stop:1 #dc2626);
            color: white;
            border: none;
            border-radius: 8px;
            padding: 6px 12px;
            min-width: 140px;
            font-weight: 600;
        }
        QPushButton#manageBackButton:hover, QPushButton#manageUninstallButton:hover {
            background: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, stop:0 #f87171, stop:1 #ef4444);
        }
        QPushButton#manageBackButton:disabled, QPushButton#manageUninstallButton:disabled {
            background: #6b7280;
            color: #e5e7eb;
        }
        """
        self.setStyleSheet(theme.qss() + extra_qss)

    def show_manage_dialog(self):
        """Show the manage installed miners and Nodes dialog."""
        if self.manage_dialog is None:
            self.manage_dialog = QtWidgets.QDialog(self)
            self.manage_dialog.setWindowTitle("Manage Installed Miners and Nodes")
            self.manage_dialog.setModal(True)
            layout = QtWidgets.QVBoxLayout(self.manage_dialog)
            self.create_manage_section(layout, persistent=False, dialog=self.manage_dialog)
            self.manage_dialog.resize(600, 400)
        else:
            # Refresh list whenever dialog is re-opened
            try:
                self._refresh_installations(use_dialog=True)
            except Exception:
                pass
        
        self.manage_dialog.show()
        self.manage_dialog.raise_()
        self.manage_dialog.activateWindow()

    def show_manage_panel(self):
        """Show the manage installed miners and nodes panel in the main window."""
        try:
            mp_attr = getattr(self, 'manage_panel', None)
            panel_visible = isinstance(mp_attr, QtWidgets.QWidget) and mp_attr.isVisible()
            if not panel_visible:
                try:
                    self._status_before_manage = self.status_bar.text()
                except Exception:
                    self._status_before_manage = None

            if isinstance(mp_attr, QtWidgets.QWidget):
                mp = mp_attr
                if panel_visible:
                    mp.raise_()
                    mp.activateWindow()
                    self.status_bar.setText("Viewing installed miners and nodes")
                    return
            else:
                mp = QtWidgets.QWidget()
                panel_layout = QtWidgets.QVBoxLayout(mp)
                self.create_manage_section(panel_layout)
                mp.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Expanding,
                    QtWidgets.QSizePolicy.Policy.Expanding,
                )
                self.manage_panel = mp

            wizard_index = -1
            try:
                wizard_index = self.main_layout.indexOf(self.wizard)
            except Exception:
                wizard_index = -1

            if self.main_layout.indexOf(mp) == -1:
                if wizard_index is not None and wizard_index >= 0:
                    self._wizard_index = wizard_index
                    self.main_layout.insertWidget(wizard_index, mp)
                else:
                    self.main_layout.addWidget(mp)
            else:
                # Refresh list when re-opening existing panel
                try:
                    self._refresh_installations()
                except Exception:
                    pass

            try:
                self.wizard.hide()
            except Exception:
                pass

            mp.setVisible(True)
            mp.raise_()
            mp.activateWindow()
            self.status_bar.setText("Viewing installed miners and nodes")

        except Exception:
            try:
                self.show_manage_dialog()
                self.status_bar.setText("Viewing installed miners and nodes")
            except Exception:
                pass

    def hide_manage_panel(self):
        """Hide the in-window manage panel and restore the wizard."""
        try:
            mp = getattr(self, 'manage_panel', None)
            if mp is None:
                return

            try:
                mp.hide()
            except Exception:
                try:
                    mp.setVisible(False)
                except Exception:
                    pass

            try:
                if getattr(self, '_wizard_index', None) is not None and self._wizard_index is not None and self._wizard_index >= 0:
                    if self.main_layout.indexOf(self.wizard) == -1:
                        self.main_layout.insertWidget(self._wizard_index, self.wizard)
                else:
                    if self.main_layout.indexOf(self.wizard) == -1:
                        self.main_layout.addWidget(self.wizard)

                try:
                    self.wizard.setVisible(True)
                except Exception:
                    pass
                try:
                    self.wizard.raise_()
                except Exception:
                    pass
                try:
                    self.wizard.activateWindow()
                except Exception:
                    pass
            except Exception:
                pass

        except Exception:
            try:
                self.show_manage_dialog()
            except Exception:
                pass
        try:
            restored_status = getattr(self, "_status_before_manage", None)
            if restored_status:
                self.status_bar.setText(restored_status)
            else:
                self.status_bar.setText("Ready - Enter a miner key to begin")
        except Exception:
            pass
        self._status_before_manage = None

    def _setup_tray_icon(self):
        """Initialize system tray icon with context menu."""
        if self._tray_icon is not None:
            self._slog.info("_setup_tray_icon(): already created, skipping")
            return

        try:
            if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
                self._slog.warning("_setup_tray_icon(): system tray not available")
                return
        except Exception:
            self._slog.warning("_setup_tray_icon(): isSystemTrayAvailable() raised exception")
            return

        icon = getattr(self, "_app_icon", None)
        if not icon or icon.isNull():
            icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon)

        self._slog.info("_setup_tray_icon(): creating QSystemTrayIcon")
        tray = QtWidgets.QSystemTrayIcon(icon, self)
        tray.setToolTip("Fry Installer - Install and update Fry miners and nodes")

        menu = QtWidgets.QMenu(self)
        show_action = menu.addAction("Show FryNetworks Installer")
        autostart_action = menu.addAction("Launch installer on login")
        autostart_action.setCheckable(True)
        autostart_action.setChecked(self._is_installer_autostart_enabled())
        task_autostart_action = None
        if os.name == 'nt':
            task_autostart_action = menu.addAction("Launch installer on login (all users, Scheduled Task)")
            task_autostart_action.setCheckable(True)
            task_autostart_action.setChecked(self._is_installer_task_autostart_enabled())
        exit_action = menu.addAction("Exit FryNetworks Installer")
        show_action.triggered.connect(self._restore_from_tray)
        autostart_action.triggered.connect(lambda checked: self._toggle_installer_autostart(checked))
        if task_autostart_action:
            task_autostart_action.triggered.connect(lambda checked: self._toggle_installer_task_autostart(checked))
        exit_action.triggered.connect(self._exit_from_tray)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._slog.info("_setup_tray_icon(): tray.show() called — tray icon now visible")
        self._tray_icon = tray

    def _restore_from_tray(self):
        """Restore window when user selects Show from tray."""
        try:
            if self._reset_on_restore:
                try:
                    self.hide_manage_panel()
                except Exception:
                    pass
                try:
                    self.clear_form()
                except Exception:
                    pass
                self._reset_on_restore = False
        except Exception:
            pass
        self.showNormal()
        self.raise_()
        self.activateWindow()
        try:
            status_text = "Enter a miner key and validate it" if getattr(self, '_reset_on_restore', False) else "FryNetworks Installer is active."
            self.status_bar.setText(status_text)
        except Exception:
            pass

    def _exit_from_tray(self):
        """Completely exit the application from tray menu."""
        self._allow_close = True
        if self._tray_icon:
            self._tray_icon.hide()
        app = None
        try:
            app = QtWidgets.QApplication.instance()
        except Exception:
            app = None
        if app:
            QtCore.QTimer.singleShot(0, app.quit)
        self.close()

    def _on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason):
        """Handle tray icon click/double-click."""
        try:
            if reason in (
                QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
                QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
            ):
                self._restore_from_tray()
        except Exception:
            pass

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Intercept close events to minimize to tray unless explicitly exiting.
        
        Only minimize to tray if the user has progressed past the welcome screen
        (clicked "Get Started"). If they close from the welcome screen, fully exit.
        """
        try:
            # Only minimize to tray if we've moved past the welcome screen
            welcome_closed = getattr(self, '_welcome_closed_by_user', False)
            if not self._allow_close and self._tray_icon and self._tray_icon.isVisible() and welcome_closed:
                event.ignore()
                self.hide()
                self._reset_on_restore = True
                if not self._tray_message_shown:
                    try:
                        self._tray_icon.showMessage(
                            "Fry Networks Hub",
                            "Install and update Fry miners and nodes. Right-click the tray icon to exit.",
                            QtWidgets.QSystemTrayIcon.MessageIcon.Information,
                            4000,
                        )
                        self._tray_message_shown = True
                    except Exception:
                        pass
                return
        except Exception:
            pass
        super().closeEvent(event)
    
    # ---- Installer autostart helpers (tray menu) ----
    def _installer_startup_shortcut_path(self) -> Path:
        """Return the per-user autostart entry path for the installer."""
        if os.name == 'nt':
            return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / \
                "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Fry Installer.lnk"
        if sys.platform.startswith("linux"):
            cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            return cfg / "autostart" / "fry-installer.desktop"
        return Path()

    def _installer_executable_path(self) -> Optional[Path]:
        """Best-effort path to the installer executable/script."""
        try:
            if getattr(sys, "frozen", False):
                return Path(sys.executable)
            return Path(sys.argv[0]).resolve()
        except Exception:
            return None

    def _is_installer_autostart_enabled(self) -> bool:
        try:
            path = self._installer_startup_shortcut_path()
            return bool(path and path.exists())
        except Exception:
            return False

    def _toggle_installer_autostart(self, enabled: bool) -> None:
        """Create or remove installer autostart entry."""
        if enabled:
            success, message = self._enable_installer_autostart()
        else:
            success, message = self._disable_installer_autostart()
        try:
            self.status_bar.setText(message or "")
        except Exception:
            pass
        try:
            self._debug_log(f"Installer autostart set to {enabled}: {message}")
        except Exception:
            pass
        if not success:
            try:
                QtWidgets.QMessageBox.warning(self, "Autostart", message or "Could not update autostart")
            except Exception:
                pass

    def _enable_installer_autostart(self) -> tuple[bool, str]:
        path = self._installer_startup_shortcut_path()
        exe_path = self._installer_executable_path()
        if not path or not exe_path:
            return False, "Could not determine installer path for autostart."

        if os.name == 'nt':
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._create_windows_shortcut(
                    shortcut_path=path,
                    target_path=exe_path,
                    working_dir=exe_path.parent,
                    description="Fry Installer"
                )
                return True, "Installer will launch automatically on login."
            except Exception as e:
                return False, f"Failed to enable autostart: {e}"

        if sys.platform.startswith("linux"):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                desktop_contents = "\n".join([
                    "[Desktop Entry]",
                    "Type=Application",
                    "Name=Fry Installer",
                    f"Exec={exe_path}",
                    f"Path={exe_path.parent}",
                    "X-GNOME-Autostart-enabled=true",
                    "Terminal=false",
                    "Hidden=false",
                ])
                path.write_text(desktop_contents, encoding="utf-8")
                return True, "Installer will launch automatically on login."
            except Exception as e:
                return False, f"Failed to enable autostart: {e}"

        return False, "Autostart is not supported on this platform."

    def _disable_installer_autostart(self) -> tuple[bool, str]:
        path = self._installer_startup_shortcut_path()
        try:
            if path and path.exists():
                path.unlink()
            return True, "Installer will no longer launch on login."
        except Exception as e:
            return False, f"Failed to disable autostart: {e}"

    # ---- Scheduled Task autostart (Windows, all users) ----
    def _installer_task_name(self) -> str:
        return "FryInstallerAutoStart"

    def _is_installer_task_autostart_enabled(self) -> bool:
        if os.name != 'nt':
            return False
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", self._installer_task_name()],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _toggle_installer_task_autostart(self, enabled: bool) -> None:
        if os.name != 'nt':
            return
        if enabled:
            success, message = self._enable_installer_task_autostart()
        else:
            success, message = self._disable_installer_task_autostart()
        try:
            self.status_bar.setText(message or "")
        except Exception:
            pass
        try:
            self._debug_log(f"Installer scheduled task autostart set to {enabled}: {message}")
        except Exception:
            pass
        if not success:
            try:
                QtWidgets.QMessageBox.warning(self, "Autostart", message or "Could not update autostart task")
            except Exception:
                pass

    def _enable_installer_task_autostart(self) -> tuple[bool, str]:
        exe_path = self._installer_executable_path()
        if not exe_path:
            return False, "Could not determine installer path for scheduled task."
        try:
            quoted_tr = f'"{exe_path}"'
            cmd = [
                "schtasks",
                "/Create",
                "/TN",
                self._installer_task_name(),
                "/SC",
                "ONLOGON",
                "/TR",
                quoted_tr,
                "/RL",
                "HIGHEST",
                "/RU",
                "SYSTEM",
                "/F",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            if result.returncode != 0:
                return False, f"Failed to create autostart task: {result.stderr or result.stdout or 'unknown error'}"
            return True, "Scheduled task created: installer will launch for all users at logon."
        except Exception as e:
            return False, f"Failed to create autostart task: {e}"

    def _disable_installer_task_autostart(self) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["schtasks", "/Delete", "/TN", self._installer_task_name(), "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0 and "Cannot find the file" not in (result.stderr or ""):
                return False, f"Failed to delete autostart task: {result.stderr or result.stdout or 'unknown error'}"
            return True, "Scheduled task removed: installer will not launch at logon."
        except Exception as e:
            return False, f"Failed to delete autostart task: {e}"
    
    def on_key_changed(self, text: str):
        """Handle real-time key input changes - only validate format, no API calls."""
        # Reset validation state when key changes
        self.is_key_validated = False
        
        # Show/hide Clear button based on whether there's text
        if hasattr(self, 'clear_button'):
            self.clear_button.setVisible(bool(text.strip()))
        
        if len(text) < 3:
            self.current_miner_info = None
            self.key_status.setText("")
            self.miner_info_label.setText("")
            self.miner_group.setVisible(False)
            self.conflict_status.setText("Enter a miner key to check compatibility")
            self.conflict_status.setStyleSheet("color: #b0b0b0;")
            # Ensure both the wizard Next/Validate button and the footer Validate
            # control are disabled when there's no input
            try:
                self._set_validate_button_enabled(False)
            except Exception:
                pass
            try:
                fb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)))
                if fb is not None:
                    fb.setEnabled(False)
            except Exception:
                pass
            try:
                # Ensure Next button text reads 'Validate Key' in all cases
                try:
                    self.wizard.setButtonText(cast(Any, getattr(QtWidgets.QWizard, 'NextButton', 0)), "Validate Key")
                except Exception:
                    # ignore if cannot set
                    pass
            except Exception:
                pass
            self._update_partner_section_visibility()
            return
        
        # Only parse key format locally - no API calls
        result = self.parser.parse_miner_key(text)
        
        if result["valid"]:
            self.current_miner_info = result
            self._update_partner_section_visibility()
            
            # Update key status
            self.key_status.setText(f"✓ Valid {result['name']} key format")
            self.key_status.setStyleSheet("color: #22c55e; font-weight: bold;")
            
            # Update miner info
            info_parts = [
                f"<b>Miner Type:</b> {result['name']}",
                f"<b>Code:</b> {result['code']}",
            ]
            # Show group only when exclusivity applies (avoid clutter for non-exclusive miners)
            if result.get("exclusive") and result.get("group"):
                info_parts.append(f"<b>Group:</b> {result['group']}")
            if result.get("exclusive"):
                info_parts.append(f"<b>Exclusive with:</b> {result['exclusive']}")

            info_text = "<br>".join(info_parts)
            
            self.miner_info_label.setText(info_text)
            self.miner_group.setVisible(True)
            
            # Do NOT check conflicts here - only validate format
            self.conflict_status.setText("Click 'Validate Key' to check compatibility")
            self.conflict_status.setStyleSheet("")
            
            # Start with footer Validate disabled; only enable after availability check
            try:
                try:
                    self._sync_validate_button_state(False)
                except Exception:
                    pass
                fb = self.wizard.button(self.FINISH_BUTTON)
                if fb is not None:
                    fb.setEnabled(False)
            except Exception:
                pass
            self.status_bar.setText(f"Ready to validate {result['name']} key")
            # Check miner availability against supported offerings for this OS
            try:
                # Ensure we have the latest supported lists (lazy-load if needed)
                self._ensure_supported_offerings_loaded()
                code = str(result.get('code') or '').upper()
                platform = 'windows' if getattr(self, '_is_windows', False) else 'linux'
                available = False
                if platform == 'windows':
                    available = code in getattr(self, '_supported_windows_codes', set())
                else:
                    available = code in getattr(self, '_supported_linux_codes', set())

                if not available:
                    # Show explicit error and disable navigation
                    self.key_status.setText(f"✗ {result['name']} ({code}) is not available for {platform.capitalize()} installers")
                    self.key_status.setStyleSheet("color: #ef4444; font-weight: bold;")
                    try:
                        try:
                            self._sync_validate_button_state(False)
                        except Exception:
                            pass
                    except Exception:
                        pass
                    try:
                        fb = self.wizard.button(self.FINISH_BUTTON)
                        if fb is not None:
                            fb.setEnabled(False)
                    except Exception:
                        pass
                    self.conflict_status.setText("Selected miner is not available for this OS")
                    self.conflict_status.setStyleSheet("color: #ef4444; font-weight: bold;")
                    self.status_bar.setText("Miner not available for this OS")
                else:
                    # Miner is available - ensure Next button is enabled
                    try:
                        self._sync_validate_button_state(True)
                    except Exception:
                        pass
            except Exception:
                # If availability check fails, do not block validation - keep current messages
                pass
            
        else:
            # Invalid key
            self.current_miner_info = None
            self.key_status.setText(f"\u2717 {result['error']}")
            self.key_status.setStyleSheet("color: #ef4444; font-weight: bold;")
            self.miner_info_label.setText("")
            self.miner_group.setVisible(False)
            self.conflict_status.setText("Invalid miner key format")
            self.conflict_status.setStyleSheet("color: #ef4444; font-weight: bold;")
            try:
                try:
                    self._sync_validate_button_state(False)
                except Exception:
                    pass
                fb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)))
                if fb is not None:
                    fb.setEnabled(False)
            except Exception:
                pass
            self.status_bar.setText("Invalid key format")
            self._update_partner_section_visibility()

    def update_conflict_display(self, conflicts: dict):
        """Update conflict display after validation."""
        if "error" in conflicts:
            self.conflict_status.setStyleSheet("color: #ef4444; font-weight: bold;")
            self.conflict_status.setText(f"⚠ Error checking conflicts: {conflicts['error']}")
            return

        # Special hard block: VM environment detected
        try:
            vm_block = any(d.get("type") == "vm_environment" for d in conflicts.get("details", []))
        except Exception:
            vm_block = False
        if vm_block:
            # Collect limited evidence lines
            evidence_lines = []
            try:
                for d in conflicts.get("details", []):
                    if d.get("type") == "vm_environment":
                        ev = d.get("evidence") or []
                        for e in ev:
                            if isinstance(e, str) and e.strip():
                                evidence_lines.append(f"&nbsp;&nbsp;• {e.strip()[:160]}")
            except Exception:
                pass
            if not evidence_lines:
                evidence_lines.append("&nbsp;&nbsp;• (no specific indicators listed)")
            evidence_html = "<br>".join(evidence_lines[:8])  # cap list to avoid overflow
            block_msg = (
                "<b style='color:#ef4444;'>Installation Blocked: Virtual Machine Detected</b><br>"
                "FryNetworks miners/nodes must run on physical hardware. The environment appears to be a VM.<br><br>"
                "<b>Detected indicators:</b><br>" + evidence_html + "<br><br>"
                "<i>Action:</i> Shut down the VM and run the installer directly on a physical machine."
            )
            self.conflict_status.setStyleSheet("color:#ef4444; font-weight:bold;")
            self.conflict_status.setText(block_msg)
            # Disable navigation buttons explicitly
            try:
                nb = self.wizard.button(self.NEXT_BUTTON)
                if nb is not None:
                    nb.setEnabled(False)
                    nb.setText("Blocked (VM)")
                fb = self.wizard.button(self.FINISH_BUTTON)
                if fb is not None:
                    fb.setEnabled(False)
            except Exception:
                pass
            return
        
        if conflicts.get("has_conflicts"):
            # Show conflicts
            conflict_text = "⚠ <b>Conflicts detected:</b><br>"
            for detail in conflicts["details"]:
                severity_color = "#ef4444" if detail["severity"] == "error" else "#f59e0b"
                conflict_text += f'<span style="color: {severity_color};">• {detail["message"]}</span><br>'
            
            self.conflict_status.setStyleSheet("")
            self.conflict_status.setText(conflict_text)
            
            # When conflicts detected, change Next button to suggest trying another key
            try:
                nb = self.wizard.button(self.NEXT_BUTTON)
                if nb is not None:
                    self.wizard.setButtonText(cast(Any, getattr(QtWidgets.QWizard, 'NextButton', 0)), "< Try Another Key")
            except Exception:
                pass
            
        else:
            # No conflicts
            self.conflict_status.setStyleSheet("color: #22c55e; font-weight: bold;")
            self.conflict_status.setText("<span style=\"color: #22c55e;\">✓ <b>No conflicts detected</b> - Device ready for installation</span>")
            
            # Reset Next button text to default
            try:
                self.wizard.setButtonText(cast(Any, getattr(QtWidgets.QWizard, 'NextButton', 0)), "Next >")
            except Exception:
                pass
    
    def validate_key(self):
        """Validate key with detailed checking - calls API and checks conflicts.

        Local/instant checks run on the main thread.  All network calls
        (supported-offerings, version-pair, conflict detection) are dispatched
        to a background thread so the GUI stays responsive.
        """
        key = self.key_input.text().strip()
        if not key:
            return

        # Prevent double-click while a validation is already running
        if self._validation_thread is not None and self._validation_thread.is_alive():
            return

        self.status_bar.setText("Validating key...")

        # Local parse of key format (instant, no network)
        try:
            result = self.parser.parse_miner_key(key)
        except Exception:
            QtWidgets.QMessageBox.critical(self, "Validation Error", "Failed to parse miner key format.")
            self.status_bar.setText("Validation failed")
            return

        if not result.get("valid"):
            QtWidgets.QMessageBox.critical(
                self, "Validation Error",
                f"Invalid miner key:\n\n{result.get('error', 'Unknown format error')}"
            )
            self.status_bar.setText("Validation failed")
            return

        # Disable Validate button while background work runs
        try:
            self._sync_validate_button_state(False)
        except Exception:
            pass

        # Launch the network-heavy work on a background thread
        self._start_validation_thread(key, result)

    # ---- background validation helpers ----

    def _start_validation_thread(self, key: str, parsed_result: dict):
        """Run availability + conflict checks in a daemon thread."""
        code = str(parsed_result.get('code') or '').upper()
        platform = 'windows' if getattr(self, '_is_windows', False) else 'linux'
        use_test = getattr(self, "_use_test_versions", False)

        def _worker():
            out: Dict[str, Any] = {"parsed": parsed_result, "key": key, "code": code, "platform": platform}
            try:
                # 1. Load supported offerings (up to 4 API calls, cached after first)
                self._ensure_supported_offerings_loaded()

                # 2. Check availability on this OS
                if platform == 'windows':
                    available = code in getattr(self, '_supported_windows_codes', set())
                else:
                    available = code in getattr(self, '_supported_linux_codes', set())
                if not available:
                    out["unavailable"] = True
                    self._invoke_validation_done.emit(out)
                    return

                # 3. Check version pair completeness
                if not self._has_complete_version_pair(code, platform, use_test=use_test):
                    out["incomplete_version"] = True
                    self._invoke_validation_done.emit(out)
                    return

                # 4. Check conflicts (the heaviest call)
                self._debug_log(f"[validate_key] Calling check_device_conflicts for key: {key[:8]}...")
                try:
                    conflicts = self.detector.check_device_conflicts(key)
                except Exception as e:
                    conflicts = {"error": str(e)}
                    self._debug_log(f"[validate_key] check_device_conflicts EXCEPTION: {e}")
                self._debug_log(f"[validate_key] Conflict result: {conflicts}")
                out["conflicts"] = conflicts

            except Exception as exc:
                out["error"] = str(exc)

            # Deliver result to main thread
            try:
                self._invoke_validation_done.emit(out)
            except Exception:
                QtCore.QTimer.singleShot(0, lambda r=out: self._on_validation_done(r))

        self._validation_thread = threading.Thread(target=_worker, daemon=True)
        self._validation_thread.start()

    @QtCore.Slot(dict)
    def _on_validation_done(self, result: dict):
        """Handle validation result on the main thread (signal handler)."""
        self._validation_thread = None

        # Re-enable refresh button if it exists
        try:
            if getattr(self, 'conflict_refresh_btn', None) is not None:
                self.conflict_refresh_btn.setEnabled(True)
        except Exception:
            pass

        # --- Refresh-only path (from refresh_conflicts) ---
        if result.get("refresh_only"):
            conflicts = result.get("conflicts", {})
            self._last_conflicts = conflicts
            if "error" in conflicts:
                try:
                    self.conflict_status.setStyleSheet("color: #ef4444; font-weight: bold;")
                    self.conflict_status.setText(f"⚠ Error checking conflicts: {conflicts['error']}")
                    self.status_bar.setText("Compatibility check failed")
                except Exception:
                    pass
                return
            try:
                self.update_conflict_display(conflicts)
            except Exception:
                pass
            try:
                self.status_bar.setText("Compatibility check completed")
            except Exception:
                pass
            try:
                fb = self.wizard.button(self.FINISH_BUTTON)
                if fb is not None:
                    fb.setEnabled(not bool(conflicts.get("has_conflicts")))
            except Exception:
                pass
            return

        parsed = result.get("parsed", {})
        code = result.get("code", "")
        platform = result.get("platform", "windows")
        platform_name = platform.capitalize()

        # --- Availability block ---
        if result.get("unavailable"):
            QtWidgets.QMessageBox.warning(
                self,
                "Miner Unsupported",
                f"The selected miner ({parsed.get('name')} / {code}) is not available for {platform_name} installers.\n\n"
                "This installer cannot validate or install unsupported miners."
            )
            try:
                self.key_status.setText(f"✗ {parsed.get('name')} ({code}) is not available for {platform_name} installers")
                self.key_status.setStyleSheet("color: #ef4444; font-weight: bold;")
            except Exception:
                pass
            try:
                self._sync_validate_button_state(False)
            except Exception:
                pass
            self.status_bar.setText("Validation blocked: miner not available for this OS")
            return

        # --- Incomplete version pair ---
        if result.get("incomplete_version"):
            QtWidgets.QMessageBox.warning(
                self,
                "Miner Incomplete",
                f"The selected miner ({parsed.get('name')} / {code}) does not have both GUI and PoC releases for {platform_name} yet.\n\n"
                "Installation will be available once both components are published."
            )
            try:
                self.key_status.setText(f"? {parsed.get('name')} ({code}) is missing a GUI or PoC release for {platform_name}")
                self.key_status.setStyleSheet("color: #ef4444; font-weight: bold;")
            except Exception:
                pass
            try:
                self._sync_validate_button_state(False)
            except Exception:
                pass
            self.status_bar.setText("Validation blocked: missing GUI/PoC releases for this platform")
            return

        # --- Unexpected error ---
        if "error" in result and "conflicts" not in result:
            self.conflict_status.setStyleSheet("color: #ef4444; font-weight: bold;")
            self.conflict_status.setText(f"⚠ Validation error: {result['error']}")
            self.status_bar.setText("Validation failed")
            try:
                self._sync_validate_button_state(False)
            except Exception:
                pass
            return

        # --- Conflict results ---
        conflicts = result.get("conflicts", {})
        self._last_conflicts = conflicts  # Cache for wizard navigation / install_miner

        if "error" in conflicts:
            self.conflict_status.setStyleSheet("color: #ef4444; font-weight: bold;")
            self.conflict_status.setText(f"⚠ Error checking conflicts: {conflicts['error']}")
            self.status_bar.setText("Validation failed")
            self._debug_log(f"[validate_key] Returning early due to error: {conflicts['error']}")
            try:
                self._sync_validate_button_state(False)
            except Exception:
                pass
            return

        # Update conflict display
        try:
            self.update_conflict_display(conflicts)
        except Exception:
            pass

        # Reveal the Refresh button
        try:
            if getattr(self, 'conflict_refresh_btn', None) is not None:
                self.conflict_refresh_btn.setVisible(True)
        except Exception:
            pass

        # Mark validated
        self.is_key_validated = True
        try:
            self.key_status.setText(f"✓ {parsed.get('name')} key validated with FryNetworks API")
            self.key_status.setStyleSheet("color: #22c55e; font-weight: bold;")
        except Exception:
            pass

        # Enable Next button
        try:
            try:
                self._sync_validate_button_state(True)
            except Exception:
                pass
            fb = self.wizard.button(self.FINISH_BUTTON)
            if fb is not None:
                fb.setEnabled(not bool(conflicts.get("has_conflicts")))
        except Exception:
            pass

        # Status bar message
        if conflicts.get("has_conflicts"):
            self.status_bar.setText("Conflicts detected - click 'Try Another Key' to enter a different miner key")
        else:
            self.status_bar.setText("Key validated - click Next to continue")

    def clear_form(self):
        """Clear the form and reset to initial state."""
        try:
            # Clear all installation progress indicators
            self._clear_installation_progress()
            
            # Hide Clear button (will be shown again if text is entered)
            if hasattr(self, 'clear_button'):
                self.clear_button.setVisible(False)
            
            # Clear the key input
            if hasattr(self, 'key_input'):
                self.key_input.clear()
            
            # Reset validation status
            self.is_key_validated = False
            self._last_conflicts = None
            self._post_install_mode = False
            
            # Clear status labels
            if hasattr(self, 'key_status'):
                self.key_status.setText("")
                self.key_status.setStyleSheet("")
            
            if hasattr(self, 'conflict_status'):
                self.conflict_status.setText("Click 'Validate Key' to check compatibility")
                self.conflict_status.setStyleSheet("color: #b0b0b0;")  # Reset to default gray
            
            # Hide refresh button
            if hasattr(self, 'conflict_refresh_btn') and self.conflict_refresh_btn is not None:
                self.conflict_refresh_btn.setVisible(False)
            
            # Clear detected miner info and hide the group
            if hasattr(self, 'miner_info_label'):
                self.miner_info_label.setText("")
            if hasattr(self, 'miner_group'):
                self.miner_group.setVisible(False)
            
            # Reset Next button text back to "Next >"
            try:
                self.wizard.setButtonText(cast(Any, getattr(QtWidgets.QWizard, 'NextButton', 0)), "Next >")
            except Exception:
                pass
            
            # Reset wizard to first page
            if hasattr(self, 'wizard'):
                self.wizard.restart()
                
                # Disable Next/Finish buttons
                try:
                    nb = self.wizard.button(self.NEXT_BUTTON)
                    if nb is not None:
                        nb.setEnabled(False)
                except Exception:
                    pass
                
                try:
                    fb = self.wizard.button(self.FINISH_BUTTON)
                    if fb is not None:
                        fb.setEnabled(False)
                except Exception:
                    pass
            
            # Reset status bar
            self.status_bar.setText("Ready - Enter a miner key to begin")

            # Set focus back to key input
            if hasattr(self, 'key_input'):
                self.key_input.setFocus()

            # Reset BM partner options
            self._reset_partner_section()
                
        except Exception as e:
            # If clear fails, show error but don't crash
            try:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Clear Form",
                    f"Could not fully clear form: {str(e)}"
                )
            except Exception:
                pass
    
    def install_another_miner(self):
        """Reset the installer to allow installing another miner."""
        try:
            # Hide the "Install Another Miner" button
            if hasattr(self, 'install_another_button'):
                self.install_another_button.setVisible(False)
            self._post_install_mode = False
            
            # Clear the form and reset to initial state
            self.clear_form()
            
            # Update status
            self.status_bar.setText("Ready - Enter another miner key to begin")
            
        except Exception as e:
            # If reset fails, show error but don't crash
            try:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Install Another Miner",
                    f"Could not reset installer: {str(e)}"
                )
            except Exception:
                pass
    
    def _clear_installation_progress(self):
        """Clear all installation progress indicators and logs."""
        try:
            # Clear progress bars - reset to 0 but keep default percentage format
            if hasattr(self, 'progress_bar') and self.progress_bar is not None:
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat("%p%")  # Reset to default percentage format
            # Reset baseline so next install can advance monotonically
            self._last_progress_value = 0
            if hasattr(self, 'step6_progress') and self.step6_progress is not None:
                self.step6_progress.setValue(0)
                self.step6_progress.setFormat("%p%")  # Reset to default percentage format
            
            # Clear progress log
            if hasattr(self, 'progress_log') and self.progress_log is not None:
                self.progress_log.clear()
            
            # Clear installation status text
            if hasattr(self, 'progress_label') and self.progress_label is not None:
                self.progress_label.setText("")
        except Exception:
            pass
    
    def install_miner(self):
        """Start miner installation process."""
        # Debug: record that install_miner was invoked
        try:
            if getattr(self, '_debug_log_path', None):
                self._debug_log("install_miner called")
        except Exception:
            pass

        # Transition out of post-install mode when starting a fresh install
        self._post_install_mode = False

        # Check for AEM miner and companion software requirements
        install_olostep = False
        install_orbit = False
        install_web_agent = False
        if self.current_miner_info and self.current_miner_info.get('code') == 'AEM':
            reply = QtWidgets.QMessageBox.question(
                self,
                "Required Software",
                "As part of the Olostep partnership with FryNetworks, installing and running "
                "the following software is mandatory for AI Edge Miner (AEM) installations:\n\n"
                "  1. Olostep Browser\n"
                "  2. Orbit Desktop App\n"
                "  3. Web Agent Browser Extension\n\n"
                "Do you want to continue and install all required components?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.Yes
            )

            if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                self.status_bar.setText("Installation cancelled")
                return
            install_olostep = True
            install_orbit = True
            install_web_agent = True

        # Hide the Installation Summary to give more room for progress
        try:
            if hasattr(self, "review_label"):
                parent_group = self.review_label.parentWidget()
                if parent_group is not None:
                    parent_group.setVisible(False)
        except Exception:
            pass


        if not self.current_miner_info:
            return
        miner_info = self.current_miner_info

        key = self.key_input.text().strip()
        
        # Get installation options
        options: Dict[str, Any] = {
            # Mandatory options
            "system_wide": True,
            "with_deps": True,
            # Optional UI choice
            "create_desktop_shortcut": (self.desktop_shortcut.isChecked() if hasattr(self, 'desktop_shortcut') else False),
            "pin_start_menu": (self.pin_start_checkbox.isChecked() if hasattr(self, 'pin_start_checkbox') else False),
            "auto_start": self.auto_start.isChecked(),
            "install_olostep": install_olostep,
            "install_orbit": install_orbit,
            "install_web_agent": install_web_agent,
        }
        # Bright consent configuration (BM installs)
        # Installer always stages SDK files but doesn't activate (consent=false)
        # GUI will handle consent dialogs and activation
        options["bright_config"] = {
            "app_id": "win_fry_networks.fry_bandwidth_miner",
            "app_name": "FRY Bandwidth Miner",
            "logo_link": "https://static.wixstatic.com/media/b2ad32_7ce110657e294cdfbdd5fdf4497dfcb1~mv2.png/v1/fill/w_115,h_74,al_c,q_85,usm_0.66_1.00_0.01,enc_avif,quality_auto/Logo%203.png",
            "benefit": "add x0.25 to the base rewards",
            "agree_btn": "I agree",
            "disagree_btn": "Decline",
            "opt_out_txt": (
                "Turning off Web Indexing pauses bandwidth sharing. "
                "While it is disabled your fVPN earnings are reduced by 0.25x. "
                "Enable it again to resume your 0.25x multiplier."
            ),
            "consent": False,
        }

        # Optional custom install directory
        custom_dir = self.install_path_edit.text().strip() if hasattr(self, "install_path_edit") else ""
        if custom_dir:
            options["install_dir"] = custom_dir

        # Screen size preference
        if hasattr(self, "screen_size_combo"):
            idx = self.screen_size_combo.currentIndex()
            if 0 <= idx < len(self._screen_size_choices):
                options["screen_size"] = self._screen_size_choices[idx][1]

        # Partner integrations (BM only)
        # Installer always stages ALL SDK files but activates NONE by default
        # GUI handles consent dialogs and activation to unlock fVPN reward multipliers
        honeygain_available = bool(self._partner_secret_flags.get("honeygain"))
        mysterium_available = True  # Credentials handled by GUI at runtime
        bright_available = bool(self._partner_secret_flags.get("bright") and self._is_windows)
        
        if miner_info.get("code") == "BM":
            # Stage all SDK files (regardless of availability)
            # But set all opt-ins to False - GUI will handle activation
            options["honeygain_opt_in"] = False
            options["mysterium_opt_in"] = False
            options["bright_opt_in"] = False
            options["_stage_partner_sdks"] = {
                "honeygain": honeygain_available,
                "mysterium": mysterium_available,
                "bright": bool(self._is_windows and bright_available),
            }
        else:
            options["honeygain_opt_in"] = False
            options["mysterium_opt_in"] = False
            options["bright_opt_in"] = False

        # Conflict resolution strategy - simplified: always retry with new key
        options["resolve_conflicts"] = "retry_key"

        # Use cached conflict result from validate_key (already ran on background thread)
        cached = getattr(self, '_last_conflicts', None)
        if cached and cached.get("has_conflicts"):
            self.status_bar.setText("Conflicts detected - click 'Try Another Key' to enter a different miner key")
            return

        # Show progress and concise header
        try:
            self.progress_group.setVisible(True)
            self.progress_bar.setValue(0)
            self.progress_log.clear()
            self._progress_seeded = False

            if self.concise_log:
                miner_name = (miner_info.get('name') if miner_info else '(unknown)')
                self.progress_log.append(f"Installing {miner_name} ({miner_info.get('code')})")
                self.progress_log.append("")
        except Exception:
            try:
                self.progress_group.setVisible(True)
            except Exception:
                pass

            # Hide the Review page header/title to free vertical space during installation
        try:
            if getattr(self, 'review_page', None) is not None:
                try:
                    # Save original title/subtitle so we can restore later
                    self._review_orig_title = self.review_page.title()
                    self._review_orig_subtitle = self.review_page.subTitle()
                except Exception:
                    self._review_orig_title = None
                    self._review_orig_subtitle = None
                try:
                    self.review_page.setTitle("")
                    self.review_page.setSubTitle("")
                except Exception:
                    pass
        except Exception:
            pass
        # Also hide the wizard header entirely by applying a temporary stylesheet
        try:
            # Save original stylesheet so we can restore it later
            try:
                self._wizard_orig_qss = self.wizard.styleSheet() or ""
            except Exception:
                self._wizard_orig_qss = ""
            hide_header_qss = "\nQWizard::title, QWizard::subTitle { height:0px; min-height:0px; max-height:0px; margin:0px; padding:0px; }\nQWizardPage { margin-top:0px; }\n"
            try:
                self.wizard.setStyleSheet(self._wizard_orig_qss + hide_header_qss)
            except Exception:
                try:
                    # Fallback: set an empty style to avoid breaking UI if concatenation fails
                    self.wizard.setStyleSheet(hide_header_qss)
                except Exception:
                    pass
        except Exception:
            pass
        
        # Disable UI during installation
        self.key_input.setEnabled(False)
        try:
            nb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'NextButton', 0)))
            if nb is not None:
                nb.setEnabled(False)
            fb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)))
            if fb is not None:
                fb.setEnabled(False)
            bb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'BackButton', 0)))
            if bb is not None:
                bb.setEnabled(False)
        except Exception:
            pass

        # Start installation in background
        try:
            if getattr(self, '_debug_log_path', None):
                import time
                with open(str(self._debug_log_path), 'a', encoding='utf-8') as _df:
                    _df.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - calling start_installation_thread\n")
        except Exception:
            pass

        # Save context so we can Retry with the same settings later
        self._last_install_ctx = {
        "key": key,
        "options": options.copy(),
        "miner_info": miner_info,
        }

        self.start_installation_thread(key, options, miner_info)
    
    def start_installation_thread(self, key: str, options: dict, miner_info: Dict[str, Any]):
        """Start installation in background thread."""
        self._debug_log("calling start_installation_thread")
        def install():
            try:
                # local helper to test cancellation early
                def _should_cancel() -> bool:
                    return bool(getattr(self, '_cancel_requested', False))

                # Step 1 — Validate configuration
                if self.concise_log:
                    self.log_progress("1. Validating configuration... ✓")
                self.update_progress(10, "Validating configuration...")
                if _should_cancel():
                    self._handle_cancel_rollback(None, miner_info, options)
                    return
                
                # Setup configuration manager
                config_manager = ConfigManager(miner_info["code"])
                
                # Step 2 — Prepare directory
                self.update_progress(20, "Setting up directories...")
                setup_result = config_manager.setup_directories(
                    options["system_wide"],
                    install_path=options.get("install_dir")
                )
                
                if not setup_result["success"]:
                    self.installation_failed("Directory setup failed", setup_result["errors"])
                    return
                install_dir_path = setup_result.get("install_dir") or options.get("install_dir") or "(default)"
                # Remember resolved install dir for later use (launching GUI, etc.)
                options["_resolved_install_dir"] = setup_result.get("install_dir") or options.get("install_dir")
                if self.concise_log:
                    self.log_progress(f"2. Preparing directory: {install_dir_path}")
                self._debug_log(f"Prepared install dir: {setup_result.get('install_dir')}")
                if _should_cancel():
                    # Rollback just created directory
                    self._handle_cancel_rollback(setup_result.get("install_dir"), miner_info, options)
                    return
                
                # Step 3 — Determine required Miner GUI and PoC versions
                desired_version = None
                poc_version = None
                try:
                    client = self.api_client
                    code_value = str(miner_info.get("code")) if miner_info and miner_info.get("code") else None
                    if client and code_value:
                        platform = "windows" if sys.platform.startswith('win') else "linux"
                        ver_dict = client.get_required_version(code_value, platform=platform, use_test=self._use_test_versions)
                        if isinstance(ver_dict, dict):
                            # Treat an empty dict or a dict containing only a 'detail' message
                            # as "no versions available" for this platform and fail early
                            if (not ver_dict) or ("detail" in ver_dict and not ver_dict.get("software_version") and not ver_dict.get("poc_version")):
                                platform_name = "Linux" if platform == "linux" else "Windows"
                                detail_msg = ver_dict.get("detail") if isinstance(ver_dict.get("detail"), str) else None
                                unavailable_msg = (
                                    detail_msg
                                    or f"{miner_info.get('name') or 'This miner'} is not available on {platform_name} until both GUI and PoC versions are published for this platform."
                                )
                                self.installation_failed("Miner unavailable", [unavailable_msg])
                                return

                            # Get both software_version (GUI) and poc_version
                            ver = ver_dict.get("software_version")
                            if ver:
                                desired_version = str(ver).lstrip("v")
                            poc_ver = ver_dict.get("poc_version")
                            if poc_ver:
                                poc_version = str(poc_ver).lstrip("v")
                except Exception:
                    desired_version = None
                    poc_version = None

                if not desired_version or not poc_version:
                    platform_name = "Windows" if getattr(self, "_is_windows", False) else "Linux"
                    friendly_name = None
                    try:
                        friendly_name = miner_info.get("name")
                    except Exception:
                        friendly_name = None
                    unavailable_msg = (
                        f"{friendly_name or 'This miner'} is not available on {platform_name} until both GUI and PoC "
                        "versions are published for this platform."
                    )
                    self.installation_failed("Miner unavailable", [unavailable_msg])
                    return
                
                version_str = desired_version or "unknown"
                poc_version_str = poc_version or "unknown"
                
                if self.concise_log:
                    try:
                        miner_display = miner_info.get('name') or 'Miner'
                    except Exception:
                        miner_display = 'Miner'
                    self.log_progress(f"3. Required {miner_display} version: {version_str}")
                    if poc_version and poc_version != desired_version:
                        self.log_progress(f"4. Required {miner_display} PoC version: {poc_version_str}")

                # Step 4 - Install Olostep Browser (AEM only) with visible progress
                if options.get("install_olostep") and miner_info.get("code") == "AEM":
                    def _olostep_progress(msg: str) -> None:
                        try:
                            self.update_progress(30, msg)
                        except Exception:
                            pass
                    if self.concise_log:
                        self.log_progress("4. Installing Olostep Browser (required)...")
                    try:
                        self._install_olostep_browser(
                            progress_cb=_olostep_progress,
                            log_cb=self.log_progress
                        )
                        if self.concise_log:
                            self.log_progress("4. Installing Olostep Browser (required)... ✓")
                    except Exception as e:
                        self.installation_failed("Olostep Browser Installation Failed", [str(e)])
                        return

                # Step 4b - Install Orbit (AEM only)
                if options.get("install_orbit") and miner_info.get("code") == "AEM":
                    if self.concise_log:
                        self.log_progress("4b. Installing Orbit Desktop App (required)...")
                    try:
                        self._install_orbit(
                            progress_cb=_olostep_progress,
                            log_cb=self.log_progress
                        )
                        if self.concise_log:
                            self.log_progress("4b. Installing Orbit Desktop App (required)... \u2713")
                    except Exception as e:
                        self.installation_failed("Orbit Installation Failed", [str(e)])
                        return

                # Step 4c - Install Web Agent Extension (AEM only)
                if options.get("install_web_agent") and miner_info.get("code") == "AEM":
                    if self.concise_log:
                        self.log_progress("4c. Installing Web Agent Extension (required)...")
                    try:
                        self._install_web_agent(
                            progress_cb=_olostep_progress,
                            log_cb=self.log_progress
                        )
                        if self.concise_log:
                            self.log_progress("4c. Installing Web Agent Extension (required)... \u2713")
                    except Exception as e:
                        self.installation_failed("Web Agent Installation Failed", [str(e)])
                        return

                # Don't write configuration here - it will be written after installation with version info

                # Step 5 — Dependencies (concise)
                if self.concise_log:
                    self.log_progress("5. Installing dependencies... ✓")

                download_attempts = None
                # Prepare a Step 6 progress callback so ServiceManager can stream updates
                def _step6_cb(percent:int, msg:str):
                    if _should_cancel():
                        return  # Ignore further progress updates once cancelling
                    # This callback receives:
                    # - Download progress: 0-100% for individual file downloads (goes to Step 6 sub-bar)
                    # - Overall milestones: 70 (GUI failed), 75 (GUI complete), 85 (PoC complete) (goes to main bar)
                    
                    # Route overall milestones to MAIN progress bar
                    try:
                        if msg and msg.lower().startswith(("gui download", "poc download")):
                            target_value = int(percent)
                            # Keep the main bar monotonic by ignoring stale callbacks
                            if target_value >= self._last_progress_value:
                                self.update_progress(target_value, msg)
                            return
                    except Exception:
                        pass
                    
                    # Route individual download progress (0-100%) to Step 6 sub-bar
                    asset_name = ""
                    if msg:
                        try:
                            # Use a short asset label for the small progress label
                            clean_msg = str(msg)
                            if '(' in clean_msg:
                                clean_msg = clean_msg.split('(')[0]
                            asset_name = clean_msg.strip()
                        except Exception:
                            asset_name = ""

                    try:
                        self._invoke_step6_update.emit(int(percent), asset_name)
                    except Exception:
                        QtCore.QTimer.singleShot(0, lambda p=int(percent), m=asset_name: self._update_step6_main_thread(p, m))
                
                # Prepare a log callback for sequential download logging
                def _log_cb(step:str, message:str):
                    """Log download steps sequentially as they happen (thread-safe)."""
                    if self.concise_log:
                        # For completion messages (with ✓), update the last line in place
                        if step.endswith('_complete') and message.endswith('✓'):
                            # Use signal to safely update from worker thread
                            self._invoke_log_update.emit(message)
                        else:
                            # For start messages, just append (thread-safe)
                            self._invoke_log.emit(message)
                
                # Attach to options so ServiceManager may call them
                try:
                    options['progress_callback'] = _step6_cb
                    options['log_callback'] = _log_cb
                    # Provide cancel flag function to service manager
                    options['cancel_flag_func'] = lambda: _should_cancel()
                except Exception:
                    pass
                
                # Don't log download steps here - let ServiceManager log them sequentially
                # through the log_callback as each download starts and completes
                    
                self.update_progress(60, "Installing service...")
                self.update_progress(65, "Downloading components and installing service...")
                # Use poc_version for ServiceManager since the service runs the PoC executable
                service_manager = ServiceManager(miner_info["code"], version=poc_version) if poc_version else ServiceManager(miner_info["code"], version=desired_version) if desired_version else ServiceManager(miner_info["code"])
                try:
                    # Compute version platform and pass into options for ServiceManager
                    base_platform = "windows" if sys.platform.startswith('win') else "linux"
                    version_platform = f"test-{base_platform}" if getattr(self, "_use_test_versions", False) else base_platform
                    options["version_platform"] = version_platform
                    install_result = service_manager.install_service(key, **options)
                except Exception as e:
                    install_result = {'success': False, 'message': str(e)}

                # If cancellation requested during service_manager work, perform rollback
                if _should_cancel():
                    self._handle_cancel_rollback(setup_result.get("install_dir"), miner_info, options)
                    return

                if not install_result.get("success"):
                    base_msg = install_result.get("message") or "Unknown error during service installation"
                    self.log_progress(f"[error] Installation error: {base_msg}")
                    self.log_progress("")
                    self.log_progress("=" * 60)
                    self.log_progress("INSTALLATION FAILED")
                    self.log_progress("=" * 60)
                    self.installation_failed("Service installation failed", [base_msg])
                    return

                if self.concise_log:
                    download_attempts = install_result.get("download_attempts") or []
                    if download_attempts:
                        # Mark downloads as complete (checkmarks already shown in steps 6/7)
                        try:
                            # Signal Step 6 completion for the small progress bar
                            self._invoke_step6_update.emit(100, "Downloads complete")
                        except Exception:
                            try:
                                QtCore.QTimer.singleShot(0, lambda: self._update_step6_main_thread(100, "Downloads complete"))
                            except Exception:
                                pass
                    else:
                        actions = install_result.get("actions") or []
                        for action in actions:
                            if isinstance(action, str) and action.startswith("Download attempts:"):
                                self.log_progress(f"   • {action[len('Download attempts:'):].strip()}")

                # Determine next step number based on whether PoC version was different
                next_step = 8 if (poc_version and poc_version != desired_version) else 7
                
                if self.concise_log:
                    self.log_progress(f"{next_step}. Installing and configuring service... ✓")
                    if options.get("auto_start", True):
                        self.log_progress(f"{next_step + 1}. Enabling autostart... ✓")

                # Write configuration with version information from installation
                gui_version = install_result.get("gui_version")
                poc_version_installed = install_result.get("poc_version")
                
                if self.concise_log:
                    self.log_progress("10. Writing configuration... ✓")
                
                write_result = config_manager.write_miner_key(
                    key,
                    options["system_wide"],
                    install_path=options.get("install_dir"),
                    gui_version=gui_version,
                    poc_version=poc_version_installed
                )
                
                if not write_result["success"]:
                    self.log_progress(f"[warning] Configuration write had issues: {write_result.get('errors', [])}")

                shortcut_install_path = None
                if os.name == 'nt':
                    try:
                        from pathlib import Path as _P
                        raw_path = (
                            install_result.get("install_dir")
                            or install_result.get("install_path")
                            or options.get("install_dir")
                            or write_result.get("install_dir")
                            or ""
                        )
                        if raw_path:
                            shortcut_install_path = _P(raw_path) if not isinstance(raw_path, _P) else raw_path
                    except Exception:
                        shortcut_install_path = None

                shortcut_path = None
                if options.get("create_desktop_shortcut", False) and os.name == 'nt':
                    try:
                        if shortcut_install_path is None:
                            raise RuntimeError("Installation path unavailable for shortcut creation")

                        shortcut_path = self._create_desktop_shortcut_for_miner(
                            miner_code=miner_info["code"],
                            install_path=shortcut_install_path,
                            gui_version=gui_version
                        )
                        self.log_progress("Desktop shortcut created successfully")
                        self._debug_log(f"Desktop shortcut created: {shortcut_path}")
                    except Exception as shortcut_err:
                        self.log_progress(f"[warning] Could not create desktop shortcut: {shortcut_err}")
                        self._debug_log(f"Desktop shortcut creation failed: {shortcut_err}")

                start_menu_shortcut = None
                if options.get("pin_start_menu", False) and os.name == 'nt':
                    try:
                        if shortcut_install_path is None:
                            raise RuntimeError("Installation path unavailable for Start menu pin")
                        start_menu_shortcut = self._create_start_menu_shortcut_for_miner(
                            miner_code=miner_info["code"],
                            install_path=shortcut_install_path,
                            gui_version=gui_version,
                        )
                        self.log_progress("Pinned miner GUI to Start menu")
                    except Exception as start_err:
                        self.log_progress(f"[warning] Could not pin GUI to Start menu: {start_err}")

                if options.get("auto_start", True):
                    try:
                        if shortcut_install_path is None:
                            raise RuntimeError("Installation path unavailable for startup shortcut")
                        startup_shortcut = self._create_startup_shortcut_for_miner(
                            miner_code=miner_info["code"],
                            install_path=shortcut_install_path,
                            gui_version=gui_version
                        )
                        self.log_progress("Configured miner GUI to launch automatically after reboot/login")
                        self._debug_log(f"Startup shortcut created: {startup_shortcut}")
                    except Exception as startup_err:
                        self.log_progress(f"[warning] Could not configure GUI autostart: {startup_err}")
                        self._debug_log(f"Startup shortcut creation failed: {startup_err}")

                self.update_progress(90, "Finalizing installation...")
                self.update_progress(100, "Installation completed successfully!")

                # Store GUI launch info in result so main thread can launch the GUI
                # (QTimer.singleShot doesn't work reliably from worker threads)
                install_result["_launch_gui"] = True
                install_result["_launch_miner_code"] = miner_info["code"]
                install_result["_launch_gui_version"] = gui_version
                install_result["_launch_dir_hints"] = [
                    install_result.get("install_dir"),
                    install_result.get("install_path"),
                    options.get("install_dir"),
                    options.get("_resolved_install_dir"),
                    setup_result.get("install_dir"),
                    write_result.get("install_dir"),
                    write_result.get("install_path"),
                ]

                self.installation_completed(install_result)

                    
            except Exception as e:
                # Debug: write exception traceback to debug log
                try:
                    import traceback, time
                    tb = traceback.format_exc()
                    if getattr(self, '_debug_log_path', None):
                        with open(str(self._debug_log_path), 'a', encoding='utf-8') as _df:
                            _df.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - exception in worker:\n")
                            _df.write(tb + "\n")
                except Exception:
                    pass
                self.installation_failed("Installation error", [str(e)])
        
        self.installation_thread = threading.Thread(target=install, daemon=True)
        self.installation_thread.start()

        # Ensure the progress section is visible and the window has enough room
        self.progress_group.setVisible(True)
        self._enter_installation_focus_mode()
        if self.height() < 900:
            self.resize(self.width(), 900)
    
    def log_progress(self, message: str):
        """Log progress message (thread-safe)."""
        try:
            self._invoke_log.emit(message)
        except Exception:
            QtCore.QTimer.singleShot(0, lambda m=message: self._log_progress_main_thread(m))
    
    def update_progress(self, value: int, message: str):
        """Update progress bar and label (thread-safe)."""
        try:
            self._invoke_update.emit(value, message)
        except Exception:
            QtCore.QTimer.singleShot(0, lambda v=value, m=message: self._update_progress_main_thread(v, m))
    
    @QtCore.Slot(str)
    def _log_progress_main_thread(self, message: str):
        """Log progress in main thread."""
        self.progress_log.append(f"{message}")
        self.progress_log.ensureCursorVisible()

    @QtCore.Slot(int, str)
    def _update_step6_main_thread(self, value: int, message: str):
        """Update Step 6 progress UI in main thread."""
        try:
            # Ensure Step 6 widgets are visible when first updated
            if not getattr(self, 'step6_label', None):
                return
            self.step6_label.setVisible(True)
            self.step6_progress.setVisible(True)
            # If a cancellation is in progress, freeze the sub progress bar
            if getattr(self, '_cancel_requested', False):
                return
            self.step6_progress.setValue(value)
            # Provide a short message in the small label
            self.step6_label.setText(message)
            # Also reflect in the status bar so user sees overall state
            self.status_bar.setText(message)
            # If completed, auto-hide the Step 6 widgets after a brief delay
            try:
                if int(value) >= 100:
                    QtCore.QTimer.singleShot(1200, lambda: (self.step6_label.setVisible(False), self.step6_progress.setVisible(False)))
            except Exception:
                pass
        except Exception:
            pass
    
    def _update_log_line_main_thread(self, message: str):
        """Update a log line in place (for checkmarks) - must run in main thread."""
        try:
            # Get the current log text
            log_text = self.progress_log.toPlainText()
            lines = log_text.split('\n')
            
            # Find the line that starts with the same step number (e.g., "6." or "7.")
            step_num = message.split('.')[0] if '.' in message else None
            if step_num:
                for i in range(len(lines) - 1, -1, -1):
                    if lines[i].startswith(f"{step_num}."):
                        # Replace the old line with the new one
                        lines[i] = message
                        break
                # Update the entire log
                self.progress_log.setPlainText('\n'.join(lines))
                # Scroll to bottom
                self.progress_log.verticalScrollBar().setValue(
                    self.progress_log.verticalScrollBar().maximum()
                )
            else:
                # Fallback: just append
                self.log_progress(message)
        except Exception:
            # Fallback: just append
            self.log_progress(message)
    
    def _install_olostep_browser(
        self,
        progress_cb: Optional[Callable[[str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None
    ) -> None:
        """Download and install Olostep Browser for AEM partnership requirement."""
        import os
        import tempfile
        import subprocess
        import urllib.request
        from pathlib import Path

        def _status(msg: str) -> None:
            if progress_cb:
                try:
                    progress_cb(msg)
                    return
                except Exception:
                    pass
            try:
                self.status_bar.setText(msg)
                QtWidgets.QApplication.processEvents()
            except Exception:
                pass

        def _log(msg: str) -> None:
            if log_cb:
                try:
                    log_cb(msg)
                    return
                except Exception:
                    pass
            _status(msg)

        # Check if Olostep Browser is already installed
        # Common installation paths for Windows browsers
        possible_paths = [
            Path(os.environ.get('PROGRAMFILES', 'C:\\Program Files')) / 'Olostep Browser' / 'Olostep Browser.exe',
            Path(os.environ.get('PROGRAMFILES(X86)', 'C:\\Program Files (x86)')) / 'Olostep Browser' / 'Olostep Browser.exe',
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Olostep Browser' / 'Olostep Browser.exe',
            Path.home() / 'AppData' / 'Local' / 'Olostep Browser' / 'Olostep Browser.exe',
        ]
        
        for path in possible_paths:
            if path.exists():
                _status("Olostep Browser is already installed")
                return  # Already installed, skip installation

        # If Olostep Browser process is already running (even if installed in a
        # non-standard location), skip the download/install step.
        try:
            if self._is_olostep_running():
                _status("Olostep Browser is already running")
                return
        except Exception:
            # If the running-check fails, continue with install attempt
            pass
        
        # Get URL from environment/config
        olostep_url = os.getenv('OLOSTEP_BROWSER_URL', 
                                'https://olostepbrowser.s3.us-east-1.amazonaws.com/updates/win32/x64/Olostep-Browser-1.0.1+Setup.exe')
        
        _status("Downloading Olostep Browser...")
        
        # Download to temp file
        temp_dir = tempfile.gettempdir()
        installer_path = os.path.join(temp_dir, 'Olostep-Browser-Setup.exe')
        
        try:
            # Download with progress feedback
            urllib.request.urlretrieve(olostep_url, installer_path)
            _status("Installing Olostep Browser...")
            
            # Run installer silently
            # Most installers support /S or /SILENT flags
            process = subprocess.run(
                [installer_path, '/S'],  # Silent install flag
                capture_output=True,
                timeout=300  # 5 minute timeout
            )
            
            if process.returncode != 0:
                raise RuntimeError(f"Installer exited with code {process.returncode}")
            
            _log("Olostep Browser installed successfully")
            
        except Exception as e:
            raise RuntimeError(f"Failed to download or install Olostep Browser: {str(e)}")
        finally:
            # Clean up installer file
            try:
                if os.path.exists(installer_path):
                    os.remove(installer_path)
            except Exception:
                pass

    def _is_olostep_running(self) -> bool:
        """Return True if an Olostep Browser process appears to be running.

        This is a best-effort check that prefers `psutil` when available,
        otherwise falls back to platform-specific commands (`tasklist` on
        Windows, `ps` on POSIX). Matching is case-insensitive and looks for
        substrings like 'olostep'.
        """
        import subprocess
        import sys
        try:
            try:
                import psutil
                for proc in psutil.process_iter(['name', 'exe', 'cmdline']):
                    try:
                        names = []
                        if proc.info.get('name'):
                            names.append(proc.info.get('name'))
                        if proc.info.get('exe'):
                            names.append(proc.info.get('exe'))
                        cmdline = proc.info.get('cmdline')
                        if cmdline and isinstance(cmdline, (list, tuple)):
                            names.extend(cmdline)
                        for n in names:
                            try:
                                if n and 'olostep' in str(n).lower():
                                    return True
                            except Exception:
                                continue
                    except Exception:
                        continue
                return False
            except Exception:
                # Fallback: use system command
                if sys.platform.startswith('win'):
                    # Use tasklist to list processes
                    try:
                        out = subprocess.check_output(['tasklist', '/FO', 'CSV'], text=True, stderr=subprocess.DEVNULL)
                        if 'olostep' in out.lower():
                            return True
                    except Exception:
                        pass
                else:
                    try:
                        out = subprocess.check_output(['ps', 'aux'], text=True, stderr=subprocess.DEVNULL)
                        if 'olostep' in out.lower():
                            return True
                    except Exception:
                        pass
                return False
        except Exception:
            return False

    # ---- Orbit + Web Agent companion software (AEM only) ----

    def _install_orbit(
        self,
        progress_cb: Optional[Callable[[str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None
    ) -> None:
        """Download and install Orbit desktop app for AEM partnership requirement."""
        import os, tempfile, subprocess, ssl, urllib.request
        try:
            import certifi
            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            _ssl_ctx = ssl.create_default_context()
        from pathlib import Path

        def _status(msg):
            if progress_cb:
                try: progress_cb(msg); return
                except Exception: pass
            try: self.status_bar.setText(msg); QtWidgets.QApplication.processEvents()
            except Exception: pass

        def _log(msg):
            if log_cb:
                try: log_cb(msg); return
                except Exception: pass
            _status(msg)

        possible_paths = [
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Orbit' / 'Orbit.exe',
            Path(os.environ.get('PROGRAMFILES', 'C:\\Program Files')) / 'Orbit' / 'Orbit.exe',
            Path(os.environ.get('PROGRAMFILES(X86)', 'C:\\Program Files (x86)')) / 'Orbit' / 'Orbit.exe',
            Path.home() / 'AppData' / 'Local' / 'Orbit' / 'Orbit.exe',
        ]
        for path in possible_paths:
            if path.exists():
                _status("Orbit is already installed")
                return

        try:
            if self._is_orbit_running():
                _status("Orbit is already running")
                return
        except Exception:
            pass

        orbit_url = os.getenv(
            'ORBIT_SETUP_URL',
            'https://frynetworks-downloads.b-cdn.net/installers/Orbit-1.2.0%2BSetup.exe'
        )
        _status("Downloading Orbit...")
        temp_dir = tempfile.gettempdir()
        installer_path = os.path.join(temp_dir, 'Orbit-Setup.exe')
        try:
            req = urllib.request.Request(orbit_url)
            with urllib.request.urlopen(req, context=_ssl_ctx) as resp, open(installer_path, "wb") as out:
                out.write(resp.read())
            _status("Installing Orbit...")
            process = subprocess.run(
                [installer_path, '--silent'],
                capture_output=True, timeout=600
            )
            if process.returncode != 0:
                raise RuntimeError(f"Orbit installer exited with code {process.returncode}")
            _log("Orbit installed successfully")
        except Exception as e:
            raise RuntimeError(f"Failed to download or install Orbit: {str(e)}")
        finally:
            try:
                if os.path.exists(installer_path):
                    os.remove(installer_path)
            except Exception:
                pass

    def _is_orbit_running(self) -> bool:
        """Return True if an Orbit process appears to be running."""
        import subprocess, sys
        try:
            try:
                import psutil
                for proc in psutil.process_iter(['name', 'exe', 'cmdline']):
                    try:
                        names = []
                        if proc.info.get('name'): names.append(proc.info.get('name'))
                        if proc.info.get('exe'): names.append(proc.info.get('exe'))
                        for n in names:
                            if n and 'orbit' in str(n).lower():
                                return True
                    except Exception:
                        continue
                return False
            except Exception:
                if sys.platform.startswith('win'):
                    try:
                        out = subprocess.check_output(['tasklist', '/FO', 'CSV'], text=True, stderr=subprocess.DEVNULL)
                        if 'orbit' in out.lower():
                            return True
                    except Exception:
                        pass
                return False
        except Exception:
            return False

    def _install_web_agent(
        self,
        progress_cb: Optional[Callable[[str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None
    ) -> None:
        """Download and install Web Agent extension for all Chromium browsers."""
        import os, sys, struct, zipfile, tempfile, subprocess, ssl, urllib.request, json
        try:
            import certifi
            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            _ssl_ctx = ssl.create_default_context()
        from pathlib import Path

        EXTENSION_ID = "boielbimiidkndfedfhjloejnilfbjel"
        CRX_URL = os.getenv(
            'WEB_AGENT_CRX_URL',
            'https://orbit-api.olostep.com/api/extensions/boielbimiidkndfedfhjloejnilfbjel/download'
        )
        local_app_data = os.environ.get('LOCALAPPDATA', '')
        ext_dir = Path(local_app_data) / 'Orbit' / 'extensions' / 'web-agent'
        ext_dir_str = str(ext_dir).replace("'", "''")

        BROWSER_POLICY_PATHS = [
            r"SOFTWARE\Policies\Google\Chrome",
            r"SOFTWARE\Policies\BraveSoftware\Brave-Browser",
            r"SOFTWARE\Policies\Microsoft\Edge",
        ]

        def _status(msg):
            if progress_cb:
                try: progress_cb(msg); return
                except Exception: pass
            try: self.status_bar.setText(msg); QtWidgets.QApplication.processEvents()
            except Exception: pass

        def _log(msg):
            if log_cb:
                try: log_cb(msg); return
                except Exception: pass
            _status(msg)

        def _dbg(msg):
            try: self._debug_log(f"[WebAgent] {msg}")
            except Exception: pass

        # 4a-pre. Add Defender exclusion BEFORE downloading/extracting
        if sys.platform.startswith('win'):
            try:
                ext_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ['powershell', '-NoProfile', '-Command',
                     f"Add-MpPreference -ExclusionPath '{ext_dir_str}'"],
                    capture_output=True, timeout=30
                )
                _dbg(f"Defender exclusion added for {ext_dir}")
            except Exception as e:
                _dbg(f"Defender exclusion failed (non-fatal): {e}")

        # 4a. Download CRX
        _status("Downloading Web Agent extension...")
        crx_path = os.path.join(tempfile.gettempdir(), 'web-agent.crx')
        _dbg(f"Downloading CRX from {CRX_URL}")
        try:
            req = urllib.request.Request(CRX_URL)
            with urllib.request.urlopen(req, context=_ssl_ctx) as resp, open(crx_path, "wb") as out:
                data = resp.read()
                out.write(data)
            crx_size = os.path.getsize(crx_path)
            _dbg(f"CRX downloaded: {crx_size} bytes")
            if crx_size < 100:
                raise RuntimeError(f"CRX file too small ({crx_size} bytes) — download likely failed")
        except RuntimeError:
            raise
        except Exception as e:
            _dbg(f"CRX download FAILED: {e}")
            raise RuntimeError(f"Failed to download Web Agent CRX: {str(e)}")

        # 4b. Parse and extract CRX
        _status("Extracting Web Agent extension...")
        try:
            ext_dir.mkdir(parents=True, exist_ok=True)
            with open(crx_path, 'rb') as f:
                magic = f.read(4)
                _dbg(f"CRX magic: {magic!r}")
                if magic != b'Cr24':
                    raise RuntimeError(f"Invalid CRX magic: {magic!r}")
                crx_version = struct.unpack('<I', f.read(4))[0]
                _dbg(f"CRX version: {crx_version}")
                if crx_version == 3:
                    header_size = struct.unpack('<I', f.read(4))[0]
                    zip_start = 12 + header_size
                    _dbg(f"CRXv3 header_size={header_size}, zip_start={zip_start}")
                elif crx_version == 2:
                    pk_len = struct.unpack('<I', f.read(4))[0]
                    sig_len = struct.unpack('<I', f.read(4))[0]
                    zip_start = 16 + pk_len + sig_len
                    _dbg(f"CRXv2 pk_len={pk_len}, sig_len={sig_len}, zip_start={zip_start}")
                else:
                    raise RuntimeError(f"Unsupported CRX version: {crx_version}")
                f.seek(zip_start)
                zip_data = f.read()
                _dbg(f"ZIP data: {len(zip_data)} bytes, magic: {zip_data[:2]!r}")

            if zip_data[:2] != b'PK':
                raise RuntimeError(f"ZIP data does not start with PK magic: {zip_data[:4]!r}")

            zip_tmp = crx_path + '.zip'
            with open(zip_tmp, 'wb') as f:
                f.write(zip_data)
            with zipfile.ZipFile(zip_tmp) as zf:
                names = zf.namelist()
                _dbg(f"ZIP contains {len(names)} files: {names[:5]}")
                zf.extractall(str(ext_dir))
            os.remove(zip_tmp)

            # Verify extraction
            manifest_path = ext_dir / 'manifest.json'
            if not manifest_path.exists():
                raise RuntimeError(f"manifest.json not found at {manifest_path} after extraction")
            extracted_count = sum(1 for _ in ext_dir.rglob('*') if _.is_file())
            _dbg(f"Extraction verified: {extracted_count} files, manifest.json present at {manifest_path}")

        except RuntimeError:
            raise
        except Exception as e:
            _dbg(f"CRX extraction FAILED: {e}")
            raise RuntimeError(f"Failed to extract CRX: {str(e)}")
        finally:
            try:
                if os.path.exists(crx_path):
                    os.remove(crx_path)
            except Exception:
                pass

        _log("Web Agent extension files extracted")

        # 4c. Write registry policies for Chrome, Brave, Edge
        if sys.platform.startswith('win'):
            _status("Configuring browser extension policies...")
            try:
                import winreg
                update_url = CRX_URL
                force_value = f"{EXTENSION_ID};{update_url}"
                settings_json = json.dumps({
                    "installation_mode": "force_installed",
                    "update_url": update_url,
                })
                for browser_path in BROWSER_POLICY_PATHS:
                    try:
                        fl_path = browser_path + r"\ExtensionInstallForcelist"
                        key = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, fl_path, 0, winreg.KEY_WRITE)
                        slot = 1
                        try:
                            i = 0
                            while True:
                                existing_name, _, _ = winreg.EnumValue(key, i)
                                try: slot = max(slot, int(existing_name) + 1)
                                except ValueError: pass
                                i += 1
                        except OSError:
                            pass
                        winreg.SetValueEx(key, str(slot), 0, winreg.REG_SZ, force_value)
                        winreg.CloseKey(key)
                        es_path = browser_path + r"\ExtensionSettings"
                        key = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, es_path, 0, winreg.KEY_WRITE)
                        winreg.SetValueEx(key, EXTENSION_ID, 0, winreg.REG_SZ, settings_json)
                        winreg.CloseKey(key)
                        _dbg(f"Registry policies set for {browser_path}")
                    except Exception as e:
                        _dbg(f"Registry policy failed for {browser_path}: {e}")
                _log("Browser extension policies configured")
            except ImportError:
                _dbg("winreg not available (non-Windows)")

            # 4d. Brave shortcut modification + create shortcut if none exist
            _status("Configuring Brave browser extension loading...")
            try:
                search_locations = [
                    os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu', 'Programs'),
                    os.path.join(os.environ.get('PUBLIC', 'C:\\Users\\Public'), 'Desktop'),
                    os.path.join(Path.home(), 'Desktop'),
                    os.path.join(Path.home(), 'OneDrive', 'Desktop'),
                    os.path.join('C:\\ProgramData', 'Microsoft', 'Windows', 'Start Menu', 'Programs'),
                    os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Internet Explorer', 'Quick Launch', 'User Pinned', 'TaskBar'),
                ]
                # Find Brave install path
                brave_exe = None
                for bp in [
                    Path('C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe'),
                    Path('C:/Program Files (x86)/BraveSoftware/Brave-Browser/Application/brave.exe'),
                    Path(local_app_data) / 'BraveSoftware' / 'Brave-Browser' / 'Application' / 'brave.exe',
                ]:
                    if bp.exists():
                        brave_exe = bp
                        break

                # Search for existing Brave shortcuts
                found_shortcuts = 0
                ps_script = f'''
$extPath = '{ext_dir_str}'
$ws = New-Object -ComObject WScript.Shell
$locations = @({", ".join(f"'{loc.replace(chr(39), chr(39)+chr(39))}'" for loc in search_locations)})
$count = 0
foreach ($loc in $locations) {{
    if (-not (Test-Path $loc)) {{ continue }}
    Get-ChildItem $loc -Recurse -Filter "*.lnk" -ErrorAction SilentlyContinue | ForEach-Object {{
        $shortcut = $ws.CreateShortcut($_.FullName)
        if (($_.Name -like "*brave*" -or $shortcut.TargetPath -like "*brave*") -and $shortcut.Arguments -notlike "*--load-extension*") {{
            if (-not $shortcut.TargetPath -and $_.Name -like "*brave*") {{
                $shortcut.TargetPath = "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"
            }}
            $shortcut.Arguments = ($shortcut.Arguments + " --load-extension=$extPath").Trim()
            $shortcut.Save()
            $count++
        }}
    }}
}}
Write-Output $count
'''
                result = subprocess.run(
                    ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
                    capture_output=True, text=True, timeout=30
                )
                try:
                    found_shortcuts = int(result.stdout.strip())
                except (ValueError, AttributeError):
                    found_shortcuts = 0
                _dbg(f"Modified {found_shortcuts} existing Brave shortcuts")

                # If no shortcuts found and Brave is installed, create one on the desktop
                if found_shortcuts == 0 and brave_exe:
                    _dbg(f"No Brave shortcuts found — creating one on Desktop with --load-extension")
                    try:
                        desktop = Path(os.path.join(Path.home(), 'Desktop'))
                        if desktop.exists():
                            shortcut_path = desktop / 'Brave Browser.lnk'
                            create_ps = f'''
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{str(shortcut_path).replace("'", "''")}')
$s.TargetPath = '{str(brave_exe).replace("'", "''")}'
$s.Arguments = '--load-extension={ext_dir_str}'
$s.WorkingDirectory = '{str(brave_exe.parent).replace("'", "''")}'
$s.Save()
'''
                            subprocess.run(
                                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', create_ps],
                                capture_output=True, timeout=15
                            )
                            _dbg(f"Created Brave shortcut at {shortcut_path}")
                    except Exception as e:
                        _dbg(f"Failed to create Brave shortcut: {e}")

                _log("Brave shortcuts configured for extension loading")

                # Check if Brave is currently running — extension won't load until full restart
                try:
                    check_brave = subprocess.run(
                        ['powershell', '-NoProfile', '-Command', 'if (Get-Process -Name brave -ErrorAction SilentlyContinue) { Write-Output 1 } else { Write-Output 0 }'],
                        capture_output=True, text=True, timeout=10
                    )
                    if check_brave.stdout.strip() == '1':
                        _dbg("Brave is running — extension will load after full Brave restart")
                        _log("[warning] Brave is running — please close and reopen Brave to activate the Web Agent extension")
                except Exception:
                    pass

            except Exception as e:
                _dbg(f"Brave shortcut configuration failed: {e}")

            # 4e. Scheduled task for Brave shortcut repair
            _status("Creating extension maintenance task...")
            try:
                repair_script_dir = Path(local_app_data) / 'Orbit'
                repair_script_dir.mkdir(parents=True, exist_ok=True)
                repair_script_path = repair_script_dir / 'repair_brave_shortcuts.ps1'
                repair_script_content = f'''# FryNetworks Web Agent - Brave shortcut repair
$extPath = "{ext_dir_str}"
if (-not (Test-Path $extPath)) {{ exit 0 }}
$locations = @(
    "$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs",
    "$env:PUBLIC\\Desktop",
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path ([Environment]::GetFolderPath("UserProfile")) "OneDrive\\Desktop"),
    "C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs",
    (Join-Path $env:APPDATA "Microsoft\\Internet Explorer\\Quick Launch\\User Pinned\\TaskBar")
)
$ws = New-Object -ComObject WScript.Shell
foreach ($loc in $locations) {{
    if (-not (Test-Path $loc)) {{ continue }}
    Get-ChildItem $loc -Recurse -Filter "*.lnk" -ErrorAction SilentlyContinue | ForEach-Object {{
        $shortcut = $ws.CreateShortcut($_.FullName)
        if (($_.Name -like "*brave*" -or $shortcut.TargetPath -like "*brave*") -and $shortcut.Arguments -notlike "*--load-extension*") {{
            if (-not $shortcut.TargetPath -and $_.Name -like "*brave*") {{
                $shortcut.TargetPath = "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"
            }}
            $shortcut.Arguments = ($shortcut.Arguments + " --load-extension=$extPath").Trim()
            $shortcut.Save()
        }}
    }}
}}
'''
                with open(repair_script_path, 'w', encoding='utf-8') as f:
                    f.write(repair_script_content)
                _dbg(f"Repair script written to {repair_script_path}")

                script_path_escaped = str(repair_script_path).replace("'", "''")
                register_cmd = f'''
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File '{script_path_escaped}'"
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$triggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 4) -RepetitionDuration (New-TimeSpan -Days 365)
Register-ScheduledTask -TaskName "FryNetworks_WebAgent_BraveRepair" -Action $action -Trigger $triggerLogon,$triggerRepeat -RunLevel Limited -Force | Out-Null
'''
                subprocess.run(
                    ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', register_cmd],
                    capture_output=True, timeout=30
                )
                _dbg("Scheduled task FryNetworks_WebAgent_BraveRepair registered")
                _log("Extension maintenance task registered")
            except Exception as e:
                _dbg(f"Scheduled task registration failed: {e}")

        _log("Web Agent extension installed successfully")

    def _cleanup_aem_companions(self) -> None:
        """Remove Orbit + Web Agent companion software installed for AEM."""
        import os, sys, subprocess, shutil
        from pathlib import Path

        local_app_data = os.environ.get('LOCALAPPDATA', '')
        ext_dir = Path(local_app_data) / 'Orbit' / 'extensions' / 'web-agent'
        ext_dir_str = str(ext_dir).replace("'", "''")
        EXTENSION_ID = "boielbimiidkndfedfhjloejnilfbjel"
        BROWSER_POLICY_PATHS = [
            r"SOFTWARE\Policies\Google\Chrome",
            r"SOFTWARE\Policies\BraveSoftware\Brave-Browser",
            r"SOFTWARE\Policies\Microsoft\Edge",
        ]

        # 1. Remove Web Agent extension files
        try:
            if ext_dir.exists():
                shutil.rmtree(str(ext_dir), ignore_errors=True)
        except Exception:
            pass

        # 2. Remove registry policies
        try:
            import winreg
            for browser_path in BROWSER_POLICY_PATHS:
                try:
                    fl_path = browser_path + r"\ExtensionInstallForcelist"
                    key = winreg.OpenKeyEx(winreg.HKEY_LOCAL_MACHINE, fl_path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
                    to_delete = []
                    try:
                        i = 0
                        while True:
                            name, value, _ = winreg.EnumValue(key, i)
                            if isinstance(value, str) and EXTENSION_ID in value:
                                to_delete.append(name)
                            i += 1
                    except OSError:
                        pass
                    for name in to_delete:
                        try: winreg.DeleteValue(key, name)
                        except Exception: pass
                    winreg.CloseKey(key)
                except Exception:
                    pass
                try:
                    es_path = browser_path + r"\ExtensionSettings"
                    key = winreg.OpenKeyEx(winreg.HKEY_LOCAL_MACHINE, es_path, 0, winreg.KEY_WRITE)
                    try: winreg.DeleteValue(key, EXTENSION_ID)
                    except Exception: pass
                    winreg.CloseKey(key)
                except Exception:
                    pass
        except ImportError:
            pass

        # 3. Remove scheduled task
        try:
            subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 'Unregister-ScheduledTask -TaskName "FryNetworks_WebAgent_BraveRepair" -Confirm:$false -ErrorAction SilentlyContinue'],
                capture_output=True, timeout=15
            )
        except Exception:
            pass

        # 4. Remove --load-extension from Brave shortcuts
        try:
            ps_script = f'''
$ws = New-Object -ComObject WScript.Shell
$locations = @(
    "$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs",
    "$env:PUBLIC\\Desktop",
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path ([Environment]::GetFolderPath("UserProfile")) "OneDrive\\Desktop")
)
foreach ($loc in $locations) {{
    if (-not (Test-Path $loc)) {{ continue }}
    Get-ChildItem $loc -Recurse -Filter "*.lnk" -ErrorAction SilentlyContinue | ForEach-Object {{
        $shortcut = $ws.CreateShortcut($_.FullName)
        if ($shortcut.TargetPath -like "*brave*" -and $shortcut.Arguments -like "*--load-extension*") {{
            $shortcut.Arguments = ($shortcut.Arguments -replace '\\s*--load-extension=[^\\s]*', '').Trim()
            $shortcut.Save()
        }}
    }}
}}
'''
            subprocess.run(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
                capture_output=True, timeout=30
            )
        except Exception:
            pass

        # 5. Remove Defender exclusion
        try:
            subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 f"Remove-MpPreference -ExclusionPath '{ext_dir_str}'"],
                capture_output=True, timeout=15
            )
        except Exception:
            pass

        # 6. Remove repair script
        try:
            repair_script = Path(local_app_data) / 'Orbit' / 'repair_brave_shortcuts.ps1'
            if repair_script.exists():
                repair_script.unlink()
        except Exception:
            pass

        # 7. Uninstall Orbit via Squirrel
        try:
            orbit_updater = Path(local_app_data) / 'Orbit' / 'Update.exe'
            if orbit_updater.exists():
                subprocess.run(
                    [str(orbit_updater), '--uninstall'],
                    capture_output=True, timeout=120
                )
        except Exception:
            pass

    @QtCore.Slot(int, str)
    def _update_progress_main_thread(self, value: int, message: str):
        """Update progress in main thread."""
        self.progress_bar.setValue(value)
        self.progress_label.setText(message)
        try:
            self._last_progress_value = max(self._last_progress_value, int(value))
        except Exception:
            pass
        self.status_bar.setText(message)
    
    def installation_completed(self, result: dict):
        """Handle successful installation completion."""
        try:
            self._invoke_installation_completed.emit(result)
        except Exception:
            try:
                QtCore.QTimer.singleShot(0, lambda r=result: self._installation_completed_main_thread(r))
            except Exception:
                pass
    
    def installation_failed(self, title: str, errors: list):
        """Handle installation failure.""" 
        try:
            self._invoke_installation_failed.emit(title, errors)
        except Exception:
            try:
                QtCore.QTimer.singleShot(0, lambda t=title, e=errors: self._installation_failed_main_thread(t, e))
            except Exception:
                pass
        self._post_install_mode = False
    
    @QtCore.Slot(dict)
    def _installation_completed_main_thread(self, result: dict):
        """Handle installation completion in main thread."""
        # Reset thread/cancel flags so subsequent installs can start
        self.installation_thread = None
        self._cancel_requested = False
        # Restore hidden key/conflict sections so user can start another install immediately
        try:
            self._exit_installation_focus_mode()
        except Exception:
            pass

        # Re-enable UI
        self.key_input.setEnabled(True)
        try:
            nb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'NextButton', 0)))
            if nb is not None:
                nb.setEnabled(True)
            fb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)))
            if fb is not None:
                # Change Finish button text to clearer completion action
                self.wizard.setButtonText(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)), "Finish")
                fb.setEnabled(True)
            bb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'BackButton', 0)))
            if bb is not None:
                # Hide Back button after successful install to simplify flow
                bb.setVisible(False)
            # Show "Install Another Miner" custom button if available
            try:
                if hasattr(self, 'install_another_button') and self.install_another_button is not None:
                    self.install_another_button.setVisible(True)
            except Exception:
                pass
        except Exception:
            pass
        
        self.status_bar.setText("Installation completed successfully")
        self._post_install_mode = True
        # Update review label to success summary with next-step hints
        try:
            if hasattr(self, 'review_label') and self.review_label is not None:
                self.review_label.setText("<b>Installation complete.</b><br>Use 'Install Another Miner' to deploy another key or 'Finish & Exit' to close the installer.")
        except Exception:
            pass

        # Launch GUI if requested (fresh install passes _launch_gui=True in result)
        # This runs on main thread so QTimer.singleShot works correctly
        if result.get("_launch_gui"):
            try:
                from pathlib import Path
                miner_code = result.get("_launch_miner_code")
                gui_version = result.get("_launch_gui_version")
                dir_hints = result.get("_launch_dir_hints") or []
                # Find first valid directory from hints
                chosen_dir = None
                for hint in dir_hints:
                    if hint:
                        candidate = Path(hint)
                        if candidate.exists():
                            chosen_dir = candidate
                            break
                if miner_code and chosen_dir:
                    self._maybe_launch_gui_post_install(
                        miner_code=miner_code,
                        install_result=result,
                        install_dir_hint=str(chosen_dir),
                        gui_version_hint=gui_version,
                    )
            except Exception as launch_err:
                self._debug_log(f"GUI launch from main thread failed: {launch_err}")

    # --- Cancellation & Rollback ---
    def _on_cancel_clicked(self):
        """User pressed Cancel during installation or normal wizard flow."""
        ctx = getattr(self, "_last_install_ctx", None)
        if getattr(self, 'installation_thread', None):
            # Active installation: request cancellation and perform rollback/reset
            self._cancel_requested = True
            try:
                self.status_bar.setText("Cancelling installation - rolling back...")
                self.progress_label.setText("Cancelling...")
                if hasattr(self, 'progress_bar') and self.progress_bar is not None:
                    self.progress_bar.setMaximum(0)  # Indeterminate
            except Exception:
                pass

            def _do_reset_after_cancel():
                try:
                    safe_ctx: Dict[str, Any] = ctx if isinstance(ctx, dict) else {}
                    opt_val = safe_ctx.get("options") if isinstance(safe_ctx.get("options"), dict) else {}
                    miner_val = safe_ctx.get("miner_info") if isinstance(safe_ctx.get("miner_info"), dict) else {}
                    opt_ctx: Dict[str, Any] = opt_val if isinstance(opt_val, dict) else {}
                    miner_ctx: Dict[str, Any] = miner_val if isinstance(miner_val, dict) else {}
                    install_dir = opt_ctx.get("install_dir") if isinstance(opt_ctx, dict) else None
                    self._handle_cancel_rollback(install_dir, miner_ctx, opt_ctx)
                except Exception:
                    # Even if rollback fails, reset UI so user can continue
                    try:
                        self._exit_installation_focus_mode()
                    except Exception:
                        pass
                # Ensure thread state is cleared so next install can start
                self.installation_thread = None
                self._cancel_requested = False
                try:
                    self.clear_form()
                except Exception:
                    pass
                try:
                    self.wizard.restart()
                except Exception:
                    pass
                try:
                    self.wizard.setVisible(True)
                    self.wizard.raise_()
                    self.wizard.activateWindow()
                except Exception:
                    pass
                try:
                    self.status_bar.setText("Ready - Enter a miner key to begin")
                except Exception:
                    pass

            try:
                QtCore.QTimer.singleShot(0, _do_reset_after_cancel)
            except Exception:
                _do_reset_after_cancel()
            return

        # No active install: reset to initial page instead of minimizing
        try:
            self.hide_manage_panel()
        except Exception:
            pass
        try:
            self._exit_installation_focus_mode()
        except Exception:
            pass
        try:
            self.clear_form()
        except Exception:
            pass
        try:
            self.wizard.restart()
        except Exception:
            pass
        try:
            self.wizard.setVisible(True)
            self.wizard.raise_()
            self.wizard.activateWindow()
        except Exception:
            pass
        try:
            self.status_bar.setText("Ready - Enter a miner key to begin")
        except Exception:
            pass

    def _handle_cancel_rollback(self, install_dir: Optional[str], miner_info: Dict[str, Any], options: dict):
        """Perform rollback of partial installation after cancellation."""
        try:
            self.status_bar.setText("Rollback in progress...")
            # Attempt cleanup using ServiceManager uninstall (tolerant to partial state)
            try:
                if miner_info and miner_info.get('code'):
                    sm = ServiceManager(miner_info['code'], version=options.get('poc_version') or options.get('desired_version') or "1.0.0")
                    if install_dir:
                        sm.uninstall_service(install_dir=install_dir)
                    else:
                        sm.uninstall_service()
            except Exception as e:
                self.log_progress(f"[warning] Rollback exception: {e}")
            # Restore progress bar to determinate and mark cancelled
            try:
                self.progress_bar.setMaximum(100)
                self.progress_bar.setValue(0)
            except Exception:
                pass
            self.progress_label.setText("Installation cancelled")
            self.log_progress("Installation cancelled by user. Partial changes cleaned up where possible.")
            self.status_bar.setText("Cancelled")
        finally:
            # Re-enable UI controls
            try:
                nb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'NextButton', 0)))
                if nb is not None:
                    nb.setEnabled(True)
                fb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)))
                if fb is not None:
                    fb.setEnabled(False)
            except Exception:
                pass
            self.installation_thread = None
            self._cancel_requested = False

        # Restore hidden sections
        self._exit_installation_focus_mode()
        # Restore Review page title/subtitle if we cleared them when install started
        try:
            if getattr(self, 'review_page', None) is not None:
                try:
                    if getattr(self, '_review_orig_title', None) is not None:
                        self.review_page.setTitle(str(self._review_orig_title))
                except Exception:
                    pass
                try:
                    if getattr(self, '_review_orig_subtitle', None) is not None:
                        self.review_page.setSubTitle(str(self._review_orig_subtitle))
                except Exception:
                    pass
        except Exception:
            pass
        # Restore original wizard stylesheet (undo header-hiding)
        try:
            if getattr(self, '_wizard_orig_qss', None) is not None:
                try:
                    self.wizard.setStyleSheet(str(self._wizard_orig_qss))
                except Exception:
                    pass
        except Exception:
            pass
        
        # Cancellation path: DO NOT show success block or modify buttons beyond re-enable logic above.
        # Leave installer ready for a new attempt without implying success.
        return
    
    @QtCore.Slot(str, list)  
    def _installation_failed_main_thread(self, title: str, errors: list):
        """Handle installation failure in main thread."""
        # Reset installation thread state to allow retry
        self.installation_thread = None
        
        # Re-enable UI
        self.key_input.setEnabled(True) 
        try:
            nb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'NextButton', 0)))
            if nb is not None:
                nb.setEnabled(True)
            fb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'FinishButton', 0)))
            if fb is not None:
                fb.setEnabled(True)
            bb = self.wizard.button(cast(Any, getattr(QtWidgets.QWizard, 'BackButton', 0)))
            if bb is not None:
                bb.setEnabled(True)
        except Exception:
            pass
    
        self.status_bar.setText("Installation failed")
        
        # Clear/reset progress indicators to show failure clearly
        try:
            if hasattr(self, 'progress_bar') and self.progress_bar is not None:
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat("Failed")
            if hasattr(self, 'step6_progress') and self.step6_progress is not None:
                self.step6_progress.setValue(0)
                self.step6_progress.setFormat("Failed")
        except Exception:
            pass
    
        # Restore hidden sections so user can act (change key, etc.)
        self._exit_installation_focus_mode()
    
        # Concise error preview
        err_preview = "\n".join([f"- {e}" for e in (errors or [])][:6])

        # Decision dialog
        dlg = QtWidgets.QMessageBox(self)
        dlg.setWindowTitle(title or "Installation Failed")
        dlg.setIcon(QtWidgets.QMessageBox.Icon.Critical)
        dlg.setText("The installation failed.")
        if err_preview:
            dlg.setInformativeText(err_preview)

        retry_btn = dlg.addButton("Retry", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        change_key_btn = dlg.addButton("Change Miner Key", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        view_log_btn = dlg.addButton("View Log", QtWidgets.QMessageBox.ButtonRole.HelpRole)
        cancel_btn = dlg.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole)

        dlg.setDefaultButton(retry_btn)
        dlg.exec()

        clicked = dlg.clickedButton()
        if clicked == retry_btn:
            # Reset progress indicators/logs so the next attempt starts cleanly
            try:
                self._clear_installation_progress()
            except Exception:
                pass
            # Keep summary hidden for maximum space
            try:
                if hasattr(self, "review_label"):
                    parent_group = self.review_label.parentWidget()
                    if parent_group is not None:
                        parent_group.setVisible(False)
            except Exception:
                pass
            self._enter_installation_focus_mode()
            self._retry_last_install()

        elif clicked == change_key_btn:
            self._switch_to_key_entry_for_retry()

        elif clicked == view_log_btn:
            try:
                log_path = getattr(self, '_debug_log_path', None)
                if log_path and Path(log_path).exists():
                    os.startfile(str(log_path))
            except Exception:
                pass
            # Reopen the dialog to allow a subsequent choice
            self._installation_failed_main_thread(title, errors)

        else:
            # Cancel: leave as-is (user can close or adjust later)
            pass
 
    def _retry_last_install(self):
        """Retry the last installation with the same context."""
        ctx = getattr(self, "_last_install_ctx", None)
        if not ctx:
            QtWidgets.QMessageBox.information(self, "Retry", "No previous install context to retry.")
            return

        # Restore UI from saved options for determinism
        try:
            opts = ctx.get("options", {})
            # Only remaining UI options
            self.auto_start.setChecked(bool(opts.get("auto_start", True)))
            if hasattr(self, 'desktop_shortcut'):
                self.desktop_shortcut.setChecked(bool(opts.get("create_desktop_shortcut", False)))
            if hasattr(self, 'pin_start_checkbox'):
                self.pin_start_checkbox.setChecked(bool(opts.get("pin_start_menu", False)))

            # Path
            self.install_path_edit.setText(opts.get("install_dir", "") or "")

            # Screen size
            screen_val = opts.get("screen_size", "auto")
            for i, (_, val) in enumerate(self._screen_size_choices):
                if val == screen_val:
                    self.screen_size_combo.setCurrentIndex(i)
                    break



            # Key
            self.key_input.setText(ctx.get("key", ""))

            # Consider the key validated (so Finish is enabled on Review page)
            self.is_key_validated = True
        except Exception:
            pass

        # Start again
        self.install_miner()

    def _switch_to_key_entry_for_retry(self):
        """Send user back to Key page to change the key and re-validate."""
        try:
            self.key_input.setEnabled(True)
            self.is_key_validated = False
            self.key_status.setText("")
            self.miner_info_label.setText("")
            self.miner_group.setVisible(False)
            self.conflict_status.setText("Enter a miner key to check compatibility")
            self.conflict_status.setStyleSheet("")

            # Clear progress area to reduce confusion
            try:
                self.progress_log.clear()
                self.progress_group.setVisible(False)
            except Exception:
                pass

            self.wizard.restart()
            self.status_bar.setText("Enter a new miner key and validate.")
            self.key_input.setFocus()
        except Exception:
            pass

    # ---- Settings confirmation and focus mode helpers ----
    def _confirm_settings(self):
        """User confirmed settings; proceed to review page."""
        self.settings_confirmed = True
        self.status_bar.setText("Settings confirmed - Proceed to review")

    def _back_to_install(self):
        """Return to key entry page without confirming settings."""
        self.wizard.restart()
        if not getattr(self, "settings_confirmed", False):
            try:
                fb = self.wizard.button(self.FINISH_BUTTON)
                if fb is not None:
                    fb.setEnabled(False)
            except Exception:
                pass
        self.status_bar.setText("Review settings later or validate again.")

    def _enter_installation_focus_mode(self):
        """Hide non-essential sections to create more room during install."""
        for w in [getattr(self, 'key_group', None), getattr(self, 'miner_group', None), getattr(self, 'conflict_group', None)]:
            try:
                if w is not None:
                    w.setVisible(False)
            except Exception:
                pass

    def _exit_installation_focus_mode(self):
        """Restore hidden sections after install completes or fails."""
        for w in [getattr(self, 'key_group', None), getattr(self, 'miner_group', None), getattr(self, 'conflict_group', None)]:
            try:
                if w is not None:
                    w.setVisible(True)
            except Exception:
                pass

    def _create_desktop_shortcut_for_miner(self, miner_code: str, install_path: Path, gui_version: Optional[str]) -> Path:
        """Create a Windows desktop shortcut to the miner GUI executable."""
        if os.name != 'nt':
            return Path()
        
        # Ensure install_path is a Path object
        if not isinstance(install_path, Path):
            install_path = Path(install_path)
        
        # Determine GUI executable name
        gui_filename = naming.gui_asset(miner_code, gui_version or "unknown", windows=True)
        gui_exe = install_path / gui_filename
        
        # If exact filename doesn't exist, try to find any GUI exe for this miner
        if not gui_exe.exists():
            # Try to find any FRY_{miner_code}_v*.exe file
            gui_pattern = f"FRY_{miner_code}_v*.exe"
            matching_files = list(install_path.glob(gui_pattern))
            if matching_files:
                gui_exe = matching_files[0]  # Use the first match
            else:
                raise FileNotFoundError(f"GUI executable not found: {gui_exe} (searched in {install_path})")
        
        # Desktop folder
        desktop = Path.home() / "Desktop"
        if not desktop.exists():
            # Fallback: try OneDrive Desktop or create
            desktop = Path.home() / "OneDrive" / "Desktop"
            if not desktop.exists():
                desktop = Path.home() / "Desktop"
                desktop.mkdir(parents=True, exist_ok=True)
        
        shortcut_name = f"FryNetworks {miner_code} Miner.lnk"
        shortcut_path = desktop / shortcut_name
        self._create_windows_shortcut(
            shortcut_path=shortcut_path,
            target_path=gui_exe,
            working_dir=install_path,
            description=f"FryNetworks {miner_code} Miner GUI"
        )
        return shortcut_path

    def _create_start_menu_shortcut_for_miner(self, miner_code: str, install_path: Path, gui_version: Optional[str]) -> Path:
        """Create a Start Menu shortcut under the FryNetworks folder."""
        if os.name != 'nt':
            return Path()

        if not isinstance(install_path, Path):
            install_path = Path(install_path)

        gui_filename = naming.gui_asset(miner_code, gui_version or "unknown", windows=True)
        gui_exe = install_path / gui_filename
        if not gui_exe.exists():
            matching_files = list(install_path.glob(f"FRY_{miner_code}_v*.exe"))
            if matching_files:
                gui_exe = matching_files[0]
            else:
                raise FileNotFoundError(f"GUI executable not found for Start menu shortcut in {install_path}")

        start_base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        start_dir = start_base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "FryNetworks"
        start_dir.mkdir(parents=True, exist_ok=True)
        shortcut_path = start_dir / f"FryNetworks {miner_code} Miner.lnk"
        self._create_windows_shortcut(
            shortcut_path=shortcut_path,
            target_path=gui_exe,
            working_dir=install_path,
            description=f"FryNetworks {miner_code} Miner GUI"
        )
        return shortcut_path

    def _create_startup_shortcut_for_miner(self, miner_code: str, install_path: Path, gui_version: Optional[str]) -> Path:
        """Create an autostart entry so the GUI launches when the user signs in."""
        if not isinstance(install_path, Path):
            install_path = Path(install_path)

        if os.name == 'nt':
            gui_filename = naming.gui_asset(miner_code, gui_version or "unknown", windows=True)
            gui_exe = install_path / gui_filename
            if not gui_exe.exists():
                matches = list(install_path.glob(f"FRY_{miner_code}_v*.exe"))
                if matches:
                    gui_exe = matches[0]
                else:
                    raise FileNotFoundError(f"GUI executable not found for Startup shortcut in {install_path}")

            startup_dir = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / \
                "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            startup_dir.mkdir(parents=True, exist_ok=True)
            shortcut_path = startup_dir / f"FryNetworks {miner_code} Miner.lnk"
            self._create_windows_shortcut(
                shortcut_path=shortcut_path,
                target_path=gui_exe,
                working_dir=install_path,
                description=f"FryNetworks {miner_code} Miner GUI"
            )
            return shortcut_path

        if sys.platform.startswith("linux"):
            gui_filename = naming.gui_asset(miner_code, gui_version or "unknown", windows=False)
            gui_exe = install_path / gui_filename
            if not gui_exe.exists():
                matches = list(install_path.glob(f"{naming.gui_prefix(miner_code)}*"))
                if matches:
                    gui_exe = matches[0]
                else:
                    raise FileNotFoundError(f"GUI executable not found for Startup entry in {install_path}")

            config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            autostart_dir = config_home / "autostart"
            autostart_dir.mkdir(parents=True, exist_ok=True)
            desktop_file = autostart_dir / f"frynetworks-{miner_code.lower()}-miner.desktop"

            desktop_contents = "\n".join([
                "[Desktop Entry]",
                "Type=Application",
                f"Name=FryNetworks {miner_code} Miner GUI",
                f"Exec={gui_exe}",
                f"Path={install_path}",
                "X-GNOME-Autostart-enabled=true",
                "Terminal=false",
                "Hidden=false",
            ])
            desktop_file.write_text(desktop_contents, encoding="utf-8")
            return desktop_file

        # macOS or other platforms: no-op for now
        return Path()

    def _detect_existing_shortcuts(self, miner_code: str) -> Dict[str, Path]:
        """Detect existing miner shortcuts/pins so updates can recreate them."""
        shortcuts: Dict[str, Path] = {}

        shortcut_name = f"FryNetworks {miner_code} Miner.lnk"

        if os.name == 'nt':
            # Desktop / OneDrive Desktop
            try:
                desktop_candidates = [
                    Path.home() / "Desktop",
                    Path.home() / "OneDrive" / "Desktop",
                ]
                for desk in desktop_candidates:
                    path = desk / shortcut_name
                    if path.exists():
                        shortcuts["desktop"] = path
                        break
            except Exception:
                pass

            # Start Menu
            try:
                start_base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
                start_dir = start_base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "FryNetworks"
                start_path = start_dir / shortcut_name
                if start_path.exists():
                    shortcuts["start_menu"] = start_path
            except Exception:
                pass

            # Startup (auto-launch)
            try:
                startup_dir = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / \
                    "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                startup_path = startup_dir / shortcut_name
                if startup_path.exists():
                    shortcuts["startup"] = startup_path
            except Exception:
                pass

            # Taskbar pinned shortcut
            try:
                taskbar_dir = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / \
                    "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar"
                taskbar_path = taskbar_dir / shortcut_name
                if taskbar_path.exists():
                    shortcuts["taskbar"] = taskbar_path
            except Exception:
                pass

        elif sys.platform.startswith("linux"):
            try:
                config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                autostart_dir = config_home / "autostart"
                desktop_file = autostart_dir / f"frynetworks-{miner_code.lower()}-miner.desktop"
                if desktop_file.exists():
                    shortcuts["startup"] = desktop_file
            except Exception:
                pass

        return shortcuts

    def _pin_gui_to_taskbar(
        self,
        miner_code: str,
        install_path: Path,
        gui_version: Optional[str],
        existing_shortcut: Optional[Path] = None
    ) -> None:
        """Pin the GUI executable to the Windows taskbar."""
        if os.name != 'nt':
            return
        if not isinstance(install_path, Path):
            install_path = Path(install_path)

        gui_filename = naming.gui_asset(miner_code, gui_version or "unknown", windows=True)
        gui_exe = install_path / gui_filename
        if not gui_exe.exists():
            matching_files = list(install_path.glob(f"FRY_{miner_code}_v*.exe"))
            if matching_files:
                gui_exe = matching_files[0]
            else:
                raise FileNotFoundError(f"GUI executable not found for taskbar pinning in {install_path}")

        shortcut_path = None
        cleanup_shortcut = False
        if existing_shortcut and Path(existing_shortcut).exists():
            shortcut_path = Path(existing_shortcut)
        else:
            import tempfile
            temp_dir = Path(tempfile.gettempdir()) / "FryNetworks" / "TaskbarPins"
            temp_dir.mkdir(parents=True, exist_ok=True)
            shortcut_path = temp_dir / f"FryNetworks_{miner_code}_pin.lnk"
            self._create_windows_shortcut(
                shortcut_path=shortcut_path,
                target_path=gui_exe,
                working_dir=install_path,
                description=f"FryNetworks {miner_code} Miner GUI"
            )
            cleanup_shortcut = True

        shortcut_str = str(shortcut_path).replace('"', '`"')
        ps_script = f"""
$shortcut = "{shortcut_str}"
if (-Not (Test-Path $shortcut)) {{ return }}
$shell = New-Object -ComObject Shell.Application
$folder = $shell.Namespace((Split-Path $shortcut))
$item = $folder.ParseName((Split-Path $shortcut -Leaf))
if ($item -ne $null) {{
    $verb = $item.Verbs() | Where-Object {{ $_.Name.Replace('&','') -match 'Pin to taskbar' }}
    if ($verb) {{ $verb.DoIt() }}
}}
"""
        subprocess.run(
            ["powershell.exe", "-NoLogo", "-WindowStyle", "Hidden", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            check=False,
            capture_output=True,
            text=True,
            timeout=10
        )

        if cleanup_shortcut:
            try:
                shortcut_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _pin_gui_to_start(
        self,
        miner_code: str,
        install_path: Path,
        gui_version: Optional[str],
        existing_shortcut: Optional[Path] = None
    ) -> None:
        """Pin the GUI executable to the Windows Start menu tiles."""
        if os.name != 'nt':
            return
        if not isinstance(install_path, Path):
            install_path = Path(install_path)

        gui_filename = naming.gui_asset(miner_code, gui_version or "unknown", windows=True)
        gui_exe = install_path / gui_filename
        if not gui_exe.exists():
            matching_files = list(install_path.glob(f"FRY_{miner_code}_v*.exe"))
            if matching_files:
                gui_exe = matching_files[0]
            else:
                raise FileNotFoundError(f"GUI executable not found for Start menu pinning in {install_path}")

        shortcut_path = None
        cleanup_shortcut = False
        if existing_shortcut and Path(existing_shortcut).exists():
            shortcut_path = Path(existing_shortcut)
        else:
            import tempfile
            temp_dir = Path(tempfile.gettempdir()) / "FryNetworks" / "StartPins"
            temp_dir.mkdir(parents=True, exist_ok=True)
            shortcut_path = temp_dir / f"FryNetworks_{miner_code}_startpin.lnk"
            self._create_windows_shortcut(
                shortcut_path=shortcut_path,
                target_path=gui_exe,
                working_dir=install_path,
                description=f"FryNetworks {miner_code} Miner GUI"
            )
            cleanup_shortcut = True

        shortcut_str = str(shortcut_path).replace('"', '`"')
        ps_script = f"""
$shortcut = "{shortcut_str}"
if (-Not (Test-Path $shortcut)) {{ return }}
$shell = New-Object -ComObject Shell.Application
$folder = $shell.Namespace((Split-Path $shortcut))
$item = $folder.ParseName((Split-Path $shortcut -Leaf))
if ($item -ne $null) {{
    $verb = $item.Verbs() | Where-Object {{ $_.Name.Replace('&','') -match 'Pin to Start' }}
    if ($verb) {{ $verb.DoIt() }}
}}
"""
        subprocess.run(
            ["powershell.exe", "-NoLogo", "-WindowStyle", "Hidden", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            check=False,
            capture_output=True,
            text=True,
            timeout=10
        )

        if cleanup_shortcut:
            try:
                shortcut_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _create_windows_shortcut(
        self,
        shortcut_path: Path,
        target_path: Path,
        working_dir: Path,
        description: str
    ) -> None:
        """Create a Windows shortcut (.lnk)."""
        if os.name != 'nt':
            return
        shortcut_path = Path(shortcut_path)
        shortcut_path.parent.mkdir(parents=True, exist_ok=True)
        target_str = str(target_path).replace('"', '`"')
        working_dir_str = str(working_dir).replace('"', '`"')
        shortcut_str = str(shortcut_path).replace('"', '`"')
        desc_str = description.replace('"', '`"')
        ps_script = f"""
$WshShell = New-Object -comObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{shortcut_str}")
$Shortcut.TargetPath = "{target_str}"
$Shortcut.WorkingDirectory = "{working_dir_str}"
$Shortcut.Description = "{desc_str}"
$Shortcut.Save()
"""
        subprocess.run(
            ["powershell.exe", "-NoLogo", "-WindowStyle", "Hidden", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            check=True,
            capture_output=True,
            text=True,
            timeout=10
        )
