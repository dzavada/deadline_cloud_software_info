#!/usr/bin/env python3
"""
AWS Deadline Cloud Software Viewer
A modern Qt application to query and display available software packages
"""

import sys
import subprocess
import json
import re
import yaml
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QStatusBar, QMessageBox, QProgressBar, QHeaderView, QGroupBox,
    QTextEdit, QSplitter, QComboBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize
from PyQt6.QtGui import QFont, QPalette, QColor, QIcon


class JobSubmitter(QThread):
    """Background thread for job submission and monitoring"""
    
    status_update = pyqtSignal(str)
    progress_update = pyqtSignal(int)
    job_completed = pyqtSignal(str)  # Emits job output
    error_occurred = pyqtSignal(str)
    job_id_received = pyqtSignal(str)  # Emits job ID when received
    
    def __init__(self, farm_id: str, queue_id: str, bundle_path: str, conda_channel: str = "deadline-cloud"):
        super().__init__()
        self.farm_id = farm_id
        self.queue_id = queue_id
        self.bundle_path = bundle_path
        self.conda_channel = conda_channel
        self.job_id = None
        self._running = True
    
    def run(self):
        """Submit job and monitor until completion"""
        try:
            # Submit the job
            self.status_update.emit("Submitting job to AWS Deadline Cloud...")
            self.progress_update.emit(10)
            
            job_id = self._submit_job()
            if not job_id:
                self.error_occurred.emit("Failed to submit job")
                return
            
            self.job_id = job_id
            self.job_id_received.emit(job_id)  # Emit job ID signal
            self.status_update.emit(f"Job submitted: {job_id}")
            self.progress_update.emit(30)
            
            # Monitor job status
            self.status_update.emit("Waiting for job to complete...")
            output = self._wait_for_completion()
            
            if output:
                self.progress_update.emit(100)
                self.status_update.emit("Job completed successfully")
                self.job_completed.emit(output)
            else:
                self.error_occurred.emit("Failed to retrieve job output")
                
        except Exception as e:
            self.error_occurred.emit(f"Error: {str(e)}")
    
    def _submit_job(self) -> Optional[str]:
        """Submit job to Deadline Cloud"""
        try:
            cmd = [
                "deadline", "bundle", "submit",
                self.bundle_path,
                "--farm-id", self.farm_id,
                "--queue-id", self.queue_id,
                "--parameter", f"CondaChannel={self.conda_channel}"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )
            
            # Parse output to get job ID
            # The output may be YAML or plain text with job ID
            output = result.stdout.strip()
            if not output:
                return None
            
            # Try YAML parsing first
            try:
                data = yaml.safe_load(output)
                if isinstance(data, dict):
                    return data.get("jobId")
            except:
                pass
            
            # Try to extract job ID from text output
            # Look for patterns like "job-xxxxx" or "jobId: job-xxxxx"
            import re
            job_id_match = re.search(r'job-[a-f0-9]+', output, re.IGNORECASE)
            if job_id_match:
                return job_id_match.group(0)
            
            # If we still can't find it, raise an error with the output for debugging
            raise Exception(f"Could not extract job ID from output: {output[:200]}")
            
        except subprocess.CalledProcessError as e:
            raise Exception(f"Job submission failed: {e.stderr}")
    
    def _wait_for_completion(self) -> Optional[str]:
        """Wait for job completion and retrieve logs"""
        max_wait = 300  # 5 minutes max
        check_interval = 5  # Check every 5 seconds
        elapsed = 0
        
        while self._running and elapsed < max_wait:
            try:
                # Check job status
                status_cmd = [
                    "deadline", "job", "get",
                    "--farm-id", self.farm_id,
                    "--queue-id", self.queue_id,
                    "--job-id", self.job_id
                ]
                
                result = subprocess.run(
                    status_cmd,
                    capture_output=True,
                    text=True,
                    check=True
                )
                
                # Parse YAML output
                data = yaml.safe_load(result.stdout)
                lifecycle_status = data.get("lifecycleStatus", "") if isinstance(data, dict) else ""
                task_run_status = data.get("taskRunStatus", "") if isinstance(data, dict) else ""
                
                # Update status message with current statuses
                self.status_update.emit(f"Job: {lifecycle_status}, Tasks: {task_run_status}...")
                
                # Check if tasks have completed successfully
                # taskRunStatus will be "SUCCEEDED" when all tasks finish
                if task_run_status == "SUCCEEDED":
                    # Wait for logs to be available in CloudWatch (5 second delay)
                    self.status_update.emit("Tasks completed, waiting for logs...")
                    QThread.msleep(5000)  # 5 second wait
                    # Tasks finished successfully, get output
                    return self._get_job_logs()
                elif task_run_status in ["FAILED", "CANCELED"]:
                    raise Exception(f"Tasks {task_run_status.lower()}")
                elif lifecycle_status in ["FAILED", "CANCELED", "CREATE_FAILED", "UPDATE_FAILED"]:
                    raise Exception(f"Job {lifecycle_status.lower()}")
                
                # Job/tasks still running - continue waiting
                
                # Update progress
                progress = min(90, 30 + (elapsed / max_wait * 60))
                self.progress_update.emit(int(progress))
                
                # Wait before next check
                QThread.msleep(check_interval * 1000)
                elapsed += check_interval
                
            except Exception as e:
                raise Exception(f"Status check failed: {str(e)}")
        
        if elapsed >= max_wait:
            raise Exception("Job timeout - exceeded maximum wait time")
        
        return None
    
    def _get_job_logs(self) -> str:
        """Retrieve job logs using deadline job logs command"""
        try:
            # Use the deadline job logs command to retrieve logs
            logs_cmd = [
                "deadline", "job", "logs",
                "--job-id", self.job_id
            ]
            
            result = subprocess.run(
                logs_cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=60
            )
            
            # Return the full log output
            return result.stdout
            
        except subprocess.CalledProcessError as e:
            raise Exception(f"Failed to retrieve job logs: {e.stderr}")
        except subprocess.TimeoutExpired:
            raise Exception("Timeout while retrieving job logs")
    
    def stop(self):
        """Stop the thread"""
        self._running = False


