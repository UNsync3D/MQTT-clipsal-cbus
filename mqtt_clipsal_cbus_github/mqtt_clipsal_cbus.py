#!/usr/bin/env python3
"""
MQTT Clipsal C-Bus Bridge
Bridges a Clipsal 5500SHAC to Home Assistant via MQTT Discovery.

AC zones are exposed as full HA climate entities with bidirectional
control of power, mode, fan speed, and target temperature.

Compatible with the Clipsal 5500SHAC HTML/WebSocket protocol.
Configure devices in config.yaml before running.
"""

import asyncio
import json
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import paho.mqtt.client as mqtt
import websockets
import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("cbus_bridge")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Address helpers
# ---------------------------------------------------------------------------
def encode_standard(app: int, network: int, group: int) -> int:
    """Standard C-Bus address: (app<<24)|(net<<16)|(grp<<8)"""
    return (app << 24) | (network << 16) | (group << 8)


def encode_user_param(group: int) -> int:
    """User Parameter address (App 250): (250<<24)|group — no shift"""
    return (250 << 24) | group


def decode_value_hex(datahex: str, datatype: str = "int") -> float:
    """Decode the 4-byte hex payload from a groupwrite message."""
    raw = bytes.fromhex(datahex[:8])
    if datatype == "float":
        return struct.unpack(">f", raw)[0]
    return raw[0]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_LIGHTING    = 56
APP_COOLMASTER  = 48
APP_TRIGGER     = 202
APP_USER_PARAM  = 250
APP_SECURITY    = 208

# Coolmaster mode values (App 48, mode group)
AC_MODE_OFF   = 0
AC_MODE_HEAT  = 10
AC_MODE_COOL  = 20
AC_MODE_AUTO  = 50

# Coolmaster fan speed values (App 48, fan group)
AC_FAN_AUTO   = 0
AC_FAN_LOW    = 85
AC_FAN_MED    = 170
AC_FAN_HIGH   = 255

# HA <-> C-Bus mode mappings
HA_MODE_TO_CBUS = {
    "off":  AC_MODE_OFF,
    "heat": AC_MODE_HEAT,
    "cool": AC_MODE_COOL,
    "auto": AC_MODE_AUTO,
}
CBUS_MODE_TO_HA = {v: k for k, v in HA_MODE_TO_CBUS.items()}

# HA <-> C-Bus fan speed mappings
HA_FAN_TO_CBUS = {
    "auto":   AC_FAN_AUTO,
    "low":    AC_FAN_LOW,
    "medium": AC_FAN_MED,
    "high":   AC_FAN_HIGH,
}


def cbus_fan_to_ha(raw: int) -> str:
    """Map a raw Coolmaster fan value to an HA fan mode string."""
    if raw <= 0:
        return "auto"
    if raw <= 127:
        return "low"
    if raw <= 212:
        return "medium"
    return "high"


# ---------------------------------------------------------------------------
# Device dataclass
# ---------------------------------------------------------------------------
@dataclass
class Device:
    unique_id: str
    name: str
    component: str
    address: int
    app: int
    group: int
    network: int = 0
    device_class: Optional[str] = None
    unit_of_measurement: Optional[str] = None
    brightness: bool = False
    state: Optional[float] = None

    @property
    def state_topic(self) -> str:
        return f"cbus/{self.unique_id}/state"

    @property
    def command_topic(self) -> str:
        return f"cbus/{self.unique_id}/set"

    @property
    def availability_topic(self) -> str:
        return "cbus/bridge/availability"


