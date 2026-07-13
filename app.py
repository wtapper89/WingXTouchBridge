#!/usr/bin/env python3
import json
import os
import socket
import struct
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import mido


APP_NAME = "Wing X-Touch Bridge"
DEFAULT_CONFIG_PATH = Path("config.json")
CONFIG_PATH = Path(os.environ.get("WXB_CONFIG", DEFAULT_CONFIG_PATH))

DEFAULT_CONFIG = {
    "web_host": "0.0.0.0",
    "web_port": 8088,
    "wing_host": "192.168.1.100",
    "wing_port": 2223,
    "wing_tcp_port": 2222,
    "midi_input": "",
    "midi_output": "",
    "midi_device_hint": "X-TOUCH",
    "fader_mode": "absolute",
    "fader": {
        "min_db": -144.0,
        "zero_db_position": 0.731,
        "max_db": 10.0,
    },
    "xtouch": {
        "surface_mode": "ctrl",
        "color_mode": "mcu-72",
        "meter_mode": "mcu-packed-aftertouch",
        "meter_gain_db": 12.0,
    },
    "master_fader": {
        "enabled": True,
        "type": "main",
        "number": 1,
    },
    "log_midi": False,
    "osc": {
        "fader_path": "/ch/{channel}/fdr",
        "mute_path": "/ch/{channel}/mute",
        "solo_path": "/ch/{channel}/$solo",
        "select_path": "",
        "meter_path": "/ch/{channel}/lvl",
        "mute_on_value": 1,
        "mute_off_value": 0,
        "solo_on_value": 1,
        "solo_off_value": 0,
        "select_on_value": 1,
        "select_off_value": 0,
        "source_name_path": "/ch/{channel}/$name",
        "source_color_path": "/ch/{channel}/col",
    },
    "source_scan": {
        "first_channel": 1,
        "last_channel": 32,
        "timeout_ms": 120,
        "transport": "auto",
    },
    "strips": [
        {
            "xtouch": index,
            "wing_channel": index,
            "name": "",
            "color": "",
            "override_name": False,
            "override_color": False,
            "enabled": True,
        }
        for index in range(1, 9)
    ],
}

XTOUCH_COLORS = {
    "off": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}

COLOR_NAMES = list(XTOUCH_COLORS.keys())
WING_COLOR_TO_XTOUCH = {
    1: "blue",
    2: "blue",
    3: "blue",
    4: "cyan",
    5: "green",
    6: "green",
    7: "yellow",
    8: "yellow",
    9: "red",
    10: "red",
    11: "magenta",
    12: "magenta",
    13: "yellow",
    14: "cyan",
    15: "red",
    16: "cyan",
    17: "off",
    18: "white",
}
FADER_MIN_DB = -144.0
FADER_MAX_DB = 10.0
MASTER_TARGET_RANGES = {
    "main": (1, 4),
    "mtx": (1, 8),
    "aux": (1, 8),
    "dca": (1, 16),
}


def deep_merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def normalize_config(config):
    normalized = deep_merge(DEFAULT_CONFIG, config)
    default_by_strip = {strip["xtouch"]: strip for strip in DEFAULT_CONFIG["strips"]}
    strips = []
    for index, strip in enumerate(normalized.get("strips", []), start=1):
        xtouch = int(strip.get("xtouch", index))
        merged = deepcopy(default_by_strip.get(xtouch, {}))
        merged.update(strip)
        merged["xtouch"] = xtouch
        merged["wing_channel"] = int(merged.get("wing_channel", xtouch) or 0)
        merged["enabled"] = bool(merged.get("enabled", True)) and merged["wing_channel"] > 0
        merged["override_name"] = bool(merged.get("override_name", False))
        merged["override_color"] = bool(merged.get("override_color", False))
        strips.append(merged)
    normalized["strips"] = strips
    master = normalized.get("master_fader", {})
    master_type = str(master.get("type", "main")).lower()
    if master_type not in MASTER_TARGET_RANGES:
        master_type = "main"
    minimum, maximum = MASTER_TARGET_RANGES[master_type]
    master_number = max(minimum, min(maximum, int(master.get("number", 1) or 1)))
    normalized["master_fader"] = {
        "enabled": bool(master.get("enabled", True)),
        "type": master_type,
        "number": master_number,
    }
    return normalized


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.config = self.load()

    def load(self):
        if not self.path.exists():
            return deepcopy(DEFAULT_CONFIG)
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return normalize_config(data)

    def get(self):
        with self.lock:
            return deepcopy(self.config)

    def save(self, next_config):
        with self.lock:
            normalized = normalize_config(next_config)
            if not str(normalized.get("wing_host", "")).strip():
                raise ValueError("WING IP cannot be empty")
            for key, label in (("wing_port", "WING OSC port"), ("wing_tcp_port", "WING TCP port")):
                port = int(normalized.get(key, 0))
                if not 1 <= port <= 65535:
                    raise ValueError(f"{label} must be between 1 and 65535")
            zero_position = float(normalized.get("fader", {}).get("zero_db_position", 0))
            if not 0.05 <= zero_position <= 0.98:
                raise ValueError("Physical 0 dB position must be between 0.05 and 0.98")
            self.config = normalized
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(self.config, handle, indent=2)
                handle.write("\n")
            tmp_path.replace(self.path)
            return deepcopy(self.config)


class OscClient:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @staticmethod
    def _pad(value):
        raw = value.encode("utf-8") + b"\0"
        return raw + (b"\0" * ((4 - len(raw) % 4) % 4))

    @staticmethod
    def _arg_tag_and_bytes(value):
        if isinstance(value, bool):
            return "i", struct.pack(">i", 1 if value else 0)
        if isinstance(value, int):
            return "i", struct.pack(">i", value)
        if isinstance(value, float):
            return "f", struct.pack(">f", value)
        return "s", OscClient._pad(str(value))

    @staticmethod
    def packet(path, *args):
        tags = []
        payload = []
        for arg in args:
            tag, raw = OscClient._arg_tag_and_bytes(arg)
            tags.append(tag)
            payload.append(raw)
        return OscClient._pad(path) + OscClient._pad("," + "".join(tags)) + b"".join(payload)

    def send(self, host, port, path, *args):
        if not path:
            return
        self.sock.sendto(self.packet(path, *args), (host, int(port)))

    @staticmethod
    def decode(packet):
        def read_padded_string(offset):
            end = packet.index(b"\0", offset)
            value = packet[offset:end].decode("utf-8", errors="replace")
            next_offset = end + 1
            next_offset += (4 - next_offset % 4) % 4
            return value, next_offset

        address, offset = read_padded_string(0)
        tags, offset = read_padded_string(offset)
        args = []
        for tag in tags.lstrip(","):
            if tag == "i":
                args.append(struct.unpack(">i", packet[offset : offset + 4])[0])
                offset += 4
            elif tag == "f":
                args.append(struct.unpack(">f", packet[offset : offset + 4])[0])
                offset += 4
            elif tag == "s":
                value, offset = read_padded_string(offset)
                args.append(value)
            elif tag == "r":
                red, green, blue, alpha = packet[offset : offset + 4]
                args.append({"r": red, "g": green, "b": blue, "a": alpha / 255.0})
                offset += 4
            elif tag in ("T", "F"):
                args.append(tag == "T")
        return address, args

    def request_many(self, host, port, paths, timeout_ms=120):
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_sock.bind(("0.0.0.0", 0))
        recv_sock.settimeout(max(timeout_ms, 20) / 1000.0)
        local_port = recv_sock.getsockname()[1]
        try:
            for path in paths:
                packet = self.packet(path)
                recv_sock.sendto(packet, (host, int(port)))
            deadline = time.time() + (max(timeout_ms, 20) / 1000.0)
            replies = {}
            while time.time() < deadline:
                try:
                    data, _addr = recv_sock.recvfrom(4096)
                except socket.timeout:
                    break
                try:
                    address, args = self.decode(data)
                    replies[address] = args
                except Exception:
                    continue
            return replies
        finally:
            recv_sock.close()

    def request_many_tcp(self, host, port, paths, timeout_ms=250):
        replies = {}
        timeout = max(timeout_ms, 50) / 1000.0
        try:
            with socket.create_connection((host, int(port)), timeout=timeout) as sock:
                sock.settimeout(timeout)
                for path in paths:
                    packet = self.packet(path)
                    sock.sendall(struct.pack(">i", len(packet)) + packet)
                deadline = time.time() + timeout
                buffer = b""
                while time.time() < deadline:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    while len(buffer) >= 4:
                        size = struct.unpack(">i", buffer[:4])[0]
                        if size <= 0 or size > 65535:
                            break
                        if len(buffer) < size + 4:
                            break
                        packet = buffer[4 : size + 4]
                        buffer = buffer[size + 4 :]
                        try:
                            address, args = self.decode(packet)
                            replies[address] = args
                        except Exception:
                            continue
        except Exception:
            return replies
        return replies


