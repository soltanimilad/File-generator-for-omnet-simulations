import sys
import os
import subprocess
import shutil
import platform
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

MIN_OSM_FILE_SIZE = 1024 * 10 

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
        self.attack_start = config.get('attack_start', 100)
        self.attack_duration = config.get('attack_duration', 500)

    def run(self):
        self.log_signal.emit("--- Starting SUMO Generation Process ---")
        
        if not self.find_sumo_and_add_path():
            self.log_signal.emit("❌ Error: SUMO_HOME not found.")
            self.finished_signal.emit(False, None)
            return

        plot_figure = None
        try:
            success, launch_clean , launch_blocked, cfg_clean , plot_figure , cfg_blocked = self.create_files() 
            
            if success:
                self.log_signal.emit("\n✨ PROCESS COMPLETE ✨")
                self.log_signal.emit(f"Veins Clean Launch File: {launch_clean}")
                self.log_signal.emit(f"Veins Blocked Launch File: {launch_blocked}")
                self.log_signal.emit(f"Clean SUMO Config File: {cfg_clean}")
                self.log_signal.emit(f"Blocked SUMO Config File: {cfg_blocked}")
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
        sumo_home = os.environ.get('SUMO_HOME')
        if not sumo_home:
            sumo_bin = shutil.which("sumo")
            if sumo_bin:
                sumo_home = os.path.dirname(os.path.dirname(sumo_bin))
        if not sumo_home:
            defaults = {
                "Windows": [r"C:\Program Files (x86)\Eclipse\Sumo", r"C:\Sumo"],
                "Linux": ["/usr/share/sumo", "/usr/local/share/sumo"],
                "Darwin": ["/opt/homebrew/opt/sumo/share/sumo", "/usr/local/opt/sumo/share/sumo"]
            }
            system = platform.system()
            for path in defaults.get(system, []):
                if os.path.exists(path):
                    sumo_home = path
                    break
        if sumo_home and os.path.exists(os.path.join(sumo_home, 'tools')):
            self.sumo_home = sumo_home
            os.environ['SUMO_HOME'] = sumo_home

            tools = os.path.join(self.sumo_home, 'tools')
            if tools not in sys.path:
                sys.path.append(tools)

            self.log(f"✅ Found SUMO_HOME: {self.sumo_home}")
            return True

        self.log("❌ Error: Could not automatically locate SUMO installation.")
        return False

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
    def cleanup(self, filename):
        files = ["routes.rou.xml", f"{filename}.rou.alt.xml", f"{filename}.trip.xml"]
        for f in files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                    self.log(f"Removed temp file: {f}")
                except: pass 
    def generate_Blocked_omnetini_File(self, filename , pg_x, pg_y, rsu_x, rsu_y, end_time ,target_edge: str , type : str):
        content = f"""[General]
cmdenv-express-mode = true
cmdenv-autoflush = true
cmdenv-status-frequency = 1s
**.cmdenv-log-level = info

image-path = ../../images

network = RSUExampleScenario

##########################################################
#            Simulation parameters                       #
##########################################################
debug-on-errors = true
print-undisposed = true

sim-time-limit = {end_time}s

**.scalar-recording = true
**.vector-recording = true

*.playgroundSizeX = {pg_x}m
*.playgroundSizeY = {pg_y}m
*.playgroundSizeZ = 50m


##########################################################
# Annotation parameters                                  #
##########################################################
*.annotations.draw = true

##########################################################
# Obstacle parameters                                    #
##########################################################
*.obstacles.obstacles = xmldoc("config.xml", "//AnalogueModel[@type='SimpleObstacleShadowing']/obstacles")

##########################################################
#            TraCIScenarioManager parameters             #
##########################################################
*.manager.updateInterval = 1s
*.manager.host = "localhost"
*.manager.port = 9999
*.manager.autoShutdown = true
*.manager.launchConfig = xmldoc("{filename}_{type}.launchd.xml")
*.manager.trafficLightModuleType = "org.car2x.veins.nodes.TrafficLight"

*.tls[*].mobility.x = 0
*.tls[*].mobility.y = 0
*.tls[*].mobility.z = 3

*.tls[*].applType = "org.car2x.veins.modules.application.traci.TraCIDemoTrafficLightApp"
*.tls[*].logicType ="org.car2x.veins.modules.world.traci.trafficLight.logics.TraCITrafficLightSimpleLogic"


##########################################################
#                       RSU SETTINGS                     #
#                                                        #
#                                                        #
##########################################################
*.rsu[0].mobility.x = {rsu_x}
*.rsu[0].mobility.y = {rsu_y}
*.rsu[0].mobility.z = 3

*.rsu[*].applType = "TraCIDemoRSU11p"
*.rsu[*].appl.attackLaneId     = "{target_edge}_0"
*.rsu[*].appl.attackStartTime  = {self.attack_start}s      
*.rsu[*].appl.attackDuration   = {self.attack_duration}s      
*.rsu[*].appl.headerLength = 80 bit
*.rsu[*].appl.sendBeacons = false
*.rsu[*].appl.dataOnSch = false
*.rsu[*].appl.beaconInterval = 1s
*.rsu[*].appl.beaconUserPriority = 7
*.rsu[*].appl.dataUserPriority = 5
*.rsu[*].nic.phy80211p.antennaOffsetZ = 0 m

##########################################################
#            11p specific parameters                     #
#                                                        #
#                    NIC-Settings                        #
##########################################################
*.connectionManager.sendDirect = true
*.connectionManager.maxInterfDist = 2600m
*.connectionManager.drawMaxIntfDist = false

*.**.nic.mac1609_4.useServiceChannel = false

*.**.nic.mac1609_4.txPower = 20mW
*.**.nic.mac1609_4.bitrate = 6Mbps
*.**.nic.phy80211p.minPowerLevel = -110dBm

*.**.nic.phy80211p.useNoiseFloor = true
*.**.nic.phy80211p.noiseFloor = -98dBm

*.**.nic.phy80211p.decider = xmldoc("config.xml")
*.**.nic.phy80211p.analogueModels = xmldoc("config.xml")
*.**.nic.phy80211p.usePropagationDelay = true

*.**.nic.phy80211p.antenna = xmldoc("antenna.xml", "/root/Antenna[@id='monopole']")
*.node[*].nic.phy80211p.antennaOffsetY = 0 m
*.node[*].nic.phy80211p.antennaOffsetZ = 1.895 m

##########################################################
#                      App Layer                         #
##########################################################
*.node[*].applType = "TraCIDemo11p"
*.node[*].appl.headerLength = 80 bit
*.node[*].appl.sendBeacons = false
*.node[*].appl.dataOnSch = false
*.node[*].appl.beaconInterval = 1s

##########################################################
#                      Mobility                          #
##########################################################
*.node[*].veinsmobility.x = 0
*.node[*].veinsmobility.y = 0
*.node[*].veinsmobility.z = 0
*.node[*].veinsmobility.setHostSpeed = false
*.node[*0].veinsmobility.accidentCount = 1
*.node[*0].veinsmobility.accidentStart = 73s
*.node[*0].veinsmobility.accidentDuration = 50s

[Config Default]

[Config WithBeaconing]
*.rsu[*].appl.sendBeacons = true
*.node[*].appl.sendBeacons = true

[Config WithChannelSwitching]
*.**.nic.mac1609_4.useServiceChannel = true
*.node[*].appl.dataOnSch = true
*.rsu[*].appl.dataOnSch = true

"""
        name = f"{filename}_Blocked.omnetpp.ini"
        with open(name, 'w') as f: f.write(content)
        self.log(f"Created {name}")
        return name         
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
        self.log("--- Step 1: Map Data Setup ---")
        
        osm_file_exists = os.path.exists(osm_file)
        should_download = True 

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
            
            download_script = os.path.join(self.sumo_home, 'tools', 'osmGet.py')
            bbox_str = f"{self.bbox['west']},{self.bbox['south']},{self.bbox['east']},{self.bbox['north']}"
            
            cmd = [sys.executable, download_script, f"--bbox={bbox_str}", "-p", filename, "-d", "."]
            
            if not self.run_command(cmd, "OSM Download"): return False, "", "", None
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
        self.log("--- Step 2: Converting to Network (Netconvert) ---")
        net_cmd = [
    "netconvert", 
    "--osm-files", osm_file, 
    "-o", net_file,
    "--junctions.join",              # Joins nearby nodes into single intersections
    "--no-internal-links", "false",  # Crucial: enables physical turns for blocking
    "--keep-edges.components", "1",  # Deletes disconnected "island" roads
    "--tls.join",                    # Groups traffic lights at large junctions
    "--geometry.remove",             # Simplifies road shapes for better performance
    "--roundabouts.guess"            # Identifies roundabouts for better routing
]
        if not self.run_command(net_cmd, "Netconvert"): return False, "", "", None
        self.log("--- Step 3: Generating Polygons (Polyconvert) ---")
        typemap = os.path.join(self.sumo_home, 'data', 'typemap', 'osmPolyconvert.typ.xml')
        if os.path.exists(typemap):
            self.run_command(["polyconvert", "--osm-files", osm_file, "--type-file", typemap, "-o", poly_file], "Polyconvert")
        else:
            self.log("⚠️ Typemap not found, skipping Polyconvert.")
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
        self.log("--- Step 5: Calculating Routes (DUAROUTER) ---")
        dua_cmd = [
            "duarouter",
            "-n", net_file,
            "-t", trip_file,
            "-o", route_file
        ]
        if not self.run_command(dua_cmd, "DUAROUTER"): return False, "", "", None
        self.log("--- Step 6: Analyzing Route Usage and Plotting ---")
        
        top_edges_list = self.most_used_route_finder(route_file, top_n=10)
        plot_figure = None
        
        if top_edges_list:
            plot_figure = create_most_used_edges_plot(top_edges_list, filename)
            self.log("📊 Graphical Report Figure created successfully.")
        else:
            self.log("⚠️ Plotting skipped: No edges found in the route file.")
        self.log("--- Step 7: Writing Configuration Files ---")
        launchd_clean = self.generate_launchd(filename , "Clean")
        launched_blocked = self.generate_launchd(filename , "Blocked")
        sumocfg_clean = self.generate_sumocfg(filename, route_file , "Clean")
        sumocfg_blocked = self.generate_sumocfg(filename, route_file , "Blocked")
        self.log("ℹ️ Extracting coordinates from net.xml...")
        try:
            tree = ET.parse(net_file)
            location_element = tree.find('.//location')
            if location_element is not None:
                conv_boundary_str = location_element.get('convBoundary')
                min_x, min_y, max_x, max_y = map(float, conv_boundary_str.split(','))
            else:
                raise ValueError("Location tag not found in net.xml.")
        except Exception as e:
            self.log(f"❌ Error extracting coordinates: {e}")
            return False, "", "", None
        PERCENTAGE_INCREASE = 0.50 
        original_width = max_x - min_x
        original_height = max_y - min_y
        BUFFER_SIZE_X = original_width * PERCENTAGE_INCREASE
        BUFFER_SIZE_Y = original_height * PERCENTAGE_INCREASE
        OFFSET_X = BUFFER_SIZE_X / 2.0  
        OFFSET_Y = BUFFER_SIZE_Y / 2.0  
        play_ground_x_final = original_width + BUFFER_SIZE_X
        play_ground_y_final = original_height + BUFFER_SIZE_Y
        rsu_x_shifted = (original_width / 2.0) + OFFSET_X
        rsu_y_shifted = (original_height / 2.0) + OFFSET_Y
        clean_ini = self.generate_omnetpp_ini(filename , play_ground_x_final , play_ground_y_final , rsu_x_shifted , rsu_y_shifted , self.end_time , "Clean")
        target_edge = top_edges_list[0][0]
        blocked_ini = self.generate_Blocked_omnetini_File(filename , play_ground_x_final , play_ground_y_final , rsu_x_shifted , rsu_y_shifted , self.end_time , target_edge , "Blocked")
        self.log("--- Step 8: Cleaning up ---")
        self.cleanup(filename)
        return True, launchd_clean, launched_blocked , sumocfg_clean, plot_figure , sumocfg_blocked 
    def generate_launchd(self, filename , type : str):
        content = f"""<?xml version="1.0"?>
<launch>
    <copy file="{filename}.net.xml" />
    <copy file="{filename}.rou.xml" />
    <copy file="{filename}.poly.xml" />
    <copy file="{filename}_{type}.sumo.cfg" type="config" />
</launch>"""
        name = f"{filename}_{type}.launchd.xml"
        with open(name, 'w') as f: f.write(content)
        self.log(f"Created {name}")
        return name

    def generate_sumocfg(self, filename, route_file , type : str):
        log_dir = f"{filename}-logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
            self.log(f"✅ Created output directory: {log_dir}/")
        os.makedirs(os.getcwd() , exist_ok=True)
        summary_output = os.path.join(os.getcwd(), f"{log_dir}/{filename}_{type}_summary_output.xml")
        tripinfo_output = os.path.join(os.getcwd(), f"{log_dir}/{filename}_{type}_tripinfo_output.xml")
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
    <processing>
        <ignore-route-errors value="true"/>
        <waiting-time-memory value="{self.end_time}"/>
        <time-to-teleport value="-1"/> 
        </processing>
    <output>
        <summary-output value="{summary_output}"/>
        <tripinfo-output value="{tripinfo_output}"/>
    </output>