# ---------------------------------------------------------------------------
# AC Zone
# ---------------------------------------------------------------------------
@dataclass
class ACZone:
    """
    One air-conditioning zone with full bidirectional climate control.

    C-Bus addresses:
      power_addr   — App 48, power group    (0=Off, 255=On)
      mode_addr    — App 48, mode group     (0=Off, 10=Heat, 20=Cool, 50=Auto)
      fan_addr     — App 48, fan group      (0=Auto, 85=Low, 170=Med, 255=High)
      temp_up_addr — App 48, temp-up group  (pulse 1 = raise setpoint 1°C)
      temp_dn_addr — App 48, temp-dn group  (pulse 1 = lower setpoint 1°C)
      cur_tmp_addr — App 250, current temp  (read-only integer °C)
      set_tmp_addr — App 250, setpoint      (read-only integer °C)
    """
    zone_id: str
    name: str
    network: int
    power_group: int
    mode_group: int
    fan_group: int
    temp_up_group: int
    temp_dn_group: int
    current_temp_group: int
    set_temp_group: int

    power_addr:   int = field(init=False)
    mode_addr:    int = field(init=False)
    fan_addr:     int = field(init=False)
    temp_up_addr: int = field(init=False)
    temp_dn_addr: int = field(init=False)
    cur_tmp_addr: int = field(init=False)
    set_tmp_addr: int = field(init=False)

    power:        Optional[int] = field(default=None, init=False)
    mode:         Optional[int] = field(default=None, init=False)
    fan:          Optional[int] = field(default=None, init=False)
    current_temp: Optional[int] = field(default=None, init=False)
    set_temp:     Optional[int] = field(default=None, init=False)

    def __post_init__(self):
        self.power_addr   = encode_standard(APP_COOLMASTER, self.network, self.power_group)
        self.mode_addr    = encode_standard(APP_COOLMASTER, self.network, self.mode_group)
        self.fan_addr     = encode_standard(APP_COOLMASTER, self.network, self.fan_group)
        self.temp_up_addr = encode_standard(APP_COOLMASTER, self.network, self.temp_up_group)
        self.temp_dn_addr = encode_standard(APP_COOLMASTER, self.network, self.temp_dn_group)
        self.cur_tmp_addr = encode_user_param(self.current_temp_group)
        self.set_tmp_addr = encode_user_param(self.set_temp_group)

    @property
    def unique_id(self) -> str:
        return f"ac_{self.zone_id}"

    @property
    def state_topic(self) -> str:
        return f"cbus/{self.unique_id}/state"

    @property
    def command_topic(self) -> str:
        return f"cbus/{self.unique_id}/set"

    @property
    def availability_topic(self) -> str:
        return "cbus/bridge/availability"

    @property
    def readable_addresses(self) -> dict[int, str]:
        return {
            self.power_addr:   "power",
            self.mode_addr:    "mode",
            self.fan_addr:     "fan",
            self.cur_tmp_addr: "current_temp",
            self.set_tmp_addr: "set_temp",
        }

    def ha_mode(self) -> str:
        if self.power == 0:
            return "off"
        return CBUS_MODE_TO_HA.get(self.mode or AC_MODE_AUTO, "auto")

    def ha_fan(self) -> str:
        return cbus_fan_to_ha(self.fan or 0)

    def state_payload(self) -> str:
        return json.dumps({
            "mode":                self.ha_mode(),
            "fan_mode":            self.ha_fan(),
            "current_temperature": self.current_temp,
            "temperature":         self.set_temp,
        })


