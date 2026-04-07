#!/bin/bash
# start_bridge.sh — Starts the MQTT Clipsal C-Bus Bridge
# Installs Python dependencies if needed (required on HA OS after each addon restart)
# then runs the bridge.
#
# Usage: bash start_bridge.sh
# Or to run in background: nohup bash start_bridge.sh > bridge.log 2>&1 &

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$SCRIPT_DIR"

# Install dependencies (safe to run multiple times)
apk add --quiet python3 py3-pip 2>/dev/null || true
pip3 install --quiet --break-system-packages aiohttp websockets paho-mqtt pyyaml

python3 mqtt_clipsal_cbus.py config.yaml
