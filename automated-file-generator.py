import sys
import os
import subprocess
import time
import glob 
import xml.etree.ElementTree as ET
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from collections import Counter
from typing import Tuple, List, Optional

from PyQt5.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, 
                             QWidget, QPushButton, QLabel, QLineEdit, QSpinBox, 
                             QTabWidget, QMessageBox, QTextEdit, QFileDialog, QSplitter)
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import QThread, pyqtSignal, Qt

# --- CONSTANTS ---
# Minimum required size for a valid OSM file (in bytes). 
# Set to 10 KB (10240 bytes) as a sanity check.
MIN_OSM_FILE_SIZE = 1024 * 10 

# --- New Plot Widget Class ---
class PlotViewer(QWidget):
    """A QWidget that contains a Matplotlib figure."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.canvas = None
        
    def set_plot(self, figure: Figure, filename: str):
        """Removes old canvas and adds a new one based on the given Figure."""
        if self.canvas:
            self.layout.removeWidget(self.canvas)
            self.canvas.deleteLater()
            
        self.canvas = FigureCanvas(figure)
        self.layout.addWidget(self.canvas)
        self.setWindowTitle(f"Edge Usage: {filename}")
        self.update()

# --- Refactored Plotting Function (Returns Figure instead of saving) ---
def create_most_used_edges_plot(top_edges: List[Tuple[str, int]], filename: str) -> Optional[Figure]:
    if not top_edges:
        return None

    edges = [item[0] for item in top_edges]
    counts = [item[1] for item in top_edges]
    
    fig = Figure(figsize=(10, 6))
    ax = fig.add_subplot(111)
    
    y_pos = range(len(counts))
    ax.barh(y_pos, counts, align='center', color='#0078D7')
    
    ax.set_yticks(y_pos)
    ax.set_yticklabels(edges)
    
    for i, count in enumerate(counts):
        ax.text(count, i, f' {count:,}', va='center')

    ax.invert_yaxis()
    ax.set_xlabel('Number of Vehicles Traversed')
    ax.set_title(f'Top {len(top_edges)} Most Used Edges in Route File')
    
    fig.tight_layout()
    return fig


# --- 1. LEAFLET MAP HTML (Unchanged) ---
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
# --- 2. WORKER THREAD ---
class SumoWorker(QThread):
    log_signal = pyqtSignal(str)    
    finished_signal = pyqtSignal(bool, object) 
    
    def __init__(self, config):
        super().__init__()
        self.filename = config['filename']
        self.bbox = config['bbox'] 
        self.end_time = config['end_time']
        self.num_trips = config['num_trips']
        self.sumo_home = ""

    def run(self):
        self.log_signal.emit("--- Starting SUMO Generation Process ---")
        
        if not self.find_sumo_and_add_path():
            self.log_signal.emit("❌ Error: SUMO_HOME not found.")
            self.finished_signal.emit(False, None)
            return

        plot_figure = None
        try:
            success, launch, cfg, plot_figure = self.create_files() 
            
            if success:
                self.log_signal.emit("\n✨ PROCESS COMPLETE ✨")
                self.log_signal.emit(f"Veins Launch File: {launch}")
                self.log_signal.emit(f"SUMO Config File: {cfg}")
                self.finished_signal.emit(True, plot_figure) 
            else:
                self.finished_signal.emit(False, None)
        except Exception as e:
            import traceback
            self.log_signal.emit(f"❌ Unexpected Error: {str(e)}")
            self.log_signal.emit(traceback.format_exc())
            self.finished_signal.emit(False, None)

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
                self.log("❌ Error: SUMO_HOME environment variable is not set and fallback path not found.")
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
            
    def most_used_route_finder(self, route_file: str, top_n: int = 10) -> List[Tuple[str, int]]:
        if not os.path.exists(route_file):
            self.log(f"⚠️ Cannot analyze routes: File not found at {route_file}")
            return []

        self.log(f"\n🔬 Starting analysis of most used edges in '{route_file}'...")
        edge_counts = Counter()
        total_edges = 0

        try:
            tree = ET.parse(route_file)
            root = tree.getroot()
            
            for vehicle in root.iter('vehicle'):
                route_element = vehicle.find('route')
                
                if route_element is not None:
                    edges_list_str = route_element.get("edges")
                    
                    if edges_list_str:
                        edge_ids = edges_list_str.split()
                        edge_counts.update(edge_ids)
                        total_edges += len(edge_ids)

        except Exception as e:
            self.log(f"❌ Error during route analysis: {e}")
            return []

        most_common_edges = edge_counts.most_common(top_n)

        # Log the results to the GUI
        self.log("\n--- Edge Usage Report ---")
        self.log(f"Total Unique Edges Used: **{len(edge_counts):,}**")
        self.log(f"Total Edges Traversed: **{total_edges:,}**")
        self.log(f"Top {top_n} Most Used Edges:")
        
        for edge_id, count in most_common_edges:
            percentage = (count / total_edges) * 100 if total_edges > 0 else 0
            self.log(f"* **{edge_id}**: {count:,} times ({percentage:.2f}%)")
            
        return most_common_edges
        
    def create_files(self) -> Tuple[bool, str, str, Optional[Figure]]:
        filename = self.filename
        osm_file = f"{filename}.osm"
        net_file = f"{filename}.net.xml"
        poly_file = f"{filename}.poly.xml"
        trip_file = f"{filename}.trip.xml"
        route_file = f"{filename}.rou.xml"

        # --- Step 1: Map Data Setup (Updated Check) ---
        self.log("--- Step 1: Map Data Setup ---")
        
        osm_file_exists = os.path.exists(osm_file)
        should_download = True # Assume download is needed unless checks pass

        if osm_file_exists:
            file_size = os.path.getsize(osm_file)
            
            if file_size > MIN_OSM_FILE_SIZE:
                self.log(f"✅ Found existing OSM file: '{osm_file}' (Size: {file_size // 1024} KB)")
                self.log("ℹ️ Skipping download step and using existing file.")
                should_download = False # File is valid, skip download
            else:
                self.log(f"⚠️ Found file '{osm_file}', but size ({file_size} bytes) is too small (<{MIN_OSM_FILE_SIZE} bytes).")
                self.log("ℹ️ Re-downloading map data to ensure completeness.")
                # should_download remains True

        if should_download:
            self.log(f"ℹ️ Starting download...")

            # If bounds are dummy (i.e., we are relying on an existing file that failed size check), we must have real bounds to download!
            # If bbox is {0,0,0,0}, the user must have pressed generate without selecting a map area. 
            # In this case, we rely on the Handle_Bounds function to stop the process before this worker starts. 
            # If the worker starts, we assume the bounds are either real or the user intended to use an existing, valid file (which we already ruled out if we are here).
            # Therefore, we use the bounds provided, which should be valid if download is needed.
            
            download_script = os.path.join(self.sumo_home, 'tools', 'osmGet.py')
            
            # The bbox must be valid here because the SumoApp checked this condition.
            bbox_str = f"{self.bbox['west']},{self.bbox['south']},{self.bbox['east']},{self.bbox['north']}"
            
            cmd = [sys.executable, download_script, f"--bbox={bbox_str}", "-p", filename, "-d", "."]
            
            if not self.run_command(cmd, "OSM Download"): return False, "", "", None

            # Find generated file and rename it
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
                return False, "", "", None

        # --- Step 2: Netconvert (Unchanged) ---
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
        if not self.run_command(net_cmd, "Netconvert"): return False, "", "", None

        # --- Step 3: Polyconvert (Unchanged) ---
        self.log("--- Step 3: Generating Polygons (Polyconvert) ---")
        typemap = os.path.join(self.sumo_home, 'data', 'typemap', 'osmPolyconvert.typ.xml')
        if os.path.exists(typemap):
            self.run_command(["polyconvert", "--osm-files", osm_file, "--type-file", typemap, "-o", poly_file], "Polyconvert")
        else:
            self.log("⚠️ Typemap not found, skipping Polyconvert.")

        # --- Step 4: Random Trips (Unchanged) ---
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
        if not self.run_command(trips_cmd, "Random Trips"): return False, "", "", None

        # --- Step 5: DUAROUTER (Unchanged) ---
        self.log("--- Step 5: Calculating Routes (DUAROUTER) ---")
        dua_cmd = [
            "duarouter",
            "-n", net_file,
            "-t", trip_file,
            "-o", route_file
        ]
        if not self.run_command(dua_cmd, "DUAROUTER"): return False, "", "", None

        # --- Step 6: Route Analysis and Plotting (Unchanged) ---
        self.log("--- Step 6: Analyzing Route Usage and Plotting ---")
        
        top_edges_list = self.most_used_route_finder(route_file, top_n=10)
        plot_figure = None
        
        if top_edges_list:
            plot_figure = create_most_used_edges_plot(top_edges_list, filename)
            self.log("📊 Graphical Report Figure created successfully.")
        else:
            self.log("⚠️ Plotting skipped: No edges found in the route file.")


        # --- Step 7: Configuration Files (Unchanged) ---
        self.log("--- Step 7: Writing Configuration Files ---")
        launchd = self.generate_launchd(filename)
        sumocfg = self.generate_sumocfg(filename, route_file)

        # --- Step 8: Cleanup (Unchanged) ---
        self.log("--- Step 8: Cleaning up ---")
        self.cleanup(filename)

        return True, launchd, sumocfg, plot_figure

    # --- Configuration Generating Functions (Unchanged) ---
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

# --- 3. MAIN APPLICATION (Modified Handle Bounds) ---
class SumoApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Veins/SUMO Scenario Generator")
        self.resize(1200, 850)
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # Top Controls (Unchanged)
        controls_layout = QHBoxLayout()
        
        self.filename_edit = QLineEdit("VeinsScenario")
        self.time_spin = QSpinBox(); self.time_spin.setRange(100, 100000); self.time_spin.setValue(3600)
        self.trips_spin = QSpinBox(); self.trips_spin.setRange(1, 100000); self.trips_spin.setValue(1000000)
        
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
        
        # Tab 1: Map (Unchanged)
        self.map_view = QWebEngineView()
        self.map_view.setHtml(MAP_HTML)
        self.tabs.addTab(self.map_view, "1. Select Area")
        
        # Tab 2: Logs (Unchanged)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background-color: #1e1e1e; color: #00ff00; font-family: monospace;")
        self.tabs.addTab(self.log_view, "2. Process Log")
        
        # Tab 3: Plot (NEW)
        self.plot_viewer = PlotViewer()
        self.tabs.addTab(self.plot_viewer, "3. Route Analysis Plot")
        
        layout.addWidget(self.tabs)

    def start_process(self):
        # 1. Get bounds from JS
        self.map_view.page().runJavaScript("getSelectionBounds()", self.handle_bounds)

    def handle_bounds(self, bounds):
        """
        Handles the bounds data and initiates the worker thread.
        Checks for file existence/size if bounds are missing.
        """
        filename = self.filename_edit.text().strip()
        osm_file = f"{filename}.osm"
        is_valid_file = False
        
        # Check if a valid file exists
        if os.path.exists(osm_file):
            if os.path.getsize(osm_file) > MIN_OSM_FILE_SIZE:
                is_valid_file = True

        # --- CORE NEW LOGIC ---
        if not bounds:
            if is_valid_file:
                # Case 1: No selection, but a valid file exists -> Proceed using dummy bounds
                QMessageBox.information(self, "Using Existing File", f"No area selected. Proceeding with analysis and generation using existing file: {osm_file}")
                # Set dummy bounds, the worker will check 'should_download' internally
                bounds = {'west': 0, 'south': 0, 'east': 0, 'north': 0} 
            else:
                # Case 2: No selection and no valid file exists -> Stop and prompt user
                QMessageBox.warning(self, "Action Required", "Please draw a rectangle on the map to define the simulation area, or ensure a valid OSM file exists.")
                self.tabs.setCurrentIndex(0) # Return to the map tab
                return
        # --- END CORE NEW LOGIC ---
        
        # If bounds exist, or if we are in Case 1, we proceed here.
        config = {
            'filename': filename,
            'bbox': bounds,
            'end_time': self.time_spin.value(),
            'num_trips': self.trips_spin.value()
        }

        # Switch to Log Tab
        self.tabs.setCurrentIndex(1)
        self.log_view.clear()
        self.btn_generate.setEnabled(False)

        # Start Worker
        self.worker = SumoWorker(config)
        self.worker.log_signal.connect(self.update_log)
        self.worker.finished_signal.connect(self.process_finished) 
        self.worker.start()

    def update_log(self, text):
        self.log_view.append(text)
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.End)
        self.log_view.setTextCursor(cursor)
        
    def process_finished(self, success: bool, plot_figure: Optional[Figure]): 
        self.btn_generate.setEnabled(True)
        filename = self.filename_edit.text().strip()
        
        if success:
            QMessageBox.information(self, "Success", "All files generated successfully! The route analysis plot is available in the 'Route Analysis Plot' tab.")
            
            if plot_figure:
                self.plot_viewer.set_plot(plot_figure, filename)
                self.tabs.setCurrentIndex(2)
        else:
            QMessageBox.critical(self, "Failed", "Process failed. Check the logs for details.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SumoApp()
    window.show()
    sys.exit(app.exec_())