# BLE Classroom Presence Verification System

A low-cost, multi-node classroom presence verification system built using ESP32, Bluetooth Low Energy (BLE), Wi-Fi, and a Python Flask server.

The system uses two ESP32 nodes placed at different observation points in a classroom. Each node scans for registered BLE devices, records detection observations and RSSI values, and sends the observations to a central Flask server running on a laptop.

The server combines observations from both ESP32 nodes and performs rule-based presence verification.

---

## Project Overview

### Goal

The goal of this project is to explore a low-cost, distributed architecture for verifying the presence of registered BLE devices in a classroom using multiple physical observation points.

Instead of relying on a single ESP32 receiver, the system uses two independent ESP32 nodes.

A device is considered verified when:

- The device is registered with the system.
- Node 1 detects the device at least 3 times.
- Node 2 detects the device at least 3 times.
- The required detections occur within the configured presence window.
- Both nodes contribute independent observations.

Once a student is verified during the current attendance session, the verification remains recorded even if the device temporarily disappears from the BLE scan.

---

## Architecture

```text
                    BLE Advertisement
                           │
                           ▼
                 ┌──────────────────┐
                 │ Student Phone    │
                 │                  │
                 │ ATTEND-KAVIN     │
                 │ ATTEND-LOHITH    │
                 └────────┬─────────┘
                          │
                     Bluetooth LE
                          │
              ┌───────────┴───────────┐
              │                       │
              ▼                       ▼
     ┌────────────────┐      ┌────────────────┐
     │    ESP32       │      │    ESP32       │
     │    NODE 1      │      │    NODE 2      │
     │                │      │                │
     │ BLE Scanner    │      │ BLE Scanner    │
     │ RSSI           │      │ RSSI           │
     └───────┬────────┘      └───────┬────────┘
             │                       │
             │        Wi-Fi          │
             └───────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ Laptop Flask Server  │
              │                      │
              │ Detection Collection │
              │ Node Tracking        │
              │ Verification Logic   │
              │ CSV Logging          │
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ Web Dashboard        │
              │                      │
              │ Node Status          │
              │ Live Detection       │
              │ Verification         │
              │ Attendance           │
              └──────────────────────┘
