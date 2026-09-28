# Attendance now follows presence across the whole class

## The rule
A student who leaves partway through is marked ABSENT. Attendance is no longer
"recorded once when both nodes hit 3 detections".

1. Every 10 seconds the server runs a presence check per student. A student is
   "in range" for a check when both nodes have heard their phone at least 3 times in
   the last 30 seconds (the same live rule as before).
2. When you press **End class and save** (or the optional timer runs out) each student
   gets a final result:
   - **PRESENT**: in range for at least 75% of the counted checks AND never out of range
     for 5 minutes in a row after arriving.
   - **ABSENT**: never detected, walked out (5+ minutes out of range), or in range less
     than 75% of the time. The reason is stored.
3. One row per student per class is written to `data/attendance.csv`.

Fairness rules:
- If a node stops sending heartbeats for 30 seconds, checks are **skipped** for everyone
  until it is back. A Wi-Fi problem never counts against a student.
- Arriving late only lowers the percentage. Time before first arrival is not treated as
  "left the room".
- A short break (for example 3 minutes) is tolerated.
- After a server restart the class resumes from `data/session_state.json`, and the first
  ~40 seconds of checks are skipped while live detections refill.

## Tune it (top of server.py, "ATTENDANCE POLICY")
`CHECK_INTERVAL_SEC = 10`, `MIN_PRESENCE_PERCENT = 75`, `MAX_CONTINUOUS_ABSENCE_SEC = 300`,
`CLASS_DURATION_MIN = None` (set e.g. 50 to end the class automatically),
`NODE_STALE_FOR_CHECKS_SEC = 30`.
These are attendance policy choices, not measured values. Pick numbers that suit your class.

## Dashboard
- Live state per student: In class / Out of range / Left the room / Not arrived / Not checked.
- Per-student timeline of the class (tall bar = in range, short bar = out of range) with the
  percentage, skipped checks, longest absence and "if the class ended now: PRESENT/ABSENT".
- End class and save, Start new class, Discard class (test runs), Download CSV.
- Banner while checks are paused because a node is not reporting.
- Final results view plus the saved attendance log.

## Files changed
- `server.py`: rewritten around the model above. Firmware endpoints and payloads are unchanged.
- `templates/dashboard.html`: rewritten for the new model.
- `data/attendance.csv`: new 14-column format. If an older CSV is found, the server renames it
  to `attendance_old_<time>.csv` instead of mixing formats.
- Firmware (`node_1/node_1.ino`, `node_2/node_2.ino`): unchanged. No re-flash needed.

## API additions
`POST /api/class/end`, `POST /api/class/start`. `POST /api/reset` now means "discard this class
without saving and start a fresh one". `/api/attendance` returns state, projected result, percentage
and timeline per student.

## Viva answer
"Attendance is decided by periodic presence checks over the whole class. Every 10 seconds the
laptop checks whether both ESP32 nodes are hearing each registered phone. At the end of class a
student is present only if they were in range at least 75% of the time and never out of range for
5 minutes in a row. Leaving mid-class therefore gives ABSENT. If a node goes silent those checks are
skipped, so network faults are not held against students. It is fully rule-based: no ML, and RSSI is
shown but never used to decide presence or distance."

## Known limits
- A student can still hand their phone to someone else: this verifies the device, not the person.
- If the teacher ends the class long after students have left, those students look like they left
  early. End the class while they are still in the room, or set `CLASS_DURATION_MIN`.
- A phone whose screen is locked may stop advertising in nRF Connect on some Android versions, which
  looks like leaving. Keep the app in the foreground during class.
