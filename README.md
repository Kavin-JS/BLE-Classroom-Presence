# Multi-Node BLE-Based Classroom Presence Verification System

Two ESP32 nodes scan for registered BLE advertisements, report detections
and heartbeats to a Flask server on the laptop, which applies rule-based,
time-window presence verification (no ML, no majority voting) and logs
attendance to CSV — with a live dashboard.

```
BLE-Classroom-Presence/
├── esp32_node_1/node_1.ino        NODE_1 firmware
├── esp32_node_2/node_2.ino        NODE_2 firmware
├── laptop_server/
│   ├── server.py                  Flask app: routes + presence logic + CSV
│   ├── requirements.txt
│   └── templates/dashboard.html   Live dashboard (polls the Flask API)
├── data/attendance.csv            Attendance log (header pre-created)
└── README.md
```

No files are missing from what your project spec requires. Nothing extra
was added beyond what's needed to make this runnable and testable.

---

## 1. Arduino IDE library requirements

You need **no extra libraries**. Everything used (`WiFi.h`, `HTTPClient.h`,
`BLEDevice.h`, `BLEUtils.h`, `BLEScan.h`, `BLEAdvertisedDevice.h`) ships
with the official **ESP32 board package** for Arduino. Install that
package via:

1. Arduino IDE → **File → Preferences** → Additional Board Manager URLs:
   `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
2. **Tools → Board → Boards Manager** → search "esp32" → install
   **esp32 by Espressif Systems**.

## 2. ESP32 board settings (Arduino IDE)

For each upload:

| Setting | Value |
|---|---|
| Board | **ESP32 Dev Module** |
| Upload Speed | 921600 (drop to 115200 if uploads fail) |
| Flash Frequency | 80MHz |
| Flash Mode | DIO |
| Flash Size | 4MB (32Mb) |
| Partition Scheme | Default |
| Core Debug Level | None |
| Port | your board's `/dev/ttyUSB0` or `/dev/ttyACM0` (check with `ls /dev/tty*` before/after plugging in) |

## 3. Exact LED wiring

Identical wiring on **both** nodes. For each LED: `GPIO pin → 220–330Ω
resistor → LED long leg (anode) → LED short leg (cathode) → GND`.

| LED | GPIO Pin | Meaning |
|---|---|---|
| Green | GPIO 25 | System ready (Wi-Fi connected) |
| Blue | GPIO 26 | Actively scanning BLE |
| Yellow | GPIO 27 | Just sent data to laptop (brief ~150ms flash) |
| Red | GPIO 14 | Wi-Fi down or 3+ consecutive failed HTTP sends |

**LED behavior, exactly as implemented:**
- **Green** is solid ON whenever Wi-Fi is connected, OFF otherwise. It
  does not turn off during scanning — it's a baseline "board is alive
  and networked" indicator.
- **Blue** turns ON for the duration of each 4-second BLE scan and OFF
  once that scan ends. Between scans there's a brief gap while the
  node processes results and sends any HTTP detections/heartbeat —
  normally well under a second unless the network is slow — so Blue
  will blink off momentarily between cycles rather than staying
  literally continuous.
- **Yellow** flashes ON for 150ms every time an HTTP POST (detection or
  heartbeat) succeeds, then turns off automatically — non-blocking, so
  it never stalls scanning.
- **Red** turns ON if Wi-Fi is disconnected, or after 3 consecutive
  failed HTTP sends. It clears the moment a send succeeds again. Red
  and Green can be on/off independently; they're not mutually
  exclusive by design (e.g., Wi-Fi could be up but the server briefly
  unreachable).

These states never conflict with BLE scanning because all LED writes
are plain `digitalWrite()` calls with no `delay()` in the scan path —
timing is handled with `millis()` comparisons only.

## 4. Exact nRF Connect configuration (per phone)

1. Install **nRF Connect for Mobile** (Nordic Semiconductor) from the
   Play Store / App Store.
2. Open the app and go to the **Advertiser** feature (in recent
   versions this is a dedicated tab or accessible from the app's main
   menu — look for "Advertiser" or a "+" to create a new advertising
   packet).
3. Create a new advertising packet and add an AD (advertising data)
   entry of type **"Local name"** (sometimes labeled "Complete Local
   Name").
4. Set its value to exactly:
   - `ATTEND-KAVIN` on Kavin's phone
   - `ATTEND-LOHITH` on Lohith's phone
   (Case-sensitive — must match `REGISTERED_DEVICES[]` in the firmware
   exactly.)
5. Leave the packet as **non-connectable / general discoverable** —
   connectability isn't needed, only the advertisement matters.
6. Start advertising and **keep the phone screen on and the app in the
   foreground** during testing/demo. Android aggressively throttles or
   stops BLE advertising when the screen locks or the app is
   backgrounded on many OS versions — this is a real limitation, not a
   bug in your code, and is worth mentioning if your professor asks
   about practical deployment constraints.

## 5. Wi-Fi configuration

Already hardcoded in both `.ino` files:
```cpp
const char* WIFI_SSID     = "Amrita_CHN2";
const char* WIFI_PASSWORD = "amrita@321";
```
No changes needed unless the network changes.

**If college Wi-Fi blocks ESP32 ↔ laptop communication** (client/AP
isolation or a captive portal login page — both are common on
institutional networks and would make BLE detection work but HTTP
posts silently fail), switch to a laptop hotspot instead.

**Experimentally observed on this network:** on testing, the college
Wi-Fi (`Amrita_CHN2`) was *not* actually isolating the ESP32 from the
laptop — both addresses fell within the same `/19` subnet and `ping`
succeeded — but round-trip latency was extremely high and inconsistent
(20ms to 6600+ ms, with duplicate packets and up to ~40% loss in some
samples), consistent with a congested or roaming mesh Wi-Fi
deployment. This caused HTTP requests from the ESP32 to time out
(`code=-1` in Serial Monitor) even though the network path itself was
open. The firmware and server timeouts in this project (`HTTP_TIMEOUT_MS
= 8000` on the ESP32, `HEARTBEAT_TIMEOUT_SECONDS = 45` on the server,
and `WiFi.setSleep(false)` to disable ESP32 modem sleep, which
otherwise worsens latency further on a busy AP) were tuned specifically
to tolerate this. If your node still shows frequent `code=-1` failures
after these changes, that's this network's congestion, not a bug — the
hotspot fallback below sidesteps it entirely.

If you still want/need the hotspot fallback:

1. On your CachyOS laptop, create a Wi-Fi hotspot (Settings → Wi-Fi →
   "Turn On Wi-Fi Hotspot", or `nmcli device wifi hotspot ssid <name>
   password <password>` from a terminal).
2. Update `WIFI_SSID` / `WIFI_PASSWORD` in **both** `.ino` files to the
   hotspot's credentials, re-flash both nodes.
3. Find your laptop's new IP on the hotspot interface (`ip addr show`
   — look for the hotspot interface, often `10.42.0.1` by default on
   NetworkManager hotspots) and update `SERVER_IP` in both `.ino`
   files to match.
4. Re-flash both nodes. No server-side changes needed.

## 6–7. Flashing Node 1 and Node 2

1. Connect ESP32 #1 via USB-C.
2. Open `esp32_node_1/node_1.ino` in Arduino IDE.
3. Select the correct **Port** (Tools → Port).
4. Click **Upload**.
5. Open **Serial Monitor** at **115200 baud** — confirm you see
   `[NODE_1] Wi-Fi connected. IP: ...`.
6. Disconnect Node 1, connect ESP32 #2.
7. Open `esp32_node_2/node_2.ino`, select its port, **Upload**.
8. Confirm `[NODE_2] Wi-Fi connected. IP: ...` in Serial Monitor.

(Only the `NODE_ID` constant differs between the two files — you do
not need to edit anything before flashing either one.)

## 8. Starting the Flask server on CachyOS

```bash
cd BLE-Classroom-Presence/laptop_server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 server.py
```

You should see:
```
Multi-Node BLE Presence Verification server starting...
Dashboard: http://11.12.10.198:5000/  (or http://localhost:5000/ locally)
 * Running on all addresses (0.0.0.0)