</configuration>"""
        name = f"{filename}_{type}.sumo.cfg"
        with open(name, 'w') as f: f.write(content)
        self.log(f"Created {name}")
        return name
    def generate_omnetpp_ini(self, filename , pg_x, pg_y, rsu_x, rsu_y, end_time , type : str):
        content = f"""[General]
cmdenv-express-mode = true
cmdenv-autoflush = true
cmdenv-status-frequency = 1s
**.cmdenv-log-level = info

image-path = ../../images

network = RSUExampleScenario

##########################################################
#            Simulation parameters                       #
##########################################################
debug-on-errors = true
print-undisposed = true

sim-time-limit = {end_time}s

**.scalar-recording = true
**.vector-recording = true

*.playgroundSizeX = {pg_x}m
*.playgroundSizeY = {pg_y}m
*.playgroundSizeZ = 50m


##########################################################
# Annotation parameters                                  #
##########################################################
*.annotations.draw = true

##########################################################
# Obstacle parameters                                    #
##########################################################
*.obstacles.obstacles = xmldoc("config.xml", "//AnalogueModel[@type='SimpleObstacleShadowing']/obstacles")

##########################################################
#            TraCIScenarioManager parameters             #
##########################################################
*.manager.updateInterval = 1s
*.manager.host = "localhost"
*.manager.port = 9999
*.manager.autoShutdown = true
*.manager.launchConfig = xmldoc("{filename}_{type}.launchd.xml")
*.manager.trafficLightModuleType = "org.car2x.veins.nodes.TrafficLight"

