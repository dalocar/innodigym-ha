import json
import os
import socketserver
import struct
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "innodigym", "app"))
import db  # noqa: E402
import mqtt  # noqa: E402
import sync  # noqa: E402


class Broker(socketserver.ThreadingTCPServer):
    """Just enough of an MQTT 3.1.1 broker: records CONNECT and PUBLISH, acks QoS 1."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, connack=0):
        self.connack, self.connects, self.published, self.lock = connack, [], [], threading.Lock()
        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


class Handler(socketserver.BaseRequestHandler):
    def read(self, n):
        b = b""
        while len(b) < n:
            c = self.request.recv(n - len(b))
            if not c:
                raise EOFError
            b += c
        return b

    def packet(self):
        first = self.read(1)[0]
        mult, length = 1, 0
        while True:
            b = self.read(1)[0]
            length += (b & 127) * mult
            if not b & 128:
                break
            mult *= 128
        return first, self.read(length)

    def handle(self):
        b = self.server
        try:
            while True:
                first, body = self.packet()
                if first == 0x10:
                    n = struct.unpack(">H", body[:2])[0]
                    assert body[2:2 + n] == b"MQTT" and body[2 + n] == 4
                    flags = body[3 + n]
                    pos = 2 + n + 4  # level, flags, keepalive
                    cid_len = struct.unpack(">H", body[pos:pos + 2])[0]
                    pos += 2 + cid_len
                    user = pw = None
                    if flags & 0x80:
                        ul = struct.unpack(">H", body[pos:pos + 2])[0]
                        user = body[pos + 2:pos + 2 + ul].decode()
                        pos += 2 + ul
                    if flags & 0x40:
                        pl = struct.unpack(">H", body[pos:pos + 2])[0]
                        pw = body[pos + 2:pos + 2 + pl].decode()
                    with b.lock:
                        b.connects.append((user, pw))
                    self.request.sendall(bytes([0x20, 2, 0, b.connack]))
                    if b.connack:
                        return
                elif first & 0xF0 == 0x30:
                    qos, retain = (first >> 1) & 3, first & 1
                    tl = struct.unpack(">H", body[:2])[0]
                    topic = body[2:2 + tl].decode()
                    pos = 2 + tl
                    pid = body[pos:pos + 2]
                    pos += 2 if qos else 0
                    with b.lock:
                        b.published.append((topic, body[pos:].decode(), retain, qos))
                    if qos:
                        self.request.sendall(bytes([0x40, 2]) + pid)
                elif first == 0xE0:
                    return
        except EOFError:
            return


class MqttTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.dir.name, "t.db"))
        self.uid = db.create_user(self.conn, "Alex", goal_per_week=3)
        self.user = db.get_user(self.conn, self.uid)
        self.broker = Broker()

    def tearDown(self):
        self.broker.shutdown()
        self.broker.server_close()
        self.conn.close()
        self.dir.cleanup()

    def pub(self, **kw):
        return mqtt.Publisher({"host": "127.0.0.1", "port": self.broker.port, "username": "u", "password": "p"}, **kw)

    def add(self, i, when, weight=20):
        db.upsert_vendor_record(self.conn, self.uid, {"sportRecordId": i, "startTime": when, "name": "Rutina A",
                                                       "coreData": 2000.0, "consume": 100, "duration": 600},
                                [{"actionId": 1, "actionName": "Row", "sort": 1, "weight": weight * 10, "count": 10, "duration": 30}])

    def test_client_connects_with_credentials_and_publishes_retained_qos1(self):
        with mqtt.MqttClient("127.0.0.1", self.broker.port, "user1", "pass1") as c:
            c.publish("a/b", {"x": 1}, retain=True)
            c.publish("a/c", "hi")
        self.assertEqual(self.broker.connects, [("user1", "pass1")])
        self.assertEqual(self.broker.published, [("a/b", '{"x":1}', 1, 1), ("a/c", "hi", 0, 1)])

    def test_refused_connection_names_the_reason(self):
        self.broker.connack = 4
        with self.assertRaises(mqtt.MqttError) as cm:
            mqtt.MqttClient("127.0.0.1", self.broker.port, "u", "bad").connect()
        self.assertIn("user name or password", str(cm.exception))

    def test_unreachable_broker_is_an_mqtt_error(self):
        with self.assertRaises(mqtt.MqttError):
            mqtt.MqttClient("127.0.0.1", 1, timeout=1).connect()

    def test_discovery_describes_one_device_per_user_with_unique_ids(self):
        msgs = dict(mqtt.discovery_messages(self.user))
        t = "homeassistant/sensor/innodigym_%d/workouts_this_week/config" % self.uid
        self.assertIn(t, msgs)
        cfg = msgs[t]
        self.assertEqual(cfg["unique_id"], "innodigym_%d_workouts_this_week" % self.uid)
        self.assertEqual(cfg["device"]["name"] + " " + cfg["name"], "INNODIGYM Alex Workouts this week")  # becomes the entity id
        self.assertNotIn("object_id", cfg)  # deprecated by Home Assistant
        self.assertEqual(cfg["state_topic"], "innodigym/%d/state" % self.uid)
        self.assertEqual(cfg["device"]["identifiers"], ["innodigym_%d" % self.uid])
        sam = db.get_user(self.conn, db.create_user(self.conn, "Sam"))
        other = {c["unique_id"] for _, c in mqtt.discovery_messages(sam)}
        self.assertFalse(other & {c["unique_id"] for c in msgs.values()})
        self.assertIn("homeassistant/binary_sensor/innodigym_%d/trained_today/config" % self.uid, msgs)
        ev = msgs["homeassistant/event/innodigym_%d/workout_finished/config" % self.uid]
        self.assertEqual(ev["event_types"], ["workout_finished"])
        self.assertEqual(ev["state_topic"], "innodigym/%d/workout_finished" % self.uid)

    def test_every_value_template_key_exists_in_the_state(self):
        self.add(1, "2026-11-12 08:00:00")
        state = mqtt.state_payload(self.conn, self.user, date(2026, 11, 13))
        for topic, cfg in mqtt.discovery_messages(self.user):
            if "value_template" in cfg:
                key = cfg["value_template"].split("value_json.")[1].split(" ")[0]
                self.assertIn(key, state, topic)

    def test_publish_state_sends_retained_discovery_then_state(self):
        self.add(1, "2026-11-13 08:00:00")
        self.assertTrue(self.pub().publish_state(self.conn, self.user, date(2026, 11, 13)))
        topics = [p[0] for p in self.broker.published]
        self.assertEqual(topics[-1], "innodigym/%d/state" % self.uid)
        self.assertTrue(all(p[2] == 1 for p in self.broker.published))
        state = json.loads(self.broker.published[-1][1])
        self.assertEqual((state["workouts_this_week"], state["trained_today"], state["last_workout_name"]), (1, "ON", "Rutina A"))

    def test_empty_history_publishes_nulls_not_empty_strings(self):
        self.pub().publish_state(self.conn, self.user, date(2026, 11, 13))
        state = json.loads(self.broker.published[-1][1])
        self.assertIsNone(state["last_workout_date"])
        self.assertEqual(state["trained_today"], "OFF")

    def test_disabled_publisher_does_nothing(self):
        p = mqtt.Publisher(None)
        self.assertFalse(p.enabled)
        self.assertFalse(p.publish_state(self.conn, self.user))
        self.assertEqual(self.broker.published, [])

    def test_broker_down_returns_false_and_warns_once(self):
        p = mqtt.Publisher({"host": "127.0.0.1", "port": 1, "timeout": 1})
        self.assertFalse(p.publish_state(self.conn, self.user))
        self.assertTrue(p._warned)

    def test_workout_finished_event_carries_new_prs(self):
        self.add(1, "2026-11-10 08:00:00", 20)
        self.add(2, "2026-11-12 08:00:00", 25)
        self.assertTrue(self.pub().publish_workout_finished(self.conn, self.user, 2))
        topic, payload, retain, _ = self.broker.published[-1]
        ev = json.loads(payload)
        self.assertEqual((topic, retain), ("innodigym/%d/workout_finished" % self.uid, 0))
        self.assertEqual((ev["event_type"], ev["user"], ev["workout_id"], ev["volume_kg"]), ("workout_finished", "Alex", 2, 200.0))
        self.assertEqual(ev["new_prs"], [{"exercise": "Row", "weight_kg": 25.0, "previous_kg": 20.0}])

    def result(self, ids):
        return {"new_ids": ids}

    def test_sync_hook_fires_events_only_for_recent_workouts_and_not_on_first_sync(self):
        self.add(1, "2026-11-12 08:00:00")
        self.add(2, "2026-09-01 08:00:00")
        now = lambda: datetime(2026, 11, 13, 9, 0, 0)  # noqa: E731
        hook = mqtt.sync_hook(self.pub(), now)
        hook(self.conn, self.user, self.result([1, 2]), True)           # first sync: state only
        self.assertEqual([p[0] for p in self.broker.published if p[0].endswith("workout_finished")], [])
        self.broker.published.clear()
        hook(self.conn, self.user, self.result([1, 2]), False)          # 1 is recent, 2 is old
        events = [json.loads(p[1])["workout_id"] for p in self.broker.published if p[0].endswith("/workout_finished")]
        self.assertEqual(events, [1])
        self.assertEqual(self.broker.published[-1][0], "innodigym/%d/state" % self.uid)

    def test_sync_all_calls_the_hook_with_first_run_flag(self):
        db.set_token(self.conn, self.uid, "tok")

        class Vendor:
            def get(self, path):
                if "record/list" in path:
                    return {"data": [{"sportRecordId": 7, "startTime": "2026-11-12 08:00:00", "name": "A", "coreData": 10.0, "consume": 1, "duration": 60}]}
                return {"data": {"extData": [{"actionName": "Row", "sort": 1, "weight": 100, "count": 5, "duration": 20}]}}
        calls = []
        sync.sync_all(self.conn, lambda t: Vendor(), lambda c, u, r, first: calls.append((first, r["new_ids"])))
        sync.sync_all(self.conn, lambda t: Vendor(), lambda c, u, r, first: calls.append((first, r["new_ids"])))
        self.assertEqual(calls, [(True, [7]), (False, [])])

    def test_a_failing_hook_does_not_stop_the_loop(self):
        db.set_token(self.conn, self.uid, "tok")

        class Vendor:
            def get(self, path):
                return {"data": []}

        def boom(*a):
            raise RuntimeError("x")
        out = sync.sync_all(self.conn, lambda t: Vendor(), boom)
        self.assertEqual(list(out), ["Alex"])

    def test_broker_settings_from_environment(self):
        old = {k: os.environ.get(k) for k in ("INNODIGYM_MQTT_HOST", "INNODIGYM_MQTT_PORT", "SUPERVISOR_TOKEN")}
        try:
            os.environ.pop("SUPERVISOR_TOKEN", None)
            os.environ.pop("INNODIGYM_MQTT_HOST", None)
            self.assertIsNone(mqtt.broker_from_environment())
            os.environ["INNODIGYM_MQTT_HOST"], os.environ["INNODIGYM_MQTT_PORT"] = "broker.local", "1884"
            s = mqtt.broker_from_environment()
            self.assertEqual((s["host"], s["port"]), ("broker.local", 1884))
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


if __name__ == "__main__":
    unittest.main()
