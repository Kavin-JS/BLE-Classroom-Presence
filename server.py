"""
BLE Classroom Presence - laptop server (Flask)

ATTENDANCE MODEL
----------------
Attendance is decided by presence across the WHOLE class, not by a single
moment.

  * Every CHECK_INTERVAL_SEC seconds the server runs a presence check for each
    registered device. A device is "in range" for that check when the live
    rule holds: at least MIN_DETECTIONS_PER_NODE detections from each node
    within the last PRESENCE_WINDOW seconds.
  * If a required node is offline (no heartbeat) or the server has just
    resumed after a restart, the check is SKIPPED for everyone. A network
    problem never counts against a student.
  * When the class ends, each student gets a final status:
        PRESENT  only if they were in range for at least MIN_PRESENCE_PERCENT
                 of the counted checks AND never went out of range for
                 MAX_CONTINUOUS_ABSENCE_SEC in a row after first arriving.
        ABSENT   otherwise (never detected, walked out, or too little time).
  * One row per student per class is written to data/attendance.csv when the
    class ends.

Rule-based only: no ML, no majority voting. RSSI is recorded and displayed but
never used to decide presence or to estimate distance.
"""

import csv
import json
import os
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

# ============================================================
# APPLICATION
# ============================================================

app = Flask(__name__)

HOST = "0.0.0.0"
PORT = 5000

SERVER_START_TIME = datetime.now()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

CSV_FILE = DATA_DIR / "attendance.csv"
STATE_FILE = DATA_DIR / "session_state.json"

# ============================================================
# NETWORK / LIVE-DETECTION CONFIGURATION
# ============================================================

# A node counts as online if its last heartbeat is newer than this.
HEARTBEAT_TIMEOUT = 60

# Only detections from this many recent seconds count toward LIVE presence.
PRESENCE_WINDOW = 30

# Detections needed from a node inside the window for it to "see" the device.
MIN_DETECTIONS_PER_NODE = 3

# Both ESP32 nodes must independently see the device.
REQUIRE_BOTH_NODES = True

# A node whose last heartbeat is older than this is treated as "not
# reporting" and attendance checks are skipped. This is deliberately shorter
# than HEARTBEAT_TIMEOUT (which only controls the Online/Offline label), so
# a node outage costs students as little as possible.
NODE_STALE_FOR_CHECKS_SEC = 30

# ============================================================
# ATTENDANCE POLICY  (tune these to your class rules)
# ============================================================

# How often a presence check runs.
CHECK_INTERVAL_SEC = 10

# Minimum share of counted checks a student must be in range for.
MIN_PRESENCE_PERCENT = 75

# Leaving for this long in a row (after arriving) marks the student ABSENT.
MAX_CONTINUOUS_ABSENCE_SEC = 300

# Optional: end the class automatically after this many minutes.
# None = the class only ends when you press "End class" on the dashboard.
CLASS_DURATION_MIN = None

# A saved unfinished class is resumed after a server restart only if the
# snapshot is newer than this.
STATE_MAX_AGE_SEC = 4 * 3600

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

NODE_IDS = ("NODE_1", "NODE_2")

CSV_HEADER = [
    "class_started",
    "class_ended",
    "device_name",
    "student_name",
    "roll_no",
    "first_seen",
    "last_seen",
    "presence_percent",
    "checks_present",
    "checks_counted",
    "checks_skipped",
    "longest_absence_sec",
    "status",
    "reason",
]

# ============================================================
# SHARED STATE  (one re-entrant lock protects everything below)
# ============================================================

lock = threading.RLock()

nodes = {
    node_id: {
        "online": False,
        "ip": "",
        "wifi_rssi": None,
        "last_heartbeat": None,
        "uptime_sec": None,
        "free_heap": None,
    }
    for node_id in NODE_IDS
}

# detections[device][node] = [datetime, ...]   (pruned to PRESENCE_WINDOW)
detections = {}

# rssi_history[device][node] = deque of recent RSSI ints (dashboard sparklines)
RSSI_HISTORY_LENGTH = 20
rssi_history = {}

# Last time a device was heard by any node (not pruned by the window).
last_seen_at = {}

session = {
    "active": True,
    "started_at": None,
    "ended_at": None,
    "checks_total": 0,
    "resume_grace_until": None,
    "paused_reason": None,
    "resumed": False,
}

students = {}

