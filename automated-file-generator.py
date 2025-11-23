import sys
import os
import subprocess
import time
import glob
from typing import Tuple, List

from PyQt5.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, 
                             QWidget, QPushButton, QLabel, QLineEdit, QSpinBox, 
                             QTabWidget, QMessageBox, QProgressBar, QTextEdit, QFileDialog, QSplitter)
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import QThread, pyqtSignal, Qt

# --- 1. LEAFLET MAP HTML ---
MAP_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>SUMO Map Selector</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.7.1/dist/leaflet.css" />
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css"/>
    <script src="https://unpkg.com/leaflet@1.7.1/dist/leaflet.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>
    <style>
        body { margin: 0; padding: 0; }
        #map { position: absolute; top: 0; bottom: 0; width: 100%; }
    </style>
</head>
<body>
    <div id="map"></div>
    <script>
        var map = L.map('map').setView([34.0522, -118.2437], 13); // Default: Los Angeles

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; OpenStreetMap contributors'
        }).addTo(map);

        var drawnItems = new L.FeatureGroup();
        map.addLayer(drawnItems);

        var drawControl = new L.Control.Draw({
            draw: {
                polygon: false, marker: false, circle: false, 
                circlemarker: false, polyline: false, 
                rectangle: true
            },
            edit: { featureGroup: drawnItems, remove: true }
        });
        map.addControl(drawControl);

        var lastLayer = null;

        map.on(L.Draw.Event.CREATED, function (e) {
            if (lastLayer) drawnItems.removeLayer(lastLayer);
            lastLayer = e.layer;
            drawnItems.addLayer(lastLayer);
        });

        function getSelectionBounds() {
            if (drawnItems.getLayers().length === 0) return null;
            var bounds = lastLayer.getBounds();
            
            function normalizeLon(lon) {
                return ((lon + 180) % 360 + 360) % 360 - 180;
            }

            return {
                south: bounds.getSouth(),
                north: bounds.getNorth(),
                west: normalizeLon(bounds.getWest()),
                east: normalizeLon(bounds.getEast())
            };
        }
    </script>