# ---------------------------------------------------------------------------
# Device registry
# ---------------------------------------------------------------------------
def build_device_registry(cfg: dict) -> tuple[dict[int, Device], dict[int, ACZone]]:
    devices: dict[int, Device] = {}
    ac_addr_map: dict[int, ACZone] = {}
    network = cfg.get("cbus", {}).get("network", 0)

    def add(dev: Device):
        devices[dev.address] = dev

    for item in cfg.get("lights", []):
        grp = item["group"]
        add(Device(
            unique_id=f"light_{grp}", name=item["name"], component="light",
            address=encode_standard(APP_LIGHTING, network, grp),
            app=APP_LIGHTING, group=grp, network=network, brightness=True,
        ))

    for item in cfg.get("fans", []):
        grp = item["group"]
        add(Device(
            unique_id=f"fan_{grp}", name=item["name"], component="switch",
            address=encode_standard(APP_LIGHTING, network, grp),
            app=APP_LIGHTING, group=grp, network=network,
        ))

    for item in cfg.get("covers", []):
        grp = item["group"]
        add(Device(
            unique_id=f"cover_{grp}", name=item["name"], component="cover",
            address=encode_standard(APP_LIGHTING, network, grp),
            app=APP_LIGHTING, group=grp, network=network,
            device_class=item.get("device_class", "blind"),
        ))

    for item in cfg.get("scenes", []):
        grp = item["group"]
        add(Device(
            unique_id=f"scene_{grp}", name=item["name"], component="scene",
            address=encode_standard(APP_TRIGGER, network, grp),
            app=APP_TRIGGER, group=grp, network=network,
        ))

    for zone_cfg in cfg.get("ac_zones", []):
        zone = ACZone(
            zone_id=zone_cfg["id"],
            name=zone_cfg["name"],
            network=network,
            power_group=zone_cfg["power_group"],
            mode_group=zone_cfg["mode_group"],
            fan_group=zone_cfg["fan_group"],
            temp_up_group=zone_cfg["temp_up_group"],
            temp_dn_group=zone_cfg["temp_down_group"],
            current_temp_group=zone_cfg["current_temp_group"],
            set_temp_group=zone_cfg["set_temp_group"],
        )
        for addr in zone.readable_addresses:
            ac_addr_map[addr] = zone

    for item in cfg.get("motion_sensors", []):
        grp = item["group"]
        add(Device(
            unique_id=f"motion_{grp}", name=item["name"], component="binary_sensor",
            address=encode_standard(APP_LIGHTING, network, grp),
            app=APP_LIGHTING, group=grp, network=network, device_class="motion",
        ))

    for item in cfg.get("alarms", []):
        grp = item["group"]
        add(Device(
            unique_id=f"alarm_{grp}", name=item["name"], component="binary_sensor",
            address=encode_standard(APP_SECURITY, network, grp),
            app=APP_SECURITY, group=grp, network=network,
            device_class=item.get("device_class", "smoke"),
        ))

    return devices, ac_addr_map


# ---------------------------------------------------------------------------
# MQTT Discovery
# ---------------------------------------------------------------------------
HA_DEVICE_INFO = {
    "identifiers": ["clipsal_cbus_5500shac"],
    "name": "Clipsal C-Bus 5500SHAC",
    "manufacturer": "Clipsal",
    "model": "5500SHAC",
}


def publish_discovery(client: mqtt.Client, dev: Device, prefix: str = "homeassistant"):
    topic = f"{prefix}/{dev.component}/{dev.unique_id}/config"
    payload: dict = {
        "name": dev.name,
        "unique_id": dev.unique_id,
        "availability_topic": dev.availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": HA_DEVICE_INFO,
    }

    if dev.component == "light":
        payload.update({
            "schema": "json",
            "state_topic": dev.state_topic,
            "command_topic": dev.command_topic,
            "brightness": True,
            "brightness_scale": 255,
        })
    elif dev.component == "switch":
        payload.update({
            "state_topic": dev.state_topic,
            "command_topic": dev.command_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
        })
    elif dev.component == "cover":
        payload.update({
            "state_topic": dev.state_topic,
            "command_topic": dev.command_topic,
            "position_topic": dev.state_topic,
            "set_position_topic": dev.command_topic,
            "position_open": 255,
            "position_closed": 0,
            "device_class": dev.device_class or "blind",
            "payload_open": "OPEN",
            "payload_close": "CLOSE",
            "payload_stop": "STOP",
        })
    elif dev.component == "sensor":
        payload.update({
            "state_topic": dev.state_topic,
            "unit_of_measurement": dev.unit_of_measurement,
            "device_class": dev.device_class,
        })
    elif dev.component == "binary_sensor":
        payload.update({
            "state_topic": dev.state_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": dev.device_class,
        })
    elif dev.component == "scene":
        payload.update({
            "command_topic": dev.command_topic,
            "payload_press": "PRESS",
        })
        topic = f"{prefix}/button/{dev.unique_id}/config"

    client.publish(topic, json.dumps(payload), retain=True)
    log.debug("Discovery published: %s", topic)