*.tls[*].mobility.x = 0
*.tls[*].mobility.y = 0
*.tls[*].mobility.z = 3

*.tls[*].applType = "org.car2x.veins.modules.application.traci.TraCIDemoTrafficLightApp"
*.tls[*].logicType ="org.car2x.veins.modules.world.traci.trafficLight.logics.TraCITrafficLightSimpleLogic"


##########################################################
#                       RSU SETTINGS                     #
#                                                        #
#                                                        #
##########################################################
*.rsu[0].mobility.x = {rsu_x}
*.rsu[0].mobility.y = {rsu_y}
*.rsu[0].mobility.z = 3

*.rsu[*].applType = "TraCIDemoRSU11p"
*.rsu[*].appl.headerLength = 80 bit
*.rsu[*].appl.sendBeacons = false
*.rsu[*].appl.dataOnSch = false
*.rsu[*].appl.beaconInterval = 1s
*.rsu[*].appl.beaconUserPriority = 7
*.rsu[*].appl.dataUserPriority = 5
*.rsu[*].nic.phy80211p.antennaOffsetZ = 0 m

##########################################################
#            11p specific parameters                     #
#                                                        #
#                    NIC-Settings                        #
##########################################################
*.connectionManager.sendDirect = true
*.connectionManager.maxInterfDist = 2600m
*.connectionManager.drawMaxIntfDist = false