</body>
</html>
"""

# --- 2. WORKER THREAD (THE LOGIC) ---
class SumoWorker(QThread):
    log_signal = pyqtSignal(str)    
    finished_signal = pyqtSignal(bool) 
    
    def __init__(self, config):
        super().__init__()
        self.filename = config['filename']
        self.bbox = config['bbox'] 
        self.end_time = config['end_time']
        self.num_trips = config['num_trips']
        self.sumo_home = ""

    def run(self):
        self.log_signal.emit("--- Starting SUMO Generation Process ---")
        
        # 1. Find SUMO
        if not self.find_sumo_and_add_path():
            self.log_signal.emit("❌ Error: SUMO_HOME not found.")
            self.finished_signal.emit(False)
            return

        # 2. Process Data
        try:
            success, launch, cfg = self.create_files()
            if success:
                self.log_signal.emit("\n✨ PROCESS COMPLETE ✨")
                self.log_signal.emit(f"Veins Launch File: {launch}")
                self.log_signal.emit(f"SUMO Config File: {cfg}")
                self.finished_signal.emit(True)
            else:
                self.finished_signal.emit(False)
        except Exception as e:
            import traceback
            self.log_signal.emit(f"❌ Unexpected Error: {str(e)}")
            self.log_signal.emit(traceback.format_exc())
            self.finished_signal.emit(False)

    def log(self, msg):
        self.log_signal.emit(msg)

    def find_sumo_and_add_path(self) -> bool:
        if 'SUMO_HOME' in os.environ:
            self.sumo_home = os.environ['SUMO_HOME']
        else:
            fallback = '/home/soltani/Downloads/Compressed/sumo-1.22.0'
            if os.path.exists(fallback):
                os.environ['SUMO_HOME'] = fallback
                self.sumo_home = fallback
            else:
                return False

        tools = os.path.join(self.sumo_home, 'tools')
        if tools not in sys.path:
            sys.path.append(tools)
        
        self.log(f"✅ Found SUMO_HOME: {self.sumo_home}")
        return True

    def run_command(self, command: List[str], description: str) -> bool:
        self.log(f"\n▶️ Running: {description}...")
        try:
            process = subprocess.Popen(
                command, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                text=True
            )
            
            stdout, stderr = process.communicate()
            
            if stdout: self.log(f"[STDOUT] {stdout[:500]}..." if len(stdout)>500 else f"[STDOUT] {stdout}")
            if stderr: self.log(f"[STDERR] {stderr[:500]}..." if len(stderr)>500 else f"[STDERR] {stderr}")
            
            if process.returncode == 0:
                self.log(f"✅ {description} finished successfully.")
                return True
            else:
                self.log(f"❌ {description} failed with return code {process.returncode}.")
                return False
        except FileNotFoundError:
            self.log(f"❌ Command not found: {command[0]}")
            return False
        except Exception as e:
            self.log(f"❌ Error executing {description}: {e}")
            return False

    def create_files(self):
        filename = self.filename
        osm_file = f"{filename}.osm"
        net_file = f"{filename}.net.xml"
        poly_file = f"{filename}.poly.xml"
        trip_file = f"{filename}.trip.xml"
        route_file = f"{filename}.rou.xml"

        # --- Step 1: Check for Existing OSM OR Download ---
        self.log("--- Step 1: Map Data Setup ---")
        
        if os.path.exists(osm_file):
            self.log(f"✅ Found existing OSM file: '{osm_file}'")
            self.log("ℹ️ Skipping download step and using existing file.")
        else:
            self.log(f"ℹ️ No existing file '{osm_file}' found. Starting download...")
            download_script = os.path.join(self.sumo_home, 'tools', 'osmGet.py')
            
            bbox_str = f"{self.bbox['west']},{self.bbox['south']},{self.bbox['east']},{self.bbox['north']}"
            
            cmd = [sys.executable, download_script, f"--bbox={bbox_str}", "-p", filename, "-d", "."]
            
            if not self.run_command(cmd, "OSM Download"):
                return False, "", ""

            # Find generated file (usually prefix_bbox.osm.xml) and rename it
            generated_files = glob.glob(f"{filename}*_bbox.osm.xml")
            
            if generated_files:
                generated_file = generated_files[0]
                if os.path.exists(osm_file): os.remove(osm_file)
                os.rename(generated_file, osm_file)
                self.log(f"✅ Renamed downloaded file '{generated_file}' to '{osm_file}'")
            elif os.path.exists(f"{filename}.osm.xml"):
                os.rename(f"{filename}.osm.xml", osm_file)
                self.log(f"✅ Renamed '{filename}.osm.xml' to '{osm_file}'")
            else:
                self.log(f"❌ Error: Download finished but expected output file not found.")
                return False, "", ""

        # --- Step 2: Netconvert ---
        self.log("--- Step 2: Converting to Network (Netconvert) ---")
        net_cmd = [
            "netconvert", 
            "--osm-files", osm_file, 
            "-o", net_file,
            "--junctions.join", 
            "--tls.guess-signals", 
            "--tls.discard-simple", 
            "--tls.join"
        ]
        if not self.run_command(net_cmd, "Netconvert"): return False, "", ""

        # --- Step 3: Polyconvert ---
        self.log("--- Step 3: Generating Polygons (Polyconvert) ---")
        typemap = os.path.join(self.sumo_home, 'data', 'typemap', 'osmPolyconvert.typ.xml')
        if os.path.exists(typemap):
            self.run_command(["polyconvert", "--osm-files", osm_file, "--type-file", typemap, "-o", poly_file], "Polyconvert")
        else:
            self.log("⚠️ Typemap not found, skipping Polyconvert.")

        # --- Step 4: Random Trips ---
        self.log("--- Step 4: Generating Random Trips ---")
        random_trips_script = os.path.join(self.sumo_home, 'tools', 'randomTrips.py')
        trip_period = self.end_time / self.num_trips
        
        trips_cmd = [
            sys.executable, random_trips_script,
            "-n", net_file,
            "-o", trip_file,
            "-e", str(self.end_time),
            "-p", str(trip_period),
            "--validate"
        ]
        if not self.run_command(trips_cmd, "Random Trips"): return False, "", ""

        # --- Step 5: DUAROUTER ---
        self.log("--- Step 5: Calculating Routes (DUAROUTER) ---")
        dua_cmd = [
            "duarouter",
            "-n", net_file,
            "-t", trip_file,
            "-o", route_file
        ]
        if not self.run_command(dua_cmd, "DUAROUTER"): return False, "", ""

        # --- Step 6: Configuration Files ---
        self.log("--- Step 6: Writing Configuration Files ---")
        launchd = self.generate_launchd(filename)
        self.generate_omnetpp(filename)
        sumocfg = self.generate_sumocfg(filename, route_file)

        # --- Step 7: Cleanup ---
        self.log("--- Step 7: Cleaning up ---")
        self.cleanup(filename)

        return True, launchd, sumocfg

    def generate_launchd(self, filename):
        content = f"""<?xml version="1.0"?>
<launch>
    <copy file="{filename}.net.xml" />
    <copy file="{filename}.rou.xml" />
    <copy file="{filename}.poly.xml" />
    <copy file="{filename}.sumo.cfg" type="config" />
</launch>"""
        name = f"{filename}.launchd.xml"
        with open(name, 'w') as f: f.write(content)
        self.log(f"Created {name}")
        return name

    def generate_omnetpp(self, filename):
        content = f"""[General]