# ---------------- activity feed ----------------

events = deque(maxlen=60)
events_lock = threading.Lock()


def current_time():
    return datetime.now()


def push_event(kind, message):
    entry = {
        "time": current_time().isoformat(timespec="seconds"),
        "kind": kind,
        "message": message,
    }
    with events_lock:
        events.appendleft(entry)


# ============================================================
# HELPERS
# ============================================================

def fmt_duration(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600} h {(sec % 3600) // 60} min"
    if sec >= 60:
        return f"{sec // 60} min {sec % 60} s"
    return f"{sec} s"


def to_iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def from_iso(text):
    return datetime.fromisoformat(text) if text else None


def new_student():
    return {
        "present_checks": 0,
        "absent_checks": 0,
        "skipped_checks": 0,
        "gap_sec": 0,             # current run of out-of-range time after arriving
        "longest_gap_sec": 0,     # longest closed run so far
        "first_present": None,
        "last_present": None,
        "left": False,
        "timeline": [],           # 1 = in range, 0 = out of range, 2 = skipped
        "final_status": None,
        "final_reason": None,
    }


def downsample_timeline(timeline, max_cells=60):
    """Squash a long list of checks into at most max_cells values in [0,1]
    (share of counted checks in range) or None when nothing was counted."""
    if not timeline:
        return []
    size = -(-len(timeline) // max_cells)
    cells = []
    for i in range(0, len(timeline), size):
        counted = [c for c in timeline[i:i + size] if c in (0, 1)]
        cells.append(round(sum(counted) / len(counted), 2) if counted else None)
    return cells


# ============================================================
# CSV LOG
# ============================================================

def initialize_csv():
    """Create the attendance CSV. If an existing file has a different header
    (an older version of this project), keep it under a new name instead of
    mixing incompatible rows."""
    if CSV_FILE.exists():
        try:
            with open(CSV_FILE, newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), [])
        except Exception:
            header = []
        if header == CSV_HEADER:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        CSV_FILE.rename(DATA_DIR / f"attendance_old_{stamp}.csv")

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(CSV_HEADER)


# ============================================================
# LIVE DETECTION LOGIC
# ============================================================

def prune_detections_locked(now):
    cutoff = now - timedelta(seconds=PRESENCE_WINDOW)
    for device in list(detections):
        for node in list(detections[device]):
            kept = [t for t in detections[device][node] if t >= cutoff]
            if kept:
                detections[device][node] = kept
            else:
                del detections[device][node]
        if not detections[device]:
            del detections[device]


def live_counts_locked(device, now):
    prune_detections_locked(now)
    per_node = detections.get(device, {})
    return (
        len(per_node.get("NODE_1", [])),
        len(per_node.get("NODE_2", [])),
    )


def in_range_locked(device, now):
    n1, n2 = live_counts_locked(device, now)
    ok1 = n1 >= MIN_DETECTIONS_PER_NODE
    ok2 = n2 >= MIN_DETECTIONS_PER_NODE
    return (ok1 and ok2) if REQUIRE_BOTH_NODES else (ok1 or ok2)


def refresh_nodes_locked(now):
    """Recompute each node's online flag from heartbeat age. Returns the ids
    of nodes that just went offline (so the caller can log events)."""
    went_offline = []
    for node_id, node in nodes.items():
        online = False
        if node["last_heartbeat"]:
            try:
                age = (now - datetime.fromisoformat(node["last_heartbeat"])).total_seconds()
                online = age <= HEARTBEAT_TIMEOUT
            except Exception:
                online = False
        if node["online"] and not online:
            went_offline.append(node_id)
        node["online"] = online
    return went_offline


def node_usable_locked(node_id, now):
    """True if the node has sent a heartbeat recently enough to trust its
    detections (or lack of them) for an attendance check."""
    beat = nodes[node_id]["last_heartbeat"]
    if not beat:
        return False
    try:
        age = (now - datetime.fromisoformat(beat)).total_seconds()
    except Exception:
        return False
    return age <= NODE_STALE_FOR_CHECKS_SEC


def required_nodes_ok_locked(now):
    usable = [node_usable_locked(n, now) for n in NODE_IDS]
    return all(usable) if REQUIRE_BOTH_NODES else any(usable)


# ============================================================
# ATTENDANCE DECISION
# ============================================================

def evaluate_student(st):
    """Return (status, reason, presence_percent, longest_absence_sec) for the
    class as it stands right now."""
    counted = st["present_checks"] + st["absent_checks"]
    pct = round(100.0 * st["present_checks"] / counted, 1) if counted else 0.0
    longest = max(st["longest_gap_sec"], st["gap_sec"])

    if st["first_present"] is None:
        return "ABSENT", "never detected in class", pct, longest

    if longest >= MAX_CONTINUOUS_ABSENCE_SEC:
        return (
            "ABSENT",
            f"out of range for {fmt_duration(longest)} in a row "
            f"(limit {fmt_duration(MAX_CONTINUOUS_ABSENCE_SEC)})",
            pct,
            longest,
        )

    if pct < MIN_PRESENCE_PERCENT:
        return (
            "ABSENT",
            f"in range only {pct}% of the class (needs {MIN_PRESENCE_PERCENT}%)",
            pct,
            longest,
        )

    return "PRESENT", f"in range {pct}% of the class", pct, longest


def start_new_class_locked(now):
    session.update({
        "active": True,
        "started_at": now,
        "ended_at": None,
        "checks_total": 0,
        "resume_grace_until": None,
        "paused_reason": None,
        "resumed": False,
    })
    students.clear()
    for device in REGISTERED_DEVICES:
        students[device] = new_student()
    detections.clear()
    rssi_history.clear()
    last_seen_at.clear()


def run_presence_check():
    """One periodic check. Called by the background thread every
    CHECK_INTERVAL_SEC seconds (and directly by tests)."""
    now = current_time()
    pending_events = []
    should_auto_end = False

    with lock:
        for node_id in refresh_nodes_locked(now):
            pending_events.append(("node", f"{node_id} went offline (no heartbeat)"))

        if not session["active"]:
            for kind, message in pending_events:
                push_event(kind, message)
            return

        prune_detections_locked(now)
        session["checks_total"] += 1

        skip_reason = None
        grace = session["resume_grace_until"]
        if grace and now < grace:
            skip_reason = "recovering after a server restart"
        elif not required_nodes_ok_locked(now):
            silent = [n for n in NODE_IDS if not node_usable_locked(n, now)]
            skip_reason = ", ".join(silent) + " not reporting"
        session["paused_reason"] = skip_reason

        for device, meta in REGISTERED_DEVICES.items():
            st = students[device]

            if skip_reason:
                st["skipped_checks"] += 1
                st["timeline"].append(2)
                continue

            if in_range_locked(device, now):
                if st["first_present"] is None:
                    st["first_present"] = now
                    pending_events.append(("arrived", f"{meta['name']} arrived (heard by both nodes)"))
                if st["left"]:
                    st["left"] = False
                    pending_events.append(("returned", f"{meta['name']} is back in range"))
                st["longest_gap_sec"] = max(st["longest_gap_sec"], st["gap_sec"])
                st["gap_sec"] = 0
                st["present_checks"] += 1
                st["last_present"] = now
                st["timeline"].append(1)
            else:
                st["absent_checks"] += 1
                st["timeline"].append(0)
                # Only time AFTER first arriving counts as "left the room".
                if st["first_present"] is not None:
                    st["gap_sec"] += CHECK_INTERVAL_SEC
                    if not st["left"] and st["gap_sec"] >= MAX_CONTINUOUS_ABSENCE_SEC:
                        st["left"] = True
                        pending_events.append((
                            "left",
                            f"{meta['name']} appears to have left "
                            f"(out of range {fmt_duration(st['gap_sec'])})",
                        ))

        if CLASS_DURATION_MIN:
            elapsed = (now - session["started_at"]).total_seconds()
            should_auto_end = elapsed >= CLASS_DURATION_MIN * 60

        save_state_locked()

    for kind, message in pending_events:
        push_event(kind, message)

    if should_auto_end:
        finalize_class(auto=True)


def finalize_class(auto=False):
    """End the class, decide every student's status and write the CSV rows.
    Returns a summary dict, or None if no class was running."""
    now = current_time()
    saved = True
    error = None
    results = []

    with lock:
        if not session["active"]:
            return None

        session["active"] = False
        session["ended_at"] = now
        session["paused_reason"] = None

        rows = []
        for device, meta in REGISTERED_DEVICES.items():
            st = students[device]
            status, reason, pct, longest = evaluate_student(st)
            st["final_status"] = status
            st["final_reason"] = reason
            counted = st["present_checks"] + st["absent_checks"]
            rows.append([
                to_iso(session["started_at"]),
                to_iso(now),
                device,
                meta["name"],
                meta["roll_no"],
                to_iso(st["first_present"]) or "",
                to_iso(st["last_present"]) or "",
                pct,
                st["present_checks"],
                counted,
                st["skipped_checks"],
                int(longest),
                status,
                reason,
            ])
            results.append({"name": meta["name"], "status": status, "reason": reason})

        try:
            initialize_csv()
            with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
        except Exception as exc:
            saved = False
            error = str(exc)
            print(f"[CSV] could not write attendance: {exc}")

        save_state_locked()

    present = sum(1 for r in results if r["status"] == "PRESENT")
    push_event(
        "class",
        f"Class ended{' automatically' if auto else ''}: "
        f"{present} present, {len(results) - present} absent"
        + ("" if saved else " (CSV NOT saved)"),
    )
    return {"results": results, "saved": saved, "error": error}


# ============================================================
# SESSION SNAPSHOT (survives a server restart mid-class)
# ============================================================

def save_state_locked():
    payload = {
        "saved_at": to_iso(current_time()),
        "session": {
            "active": session["active"],
            "started_at": to_iso(session["started_at"]),
            "ended_at": to_iso(session["ended_at"]),
            "checks_total": session["checks_total"],
        },
        "students": {
            device: {
                **{k: v for k, v in st.items() if k not in ("first_present", "last_present")},
                "first_present": to_iso(st["first_present"]),
                "last_present": to_iso(st["last_present"]),
            }
            for device, st in students.items()
        },
    }
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        print(f"[STATE] could not save snapshot: {exc}")


def init_session():
    """Resume an unfinished class from the snapshot if it is recent,
    otherwise start a fresh one."""
    now = current_time()
    initialize_csv()

    with lock:
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                saved_at = from_iso(data["saved_at"])
                age = (now - saved_at).total_seconds()
                if data["session"]["active"] and 0 <= age <= STATE_MAX_AGE_SEC:
                    session.update({
                        "active": True,
                        "started_at": from_iso(data["session"]["started_at"]),
                        "ended_at": None,
                        "checks_total": data["session"]["checks_total"],
                        "resume_grace_until": now + timedelta(
                            seconds=PRESENCE_WINDOW + CHECK_INTERVAL_SEC
                        ),
                        "paused_reason": None,
                        "resumed": True,
                    })
                    students.clear()
                    for device in REGISTERED_DEVICES:
                        st = new_student()
                        saved = data["students"].get(device)
                        if saved:
                            st.update(saved)
                            st["first_present"] = from_iso(saved.get("first_present"))
                            st["last_present"] = from_iso(saved.get("last_present"))
                        students[device] = st
                    push_event("class", "Class resumed after a server restart")
                    return
        except Exception as exc:
            print(f"[STATE] could not resume snapshot, starting fresh: {exc}")

        start_new_class_locked(now)
        save_state_locked()
    push_event("class", "Class started")


# ============================================================
# API: NODE HEARTBEAT
# ============================================================

@app.post("/api/heartbeat")
def heartbeat():
    data = request.get_json(silent=True) or {}
    node_id = data.get("node_id")

    if node_id not in nodes:
        return jsonify({"status": "error", "message": "Unknown node"}), 400

    now = current_time()
    with lock:
        was_online = nodes[node_id]["online"]
        nodes[node_id].update({
            "online": True,
            "ip": data.get("ip", ""),
            "wifi_rssi": data.get("wifi_rssi"),
            "uptime_sec": data.get("uptime_sec"),
            "free_heap": data.get("free_heap"),
            "last_heartbeat": to_iso(now),
        })

    if not was_online:
        push_event("node", f"{node_id} came online ({data.get('ip', '?')})")

    print(
        f"[HEARTBEAT] {node_id} | IP={data.get('ip')} | RSSI={data.get('wifi_rssi')} | "
        f"uptime={data.get('uptime_sec')}s | heap={data.get('free_heap')}"
    )
    return jsonify({"status": "ok", "node_id": node_id})


# ============================================================
# API: BLE DETECTION
# ============================================================

@app.post("/api/detection")
def detection():
    data = request.get_json(silent=True) or {}
    node_id = data.get("node_id")
    device_name = data.get("device_name")
    rssi = data.get("rssi")

    if node_id not in nodes:
        return jsonify({"status": "error", "message": "Unknown node"}), 400

    if device_name not in REGISTERED_DEVICES:
        return jsonify({"status": "ignored", "message": "Unregistered device"})

    now = current_time()
    with lock:
        prune_detections_locked(now)
        detections.setdefault(device_name, {}).setdefault(node_id, []).append(now)
        last_seen_at[device_name] = now

        if isinstance(rssi, (int, float)):
            rssi_history.setdefault(device_name, {}).setdefault(
                node_id, deque(maxlen=RSSI_HISTORY_LENGTH)
            ).append(int(rssi))

        n1, n2 = live_counts_locked(device_name, now)

    print(f"[DETECTION] {node_id} | {device_name} | RSSI={rssi} | live N1={n1} N2={n2}")
    return jsonify({
        "status": "ok",
        "node_id": node_id,
        "device_name": device_name,
        "node_1_count": n1,
        "node_2_count": n2,
    })


# ============================================================
# API: STATUS (nodes + class info)
# ============================================================

def class_info_locked(now):
    end = session["ended_at"] or now
    elapsed = int((end - session["started_at"]).total_seconds())
    remaining = None
    if CLASS_DURATION_MIN and session["active"]:
        remaining = max(0, int(CLASS_DURATION_MIN * 60 - elapsed))
    return {
        "active": session["active"],
        "started_at": to_iso(session["started_at"]),
        "ended_at": to_iso(session["ended_at"]),
        "elapsed_sec": elapsed,
        "remaining_sec": remaining,
        "duration_min": CLASS_DURATION_MIN,
        "checks_total": session["checks_total"],
        "check_interval_sec": CHECK_INTERVAL_SEC,
        "min_presence_percent": MIN_PRESENCE_PERCENT,
        "max_absence_sec": MAX_CONTINUOUS_ABSENCE_SEC,
        "paused_reason": session["paused_reason"],
        "resumed": session["resumed"],
    }


@app.get("/api/status")
def status():
    now = current_time()
    result = {}

    with lock:
        went_offline = refresh_nodes_locked(now)

        for node_id, node in nodes.items():
            age = None
            if node["last_heartbeat"]:
                try:
                    age = round((now - datetime.fromisoformat(node["last_heartbeat"])).total_seconds(), 1)
                except Exception:
                    age = None
            result[node_id] = {
                "online": node["online"],
                "ip": node["ip"],
                "wifi_rssi": node["wifi_rssi"],
                "last_heartbeat": node["last_heartbeat"],
                "heartbeat_age_sec": age,
                "uptime_sec": node["uptime_sec"],
                "free_heap": node["free_heap"],
            }

        result["_meta"] = {
            "server_time": to_iso(now),
            "server_uptime_sec": int((now - SERVER_START_TIME).total_seconds()),
            "presence_window_sec": PRESENCE_WINDOW,
            "min_detections": MIN_DETECTIONS_PER_NODE,
            "require_both_nodes": REQUIRE_BOTH_NODES,
            "registered_total": len(REGISTERED_DEVICES),
            "class": class_info_locked(now),
        }

    for node_id in went_offline:
        push_event("node", f"{node_id} went offline (no heartbeat)")

    return jsonify(result)


# ============================================================
# API: PER-STUDENT ATTENDANCE
# ============================================================

@app.get("/api/attendance")
def get_attendance():
    now = current_time()
    rows = []

    with lock:
        for device, meta in REGISTERED_DEVICES.items():
            st = students[device]
            n1, n2 = live_counts_locked(device, now)
            in_range = in_range_locked(device, now)
            status_now, reason, pct, longest = evaluate_student(st)

            if not session["active"]:
                state = "ended"
            elif st["first_present"] is None:
                state = "arriving" if in_range else "not_arrived"
            elif in_range:
                state = "in_class"
            elif st["left"]:
                state = "left"
            else:
                state = "out_of_range"

            # While a node is silent the checks are paused, so we cannot say
            # whether the student is in range. Say so instead of guessing.
            if session["active"] and session["paused_reason"] and state in (
                "in_class", "out_of_range", "arriving"
            ):
                state = "unchecked"

            counted = st["present_checks"] + st["absent_checks"]
            seen = last_seen_at.get(device)

            rows.append({
                "device_name": device,
                "name": meta["name"],
                "roll_no": meta["roll_no"],
                "node_1_detections": n1,
                "node_2_detections": n2,
                "in_range": in_range,
                "state": state,
                "projected_status": status_now,
                "reason": reason,
                "presence_percent": pct,
                "checks_present": st["present_checks"],
                "checks_counted": counted,
                "checks_skipped": st["skipped_checks"],
                "current_gap_sec": st["gap_sec"],
                "longest_absence_sec": int(longest),
                "first_present": to_iso(st["first_present"]),
                "last_present": to_iso(st["last_present"]),
                "final_status": st["final_status"],
                "final_reason": st["final_reason"],
                "timeline": downsample_timeline(st["timeline"]),
                "last_seen": to_iso(seen),
                "rssi_history_node_1": list(rssi_history.get(device, {}).get("NODE_1", [])),
                "rssi_history_node_2": list(rssi_history.get(device, {}).get("NODE_2", [])),
            })

    return jsonify(rows)


# ============================================================
# API: CLASS CONTROL
# ============================================================

@app.post("/api/class/end")
def end_class():
    summary = finalize_class()
    if summary is None:
        return jsonify({"status": "error", "message": "No class is running"}), 409
    return jsonify({"status": "ok", **summary})


@app.post("/api/class/start")
def start_class():
    now = current_time()
    with lock:
        if session["active"]:
            return jsonify({
                "status": "error",
                "message": "A class is already running. End it first (or discard it).",
            }), 409
        start_new_class_locked(now)
        save_state_locked()
    push_event("class", "New class started")
    return jsonify({"status": "ok"})


@app.post("/api/reset")
def reset_class():
    """Discard the current class WITHOUT saving it and start a fresh one.
    Useful while testing. Rows already in the CSV are kept."""
    now = current_time()
    with lock:
        start_new_class_locked(now)
        save_state_locked()
    push_event("class", "Class discarded and restarted (nothing saved)")
    print("[SESSION] class discarded and restarted")
    return jsonify({"status": "ok"})


# ============================================================
# API: FEED, HISTORY, EXPORT, HEALTH
# ============================================================

@app.get("/api/events")
def get_events():
    with events_lock:
        snapshot = list(events)
    return jsonify(snapshot)


@app.get("/api/history")
def get_history():
    rows = []
    if CSV_FILE.exists():
        with lock:
            with open(CSV_FILE, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
    rows.reverse()
    return jsonify(rows)


@app.get("/api/export")
def export_csv():
    if not CSV_FILE.exists():
        return jsonify({"status": "error", "message": "No attendance log yet"}), 404
    return send_file(
        CSV_FILE,
        mimetype="text/csv",
        as_attachment=True,
        download_name="attendance.csv",
    )


@app.get("/api/health")
def health():
    return jsonify({
        "server": "BLE Classroom Presence",
        "status": "ok",
        "time": current_time().isoformat(),
    })


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


# ============================================================
# BACKGROUND CHECKER + MAIN
# ============================================================

_checker_started = False


def presence_checker_loop():
    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        try:
            run_presence_check()
        except Exception:
            traceback.print_exc()


def start_presence_checker():
    global _checker_started
    if _checker_started:
        return
    _checker_started = True
    threading.Thread(target=presence_checker_loop, daemon=True).start()


init_session()

if __name__ == "__main__":
    print()
    print("=" * 60)
    print("BLE CLASSROOM PRESENCE SERVER")
    print("=" * 60)
    print(f"Dashboard: http://11.12.10.198:{PORT}")
    print(f"Server: {HOST}:{PORT}")
    print(f"Registered devices: {len(REGISTERED_DEVICES)}")
    print(f"Live rule: {MIN_DETECTIONS_PER_NODE} detections/node in {PRESENCE_WINDOW}s"
          f" (both nodes required: {REQUIRE_BOTH_NODES})")
    print(f"Attendance: check every {CHECK_INTERVAL_SEC}s, present if in range "
          f">= {MIN_PRESENCE_PERCENT}% and never out of range "
          f">= {MAX_CONTINUOUS_ABSENCE_SEC}s in a row")
    print(f"Heartbeat timeout: {HEARTBEAT_TIMEOUT}s")
    print(f"CSV: {CSV_FILE}")
    print("=" * 60)
    print()

    start_presence_checker()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
