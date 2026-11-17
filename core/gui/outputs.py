"""Leaving the coil / lock-in oscillator running when control is released.

The coil keeps running after measurements and the lock-in is only changed by
hand.  When control of the setup is released (Release control, Log out, closing
the window) while either is on, the user decides: leave it on -- with nobody in
control -- or switch it off now.  Losing control without a choice (a crash, an
expired lease, a forced release, the service stopping) always switches them off.

``SAMBA_MINI_KEEP_OUTPUTS=no`` (or ``yes``) answers the question without asking
(unattended use, automated tests).
"""

from __future__ import annotations

import math
import os

from PyQt6.QtWidgets import QMessageBox

from core.protocol import ServiceError

ON_V = 1e-6


def outputs_on(client) -> dict[str, float]:
    """``{"coil": V, "lockin": V}`` for the outputs that are on now (unknown counts as off)."""
    out: dict[str, float] = {}
    for key, dev, attr in (("coil", "daq", "coil_command_V"), ("lockin", "lockin", "oscillator_V")):
        try:
            r = client.device(dev).read(attr, timeout_s=5.0)[attr]
        except ServiceError:
            continue
        v = r.value if r.valid else None
        if isinstance(v, (int, float)) and math.isfinite(v) and abs(v) > ON_V:
            out[key] = float(v)
    return out


def ask_keep_outputs(parent, client) -> list[str] | None:
    """Before releasing control: the outputs to leave running (``[]`` = the usual cleanup), or
    None when the user cancelled the release."""
    on = outputs_on(client)
    if not on:
        return []
    preset = os.environ.get("SAMBA_MINI_KEEP_OUTPUTS", "").strip().lower()
    if preset in ("yes", "no"):                # unattended use (and the automated tests): no question
        return list(on) if preset == "yes" else []
    parts = []
    if "coil" in on:
        parts.append(f"the coil command is at {on['coil']:+.4g} V")
    if "lockin" in on:
        parts.append(f"the lock-in oscillator is at {on['lockin']:.4g} V")
    them = "it" if len(on) == 1 else "them"
    text = (" and ".join(parts)[0].upper() + " and ".join(parts)[1:] + ".\n\n"
            f"Leave {them} on after you release control?\n\n"
            f"Yes: {them} keep running with nobody in control of the setup, until someone switches "
            f"{them} off (Zero outputs / Osc. off) or the device service stops.\n"
            f"No: switch {them} off now (the usual cleanup).")
    ans = QMessageBox.question(parent, "Leave outputs on?", text,
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                               | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
    if ans == QMessageBox.StandardButton.Yes:
        return list(on)
    if ans == QMessageBox.StandardButton.No:
        return []
    return None
