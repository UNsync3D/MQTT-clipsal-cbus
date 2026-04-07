# MQTT Clipsal C-Bus

A Python bridge that connects a **Clipsal 5500SHAC** home automation controller to **Home Assistant** via MQTT, using the reverse-engineered WebSocket protocol.

All devices auto-register in Home Assistant using **MQTT Discovery** — no manual HA entity configuration required.

---

## Features

- **Full bidirectional control** of lights, fans, blinds, curtains, scenes, AC zones, motion sensors and alarms
- **AC climate entities** with mode (heat/cool/auto/off), fan speed, and target temperature
- **Real-time state updates** from C-Bus wall switches and sensors
- **MQTT Discovery** — devices appear automatically in Home Assistant
- **Auto-reconnect** on WebSocket or MQTT disconnection
- **Works on Home Assistant OS** (Raspberry Pi and other hardware)
- **Coexists with Homebridge** — both can run simultaneously

---

## Supported Device Types

| Type | HA Component | Notes |
|------|-------------|-------|
| Dimmable lights | `light` | Full brightness 0–255 |
| Fans | `switch` | On/Off |
| Blinds & curtains | `cover` | Position 0–255, device_class: blind or curtain |
| Scenes | `button` | Press to trigger |
| Air conditioning | `climate` | Mode, fan speed, temperature setpoint |
| Motion sensors | `binary_sensor` | Class: motion |
| Smoke alarm | `binary_sensor` | Class: smoke |
| Gas alarm | `binary_sensor` | Class: gas |

---

## Requirements

- Python 3.10+
- Clipsal 5500SHAC on your local network
- MQTT broker (e.g. Mosquitto)
- Home Assistant (recommended)

### Python dependencies

```
aiohttp>=3.9
websockets>=12.0
paho-mqtt>=1.6
pyyaml>=6.0
```

Install with:

```bash
pip install -r requirements.txt
```

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/mqtt_clipsal_cbus.git
cd mqtt_clipsal_cbus
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Edit config.yaml

Open `config.yaml` and update to match your installation:

```yaml
cbus:
  host: "192.168.1.250"    # Your 5500SHAC IP address
  port: 8087

mqtt:
  host: "localhost"        # Your MQTT broker IP
  port: 1883
  username: ""             # Leave blank if no auth
  password: ""
```

Then configure your devices under `lights`, `fans`, `covers`, `scenes`, `ac_zones`, `motion_sensors` and `alarms`.

### 4. Run the bridge

```bash
python3 mqtt_clipsal_cbus.py config.yaml
```

You should see:

```
MQTT Clipsal C-Bus Bridge starting...
MQTT connected (rc=0)
C-Bus auth token: ''
WebSocket connected to C-Bus.
<- C-Bus Living Room = 255
```

Devices will appear automatically in Home Assistant under:
**Settings → Devices & Services → MQTT → Devices**

---

## Configuration Reference

### C-Bus Application Numbers

| Application | Number | Usage |
|-------------|--------|-------|
| Lighting | 56 | Lights, fans, blinds, motion sensors |
| Coolmaster (AC) | 48 | Air conditioning power, mode, fan |
| Trigger Control | 202 | Scenes |
| User Parameter | 250 | AC temperatures (integer °C) |
| Security | 208 | Smoke/gas alarms |

### Address Encoding

```
Standard apps:    address = (app << 24) | (network << 16) | (group << 8)
User Parameter:   address = (250 << 24) | group   ← no group shift
```

### AC Zone Configuration

Each AC zone requires seven C-Bus group numbers:

```yaml
ac_zones:
  - id: living              # Unique ID (no spaces)
    name: "Living Room"
    power_group: 8          # App 48 — power on/off (0=off, 255=on)
    mode_group: 11          # App 48 — mode (0=off, 10=heat, 20=cool, 50=auto)
    fan_group: 10           # App 48 — fan (0=auto, 85=low, 170=med, 255=high)
    temp_up_group: 20       # App 48 — pulse to raise setpoint 1°C
    temp_down_group: 21     # App 48 — pulse to lower setpoint 1°C
    current_temp_group: 4   # App 250 — current temperature (read-only)
    set_temp_group: 33      # App 250 — target temperature (read-only)
```

---

## Running on Home Assistant OS

Home Assistant OS uses a locked-down Alpine Linux environment. The recommended approach is to run the bridge inside the **Terminal & SSH addon**.

### Step 1 — Install required addons

In HA: **Settings → Add-ons → Add-on Store**

- **Mosquitto broker** — install and start
- **Terminal & SSH** — install, set a password, enable Show in sidebar, start
- **File Editor** — install, enable Show in sidebar, start

### Step 2 — Copy files to the Pi

From your computer:

```bash
# Create directory
ssh root@<pi-ip> -p 22222 "mkdir -p /homeassistant/mqtt_clipsal_cbus"

# Copy files
scp -P 22222 mqtt_clipsal_cbus.py root@<pi-ip>:/homeassistant/mqtt_clipsal_cbus/
scp -P 22222 config.yaml root@<pi-ip>:/homeassistant/mqtt_clipsal_cbus/
scp -P 22222 start_bridge.sh root@<pi-ip>:/homeassistant/mqtt_clipsal_cbus/
```

### Step 3 — Install Python and dependencies

In the Terminal addon:

```bash
apk add python3 py3-pip
pip3 install --break-system-packages aiohttp websockets paho-mqtt pyyaml
chmod +x /homeassistant/mqtt_clipsal_cbus/start_bridge.sh
```

### Step 4 — Test the bridge

```bash
cd /homeassistant/mqtt_clipsal_cbus
python3 mqtt_clipsal_cbus.py config.yaml
```

### Step 5 — Run in background

```bash
nohup bash /homeassistant/mqtt_clipsal_cbus/start_bridge.sh \
  > /homeassistant/mqtt_clipsal_cbus/bridge.log 2>&1 &
```

### Step 6 — Auto-start on HA boot

Add to `/config/configuration.yaml`:

```yaml
shell_command:
  start_cbus_bridge: "bash /homeassistant/mqtt_clipsal_cbus/start_bridge.sh"
```

Create an automation (**Settings → Automations → YAML mode**):

```yaml
alias: Start C-Bus Bridge on Boot
description: Starts the MQTT Clipsal C-Bus bridge when HA starts
trigger:
  - platform: homeassistant
    event: start
condition: []
action:
  - delay: "00:00:30"
  - service: shell_command.start_cbus_bridge
mode: single
```

Restart HA: **Settings → System → Restart**

> **Note:** On HA OS, Python packages are lost when the Terminal & SSH addon restarts (e.g. after a full reboot). The `start_bridge.sh` script reinstalls them automatically each time.

---

## MQTT Topics

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `cbus/{id}/state` | Bridge → HA | Current device state |
| `cbus/{id}/set` | HA → Bridge | Command |
| `cbus/bridge/availability` | Bridge → HA | `online` / `offline` |

### Light payload

```json
// State published by bridge
{"state": "ON", "brightness": 200}

// Command from HA
{"state": "OFF"}
{"state": "ON", "brightness": 128}
```

### Climate command

HA sends individual JSON keys per control:

```json
{"mode": "cool"}
{"fan_mode": "high"}
{"temperature": 23}
```

---

## Protocol Notes

The Clipsal 5500SHAC WebSocket protocol was reverse-engineered from the unit's built-in `cbuslib.js`.

**Important:** The 5500SHAC returns an **HTML page** (not pure JSON) from the initial HTTP POST. The objects list is embedded as JavaScript data within the HTML. This bridge correctly parses the embedded JSON, extracting object addresses (field name `id`) and values (nested in `datadec`).

**Auth token** — obtained via HTTP POST:

```
POST http://<host>:<port>/scada-vis/objects/ws
Content-Type: application/x-www-form-urlencoded
Body: updatetime=<unix_timestamp>
```

**WebSocket URL:**

```
ws://<host>:<port>/scada-vis/objects/ws?auth=<token>
```

**Incoming event format:**

```json
{
  "type": "groupwrite",
  "dst": "0/56/1",
  "dstraw": 939524352,
  "datahex": "FF000000",
  "sender": "local",
  "time": 1775194459
}
```

**Outgoing command format:**

```json
{
  "address": 939524352,
  "datatype": 5,
  "value": 255,
  "type": "text",
  "update": false,
  "action": "write"
}
```

**Keepalive:** Send the string `"ping"` every 10 seconds; the unit responds with `"pong"`.

---

## Troubleshooting

| Symptom | Solution |
|---------|----------|
| `MQTT connected (rc=5)` | Wrong username/password in config.yaml |
| `MQTT connected (rc=1)` | Wrong broker hostname — use `core-mosquitto` for HA OS |
| `Failed to get C-Bus token` | Check 5500SHAC IP/port; verify reachable from bridge host |
| No devices in HA | Check MQTT integration is connected in HA |
| Devices show unavailable | Bridge not running — check bridge.log |
| AC temperature shows null | AC unit is off; updates when powered on |
| Bridge reconnecting in loop | Multiple instances running — `pkill -f mqtt_clipsal_cbus.py` |

---

## Contributing

Pull requests are welcome. Please open an issue first to discuss significant changes.

When contributing:
- Follow the existing code style
- Test with a real 5500SHAC if possible
- Update `config.yaml` comments if adding new device types

---

## License

MIT License

Copyright (c) 2024

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

---

## Acknowledgements

- Protocol reverse-engineered from Clipsal 5500SHAC `cbuslib.js`
- Built for Home Assistant with MQTT Discovery
- Tested on Home Assistant OS 17.1 on Raspberry Pi (aarch64)
# MQTT-clipsal-cbus
# MQTT-clipsal-cbus
# MQTT-clipsal-cbus