*.**.nic.mac1609_4.useServiceChannel = false

*.**.nic.mac1609_4.txPower = 20mW
*.**.nic.mac1609_4.bitrate = 6Mbps
*.**.nic.phy80211p.minPowerLevel = -110dBm

*.**.nic.phy80211p.useNoiseFloor = true
*.**.nic.phy80211p.noiseFloor = -98dBm

*.**.nic.phy80211p.decider = xmldoc("config.xml")
*.**.nic.phy80211p.analogueModels = xmldoc("config.xml")
*.**.nic.phy80211p.usePropagationDelay = true

*.**.nic.phy80211p.antenna = xmldoc("antenna.xml", "/root/Antenna[@id='monopole']")
*.node[*].nic.phy80211p.antennaOffsetY = 0 m
*.node[*].nic.phy80211p.antennaOffsetZ = 1.895 m

##########################################################
#                      App Layer                         #
##########################################################
*.node[*].applType = "TraCIDemo11p"
*.node[*].appl.headerLength = 80 bit
*.node[*].appl.sendBeacons = false
*.node[*].appl.dataOnSch = false
*.node[*].appl.beaconInterval = 1s

##########################################################
#                      Mobility                          #
##########################################################
*.node[*].veinsmobility.x = 0
*.node[*].veinsmobility.y = 0
*.node[*].veinsmobility.z = 0
*.node[*].veinsmobility.setHostSpeed = false
*.node[*0].veinsmobility.accidentCount = 1
*.node[*0].veinsmobility.accidentStart = 73s
*.node[*0].veinsmobility.accidentDuration = 50s

