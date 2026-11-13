"""
Publishes each user's training state to Home Assistant over MQTT (discovery), with a minimal MQTT 3.1.1
client so the add-on stays dependency free. One short connection per batch: connect, publish with QoS 1
(wait for each ack), disconnect.

Broker settings come from the Supervisor (services: mqtt:need in the add-on manifest) or, for development,
from INNODIGYM_MQTT_HOST / _PORT / _USER / _PASSWORD. Without either, publishing is switched off.

Topics (prefix defaults to "homeassistant"):
  <prefix>/<component>/innodigym_<uid>/<key>/config   retained discovery, one device per user
  innodigym/<uid>/state                               retained JSON with every sensor value
  innodigym/<uid>/workout_finished                    not retained, JSON; also backs an `event` entity
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import struct
import urllib.request
from datetime import date

import db
import stats

SUPERVISOR_URL = "http://supervisor/services/mqtt"
EVENT_WINDOW_HOURS = 48


class MqttError(Exception):
    pass


def _utf(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        d, n = n % 128, n // 128
        out.append(d | (128 if n else 0))
        if not n:
            return bytes(out)


class MqttClient:
    def __init__(self, host, port=1883, username=None, password=None, use_tls=False,
                 client_id="innodigym-addon", timeout=10):
        self.host, self.port, self.username, self.password = host, int(port), username, password
        self.use_tls, self.client_id, self.timeout = use_tls, client_id, timeout
        self.sock, self._pid = None, 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise MqttError("broker closed the connection")
            buf += chunk
        return buf

    def _packet(self):
        first = self._read(1)[0]
        mult, length = 1, 0
        while True:
            b = self._read(1)[0]
            length += (b & 127) * mult
            if not b & 128:
                break
            mult *= 128
        return first, self._read(length)

    def connect(self):
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host) if self.use_tls else raw
        except (OSError, ssl.SSLError) as e:
            raise MqttError("cannot reach the broker at %s:%s (%s)" % (self.host, self.port, e))
        try:
            self._handshake()
        except Exception:
            self.close()
            raise

    def _handshake(self):
        flags = 0x02 | (0x80 if self.username else 0) | (0x40 if self.username and self.password else 0)
        body = _utf("MQTT") + bytes([4, flags]) + struct.pack(">H", 60) + _utf(self.client_id)
        if self.username:
            body += _utf(self.username)
            if self.password:
                body += _utf(self.password)
        self.sock.sendall(bytes([0x10]) + _varint(len(body)) + body)
        first, data = self._packet()
        if first != 0x20 or len(data) != 2:
            raise MqttError("unexpected reply to CONNECT")
        if data[1] != 0:
            raise MqttError("broker refused the connection (code %d: %s)" % (
                data[1], {4: "bad user name or password", 5: "not authorised"}.get(data[1], "see MQTT 3.1.1 CONNACK")))

    def publish(self, topic, payload, retain=False):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._pid = self._pid % 65535 + 1
        body = _utf(topic) + struct.pack(">H", self._pid) + payload.encode("utf-8")
        self.sock.sendall(bytes([0x32 | (1 if retain else 0)]) + _varint(len(body)) + body)
        first, data = self._packet()
        if first != 0x40 or struct.unpack(">H", data[:2])[0] != self._pid:
            raise MqttError("no acknowledgement for %s" % topic)

    def close(self):
        if self.sock:
            try:
                self.sock.sendall(b"\xe0\x00")
            except OSError:
                pass
            self.sock.close()
            self.sock = None


# ---- what to publish --------------------------------------------------------------------------

def _device(user):
    return {"identifiers": ["innodigym_%d" % user["id"]], "name": "INNODIGYM %s" % user["name"],
            "manufacturer": "INNODIGYM", "model": "X1 Pro"}


def discovery_messages(user, prefix="homeassistant"):
    """[(topic, payload dict)] describing the entities of one user's device."""
    uid, dev = user["id"], _device(user)
    state = "innodigym/%d/state" % uid

    def ent(component, key, name, **extra):
        # No object_id: with a device, Home Assistant builds the entity id from "<device name> <name>",
        # e.g. sensor.innodigym_alex_workouts_this_week.
        cfg = {"name": name, "unique_id": "innodigym_%d_%s" % (uid, key),
            "device": dev, "state_topic": state, "value_template": "{{ value_json.%s }}" % key}
        cfg.update(extra)
        return "%s/%s/innodigym_%d/%s/config" % (prefix, component, uid, key), cfg

    msgs = [
        ent("sensor", "workouts_this_week", "Workouts this week", state_class="measurement", icon="mdi:dumbbell",
            unit_of_measurement="workouts"),
        ent("sensor", "volume_this_week_kg", "Volume this week", state_class="measurement", icon="mdi:weight-kilogram",
            unit_of_measurement="kg"),
        ent("sensor", "energy_this_week_kcal", "Energy this week", state_class="measurement",
            unit_of_measurement="kcal", icon="mdi:fire"),
        ent("sensor", "goal_per_week", "Weekly goal", icon="mdi:target", unit_of_measurement="workouts",
            entity_category="diagnostic"),
        ent("sensor", "goal_streak_weeks", "Goal streak", state_class="measurement", icon="mdi:calendar-check",
            unit_of_measurement="weeks"),
        ent("sensor", "last_workout_name", "Last workout", icon="mdi:clipboard-text-clock"),
        ent("sensor", "last_workout_date", "Last workout date", device_class="date"),
        ent("sensor", "last_workout_kcal", "Last workout energy", unit_of_measurement="kcal", icon="mdi:fire"),
        ent("sensor", "last_workout_volume_kg", "Last workout volume", unit_of_measurement="kg",
            icon="mdi:weight-kilogram"),
        ent("sensor", "total_volume_kg", "Total volume", state_class="total", unit_of_measurement="kg",
            icon="mdi:weight-kilogram"),
        ent("binary_sensor", "trained_today", "Trained today", icon="mdi:run"),
    ]
    ev_topic = "innodigym/%d/workout_finished" % uid
    msgs.append(("%s/event/innodigym_%d/workout_finished/config" % (prefix, uid), {
        "name": "Workout finished", "unique_id": "innodigym_%d_workout_finished" % uid, "device": dev,
        "state_topic": ev_topic, "event_types": ["workout_finished"], "icon": "mdi:flag-checkered"}))
    return msgs


