"""Backend device service (Qt-free).  Run with ``python -m Samba_controls.service``.

Only this package may import hardware driver packages (nidaqmx, pyvisa,
pythonnet/clr, pylablib, pytrinamic).  The measurement GUI talks to it through
``core.client``.
"""
