#! /usr/bin/env python
# -*- coding: utf-8 -*-
"""
RFXCom433 for Indigo - RFXtrx433 / 433E / 868 transceivers.

Built on pyRFXtrx (the library Home Assistant uses). Devices are identified the
same way Home Assistant does it: packet type + subtype + RF id. A switch you
create from a remote press can also *transmit* that code, so one Indigo device
covers both the plug and the remote that controls it.

Author: Durosity. MIT licence.
"""

import datetime
import logging
import os
import queue
import threading
import time

import indigo

try:
    import RFXtrx
    from RFXtrx import lowlevel, RFXtrxTransportError
except ImportError:                     # reported cleanly in startup()
    RFXtrx = None
    lowlevel = None
    RFXtrxTransportError = Exception

import fnmatch

import rfx_mapping as M
import rfx_firmware as FW
import rfx_migrate as MIG
from rfx_transport import IndigoRFXTransport, is_network_url

SWITCH_TYPES = ("rfxSwitch", "rfxDimmer", "rfxCover")
RF_TYPES = SWITCH_TYPES + ("rfxSensor", "rfxSecurity", "rfxDoorbell", "rfxRemote")
INTERFACE_TYPE = "rfxInterface"

# Indigo rejects empty option IDs in dynamic lists, so "nothing chosen" needs a real value
NO_CHOICE = "none"
AUTO_CHOICE = "auto"

RECONNECT_DELAYS = (5, 10, 30, 60, 120)
HEALTH_INTERVAL = 30


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def as_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes", "on")


def as_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class TxJob:
    def __init__(self, dev_id, op, arg=None, states=None, label="", cover=False):
        self.cover = cover
        self.dev_id = dev_id
        self.op = op
        self.arg = arg
        self.states = states or []
        self.label = label