class Bridge:
    def __init__(self, store):
        self.store = store
        self.osc = OscClient()
        self.running = threading.Event()
        self.running.set()
        self.midi_thread = None
        self.wing_thread = None
        self.meter_thread = None
        self.reload_event = threading.Event()
        self.wing_reload_event = threading.Event()
        self.meter_reload_event = threading.Event()
        self.wing_sock = None
        self.wing_sock_lock = threading.Lock()
        self.midi_out = None
        self.midi_out_lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.status = {
            "started_at": time.time(),
            "midi_input": "",
            "midi_output": "",
            "last_midi": "",
            "last_osc": "",
            "last_error": "",
            "messages": 0,
            "meter_messages": 0,
            "meter_local_port": "",
            "wing_local_port": "",
            "last_wing": "",
            "last_meter": "",
        }
        self.button_states = {}
        self.button_state_lock = threading.Lock()
        self.sources_lock = threading.Lock()
        self.sources_cache = []
        self.sources_at = 0

    def update_status(self, **values):
        with self.status_lock:
            self.status.update(values)

    def get_status(self):
        with self.status_lock:
            return deepcopy(self.status)

    def start(self):
        self.midi_thread = threading.Thread(target=self._midi_loop, daemon=True)
        self.midi_thread.start()
        self.wing_thread = threading.Thread(target=self._wing_loop, daemon=True)
        self.wing_thread.start()
        self.meter_thread = threading.Thread(target=self._meter_loop, daemon=True)
        self.meter_thread.start()

    def stop(self):
        self.running.clear()
        self.reload_event.set()
        self.wing_reload_event.set()
        self.meter_reload_event.set()

    def reload(self):
        self.reload_event.set()
        self.wing_reload_event.set()
        self.meter_reload_event.set()

    @staticmethod
    def _safe_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normal_color(value):
        if value is None or value == "":
            return "off"
        if isinstance(value, (int, float)):
            return WING_COLOR_TO_XTOUCH.get(int(value), COLOR_NAMES[int(value) % len(COLOR_NAMES)])
        if isinstance(value, dict):
            red = int(value.get("r", 0))
            green = int(value.get("g", 0))
            blue = int(value.get("b", 0))
            if max(red, green, blue) < 48:
                return "off"
            if red > 170 and green > 170 and blue > 170:
                return "white"
            if red >= green and red >= blue:
                return "yellow" if green > 120 else ("magenta" if blue > 120 else "red")
            if green >= red and green >= blue:
                return "cyan" if blue > 120 else "green"
            return "blue"
        text = str(value).strip().lower()
        if text in XTOUCH_COLORS:
            return text
        if text.isdigit():
            number = int(text)
            return WING_COLOR_TO_XTOUCH.get(number, COLOR_NAMES[number % len(COLOR_NAMES)])
        if text.startswith("#"):
            hex_value = text.lstrip("#")
            if len(hex_value) >= 6:
                try:
                    red = int(hex_value[0:2], 16)
                    green = int(hex_value[2:4], 16)
                    blue = int(hex_value[4:6], 16)
                except ValueError:
                    return "white"
                if max(red, green, blue) < 48:
                    return "off"
                if red > 170 and green > 170 and blue > 170:
                    return "white"
                if red >= green and red >= blue:
                    return "yellow" if green > 120 else ("magenta" if blue > 120 else "red")
                if green >= red and green >= blue:
                    return "cyan" if blue > 120 else "green"
                return "blue"
        return "white"

    @staticmethod
    def _fader_config(config):
        fader = config.get("fader", {})
        min_db = float(fader.get("min_db", FADER_MIN_DB))
        max_db = float(fader.get("max_db", FADER_MAX_DB))
        zero_pos = float(fader.get("zero_db_position", 0.731))
        zero_pos = max(0.05, min(0.98, zero_pos))
        return min_db, zero_pos, max_db

    def _norm_to_db(self, value, config=None):
        config = config or self.store.get()
        min_db, zero_pos, max_db = self._fader_config(config)
        value = max(0.0, min(1.0, float(value)))
        if value <= 0.001:
            return min_db
        if value <= zero_pos:
            return min_db + (value / zero_pos) * (0 - min_db)
        return ((value - zero_pos) / (1.0 - zero_pos)) * max_db

    def _db_to_norm(self, value, config=None):
        config = config or self.store.get()
        min_db, zero_pos, max_db = self._fader_config(config)
        try:
            db = float(value)
        except (TypeError, ValueError):
            db = min_db
        db = max(min_db, min(max_db, db))
        if db <= 0:
            return ((db - min_db) / (0 - min_db)) * zero_pos
        return zero_pos + (db / max_db) * (1.0 - zero_pos)

    @staticmethod
    def midi_devices():
        return {"inputs": mido.get_input_names(), "outputs": mido.get_output_names()}

    def _fallback_sources(self, config):
        scan = config.get("source_scan", {})
        first = self._safe_int(scan.get("first_channel"), 1)
        last = self._safe_int(scan.get("last_channel"), 48)
        return [
            {"channel": channel, "name": f"Channel {channel}", "color": "white", "live": False}
            for channel in range(first, last + 1)
        ]

    def discover_sources(self, refresh=False):
        config = self.store.get()
        with self.sources_lock:
            if self.sources_cache and not refresh and time.time() - self.sources_at < 30:
                return deepcopy(self.sources_cache)

        scan = config.get("source_scan", {})
        first = self._safe_int(scan.get("first_channel"), 1)
        last = self._safe_int(scan.get("last_channel"), 48)
        timeout_ms = self._safe_int(scan.get("timeout_ms"), 120)
        transport = str(scan.get("transport", "auto")).lower()
        osc_config = config.get("osc", {})
        requests = []
        expected = {}
        for channel in range(first, last + 1):
            for kind, template in (("name", osc_config.get("source_name_path", "")), ("color", osc_config.get("source_color_path", ""))):
                path = self._format_path(template, channel)
                if path:
                    requests.append(path)
                    expected[path] = (channel, kind)

        found = {source["channel"]: source for source in self._fallback_sources(config)}
        if requests:
            try:
                replies = {}
                if transport in ("auto", "udp"):
                    replies.update(self.osc.request_many(config["wing_host"], config["wing_port"], requests, timeout_ms=timeout_ms))
                if transport in ("auto", "tcp") and not replies:
                    replies.update(
                        self.osc.request_many_tcp(
                            config["wing_host"],
                            config.get("wing_tcp_port", 2222),
                            requests,
                            timeout_ms=max(timeout_ms, 250),
                        )
                    )
                for path, args in replies.items():
                    if path not in expected or not args:
                        continue
                    channel, kind = expected[path]
                    value = args[0]
                    found[channel]["live"] = True
                    if kind == "name" and str(value).strip():
                        found[channel]["name"] = str(value).strip()
                    elif kind == "color":
                        found[channel]["color"] = self._normal_color(value)
                self.update_status(sources_last_refresh=time.time(), sources_live=sum(1 for item in found.values() if item["live"]))
            except Exception as exc:
                self.update_status(last_error=f"WING source scan error: {exc}")

        sources = [found[channel] for channel in sorted(found)]
        with self.sources_lock:
            self.sources_cache = deepcopy(sources)
            self.sources_at = time.time()
        return sources

    @staticmethod
    def _choose_device(devices, requested, hint):
        if requested and requested in devices:
            return requested
        lowered_hint = (hint or "").lower()
        if lowered_hint:
            for device in devices:
                if lowered_hint in device.lower():
                    return device
        return devices[0] if devices else ""

    @staticmethod
    def _strip_for(config, xtouch_number):
        for strip in config.get("strips", []):
            if int(strip.get("xtouch", 0)) == xtouch_number:
                return strip
        return None

    @staticmethod
    def _format_path(template, channel):
        if not template:
            return ""
        return template.format(channel=int(channel))

    @staticmethod
    def _fit_lcd(text, width=7):
        clean = "".join(char if 32 <= ord(char) <= 126 else " " for char in str(text or ""))
        return clean[:width].ljust(width)

    def _source_for_channel(self, sources, channel):
        if int(channel or 0) <= 0:
            return {"channel": 0, "name": "None", "color": "off", "live": True}
        for source in sources:
            if int(source.get("channel", 0)) == int(channel):
                return source
        return {"channel": channel, "name": f"Channel {channel}", "color": "white", "live": False}

    def _strip_label_color(self, strip, sources):
        source = self._source_for_channel(sources, strip.get("wing_channel", 1))
        source_name = source.get("name") or f"Channel {strip.get('wing_channel', 1)}"
        source_color = source.get("color") or "white"
        label = strip.get("name") if strip.get("override_name") and strip.get("name") else source_name
        color = strip.get("color") if strip.get("override_color") and strip.get("color") else source_color
        return label, self._normal_color(color)

    def _send_lcd_text(self, midi_out, strip_number, top, bottom):
        if not midi_out:
            return
        top_offset = (int(strip_number) - 1) * 7
        bottom_offset = 56 + top_offset
        manufacturer = [0x00, 0x00, 0x66, 0x14, 0x12]
        for offset, text in ((top_offset, top), (bottom_offset, bottom)):
            data = manufacturer + [offset] + [ord(char) for char in self._fit_lcd(text)]
            midi_out.send(mido.Message("sysex", data=data))

    def _send_lcd_color(self, midi_out, strip_number, color, top="", bottom=""):
        if not midi_out:
            return
        mode = str(self.store.get().get("xtouch", {}).get("color_mode", "off")).lower()
        if mode == "off":
            return
        color_index = XTOUCH_COLORS.get(self._normal_color(color), XTOUCH_COLORS["white"])
        strip_index = max(0, int(strip_number) - 1)
        candidates = []
        if mode in ("mcu-72", "all"):
            candidates.append([0x00, 0x00, 0x66, 0x14, 0x72, strip_index, color_index])
        if mode in ("behringer-72", "all"):
            candidates.append([0x00, 0x20, 0x32, 0x14, 0x72, strip_index, color_index])
        if mode in ("behringer-4c", "all"):
            candidates.append([0x00, 0x20, 0x32, 0x15, 0x00, 0x4C, strip_index, color_index])
        # X-Touch MIDI implementation: device 0x14, LCD command 0x4C,
        # strip, color flags, then both seven-character display rows.
        text = self._fit_lcd(top) + self._fit_lcd(bottom)
        candidates.append(
            [0x00, 0x20, 0x32, 0x14, 0x4C, strip_index, color_index]
            + [ord(char) for char in text]
        )
        for data in candidates:
            try:
                midi_out.send(mido.Message("sysex", data=data))
            except Exception:
                continue

    def _send_motor_fader(self, xtouch, db_value):
        norm = self._db_to_norm(db_value)
        config = self.store.get()
        surface_mode = str(config.get("xtouch", {}).get("surface_mode", "mcu")).lower()
        with self.midi_out_lock:
            midi_out = self.midi_out
        if not midi_out:
            return
        try:
            if surface_mode == "ctrl":
                midi_out.send(
                    mido.Message(
                        "control_change",
                        channel=0,
                        control=69 + int(xtouch),
                        value=max(0, min(127, int(round(norm * 127)))),
                    )
                )
            else:
                pitch = int(round(norm * 16383)) - 8192
                pitch = max(-8192, min(8191, pitch))
                midi_out.send(mido.Message("pitchwheel", channel=int(xtouch) - 1, pitch=pitch))
        except Exception as exc:
            self.update_status(last_error=f"MIDI fader error: {exc}")

    @staticmethod
    def _button_note(kind, xtouch):
        starts = {"solo": 8, "mute": 16, "select": 24}
        return starts.get(kind, 0) + int(xtouch) - 1

    @staticmethod
    def _feedback_path(path):
        if not path or "/" not in path:
            return ""
        head, tail = path.rsplit("/", 1)
        if tail.startswith("$"):
            return path
        return f"{head}/${tail}"

    def _configured_paths(self, osc_config, kind, channel):
        path = self._format_path(osc_config.get(f"{kind}_path", ""), channel)
        paths = [path] if path else []
        feedback_path = self._feedback_path(path)
        if feedback_path and feedback_path not in paths:
            paths.append(feedback_path)
        return paths

    @staticmethod
    def _master_fader_path(config):
        target = config.get("master_fader", {})
        if not target.get("enabled", True):
            return ""
        target_type = str(target.get("type", "main")).lower()
        if target_type not in MASTER_TARGET_RANGES:
            return ""
        minimum, maximum = MASTER_TARGET_RANGES[target_type]
        number = max(minimum, min(maximum, int(target.get("number", 1) or 1)))
        return f"/{target_type}/{number}/fdr"

    @staticmethod
    def _fader_db(args):
        # WING query replies contain display text, normalized position, then dB.
        if len(args) >= 3:
            try:
                return float(args[2])
            except (TypeError, ValueError):
                pass
        return args[0] if args else FADER_MIN_DB

    def _set_button_state(self, kind, xtouch, enabled):
        state_key = f"{kind}:{int(xtouch)}"
        with self.button_state_lock:
            self.button_states[state_key] = bool(enabled)
        with self.midi_out_lock:
            midi_out = self.midi_out
        if midi_out:
            self._set_button_led(midi_out, self._button_note(kind, xtouch), enabled)

    def _known_button_state(self, kind, xtouch):
        state_key = f"{kind}:{int(xtouch)}"
        with self.button_state_lock:
            return self.button_states.get(state_key)

    @staticmethod
    def _is_on_value(kind, value, config):
        osc_config = config.get("osc", {})
        expected = osc_config.get(f"{kind}_on_value", 1)
        try:
            return int(float(value)) == int(float(expected))
        except (TypeError, ValueError):
            return bool(value)

    def _meter_to_mcu_value(self, value, config=None):
        config = config or self.store.get()
        try:
            level = float(value)
        except (TypeError, ValueError):
            level = 0.0
        if 0.0 <= level <= 1.0:
            return max(0, min(12, int(round(level * 12))))
        gain = float(config.get("xtouch", {}).get("meter_gain_db", 12.0))
        adjusted = level + gain
        thresholds = (-60, -50, -40, -30, -20, -14, -10, -7, -5, -3, -1, 0)
        return sum(1 for threshold in thresholds if adjusted >= threshold)

    def _send_meter(self, xtouch, value):
        config = self.store.get()
        xtouch_config = config.get("xtouch", {})
        mode = str(xtouch_config.get("meter_mode", "mcu-packed-aftertouch")).lower()
        surface_mode = str(xtouch_config.get("surface_mode", "mcu")).lower()
        if mode == "off":
            return
        with self.midi_out_lock:
            midi_out = self.midi_out
        if not midi_out:
            return
        try:
            level = self._meter_to_mcu_value(value, config)
            if surface_mode == "ctrl":
                midi_out.send(
                    mido.Message(
                        "control_change",
                        channel=0,
                        control=89 + max(1, min(8, int(xtouch))),
                        value=max(0, min(127, int(round(level * 127 / 12)))),
                    )
                )
            elif mode in ("mcu-packed-aftertouch", "aftertouch"):
                meter_value = ((max(1, min(8, int(xtouch))) - 1) << 4) | level
                midi_out.send(mido.Message("aftertouch", channel=0, value=meter_value))
            elif mode == "per-channel-aftertouch":
                midi_out.send(mido.Message("aftertouch", channel=int(xtouch) - 1, value=level))
        except Exception as exc:
            self.update_status(last_error=f"MIDI meter error: {exc}")

    def _mapped_strip_for_wing_path(self, config, path):
        osc_config = config.get("osc", {})
        for strip in config.get("strips", []):
            if not strip.get("enabled", True):
                continue
            wing_channel = int(strip.get("wing_channel", 1))
            for kind in ("fader", "mute", "solo", "select", "meter"):
                if path in self._configured_paths(osc_config, kind, wing_channel):
                    return strip, kind
            if path == self._format_path(osc_config.get("source_name_path", ""), wing_channel):
                return strip, "name"
            if path == self._format_path(osc_config.get("source_color_path", ""), wing_channel):
                return strip, "color"
        return None, ""

    def _handle_wing_message(self, address, args):
        config = self.store.get()
        self.update_status(last_wing=f"{address} {args[0] if args else ''}")
        master_path = self._master_fader_path(config)
        if args and master_path and address in (master_path, self._feedback_path(master_path)):
            self._send_motor_fader(9, self._fader_db(args))
            return
        strip, kind = self._mapped_strip_for_wing_path(config, address)
        if not strip or not args:
            return
        if kind == "fader":
            self._send_motor_fader(strip.get("xtouch", 1), self._fader_db(args))
        elif kind in ("mute", "solo", "select"):
            self._set_button_state(kind, strip.get("xtouch", 1), self._is_on_value(kind, args[0], config))
        elif kind == "meter":
            self.update_status(
                last_meter=f"{address} {args[0] if args else ''}",
                meter_messages=self.get_status().get("meter_messages", 0) + 1,
            )
            self._send_meter(strip.get("xtouch", 1), args[0])
        elif kind in ("name", "color"):
            with self.midi_out_lock:
                midi_out = self.midi_out
            if midi_out:
                self.refresh_xtouch_scribbles(midi_out, config)

    def refresh_xtouch_scribbles(self, midi_out, config=None):
        if not midi_out:
            return
        config = config or self.store.get()
        sources = self.discover_sources(refresh=False)
        for strip in config.get("strips", []):
            if not strip.get("enabled", True):
                xtouch = int(strip.get("xtouch", 1))
                self._send_lcd_text(midi_out, xtouch, "", "")
                self._send_lcd_color(midi_out, xtouch, "off", "", "")
                continue
            label, color = self._strip_label_color(strip, sources)
            xtouch = int(strip.get("xtouch", 1))
            bottom = f"Ch {strip.get('wing_channel', xtouch)}"
            self._send_lcd_text(midi_out, xtouch, label, bottom)
            self._send_lcd_color(midi_out, xtouch, color, label, bottom)

    def _wing_poll_paths(self, config):
        paths = ["/*S"]
        master_path = self._master_fader_path(config)
        if master_path:
            paths.extend((master_path, self._feedback_path(master_path)))
        osc_config = config.get("osc", {})
        for strip in config.get("strips", []):
            if not strip.get("enabled", True):
                continue
            channel = int(strip.get("wing_channel", 1))
            for kind in ("fader", "mute", "solo", "select"):
                for path in self._configured_paths(osc_config, kind, channel):
                    if path and path not in paths:
                        paths.append(path)
            for key in ("source_name_path", "source_color_path"):
                path = self._format_path(osc_config.get(key, ""), channel)
                if path and path not in paths:
                    paths.append(path)
        return paths

    def _wing_loop(self):
        while self.running.is_set():
            config = self.store.get()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.2)
            try:
                sock.bind(("0.0.0.0", 0))
                with self.wing_sock_lock:
                    self.wing_sock = sock
                self.update_status(wing_local_port=sock.getsockname()[1])
                next_poll = 0
                while self.running.is_set() and not self.wing_reload_event.is_set():
                    now = time.time()
                    if now >= next_poll:
                        config = self.store.get()
                        for path in self._wing_poll_paths(config):
                            sock.sendto(OscClient.packet(path), (config["wing_host"], int(config["wing_port"])))
                        next_poll = now + 3.0
                    try:
                        data, _addr = sock.recvfrom(4096)
                    except socket.timeout:
                        continue
                    try:
                        address, args = OscClient.decode(data)
                    except Exception:
                        continue
                    self._handle_wing_message(address, args)
                self.wing_reload_event.clear()
            except Exception as exc:
                self.update_status(last_error=f"WING listener error: {exc}")
                time.sleep(2)
            finally:
                with self.wing_sock_lock:
                    if self.wing_sock is sock:
                        self.wing_sock = None
                try:
                    sock.close()
                except Exception:
                    pass

    @staticmethod
    def _nrp_escape(payload):
        escaped = bytearray()
        for index, value in enumerate(payload):
            escaped.append(value)
            if value == 0xDF:
                next_value = payload[index + 1] if index + 1 < len(payload) else None
                if next_value is None or 0xD0 <= next_value <= 0xDE:
                    escaped.append(0xDE)
        return bytes(escaped)

    @classmethod
    def _nrp_meter_packet(cls, payload):
        return b"\xdf\xd3" + cls._nrp_escape(payload) + b"\xdf\xd1"

    @staticmethod
    def _meter_channels(config):
        channels = []
        for strip in config.get("strips", []):
            if not strip.get("enabled", True):
                continue
            channel = int(strip.get("wing_channel", 0))
            if 1 <= channel <= 40 and channel not in channels:
                channels.append(channel)
        return channels

    @staticmethod
    def _meter_strips_by_channel(config):
        mapped = {}
        for strip in config.get("strips", []):
            if not strip.get("enabled", True):
                continue
            channel = int(strip.get("wing_channel", 0))
            if 1 <= channel <= 40:
                mapped.setdefault(channel, []).append(int(strip.get("xtouch", 1)))
        return mapped

    def _handle_meter_packet(self, packet, channels, report_id):
        if len(packet) < 4 or struct.unpack(">I", packet[:4])[0] != report_id:
            return
        words_per_channel = 8
        expected = 4 + len(channels) * words_per_channel * 2
        if len(packet) < expected:
            return
        config = self.store.get()
        mapped = self._meter_strips_by_channel(config)
        last_channel = None
        last_db = -128.0
        offset = 4
        for channel in channels:
            values = struct.unpack(">8h", packet[offset : offset + 16])
            offset += 16
            output_db = max(values[2], values[3]) / 256.0
            for xtouch in mapped.get(channel, []):
                self._send_meter(xtouch, output_db)
            last_channel = channel
            last_db = output_db
        count = self.get_status().get("meter_messages", 0) + 1
        self.update_status(
            meter_messages=count,
            last_meter=f"native ch {last_channel}: {last_db:.1f} dB" if last_channel else "native meter packet",
            last_error="",
        )

    def _meter_loop(self):
        report_id = 2
        while self.running.is_set():
            udp_sock = None
            tcp_sock = None
            try:
                config = self.store.get()
                channels = self._meter_channels(config)
                if not channels:
                    self.meter_reload_event.wait(1.0)
                    self.meter_reload_event.clear()
                    continue

                udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp_sock.bind(("0.0.0.0", 0))
                udp_sock.settimeout(0.2)
                meter_port = udp_sock.getsockname()[1]
                self.update_status(meter_local_port=meter_port)

                tcp_sock = socket.create_connection(
                    (config["wing_host"], int(config.get("wing_tcp_port", 2222))), timeout=3.0
                )
                tcp_sock.settimeout(3.0)
                port_payload = b"\xd3" + struct.pack(">H", meter_port)
                request_payload = (
                    b"\xd4"
                    + struct.pack(">I", report_id)
                    + b"\xdc\xa0"
                    + bytes(channel - 1 for channel in channels)
                    + b"\xde"
                )
                tcp_sock.sendall(self._nrp_meter_packet(port_payload))
                tcp_sock.sendall(self._nrp_meter_packet(request_payload))
                next_renew = time.monotonic() + 4.0

                while self.running.is_set() and not self.meter_reload_event.is_set():
                    if time.monotonic() >= next_renew:
                        renew_payload = b"\xd4" + struct.pack(">I", report_id)
                        tcp_sock.sendall(self._nrp_meter_packet(renew_payload))
                        next_renew = time.monotonic() + 4.0
                    try:
                        packet, sender = udp_sock.recvfrom(4096)
                    except socket.timeout:
                        continue
                    if sender[0] == socket.gethostbyname(config["wing_host"]):
                        self._handle_meter_packet(packet, channels, report_id)
                self.meter_reload_event.clear()
            except Exception as exc:
                self.update_status(last_error=f"WING native meter error: {exc}")
                self.meter_reload_event.wait(2.0)
                self.meter_reload_event.clear()
            finally:
                for sock in (tcp_sock, udp_sock):
                    if sock:
                        try:
                            sock.close()
                        except Exception:
                            pass

    def _send_channel_value(self, config, strip, kind, value):
        if not strip or not strip.get("enabled", True):
            return
        channel = int(strip.get("wing_channel", 1))
        osc_config = config.get("osc", {})
        path = self._format_path(osc_config.get(f"{kind}_path", ""), channel)
        if not path:
            return
        self._send_wing(config, path, value)
        self.update_status(
            last_osc=f"{path} {value}",
            messages=self.get_status()["messages"] + 1,
            last_error="",
        )

    def _send_master_fader(self, config, value):
        path = self._master_fader_path(config)
        if not path:
            return
        self._send_wing(config, path, value)
        self.update_status(
            last_osc=f"{path} {value}",
            messages=self.get_status()["messages"] + 1,
            last_error="",
        )

    def _send_wing(self, config, path, *args):
        with self.wing_sock_lock:
            sock = self.wing_sock
        if sock:
            try:
                sock.sendto(OscClient.packet(path, *args), (config["wing_host"], int(config["wing_port"])))
                if args:
                    sock.sendto(OscClient.packet(path), (config["wing_host"], int(config["wing_port"])))
                return
            except Exception as exc:
                self.update_status(last_error=f"WING send error: {exc}")
        self.osc.send(config["wing_host"], config["wing_port"], path, *args)
        if args:
            self.osc.send(config["wing_host"], config["wing_port"], path)

    def send_test(self, kind, xtouch, value):
        config = self.store.get()
        strip = self._strip_for(config, int(xtouch))
        if kind == "fader":
            value = self._norm_to_db(value, config)
        self._send_channel_value(config, strip, kind, value)

    def test_colors(self):
        colors = ["red", "green", "yellow", "blue", "magenta", "cyan", "white", "off"]
        with self.midi_out_lock:
            midi_out = self.midi_out
        if not midi_out:
            return False
        for index, color in enumerate(colors, start=1):
            self._send_lcd_text(midi_out, index, color[:7], "Color")
            self._send_lcd_color(midi_out, index, color, color[:7], "Color")
        return True

    def probe_colors(self):
        with self.midi_out_lock:
            midi_out = self.midi_out
        if not midi_out:
            return False
        probes = [
            ("black", 0),
            ("red", 1),
            ("green", 2),
            ("yellow", 3),
            ("blue", 4),
            ("magenta", 5),
            ("cyan", 6),
            ("white", 7),
        ]
        for index, (label, color_index) in enumerate(probes, start=1):
            text = self._fit_lcd(label) + self._fit_lcd("Probe")
            data = (
                [0x00, 0x20, 0x32, 0x14, 0x4C, index - 1, color_index]
                + [ord(char) for char in text]
            )
            try:
                midi_out.send(mido.Message("sysex", data=data))
            except Exception as exc:
                self.update_status(last_error=f"MIDI color probe error: {exc}")
        return True

    def test_meters(self):
        with self.midi_out_lock:
            midi_out = self.midi_out
        if not midi_out:
            return False
        for index in range(1, 9):
            self._send_meter(index, index / 8.0)
        return True

    def _set_button_led(self, midi_out, note, enabled):
        if not midi_out:
            return
        try:
            midi_out.send(mido.Message("note_on", note=note, velocity=127 if enabled else 0))
        except Exception as exc:
            self.update_status(last_error=f"MIDI LED error: {exc}")

    def _handle_midi(self, message, midi_out=None):
        config = self.store.get()
        surface_mode = str(config.get("xtouch", {}).get("surface_mode", "mcu")).lower()
        self.update_status(last_midi=str(message))
        if config.get("log_midi"):
            print(message, flush=True)

        if surface_mode == "ctrl" and message.type == "control_change" and 70 <= message.control <= 78:
            value = self._norm_to_db(message.value / 127.0, config)
            if message.control == 78:
                self._send_master_fader(config, value)
            else:
                strip = self._strip_for(config, message.control - 69)
                self._send_channel_value(config, strip, "fader", value)
            return

        if surface_mode != "ctrl" and message.type == "pitchwheel" and 0 <= message.channel <= 8:
            value = (message.pitch + 8192) / 16383.0
            db_value = self._norm_to_db(value, config)
            if message.channel == 8:
                self._send_master_fader(config, db_value)
            else:
                strip = self._strip_for(config, message.channel + 1)
                self._send_channel_value(config, strip, "fader", db_value)
            return

        if message.type != "note_on":
            return

        if message.velocity == 0:
            return

        note = int(message.note)
        button_map = [
            ("solo", 8, "solo_on_value", "solo_off_value"),
            ("mute", 16, "mute_on_value", "mute_off_value"),
            ("select", 24, "select_on_value", "select_off_value"),
        ]
        for kind, start_note, on_key, off_key in button_map:
            if start_note <= note <= start_note + 7:
                xtouch = note - start_note + 1
                strip = self._strip_for(config, xtouch)
                if not strip:
                    return
                osc_config = config.get("osc", {})
                current_state = self._known_button_state(kind, xtouch)
                if current_state is None:
                    current_state = False
                    path = self._format_path(osc_config.get(f"{kind}_path", ""), int(strip.get("wing_channel", 1)))
                    if path:
                        self._send_wing(config, path)
                next_state = not current_state
                value = osc_config.get(on_key if next_state else off_key)
                self._send_channel_value(config, strip, kind, value)
                return

    def _midi_loop(self):
        while self.running.is_set():
            config = self.store.get()
            midi_out = None
            try:
                devices = self.midi_devices()
                input_name = self._choose_device(
                    devices["inputs"], config.get("midi_input", ""), config.get("midi_device_hint", "")
                )
                output_name = self._choose_device(
                    devices["outputs"], config.get("midi_output", ""), config.get("midi_device_hint", "")
                )
                if not input_name:
                    self.update_status(midi_input="", last_error="No MIDI input found")
                    time.sleep(2)
                    continue

                self.update_status(midi_input=input_name, midi_output=output_name, last_error="")
                if output_name:
                    try:
                        midi_out = mido.open_output(output_name)
                        with self.midi_out_lock:
                            self.midi_out = midi_out
                        self.refresh_xtouch_scribbles(midi_out, config)
                    except Exception as exc:
                        self.update_status(last_error=f"MIDI output unavailable: {exc}")
                with mido.open_input(input_name) as midi_in:
                    next_device_check = time.monotonic() + 1.0
                    next_surface_refresh = time.monotonic() + 5.0
                    while self.running.is_set() and not self.reload_event.is_set():
                        for message in midi_in.iter_pending():
                            self._handle_midi(message, midi_out)
                        now = time.monotonic()
                        if now >= next_device_check:
                            current_devices = self.midi_devices()
                            if input_name not in current_devices["inputs"]:
                                raise RuntimeError("X-Touch MIDI input disconnected")
                            if output_name and output_name not in current_devices["outputs"]:
                                raise RuntimeError("X-Touch MIDI output disconnected")
                            next_device_check = now + 1.0
                        if midi_out and now >= next_surface_refresh:
                            self.refresh_xtouch_scribbles(midi_out, self.store.get())
                            next_surface_refresh = now + 5.0
                        time.sleep(0.005)
            except Exception as exc:
                self.update_status(last_error=str(exc))
                time.sleep(2)
            finally:
                if midi_out:
                    with self.midi_out_lock:
                        if self.midi_out is midi_out:
                            self.midi_out = None
                    try:
                        midi_out.close()
                    except Exception:
                        pass
                self.reload_event.clear()


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Wing X-Touch Bridge</title>
  <style>
    :root { color-scheme: light dark; --accent:#177f75; --line:#c8d3d1; --bg:#f5f7f7; --panel:#ffffff; --ink:#16201f; }
    @media (prefers-color-scheme: dark) { :root { --bg:#111817; --panel:#182221; --line:#34413f; --ink:#edf5f3; } }
    body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--ink); }
    header { padding:22px clamp(18px, 4vw, 42px); border-bottom:1px solid var(--line); background:var(--panel); }
    h1 { margin:0; font-size:clamp(24px, 3vw, 34px); letter-spacing:0; }
    main { max-width:1180px; margin:0 auto; padding:24px clamp(14px, 3vw, 32px) 48px; display:grid; gap:22px; }
    section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; }
    h2 { margin:0 0 14px; font-size:18px; }
    .grid { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:14px; }
    .strip-grid { display:grid; grid-template-columns:repeat(8, minmax(156px, 1fr)); gap:10px; overflow-x:auto; padding-bottom:4px; }
    label { display:grid; gap:6px; font-size:13px; font-weight:650; }
    input, select { width:100%; box-sizing:border-box; border:1px solid var(--line); background:transparent; color:var(--ink); border-radius:6px; padding:9px 10px; font:inherit; }
    input[type=checkbox] { width:auto; transform:scale(1.15); }
    .strip { border:1px solid var(--line); border-radius:8px; padding:10px; display:grid; gap:10px; min-width:156px; }
    .strip-title { display:flex; align-items:center; justify-content:space-between; font-weight:750; }
    .checkline { display:flex; align-items:center; gap:8px; font-size:12px; font-weight:650; }
    .source-line { display:flex; align-items:center; gap:8px; min-height:22px; font-size:12px; opacity:.78; }
    .swatch { width:18px; height:18px; border-radius:50%; border:1px solid var(--line); flex:0 0 auto; background:var(--swatch, #fff); }
    .actions { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    button { border:0; background:var(--accent); color:white; border-radius:6px; padding:10px 14px; font:inherit; font-weight:750; cursor:pointer; }
    button.secondary { background:transparent; color:var(--ink); border:1px solid var(--line); }
    .status { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:12px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; min-height:54px; }
    .metric b { display:block; font-size:12px; opacity:.72; margin-bottom:4px; }
    .metric span { overflow-wrap:anywhere; }
    .muted { opacity:.72; font-size:13px; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    @media (max-width: 760px) { .grid, .status { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Wing X-Touch Bridge</h1>
    <div class="muted">Setup page for mapping Behringer X-Touch strips to WING Rack sources.</div>
  </header>
  <main>
    <section>
      <h2>Status</h2>
      <div class="status">
        <div class="metric"><b>MIDI input</b><span id="status-midi">Loading</span></div>
        <div class="metric"><b>Last MIDI</b><span id="status-last-midi">-</span></div>
        <div class="metric"><b>Last OSC</b><span id="status-last-osc">-</span></div>
        <div class="metric"><b>Messages</b><span id="status-messages">0</span></div>
        <div class="metric"><b>Meter messages</b><span id="status-meter-messages">0</span></div>
        <div class="metric"><b>Last meter</b><span id="status-last-meter">-</span></div>
        <div class="metric"><b>Error</b><span id="status-error">-</span></div>
        <div class="metric"><b>WING target</b><span id="status-wing">-</span></div>
      </div>
    </section>

    <section>
      <h2>Connection</h2>
      <div class="grid">
        <label>WING IP<input id="wing_host"></label>
        <label>WING OSC Port<input id="wing_port" type="number" min="1" max="65535"></label>
        <label>WING TCP Port<input id="wing_tcp_port" type="number" min="1" max="65535"></label>
        <label>MIDI Input<select id="midi_input"></select></label>
        <label>MIDI Output<select id="midi_output"></select></label>
      </div>
    </section>

    <section>
      <h2>X-Touch Behavior</h2>
      <div class="grid">
        <label>Physical 0 dB Position<input id="zero_db_position" type="number" min="0.05" max="0.98" step="0.001"></label>
        <label>Fader Bottom dB<input id="fader_min_db" type="number" step="0.1"></label>
        <label>Fader Top dB<input id="fader_max_db" type="number" step="0.1"></label>
        <label>Surface Mode<select id="surface_mode">
          <option value="mcu">MC USB</option>
          <option value="ctrl">CTRL USB</option>
        </select></label>
        <label>Meter Boost dB<input id="meter_gain_db" type="number" min="-20" max="30" step="1"></label>
        <label>Scribble Color Mode<select id="color_mode">
          <option value="off">Off</option>
          <option value="mcu-72">MCU 72</option>
          <option value="behringer-72">Behringer 72</option>
          <option value="behringer-4c">Behringer 4C</option>
          <option value="all">Try all</option>
        </select></label>
        <label>Meter Mode<select id="meter_mode">
          <option value="mcu-packed-aftertouch">MCU packed aftertouch</option>
          <option value="per-channel-aftertouch">Per-channel aftertouch</option>
          <option value="off">Off</option>
        </select></label>
        <label>Master Fader Target<select id="master_fader_target"></select></label>
      </div>
    </section>

    <section>
      <h2>Strip Mapping</h2>
      <div class="actions" style="margin-bottom:12px">
        <button id="refresh-sources" class="secondary">Refresh WING sources</button>
        <span id="source-count" class="muted"></span>
      </div>
      <div id="strips" class="strip-grid"></div>
    </section>

    <section>
      <h2>OSC Paths</h2>
      <div class="grid">
        <label>Fader Path<input id="fader_path"></label>
        <label>Mute Path<input id="mute_path"></label>
        <label>Solo Path<input id="solo_path"></label>
        <label>Select Path<input id="select_path"></label>
        <label>Meter Path<input id="meter_path"></label>
        <label>Source Name Path<input id="source_name_path"></label>
        <label>Source Color Path<input id="source_color_path"></label>
      </div>
      <p class="muted">Use <code>{channel:02d}</code> for two-digit channel numbers or <code>{channel}</code> for plain numbers.</p>
    </section>

    <section>
      <h2>Button Values</h2>
      <div class="grid">
        <label>Mute Press Value<input id="mute_on_value" type="number" step="1"></label>
        <label>Solo Press Value<input id="solo_on_value" type="number" step="1"></label>
        <label>Select Press Value<input id="select_on_value" type="number" step="1"></label>
      </div>
    </section>

    <section>
      <h2>Save And Test</h2>
      <div class="actions">
        <button id="save">Save settings</button>
        <button id="test-fader" class="secondary">Send strip 1 fader 50%</button>
        <button id="test-colors" class="secondary">Test colors</button>
        <button id="probe-colors" class="secondary">Probe colors</button>
        <button id="test-meters" class="secondary">Test meters</button>
        <button id="refresh-midi" class="secondary">Refresh MIDI devices</button>
      </div>
    </section>
  </main>
  <script>
    let config = null;
    let devices = {inputs: [], outputs: []};
    let sources = [];
    const colorMap = {off:"#111", red:"#d83a2e", green:"#28a745", yellow:"#e6be28", blue:"#2477d8", magenta:"#c23bd8", cyan:"#2bbcc0", white:"#f7f7f7"};
    const $ = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[char]));

    function optionList(select, names, current) {
      select.innerHTML = "";
      const auto = document.createElement("option");
      auto.value = "";
      auto.textContent = "Auto";
      select.appendChild(auto);
      names.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      });
      select.value = current || "";
    }

    function sourceFor(channel) {
      if (Number(channel) <= 0) return {channel:0, name:"None", color:"off", live:true};
      return sources.find((source) => Number(source.channel) === Number(channel)) || {channel, name:`Channel ${channel}`, color:"white", live:false};
    }

    function sourceOptions(current) {
      const noneSelected = Number(current) <= 0 ? "selected" : "";
      const none = `<option value="0" ${noneSelected}>None</option>`;
      return none + sources.map((source) => {
        const selected = Number(source.channel) === Number(current) ? "selected" : "";
        const marker = source.live ? "" : " *";
        return `<option value="${source.channel}" ${selected}>${escapeHtml(source.name)} - Ch ${source.channel}${marker}</option>`;
      }).join("");
    }

    function colorOptions(current) {
      return Object.keys(colorMap).map((color) => `<option value="${color}" ${color === current ? "selected" : ""}>${color}</option>`).join("");
    }

    function fillMasterTargets() {
      const select = $("master_fader_target");
      const target = config.master_fader || {enabled:true, type:"main", number:1};
      const current = target.enabled === false ? "none" : `${target.type || "main"}:${target.number || 1}`;
      const groups = [
        ["main", "Main Output", 4],
        ["mtx", "Matrix", 8],
        ["aux", "Aux", 8],
        ["dca", "DCA", 16],
      ];
      select.innerHTML = '<option value="none">None</option>';
      groups.forEach(([type, label, count]) => {
        const group = document.createElement("optgroup");
        group.label = label;
        for (let number = 1; number <= count; number += 1) {
          const option = document.createElement("option");
          option.value = `${type}:${number}`;
          option.textContent = `${label} ${number}`;
          group.appendChild(option);
        }
        select.appendChild(group);
      });
      select.value = current;
    }

    function drawStrips() {
      const root = $("strips");
      root.innerHTML = "";
      const liveCount = sources.filter((source) => source.live).length;
      $("source-count").textContent = sources.length ? `${sources.length} sources loaded${liveCount ? `, ${liveCount} live from WING` : ", fallback list"}` : "No sources loaded";
      config.strips.forEach((strip, index) => {
        const source = sourceFor(strip.wing_channel);
        const effectiveName = strip.override_name && strip.name ? strip.name : source.name;
        const effectiveColor = strip.override_color && strip.color ? strip.color : source.color;
        const color = colorMap[effectiveColor] || colorMap.white;
        const el = document.createElement("div");
        el.className = "strip";
        el.innerHTML = `
          <div class="strip-title"><span>X${strip.xtouch}</span><input type="checkbox" ${strip.enabled ? "checked" : ""} data-field="enabled"></div>
          <label>Source<select data-field="wing_channel">${sourceOptions(strip.wing_channel)}</select></label>
          <div class="source-line"><span class="swatch" style="--swatch:${color}"></span><span>${escapeHtml(effectiveName)}</span></div>
          <label class="checkline"><input type="checkbox" ${strip.override_name ? "checked" : ""} data-field="override_name">Override name</label>
          <label>Name<input value="${escapeHtml(strip.name || "")}" data-field="name" ${strip.override_name ? "" : "disabled"}></label>
          <label class="checkline"><input type="checkbox" ${strip.override_color ? "checked" : ""} data-field="override_color">Override color</label>
          <label>Color<select data-field="color" ${strip.override_color ? "" : "disabled"}>${colorOptions(strip.color || source.color || "white")}</select></label>
        `;
        el.querySelectorAll("[data-field]").forEach((input) => {
          input.addEventListener("input", () => {
            const field = input.dataset.field;
            strip[field] = input.type === "checkbox" ? input.checked : (field === "wing_channel" ? Number(input.value) : input.value);
            if (field === "wing_channel") {
              const nextSource = sourceFor(strip.wing_channel);
              strip.enabled = strip.wing_channel > 0;
              if (!strip.override_name) strip.name = "";
              if (!strip.override_color) strip.color = "";
            }
            if (field === "override_name" || field === "override_color" || field === "wing_channel" || field === "color" || field === "name") {
              drawStrips();
            }
          });
        });
        root.appendChild(el);
      });
    }

    function fillForm() {
      $("wing_host").value = config.wing_host;
      $("wing_port").value = config.wing_port;
      $("wing_tcp_port").value = config.wing_tcp_port;
      $("zero_db_position").value = config.fader?.zero_db_position ?? 0.731;
      $("fader_min_db").value = config.fader?.min_db ?? -144;
      $("fader_max_db").value = config.fader?.max_db ?? 10;
      $("surface_mode").value = config.xtouch?.surface_mode ?? "mcu";
      $("meter_gain_db").value = config.xtouch?.meter_gain_db ?? 12;
      $("color_mode").value = config.xtouch?.color_mode ?? "mcu-72";
      $("meter_mode").value = config.xtouch?.meter_mode ?? "mcu-packed-aftertouch";
      fillMasterTargets();
      $("fader_path").value = config.osc.fader_path;
      $("mute_path").value = config.osc.mute_path;
      $("solo_path").value = config.osc.solo_path;
      $("select_path").value = config.osc.select_path;
      $("meter_path").value = config.osc.meter_path || "";
      $("source_name_path").value = config.osc.source_name_path;
      $("source_color_path").value = config.osc.source_color_path;
      $("mute_on_value").value = config.osc.mute_on_value;
      $("solo_on_value").value = config.osc.solo_on_value;
      $("select_on_value").value = config.osc.select_on_value;
      optionList($("midi_input"), devices.inputs, config.midi_input);
      optionList($("midi_output"), devices.outputs, config.midi_output);
      drawStrips();
    }

    function readForm() {
      config.wing_host = $("wing_host").value.trim();
      config.wing_port = Number($("wing_port").value);
      config.wing_tcp_port = Number($("wing_tcp_port").value);
      config.midi_input = $("midi_input").value;
      config.midi_output = $("midi_output").value;
      config.fader = config.fader || {};
      config.fader.zero_db_position = Number($("zero_db_position").value);
      config.fader.min_db = Number($("fader_min_db").value);
      config.fader.max_db = Number($("fader_max_db").value);
      config.xtouch = config.xtouch || {};
      config.xtouch.surface_mode = $("surface_mode").value;
      config.xtouch.meter_gain_db = Number($("meter_gain_db").value);
      config.xtouch.color_mode = $("color_mode").value;
      config.xtouch.meter_mode = $("meter_mode").value;
      const masterTarget = $("master_fader_target").value;
      if (masterTarget === "none") {
        config.master_fader = {enabled:false, type:"main", number:1};
      } else {
        const [type, number] = masterTarget.split(":");
        config.master_fader = {enabled:true, type, number:Number(number)};
      }
      config.osc.fader_path = $("fader_path").value.trim();
      config.osc.mute_path = $("mute_path").value.trim();
      config.osc.solo_path = $("solo_path").value.trim();
      config.osc.select_path = $("select_path").value.trim();
      config.osc.meter_path = $("meter_path").value.trim();
      config.osc.source_name_path = $("source_name_path").value.trim();
      config.osc.source_color_path = $("source_color_path").value.trim();
      config.osc.mute_on_value = Number($("mute_on_value").value);
      config.osc.solo_on_value = Number($("solo_on_value").value);
      config.osc.select_on_value = Number($("select_on_value").value);
    }

    async function loadAll() {
      devices = await fetch("/api/midi/devices").then(r => r.json());
      config = await fetch("/api/config").then(r => r.json());
      sources = await fetch("/api/wing/sources").then(r => r.json());
      fillForm();
    }

    async function refreshSources() {
      readForm();
      await fetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(config)});
      sources = await fetch("/api/wing/sources?refresh=1").then(r => r.json());
      drawStrips();
    }

    async function save() {
      readForm();
      config = await fetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(config)}).then(r => r.json());
      fillForm();
    }

    async function refreshStatus() {
      const status = await fetch("/api/status").then(r => r.json());
      $("status-midi").textContent = status.midi_input || "No MIDI input";
      $("status-last-midi").textContent = status.last_midi || "-";
      $("status-last-osc").textContent = status.last_osc || "-";
      $("status-messages").textContent = status.messages || 0;
      $("status-meter-messages").textContent = status.meter_messages || 0;
      $("status-last-meter").textContent = status.last_meter || "-";
      $("status-error").textContent = status.last_error || "-";
      $("status-wing").textContent = config ? `${config.wing_host}:${config.wing_port}` : "-";
    }

    $("save").addEventListener("click", save);
    $("refresh-midi").addEventListener("click", loadAll);
    $("refresh-sources").addEventListener("click", refreshSources);
    $("test-fader").addEventListener("click", async () => {
      await save();
      await fetch("/api/test/send", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({kind:"fader", xtouch:1, value:0.5})});
      await refreshStatus();
    });
    $("test-colors").addEventListener("click", async () => {
      await save();
      await fetch("/api/test/colors", {method:"POST"});
      await refreshStatus();
    });
    $("probe-colors").addEventListener("click", async () => {
      await save();
      await fetch("/api/test/color-probe", {method:"POST"});
      await refreshStatus();
    });
    $("test-meters").addEventListener("click", async () => {
      await save();
      await fetch("/api/test/meters", {method:"POST"});
      await refreshStatus();
    });
    loadAll().then(refreshStatus);
    setInterval(refreshStatus, 1500);
  </script>