```

Make sure your laptop's firewall allows inbound connections on port
5000 (on CachyOS with `ufw`: `sudo ufw allow 5000/tcp`; if using
`firewalld`: `sudo firewall-cmd --add-port=5000/tcp --permanent &&
sudo firewall-cmd --reload`).

## 9. Accessing the dashboard

From the laptop itself: **http://localhost:5000/**
From any device on the same network: **http://11.12.10.198:5000/**

It auto-refreshes node/device status every 2 seconds and attendance
history every 4 seconds — no manual refresh needed during a demo.

## 10. Node-by-node testing

1. Start the Flask server first.
2. Power **only Node 1**. Confirm on the dashboard: Node 1 shows
   **online**, Node 2 shows **offline**.
3. Start BLE advertising `ATTEND-KAVIN` on the test phone, hold it
   near Node 1.
4. Within ~15–20 seconds you should see Node 1's detection count for
   `ATTEND-KAVIN` climb and its RSSI populate on the dashboard.
5. Repeat with only Node 2 powered.

## 11. Complete two-node testing

1. Power both nodes, confirm both show **online**.
2. Place the test phone roughly between both nodes, advertising
   `ATTEND-KAVIN`.
3. Watch both nodes' detection counts climb simultaneously on the
   dashboard.
4. Once both nodes reach `MIN_DETECTIONS_PER_NODE_IN_WINDOW` (default
   3) within `PRESENCE_WINDOW_SECONDS` (default 30s), the device's
   badge should flip to **PRESENT**.

## 12. Attendance testing

1. Once a device shows **PRESENT**, check `data/attendance.csv` — a
   new row should appear with student ID, date, time, both nodes'
   detection status, and both RSSI values.
2. Keep the phone advertising continuously for several more minutes —
   confirm **no duplicate row** is added for the same student on the
   same day (this is the intended duplicate-prevention behavior).
3. Stop advertising, wait past the presence window, then start
   advertising again on the **next day** (or manually test by editing
   the system date, if your setup allows) to confirm a new row is
   created the following day.

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Node never shows "online" | Wrong Wi-Fi credentials, or client isolation on college Wi-Fi | Check Serial Monitor for connection errors; try the hotspot fallback (Section 5) |
| Node online, but no detections ever appear | nRF Connect "Local name" doesn't exactly match `REGISTERED_DEVICES[]`, or phone stopped advertising (screen locked/app backgrounded) | Double-check spelling/case; keep phone screen on and app foregrounded |
| Frequent `HTTP POST ... failed, code=-1` in Serial Monitor even though Wi-Fi shows connected | Congested/high-latency college Wi-Fi (verify with `ping <esp32-ip>` from the laptop — very high or wildly varying round-trip times confirm this) | Already mitigated with an 8s HTTP timeout and `WiFi.setSleep(false)`; if it persists, use the hotspot fallback above |
| Detections appear but device never reaches PRESENT | Not enough detections within the window from both nodes | Lower `MIN_DETECTIONS_PER_NODE_IN_WINDOW` or increase `PRESENCE_WINDOW_SECONDS` in `server.py`, or hold the phone closer/longer during testing |
| Red LED stuck on | Node lost Wi-Fi or can't reach `11.12.10.198:5000` | Check laptop firewall (port 5000), confirm laptop and ESP32 are on the same network/subnet |
| Dashboard shows blank/error | Server not running, or wrong IP typed in browser | Confirm `python3 server.py` is running and note the exact IP it prints |
| CSV has duplicate rows for the same student same day | Should not happen with default logic — if it does, check that both node clocks/server weren't restarted mid-test (restarting the server clears `attendance_recorded` in memory) | Restart server only between demo sessions, not during one |

## 14. Final deployment checklist

- [ ] Both `.ino` files flashed, each printing its own Wi-Fi IP over Serial
- [ ] Both boards' LEDs wired per Section 3
- [ ] Flask server running (`python3 server.py`) on the laptop at 11.12.10.198:5000
- [ ] Dashboard loads and both nodes show **online**
- [ ] Both phones configured in nRF Connect with correct Local Names
- [ ] `data/attendance.csv` exists with header row (pre-created for you)
- [ ] Full walk-through test (Section 11–12) completed at least once before the actual demo
- [ ] Fallback hotspot credentials ready in case college Wi-Fi has client isolation (Section 5)

## 15. Viva explanation — how the complete system works

**End-to-end flow:** A phone advertises a fixed BLE "Local Name"
(`ATTEND-KAVIN` / `ATTEND-LOHITH`) via nRF Connect. Both ESP32 nodes
independently run BLE scans and, whenever they see one of these
registered names, record its RSSI and immediately report it to the
laptop over HTTP (Wi-Fi). The laptop's Flask server timestamps every
observation itself (not the ESP32's clock, avoiding sync issues),
keeps a rolling window of detections per device per node, and applies
a simple rule: **if both nodes have each seen the device at least 3
times in the last 30 seconds, the device is verified present.** RSSI
is recorded and displayed for reference but does **not** gate the
decision by default — it's supporting evidence, since RSSI is affected
by walls, orientation, and multipath and isn't a reliable distance
measurement without experimental calibration you haven't done yet.
The first time a device flips to "present," one row is written to
`attendance.csv`; further detections that day don't create duplicate
rows. A live dashboard polls the server every 2–4 seconds so the whole
process — node connectivity, live RSSI, and attendance — is visible in
real time.

**Why this isn't ML, majority voting, or identity verification:** the
decision is a fixed, human-readable rule (`count ≥ threshold within a
time window, from both nodes`), not a learned model. Majority voting
needs 3+ nodes to be meaningful — with exactly 2, "majority" would
just mean "both agree," which is already what `REQUIRE_BOTH_NODES_FOR_PRESENCE`
does directly and transparently. The system verifies that a
**registered device** is present, not the identity of whoever is
carrying it — if asked about phone-sharing, that's the honest, scoped
answer.
