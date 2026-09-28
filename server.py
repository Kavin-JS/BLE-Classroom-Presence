from flask import Flask, request, jsonify, render_template, send_file
from datetime import datetime
from pathlib import Path
from collections import deque
import csv
import threading


# ============================================================
# APPLICATION
# ============================================================

app = Flask(__name__)

HOST = "0.0.0.0"
PORT = 5000

SERVER_START_TIME = datetime.now()


# ============================================================
# DATA CONFIGURATION
# ============================================================

DATA_DIR = Path(__file__).resolve().parent / "data"
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
        "uptime_sec": None,
        "free_heap": None,
    },

    "NODE_2": {
        "online": False,
        "ip": "",
        "wifi_rssi": None,
        "last_heartbeat": None,
        "uptime_sec": None,
        "free_heap": None,
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
# RSSI HISTORY (for live sparkline charts on the dashboard)
# ============================================================

# Keeps the last RSSI_HISTORY_LENGTH readings per device per node.
#
# Structure:
#
# rssi_history = {
#     "ATTEND-KAVIN": {
#         "NODE_1": deque([-58, -57, -60, ...], maxlen=20),
#         "NODE_2": deque([-64, -63, -65, ...], maxlen=20),
#     }
# }
#
RSSI_HISTORY_LENGTH = 20
rssi_history = {}

# Most recent time each device was heard by ANY node. Unlike `detections`,
# this is not pruned after the presence window, so the dashboard can say
# "last heard 3 min ago" after a phone leaves.
last_seen_at = {}


# ============================================================
# ACTIVITY FEED
# ============================================================

# A rolling log of notable events for the dashboard's live feed.
# Kept small and in-memory only — this is a UI convenience, not a
# system of record (the CSV remains the authoritative attendance log).
EVENTS_MAX = 60
events = deque(maxlen=EVENTS_MAX)
events_lock = threading.Lock()


def push_event(kind, message):
    """Append one event to the activity feed.

    Uses its own lock (events_lock), so callers must NOT hold it and
    may call this whether or not they hold the main `lock`."""
    entry = {
        "time": current_time().isoformat(timespec="seconds"),
        "kind": kind,       # "node" | "detection" | "verified" | "session"
        "message": message,
    }
    with events_lock:
        events.appendleft(entry)


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

        was_online = nodes[node_id]["online"]

        nodes[node_id]["online"] = True

        nodes[node_id]["ip"] = data.get(
            "ip",
            ""
        )

        nodes[node_id]["wifi_rssi"] = data.get(
            "wifi_rssi"
        )

        nodes[node_id]["uptime_sec"] = data.get(
            "uptime_sec"
        )

        nodes[node_id]["free_heap"] = data.get(
            "free_heap"
        )

        nodes[node_id]["last_heartbeat"] = (
            now.isoformat(
                timespec="seconds"
            )
        )

    if not was_online:
        push_event("node", f"{node_id} came online ({data.get('ip', '?')})")

    print(
        f"[HEARTBEAT] "
        f"{node_id} | "
        f"IP={data.get('ip')} | "
        f"RSSI={data.get('wifi_rssi')} | "
        f"uptime={data.get('uptime_sec')}s | "
        f"heap={data.get('free_heap')}"
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

        # Counts BEFORE this observation (used to spot a device that has
        # just appeared). Must run before the empty lists are created
        # below, because the cleanup inside get_live_counts_locked()
        # deletes empty entries.
        prev_1, prev_2 = get_live_counts_locked(device_name)

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

        last_seen_at[device_name] = now

        # Record RSSI for the dashboard's live sparkline charts.
        if isinstance(rssi, (int, float)):

            history_for_device = rssi_history.setdefault(
                device_name, {}
            )

            history_for_device.setdefault(
                node_id,
                deque(maxlen=RSSI_HISTORY_LENGTH),
            ).append(int(rssi))

        node_1_count, node_2_count = (
            get_live_counts_locked(
                device_name
            )
        )

        already_verified = (
            device_name in attendance
        )

        newly_appeared = (prev_1 == 0 and prev_2 == 0)

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

    if newly_appeared and not already_verified:

        push_event(
            "detection",
            f"{device_name} detected by {node_id} "
            f"(RSSI {rssi} dBm)"
        )

    if not already_verified:

        verified = log_attendance(
            device_name
        )

        if verified:

            student = REGISTERED_DEVICES[device_name]

            push_event(
                "verified",
                f"{student['name']} ({student['roll_no']}) "
                f"marked PRESENT"
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
    went_offline = []

    with lock:

        for node_id, node in nodes.items():

            online = False
            age = None

            if node["last_heartbeat"]:

                try:

                    last_time = datetime.fromisoformat(
                        node["last_heartbeat"]
                    )

                    age = (now - last_time).total_seconds()

                    online = age <= HEARTBEAT_TIMEOUT

                except Exception:

                    online = False

            # Write the derived state back so the heartbeat handler can
            # detect offline -> online transitions, and so we can emit an
            # event exactly once when a node drops offline.
            if node["online"] and not online:
                went_offline.append(node_id)

            node["online"] = online

            result[node_id] = {
                "online": online,
                "ip": node["ip"],
                "wifi_rssi": node["wifi_rssi"],
                "last_heartbeat": node["last_heartbeat"],
                "heartbeat_age_sec": (
                    round(age, 1) if age is not None else None
                ),
                "uptime_sec": node["uptime_sec"],
                "free_heap": node["free_heap"],
            }

        result["_meta"] = {
            "server_time": now.isoformat(timespec="seconds"),
            "server_uptime_sec": int(
                (now - SERVER_START_TIME).total_seconds()
            ),
            "presence_window_sec": PRESENCE_WINDOW,
            "min_detections": MIN_DETECTIONS_PER_NODE,
            "require_both_nodes": REQUIRE_BOTH_NODES,
            "registered_total": len(REGISTERED_DEVICES),
            "verified_total": len(attendance),
        }

    for node_id in went_offline:
        push_event("node", f"{node_id} went offline (no heartbeat)")

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


            last_seen_dt = last_seen_at.get(device_name)

            last_seen_iso = (
                last_seen_dt.isoformat(timespec="seconds")
                if last_seen_dt else None
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

                # Recent RSSI samples (oldest -> newest) for sparklines.
                # RSSI is shown as observed signal strength only; it is
                # NOT converted into a distance estimate anywhere.
                "rssi_history_node_1":
                    list(
                        rssi_history
                        .get(device_name, {})
                        .get("NODE_1", [])
                    ),

                "rssi_history_node_2":
                    list(
                        rssi_history
                        .get(device_name, {})
                        .get("NODE_2", [])
                    ),

                # Most recent live detection timestamp (either node).
                "last_seen":
                    last_seen_iso,
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
        rssi_history.clear()
        last_seen_at.clear()

    push_event("session", "New attendance session started (reset)")

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
# ACTIVITY FEED API
# ============================================================

@app.get("/api/events")
def get_events():

    """Most recent events first (node up/down, detections, verifications)."""

    with events_lock:
        snapshot = list(events)

    return jsonify(snapshot)


# ============================================================
# ATTENDANCE HISTORY (rows recorded in the CSV log)
# ============================================================

@app.get("/api/history")
def get_history():

    """Recorded attendance rows from data/attendance.csv, newest first.

    Unlike /api/attendance (live presence for the current session),
    this is the permanent log of what was actually written to disk."""

    rows = []

    if CSV_FILE.exists():

        with lock:

            with open(CSV_FILE, newline="") as file:

                for row in csv.DictReader(file):
                    rows.append(row)

    rows.reverse()

    return jsonify(rows)


# ============================================================
# CSV EXPORT
# ============================================================

@app.get("/api/export")
def export_csv():

    """Download the attendance CSV log."""

    if not CSV_FILE.exists():

        return jsonify({
            "status": "error",
            "message": "No attendance log yet",
        }), 404

    return send_file(
        CSV_FILE,
        mimetype="text/csv",
        as_attachment=True,
        download_name="attendance.csv",
    )


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