</body>
</html>
"""


def make_handler(store, bridge):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, data, status=200):
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self):
            path = urlparse(self.path).path
            query = urlparse(self.path).query
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                body = HTML.encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/config":
                self._send_json(store.get())
            elif path == "/api/status":
                self._send_json(bridge.get_status())
            elif path == "/api/midi/devices":
                self._send_json(bridge.midi_devices())
            elif path == "/api/wing/sources":
                self._send_json(bridge.discover_sources(refresh="refresh=1" in query))
            else:
                self._send_json({"error": "Not found"}, status=404)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/config":
                    saved = store.save(self._read_json())
                    bridge.reload()
                    self._send_json(saved)
                elif path == "/api/test/send":
                    data = self._read_json()
                    bridge.send_test(data.get("kind", "fader"), data.get("xtouch", 1), data.get("value", 0.5))
                    self._send_json({"ok": True})
                elif path == "/api/test/colors":
                    self._send_json({"ok": bridge.test_colors()})
                elif path == "/api/test/color-probe":
                    self._send_json({"ok": bridge.probe_colors()})
                elif path == "/api/test/meters":
                    self._send_json({"ok": bridge.test_meters()})
                elif path == "/api/scribbles/refresh":
                    bridge.reload()
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "Not found"}, status=404)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)

        def do_HEAD(self):
            path = urlparse(self.path).path
            if path == "/":
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} - {fmt % args}", flush=True)

    return Handler


def main():
    store = ConfigStore(CONFIG_PATH)
    config = store.get()
    bridge = Bridge(store)
    bridge.start()
    server = ThreadingHTTPServer((config["web_host"], int(config["web_port"])), make_handler(store, bridge))
    print(f"{APP_NAME} listening on http://{config['web_host']}:{config['web_port']}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
