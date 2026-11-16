"""Real driver classes exercised against fake vendor libraries (no hardware).

These verify the command sequences, conversions and framing ported from
MiniMoke.  They do not prove the instruments behave as the fakes do; the
hardware checks are listed in docs/progress.md.
"""

import sys
import types

import numpy as np
import pytest

from core.protocol import ErrorCode, ServiceError


# --------------------------------------------------------------------------- NI-DAQ
class FakeTask:
    instances = []

    def __init__(self, name):
        self.name = name
        self.calls = []
        self.ao = []
        self.ai = []
        self.do = []
        self.timing_n = None
        self.written = None
        self.started = False
        FakeTask.instances.append(self)
        task = self

        class Chans:
            def __init__(self, kind):
                self.kind = kind

            def add_ao_voltage_chan(self, ch):
                task.ao.append(ch)

            def add_ai_voltage_chan(self, ch):
                task.ai.append(ch)

            def add_do_chan(self, ch):
                task.do.append(ch)

        class Timing:
            def cfg_samp_clk_timing(self, rate, samps_per_chan):
                task.calls.append(("timing", rate, samps_per_chan))
                task.timing_n = samps_per_chan

        class StartTrig:
            def cfg_dig_edge_start_trig(self, src, trigger_edge=None):
                task.calls.append(("trigger", src))

        class Triggers:
            start_trigger = StartTrig()

        self.ao_channels = self.ai_channels = self.do_channels = Chans("x")
        self.timing = Timing()
        self.triggers = Triggers()

    def start(self):
        self.started = True
        self.calls.append(("start",))

    def stop(self):
        self.started = False
        self.calls.append(("stop",))

    def close(self):
        self.calls.append(("close",))

    def write(self, data, auto_start=False):
        self.written = np.asarray(data)
        self.calls.append(("write", np.asarray(data).shape, auto_start))

    def wait_until_done(self, timeout):
        self.calls.append(("wait", timeout))

    def read(self, number_of_samples_per_channel, timeout):
        n = number_of_samples_per_channel
        # balanced: 9.0 during the settle samples, 1.0 afterwards; intensity 2.0
        out_task = [t for t in FakeTask.instances if t.name == "SambaMiniOutput"][-1]
        n_settle = FakeTask.n_settle
        bd = np.r_[np.full(n_settle, 9.0), np.full(n - n_settle, 1.0)]
        return [bd.tolist(), np.full(n, 2.0).tolist()]


@pytest.fixture
def fake_nidaqmx(monkeypatch):
    FakeTask.instances = []
    mod = types.ModuleType("nidaqmx")
    mod.Task = FakeTask
    consts = types.ModuleType("nidaqmx.constants")
    consts.Edge = types.SimpleNamespace(RISING="RISING")
    system = types.ModuleType("nidaqmx.system")
    system.Device = lambda name: types.SimpleNamespace(product_type="USB-6xxx (fake)", dev_serial_num=123)
    mod.constants = consts
    mod.system = system
    monkeypatch.setitem(sys.modules, "nidaqmx", mod)
    monkeypatch.setitem(sys.modules, "nidaqmx.constants", consts)
    monkeypatch.setitem(sys.modules, "nidaqmx.system", system)
    return mod


def test_nidaq_channels_trigger_and_settle_prefix(fake_nidaqmx):
    from Samba_controls.service.devices.nidaq import NiDaq
    d = NiDaq("daq", {"coil_min_V": -6, "coil_max_V": 6})
    d.connect()
    out, inp, trig = FakeTask.instances
    assert out.ao == ["Dev1/ao1", "Dev1/ao2", "Dev1/ao3"]           # MiniMoke channel order
    assert inp.ai == ["Dev1/ai0", "Dev1/ai1"]
    assert trig.do == ["/Dev1/PFI0"]
    assert ("trigger", "/Dev1/PFI0") in out.calls and ("trigger", "/Dev1/PFI0") in inp.calls
    cfg = d.configure_acquisition(0.01, 0.002)                      # 500 + 100 + 1 samples
    assert (cfg["n_acq"], cfg["n_settle"], cfg["n_total"]) == (500, 100, 601)
    FakeTask.n_settle = 100
    info = d.start_acquisition(1.5)
    assert out.written.shape == (3, 601)
    assert np.all(out.written[2] == 1.5)                             # coil steps at the first sample
    assert ("timing", 50000.0, 601) in inp.calls
    assert trig.written is not None and list(trig.written) == [True, False]
    res = d.read_acquisition(info["acq_id"])
    assert res["n_samples"] == 500
    assert res["balanced_V"] == pytest.approx(1.0)                  # settle samples discarded
    assert res["intensity_V"] == pytest.approx(2.0)
    with pytest.raises(ServiceError) as e:                          # stale id
        d.read_acquisition(info["acq_id"])
    assert e.value.code == ErrorCode.INVALID_STATE
    with pytest.raises(ServiceError) as e:
        d.start_acquisition(7.0)
    assert e.value.code == ErrorCode.OUT_OF_RANGE


