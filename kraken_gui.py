import sys
import os
import subprocess
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QLineEdit, QPushButton, QGroupBox, QMessageBox,
                             QTabWidget, QTextEdit)
from PyQt6.QtCore import Qt

LIQUIDCTL_BIN = "/home/theo/liquidctl-env/venv/bin/liquidctl"
HUD_SCRIPT_PATH = "/home/theo/liquidctl-env/kraken_hud.py"

def run_command(args):
    try:
        result = subprocess.run([LIQUIDCTL_BIN] + args, capture_output=True, text=True)
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()
    except Exception as e:
        return False, str(e)

class KrakenController(QWidget):
    def __init__(self):
        super().__init__()
        self.init_ui()
        
    def init_ui(self):
        self.setWindowTitle("Kraken Elite Controller Console")
        self.setGeometry(100, 100, 600, 550)
        self.setStyleSheet("background-color: #f8fafc;")
        
        # Build Container Layout
        window_layout = QVBoxLayout()
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("QTabBar::tab { background-color: #e2e8f0; padding: 8px 16px; font-weight: bold; color: #0f172a; } QTabBar::tab:selected { background-color: #ffffff; border-bottom: 2px solid #0f172a; }")
        
        # Instantiate separate view tab frameworks
        control_tab = QWidget()
        editor_tab = QWidget()
        
        self.setup_control_tab(control_tab)
        self.setup_editor_tab(editor_tab)
        
        self.tabs.addTab(control_tab, "Hardware Controls")
        self.tabs.addTab(editor_tab, "Edit Dashboard Code")
        window_layout.addWidget(self.tabs)
        
        # Global Diagnostic Footer Log Meter Panel
        self.status_label = QLabel("Console Interface Idle.")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-style: italic; color: #64748b; margin-top: 10px; margin-bottom: 10px;")
        window_layout.addWidget(self.status_label)
        
        self.setLayout(window_layout)

    def setup_control_tab(self, tab):
        layout = QVBoxLayout()
        layout.setSpacing(15)
        
        header = QLabel("NZXT Kraken Elite Control Panel")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStyleSheet("font-size: 18px; font-weight: bold; color: #0f172a; margin-top: 10px;")
        layout.addWidget(header)
        
        # --- CONTROL BLOCK A: PUMP CONTROL SETTING LAYOUT ---
        pump_group = QGroupBox(" Dynamic Pump Calibration ")
        pump_group.setStyleSheet("QGroupBox { font-weight: bold; color: #475569; }")
        pump_layout = QHBoxLayout()
        pump_label = QLabel("Target Speed (20-100%):")
        pump_label.setStyleSheet("color: #0f172a;")
        self.pump_entry = QLineEdit("75")
        self.pump_entry.setFixedWidth(60)
        self.pump_entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pump_entry.setStyleSheet("background-color: #ffffff; color: #0f172a; border: 1px solid #cbd5e1; padding: 3px;")
        pump_btn = QPushButton("Apply Speed")
        pump_btn.setStyleSheet("background-color: #0f172a; color: white; padding: 5px 10px; border-radius: 3px;")
        pump_btn.clicked.connect(self.update_pump)
        pump_layout.addWidget(pump_label)
        pump_layout.addWidget(self.pump_entry)
        pump_layout.addWidget(pump_btn)
        pump_group.setLayout(pump_layout)
        layout.addWidget(pump_group)
        
        # --- CONTROL BLOCK B: HARDWARE ROTATION ORIENTATION LAYOUT ---
        screen_group = QGroupBox(" Screen Geometry Settings ")
        screen_group.setStyleSheet("QGroupBox { font-weight: bold; color: #475569; }")
        screen_layout = QVBoxLayout()
        screen_label = QLabel("Flip Canvas Orientation Mapping:")
        screen_label.setStyleSheet("color: #0f172a;")
        screen_layout.addWidget(screen_label)
        btn_layout = QHBoxLayout()
        btn_standard = QPushButton("Standard (0°)")
        btn_standard.setStyleSheet("background-color: #e2e8f0; color: #0f172a; padding: 6px; border-radius: 3px;")
        btn_standard.clicked.connect(lambda: self.set_orientation(0))
        btn_inverted = QPushButton("Inverted (180°)")
        btn_inverted.setStyleSheet("background-color: #e2e8f0; color: #0f172a; padding: 6px; border-radius: 3px;")
        btn_inverted.clicked.connect(lambda: self.set_orientation(180))
        btn_layout.addWidget(btn_standard)
        btn_layout.addWidget(btn_inverted)
        screen_layout.addLayout(btn_layout)
        screen_group.setLayout(screen_layout)
        layout.addWidget(screen_group)
        
        # --- CONTROL BLOCK C: SERVICE AUTOMATION ENGINE CONTROL ---
        service_group = QGroupBox(" Background Script Daemon Options ")
        service_group.setStyleSheet("QGroupBox { font-weight: bold; color: #475569; }")
        service_layout = QVBoxLayout()
        service_label = QLabel("HUD Telemetry Background Service Engine:")
        service_label.setStyleSheet("color: #0f172a;")
        service_layout.addWidget(service_label)
        srv_layout = QHBoxLayout()
        btn_start = QPushButton("Start HUD Loop")
        btn_start.setStyleSheet("background-color: #10b981; color: white; padding: 6px; border-radius: 3px;")
        btn_start.clicked.connect(lambda: self.toggle_service("start"))
        btn_stop = QPushButton("Stop HUD Loop")
        btn_stop.setStyleSheet("background-color: #ef4444; color: white; padding: 6px; border-radius: 3px;")
        btn_stop.clicked.connect(lambda: self.toggle_service("stop"))
        btn_restart = QPushButton("Restart Service")
        btn_restart.setStyleSheet("background-color: #3b82f6; color: white; padding: 6px; border-radius: 3px;")
        btn_restart.clicked.connect(lambda: self.toggle_service("restart"))
        srv_layout.addWidget(btn_start)
        srv_layout.addWidget(btn_stop)
        srv_layout.addWidget(btn_restart)
        service_layout.addLayout(srv_layout)
        service_group.setLayout(service_layout)
        layout.addWidget(service_group)
        
        tab.setLayout(layout)

    def setup_editor_tab(self, tab):
        layout = QVBoxLayout()
        layout.setSpacing(10)
        
        editor_label = QLabel("Edit Live HUD Render Engine Code File (kraken_hud.py):")
        editor_label.setStyleSheet("font-weight: bold; color: #0f172a; margin-top: 5px;")
        layout.addWidget(editor_label)
        
        # Continuous Script Text Display Window Box Component
        self.code_edit = QTextEdit()
        self.code_edit.setFontFamily("monospace")
        self.code_edit.setFontPointSize(10)
        self.code_edit.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4; border: 1px solid #cbd5e1; padding: 5px;")
        layout.addWidget(self.code_edit)
        
        # Load active file text into memory on script invocation
        self.load_hud_code()
        
        # File Action Control Subframe layout
        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save Updates and Restart HUD")
        save_btn.setStyleSheet("background-color: #1484e9; color: white; font-weight: bold; padding: 8px; border-radius: 3px;")
        save_btn.clicked.connect(self.save_hud_code)
        
        reload_btn = QPushButton("Discard & Reload File")
        reload_btn.setStyleSheet("background-color: #64748b; color: white; padding: 8px; border-radius: 3px;")
        reload_btn.clicked.connect(self.load_hud_code)
        
        btn_row.addWidget(reload_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)
        
        tab.setLayout(layout)

    def load_hud_code(self):
        try:
            if os.path.exists(HUD_SCRIPT_PATH):
                with open(HUD_SCRIPT_PATH, 'r') as f:
                    self.code_edit.setPlainText(f.read())
                self.status_label.setText("Successfully loaded kraken_hud.py source.")
                self.status_label.setStyleSheet("font-style: italic; color: #64748b;")
            else:
                self.code_edit.setPlainText("# Error: kraken_hud.py script missing from folder tree.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load script into display:\n{str(e)}")

    def save_hud_code(self):
        try:
            # Overwrite source file with edits made in GUI text editor block
            with open(HUD_SCRIPT_PATH, 'w') as f:
                f.write(self.code_edit.toPlainText())
            
            # Flush parameters and bounce background engine service to apply live edits instantly
            subprocess.run(["systemctl", "--user", "restart", "liquidctl.service"], check=True)
            self.status_label.setText("Success: Updates saved. Script engine restarted!")
            self.status_label.setStyleSheet("font-style: italic; color: #10b981;")
            QMessageBox.information(self, "Success", "Code updates written successfully!\nBackground loop restarted.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to write file updates or bounce engine:\n{str(e)}")

    def update_pump(self):
        speed = self.pump_entry.text()
        if not speed.isdigit() or not (20 <= int(speed) <= 100):
            QMessageBox.critical(self, "Error", "Pump speed must be a number between 20 and 100.")
            return
        args = ["--match", "Kraken", "set", "pump", "speed", speed]
        success, msg = run_command(args)
        if success:
            self.status_label.setText(f"Success: Pump speed set to {speed}%")
            self.status_label.setStyleSheet("font-style: italic; color: #10b981;")
        else:
            QMessageBox.critical(self, "Error", f"Failed to set pump speed:\n{msg}")

    def set_orientation(self, degrees):
