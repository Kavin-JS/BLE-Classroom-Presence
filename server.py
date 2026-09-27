from flask import Flask, request, jsonify, render_template
from datetime import datetime
from pathlib import Path
import csv
import threading


# ============================================================
# APPLICATION
# ============================================================

app = Flask(__name__)

HOST = "0.0.0.0"
PORT = 5000


# ============================================================
# DATA CONFIGURATION
# ============================================================

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

CSV_FILE = DATA_DIR / "attendance.csv"


# ============================================================
# VERIFICATION CONFIGURATION
# ============================================================

# ESP32 sends heartbeat approximately every 8 seconds.
# Allow temporary Wi-Fi/HTTP failures without immediately
# showing the node as offline.
HEARTBEAT_TIMEOUT = 60

# Only detections from this period count toward LIVE presence.
PRESENCE_WINDOW = 30

# Required observations from each physical node.
MIN_DETECTIONS_PER_NODE = 3

# Both ESP32 nodes must independently observe the device.
REQUIRE_BOTH_NODES = True


# ============================================================
# REGISTERED BLE DEVICES
# ============================================================

REGISTERED_DEVICES = {

    "ATTEND-KAVIN": {
        "roll_no": "CH.SC.U4CSE24119",
        "name": "Kavin J S",
    },

    "ATTEND-LOHITH": {
        "roll_no": "CH.SC.U4CSE24153",
        "name": "Lohith",
    },
}


# ============================================================
# NODE STATE
# ============================================================

nodes = {

    "NODE_1": {
        "online": False,
        "ip": "",
        "wifi_rssi": None,
        "last_heartbeat": None,
    },

    "NODE_2": {
        "online": False,
        "ip": "",
        "wifi_rssi": None,
        "last_heartbeat": None,
    },
}


# ============================================================
# DETECTION STATE
# ============================================================

# Live BLE detections.
#
# Structure:
#
# detections = {
#     "ATTEND-KAVIN": {
#         "NODE_1": [timestamp, timestamp, ...],
#         "NODE_2": [timestamp, timestamp, ...]
#     }
# }
#
detections = {}


# ============================================================
# ATTENDANCE STATE
# ============================================================

# Stores students that have already been verified
# during the CURRENT attendance session.
#
# Example:
#
# attendance = {
#     "ATTEND-KAVIN": {
#         "verified_at": datetime(...)
#     }
# }
#
# IMPORTANT:
# This is intentionally separate from live detections.
#
# Live detections can expire after 30 seconds.
# Verification does NOT expire during the current session.
#
attendance = {}


# ============================================================
# THREAD LOCK
# ============================================================

lock = threading.Lock()


# ============================================================
# CSV INITIALIZATION
# ============================================================