def test_nidaq_static_write_like_minimoke(fake_nidaqmx):
    from Samba_controls.service.devices.nidaq import NiDaq
    d = NiDaq("daq", {})
    d.connect()
    out = FakeTask.instances[0]
    d.static_outputs(0.25, 0.0, 0.0)
    assert ("timing", 10000.0, 2) in out.calls
    assert out.written.shape == (3, 2) and np.all(out.written[2] == 0.25)
    assert any(c[0] == "wait" for c in out.calls)                   # waits for the two samples
    assert d.read_attribute("coil_command_V").value == 0.25


def test_nidaq_missing_driver_is_disabled(monkeypatch):
    monkeypatch.setitem(sys.modules, "nidaqmx", None)
    from Samba_controls.service.devices.nidaq import NiDaq
    d = NiDaq("daq", {})
    with pytest.raises(ServiceError):
        d.connect()
    assert d.state.value == "DISABLED"
    with pytest.raises(ServiceError) as e:
        d.read_attribute("balanced_V")
    assert e.value.code == ErrorCode.DEVICE_UNAVAILABLE


# --------------------------------------------------------------------------- Ametek 7270
class Fake7270:
    """Responds like the 7270; socket framing adds one extra chunk per reply."""

    def __init__(self, resource, socket_framing, fail_reads=0, refuse_50000=False):
        if refuse_50000 and "::50000::" in resource:
            raise OSError("connection refused")
        self.resource = resource
        self.extra = 1 if socket_framing else 0
        self.queue = []
        self.timeout = 2000
        self.log = []
        self.fail_reads = fail_reads
        self.state = {"SEN": 0, "TC": 0, "OA.": 0.0, "OF.": 0.0, "REFP.": 0.0, "REFMODE": 1}

    def write(self, cmd):
        self.log.append(cmd)
        parts = cmd.split()
        if cmd == "ID":
            reply = "7270"
        elif cmd == "VER":
            reply = "2.0"
        elif cmd == "XY.":
            reply = "1.0E-4,-2.0E-5"
        elif len(parts) == 2 and parts[0] in self.state:
            self.state[parts[0]] = float(parts[1])
            reply = ""
        elif parts[0] in self.state:
            reply = str(self.state[parts[0]])
        else:
            reply = ""
        self.queue.append(reply)
        self.queue.extend(["\n"] * self.extra)

    def read(self):
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise OSError("VI_ERROR_TMO")
        if not self.queue:
            raise TimeoutError("timeout")
        return self.queue.pop(0)

    def clear(self):
        self.queue.clear()

    def close(self):
        pass


class FakeRM:
    def __init__(self, socket_framing=True, refuse_50000=False):
        self.socket_framing = socket_framing
        self.refuse_50000 = refuse_50000
        self.opened = []

    def open_resource(self, res, read_termination=None, write_termination=None):
        assert read_termination == "\x00" and write_termination == "\x00"
        inst = Fake7270(res, self.socket_framing, refuse_50000=self.refuse_50000)
        self.opened.append(inst)
        return inst


@pytest.fixture
def fake_pyvisa(monkeypatch):
    mod = types.ModuleType("pyvisa")
    holder = {"rm": FakeRM()}
    mod.ResourceManager = lambda *a: holder["rm"]
    monkeypatch.setitem(sys.modules, "pyvisa", mod)
    return holder