network = {filename}
sim-time-limit = {self.end_time}s
*.manager.launchConfig = xmldoc("{filename}.launchd.xml")
*.manager.moduleType = "org.car2x.veins.nodes.Car"
*.rsu[*].applType = "TraCIDemoRSU11p"
*.node[*].applType = "TraCIDemo11p"
*.node[*].veinsmobility.x = 0
*.node[*].veinsmobility.y = 0
*.node[*].veinsmobility.z = 1.895
"""
        with open("omnetpp.ini", 'w') as f: f.write(content)
        self.log("Created omnetpp.ini")

    def generate_sumocfg(self, filename, route_file):
        content = f"""<configuration>
    <input>
        <net-file value="{filename}.net.xml"/>
        <route-files value="{route_file}"/>
        <additional-files value="{filename}.poly.xml"/>
    </input>
    <time>
        <begin value="0"/>
        <end value="{self.end_time}"/>
    </time>
</configuration>"""
        name = f"{filename}.sumo.cfg"
        with open(name, 'w') as f: f.write(content)
        self.log(f"Created {name}")
        return name

    def cleanup(self, filename):
        files = ["routes.rou.xml", f"{filename}.rou.alt.xml", f"{filename}.trip.xml"]
        for f in files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                    self.log(f"Removed temp file: {f}")
                except: pass

# --- 3. MAIN APPLICATION ---
class SumoApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Veins/SUMO Scenario Generator")
        self.resize(1200, 850)
        
        # Setup UI
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # Top Controls
        controls_layout = QHBoxLayout()
        
        # Inputs
        self.filename_edit = QLineEdit("VeinsScenario")
        self.time_spin = QSpinBox(); self.time_spin.setRange(100, 100000); self.time_spin.setValue(3600)
        self.trips_spin = QSpinBox(); self.trips_spin.setRange(1, 100000); self.trips_spin.setValue(1000)
        
        controls_layout.addWidget(QLabel("Filename:"))
        controls_layout.addWidget(self.filename_edit)
        controls_layout.addWidget(QLabel("Duration (s):"))
        controls_layout.addWidget(self.time_spin)
        controls_layout.addWidget(QLabel("Vehicles:"))
        controls_layout.addWidget(self.trips_spin)
        
        self.btn_generate = QPushButton("Generate Simulation Files")
        self.btn_generate.setStyleSheet("background-color: #0078D7; color: white; font-weight: bold; padding: 8px;")
        self.btn_generate.clicked.connect(self.start_process)
        controls_layout.addWidget(self.btn_generate)
        
        layout.addLayout(controls_layout)

        # Tabs
        self.tabs = QTabWidget()
        
        # Tab 1: Map
        self.map_view = QWebEngineView()
        self.map_view.setHtml(MAP_HTML)
        self.tabs.addTab(self.map_view, "1. Select Area")
        
        # Tab 2: Logs
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background-color: #1e1e1e; color: #00ff00; font-family: monospace;")
        self.tabs.addTab(self.log_view, "2. Process Log")
        
        layout.addWidget(self.tabs)

    def start_process(self):
        # 1. Get bounds from JS
        self.map_view.page().runJavaScript("getSelectionBounds()", self.handle_bounds)

    def handle_bounds(self, bounds):
        if not bounds:
            # If manual filename provided, try to proceed even without bounds
            # (Useful if file already exists)
            pass 
            # However, current worker expects bounds. 
            # We will just let the user know or pass default/dummy bounds if file exists.
            # For safety, we enforce bounds selection or assume default if not provided (user must draw box).
            QMessageBox.warning(self, "Error", "Please draw a rectangle on the map first.")
            return

        # 2. Prepare Config
        config = {
            'filename': self.filename_edit.text().strip(),
            'bbox': bounds,
            'end_time': self.time_spin.value(),
            'num_trips': self.trips_spin.value()
        }

        # 3. Switch to Log Tab
        self.tabs.setCurrentIndex(1)
        self.log_view.clear()
        self.btn_generate.setEnabled(False)

        # 4. Start Worker
        self.worker = SumoWorker(config)
        self.worker.log_signal.connect(self.update_log)
        self.worker.finished_signal.connect(self.process_finished)
        self.worker.start()

    def update_log(self, text):
        self.log_view.append(text)
        # Scroll to bottom
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.End)
        self.log_view.setTextCursor(cursor)

    def process_finished(self, success):
        self.btn_generate.setEnabled(True)
        if success:
            QMessageBox.information(self, "Success", "All files generated successfully!\nCheck the application folder.")
        else:
            QMessageBox.critical(self, "Failed", "Process failed. Check the logs for details.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SumoApp()
    window.show()
    sys.exit(app.exec_())