################################################################################
class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = as_bool(pluginPrefs.get("showDebugInfo"), False)
        self._apply_log_level()

        self.events = queue.Queue()
        self.txq = queue.Queue()
        self.stop_event = threading.Event()

        self.conn = None
        self.transport = None
        self.connected = False
        self.connecting = False
        self.next_attempt = 0.0
        self.fail_count = 0
        self.port_warned = False
        self.status_info = {}

        self.devices_by_key = {}        # deviceKey -> dev.id
        self.key_extras = {}            # deviceKey -> [other dev.ids sharing that code]
        self.groups = {}                # (pt, st, base) -> set(dev.id)
        self.discovered = {}            # deviceKey -> info dict (signals heard)
        self.recent = {}                # dedupe: (key, cmd, level) -> time
        self.pending_off = {}           # momentary devices: dev.id -> time
        self.pending_clear = {}         # 'command' auto-blank: dev.id -> time
        self.pending_patterns = {}      # migrated devices still learning their code: dev.id -> [patterns]
        self.triggers = {}
        self.interface_dev_id = None
        self.last_health = 0.0

        self.flashing = False
        self.flash_thread = None
        self.tx_thread = None

    # ------------------------------------------------------------------ logging
    def _apply_log_level(self):
        level = logging.DEBUG if self.debug else logging.INFO
        try:
            self.indigo_log_handler.setLevel(level)
        except AttributeError:
            pass
        try:
            self.plugin_file_handler.setLevel(logging.DEBUG)
        except AttributeError:
            pass
        logging.getLogger("RFXtrx").setLevel(logging.DEBUG if self.debug else logging.WARNING)

    # ------------------------------------------------------------------ lifecycle
    def startup(self):
        if RFXtrx is None:
            self.logger.error("pyRFXtrx is not installed - check requirements.txt installed correctly "
                              "(Plugins > Reload, or reinstall the plugin)")
            return
        self.logger.debug("startup")
        self.tx_thread = threading.Thread(target=self._tx_worker, name="RFXCOM-TX", daemon=True)
        self.tx_thread.start()
        self._ensure_interface_device()

    def shutdown(self):
        self.logger.debug("shutdown")
        self.stop_event.set()
        self._disconnect("plugin shutting down")

    def runConcurrentThread(self):
        try:
            while True:
                if RFXtrx is not None:
                    self._ensure_connection()
                    deadline = time.time() + 0.5
                    while time.time() < deadline:
                        try:
                            item = self.events.get(timeout=0.1)
                        except queue.Empty:
                            break
                        try:
                            self._process_item(item)
                        except Exception:
                            self.logger.exception("error processing RF event")
                    self._service_momentary()
                    if time.time() - self.last_health > HEALTH_INTERVAL:
                        self.last_health = time.time()
                        self._health_check()
                self.sleep(0.05)
        except self.StopThread:
            pass

    # ------------------------------------------------------------------ prefs
    def validatePrefsConfigUi(self, valuesDict):
        errors = indigo.Dict()
        try:
            self.validateSerialPortUi(valuesDict, errors, "serialPort")
        except Exception:
            pass
        for fid, lo, hi in (("minSightingsControls", 1, 10), ("minSightingsSensors", 1, 20)):
            v = as_int(valuesDict.get(fid), -1)
            if not lo <= v <= hi:
                errors[fid] = "Enter a number from {0} to {1}".format(lo, hi)
        if as_float(valuesDict.get("dedupeSeconds"), -1) < 0:
            errors["dedupeSeconds"] = "Enter 0 or more seconds"
        if as_bool(valuesDict.get("manageProtocols")):
            if not any(as_bool(valuesDict.get("proto_" + m)) for m in M.all_recmodes()):
                errors["manageProtocols"] = "Enable at least one protocol, or untick this option"
        if len(errors):
            return False, valuesDict, errors
        return True, valuesDict

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return
        self.debug = as_bool(valuesDict.get("showDebugInfo"))
        self._apply_log_level()
        self.logger.info("settings saved - reconnecting to the RFXtrx")
        self._disconnect("settings changed")
        self.next_attempt = time.time() + 1
        self.fail_count = 0
        self.port_warned = False

    def _port_url(self):
        try:
            url = self.getSerialPortUrl(self.pluginPrefs, "serialPort")
            if url:
                return url
        except Exception:
            pass
        p = self.pluginPrefs
        kind = p.get("serialPort_serialConnType", "local")
        if kind == "netSocket":
            return p.get("serialPort_serialPortNetSocket", "")
        if kind == "netRfc2217":
            return p.get("serialPort_serialPortNetRfc2217", "")
        return p.get("serialPort_serialPortLocal", "")

    def _configured_modes(self):
        if not as_bool(self.pluginPrefs.get("manageProtocols")):
            return None
        return [m for m in M.all_recmodes() if as_bool(self.pluginPrefs.get("proto_" + m))]

    def _unit_prefs(self):
        p = self.pluginPrefs
        return {"tempUnits": p.get("tempUnits", "C"), "tempDecimals": p.get("tempDecimals", "1"),
                "windUnits": p.get("windUnits", "mph"), "rainUnits": p.get("rainUnits", "mm")}

    # ------------------------------------------------------------------ connection
    def _ensure_connection(self):
        if self.flashing or self.connected or self.connecting:
            return
        if time.time() < self.next_attempt:
            return
        url = self._port_url()
        if not url:
            if not self.port_warned:
                self.logger.warning("no RFXtrx port configured - open Plugins > RFXCom433 > Configure")
                self.port_warned = True
            self.next_attempt = time.time() + 30
            return

        self.connecting = True
        self._set_interface_states([{"key": "connectionState", "value": "connecting"}])
        transport = IndigoRFXTransport(url, raw_hook=self._raw_hook)
        modes = self._configured_modes()
        conn = RFXtrx.Connect(transport, self._rfx_callback, modes=modes)
        try:
            self.logger.debug("connecting to {0}".format(url))
            conn.connect(timeout=15)
        except (TimeoutError, RFXtrxTransportError, OSError, ValueError) as exc:
            try:
                conn.close_connection()
            except Exception:
                pass
            delay = RECONNECT_DELAYS[min(self.fail_count, len(RECONNECT_DELAYS) - 1)]
            self.fail_count += 1
            msg = str(exc) or exc.__class__.__name__
            if isinstance(exc, TimeoutError):
                msg = "no response from the RFXtrx (wrong port, or another program has it open?)"
            if self.fail_count <= 3 or self.debug:
                self.logger.error("could not connect to {0}: {1} - retrying in {2}s".format(url, msg, delay))
            self.next_attempt = time.time() + delay
            self.connecting = False
            self._set_interface_states([{"key": "connectionState", "value": "disconnected"}])
            return

        self.conn = conn
        self.transport = transport
        self.connected = True
        self.connecting = False
        self.fail_count = 0

        status = getattr(conn, "_status", None)
        raw = getattr(status, "data", None)
        info = M.decode_status(raw) or {}
        self.status_info = info
        started = transport.receiver_started()

        desc = "{0}, firmware {1} {2}".format(info.get("transceiverType", "RFXtrx"),
                                               info.get("firmwareType", "?"),
                                               info.get("firmwareVersion", "?"))
        if info.get("hardwareVersion"):
            desc += ", hardware {0}".format(info["hardwareVersion"])
        self.logger.info("connected to {0} on {1}".format(desc, url))
        if modes is not None:
            self.logger.info("receiver protocols set: {0}".format(", ".join(info.get("enabledProtocols", modes))))
        else:
            self.logger.debug("receiver protocols: {0}".format(", ".join(info.get("enabledProtocols", []))))
        if not started:
            self.logger.warning("the RFXtrx didn't confirm 'start receiver' - this firmware is old; "
                                "it will still work, but a firmware update is recommended")

        states = [
            {"key": "connectionState", "value": "connected"},
            {"key": "port", "value": url},
            {"key": "transceiverType", "value": info.get("transceiverType", "")},
            {"key": "firmwareVersion", "value": info.get("firmwareVersion", 0)},
            {"key": "firmwareType", "value": info.get("firmwareType", "")},
            {"key": "hardwareVersion", "value": info.get("hardwareVersion", "")},
            {"key": "outputPower", "value": info.get("outputPower") if info.get("outputPower") is not None else 0},
            {"key": "noiseLevel", "value": info.get("noiseLevel") if info.get("noiseLevel") is not None else 0},
            {"key": "enabledProtocols", "value": ", ".join(info.get("enabledProtocols", []))},
            {"key": "receiverStarted", "value": started},
            {"key": "lastConnected", "value": now_str()},
        ]
        self._set_interface_states(states)
        self._fire("interfaceConnected")

    def _disconnect(self, reason=""):
        conn = self.conn
        self.conn = None
        self.transport = None
        was = self.connected
        self.connected = False
        if conn is not None:
            try:
                conn.close_connection()
            except Exception:
                pass
        if was:
            self.logger.debug("disconnected ({0})".format(reason))
            self._set_interface_states([{"key": "connectionState",
                                          "value": "updating firmware" if self.flashing else "disconnected"}])

    def _connection_lost(self):
        if not self.connected:
            return
        self.logger.error("lost connection to the RFXtrx - reconnecting")
        self._disconnect("connection lost")
        self.next_attempt = time.time() + RECONNECT_DELAYS[0]
        self._fire("interfaceDisconnected")

    # Called on pyRFXtrx's reader thread - keep it cheap, hand everything over.
    def _rfx_callback(self, event):
        self.events.put(("event", event))

    def _raw_hook(self, direction, pkt):
        if direction == "rx" and lowlevel is not None and len(pkt) > 1:
            if pkt[1] not in lowlevel.PACKET_TYPES and pkt[1] not in lowlevel.ACK_PACKETTYPES:
                self.events.put(("unsupported", pkt))
        if self.debug and as_bool(self.pluginPrefs.get("logRawPackets")):
            self.events.put(("raw", direction, pkt))

    # ------------------------------------------------------------------ RX processing
    def _process_item(self, item):
        kind = item[0]
        if kind == "raw":
            self.logger.debug("{0}: {1}".format(item[1].upper(), item[2].hex(" ").upper()))
            return
        if kind == "unsupported":
            pkt = item[1]
            self._bump_interface(pkt)
            if as_bool(self.pluginPrefs.get("logUnknown"), True):
                self.logger.info("received packet type 0x{0:02X} that pyRFXtrx doesn't decode yet: {1}".format(
                    pkt[1], pkt.hex(" ").upper()))
            return

        event = item[1]
        if isinstance(event, RFXtrx.ConnectionLost):
            self._connection_lost()
            return
        if isinstance(event, (RFXtrx.ConnectionDone, RFXtrx.StatusEvent)):
            return
        data = getattr(event, "data", None)
        if data is not None:
            self._bump_interface(data)

        if isinstance(event, RFXtrx.SensorEvent):
            if isinstance(event.pkt, lowlevel.Undecoded):
                if as_bool(self.pluginPrefs.get("logUndecoded")):
                    self.logger.info("undecoded RF: {0}".format(event.values.get("Payload", "")))
                return
            if isinstance(event.pkt, lowlevel.Security1):
                self._handle_security(event)
            else:
                self._handle_sensor(event)
        elif isinstance(event, RFXtrx.ControlEvent):
            self._handle_control(event)

    def _bump_interface(self, data):
        if not self.interface_dev_id:
            return
        try:
            dev = indigo.devices[self.interface_dev_id]
            self._set_interface_states([
                {"key": "packetsReceived", "value": as_int(dev.states.get("packetsReceived")) + 1},
                {"key": "lastPacketTime", "value": now_str()},
                {"key": "lastPacketHex", "value": bytes(data).hex(" ").upper()},
            ])
        except (KeyError, ValueError):
            self.interface_dev_id = None

    def _is_duplicate(self, key, *parts):
        window = as_float(self.pluginPrefs.get("dedupeSeconds"), 0.5)
        if window <= 0:
            return False
        sig = (key,) + parts
        now = time.time()
        last = self.recent.get(sig)
        self.recent[sig] = now
        if len(self.recent) > 500:
            cutoff = now - 60
            self.recent = {k: v for k, v in self.recent.items() if v > cutoff}
        return last is not None and now - last < window

    def _note_heard(self, key, pt, st, ident, dtype, summary, values=None):
        d = self.discovered.get(key)
        now = time.time()
        first_time = d is None
        if d is None or now - d["lastSeen"] > 600:
            d = {"key": key, "pt": pt, "st": st, "id": ident, "count": 0, "firstSeen": now,
                 "protocol": M.protocol_name(pt, st), "short": M.short_protocol(pt, st)}
            self.discovered[key] = d
        d["count"] += 1
        d["lastSeen"] = now
        d["deviceType"] = dtype
        d["summary"] = summary
        if values is not None:
            d["values"] = values
        if first_time and key not in self.devices_by_key:
            self._set_interface_states([{"key": "lastNewSignal", "value": "{0} {1} ({2})".format(
                d["short"], ident, summary)}, {"key": "lastNewSignalKey", "value": key}])
            if key not in self._ignored():
                self.logger.info("heard new {0} {1}: {2}".format(d["protocol"], ident, summary))
                self._fire("newSignal", key=key)
        return d

    # ---- control packets (switches, remotes, chimes, covers) ----
    def _targets(self, key):
        """Every device using this RF code: the primary one, any others sharing it,
        and migrated devices that learn the code now."""
        if self.pending_patterns:
            self._adopt_pending(key)            # may add to an existing code as well
        primary = self.devices_by_key.get(key)
        if primary is None:
            return []
        return [primary] + [d for d in self.key_extras.get(key, ()) if d != primary]

    def _legacy_states(self, dev):
        """Old-plugin 'lastUpdated' state, kept for devices converted from the old plugin."""
        if not as_bool(dev.pluginProps.get("legacyStates")):
            return []
        return [{"key": "lastUpdated", "value": time.strftime(M.LEGACY_TIME_FORMAT)}]

    def _handle_control(self, event):
        dev_obj = event.device
        pt, st, ident = dev_obj.packettype, dev_obj.subtype, dev_obj.id_string
        key = M.make_key(pt, st, ident)
        vals = event.values
        cmd = vals.get("Command") or ""
        level = vals.get("Dim level")
        rssi = vals.get("Rssi numeric")
        sound = vals.get("Sound")
        if pt == 0x16 and sound is not None:
            cmd = "Sound {0}".format(sound)

        if self._is_duplicate(key, cmd, level):
            self.logger.debug("duplicate {0} {1} ignored".format(key, cmd))
            return

        kind, _on = M.classify_command(cmd)
        dtype = M.device_type_for(pt, cmd)
        summary = cmd + (" {0}%".format(level) if level is not None else "")
        self.logger.debug("RX control {0} [{1}] {2}".format(M.protocol_name(pt, st), ident, summary))

        targets = self._targets(key)
        self._note_heard(key, pt, st, ident, dtype, summary)
        if not targets:
            created = self._maybe_autocreate(key, dtype, control=True)
            targets = [created] if created is not None else []

        for dev_id in targets:
            self._apply_control(dev_id, cmd, kind, level, rssi)

        # group commands also update every other unit on the same house/remote code
        if kind in ("group_on", "group_off", "group_level"):
            base = M.group_base(pt, ident)
            for gid in list(self.groups.get((pt, st, base), ())):
                if gid in targets:
                    continue
                try:
                    gdev = indigo.devices[gid]
                except KeyError:
                    continue
                if not as_bool(gdev.pluginProps.get("respondToGroup"), True):
                    continue
                self._apply_control(gid, cmd, kind, level, rssi, from_group=True)

        self._set_interface_states([{"key": "lastReceived", "value": "{0} {1}: {2}".format(
            M.short_protocol(pt, st), ident, summary)}])
        for dev_id in targets or [None]:
            self._fire("commandReceived", dev_id=dev_id, key=key, command=cmd)

    def _apply_control(self, dev_id, cmd, kind, level, rssi, from_group=False):
        try:
            dev = indigo.devices[dev_id]
        except KeyError:
            return
        props = dev.pluginProps
        role = props.get("role", "switch")
        if role == "transmit":
            self.logger.debug("'{0}' is transmit-only; ignoring received {1}".format(dev.name, cmd))
            return

        # a plain switch receiving a level means it's really a dimmer - upgrade it
        if (dev.deviceTypeId == "rfxSwitch" and kind in ("level", "group_level") and level is not None
                and as_bool(props.get("autoDimmer"), True)):
            self.logger.info("'{0}' received a dim level - converting it to a dimmer".format(dev.name))
            indigo.device.changeDeviceTypeId(dev, "rfxDimmer")
            dev = indigo.devices[dev_id]

        states = self._seen_states(dev, rssi)
        # 'command' is the current command and may auto-clear; 'lastCommand' always keeps the last one
        states.append({"key": "command", "value": cmd})
        states.append({"key": "lastCommand", "value": cmd})
        states += self._legacy_states(dev)
        self._schedule_clear(dev)
        changed = None
        t = dev.deviceTypeId

        if t == "rfxCover":
            if kind in ("on", "open", "group_on"):
                changed = True
                states.append({"key": "coverState", "value": "open"})
            elif kind in ("off", "close", "group_off"):
                changed = False
                states.append({"key": "coverState", "value": "closed"})
            elif kind == "stop":
                states.append({"key": "coverState", "value": "stopped"})
        elif t == "rfxDoorbell":
            changed = True
            self.pending_off[dev_id] = time.time() + as_float(props.get("momentarySeconds"), 5)
        elif t == "rfxDimmer":
            if kind in ("on", "group_on"):
                states.append({"key": "brightnessLevel", "value": as_int(props.get("lastLevel"), 100) or 100})
            elif kind in ("off", "group_off"):
                states.append({"key": "brightnessLevel", "value": 0})
            elif kind in ("level", "group_level") and level is not None:
                states.append({"key": "brightnessLevel", "value": int(level)})
                if level > 0:
                    self._remember_level(dev, int(level))
        elif t == "rfxSwitch":
            if kind in ("on", "group_on"):
                changed = True
            elif kind in ("off", "group_off"):
                changed = False
            elif kind in ("level", "group_level") and level is not None:
                changed = level > 0
            elif role == "button" and as_float(props.get("momentarySeconds"), 0) > 0:
                changed = True
                self.pending_off[dev_id] = time.time() + as_float(props.get("momentarySeconds"), 1)
        # rfxRemote: a handset or wall controller - only its command states change

        if changed is not None:
            states.append({"key": "onOffState", "value": changed})
        self._update_states(dev, states)
        if not from_group:
            self.logger.debug("'{0}' <- {1}".format(dev.name, cmd))

    # ---- security sensors ----
    def _handle_security(self, event):
        dev_obj = event.device
        pt, st, ident = dev_obj.packettype, dev_obj.subtype, dev_obj.id_string
        key = M.make_key(pt, st, ident)
        vals = event.values
        status = str(vals.get("Sensor Status", ""))
        if self._is_duplicate(key, status):
            return
        targets = self._targets(key)
        self._note_heard(key, pt, st, ident, "rfxSecurity", status)
        if not targets:
            created = self._maybe_autocreate(key, "rfxSecurity", control=True,
                                             battery=vals.get("Battery numeric") is not None)
            targets = [created] if created is not None else []
        for dev_id in targets:
            dev = indigo.devices[dev_id]
            states = self._seen_states(dev, vals.get("Rssi numeric"), vals.get("Battery numeric"))
            s = status.lower()
            if s in M.SECURITY_ON:
                states.append({"key": "onOffState", "value": True})
            elif s in M.SECURITY_OFF:
                states.append({"key": "onOffState", "value": False})
            states.append({"key": "status", "value": status})
            states.append({"key": "tamper", "value": bool(vals.get("Tamper"))})
            states.append({"key": "command", "value": status})
            states.append({"key": "lastCommand", "value": status})
            states += self._legacy_states(dev)
            self._schedule_clear(dev)
            self._update_states(dev, states)
        for dev_id in targets or [None]:
            self._fire("commandReceived", dev_id=dev_id, key=key, command=status)

    # ---- environmental sensors ----
    def _handle_sensor(self, event):
        dev_obj = event.device
        pt, st, ident = dev_obj.packettype, dev_obj.subtype, dev_obj.id_string
        key = M.make_key(pt, st, ident)
        raw_vals = event.values
        vals = M.extract_values(raw_vals)
        if not vals:
            return
        battery = raw_vals.get("Battery numeric")
        rssi = raw_vals.get("Rssi numeric")
        self.logger.debug("RX sensor {0} [{1}] {2}".format(M.protocol_name(pt, st), ident, vals))

        targets = self._targets(key)
        summary = ", ".join("{0}={1}".format(k, v) for k, v in list(vals.items())[:3])
        self._note_heard(key, pt, st, ident, "rfxSensor", summary, values=vals)
        if not targets:
            created = self._maybe_autocreate(key, "rfxSensor", control=False, value_keys=list(vals),
                                             battery=battery is not None)
            targets = [created] if created is not None else []
        for dev_id in targets:
            self._update_sensor(dev_id, vals, battery, rssi)

    def _update_sensor(self, dev_id, vals, battery, rssi):
        dev = indigo.devices[dev_id]
        if dev.deviceTypeId != "rfxSensor":
            return
        props = dev.pluginProps

        # new values reported (e.g. wind sensor's temperature) -> extend the state list
        known = [k for k in props.get("valueKeys", "").split(",") if k]
        missing = [k for k in vals if k not in known]
        needs_batt = battery is not None and not as_bool(props.get("SupportsBatteryLevel"))
        if missing or needs_batt:
            props["valueKeys"] = ",".join(known + missing)
            if needs_batt:
                props["SupportsBatteryLevel"] = True
            dev.replacePluginPropsOnServer(props)
            dev.stateListOrDisplayStateIdChanged()
            dev = indigo.devices[dev_id]
            props = dev.pluginProps
            known = known + missing

        units = self._unit_prefs()
        states = self._seen_states(dev, rssi, battery)
        states += self._legacy_states(dev)
        display = {}
        for sid, value in vals.items():
            conv, suffix, dec = M.convert(sid, value, units)
            entry = {"key": sid, "value": conv}
            if isinstance(conv, (int, float)) and not isinstance(conv, bool):
                entry["uiValue"] = "{0}{1}".format(conv, suffix)
                if dec is not None:
                    entry["decimalPlaces"] = dec
            states.append(entry)
            display[sid] = (conv, suffix, dec)

        preferred = props.get("primaryValue")
        primary = M.choose_primary(known, None if preferred in (None, "", AUTO_CHOICE) else preferred)
        if primary in display:
            conv, suffix, dec = display[primary]
            if isinstance(conv, (int, float)) and not isinstance(conv, bool):
                sv = {"key": "sensorValue", "value": conv, "uiValue": "{0}{1}".format(conv, suffix)}
                if dec is not None:
                    sv["decimalPlaces"] = dec
                states.append(sv)
        self._update_states(dev, states)
        self._set_sensor_image(dev, primary)

    def _set_sensor_image(self, dev, primary):
        name = {"temperature": "TemperatureSensor", "humidity": "HumiditySensor",
                "power": "EnergyMeterOn", "windSpeed": "WindSpeedSensor",
                "uvIndex": "LightSensor"}.get(primary, "Auto")
        sel = getattr(indigo.kStateImageSel, name, None) or indigo.kStateImageSel.Auto
        try:
            dev.updateStateImageOnServer(sel)
        except Exception:
            pass

    # ---- shared state helpers ----
    def _seen_states(self, dev, rssi=None, battery=None):
        states = [
            {"key": "lastSeen", "value": now_str()},
            {"key": "lastSeenTimestamp", "value": int(time.time())},
        ]
        if not dev.states.get("online", True):
            states.append({"key": "online", "value": True})
            self.logger.info("'{0}' is reporting again".format(dev.name))
            self._fire("deviceOnline", dev_id=dev.id)
        else:
            states.append({"key": "online", "value": True})
        if rssi is not None:
            states.append({"key": "signalStrength", "value": int(rssi)})
        pct = M.battery_percent(battery)
        if pct is not None and as_bool(dev.pluginProps.get("SupportsBatteryLevel")):
            states.append({"key": "batteryLevel", "value": pct})
        return states

    def _update_states(self, dev, states):
        valid = []
        for s in states:
            k = s["key"]
            if k in dev.states or k in ("batteryLevel",):
                valid.append(s)
            else:
                self.logger.debug("'{0}' has no state '{1}' - skipped".format(dev.name, k))
        if valid:
            dev.updateStatesOnServer(valid)

    def _remember_level(self, dev, level):
        props = dev.pluginProps
        if as_int(props.get("lastLevel")) != level:
            props["lastLevel"] = str(level)
            dev.replacePluginPropsOnServer(props)

    def _schedule_clear(self, dev):
        """Blank 'command' after the device's configured delay (0 = keep it)."""
        delay = as_float(dev.pluginProps.get("clearCommandSeconds"), 0)
        if delay > 0:
            self.pending_clear[dev.id] = time.time() + delay
        else:
            self.pending_clear.pop(dev.id, None)

    def _service_momentary(self):
        if not self.pending_off and not self.pending_clear:
            return
        now = time.time()
        for dev_id, when in list(self.pending_clear.items()):
            if now >= when:
                del self.pending_clear[dev_id]
                try:
                    indigo.devices[dev_id].updateStateOnServer("command", "")
                except KeyError:
                    pass
        for dev_id, when in list(self.pending_off.items()):
            if now >= when:
                del self.pending_off[dev_id]
                try:
                    dev = indigo.devices[dev_id]
                    dev.updateStateOnServer("onOffState", False)
                except KeyError:
                    pass

    def _health_check(self):
        now = time.time()
        for dev in indigo.devices.iter("self"):
            if dev.deviceTypeId not in RF_TYPES or not dev.enabled:
                continue
            mins = as_float(dev.pluginProps.get("offlineAfterMinutes"), 0)
            if mins <= 0 or not dev.states.get("online", True):
                continue
            last = as_int(dev.states.get("lastSeenTimestamp"), 0)
            if last and now - last > mins * 60:
                dev.updateStateOnServer("online", False)
                if as_bool(self.pluginPrefs.get("offlineAsError"), True):
                    try:
                        dev.setErrorStateOnServer("offline")
                    except Exception:
                        pass
                self.logger.warning("'{0}' hasn't reported for {1:.0f} minutes - marked offline".format(
                    dev.name, (now - last) / 60))
                self._fire("deviceOffline", dev_id=dev.id)

    # ------------------------------------------------------------------ migrated devices learning their code
    def _adopt_pending(self, key):
        adopted = []
        for dev_id, patterns in list(self.pending_patterns.items()):
            if not any(fnmatch.fnmatchcase(key, pat) for pat in patterns):
                continue
            try:
                dev = indigo.devices[dev_id]
            except KeyError:
                self.pending_patterns.pop(dev_id, None)
                continue
            pt, st, ident = M.split_key(key)
            props = dev.pluginProps
            props.update({"deviceKey": key, "protocol": "{0:02x}:{1:02x}".format(pt, st),
                          "idString": ident, "address": ident, "pendingPatterns": ""})
            dev.replacePluginPropsOnServer(props)
            self.pending_patterns.pop(dev_id, None)
            dev = indigo.devices[dev_id]
            self._register(dev)
            self.logger.info("'{0}' learned its code: {1} {2}".format(dev.name, M.protocol_name(pt, st), ident))
            adopted.append(dev_id)
        return self.devices_by_key.get(key) if adopted else None

    # ------------------------------------------------------------------ auto-creation
    def _ignored(self):
        return set(k for k in self.pluginPrefs.get("ignoredKeys", "").split("|") if k)

    def _add_ignored(self, key):
        ign = self._ignored()
        if key not in ign:
            ign.add(key)
            self.pluginPrefs["ignoredKeys"] = "|".join(sorted(ign))
            indigo.server.savePluginPrefs()

    def _maybe_autocreate(self, key, dtype, control, value_keys=None, battery=False):
        mode = self.pluginPrefs.get("autoCreate", "controls")
        if mode == "off" or key in self._ignored():
            return None
        if mode == "controls" and not control:
            return None
        info = self.discovered.get(key)
        need = as_int(self.pluginPrefs.get("minSightingsControls" if control else "minSightingsSensors"),
                      1 if control else 3)
        if info is None or info["count"] < need:
            if info is not None:
                self.logger.debug("{0}: heard {1}/{2} times before auto-create".format(key, info["count"], need))
            return None
        return self._create_device(key, dtype, value_keys=value_keys, battery=battery, auto=True)

    def _folder_id(self):
        name = (self.pluginPrefs.get("folderName") or "").strip()
        if not name:
            return 0
        try:
            if name not in indigo.devices.folders:
                indigo.devices.folder.create(name)
            return indigo.devices.folders[name].id
        except Exception:
            return 0

    def _unique_name(self, base):
        name = base
        n = 2
        while name in indigo.devices:
            name = "{0} ({1})".format(base, n)
            n += 1
        return name

    def _create_device(self, key, dtype, name=None, value_keys=None, battery=False, auto=False,
                       role=None, extra_props=None, pending=None, folder=None):
        if key:
            pt, st, ident = M.split_key(key)
            proto = "{0:02x}:{1:02x}".format(pt, st)
        else:                                   # migrated device that learns its code when first heard
            pt, st, ident, proto = 0, 0, "", ""
        props = {
            "deviceKey": key or "",
            "pendingPatterns": "|".join(pending or []),
            "protocol": proto,
            "idString": ident,
            "address": ident,
            "autoCreated": auto,
            "respondToGroup": True,
            "repeats": "1",
            "autoDimmer": True,
            "role": role or M.default_role(pt),
            "momentarySeconds": "5" if dtype == "rfxDoorbell" else ("1" if pt == 0x13 else "0"),
            "offlineAfterMinutes": "90" if dtype in ("rfxSensor",) else "0",
            "lastLevel": "100",
            "primaryValue": "",
            "SupportsStatusRequest": False,
        }
        if dtype in ("rfxSensor",):
            props.update({"SupportsSensorValue": True, "SupportsOnState": False,
                          "valueKeys": ",".join(value_keys or []),
                          "SupportsBatteryLevel": bool(battery)})
        elif dtype in ("rfxSecurity", "rfxDoorbell"):
            props.update({"SupportsSensorValue": False, "SupportsOnState": True,
                          "SupportsBatteryLevel": bool(battery)})
        if extra_props:
            props.update(extra_props)
        name = self._unique_name(name or "{0} {1}".format(M.short_protocol(pt, st), ident))
        desc = M.protocol_name(pt, st) if key else "waiting to hear its code"
        try:
            dev = indigo.device.create(protocol=indigo.kProtocol.Plugin, address=ident, name=name,
                                       description="{0} - {1}".format(desc, "auto-created" if auto else "created"),
                                       pluginId=self.pluginId, deviceTypeId=dtype, props=props,
                                       folder=self._folder_id() if folder is None else folder)
        except Exception as exc:
            self.logger.error("couldn't create device for {0}: {1}".format(key or name, exc))
            return None
        if key:
            self._register(dev)
            self.logger.info("{0} device '{1}' ({2})".format("auto-created" if auto else "created", dev.name,
                                                             M.protocol_name(pt, st)))
        else:
            self.pending_patterns[dev.id] = list(pending)
        return dev.id

    # ------------------------------------------------------------------ device lifecycle
    def _register(self, dev):
        key = dev.pluginProps.get("deviceKey")
        if not key:
            return
        primary = self.devices_by_key.get(key)
        if primary is None or primary == dev.id:
            self.devices_by_key[key] = dev.id
        else:
            extras = self.key_extras.setdefault(key, [])
            if dev.id not in extras:
                extras.append(dev.id)
                self.logger.debug("'{0}' shares its RF code with device {1}".format(dev.name, primary))
        try:
            pt, st, ident = M.split_key(key)
            base = M.group_base(pt, ident)
            if base is not None:
                self.groups.setdefault((pt, st, base), set()).add(dev.id)
        except ValueError:
            pass

    def _unregister(self, dev):
        for k, v in list(self.devices_by_key.items()):
            if v == dev.id:
                extras = self.key_extras.get(k) or []
                if extras:
                    self.devices_by_key[k] = extras.pop(0)
                else:
                    del self.devices_by_key[k]
        for extras in self.key_extras.values():
            if dev.id in extras:
                extras.remove(dev.id)
        for members in self.groups.values():
            members.discard(dev.id)

    def deviceStartComm(self, dev):
        dev.stateListOrDisplayStateIdChanged()
        if dev.deviceTypeId == INTERFACE_TYPE:
            self.interface_dev_id = dev.id
            dev.updateStateOnServer("connectionState", "connected" if self.connected else "disconnected")
            return
        if dev.deviceTypeId not in RF_TYPES:
            return
        if as_bool(dev.pluginProps.get("dbMigrated")):
            # converted offline from the old plugin: re-save props so Indigo refreshes the device
            props = dev.pluginProps
            props["dbMigrated"] = False
            dev.replacePluginPropsOnServer(props)
            dev = indigo.devices[dev.id]
            self.logger.info("'{0}' converted from the old RFXCOM plugin".format(dev.name))
        if not dev.pluginProps.get("deviceKey"):
            patterns = [p for p in dev.pluginProps.get("pendingPatterns", "").split("|") if p]
            if patterns:
                self.pending_patterns[dev.id] = patterns
                self.logger.debug("'{0}' is waiting to hear its code ({1})".format(dev.name, ", ".join(patterns)))
                return
            self.logger.error("'{0}' has no RF code configured - edit the device".format(dev.name))
            return
        self._register(dev)

    def deviceStopComm(self, dev):
        if dev.deviceTypeId == INTERFACE_TYPE:
            if self.interface_dev_id == dev.id:
                self.interface_dev_id = None
            return
        self._unregister(dev)
        self.pending_patterns.pop(dev.id, None)
        self.pending_off.pop(dev.id, None)
        self.pending_clear.pop(dev.id, None)

    def deviceDeleted(self, dev):
        super().deviceDeleted(dev)
        key = dev.pluginProps.get("deviceKey")
        if key and dev.deviceTypeId in RF_TYPES and as_bool(self.pluginPrefs.get("ignoreDeleted"), True) \
                and self.pluginPrefs.get("autoCreate", "controls") != "off":
            self._add_ignored(key)
            self.logger.info("'{0}' deleted - its RF code won't be auto-created again "
                             "(Plugins > RFXCom433 > Manage Ignored Codes to undo)".format(dev.name))

    def getDeviceStateList(self, dev):
        states = super().getDeviceStateList(dev)
        if dev.deviceTypeId in RF_TYPES and as_bool(dev.pluginProps.get("legacyStates")):
            states.append(self.getDeviceStateDictForStringType("lastUpdated", "Last Updated (legacy)",
                                                               "Last Updated (legacy)"))
        if dev.deviceTypeId == "rfxSensor":
            for sid in [k for k in dev.pluginProps.get("valueKeys", "").split(",") if k]:
                label = M.STATE_LABELS.get(sid, sid)
                if sid in M.STRING_STATES:
                    states.append(self.getDeviceStateDictForStringType(sid, label, label))
                else:
                    states.append(self.getDeviceStateDictForNumberType(sid, label, label))
        return states

    def _ensure_interface_device(self):
        for dev in indigo.devices.iter("self." + INTERFACE_TYPE):
            self.interface_dev_id = dev.id
            return
        try:
            dev = indigo.device.create(protocol=indigo.kProtocol.Plugin, name=self._unique_name("RFXtrx Interface"),
                                       description="RFXCom433 transceiver status", pluginId=self.pluginId,
                                       deviceTypeId=INTERFACE_TYPE, props={}, folder=self._folder_id())
            self.interface_dev_id = dev.id
        except Exception as exc:
            self.logger.debug("couldn't create interface device: {0}".format(exc))

    def _set_interface_states(self, states):
        if not self.interface_dev_id:
            return
        try:
            dev = indigo.devices[self.interface_dev_id]
        except KeyError:
            self.interface_dev_id = None
            return
        valid = [s for s in states if s["key"] in dev.states]
        if valid:
            dev.updateStatesOnServer(valid)

    # ------------------------------------------------------------------ device config UI
    def protocolList(self, filter="", valuesDict=None, typeId="", targetId=0):
        out = []
        for pt, cls in sorted(lowlevel.PACKET_TYPES.items()):
            if pt in (0x01, 0x03):
                continue
            want_tx = filter == "transmit"
            if want_tx and not hasattr(cls, "parse_id"):
                continue
            if filter == "sensor" and hasattr(cls, "parse_id") and pt not in M.SECURITY_TYPES:
                continue
            for st, label in sorted(getattr(cls, "TYPES", {}).items()):
                out.append(("{0:02x}:{1:02x}".format(pt, st), "{0} - {1}".format(label, cls.__name__)))
        return out

    def _owner_names(self, key):
        names = []
        for dev_id in [self.devices_by_key.get(key)] + list(self.key_extras.get(key, ())):
            if dev_id and dev_id in indigo.devices:
                names.append(indigo.devices[dev_id].name)
        return names

    def heardList(self, filter="", valuesDict=None, typeId="", targetId=0):
        out = [(NO_CHOICE, "- choose a recently heard code -")]
        items = sorted(self.discovered.values(), key=lambda d: -d["lastSeen"])
        for d in items:
            ago = int((time.time() - d["lastSeen"]) / 60)
            label = "{0} {1} - {2} ({3}x, {4} min ago)".format(
                d["short"], d["id"], d.get("summary", ""), d["count"], ago)
            owners = [n for n in self._owner_names(d["key"])
                      if not (targetId and targetId in indigo.devices and indigo.devices[targetId].name == n)]
            if owners:
                label += " - used by '{0}'".format("', '".join(owners))
            out.append((d["key"], label))
        return out

    def useHeardCode(self, valuesDict, typeId="", devId=0):
        key = valuesDict.get("heardKey", "")
        if key and key != NO_CHOICE:
            pt, st, ident = M.split_key(key)
            valuesDict["protocol"] = "{0:02x}:{1:02x}".format(pt, st)
            valuesDict["idString"] = ident
        return valuesDict

    def primaryValueList(self, filter="", valuesDict=None, typeId="", targetId=0):
        out = [(AUTO_CHOICE, "Automatic")]
        keys = []
        if targetId:
            try:
                keys = [k for k in indigo.devices[targetId].pluginProps.get("valueKeys", "").split(",") if k]
            except KeyError:
                pass
        for k in keys:
            if k not in M.STRING_STATES:
                out.append((k, M.STATE_LABELS.get(k, k)))
        return out

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errors = indigo.Dict()
        if typeId not in RF_TYPES:
            return True, valuesDict
        proto = valuesDict.get("protocol", "")
        ident = (valuesDict.get("idString") or "").strip()
        if not ident and valuesDict.get("pendingPatterns"):
            return True, valuesDict             # migrated device still waiting to hear its code
        if ident:
            valuesDict["pendingPatterns"] = ""
        if not proto:
            errors["protocol"] = "Choose a protocol"
        if not ident:
            errors["idString"] = "Enter the RF id, or pick a recently heard code"
        if not len(errors):
            try:
                pt, st = [int(x, 16) for x in proto.split(":")]
            except ValueError:
                errors["protocol"] = "Invalid protocol"
            else:
                if typeId in SWITCH_TYPES or typeId == "rfxDoorbell":
                    try:
                        RFXtrx.get_device(pt, st, ident)
                    except (ValueError, IndexError) as exc:
                        errors["idString"] = "Not a valid id for this protocol ({0})".format(exc)
                if not len(errors):
                    key = M.make_key(pt, st, ident)
                    valuesDict["deviceKey"] = key
                    valuesDict["address"] = ident
        for fid in ("offlineAfterMinutes", "momentarySeconds", "clearCommandSeconds"):
            if fid in valuesDict and as_float(valuesDict.get(fid), -1) < 0:
                errors[fid] = "Enter 0 or more"
        if typeId == "rfxSensor":
            valuesDict["SupportsSensorValue"] = True
            valuesDict["SupportsOnState"] = False
        elif typeId in ("rfxSecurity", "rfxDoorbell"):
            valuesDict["SupportsOnState"] = True
            valuesDict["SupportsSensorValue"] = False
        if len(errors):
            return False, valuesDict, errors
        return True, valuesDict

    # ------------------------------------------------------------------ transmit
    def actionControlDevice(self, action, dev):
        a = action.deviceAction
        t = dev.deviceTypeId
        K = indigo.kDeviceAction
        on_label, off_label = ("open", "close") if t == "rfxCover" else ("on", "off")

        if a in (K.TurnOn, getattr(K, "AllLightsOn", None)):
            self._command(dev, "on", label=on_label)
        elif a in (K.TurnOff, getattr(K, "AllLightsOff", None), getattr(K, "AllOff", None)):
            self._command(dev, "off", label=off_label)
        elif a == K.Toggle:
            if dev.onState:
                self._command(dev, "off", label=off_label)
            else:
                self._command(dev, "on", label=on_label)
        elif a == K.SetBrightness:
            self._set_level(dev, int(action.actionValue))
        elif a == K.BrightenBy:
            self._set_level(dev, min(100, dev.brightness + int(action.actionValue)))
        elif a == K.DimBy:
            self._set_level(dev, max(0, dev.brightness - int(action.actionValue)))
        elif a == K.RequestStatus:
            self.logger.info("'{0}' is a one-way RF device - its state is what was last sent or heard".format(dev.name))

    def actionControlUniversal(self, action, dev):
        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            self.logger.info("'{0}': RF devices can't be polled".format(dev.name))

    def _set_level(self, dev, level):
        if dev.deviceTypeId != "rfxDimmer":
            self._command(dev, "on" if level > 0 else "off")
            return
        if level <= 0:
            self._command(dev, "off")
        else:
            self._command(dev, "dim", level, label="set brightness to {0}".format(level))

    def _states_for(self, dev, op, arg):
        t = dev.deviceTypeId
        if op == "on":
            if t == "rfxDimmer":
                return [{"key": "onOffState", "value": True},
                        {"key": "brightnessLevel", "value": as_int(dev.pluginProps.get("lastLevel"), 100) or 100}]
            if t == "rfxCover":
                return [{"key": "onOffState", "value": True}, {"key": "coverState", "value": "open"}]
            return [{"key": "onOffState", "value": True}]
        if op == "off":
            if t == "rfxCover":
                return [{"key": "onOffState", "value": False}, {"key": "coverState", "value": "closed"}]
            return [{"key": "onOffState", "value": False}]
        if op == "dim":
            return [{"key": "brightnessLevel", "value": int(arg)}]
        if op == "stop":
            return [{"key": "coverState", "value": "stopped"}]
        if op == "group_on":
            return [{"key": "onOffState", "value": True}]
        if op == "group_off":
            return [{"key": "onOffState", "value": False}]
        return []

    def _command(self, dev, op, arg=None, label=None):
        role = dev.pluginProps.get("role", "switch")
        states = self._states_for(dev, op, arg)
        if role == "button":
            # receive-only device: behave like a virtual switch, don't transmit
            self._update_states(dev, states)
            self.logger.info('"{0}" {1} (receive-only, nothing transmitted)'.format(dev.name, label or op))
            if op == "dim" and arg:
                self._remember_level(dev, int(arg))
            return
        self.txq.put(TxJob(dev.id, op, arg, states, label or op, cover=dev.deviceTypeId == "rfxCover"))

    def _tx_worker(self):
        while not self.stop_event.is_set():
            try:
                job = self.txq.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run_job(job)
            except Exception:
                self.logger.exception("transmit error")

    def _wait_connected(self, seconds):
        end = time.time() + seconds
        while time.time() < end and not self.stop_event.is_set():
            if self.connected and self.transport is not None:
                return True
            time.sleep(0.2)
        return self.connected and self.transport is not None

    def _run_job(self, job):
        if not self._wait_connected(10):
            name = indigo.devices[job.dev_id].name if job.dev_id else "raw packet"
            self.logger.error('send "{0}" {1} failed - not connected to the RFXtrx'.format(name, job.label))
            self._count_tx_failure()
            return
        transport = self.transport

        if job.op == "raw":
            transport.send(job.arg)
            ok, detail = transport.tx_result()
            self.logger.info("sent raw packet {0} - {1}".format(job.arg.hex(" ").upper(), detail))
            return

        try:
            dev = indigo.devices[job.dev_id]
        except KeyError:
            return
        if not dev.pluginProps.get("deviceKey"):
            self.logger.error("'{0}' hasn't learned its RF code yet - press its remote button once".format(dev.name))
            return
        pt, st, ident = M.split_key(dev.pluginProps["deviceKey"])
        repeats = max(1, min(5, as_int(dev.pluginProps.get("repeats"), 1)))
        if job.op == "pair":
            repeats = max(3, repeats)

        try:
            rfx = RFXtrx.get_device(pt, st, ident)
        except ValueError as exc:
            self.logger.error("'{0}' can't transmit: {1}".format(dev.name, exc))
            return

        ok, detail = True, ""
        try:
            for i in range(repeats):
                self._transmit(rfx, transport, job, pt, st)
                ok, detail = transport.tx_result()
                if not ok:
                    break
                if i < repeats - 1:
                    time.sleep(1.0 if job.op == "pair" else 0.25)
        except RFXtrxTransportError as exc:
            self.logger.error('send "{0}" {1} failed: {2}'.format(dev.name, job.label, exc))
            self._count_tx_failure()
            return
        except (ValueError, AttributeError) as exc:
            self.logger.error('send "{0}" {1} failed: this device type can\'t do that ({2})'.format(
                dev.name, job.label, exc))
            return

        if not ok:
            self.logger.error('send "{0}" {1} failed: {2}'.format(dev.name, job.label, detail))
            self._count_tx_failure()
            return

        self.logger.info('sent "{0}" {1}'.format(dev.name, job.label))
        if job.op == "dim" and job.arg:
            self._remember_level(dev, int(job.arg))
            dev = indigo.devices[job.dev_id]
        if job.states:
            self._update_states(dev, job.states + [{"key": "lastCommand", "value": "sent: " + job.label}])
        if job.op in ("group_on", "group_off"):
            base = M.group_base(pt, ident)
            for gid in self.groups.get((pt, st, base), ()):
                if gid != dev.id:
                    try:
                        g = indigo.devices[gid]
                        if as_bool(g.pluginProps.get("respondToGroup"), True):
                            self._update_states(g, [{"key": "onOffState", "value": job.op == "group_on"}])
                    except KeyError:
                        pass
        self._set_interface_states([{"key": "packetsSent", "value": transport.packets_out}])

    def _transmit(self, rfx, transport, job, pt, st):
        op = job.op
        is_dd = isinstance(rfx, RFXtrx.DDxxxxDevice)
        if op in ("on", "pair"):
            if is_dd:
                rfx.send_up(transport)
            elif job.cover and hasattr(rfx, "send_open"):
                rfx.send_open(transport)
            else:
                rfx.send_on(transport)
        elif op == "off":
            if is_dd:
                rfx.send_down(transport)
            elif job.cover and hasattr(rfx, "send_close"):
                rfx.send_close(transport)
            else:
                rfx.send_off(transport)
        elif op == "dim":
            rfx.send_dim(transport, int(job.arg))
        elif op == "stop":
            rfx.send_stop(transport)
        elif op in ("group_on", "group_off"):
            on_code, off_code = M.group_command_codes(pt, st)
            code = on_code if op == "group_on" else off_code
            if code is None:
                raise ValueError("this protocol has no group {0} command".format(op.split("_")[1]))
            rfx.send_command(transport, code)
        elif op == "lwcmd":
            rfx.send_command(transport, int(job.arg))
        elif op == "chime":
            rfx.send_command(transport, int(job.arg))
        else:
            raise ValueError("unknown operation " + op)

    def _count_tx_failure(self):
        if not self.interface_dev_id:
            return
        try:
            dev = indigo.devices[self.interface_dev_id]
            self._set_interface_states([{"key": "txFailures", "value": as_int(dev.states.get("txFailures")) + 1}])
        except KeyError:
            pass

    # ------------------------------------------------------------------ custom actions
    def actionGroup(self, action, dev):
        op = "group_on" if action.pluginTypeId == "groupOn" else "group_off"
        self._command(dev, op, label=op.replace("_", " "))

    def actionLightwave(self, action, dev):
        if dev.pluginProps.get("protocol") != "14:00":
            self.logger.error("'{0}' isn't a LightwaveRF device".format(dev.name))
            return
        cmd = action.props.get("lwCommand", "mood1")
        code = M.LIGHTWAVE_COMMANDS.get(cmd)
        if code is None:
            return
        self.txq.put(TxJob(dev.id, "lwcmd", code, [], "LightwaveRF " + cmd))

    def actionStop(self, action, dev):
        self._command(dev, "stop", label="stop")

    def actionPair(self, action, dev):
        self.logger.info("sending pairing signal from '{0}' - the receiver must be in learn mode".format(dev.name))
        self.txq.put(TxJob(dev.id, "pair", None, self._states_for(dev, "on", None), "pairing signal (on)"))

    def actionChime(self, action, dev):
        raw = (action.props.get("sound") or "").strip()
        default = as_int(dev.pluginProps.get("chimeSound"), 1)
        sound = as_int(raw, default) if raw else default
        self.txq.put(TxJob(dev.id, "chime", sound, [], "chime sound {0}".format(sound)))

    def actionRaw(self, action, dev=None):
        try:
            pkt = M.parse_hex_packet(action.props.get("packet", ""))
        except ValueError as exc:
            self.logger.error("raw packet: {0}".format(exc))
            return
        self.txq.put(TxJob(0, "raw", pkt, [], "raw"))

    def actionReconnect(self, action=None, dev=None):
        self.logger.info("reconnecting to the RFXtrx")
        self._disconnect("reconnect requested")
        self.fail_count = 0
        self.next_attempt = 0

    def validateActionConfigUi(self, valuesDict, typeId, devId):
        errors = indigo.Dict()
        if typeId == "sendRaw":
            try:
                M.parse_hex_packet(valuesDict.get("packet", ""))
            except ValueError as exc:
                errors["packet"] = str(exc)
        if typeId == "chimeRing":
            if not 0 <= as_int(valuesDict.get("sound"), -2) <= 15 and valuesDict.get("sound", "") != "":
                errors["sound"] = "0-15, or blank for the device default"
        if len(errors):
            return False, valuesDict, errors
        return True, valuesDict

    # ------------------------------------------------------------------ triggers
    def triggerStartProcessing(self, trigger):
        self.triggers[trigger.id] = trigger

    def triggerStopProcessing(self, trigger):
        self.triggers.pop(trigger.id, None)

    def rfDeviceList(self, filter="", valuesDict=None, typeId="", targetId=0):
        out = [("any", "- any RFXCom433 device -")]
        for dev in sorted(indigo.devices.iter("self"), key=lambda d: d.name.lower()):
            if dev.deviceTypeId in RF_TYPES:
                out.append((str(dev.id), dev.name))
        return out

    def _fire(self, event_type, dev_id=None, key=None, command=None):
        for trig in list(self.triggers.values()):
            if trig.pluginTypeId != event_type:
                continue
            p = trig.pluginProps
            want_dev = p.get("deviceId", "any")
            if want_dev not in ("", "any", None):
                if dev_id is None or str(dev_id) != str(want_dev):
                    continue
            if event_type == "commandReceived":
                want = p.get("command", "any")
                c = (command or "").lower()
                if want == "text":
                    needle = (p.get("commandText") or "").strip().lower()
                    if needle and needle not in c:
                        continue
                elif want not in ("", "any"):
                    if c not in M.TRIGGER_COMMANDS.get(want, ()):
                        continue
                if p.get("deviceId", "any") in ("", "any") and dev_id is None \
                        and not as_bool(p.get("includeUnknown")):
                    continue
            try:
                indigo.trigger.execute(trig)
            except Exception as exc:
                self.logger.debug("trigger '{0}' failed: {1}".format(trig.name, exc))

    # ------------------------------------------------------------------ menu items
    def menuToggleDebug(self, valuesDict=None, typeId=""):
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        self._apply_log_level()
        self.logger.info("debug logging {0}".format("on" if self.debug else "off"))

    def menuStatus(self, valuesDict=None, typeId=""):
        i = self.status_info
        self.logger.info("RFXtrx: {0}".format("connected to " + self._port_url() if self.connected else "not connected"))
        if i:
            self.logger.info("  type: {0}".format(i.get("transceiverType")))
            self.logger.info("  firmware: {0} {1}".format(i.get("firmwareType"), i.get("firmwareVersion")))
            if i.get("hardwareVersion"):
                self.logger.info("  hardware: {0}".format(i.get("hardwareVersion")))
            if i.get("noiseLevel") is not None:
                self.logger.info("  noise level: {0}".format(i.get("noiseLevel")))
            self.logger.info("  receive protocols: {0}".format(", ".join(i.get("enabledProtocols", []))))
        self.logger.info("  RF devices: {0}, codes heard but not created: {1}, ignored codes: {2}".format(
            len(self.devices_by_key), len([k for k in self.discovered if k not in self.devices_by_key]),
            len(self._ignored())))

    def menuHeard(self, valuesDict=None, typeId=""):
        rows = [d for d in sorted(self.discovered.values(), key=lambda d: -d["lastSeen"])]
        if not rows:
            self.logger.info("no RF codes heard yet this session")
            return
        self.logger.info("RF codes heard this session:")
        for d in rows:
            dev_id = self.devices_by_key.get(d["key"])
            owner = indigo.devices[dev_id].name if dev_id and dev_id in indigo.devices else (
                "ignored" if d["key"] in self._ignored() else "no device")
            self.logger.info("  {0:28} {1:16} {2:>4}x  {3}  [{4}]".format(
                d["protocol"][:28], d["id"], d["count"], d.get("summary", ""), owner))

    # Create from heard
    def createFromHeard(self, valuesDict, typeId=""):
        key = valuesDict.get("heardKey", "")
        if not key or key == NO_CHOICE:
            valuesDict["createResult"] = "Choose a code first."
            return valuesDict
        d = self.discovered.get(key, {})
        dtype = valuesDict.get("createType", "auto")
        role = None
        if dtype == "auto":
            dtype = d.get("deviceType", "rfxSwitch")
        elif dtype == "button":
            dtype, role = "rfxSwitch", "button"
        name = (valuesDict.get("createName") or "").strip() or None
        dev_id = self._create_device(key, dtype, name=name, value_keys=list((d.get("values") or {}).keys()),
                                     role=role)
        if dev_id:
            self._unignore(key)
            valuesDict["createResult"] = "Created '{0}'.".format(indigo.devices[dev_id].name)
            valuesDict["heardKey"] = NO_CHOICE
            valuesDict["createName"] = ""
        else:
            valuesDict["createResult"] = "Failed - see the Indigo log."
        return valuesDict

    def _unignore(self, key):
        ign = self._ignored()
        if key in ign:
            ign.discard(key)
            self.pluginPrefs["ignoredKeys"] = "|".join(sorted(ign))
            indigo.server.savePluginPrefs()

    # Ignored codes
    def ignoredList(self, filter="", valuesDict=None, typeId="", targetId=0):
        return [(k, k) for k in sorted(self._ignored())] or [(NO_CHOICE, "(nothing ignored)")]

    def unignoreSelected(self, valuesDict, typeId=""):
        for k in valuesDict.get("ignoredSelection", []) or []:
            if k != NO_CHOICE:
                self._unignore(k)
        valuesDict["ignoredSelection"] = []
        return valuesDict

    def unignoreAll(self, valuesDict, typeId=""):
        self.pluginPrefs["ignoredKeys"] = ""
        indigo.server.savePluginPrefs()
        return valuesDict

    # New transmitter code
    def createNewCode(self, valuesDict, typeId=""):
        proto = valuesDict.get("newProtocol", "ac")
        unit = as_int(valuesDict.get("newUnit"), 0)
        if not 1 <= unit <= 16:
            valuesDict["newResult"] = "Unit must be 1-16."
            return valuesDict
        pt, st, label = M.NEW_CODE_PROTOCOLS[proto]
        for _ in range(20):
            ident = M.random_id_string(proto, unit)
            key = M.make_key(pt, st, ident)
            if key not in self.devices_by_key:
                break
        dtype = "rfxDimmer" if as_bool(valuesDict.get("newDimmable")) and pt in M.DIMMABLE_TYPES else "rfxSwitch"
        name = (valuesDict.get("newName") or "").strip() or "{0} {1}".format(label, ident)
        dev_id = self._create_device(key, dtype, name=name, role="switch")
        if dev_id:
            dev = indigo.devices[dev_id]
            valuesDict["newResult"] = ("Created '{0}'. Put the receiver into learn mode, then use the "
                                       "'Send Pairing Signal' action or turn the device on.").format(dev.name)
            self.logger.info("new {0} code {1} created as '{2}' - put the receiver into learn mode, then "
                             "turn '{2}' on (or run 'Send Pairing Signal')".format(label, ident, dev.name))
        else:
            valuesDict["newResult"] = "Failed - see the Indigo log."
        return valuesDict

    def menuReconnect(self, valuesDict=None, typeId=""):
        self.actionReconnect()

    # ------------------------------------------------------------------ migration from the old RFXCOM plugin
    def _old_devices(self):
        return [d for d in indigo.devices.iter() if getattr(d, "pluginId", "") == MIG.OLD_PLUGIN_ID]

    @staticmethod
    def _old_props(dev):
        props = None
        try:
            props = dev.ownerProps
        except AttributeError:
            pass
        if not props:
            try:
                props = dev.globalProps.get(MIG.OLD_PLUGIN_ID, {})
            except Exception:
                props = {}
        return dict(props)

    @staticmethod
    def _usage(dev_id):
        """Short description of what references a device, if Indigo can tell us."""
        try:
            deps = indigo.device.getDependencies(dev_id)
        except Exception:
            return ""
        parts = []
        try:
            for kind, items in dict(deps).items():
                n = len(items) if hasattr(items, "__len__") else 0
                if n:
                    parts.append("{0} {1}".format(n, kind))
        except Exception:
            return ""
        return ", ".join(parts)

    def _migration_plans(self):
        done = {}
        for d in indigo.devices.iter("self"):
            src = as_int(d.pluginProps.get("migratedFrom"), 0)
            if src:
                done[src] = d.id
        plans = [MIG.plan_device(d.id, d.name, d.deviceTypeId, self._old_props(d), dict(d.states))
                 for d in self._old_devices()]
        return plans, done

    def migratePreview(self, valuesDict, typeId=""):
        plans, done = self._migration_plans()
        if not plans:
            valuesDict["migrateResult"] = "No devices from the old RFXCOM plugin were found."
            return valuesDict
        self.logger.info("Migration preview - {0} device(s) from the old RFXCOM plugin:".format(len(plans)))
        todo = skipped = 0
        for p in sorted(plans, key=lambda x: x.old_name.lower()):
            used = self._usage(p.old_id)
            if p.old_id in done:
                line = "already migrated"
            else:
                line = p.describe()
                if p.supported:
                    todo += 1
                elif not p.supported:
                    skipped += 1
            self.logger.info("  '{0}' ({1}) -> {2}{3}".format(p.old_name, p.old_type, line,
                                                             "  [used by: {0}]".format(used) if used else ""))
        valuesDict["migrateResult"] = "{0} to migrate, {1} not supported, {2} already done - details in the log.".format(
            todo, skipped, len([p for p in plans if p.old_id in done]))
        return valuesDict

    def migrateRun(self, valuesDict, typeId=""):
        plans, done = self._migration_plans()
        rename = as_bool(valuesDict.get("migrateRename"), True)
        copy_states = as_bool(valuesDict.get("migrateStates"), True)
        created = reused = failed = 0
        new_for_old = {}
        for p in plans:
            if p.old_id in done or not p.supported:
                continue
            try:
                old = indigo.devices[p.old_id]
            except KeyError:
                continue
            name = old.name
            new_name = name + " (RFXCom433)"
            if rename:
                try:
                    old.name = name + " (old)"
                    old.replaceOnServer()
                    new_name = name
                except Exception as exc:
                    self.logger.debug("couldn't rename '{0}': {1}".format(name, exc))
            extra = dict(p.extra_props)
            extra["migratedFrom"] = str(p.old_id)
            extra["role"] = p.role
            folder = getattr(old, "folderId", 0) or 0

            existing = self.devices_by_key.get(p.key) if p.key else None
            if existing and indigo.devices[existing].pluginProps.get("migratedFrom"):
                existing = None                 # already holds another migrated device; share the code
            if existing:
                dev = indigo.devices[existing]
                props = dev.pluginProps
                props.update(extra)
                dev.replacePluginPropsOnServer(props)
                try:
                    dev = indigo.devices[existing]
                    dev.name = self._unique_name(new_name) if dev.name != new_name else new_name
                    dev.replaceOnServer()
                except Exception:
                    pass
                dev_id = existing
                reused += 1
                self.logger.info("'{0}' -> using the device already auto-created for that code".format(name))
            else:
                dev_id = self._create_device(p.key, p.new_type, name=new_name, role=p.role, extra_props=extra,
                                             pending=None if p.key else p.patterns, folder=folder)
                if dev_id is None:
                    failed += 1
                    continue
                created += 1
                self.logger.info("'{0}' -> {1}".format(name, p.describe()))
            new_for_old[p.old_id] = dev_id

            if copy_states:
                try:
                    dev = indigo.devices[dev_id]
                    carry = []
                    for k in ("onOffState", "brightnessLevel", "lastUpdated") + MIG.SENSOR_STATE_CARRY:
                        if k in old.states and k in dev.states:
                            carry.append({"key": k, "value": old.states[k]})
                    if carry:
                        dev.updateStatesOnServer(carry)
                except Exception:
                    pass

        msg = "Created {0}, reused {1}, failed {2}.".format(created, reused, failed)
        if created or reused:
            msg += " Triggers, action groups and control pages still point at the old devices - see the log."
            self.logger.warning("migration done - repoint triggers, action groups and control pages from the "
                                "'(old)' devices to the new ones, then disable/delete the old RFXCOM plugin")
        valuesDict["migrateResult"] = msg
        return valuesDict

    # ------------------------------------------------------------------ firmware
    def firmwareCheck(self, valuesDict, typeId=""):
        ok, msg, _blocks, _rng = self._firmware_preflight(valuesDict.get("firmwarePath", ""))
        valuesDict["firmwareResult"] = msg
        valuesDict["firmwareChecked"] = ok
        return valuesDict

    def _firmware_preflight(self, path):
        path = os.path.expanduser((path or "").strip())
        url = self._port_url()
        if not path or not os.path.isfile(path):
            return False, "File not found. Enter the full path to the .hex file.", None, None
        if is_network_url(url):
            return False, "Firmware updates are only allowed over a local USB connection.", None, None
        reason = FW.check_hardware(self.status_info.get("transceiverTypeCode"),
                                   self.status_info.get("firmwareTypeCode"))
        if reason:
            return False, "Can't flash: " + reason + ".", None, None
        try:
            blocks = FW.parse_hex(path)
        except (FW.FirmwareError, OSError, UnicodeDecodeError) as exc:
            return False, "Invalid firmware file: {0}".format(exc), None, None
        rng = FW.pm_range_for(self.status_info.get("transceiverTypeCode"))
        s = FW.summarise(blocks, rng)
        name = os.path.basename(path).lower()
        warn = ""
        if self.status_info.get("transceiverTypeCode") in FW.FLASHABLE_433_TYPES and "868" in name:
            return False, "This looks like 868 MHz firmware but the unit is a 433 MHz model.", None, None
        if self.status_info.get("transceiverTypeCode") in FW.FLASHABLE_868_TYPES and "433" in name:
            return False, "This looks like 433 MHz firmware but the unit is an 868 MHz model.", None, None
        if s["outside"]:
            warn = (" Note: {0} block(s) lie outside the program range 0x{1:06X}-0x{2:06X}; they'll be "
                    "written as-is.").format(len(s["outside"]), rng[0], rng[1])
        msg = ("OK: {0} blocks, 0x{1:06X}-0x{2:06X}, for your {3} (currently {4} {5}).{6}").format(
            s["blocks"], s["low"], s["high"], self.status_info.get("transceiverType"),
            self.status_info.get("firmwareType"), self.status_info.get("firmwareVersion"), warn)
        return True, msg, blocks, rng

    def firmwareStart(self, valuesDict, typeId=""):
        if self.flashing:
            valuesDict["firmwareResult"] = "An update is already running."
            return valuesDict
        if not as_bool(valuesDict.get("firmwareConfirm")):
            valuesDict["firmwareResult"] = "Tick the confirmation box first."
            return valuesDict
        ok, msg, blocks, rng = self._firmware_preflight(valuesDict.get("firmwarePath", ""))
        if not ok:
            valuesDict["firmwareResult"] = msg
            return valuesDict
        url = self._port_url()
        self.flashing = True
        self._disconnect("firmware update")
        self._set_interface_states([{"key": "connectionState", "value": "updating firmware"},
                                    {"key": "firmwareUpdateStatus", "value": "starting"}])
        self.logger.warning("RFXtrx firmware update starting - do not unplug the RFXtrx or restart Indigo")
        self.flash_thread = threading.Thread(target=self._flash_worker, args=(url, blocks, rng),
                                             name="RFXCOM-flash", daemon=True)
        self.flash_thread.start()
        valuesDict["firmwareResult"] = "Update started - progress is shown in the Indigo log and on the interface device."
        valuesDict["firmwareConfirm"] = False
        return valuesDict

    def _flash_worker(self, url, blocks, rng):
        def log(msg, error=False):
            (self.logger.error if error else self.logger.info)("firmware: " + msg)

        last = {"pct": -10}

        def progress(pct, msg):
            self._set_interface_states([{"key": "firmwareUpdateStatus",
                                         "value": "{0}% {1}".format(pct, msg) if pct >= 0 else "failed"}])
            if pct >= last["pct"] + 10 or pct in (100, -1):
                last["pct"] = pct
                if pct >= 0:
                    self.logger.info("firmware: {0}% ({1})".format(pct, msg))

        ok = False
        try:
            time.sleep(1.5)     # let the port settle after closing the normal connection
            ok = FW.Bootloader(url, log=log, progress=progress).flash(blocks, rng, stop_event=self.stop_event)
        except FW.FirmwareError as exc:
            self.logger.error("firmware update failed: {0}".format(exc))
        except Exception:
            self.logger.exception("firmware update failed")
        finally:
            self.flashing = False
            self.fail_count = 0
            self.next_attempt = time.time() + 3
            if ok:
                self.logger.info("firmware update complete - reconnecting")
                if self._configured_modes() is None:
                    self.logger.warning("receiver protocol settings are reset by a firmware update - enable "
                                        "'Manage receiver protocols' in the plugin settings to have them re-applied")
            self._set_interface_states([{"key": "firmwareUpdateStatus",
                                         "value": "completed " + now_str() if ok else "failed " + now_str()}])
            self._fire("firmwareFinished")