[Config Default]

[Config WithBeaconing]
*.rsu[*].appl.sendBeacons = true
*.node[*].appl.sendBeacons = true

[Config WithChannelSwitching]
*.**.nic.mac1609_4.useServiceChannel = true
*.node[*].appl.dataOnSch = true
*.rsu[*].appl.dataOnSch = true

"""
        name = f"{filename}_Clean.omnetpp.ini"
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
class SumoApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Veins/SUMO Scenario Generator")
        self.resize(1200, 850)
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        controls_layout = QHBoxLayout() 
        self.filename_edit = QLineEdit("VeinsScenario")
        self.time_spin = QSpinBox(); self.time_spin.setRange(100, 100000); self.time_spin.setValue(3600)
        self.trips_spin = QSpinBox(); self.trips_spin.setRange(1, 100000); self.trips_spin.setValue(10000)
        self.attack_start_spin = QSpinBox()
        self.attack_start_spin.setRange(0, 100000)
        self.attack_start_spin.setValue(100)
        self.attack_start_spin.setSuffix("s")
        self.attack_duration_spin = QSpinBox()
        self.attack_duration_spin.setRange(0, 100000)
        self.attack_duration_spin.setValue(500)
        self.attack_duration_spin.setSuffix("s")
        controls_layout.addWidget(QLabel("Attack Start:"))
        controls_layout.addWidget(self.attack_start_spin)
        controls_layout.addWidget(QLabel("Duration:"))
        controls_layout.addWidget(self.attack_duration_spin)
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
        self.tabs = QTabWidget()
        self.map_view = QWebEngineView()
        self.map_view.setHtml(MAP_HTML)
        self.tabs.addTab(self.map_view, "1. Select Area")
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background-color: #1e1e1e; color: #00ff00; font-family: monospace;")
        self.tabs.addTab(self.log_view, "2. Process Log")
        self.plot_viewer = PlotViewer()
        self.tabs.addTab(self.plot_viewer, "3. Route Analysis Plot")
        
        layout.addWidget(self.tabs)

    def start_process(self):
        self.map_view.page().runJavaScript("getSelectionBounds()", self.handle_bounds)
    def handle_bounds(self, bounds):
        """
        Handles the bounds data and initiates the worker thread.
        Checks for file existence/size if bounds are missing.
        """
        filename = self.filename_edit.text().strip()
        osm_file = f"{filename}.osm"
        is_valid_file = False
        if os.path.exists(osm_file):
            if os.path.getsize(osm_file) > MIN_OSM_FILE_SIZE:
                is_valid_file = True
        if not bounds:
            if is_valid_file:
                QMessageBox.information(self, "Using Existing File", f"No area selected. Proceeding with analysis and generation using existing file: {osm_file}")
                bounds = {'west': 0, 'south': 0, 'east': 0, 'north': 0} 
            else:
                QMessageBox.warning(self, "Action Required", "Please draw a rectangle on the map to define the simulation area, or ensure a valid OSM file exists.")
                self.tabs.setCurrentIndex(0)
                return
        config = {
            'filename': filename,
            'bbox': bounds,
            'end_time': self.time_spin.value(),
            'num_trips': self.trips_spin.value() , 
            'attack_start': self.attack_start_spin.value(),    
            'attack_duration': self.attack_duration_spin.value()
        }
        self.tabs.setCurrentIndex(1)
        self.log_view.clear()
        self.btn_generate.setEnabled(False)
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