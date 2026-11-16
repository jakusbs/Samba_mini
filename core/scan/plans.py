"""Pure functions that turn workflow parameters into point sequences and timing.

Kept free of I/O so they can be tested against MiniMoke's formulas.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

HALL_MIN_ACQ_S = 0.002        # MiniMoke HALL_MIN_ACQ_S
SETTLE_FRACTION = 0.05        # MiniMoke DC B-sweep
SETTLE_TC_MULTIPLES = 5       # MiniMoke lock-in B-sweep
DAC_SAMPLING_RATE = 50_000.0
HALL_MAX_INTEGRATION_S = 255 / 470.0


def n_points(lo: float, hi: float, step: float) -> int:
    """MiniMoke ``int(|hi-lo| // step + 1)``, robust to float rounding (F7)."""
    if not step or step <= 0:
        return 1
    ratio = abs(hi - lo) / step
    return int(math.floor(ratio + 1e-9)) + 1


def axis_values(lo: float, hi: float, step: float) -> np.ndarray:
    return np.linspace(lo, hi, n_points(lo, hi, step), endpoint=True)


@dataclass
class FieldSweepPlan:
    forward: np.ndarray
    backward: np.ndarray           # without the shared end point
    setpoints: np.ndarray          # forward then backward: one full sweep
    branch: np.ndarray             # 0 forward, 1 backward
    n_total: int
    t_point_s: float
    settle_s: float
    acq_time_s: float
    hall_live: bool
    hall_integration_s: float
    actual_step: float


def field_sweep(b_min: float, b_max: float, b_step: float) -> tuple[np.ndarray, np.ndarray]:
    n = n_points(b_min, b_max, b_step)
    fwd = np.linspace(b_min, b_max, n, endpoint=True)
    bwd = np.flip(fwd)[1:].copy()
    return fwd, bwd


def b_sweep_dc_plan(b_min, b_max, b_step, sweep_freq) -> FieldSweepPlan:
    fwd, bwd = field_sweep(b_min, b_max, b_step)
    n_total = len(fwd) + len(bwd)
    t_point = 1.0 / (sweep_freq * n_total)
    settle = t_point * SETTLE_FRACTION
    acq = max(1.0 / DAC_SAMPLING_RATE, t_point * (1.0 - SETTLE_FRACTION))
    hall_live = t_point >= HALL_MIN_ACQ_S
    hall_acq = min(acq, HALL_MIN_ACQ_S) if hall_live else HALL_MIN_ACQ_S
    return FieldSweepPlan(fwd, bwd, np.concatenate([fwd, bwd]),
                          np.concatenate([np.zeros(len(fwd), int), np.ones(len(bwd), int)]),
                          n_total, t_point, settle, acq, hall_live, hall_acq,
                          (fwd[1] - fwd[0]) if len(fwd) > 1 else 0.0)


def b_sweep_lockin_plan(b_min, b_max, b_step, sweep_freq, time_const, acq_time) -> FieldSweepPlan:
    fwd, bwd = field_sweep(b_min, b_max, b_step)
    n_total = len(fwd) + len(bwd)
    settle = SETTLE_TC_MULTIPLES * time_const
    t_point = max(settle + acq_time, 1.0 / (sweep_freq * n_total))
    hall_live = t_point >= HALL_MIN_ACQ_S
    hall_acq = min(acq_time, HALL_MIN_ACQ_S) if hall_live else HALL_MIN_ACQ_S
    return FieldSweepPlan(fwd, bwd, np.concatenate([fwd, bwd]),
                          np.concatenate([np.zeros(len(fwd), int), np.ones(len(bwd), int)]),
                          n_total, t_point, settle, acq_time, hall_live, hall_acq,
                          (fwd[1] - fwd[0]) if len(fwd) > 1 else 0.0)


def b_sweep_dc_validation(b_min, b_max, b_step, sweep_freq) -> str | None:
    """MiniMoke ``B_Sweep.queue_validation_error``."""
    fwd, bwd = field_sweep(b_min, b_max, b_step)
    n_total = len(fwd) + len(bwd)
    f_max = 1.0 / (n_total * HALL_MIN_ACQ_S)
    if sweep_freq > f_max:
        return (f"Sweep frequency {sweep_freq:g} Hz is too high for the Hall probe: {n_total} points per "
                f"sweep need at least {HALL_MIN_ACQ_S * 1e3:g} ms each, so the maximum at this step size "
                f"is {f_max:.3g} Hz. Lower the sweep frequency or increase the step.")
    return None


def b_sweep_lockin_validation(b_min, b_max, b_step, sweep_freq, time_const, acq_time) -> str | None:
    plan = b_sweep_lockin_plan(b_min, b_max, b_step, sweep_freq, time_const, acq_time)
    if plan.t_point_s < HALL_MIN_ACQ_S:
        return (f"Even after lock-in settling each point would take {plan.t_point_s * 1e3:.2g} ms, below "
                f"the {HALL_MIN_ACQ_S * 1e3:g} ms the Hall probe needs.")
    return None


@dataclass
class PositionPoint:
    x_um: float
    y_um: float
    coil_V: float
    iteration: int     # MiniMoke "Iteration" (position index along the scan axis)
    loop: int          # MiniMoke "Loop"
    ix: int
    iy: int
    rep: int = 0


def x_sweep_points(x_min, x_max, x_step, y, b, repeat_num) -> tuple[list[PositionPoint], np.ndarray]:
    xs = axis_values(x_min, x_max, x_step)
    field_seq = [b, -b] * int(repeat_num)
    pts = [PositionPoint(float(xv), float(y), float(f), i, loop, i, 0)
           for loop, f in enumerate(field_seq) for i, xv in enumerate(xs)]
    return pts, xs


def y_sweep_points(y_min, y_max, y_step, x, b, repeat_num) -> tuple[list[PositionPoint], np.ndarray]:
    ys = axis_values(y_min, y_max, y_step)
    field_seq = [b, -b] * int(repeat_num)
    pts = [PositionPoint(float(x), float(yv), float(f), i, loop, i, 0)
           for loop, f in enumerate(field_seq) for i, yv in enumerate(ys)]
    return pts, ys


def xy_points(x_min, x_max, x_step, y_min, y_max, y_step, b, repeat_num):
    """Serpentine in y per column, whole map repeated (MiniMoke XY_Sweep)."""
    xs = axis_values(x_min, x_max, x_step)
    ys = axis_values(y_min, y_max, y_step)
    pts = []
    loop = 0
    for rep in range(max(int(repeat_num), 1)):
        for i, xv in enumerate(xs):
            order = range(len(ys)) if i % 2 == 0 else range(len(ys) - 1, -1, -1)
            for j in order:
                pts.append(PositionPoint(float(xv), float(ys[j]), float(b), i, loop, i, j, rep))
            loop += 1
    return pts, xs, ys


def time_points(duration: float, interval: float) -> int:
    """MiniMoke: max(1, int(duration / interval))."""
    return max(1, int(duration / interval + 1e-9))


def hall_integration_for(acq_time: float) -> float:
    return min(acq_time, HALL_MAX_INTEGRATION_S)