def publish_ac_discovery(client: mqtt.Client, zone: ACZone, prefix: str = "homeassistant"):
    """Publish MQTT Discovery for a full HA climate entity."""
    topic = f"{prefix}/climate/{zone.unique_id}/config"
    payload = {
        "name": zone.name,
        "unique_id": zone.unique_id,
        "availability_topic": zone.availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": HA_DEVICE_INFO,
        "mode_state_topic":             zone.state_topic,
        "mode_state_template":          "{{ value_json.mode }}",
        "fan_mode_state_topic":         zone.state_topic,
        "fan_mode_state_template":      "{{ value_json.fan_mode }}",
        "current_temperature_topic":    zone.state_topic,
        "current_temperature_template": "{{ value_json.current_temperature }}",
        "temperature_state_topic":      zone.state_topic,
        "temperature_state_template":   "{{ value_json.temperature }}",
        "mode_command_topic":            zone.command_topic,
        "mode_command_template":         '{"mode": "{{ value }}"}',
        "fan_mode_command_topic":        zone.command_topic,
        "fan_mode_command_template":     '{"fan_mode": "{{ value }}"}',
        "temperature_command_topic":     zone.command_topic,
        "temperature_command_template":  '{"temperature": {{ value }}}',
        "modes":     ["off", "heat", "cool", "auto"],
        "fan_modes": ["auto", "low", "medium", "high"],
        "min_temp":        16,
        "max_temp":        30,
        "temp_step":       1,
        "temperature_unit": "C",
        "precision":       1.0,
    }
    client.publish(topic, json.dumps(payload), retain=True)
    log.info("AC Discovery published: %s  (%s)", topic, zone.name)