def test_ametek_socket_framing_and_protocol(fake_pyvisa, monkeypatch):
    import Samba_controls.service.devices.ametek7270 as am
    monkeypatch.setattr(am, "RECONNECT_SETTLE_S", 0.0)
    li = am.Ametek7270("lockin", {"resource": "TCPIP0::192.168.77.2::50000::SOCKET"})
    li.connect()
    assert li._extra_chunks == 1                                   # Ethernet status chunk detected
    applied = li.configure(volt=1.0, sensi=4e-4, freq=1777.0, time_const=0.15, phase=370.0)
    inst = fake_pyvisa["rm"].opened[-1]
    assert "REFMODE 0" in inst.log
    assert "SEN 17" in inst.log and applied["sensitivity_V"] == 500e-6   # rounded up (PyMeasure)
    assert "TC 13" in inst.log and applied["time_constant_s"] == 0.2
    assert "REFP. 10" in inst.log                                   # modular phase
    assert inst.log.index("OA. 1") < inst.log.index("SEN 17") < inst.log.index("OF. 1777")
    snap = li.read_xy_rt()
    assert snap["x_V"] == pytest.approx(1e-4) and snap["y_V"] == pytest.approx(-2e-5)
    assert snap["theta_deg"] == pytest.approx(np.degrees(np.arctan2(-2e-5, 1e-4)))
    assert inst.queue == []                                         # stream stays aligned
    li.output_off()
    assert inst.log[-1] == "OA. 0"


def test_ametek_reconnect_and_port_fallback(fake_pyvisa, monkeypatch):
    import Samba_controls.service.devices.ametek7270 as am
    monkeypatch.setattr(am, "RECONNECT_SETTLE_S", 0.0)
    fake_pyvisa["rm"] = FakeRM(socket_framing=True, refuse_50000=True)
    li = am.Ametek7270("lockin", {"resource": "TCPIP0::10.0.0.2::50000::SOCKET"})
    li.connect()
    assert li._resource_in_use.endswith("::50001::SOCKET")
    inst = fake_pyvisa["rm"].opened[-1]
    inst.fail_reads = 1                                             # one I/O glitch
    x, y = li.hw_read_xy()
    assert x == pytest.approx(1e-4)
    assert len(fake_pyvisa["rm"].opened) >= 2                       # session re-opened


# --------------------------------------------------------------------------- Kinesis
class FakeMotor:
    def __init__(self, serial):
        self.serial = serial
        self.pos = 1000.0
        self.moving_polls = 0
        self.calls = []

    def setup_gen_move(self, backlash_distance=None, scale=True):
        self.calls.append(("backlash", backlash_distance, scale))

    def get_position(self, scale=True):
        return self.pos

    def move_to(self, pos, scale=True):
        self.calls.append(("move_to", pos, scale))
        self.target = pos
        self.moving_polls = 2

    def move_by(self, d, scale=True):
        self.calls.append(("move_by", d, scale))
        self.target = self.pos + d
        self.moving_polls = 2

    def is_moving(self):
        if self.moving_polls > 0:
            self.moving_polls -= 1
            if self.moving_polls == 0:
                self.pos = self.target
            return True
        return False

    def stop(self, immediate=False, sync=True, timeout=None):
        self.calls.append(("stop",))
        self.moving_polls = 0

    def close(self):
        pass


@pytest.fixture
def fake_pylablib(monkeypatch):
    motors = {}
    thor = types.SimpleNamespace(KinesisMotor=lambda s: motors.setdefault(s, FakeMotor(s)))
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = thor
    pkg = types.ModuleType("pylablib")
    pkg.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", pkg)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)
    return motors


def test_kinesis_conversions_backlash_and_moves(fake_pylablib):
    from Samba_controls.service.devices.kinesis_stage import KinesisStage
    st = KinesisStage("stage_longitudinal", {"serial_x": "1", "serial_y": "2", "serial_z": "3",
                                             "mm2steps": 2184533, "backlash_mm": 0.02,
                                             "offsets_steps": {"x": 1000, "y": 0, "z": 0}})
    st.connect()
    mx = fake_pylablib["1"]
    assert ("backlash", 43691, False) in mx.calls                   # round(0.02 * 2184533)
    assert st.get_mm("x") == 0.0                                    # raw 1000 - offset 1000
    res = st.move_to("x", 0.5)
    assert ("move_to", 0.5 * 2184533 + 1000, False) in mx.calls
    assert res["position_mm"] == pytest.approx(0.5)
    st.jog("x", -0.1)
    assert ("move_by", -0.1 * 2184533, False) in mx.calls