class SoftwareParser:
    """Parse conda output to extract software information"""
    
    @staticmethod
    def parse_conda_output(output: str) -> List[Dict[str, str]]:
        """
        Parse conda search output and extract software info
        
        Returns:
            List of dictionaries with keys: name, version, build
        """
        software_list = []
        
        # Look for lines matching conda package format with various timestamp formats
        # Examples:
        # [2025-12-03T21:49:43.773000+00:00] blender 3.6.23 481731fa3deb7292fd3d0f1fbec830787d44c023_0 deadline-cloud
        # 2025/12/06 11:27:34-08:00 blender 4.5.0 hb0f4dca_0 Conda/Default
        # Pattern matches optional timestamp, then package name, version, build, and channel
        
        # Pattern that matches both timestamp formats and any channel name
        pattern = r'(?:(?:\[[\d\-T:+\.]+\]|\d{4}/\d{2}/\d{2}\s+[\d:\-]+)\s+)?(\S+)\s+([\d\.]+)\s+(\S+)\s+(\S+)'
        
        for line in output.split('\n'):
            # Skip header lines, empty lines, and system messages
            if not line.strip() or line.strip().startswith('#'):
                continue
            
            # Skip conda table header line
            if 'Name' in line and 'Version' in line and 'Build' in line:
                continue
            
            # Skip system log messages - be more specific to avoid false positives
            lower_line = line.lower()
            if any(pattern in lower_line for pattern in [
                'process pid', 'exited with code', 'retrieving logs', 'retrieved ', 
                'uploading output', 'job attachments', 'session session',
                'worker 0 of 0', 'messages (0 of 0)'
            ]):
                continue
            
            # Skip separator lines
            if line.strip().startswith('---') or line.strip() == '':
                continue
                
            match = re.search(pattern, line)
            if match:
                name = match.group(1)
                version = match.group(2)
                build = match.group(3)
                channel = match.group(4)
                
                # Filter out non-package lines (like system info, conda commands, etc.)
                # Package names should be lowercase alphanumeric with hyphens/underscores
                if re.match(r'^[a-z0-9_\-]+$', name, re.IGNORECASE):
                    software_list.append({
                        'name': name,
                        'version': version,
                        'build': build,
                        'channel': channel
                    })
        
        return software_list