# ---------------------------------------------------------------------------
# C-Bus WebSocket client
# ---------------------------------------------------------------------------
class CBusClient:
    def __init__(self, cfg, devices, ac_zones, mqtt_client):
        self.host    = cfg["cbus"]["host"]
        self.port    = cfg["cbus"]["port"]
        self.network = cfg["cbus"].get("network", 0)
        self.devices  = devices
        self.ac_zones = ac_zones
        self.mqtt    = mqtt_client
        self._ws     = None
        self._token: Optional[str] = None
        self._command_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._unique_zones: dict[str, ACZone] = {}
        for zone in ac_zones.values():
            self._unique_zones[zone.zone_id] = zone

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}/scada-vis/objects/ws"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/scada-vis/objects/ws?auth={self._token}"

    async def _get_token(self) -> bool:
        """
        Obtain auth token and initial device states from the 5500SHAC.
        The unit returns an HTML page with JSON embedded in a script tag.
        We parse the JSON objects list directly from the raw HTML response.
        """
        ts = int(time.time())
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.http_url,
                    data=f"updatetime={ts}",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    text = await resp.text()

                    # Try clean JSON first, fall back to HTML extraction
                    try:
                        data = json.loads(text)
                        self._token = data.get("auth", "")
                    except Exception:
                        self._token = ""
                    log.info("C-Bus auth token: '%s'", self._token)

                    # Extract the objects list from HTML-embedded JSON
                    m = re.search(r'"objects"\s*:\s*(\[.*?\])\s*[,}]', text, re.DOTALL)
                    objects = json.loads(m.group(1)) if m else []

                    for obj in objects:
                        # 5500SHAC uses 'id' (not 'address') and 'datadec' for values
                        addr = obj.get("id")
                        datadec = obj.get("datadec")
                        val = datadec.get("level", datadec.get("value")) \
                            if isinstance(datadec, dict) else datadec
                        if addr is None or val is None:
                            continue
                        try:
                            val = float(val)
                        except (TypeError, ValueError):
                            continue
                        dev = self.devices.get(addr)
                        if dev:
                            self._publish_device_state(dev, float(val))
                            continue
                        zone = self.ac_zones.get(addr)
                        if zone:
                            self._update_zone_field(zone, addr, int(val))

                    for zone in self._unique_zones.values():
                        self._publish_zone_state(zone)
                    return True

        except Exception as e:
            log.error("Failed to get C-Bus token: %s", e)
            return False

    def _publish_device_state(self, dev: Device, raw_value: float):
        dev.state = raw_value
        if dev.component == "light":
            brightness = int(round(raw_value))
            self.mqtt.publish(
                dev.state_topic,
                json.dumps({"state": "ON" if brightness > 0 else "OFF", "brightness": brightness}),
                retain=True,
            )
        elif dev.component == "switch":
            self.mqtt.publish(dev.state_topic, "ON" if raw_value > 0 else "OFF", retain=True)
        elif dev.component == "cover":
            self.mqtt.publish(dev.state_topic, str(int(round(raw_value))), retain=True)
        elif dev.component == "sensor":
            self.mqtt.publish(dev.state_topic, str(int(round(raw_value))), retain=True)
        elif dev.component == "binary_sensor":
            self.mqtt.publish(dev.state_topic, "ON" if raw_value > 0 else "OFF", retain=True)

    def _update_zone_field(self, zone: ACZone, addr: int, value: int):
        """Map an incoming C-Bus address to the correct ACZone field."""
        field_name = zone.readable_addresses.get(addr)
        if field_name:
            setattr(zone, field_name, value)
            log.debug("AC '%s'.%s <- %s", zone.name, field_name, value)

    def _publish_zone_state(self, zone: ACZone):
        payload = zone.state_payload()
        self.mqtt.publish(zone.state_topic, payload, retain=True)
        log.info("AC state -> %s : %s", zone.name, payload)

    async def _handle_message(self, raw: str):
        if raw == "pong":
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("type") != "groupwrite":
            return
        addr    = msg.get("dstraw")
        datahex = msg.get("datahex", "00000000")
        value   = int(decode_value_hex(datahex, "int"))
        dev = self.devices.get(addr)
        if dev:
            log.info("<- C-Bus %s (%s) = %s", dev.name, hex(addr), value)
            self._publish_device_state(dev, float(value))
            return
        zone = self.ac_zones.get(addr)
        if zone:
            log.info("<- C-Bus AC '%s' addr=%s field=%s val=%s",
                     zone.name, hex(addr),
                     zone.readable_addresses.get(addr, "?"), value)
            self._update_zone_field(zone, addr, value)
            self._publish_zone_state(zone)

    async def send_command(self, address: int, value: int):
        await self._command_queue.put({"address": address, "value": value})

    async def send_temp_pulses(self, zone: ACZone, target: int):
        """Adjust AC setpoint by pulsing Temp Up/Down groups (1 pulse = 1°C)."""
        current = zone.set_temp
        if current is None:
            log.warning("AC '%s': set_temp unknown - cannot pulse to %d C", zone.name, target)
            return
        delta = target - current
        if delta == 0:
            return
        addr      = zone.temp_up_addr if delta > 0 else zone.temp_dn_addr
        pulses    = abs(delta)
        direction = "UP" if delta > 0 else "DOWN"
        log.info("AC '%s': %d pulse(s) %s  (%d -> %d C)", zone.name, pulses, direction, current, target)
        for i in range(pulses):
            await self.send_command(addr, 1)
            if i < pulses - 1:
                await asyncio.sleep(0.15)

    async def _run_ping(self):
        while self._running:
            await asyncio.sleep(10)
            if self._ws:
                try:
                    await self._ws.send("ping")
                except Exception:
                    pass

    async def run(self):
        self._running = True
        asyncio.create_task(self._run_ping())
        while self._running:
            if not await self._get_token():
                log.warning("Retrying token in 30s...")
                await asyncio.sleep(30)
                continue
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self.mqtt.publish("cbus/bridge/availability", "online", retain=True)
                    log.info("WebSocket connected to C-Bus.")

                    async def sender():
                        while True:
                            cmd = await self._command_queue.get()
                            cbus_payload = {
                                "address": cmd["address"],
                                "datatype": 5,
                                "value": cmd["value"],
                                "type": "text",
                                "update": False,
                                "action": "write",
                            }
                            await ws.send(json.dumps(cbus_payload))
                            log.info("-> C-Bus addr=%s val=%s", cmd["address"], cmd["value"])

                    send_task = asyncio.create_task(sender())
                    async for message in ws:
                        await self._handle_message(message)
                    send_task.cancel()

            except Exception as e:
                log.error("WebSocket error: %s - reconnecting in 10s", e)
                self._ws = None
                self.mqtt.publish("cbus/bridge/availability", "offline", retain=True)
                await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# MQTT command handler