def test_kinesis_timeout_stops_and_reports_position(fake_pylablib):
    from Samba_controls.service.devices.kinesis_stage import KinesisStage
    st = KinesisStage("s", {"serial_x": "1", "serial_y": "2", "serial_z": "3", "move_timeout_s": 0.2})
    st.connect()
    m = fake_pylablib["1"]
    m.is_moving = lambda: True                                       # never arrives
    with pytest.raises(ServiceError) as e:
        st.move_to("x", 1.0)
    assert e.value.code == ErrorCode.TIMEOUT and "not retried" in e.value.message
    assert ("stop",) in m.calls
    assert "position_mm" in e.value.details


# --------------------------------------------------------------------------- Trinamic
class FakeIface:
    def __init__(self):
        self.sent = []
        self.raw = {0: 0, 1: 0, 2: 0}
        self.stopped = []

    def send(self, op, typ, motor, value, module_id):
        self.sent.append((op, typ, motor, value, module_id))
        if op == 4:   # MVP
            self.raw[motor] = value if typ == 0 else self.raw[motor] + value
        if op == 6:   # GAP
            if typ == 1:
                return types.SimpleNamespace(value=self.raw[motor] & 0xFFFFFFFF)
            if typ == 8:
                return types.SimpleNamespace(value=1)
        return types.SimpleNamespace(value=0)

    def stop(self, motor, module_id):
        self.stopped.append(motor)

    def close(self):
        pass


@pytest.fixture
def fake_trinamic(monkeypatch):
    iface = FakeIface()
    conns = types.ModuleType("pytrinamic.connections")
    conns.ConnectionManager = lambda args: types.SimpleNamespace(connect=lambda: iface)
    pkg = types.ModuleType("pytrinamic")
    pkg.connections = conns
    monkeypatch.setitem(sys.modules, "pytrinamic", pkg)
    monkeypatch.setitem(sys.modules, "pytrinamic.connections", conns)
    return iface


def test_trinamic_motor_config_and_signs(fake_trinamic):
    from Samba_controls.service.devices.trinamic_stage import TrinamicStage
    st = TrinamicStage("stage_polar", {"offsets_steps": {"x": 153, "y": -122880, "z": 271872}})
    st.connect()
    sap = [s for s in fake_trinamic.sent if s[0] == 5]
    assert (5, 6, 2, 60, 1) in sap and (5, 6, 1, 102, 1) in sap and (5, 6, 0, 60, 1) in sap   # currents
    assert (5, 167, 1, 1, 1) in sap and (5, 140, 0, 8, 1) in sap                                # StealthChop, 256 µsteps
    st.move_to("y", 1.0)
    assert fake_trinamic.raw[1] == -51200 + -122880                  # Y is sign-inverted
    assert st.get_mm("y") == pytest.approx(1.0)
    st.move_to("x", 1.0)
    assert fake_trinamic.raw[2] == 51200 + 153
    fake_trinamic.raw[0] = (-5) & 0xFFFFFFFF                          # signed 32-bit readback
    assert st.hw_get_raw("z") == -5