class FarmQueueLoader(QThread):
    """Background thread for loading farms and queues"""
    
    farms_loaded = pyqtSignal(list)  # Emits list of farms
    queues_loaded = pyqtSignal(list)  # Emits list of queues
    error_occurred = pyqtSignal(str)
    
    def __init__(self, farm_id: Optional[str] = None):
        super().__init__()
        self.farm_id = farm_id
    
    def run(self):
        """Load farms or queues"""
        try:
            if self.farm_id:
                # Load queues for specific farm
                queues = self._load_queues()
                self.queues_loaded.emit(queues)
            else:
                # Load all farms
                farms = self._load_farms()
                self.farms_loaded.emit(farms)
        except Exception as e:
            self.error_occurred.emit(str(e))
    
    def _load_farms(self) -> List[Dict[str, str]]:
        """Load available farms"""
        try:
            result = subprocess.run(
                ["deadline", "farm", "list"],
                capture_output=True,
                text=True,
                check=True
            )
            
            # Parse YAML output
            farms = yaml.safe_load(result.stdout)
            if not farms:
                return []
            
            return [
                {
                    "id": farm.get("farmId", ""),
                    "name": farm.get("displayName", farm.get("farmId", "Unknown"))
                }
                for farm in farms
            ]
        except subprocess.CalledProcessError as e:
            raise Exception(f"Failed to load farms: {e.stderr}")
        except yaml.YAMLError:
            raise Exception("Failed to parse farms data")
    
    def _load_queues(self) -> List[Dict[str, str]]:
        """Load queues for a specific farm"""
        try:
            result = subprocess.run(
                ["deadline", "queue", "list", "--farm-id", self.farm_id],
                capture_output=True,
                text=True,
                check=True
            )
            
            # Parse YAML output
            queues = yaml.safe_load(result.stdout)
            if not queues:
                return []
            
            return [
                {
                    "id": queue.get("queueId", ""),
                    "name": queue.get("displayName", queue.get("queueId", "Unknown"))
                }
                for queue in queues
            ]
        except subprocess.CalledProcessError as e:
            raise Exception(f"Failed to load queues: {e.stderr}")
        except yaml.YAMLError:
            raise Exception("Failed to parse queues data")