def initialize_csv():

    if CSV_FILE.exists():
        return

    with open(
        CSV_FILE,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.writer(file)

        writer.writerow([
            "timestamp",
            "device_name",
            "student_name",
            "roll_no",
            "node_1_detections",
            "node_2_detections",
            "verification",
        ])


initialize_csv()


# ============================================================
# TIME
# ============================================================

def current_time():
    return datetime.now()


# ============================================================
# CLEAN OLD LIVE DETECTIONS
# ============================================================

def cleanup_detections_locked():

    """
    Remove BLE observations older than PRESENCE_WINDOW.

    Caller must hold `lock`.
    """

    now = current_time()

    for device_name in list(detections.keys()):

        device_data = detections[device_name]

        for node_id in list(device_data.keys()):

            timestamps = device_data[node_id]

            device_data[node_id] = [
                timestamp
                for timestamp in timestamps
                if (
                    now - timestamp
                ).total_seconds()
                <= PRESENCE_WINDOW
            ]

            if not device_data[node_id]:
                del device_data[node_id]

        if not device_data:
            del detections[device_name]


def cleanup_detections():

    with lock:
        cleanup_detections_locked()


# ============================================================
# GET LIVE COUNTS
# ============================================================

def get_live_counts_locked(device_name):

    """
    Return current live detection counts.

    Caller must hold `lock`.
    """

    cleanup_detections_locked()

    device_data = detections.get(
        device_name,
        {}
    )

    node_1_count = len(
        device_data.get(
            "NODE_1",
            []
        )
    )

    node_2_count = len(
        device_data.get(
            "NODE_2",
            []
        )
    )

    return node_1_count, node_2_count


# ============================================================
# PRESENCE VERIFICATION
# ============================================================

def verify_presence(device_name):

    """
    Check whether the device currently has enough
    live evidence from the required nodes.

    IMPORTANT:
    This function only evaluates LIVE evidence.

    Persistent attendance verification is handled separately
    by `attendance`.
    """

    with lock:

        if device_name not in REGISTERED_DEVICES:
            return False

        node_1_count, node_2_count = (
            get_live_counts_locked(
                device_name
            )
        )

        if REQUIRE_BOTH_NODES:

            return (
                node_1_count
                >= MIN_DETECTIONS_PER_NODE
                and
                node_2_count
                >= MIN_DETECTIONS_PER_NODE
            )

        return (
            node_1_count
            >= MIN_DETECTIONS_PER_NODE
            or
            node_2_count
            >= MIN_DETECTIONS_PER_NODE
        )


# ============================================================
# LOG ATTENDANCE
# ============================================================

def log_attendance(device_name):

    """
    Mark a device as VERIFIED for the current session.

    Once verified, it remains verified even when its
    30-second live detections expire.

    Only one CSV record is written per session.
    """

    if device_name not in REGISTERED_DEVICES:
        return False

    with lock:

        # Already verified in this session.
        if device_name in attendance:
            return True

        cleanup_detections_locked()

        device_data = detections.get(
            device_name,
            {}
        )

        node_1_count = len(
            device_data.get(
                "NODE_1",
                []
            )
        )

        node_2_count = len(
            device_data.get(
                "NODE_2",
                []
            )
        )

        # Check verification requirement.
        if REQUIRE_BOTH_NODES:

            verified = (
                node_1_count
                >= MIN_DETECTIONS_PER_NODE
                and
                node_2_count
                >= MIN_DETECTIONS_PER_NODE
            )

        else:

            verified = (
                node_1_count
                >= MIN_DETECTIONS_PER_NODE
                or
                node_2_count
                >= MIN_DETECTIONS_PER_NODE
            )

        if not verified:
            return False

        now = current_time()

        # Store persistent session verification.
        attendance[device_name] = {
            "verified_at": now,
        }

        student = REGISTERED_DEVICES[
            device_name
        ]

        timestamp = now.isoformat(
            timespec="seconds"
        )

        # Write one attendance record.
        with open(
            CSV_FILE,
            "a",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.writer(file)

            writer.writerow([
                timestamp,
                device_name,
                student["name"],
                student["roll_no"],
                node_1_count,
                node_2_count,
                "VERIFIED",
            ])

        print(
            f"[ATTENDANCE] "
            f"{student['name']} "
            f"({student['roll_no']}) "
            f"VERIFIED "
            f"| NODE_1={node_1_count} "
            f"| NODE_2={node_2_count}"
        )

        return True


# ============================================================
# HEARTBEAT API
# ============================================================

@app.post("/api/heartbeat")
def heartbeat():

    data = request.get_json(
        silent=True
    ) or {}

    node_id = data.get(
        "node_id"
    )

    if node_id not in nodes:

        return jsonify({
            "status": "error",
            "message": "Unknown node",
        }), 400

    now = current_time()

    with lock:

        nodes[node_id]["online"] = True

        nodes[node_id]["ip"] = data.get(
            "ip",
            ""
        )

        nodes[node_id]["wifi_rssi"] = data.get(
            "wifi_rssi"
        )

        nodes[node_id]["last_heartbeat"] = (
            now.isoformat(
                timespec="seconds"
            )
        )

    print(
        f"[HEARTBEAT] "
        f"{node_id} | "
        f"IP={data.get('ip')} | "
        f"RSSI={data.get('wifi_rssi')}"
    )

    return jsonify({
        "status": "ok",
        "node_id": node_id,
    })


# ============================================================
# DETECTION API
# ============================================================

@app.post("/api/detection")
def detection():

    data = request.get_json(
        silent=True
    ) or {}

    node_id = data.get(
        "node_id"
    )

    device_name = data.get(
        "device_name"
    )

    mac = data.get(
        "mac"
    )

    rssi = data.get(
        "rssi"
    )

    # --------------------------------------------------------
    # Validate node
    # --------------------------------------------------------

    if node_id not in nodes:

        return jsonify({
            "status": "error",
            "message": "Unknown node",
        }), 400

    # --------------------------------------------------------
    # Validate BLE device
    # --------------------------------------------------------

    if device_name not in REGISTERED_DEVICES:

        return jsonify({
            "status": "ignored",
            "message": "Unregistered device",
        })

    now = current_time()

    # --------------------------------------------------------
    # Add live detection
    # --------------------------------------------------------

    with lock:

        cleanup_detections_locked()

        if device_name not in detections:

            detections[device_name] = {}

        if node_id not in detections[device_name]:

            detections[
                device_name
            ][node_id] = []

        detections[
            device_name
        ][
            node_id
        ].append(now)

        node_1_count, node_2_count = (
            get_live_counts_locked(
                device_name
            )
        )

        already_verified = (
            device_name in attendance
        )

    print(
        f"[DETECTION] "
        f"{node_id} | "
        f"{device_name} | "
        f"MAC={mac} | "
        f"RSSI={rssi} | "
        f"LIVE N1={node_1_count} "
        f"N2={node_2_count}"
    )

    # --------------------------------------------------------
    # Verify only once
    # --------------------------------------------------------

    if not already_verified:

        verified = log_attendance(
            device_name
        )

    else:

        verified = True

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    return jsonify({

        "status": "ok",

        "verified": verified,

        "device_name":
            device_name,

        "node_1_detections":
            node_1_count,

        "node_2_detections":
            node_2_count,

    })


# ============================================================
# NODE STATUS API
# ============================================================

@app.get("/api/status")
def status():

    now = current_time()

    result = {}

    with lock:

        for node_id, node in nodes.items():

            online = False

            if node["last_heartbeat"]:

                try:

                    last_time = (
                        datetime.fromisoformat(
                            node["last_heartbeat"]
                        )
                    )

                    age = (
                        now - last_time
                    ).total_seconds()

                    online = (
                        age <= HEARTBEAT_TIMEOUT
                    )

                except Exception:

                    online = False

            result[node_id] = {

                "online":
                    online,

                "ip":
                    node["ip"],

                "wifi_rssi":
                    node["wifi_rssi"],

                "last_heartbeat":
                    node["last_heartbeat"],
            }

    return jsonify(result)


# ============================================================
# ATTENDANCE API
# ============================================================

@app.get("/api/attendance")
def get_attendance():

    result = []

    with lock:

        cleanup_detections_locked()

        for device_name, student in (
            REGISTERED_DEVICES.items()
        ):

            # ----------------------------------------------
            # LIVE COUNTS
            # ----------------------------------------------

            device_data = detections.get(
                device_name,
                {}
            )

            node_1_count = len(
                device_data.get(
                    "NODE_1",
                    []
                )
            )

            node_2_count = len(
                device_data.get(
                    "NODE_2",
                    []
                )
            )

            # ----------------------------------------------
            # PERSISTENT VERIFICATION
            # ----------------------------------------------

            is_verified = (
                device_name in attendance
            )

            verified_at = None

            if is_verified:

                verified_at = (
                    attendance[
                        device_name
                    ][
                        "verified_at"
                    ].isoformat(
                        timespec="seconds"
                    )
                )

            result.append({

                "device_name":
                    device_name,

                "name":
                    student["name"],

                "roll_no":
                    student["roll_no"],

                # Current live observations.
                "node_1_detections":
                    node_1_count,

                "node_2_detections":
                    node_2_count,

                # Persistent session verification.
                "verified":
                    is_verified,

                "verified_at":
                    verified_at,
            })

    return jsonify(result)


# ============================================================
# RESET ATTENDANCE SESSION
# ============================================================

@app.post("/api/reset")
def reset_attendance():

    """
    Start a fresh attendance session.

    This clears:
        - verified attendance
        - live BLE detections

    It does NOT affect:
        - ESP32 nodes
        - Wi-Fi
        - Flask server
        - registered devices
        - CSV history
    """

    with lock:

        attendance.clear()
        detections.clear()

    print(
        "[SESSION] Attendance session reset"
    )

    return jsonify({

        "status":
            "ok",

        "message":
            "Attendance session reset",

    })


# ============================================================
# HEALTH API
# ============================================================

@app.get("/api/health")
def health():

    return jsonify({

        "server":
            "BLE Classroom Presence",

        "status":
            "ok",

        "time":
            current_time().isoformat(),

    })


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/")
def dashboard():

    return render_template(
        "dashboard.html"
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print()
    print("=" * 60)
    print("BLE CLASSROOM PRESENCE SERVER")
    print("=" * 60)

    print(
        f"Dashboard: "
        f"http://11.12.10.198:{PORT}"
    )

    print(
        f"Server: "
        f"{HOST}:{PORT}"
    )

    print(
        f"Registered devices: "
        f"{len(REGISTERED_DEVICES)}"
    )

    print(
        f"Presence window: "
        f"{PRESENCE_WINDOW}s"
    )

    print(
        f"Minimum detections/node: "
        f"{MIN_DETECTIONS_PER_NODE}"
    )

    print(
        f"Require both nodes: "
        f"{REQUIRE_BOTH_NODES}"
    )

    print(
        f"Heartbeat timeout: "
        f"{HEARTBEAT_TIMEOUT}s"
    )

    print("=" * 60)
    print()

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        threaded=True,
    )