# --------------------------------------------------------------------------- Hall probe
def test_hall_filter_read_and_bounded_zero(tmp_path, monkeypatch):
    dll = tmp_path / "MagnetPhysik.Usb.dll"
    dll.write_bytes(b"fake")
    probe = types.SimpleNamespace(Speed=0, Filter=0, Zero=False, get_Tesla=lambda: 0.0123)
    mp = types.ModuleType("MagnetPhysik")
    mp.HallProbe = lambda: probe
    clr = types.ModuleType("clr")
    clr.AddReference = lambda p: None
    monkeypatch.setitem(sys.modules, "clr", clr)
    monkeypatch.setitem(sys.modules, "MagnetPhysik", mp)
    from Samba_controls.service.devices.hall import MagnetPhysikHall
    h = MagnetPhysikHall("hall", {"dll_path": str(dll), "speed": 1, "default_integration_s": 0.5})
    h.connect()
    assert probe.Speed == 1 and probe.Filter == 235                  # round(0.5 * 470)
    assert h.integration_s == pytest.approx(235 / 470)
    assert h.set_integration(2.0) == pytest.approx(255 / 470)        # clamped to 255 points
    h.set_integration(0.002)
    r = h.read_mT()
    assert r["field_mT"] == pytest.approx(12.3)
    probe.Zero = True                                                 # probe never finishes zeroing
    with pytest.raises(ServiceError) as e:
        h.hw_zero(0.2, None)
    assert e.value.code == ErrorCode.TIMEOUT


def test_hall_missing_dll_disabled():
    from Samba_controls.service.devices.hall import MagnetPhysikHall
    h = MagnetPhysikHall("hall", {"dll_path": r"C:\nowhere\MagnetPhysik.Usb.dll"})
    with pytest.raises(ServiceError):
        h.connect()
    assert h.state.value == "DISABLED"


def test_read_only_connect_writes_nothing(fake_pylablib, fake_trinamic, tmp_path, monkeypatch):
    from Samba_controls.service.devices.kinesis_stage import KinesisStage
    from Samba_controls.service.devices.trinamic_stage import TrinamicStage
    st = KinesisStage("s", {"serial_x": "1", "serial_y": "2", "serial_z": "3", "read_only": True})
    st.connect()
    assert not any(c[0] == "backlash" for c in fake_pylablib["1"].calls)
    assert isinstance(st.get_mm("x"), float)
    tr = TrinamicStage("p", {"read_only": True})
    with pytest.raises(ServiceError):
        tr.connect()
    assert fake_trinamic.sent == []                                 # no SAP written
    dll = tmp_path / "MagnetPhysik.Usb.dll"
    dll.write_bytes(b"fake")

    class Probe:
        def __init__(self):
            self.writes = []
            self._speed, self._filter = 1, 128

        def __setattr__(self, k, v):
            if k in ("Speed", "Filter", "Zero"):
                self.writes.append(k)
            object.__setattr__(self, k, v)

        Speed = property(lambda s: s._speed, lambda s, v: object.__setattr__(s, "_speed", v))
        Filter = property(lambda s: s._filter, lambda s, v: object.__setattr__(s, "_filter", v))

        def get_Tesla(self):
            return 0.001
    probe = Probe()
    mp = types.ModuleType("MagnetPhysik")
    mp.HallProbe = lambda: probe
    clr = types.ModuleType("clr")
    clr.AddReference = lambda p: None
    monkeypatch.setitem(sys.modules, "clr", clr)
    monkeypatch.setitem(sys.modules, "MagnetPhysik", mp)
    from Samba_controls.service.devices.hall import MagnetPhysikHall
    h = MagnetPhysikHall("hall", {"dll_path": str(dll), "read_only": True})
    h.connect()
    assert probe.writes == []                                       # Speed/Filter only read
    assert h.integration_s == pytest.approx(128 / 470)
    with pytest.raises(ServiceError):
        h.set_integration(0.1)


def test_hall_refuses_dll_with_download_mark(tmp_path, monkeypatch):
    dll = tmp_path / "MagnetPhysik.Usb.dll"
    dll.write_bytes(b"fake")
    with open(f"{dll}:Zone.Identifier", "w") as f:          # NTFS alternate data stream
        f.write("[ZoneTransfer]\nZoneId=3\n")
    clr = types.ModuleType("clr")
    clr.AddReference = lambda p: (_ for _ in ()).throw(AssertionError("must not try to load"))
    monkeypatch.setitem(sys.modules, "clr", clr)
    from Samba_controls.service.devices.hall import MagnetPhysikHall
    h = MagnetPhysikHall("hall", {"dll_path": str(dll)})
    with pytest.raises(ServiceError) as e:
        h.connect()
    assert "marked as downloaded" in e.value.message
    assert h.state.value == "DISABLED"