class ConnectionChecker(QThread):
    """Background thread for checking AWS Deadline Cloud connection"""
    
    connection_status = pyqtSignal(bool, str)  # (is_connected, message)
    version_warning = pyqtSignal(str)  # Emits version warning message
    
    def __init__(self, timeout: int = 60):
        super().__init__()
        self.timeout = timeout
    
    def run(self):
        """Check connection to AWS Deadline Cloud"""
        try:
            # First, check Deadline CLI version
            version_check = self._check_version()
            if version_check:
                self.version_warning.emit(version_check)
            
            result = subprocess.run(
                ["deadline", "farm", "list"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True
            )
            
            # If we get here, we're connected
            try:
                farms = yaml.safe_load(result.stdout)
                farm_count = len(farms) if farms else 0
                self.connection_status.emit(True, f"Connected ({farm_count} farms)")
            except:
                self.connection_status.emit(True, "Connected")
                
        except subprocess.TimeoutExpired:
            self.connection_status.emit(False, "Connection Timeout")
        except FileNotFoundError:
            self.connection_status.emit(False, "Deadline CLI Not Found")
        except subprocess.CalledProcessError as e:
            if "credentials" in e.stderr.lower() or "auth" in e.stderr.lower():
                self.connection_status.emit(False, "Authentication Failed")
            else:
                self.connection_status.emit(False, "Connection Error")
        except Exception as e:
            self.connection_status.emit(False, "Not Connected")
    
    def _check_version(self) -> Optional[str]:
        """Check Deadline CLI version and return warning if below minimum"""
        try:
            result = subprocess.run(
                ["deadline", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True
            )
            
            # Parse version from output (e.g., "deadline version 0.51.1")
            output = result.stdout.strip()
            version_match = re.search(r'(\d+)\.(\d+)\.(\d+)', output)
            
            if version_match:
                major = int(version_match.group(1))
                minor = int(version_match.group(2))
                patch = int(version_match.group(3))
                
                # Check if version is less than 0.51.1
                if (major, minor, patch) < (0, 51, 1):
                    return f"Deadline CLI version {major}.{minor}.{patch} detected. Please upgrade to version 0.51.1 or higher for full compatibility."
            
            return None
            
        except Exception:
            # If we can't check version, don't block the app
            return None


class MainWindow(QMainWindow):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        self.job_thread = None
        self.loader_thread = None
        self.connection_checker = None
        self.farms_data = []
        self.queues_data = []
        self.init_ui()
        self.load_settings()
        # Check connection status on startup with extended timeout (90 seconds)
        # This allows time for AWS authentication to complete
        QTimer.singleShot(100, lambda: self.check_connection(timeout=90))
        # Auto-load farms on startup
        QTimer.singleShot(500, self.refresh_farms)
        # Periodic connection check (every 30 seconds) with standard timeout
        self.connection_timer = QTimer()
        self.connection_timer.timeout.connect(lambda: self.check_connection(timeout=30))
        self.connection_timer.start(30000)
    
    def init_ui(self):
        """Initialize the user interface"""
        self.setWindowTitle("AWS Deadline Cloud - Software Viewer")
        self.setMinimumSize(1000, 700)
        
        # Apply modern styling
        self.apply_modern_style()
        
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)
        
        # Title
        title = QLabel("AWS Deadline Cloud Software Viewer")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(title)
        
        # Configuration group
        config_group = QGroupBox("Configuration")
        config_layout = QVBoxLayout()
        
        # Farm ID with dropdown and refresh
        farm_layout = QHBoxLayout()
        farm_label = QLabel("Farm:")
        farm_label.setFixedWidth(100)
        self.farm_combo = QComboBox()
        self.farm_combo.setEditable(True)
        self.farm_combo.setPlaceholderText("Select or enter farm ID...")
        self.farm_combo.currentTextChanged.connect(self.on_farm_changed)
        self.refresh_farms_btn = QPushButton()
        refresh_icon_path = Path(__file__).parent / "icons" / "refresh.png"
        if refresh_icon_path.exists():
            self.refresh_farms_btn.setIcon(QIcon(str(refresh_icon_path)))
            self.refresh_farms_btn.setIconSize(QSize(24, 24))
        else:
            self.refresh_farms_btn.setText("⟳")
        self.refresh_farms_btn.setFixedWidth(40)
        self.refresh_farms_btn.setToolTip("Refresh farms list")
        self.refresh_farms_btn.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #232F3E;
                border: 2px solid #FF9900;
                border-radius: 4px;
                font-size: 20px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #FFF5E6;
                border: 2px solid #EC7211;
            }
            QPushButton:pressed {
                background-color: #FFE6CC;
            }
            QPushButton:disabled {
                background-color: #F5F5F5;
                color: #CCCCCC;
                border: 2px solid #CCCCCC;
            }
        """)
        self.refresh_farms_btn.clicked.connect(self.refresh_farms)
        farm_layout.addWidget(farm_label)
        farm_layout.addWidget(self.farm_combo)
        farm_layout.addWidget(self.refresh_farms_btn)
        config_layout.addLayout(farm_layout)
        
        # Queue ID with dropdown and refresh
        queue_layout = QHBoxLayout()
        queue_label = QLabel("Queue:")
        queue_label.setFixedWidth(100)
        self.queue_combo = QComboBox()
        self.queue_combo.setEditable(True)
        self.queue_combo.setPlaceholderText("Select or enter queue ID...")
        self.refresh_queues_btn = QPushButton()
        if refresh_icon_path.exists():
            self.refresh_queues_btn.setIcon(QIcon(str(refresh_icon_path)))
            self.refresh_queues_btn.setIconSize(QSize(24, 24))
        else:
            self.refresh_queues_btn.setText("⟳")
        self.refresh_queues_btn.setFixedWidth(40)
        self.refresh_queues_btn.setToolTip("Refresh queues list")
        self.refresh_queues_btn.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #232F3E;
                border: 2px solid #FF9900;
                border-radius: 4px;
                font-size: 20px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #FFF5E6;
                border: 2px solid #EC7211;
            }
            QPushButton:pressed {
                background-color: #FFE6CC;
            }
            QPushButton:disabled {
                background-color: #F5F5F5;
                color: #CCCCCC;
                border: 2px solid #CCCCCC;
            }
        """)
        self.refresh_queues_btn.clicked.connect(self.refresh_queues)
        self.refresh_queues_btn.setEnabled(False)
        queue_layout.addWidget(queue_label)
        queue_layout.addWidget(self.queue_combo)
        queue_layout.addWidget(self.refresh_queues_btn)
        config_layout.addLayout(queue_layout)
        
        # Conda Channel with dropdown
        channel_layout = QHBoxLayout()
        channel_label = QLabel("Conda Channel:")
        channel_label.setFixedWidth(100)
        self.channel_combo = QComboBox()
        self.channel_combo.setEditable(True)
        self.channel_combo.setPlaceholderText("Select or enter conda channel...")
        # Add default channels
        self.channel_combo.addItem("deadline-cloud")
        self.channel_combo.addItem("s3://")
        self.channel_combo.setCurrentText("deadline-cloud")
        channel_layout.addWidget(channel_label)
        channel_layout.addWidget(self.channel_combo)
        config_layout.addLayout(channel_layout)
        
        config_group.setLayout(config_layout)
        main_layout.addWidget(config_group)
        
        # Action buttons
        button_layout = QHBoxLayout()
        
        self.submit_btn = QPushButton("Submit Job & Fetch Software List")
        self.submit_btn.setMinimumHeight(40)
        self.submit_btn.clicked.connect(self.submit_job)
        
        self.export_btn = QPushButton("Export to CSV")
        self.export_btn.setMinimumHeight(40)
        self.export_btn.clicked.connect(self.export_to_csv)
        self.export_btn.setEnabled(False)
        
        button_layout.addWidget(self.submit_btn)
        button_layout.addWidget(self.export_btn)
        main_layout.addLayout(button_layout)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)
        
        # Software table
        table_label = QLabel("Available Software Packages:")
        table_label_font = QFont()
        table_label_font.setPointSize(12)
        table_label_font.setBold(True)
        table_label.setFont(table_label_font)
        main_layout.addWidget(table_label)
        
        self.software_table = QTableWidget()
        self.software_table.setColumnCount(3)
        self.software_table.setHorizontalHeaderLabels(["Name", "Version", "Build Hash"])
        
        # Configure table appearance
        header = self.software_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        
        self.software_table.setAlternatingRowColors(True)
        self.software_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.software_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        
        main_layout.addWidget(self.software_table)
        
        # Status bar with connection indicator
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        # Status display (left side of status bar) - shows job progress
        self.status_display = QLabel("Ready")
        self.status_display.setStyleSheet("""
            padding: 8px 12px; 
            font-weight: bold; 
            font-size: 13px;
            color: #000000;
            background-color: #E8F5E9;
            border-radius: 4px;
            border: 2px solid #4CAF50;
        """)
        self.status_bar.addWidget(self.status_display)

        
        # Connection status indicator (right side of status bar)
        self.connection_indicator = QLabel("⚫ Checking...")
        self.connection_indicator.setStyleSheet("padding: 5px; font-weight: bold;")
        self.status_bar.addPermanentWidget(self.connection_indicator)
        
        self.status_bar.showMessage("Ready to submit job")
        
        # Info label
        info_label = QLabel("ℹThis tool queries the deadline-cloud conda channel for available software packages")
        info_label.setStyleSheet("color: #666; font-style: italic;")
        main_layout.addWidget(info_label)
    
    def apply_modern_style(self):
        """Apply Amazon-inspired styling"""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #EAEDED;
            }
            QGroupBox {
                font-weight: bold;
                border: 2px solid #D5D9D9;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 10px;
                background-color: #FFFFFF;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #232F3E;
            }
            QLineEdit, QComboBox {
                padding: 8px;
                border: 1px solid #D5D9D9;
                border-radius: 4px;
                background-color: #FFFFFF;
                font-size: 12px;
                color: #232F3E;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 2px solid #FF9900;
                outline: none;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #232F3E;
                margin-right: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #FFFFFF;
                color: #232F3E;
                selection-background-color: #FF9900;
                selection-color: #232F3E;
                border: 1px solid #D5D9D9;
            }
            QPushButton {
                background-color: #FF9900;
                color: #232F3E;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #EC7211;
            }
            QPushButton:pressed {
                background-color: #D35400;
            }
            QPushButton:disabled {
                background-color: #D5D9D9;
                color: #879196;
            }
            QTableWidget {
                border: 1px solid #D5D9D9;
                border-radius: 4px;
                background-color: #FFFFFF;
                gridline-color: #EAEDED;
            }
            QTableWidget::item {
                padding: 8px;
                color: #232F3E;
            }
            QTableWidget::item:selected {
                background-color: #FF9900;
                color: #232F3E;
            }
            QHeaderView::section {
                background-color: #232F3E;
                color: #FFFFFF;
                padding: 10px;
                border: none;
                font-weight: bold;
            }
            QProgressBar {
                border: 1px solid #D5D9D9;
                border-radius: 4px;
                text-align: center;
                background-color: #FFFFFF;
                color: #232F3E;
            }
            QProgressBar::chunk {
                background-color: #FF9900;
                border-radius: 3px;
            }
            QStatusBar {
                background-color: #F0F1F1;
                border-top: 1px solid #D5D9D9;
            }
            QLabel {
                color: #232F3E;
            }
        """)
    
    def load_settings(self):
        """Load saved settings (farm, queue IDs, and conda channel)"""
        settings_file = Path.home() / ".deadline_software_viewer.json"
        if settings_file.exists():
            try:
                with open(settings_file, 'r') as f:
                    settings = json.load(f)
                    self.farm_combo.setCurrentText(settings.get("farm_id", ""))
                    self.queue_combo.setCurrentText(settings.get("queue_id", ""))
                    conda_channel = settings.get("conda_channel", "deadline-cloud")
                    self.channel_combo.setCurrentText(conda_channel)
            except Exception:
                pass
    
    def save_settings(self):
        """Save settings (farm, queue IDs, and conda channel)"""
        settings_file = Path.home() / ".deadline_software_viewer.json"
        try:
            settings = {
                "farm_id": self.farm_combo.currentText(),
                "queue_id": self.queue_combo.currentText(),
                "conda_channel": self.channel_combo.currentText()
            }
            with open(settings_file, 'w') as f:
                json.dump(settings, f)
        except Exception:
            pass
    
    def refresh_farms(self):
        """Load available farms from AWS"""
        self.status_bar.showMessage("Loading farms...")
        self.refresh_farms_btn.setEnabled(False)
        
        # Start loader thread
        self.loader_thread = FarmQueueLoader()
        self.loader_thread.farms_loaded.connect(self.handle_farms_loaded)
        self.loader_thread.error_occurred.connect(self.handle_loader_error)
        self.loader_thread.start()
    
    def refresh_queues(self):
        """Load available queues for selected farm"""
        farm_id = self.farm_combo.currentText().strip()
        
        if not farm_id:
            return
        
        # Extract farm ID if full text is displayed
        if " (" in farm_id:
            farm_id = farm_id.split(" (")[1].rstrip(")")
        
        self.status_bar.showMessage(f"Loading queues for {farm_id}...")
        self.refresh_queues_btn.setEnabled(False)
        
        # Start loader thread
        self.loader_thread = FarmQueueLoader(farm_id)
        self.loader_thread.queues_loaded.connect(self.handle_queues_loaded)
        self.loader_thread.error_occurred.connect(self.handle_loader_error)
        self.loader_thread.start()
    
    def on_farm_changed(self, text: str):
        """Handle farm selection change"""
        if text and text.strip():
            # Extract farm ID
            farm_id = text.strip()
            if " (" in farm_id:
                farm_id = farm_id.split(" (")[1].rstrip(")")
            
            if farm_id.startswith("farm-"):
                self.refresh_queues_btn.setEnabled(True)
                # Auto-refresh queues when farm changes
                self.refresh_queues()
            else:
                self.refresh_queues_btn.setEnabled(False)
        else:
            self.refresh_queues_btn.setEnabled(False)
    
    def handle_farms_loaded(self, farms: List[Dict[str, str]]):
        """Handle successfully loaded farms"""
        self.farms_data = farms
        current_text = self.farm_combo.currentText()
        
        # Update combo box
        self.farm_combo.clear()
        for farm in farms:
            display_text = f"{farm['name']} ({farm['id']})"
            self.farm_combo.addItem(display_text, farm['id'])
        
        # Restore previous selection if it exists
        if current_text:
            index = self.farm_combo.findData(current_text)
            if index >= 0:
                self.farm_combo.setCurrentIndex(index)
            else:
                self.farm_combo.setCurrentText(current_text)
        
        self.refresh_farms_btn.setEnabled(True)
        self.status_bar.showMessage(f"Loaded {len(farms)} farms")
    
    def handle_queues_loaded(self, queues: List[Dict[str, str]]):
        """Handle successfully loaded queues"""
        self.queues_data = queues
        current_text = self.queue_combo.currentText()
        
        # Update combo box
        self.queue_combo.clear()
        for queue in queues:
            display_text = f"{queue['name']} ({queue['id']})"
            self.queue_combo.addItem(display_text, queue['id'])
        
        # Restore previous selection if it exists
        if current_text:
            index = self.queue_combo.findData(current_text)
            if index >= 0:
                self.queue_combo.setCurrentIndex(index)
            else:
                self.queue_combo.setCurrentText(current_text)
        
        self.refresh_queues_btn.setEnabled(True)
        self.status_bar.showMessage(f"Loaded {len(queues)} queues")
    
    def handle_loader_error(self, error_message: str):
        """Handle errors loading farms/queues"""
        self.refresh_farms_btn.setEnabled(True)
        self.refresh_queues_btn.setEnabled(True)
        self.status_bar.showMessage("Failed to load data")
        
        # Don't show error dialog on initial load, just log to status
        if "Failed to load" in error_message:
            self.status_bar.showMessage(f"Error: {error_message}")
    
    def check_connection(self, timeout: int = 30):
        """Check connection to AWS Deadline Cloud"""
        # Start connection checker thread with specified timeout
        self.connection_checker = ConnectionChecker(timeout=timeout)
        self.connection_checker.connection_status.connect(self.handle_connection_status)
        self.connection_checker.version_warning.connect(self.handle_version_warning)
        self.connection_checker.start()
    
    def handle_connection_status(self, is_connected: bool, message: str):
        """Handle connection status update"""
        if is_connected:
            self.connection_indicator.setText(f"🟢 {message}")
            self.connection_indicator.setStyleSheet(
                "padding: 5px; font-weight: bold; color: #28a745;"
            )
            self.connection_indicator.setToolTip("Connected to AWS Deadline Cloud")
        else:
            self.connection_indicator.setText(f"🔴 {message}")
            self.connection_indicator.setStyleSheet(
                "padding: 5px; font-weight: bold; color: #dc3545;"
            )
            self.connection_indicator.setToolTip(f"Not connected: {message}")
    
    def handle_version_warning(self, warning_message: str):
        """Handle Deadline CLI version warning"""
        QMessageBox.warning(
            self,
            "Deadline CLI Version Warning",
            warning_message
        )
    
    def submit_job(self):
        """Submit job to AWS Deadline Cloud"""
        farm_text = self.farm_combo.currentText().strip()
        queue_text = self.queue_combo.currentText().strip()
        
        # Extract IDs from display text if needed
        farm_id = farm_text
        if " (" in farm_text:
            farm_id = farm_text.split(" (")[1].rstrip(")")
        
        queue_id = queue_text
        if " (" in queue_text:
            queue_id = queue_text.split(" (")[1].rstrip(")")
        
        # Validate inputs
        if not farm_id or not queue_id:
            QMessageBox.warning(
                self,
                "Missing Information",
                "Please select both Farm and Queue"
            )
            return
        
        if not farm_id.startswith("farm-"):
            QMessageBox.warning(
                self,
                "Invalid Farm ID",
                "Farm ID should start with 'farm-'\n\nProvided: " + farm_id
            )
            return
        
        if not queue_id.startswith("queue-"):
            QMessageBox.warning(
                self,
                "Invalid Queue ID",
                "Queue ID should start with 'queue-'\n\nProvided: " + queue_id
            )
            return
        
        # Save settings
        self.save_settings()
        
        # Clear previous results
        self.software_table.setRowCount(0)
        
        # Show progress bar
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        
        # Disable submit button
        self.submit_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        
        # Get bundle path (list_software directory)
        bundle_path = Path(__file__).parent / "list_software"
        
        if not bundle_path.exists():
            QMessageBox.critical(
                self,
                "Error",
                f"Bundle directory not found: {bundle_path}"
            )
            self.reset_ui()
            return
        
        # Get conda channel
        conda_channel = self.channel_combo.currentText().strip()
        if not conda_channel:
            conda_channel = "deadline-cloud"
        
        # Create and start job thread
        self.job_thread = JobSubmitter(farm_id, queue_id, str(bundle_path), conda_channel)
        self.job_thread.status_update.connect(self.update_status)
        self.job_thread.progress_update.connect(self.update_progress)
        self.job_thread.job_id_received.connect(self.display_job_id)
        self.job_thread.job_completed.connect(self.handle_job_completion)
        self.job_thread.error_occurred.connect(self.handle_error)
        self.job_thread.start()
    
    def update_status(self, message: str):
        """Update status display with detailed message and appropriate styling"""
        # Update main status bar
        self.status_bar.showMessage(message)
        
        # Update status display with color coding
        if "submitting" in message.lower():
            # Blue for submission
            self.status_display.setText(f" {message}")
            self.status_display.setStyleSheet("""
                padding: 5px 10px; 
                font-weight: bold; 
                color: #FFFFFF;
                background-color: #2196F3;
                border-radius: 3px;
                border: 1px solid #1976D2;
            """)
        elif "waiting" in message.lower() or "tasks:" in message.lower():
            # Yellow/Orange for waiting/in-progress
            self.status_display.setText(f" {message}")
            self.status_display.setStyleSheet("""
                padding: 5px 10px; 
                font-weight: bold; 
                color: #FFFFFF;
                background-color: #FF9800;
                border-radius: 3px;
                border: 1px solid #F57C00;
            """)
        elif "completed" in message.lower() or "success" in message.lower():
            # Green for success
            self.status_display.setText(f"✓ {message}")
            self.status_display.setStyleSheet("""
                padding: 5px 10px; 
                font-weight: bold; 
                color: #FFFFFF;
                background-color: #4CAF50;
                border-radius: 3px;
                border: 1px solid #388E3C;
            """)
        elif "submitted" in message.lower() and "job:" in message.lower():
            # Light blue for submitted status
            self.status_display.setText(f"✓ {message}")
            self.status_display.setStyleSheet("""
                padding: 5px 10px; 
                font-weight: bold; 
                color: #FFFFFF;
                background-color: #00BCD4;
                border-radius: 3px;
                border: 1px solid #0097A7;
            """)
        else:
            # Default gray
            self.status_display.setText(message)
            self.status_display.setStyleSheet("""
                padding: 5px 10px; 
                font-weight: bold; 
                color: #232F3E;
                background-color: #E0E0E0;
                border-radius: 3px;
                border: 1px solid #BDBDBD;
            """)
    
    def update_progress(self, value: int):
        """Update progress bar"""
        self.progress_bar.setValue(value)
    
    def display_job_id(self, job_id: str):
        """Display job ID when received"""
        # Show job ID in status display
        self.update_status(f"Job submitted: {job_id}")
        self.status_bar.showMessage(f"Job ID: {job_id} - Monitoring progress...")
    
    def handle_job_completion(self, output: str):
        """Handle successful job completion"""
        # Save output to debug file for troubleshooting
        debug_file = Path.home() / "deadline_job_output_debug.txt"
        try:
            with open(debug_file, 'w', encoding='utf-8') as f:
                f.write(output)
        except:
            pass
        
        # Parse the output
        software_list = SoftwareParser.parse_conda_output(output)
        
        if not software_list:
            QMessageBox.warning(
                self,
                "No Data",
                f"No software packages found in the output.\n\n"
                f"Output saved to: {debug_file}\n"
                f"Output length: {len(output)} characters\n"
                f"First 500 chars:\n{output[:500]}"
            )
            self.reset_ui()
            return
        
        # Populate table
        self.software_table.setRowCount(len(software_list))
        
        for row, software in enumerate(software_list):
            name_item = QTableWidgetItem(software['name'])
            version_item = QTableWidgetItem(software['version'])
            build_item = QTableWidgetItem(software['build'])
            
            self.software_table.setItem(row, 0, name_item)
            self.software_table.setItem(row, 1, version_item)
            self.software_table.setItem(row, 2, build_item)
        
        self.status_bar.showMessage(
            f"Successfully retrieved {len(software_list)} software packages"
        )
        
        self.export_btn.setEnabled(True)
        self.reset_ui()
        
        QMessageBox.information(
            self,
            "Success",
            f"Found {len(software_list)} software packages!"
        )
    
    def handle_error(self, error_message: str):
        """Handle job errors"""
        QMessageBox.critical(
            self,
            "Error",
            f"Job failed:\n\n{error_message}"
        )
        self.reset_ui()
    
    def reset_ui(self):
        """Reset UI after job completion or error"""
        self.progress_bar.setVisible(False)
        self.submit_btn.setEnabled(True)
        # Reset status display to ready state
        self.status_display.setText("Ready")
        self.status_display.setStyleSheet("""
            padding: 5px 10px; 
            font-weight: bold; 
            color: #232F3E;
            background-color: #E8F5E9;
            border-radius: 3px;
            border: 1px solid #4CAF50;
        """)
    
    def export_to_csv(self):
        """Export table data to CSV"""
        if self.software_table.rowCount() == 0:
            return
        
        from PyQt6.QtWidgets import QFileDialog
        
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export to CSV",
            f"deadline_software_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            "CSV Files (*.csv)"
        )
        
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    # Write header
                    f.write("Name,Version,Build Hash\n")
                    
                    # Write data
                    for row in range(self.software_table.rowCount()):
                        name = self.software_table.item(row, 0).text()
                        version = self.software_table.item(row, 1).text()
                        build = self.software_table.item(row, 2).text()
                        f.write(f'"{name}","{version}","{build}"\n')
                
                QMessageBox.information(
                    self,
                    "Export Successful",
                    f"Data exported to:\n{filename}"
                )
                
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "Export Failed",
                    f"Failed to export data:\n{str(e)}"
                )


def main():
    """Main application entry point"""
    app = QApplication(sys.argv)
    app.setApplicationName("AWS Deadline Cloud Software Viewer")
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