# ---------------------------------------------------------------------------
class CommandHandler:
    def __init__(self, devices, ac_zones, cbus):
        self._cmd_map = {dev.command_topic: dev for dev in devices.values()}
        self._ac_cmd_map: dict[str, ACZone] = {}
        self._cbus = cbus
        seen: set[str] = set()
        for zone in ac_zones.values():
            if zone.zone_id not in seen:
                self._ac_cmd_map[zone.command_topic] = zone
                seen.add(zone.zone_id)

    def get_subscriptions(self) -> list[str]:
        return list(self._cmd_map.keys()) + list(self._ac_cmd_map.keys())

    async def handle(self, topic: str, payload: str):
        dev = self._cmd_map.get(topic)
        if dev:
            await self._handle_device(dev, payload)
            return
        zone = self._ac_cmd_map.get(topic)
        if zone:
            await self._handle_ac(zone, payload)

    async def _handle_device(self, dev: Device, payload: str):
        log.info("MQTT cmd -> %s : %s", dev.name, payload)
        if dev.component == "light":
            try:
                data  = json.loads(payload)
                value = 0 if data.get("state") == "OFF" else int(data.get("brightness", 255))
            except (json.JSONDecodeError, TypeError):
                value = 255 if payload.strip().upper() == "ON" else 0
            await self._cbus.send_command(dev.address, value)
        elif dev.component == "switch":
            value = 255 if payload.strip().upper() == "ON" else 0
            await self._cbus.send_command(dev.address, value)
        elif dev.component == "cover":
            p = payload.strip().upper()
            if p == "OPEN":
                value = 255
            elif p == "CLOSE":
                value = 0
            elif p == "STOP":
                value = int(dev.state) if dev.state is not None else 128
            else:
                try:
                    value = int(payload)
                except ValueError:
                    return
            await self._cbus.send_command(dev.address, value)
        elif dev.component == "scene":
            await self._cbus.send_command(dev.address, 255)

    async def _handle_ac(self, zone: ACZone, payload: str):
        """
        Handle HA climate commands. HA sends individual JSON keys:
          {"mode": "cool"} | {"fan_mode": "high"} | {"temperature": 23}
        """
        log.info("MQTT AC cmd -> %s : %s", zone.name, payload)
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("AC '%s': invalid JSON '%s'", zone.name, payload)
            return

        if "mode" in data:
            ha_mode = data["mode"].lower()
            if ha_mode == "off":
                await self._cbus.send_command(zone.power_addr, 0)
                zone.power = 0
                log.info("AC '%s': power OFF", zone.name)
            else:
                cbus_mode = HA_MODE_TO_CBUS.get(ha_mode)
                if cbus_mode is None:
                    log.warning("AC '%s': unknown mode '%s'", zone.name, ha_mode)
                else:
                    if zone.power != 255:
                        await self._cbus.send_command(zone.power_addr, 255)
                        zone.power = 255
                        await asyncio.sleep(0.1)
                    await self._cbus.send_command(zone.mode_addr, cbus_mode)
                    zone.mode = cbus_mode
                    log.info("AC '%s': mode -> %s (C-Bus %s)", zone.name, ha_mode, cbus_mode)

        if "fan_mode" in data:
            ha_fan   = data["fan_mode"].lower()
            cbus_fan = HA_FAN_TO_CBUS.get(ha_fan)
            if cbus_fan is None:
                log.warning("AC '%s': unknown fan mode '%s'", zone.name, ha_fan)
            else:
                await self._cbus.send_command(zone.fan_addr, cbus_fan)
                zone.fan = cbus_fan
                log.info("AC '%s': fan -> %s (C-Bus %s)", zone.name, ha_fan, cbus_fan)

        if "temperature" in data:
            try:
                target = int(round(float(data["temperature"])))
                target = max(16, min(30, target))
                await self._cbus.send_temp_pulses(zone, target)
                zone.set_temp = target
            except (ValueError, TypeError):
                log.warning("AC '%s': invalid temperature '%s'", zone.name, data["temperature"])

        self._cbus._publish_zone_state(zone)


