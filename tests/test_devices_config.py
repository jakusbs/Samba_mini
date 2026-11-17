"""Samba_controls: device display names, device settings editor, start-up order and lights.

* Instruments connect one after another in ``start_order`` (Astor-like), and every step is
  visible: state events INIT -> ON per device, ``startup`` progress events, and the public
  ``server.devices`` list that feeds the lights before anyone logs in.
* Device settings (display name, enabled, start order, connection settings, extra stages) are
  saved through the service: administrators only, validated, refused when the file changed
  meanwhile, never removing a setup stage, with a backup; they apply after a restart.
"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.config import machine  # noqa: E402
from core.protocol import ErrorCode, ServiceError  # noqa: E402
from tests.conftest import ADMIN, OPERATOR, start_service  # noqa: E402

PHYSICAL = ["daq", "lockin", "hall", "stage_longitudinal", "stage_polar"]
# Windows stay referenced until the end of the session: a window deleted mid-session (garbage
# collected) left its Devices-panel timer and queued replies to fire into freed widgets during a
# later test -- an intermittent access violation in test_gui (17 Nov).  test_gui does the same.
_KEEP: list = []


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_until(app, cond, timeout=20.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    return cond()


def _record(server):
    events = []
    orig = server.host.publish

    def publish(topic, body):
        events.append((topic, dict(body)))
        orig(topic, body)
    server.host.publish = publish
    return events


# --------------------------------------------------------------------------- service
def test_instruments_connect_one_after_another_in_start_order(samba_home):
    svc = start_service(samba_home, overrides={"lockin": {"start_order": 5}}, connect=False)
    try:
        events = _record(svc.server)
        c = svc.client("lights")
        c.hello()
        brief = c.call("server.devices")                      # public: no login
        assert {d["name"] for d in brief["devices"]} >= set(PHYSICAL)
        assert not brief["startup"]["active"]
        res = svc.server.host.connect_all()
        assert set(res) == set(PHYSICAL) and set(res.values()) == {"ON"}
        order = ["lockin", "daq", "hall", "stage_longitudinal", "stage_polar"]   # lock-in moved first
        states = [(b["device"], b["state"]) for t, b in events if t.startswith("state.")
                  and b["device"] in PHYSICAL]
        # strictly one after another: each device is ON before the next one starts connecting
        assert states == [x for name in order for x in ((name, "INIT"), (name, "ON"))], states
        startup = [b for t, b in events if t == "startup"]
        assert startup[0]["active"] and startup[0]["order"] == order
        assert [b["current"] for b in startup[1:-1]] == order
        assert not startup[-1]["active"] and set(startup[-1]["results"]) == set(order)
        brief = c.call("server.devices")
        labels = {d["name"]: d["label"] for d in brief["devices"]}
        assert labels["lockin"] == "Lock-in (Ametek 7270)" and labels["stage_polar"].startswith("Polar stage")
        assert all(d["state"] == "ON" for d in brief["devices"] if d["physical"])
        assert "value" not in str(brief) and "resource" not in str(brief)       # no settings, no readings
    finally:
        svc.stop()


def test_device_settings_are_saved_by_admins_through_the_service(samba_home):
    svc = start_service(samba_home)
    try:
        admin = svc.client("admin")
        admin.hello()
        assert admin.has_feature("device-config-1") and admin.has_feature("startup-lights-1")
        admin.bootstrap(*ADMIN)
        admin.login(*ADMIN)
        admin.call("auth.create_user", {"username": OPERATOR[0], "password": OPERATOR[1]})
        got = admin.call("config.get")
        assert not got["restart_required"] and set(got["stage_drivers"]) == {"kinesis", "trinamic"}
        devices = {k: got["file"][k] for k in ("daq", "lockin", "hall", "stages")}
        devices["lockin"]["label"] = "7270 on the optical table"
        devices["stages"]["micro"] = machine.stage_template("kinesis", "micro")
        devices["stages"]["micro"]["serial_x"] = "27000001"

        op = svc.client("op")
        op.hello()
        op.login(*OPERATOR)
        with pytest.raises(ServiceError) as exc:                     # operators cannot change it
            op.call("config.save_devices", {"devices": devices, "base_hash": got["file_hash"]})
        assert exc.value.code == ErrorCode.FORBIDDEN

        bad = dict(devices, stages=dict(devices["stages"], **{"Bad name": devices["stages"]["micro"]}))
        with pytest.raises(ServiceError) as exc:                     # validated, nothing written
            admin.call("config.save_devices", {"devices": bad, "base_hash": got["file_hash"]})
        assert exc.value.code == ErrorCode.INVALID_VALUE and "stage name" in exc.value.message
        no_polar = dict(devices, stages={k: v for k, v in devices["stages"].items() if k != "polar"})
        with pytest.raises(ServiceError) as exc:                     # setup stages cannot be removed
            admin.call("config.save_devices", {"devices": no_polar, "base_hash": got["file_hash"]})
        assert "polar" in exc.value.message
        with pytest.raises(ServiceError) as exc:                     # only device sections
            admin.call("config.save_devices", {"devices": {"backend": {"rpc_port": 1}}})
        assert exc.value.code == ErrorCode.INVALID_VALUE

        res = admin.call("config.save_devices", {"devices": devices, "base_hash": got["file_hash"]})
        assert res["restart_required"]
        after = admin.call("config.get")
        assert after["restart_required"] and after["file"]["lockin"]["label"] == "7270 on the optical table"
        assert after["data"]["lockin"]["label"] == "Lock-in (Ametek 7270)"        # running service unchanged
        assert after["file"]["backend"] == got["file"]["backend"]                # other sections kept
        with pytest.raises(ServiceError) as exc:                     # stale hash: changed meanwhile
            admin.call("config.save_devices", {"devices": devices, "base_hash": got["file_hash"]})
        assert exc.value.code == ErrorCode.CONFLICT
        devices["lockin"]["start_order"] = 25
        admin.call("config.save_devices", {"devices": devices, "base_hash": after["file_hash"]})
        backups = list((samba_home / "config" / "backups").glob("machine.*.toml"))
        assert backups, "the previous file is backed up before it is replaced"
        saved = machine.load(samba_home / "config" / "machine.toml")
        assert saved.status == "loaded" and saved.data["stages"]["micro"]["serial_x"] == "27000001"
    finally:
        svc.stop()
    # After a restart the service runs with the saved settings (simulation: the extra stage too)
    svc2 = start_service(samba_home, overrides={k: saved.data[k] for k in ("daq", "lockin", "hall", "stages")})
    try:
        c = svc2.client("after")
        c.hello()
        brief = {d["name"]: d for d in c.call("server.devices")["devices"]}
        assert brief["lockin"]["label"] == "7270 on the optical table"
        assert brief["stage_micro"]["label"] == "micro stage (Thorlabs Kinesis)"
        assert brief["stage_micro"]["state"] == "ON"
    finally:
        svc2.stop()


def test_machine_config_validation_of_device_sections():
    import copy
    d = copy.deepcopy(machine.DEFAULTS)
    assert machine.validate(d) == []
    d["lockin"]["label"] = ""
    d["daq"]["start_order"] = "first"
    d["stages"]["x2"] = {"driver": "smaract"}
    probs = " | ".join(machine.validate(d))
    assert "lockin.label" in probs and "daq.start_order" in probs and "stages.x2.driver" in probs
    with pytest.raises(machine.ConfigError):
        machine.stage_template("smaract", "x2")


# --------------------------------------------------------------------------- GUI
def test_lights_show_waiting_starting_running_and_fault(qapp):
    from Samba_controls.panels.lights import StartupLights
    lights = StartupLights()
    devs = [{"name": n, "label": f"L-{n}", "physical": True, "state": "OFF", "status": "", "start_order": i}
            for i, n in enumerate(PHYSICAL)] + [{"name": "magnet", "physical": False, "state": "ON"}]
    lights.update_devices({"devices": devs, "startup": {"active": True, "order": PHYSICAL, "current": "daq",
                                                        "results": {}}})
    assert list(lights.lights) == PHYSICAL                              # physical only, in start order
    assert lights.lights["daq"].title.text() == "L-daq"
    lights.on_state("daq", "INIT", "connecting")
    assert lights.states()["daq"] == "INIT" and lights.states()["lockin"] == "WAITING"
    lights.on_state("daq", "ON", "connected")
    lights.on_startup({"active": True, "order": PHYSICAL, "current": "lockin", "results": {"daq": "ON"}})
    lights.on_state("lockin", "FAULT", "not reachable")
    assert lights.states()["daq"] == "ON" and lights.states()["lockin"] == "FAULT"
    assert lights.states()["stage_polar"] == "WAITING"
    assert "1/5 running" in lights.summary.text() and "starting" in lights.summary.text()
    clicked = []
    lights.device_clicked.connect(clicked.append)
    lights.lights["hall"].clicked.emit("hall")
    assert clicked == ["hall"]
    lights.set_service_stopped()
    assert set(lights.states().values()) == {"STOPPED"}


def test_controls_window_lights_follow_a_real_start(qapp, samba_home, monkeypatch):
    """Samba_controls attached to a starting service: the lights go waiting -> running in order,
    without anyone logged in; the table shows display names and selects by device id."""
    from core.gui.bridge import ServiceBridge
    from Samba_controls.app import ControlsWindow
    svc = start_service(samba_home, connect=False)
    try:
        bridge = ServiceBridge("Samba_controls", svc.rpc_port, svc.event_port)
        w = ControlsWindow(bridge, start_service=False)
        w.show()
        assert wait_until(qapp, lambda: len(w.lights.lights) == 5, 10)
        assert set(w.lights.states().values()) == {"OFF"}
        seen = []
        orig = w.lights.on_state
        w.lights.on_state = lambda d, s, st="": (seen.append((d, s)), orig(d, s, st))
        import threading
        threading.Thread(target=svc.server.host.connect_all, daemon=True).start()   # as at service start
        assert wait_until(qapp, lambda: set(w.lights.states().values()) == {"ON"}, 30), w.lights.states()
        assert ("daq", "INIT") in seen and ("stage_polar", "ON") in seen
        assert seen.index(("daq", "ON")) < seen.index(("lockin", "INIT"))
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
        w._we_started = False
        w.close()
        w._timer.stop()
        w.panel._timer.stop()
        _KEEP.append(w)
        bridge.close()
    finally:
        svc.stop()


def test_device_editor_edits_adds_saves_and_keeps_edits_on_failure(qapp, samba_home, monkeypatch):
    from core.gui.bridge import ServiceBridge
    from Samba_controls.panels.device_editor import DeviceEditor
    from Samba_controls.panels.devices import C_ID, C_LABEL, DeviceTable
    svc = start_service(samba_home)
    try:
        boot = svc.client("boot")
        boot.hello()
        boot.bootstrap(*ADMIN)
        boot.login(*ADMIN)
        boot.call("auth.create_user", {"username": OPERATOR[0], "password": OPERATOR[1]})
        bridge = ServiceBridge("Samba_controls", svc.rpc_port, svc.event_port)
        bridge.probe()
        ed = DeviceEditor(lambda: bridge)
        assert not ed.refresh() and not ed.save_btn.isEnabled()           # not logged in
        bridge.client.login(*OPERATOR)
        assert ed.refresh() and not ed.save_btn.isEnabled() and not ed.add_btn.isEnabled()   # read-only
        bridge.client.logout()
        bridge.client.login(*ADMIN)
        assert ed.refresh()
        assert ed.list.count() == 5
        ed.set_value("lockin", "label", "Lock-in on table 2")
        assert ed.is_dirty() and ed.save_btn.isEnabled()
        assert not ed.add_stage("polar", "kinesis")                       # name taken
        assert ed.add_stage("micro", "trinamic")
        assert ed.list.count() == 6 and not ed.remove_stage("polar")
        ed.set_value("stage_micro", "start_order", 7)
        # the file changes meanwhile (another administrator): refused, the edits stay
        other = svc.client("other")
        other.hello()
        other.login(*ADMIN)
        cfg = other.call("config.get")
        other.call("config.save_devices", {"devices": {"daq": dict(cfg["file"]["daq"], label="DAQ (other admin)")},
                                           "base_hash": cfg["file_hash"]})
        assert not ed.save()
        assert "changed since" in ed.status.text() and ed.is_dirty()
        assert ed._edits["lockin"]["label"] == "Lock-in on table 2" and "micro" in ed._edits["stages"]
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        assert ed.refresh(ask=True)                                       # reload (discarding) ...
        ed.set_value("lockin", "label", "Lock-in on table 2")             # ... and redo the edit
        saved = []
        ed.saved.connect(lambda: saved.append(True))
        assert ed.save() and saved == [True] and not ed.is_dirty()
        got = other.call("config.get")
        assert got["file"]["lockin"]["label"] == "Lock-in on table 2"
        assert got["file"]["daq"]["label"] == "DAQ (other admin)"          # the other change kept
        # the device table shows display names and still selects by device id
        table = DeviceTable()
        table.update_devices(bridge.client.call("device.list"))
        r = table._rows["lockin"]
        assert table.item(r, C_LABEL).text() == "Lock-in (Ametek 7270)" and table.item(r, C_ID).text() == "lockin"
        picked = []
        table.selected.connect(picked.append)
        table.select_device("hall")
        assert picked == ["hall"]
        bridge.close()
    finally:
        svc.stop()