def event_payload(user, workout, prs):
    return {"event_type": "workout_finished", "user": user["name"], "workout_id": workout["id"],
            "name": workout["name"], "started_at": workout["started_at"], "volume_kg": workout["volume_kg"],
            "kcal": workout["kcal"], "duration_s": workout["duration_s"], "new_prs": prs}


def state_payload(conn, user, today=None):
    snap = stats.snapshot(db.list_workouts(conn, user["id"]), user["goal_per_week"], today or date.today())
    return snap  # None becomes JSON null, which Home Assistant shows as unknown


def broker_from_environment():
    """Connection settings, or None when MQTT is not available."""
    if os.environ.get("INNODIGYM_MQTT_HOST"):
        return {"host": os.environ["INNODIGYM_MQTT_HOST"], "port": int(os.environ.get("INNODIGYM_MQTT_PORT", "1883")),
                "username": os.environ.get("INNODIGYM_MQTT_USER"), "password": os.environ.get("INNODIGYM_MQTT_PASSWORD"),
                "use_tls": False}
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        req = urllib.request.Request(SUPERVISOR_URL, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)["data"]
        return {"host": d["host"], "port": d["port"], "username": d.get("username"), "password": d.get("password"),
                "use_tls": bool(d.get("ssl"))}
    except Exception as e:  # noqa: BLE001
        print("mqtt: could not read the broker settings from the Supervisor: %s" % e, flush=True)
        return None


class Publisher:
    """Turns database state into MQTT messages. `settings` is a dict for MqttClient or None (disabled).
    `client_factory` exists so tests can point it at a fake broker."""

    def __init__(self, settings, prefix="homeassistant", client_factory=MqttClient):
        self.settings, self.prefix, self.factory = settings, prefix, client_factory
        self._warned = False

    @property
    def enabled(self):
        return self.settings is not None

    def _send(self, build):
        if not self.enabled:
            return False
        try:
            with self.factory(client_id="innodigym-addon", **self.settings) as c:
                build(c)
            self._warned = False
            return True
        except (MqttError, OSError) as e:
            if not self._warned:
                print("mqtt: %s" % e, flush=True)
                self._warned = True
            return False

    def publish_state(self, conn, user, today=None):
        """Discovery (retained, so Home Assistant restarts keep the entities) plus the current values."""
        state = state_payload(conn, user, today)

        def build(c):
            for topic, cfg in discovery_messages(user, self.prefix):
                c.publish(topic, cfg, retain=True)
            c.publish("innodigym/%d/state" % user["id"], state, retain=True)
        return self._send(build)

    def publish_workout_finished(self, conn, user, workout_id):
        w = db.get_workout(conn, user["id"], workout_id)
        if w is None:
            return False
        prs = stats.new_prs(db.all_sets(conn, user["id"]), workout_id)
        return self._send(lambda c: c.publish("innodigym/%d/workout_finished" % user["id"], event_payload(user, w, prs)))

    def publish_all(self, conn, today=None):
        for u in db.list_users(conn):
            self.publish_state(conn, u, today)


def sync_hook(publisher, now=None):
    """Callback for sync.sync_all: announce workouts that just arrived (only recent ones, and never on a
    user's first sync, so importing a whole history does not fire a hundred events), then refresh the state."""
    from datetime import datetime, timedelta

    def hook(conn, user, result, first_run):
        if not publisher.enabled:
            return
        if not first_run:
            cutoff = ((now() if now else datetime.now()) - timedelta(hours=EVENT_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
            for wid in result.get("new_ids", []):
                w = db.get_workout(conn, user["id"], wid)
                if w and w["started_at"] >= cutoff:
                    publisher.publish_workout_finished(conn, user, wid)
        publisher.publish_state(conn, user)
    return hook