# ---------------------------------------------------------------------------
# Bridge orchestrator
# ---------------------------------------------------------------------------
class Bridge:
    def __init__(self, config_path: str = "config.yaml"):
        self.cfg                    = load_config(config_path)
        self.devices, self.ac_zones = build_device_registry(self.cfg)
        self._loop                  = asyncio.get_event_loop()
        self._mqtt_cmd_queue: asyncio.Queue = asyncio.Queue()

        mc = self.cfg["mqtt"]
        self.mqtt = mqtt.Client(client_id=mc.get("client_id", "mqtt_clipsal_cbus"))
        if mc.get("username"):
            self.mqtt.username_pw_set(mc["username"], mc.get("password", ""))
        self.mqtt.will_set("cbus/bridge/availability", "offline", retain=True)
        self.mqtt.on_connect = self._on_mqtt_connect
        self.mqtt.on_message = self._on_mqtt_message
        self.mqtt.connect(mc["host"], mc.get("port", 1883), 60)
        self.mqtt.loop_start()

        self.cbus    = CBusClient(self.cfg, self.devices, self.ac_zones, self.mqtt)
        self.handler = CommandHandler(self.devices, self.ac_zones, self.cbus)

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        log.info("MQTT connected (rc=%s)", rc)
        prefix = self.cfg.get("homeassistant", {}).get("discovery_prefix", "homeassistant")
        for dev in self.devices.values():
            publish_discovery(client, dev, prefix)
        seen: set[str] = set()
        for zone in self.ac_zones.values():
            if zone.zone_id not in seen:
                publish_ac_discovery(client, zone, prefix)
                seen.add(zone.zone_id)
        for topic in self.handler.get_subscriptions():
            client.subscribe(topic)
            log.debug("Subscribed: %s", topic)

    def _on_mqtt_message(self, client, userdata, msg):
        topic   = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace")
        self._loop.call_soon_threadsafe(
            self._mqtt_cmd_queue.put_nowait, (topic, payload)
        )

    async def _process_mqtt_commands(self):
        while True:
            topic, payload = await self._mqtt_cmd_queue.get()
            await self.handler.handle(topic, payload)

    async def run(self):
        log.info("MQTT Clipsal C-Bus Bridge starting...")
        await asyncio.gather(
            self.cbus.run(),
            self._process_mqtt_commands(),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    bridge = Bridge(cfg_path)
    asyncio.run(bridge.run())
