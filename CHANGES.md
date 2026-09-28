# What changed in this version

## Dashboard (templates/dashboard.html, fully rewritten)
- "Who hears whom" diagram: shows which node has heard each device in the live window. Line thickness grows toward the detection threshold.
- Node panels: online state, address, Wi-Fi signal, heartbeat age, node uptime, free memory.
- Per-device rows: detection progress per node, RSSI sparkline per node, present / being heard / not seen.
- Live activity feed (node up/down, first detection, attendance recorded).
- Attendance log table read from data/attendance.csv.
- Toast when someone is marked present. "Start new session" (with confirmation) and "Download CSV" buttons.
- Works offline (no CDN, no web fonts). Shows a banner if the server stops responding.
- RSSI is displayed as measured only. It is never converted to distance.

## server.py
- New endpoints: /api/events, /api/history, /api/export.
- /api/status now also returns uptime, free heap, heartbeat age and a _meta block.
- /api/attendance now also returns RSSI history and last-seen time per device.
- Earlier fixes kept: CSV path anchored to the script folder.
- Verification rules, thresholds, CSV format and duplicate handling are unchanged.

## node_1.ino / node_2.ino
- Heartbeat JSON now also carries uptime_sec and free_heap. Nothing else changed.
- node_2.ino uses NODE_ID "NODE_2" (all caps). Both files differ only in that line.

## Deploying
1. Replace server.py and templates/dashboard.html, then restart the server.
2. Re-flash both nodes to get uptime and memory on the node panels (optional; the dashboard shows "unknown" until then).
3. If data/attendance.csv still has the old 9-column header, delete it once. The server recreates it with the correct 7 columns.
