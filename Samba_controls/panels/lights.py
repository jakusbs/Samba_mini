"""Start-up lights: one light per instrument, in start order (like TANGO's Astor).

When the service starts, the instruments connect one after another
(``start_order`` in the machine configuration).  Each light shows where its
instrument is:

=========  ==========================  ==================================
colour     meaning                     device state
=========  ==========================  ==================================
hollow     waiting for its turn        (start-up queue)
yellow ◌   connecting (blinks)         INIT
green      running                     ON
blue       busy (moving / measuring)   MOVING / RUNNING
red        fault                       FAULT / UNKNOWN
grey       off / service stopped       OFF
dark       disabled in configuration   DISABLED
=========  ==========================  ==================================

The lights are fed by the public ``server.devices`` call and the service's
``state.*`` / ``startup`` events, so they work before anyone logs in.
Clicking a light selects that instrument in the device table.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

LOOK = {  # state -> (led colour, filled, text)
    "WAITING": ("#6c7086", False, "waiting"),
    "INIT": ("#f9e2af", True, "starting…"),
    "ON": ("#a6e3a1", True, "running"),
    "MOVING": ("#89b4fa", True, "moving"),
    "RUNNING": ("#89b4fa", True, "measuring"),
    "FAULT": ("#f38ba8", True, "fault"),
    "UNKNOWN": ("#f38ba8", True, "unknown"),
    "OFF": ("#6c7086", True, "off"),
    "DISABLED": ("#313244", True, "disabled"),
    "STOPPED": ("#45475a", True, "service stopped"),
}


class _Light(QFrame):
    clicked = pyqtSignal(str)

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.state = "STOPPED"
        self.setObjectName("light")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("#light{background:#181825;border:1px solid #313244;border-radius:6px;}"
                           "#light:hover{border-color:#585b70;}")
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 4, 10, 4)
        h.setSpacing(8)
        self.led = QLabel()
        self.led.setFixedSize(16, 16)
        h.addWidget(self.led)
        v = QVBoxLayout()
        v.setSpacing(0)
        self.title = QLabel(name)
        self.title.setStyleSheet("color:#cdd6f4;font-weight:bold;font-size:11px;")
        self.text = QLabel("")
        self.text.setStyleSheet("color:#a6adc8;font-size:10px;")
        v.addWidget(self.title)
        v.addWidget(self.text)
        h.addLayout(v)
        self._blink_on = True
        self.set_state("STOPPED")

    def mousePressEvent(self, ev) -> None:  # noqa: N802 - Qt
        self.clicked.emit(self.name)
        super().mousePressEvent(ev)

    def set_label(self, label: str) -> None:
        self.title.setText(label or self.name)

    def set_state(self, state: str, status: str = "") -> None:
        self.state = state if state in LOOK else "UNKNOWN"
        _color, _filled, text = LOOK[self.state]
        self.text.setText(text)
        self.setToolTip(f"{self.title.text()}  ({self.name})\n{state}" + (f": {status}" if status else ""))
        self._paint()

    def blink(self) -> None:
        if self.state == "INIT":
            self._blink_on = not self._blink_on
            self._paint()

    def _paint(self) -> None:
        color, filled, _text = LOOK[self.state]
        if self.state == "INIT" and not self._blink_on:
            filled = False
        bg = color if filled else "transparent"
        self.led.setStyleSheet(f"background:{bg};border:2px solid {color};border-radius:8px;")


class StartupLights(QWidget):
    device_clicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("lights_bar")
        self.setStyleSheet("#lights_bar{background:#12121f;border-bottom:1px solid #313244;}")
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(8, 4, 8, 4)
        self._row.setSpacing(6)
        self.caption = QLabel("Instruments:")
        self.caption.setStyleSheet("color:#89b4fa;font-weight:bold;font-size:11px;")
        self._row.addWidget(self.caption)
        self._row.addStretch(1)
        self.summary = QLabel("")
        self.summary.setStyleSheet("color:#a6adc8;font-size:11px;")
        self._row.addWidget(self.summary)
        self.lights: dict[str, _Light] = {}
        self._order: list[str] = []
        self._startup: dict = {}
        self._service = "stopped"
        self._timer = QTimer(self)
        self._timer.setInterval(450)
        self._timer.timeout.connect(self._blink)
        self._timer.start()

    # ------------------------------------------------------------------ data in
    def update_devices(self, brief: dict) -> None:
        """A ``server.devices`` answer: create/reorder lights and set every state."""
        self._service = "running"
        devs = [d for d in brief.get("devices", []) if d.get("physical")]
        devs.sort(key=lambda d: (d.get("start_order", 100), d["name"]))
        names = [d["name"] for d in devs]
        if names != self._order:
            self._rebuild(names)
        for d in devs:
            light = self.lights[d["name"]]
            light.set_label(d.get("label") or d["name"])
            light.set_state(d.get("state", "UNKNOWN"), d.get("status", ""))
        self.on_startup(brief.get("startup") or {})

    def on_state(self, device: str, state: str, status: str = "") -> None:
        light = self.lights.get(device)
        if light is not None:
            light.set_state(state, status)
            self._update_summary()

    def on_startup(self, info: dict) -> None:
        """Start-up progress: devices not yet reached show as waiting."""
        self._startup = dict(info or {})
        if self._startup.get("active"):
            done = set((self._startup.get("results") or {}).keys())
            current = self._startup.get("current")
            for name in self._startup.get("order") or []:
                light = self.lights.get(name)
                if light is not None and name not in done and name != current and light.state in ("OFF", "STOPPED"):
                    light.set_state("WAITING", "waiting for its turn in the start-up sequence")
        self._update_summary()

    def set_service_stopped(self, text: str = "service stopped") -> None:
        self._service = "stopped"
        self._startup = {}
        for light in self.lights.values():
            light.set_state("STOPPED", text)
        self.summary.setText(text)

    def set_service_starting(self) -> None:
        self._service = "starting"
        for light in self.lights.values():
            light.set_state("WAITING", "the service is starting")
        self.summary.setText("starting the service…")

    # ------------------------------------------------------------------ view
    def _rebuild(self, names: list[str]) -> None:
        for light in self.lights.values():
            self._row.removeWidget(light)
            light.deleteLater()
        self.lights = {}
        for i, name in enumerate(names):
            light = _Light(name)
            light.clicked.connect(self.device_clicked.emit)
            self._row.insertWidget(1 + i, light)
            self.lights[name] = light
        self._order = list(names)

    def _blink(self) -> None:
        for light in self.lights.values():
            light.blink()

    def _update_summary(self) -> None:
        if self._service == "stopped":
            return
        states = [light.state for light in self.lights.values()]
        n_on = sum(s in ("ON", "MOVING", "RUNNING") for s in states)
        current = self._startup.get("current") if self._startup.get("active") else None
        text = f"{n_on}/{len(states)} running"
        if current:
            label = self.lights[current].title.text() if current in self.lights else current
            text += f" — starting {label}…"
        elif any(s in ("FAULT", "UNKNOWN") for s in states):
            text += " — check the red ones"
        self.summary.setText(text)

    def states(self) -> dict[str, str]:
        return {n: light.state for n, light in self.lights.items()}
