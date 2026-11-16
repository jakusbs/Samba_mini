"""Start the device service.

    python -m Samba_controls.service [--config PATH] [--mode real|simulation]
                                     [--rpc-port N --event-port N] [--no-connect]

Normally started by Samba_controls.  Exit codes: 0 normal stop, 2 bad
arguments/configuration, 3 another service instance is running, 4 port in use.

Shutdown behaviour: on a ``server.shutdown`` request (from the managing
Samba_controls or an administrator), on Ctrl+C or on console close, running
operations are aborted, the configured cleanup runs (coil 0 V, lock-in
oscillator 0 V, stages stopped), devices are disconnected, then the process
exits.  If the process is killed instead, no cleanup runs; the outputs keep
their last values until the next service start (reported in the log).
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _acquire_instance_lock(state_dir: Path):
    """Exclusive lock file; held for the process lifetime (released by the OS on exit)."""
    lock_path = state_dir / "service.lock"
    fh = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        try:
            info = (state_dir / "service.pid").read_text(encoding="utf-8").strip()
        except OSError:
            info = ""
        return None, info
    # The locked byte stays locked for the process lifetime; who holds it is
    # recorded next to it (readable while locked).
    (state_dir / "service.pid").write_text(f"pid={os.getpid()}\n", encoding="utf-8")
    return fh, ""


def main(argv=None) -> int:
    from core import paths
    from core.config import machine

    ap = argparse.ArgumentParser(prog="Samba_controls.service")
    ap.add_argument("--config", help="machine configuration file (default: %%LOCALAPPDATA%%\\SambaMini\\config\\machine.toml)")
    ap.add_argument("--mode", choices=["real", "simulation"], help="override backend.mode")
    ap.add_argument("--rpc-port", type=int)
    ap.add_argument("--event-port", type=int)
    ap.add_argument("--no-connect", action="store_true", help="do not connect devices at start")
    ap.add_argument("--read-only", action="store_true",
                    help="diagnostics: connect and read only; no control, no writes, no cleanup writes, "
                         "no connect-time configuration writes (devices that need them stay disconnected)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    log_file = paths.logs_dir() / "service.log"
    handlers = [logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5,
                                                     encoding="utf-8"),
                logging.StreamHandler(sys.stderr)]
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    log = logging.getLogger("Samba_controls.service")

    state_dir = paths.state_dir()
    lock, holder = _acquire_instance_lock(state_dir)
    if lock is None:
        print(f"Another Samba_mini service is already running ({holder or 'lock held'}).", file=sys.stderr)
        return 3

    cfg = machine.load(args.config)
    for p in cfg.problems:
        log.warning("configuration: %s", p)
    if args.mode:
        cfg.data["backend"]["mode"] = args.mode   # devices stay disabled if the file was invalid
    if args.read_only:
        cfg.data["backend"]["read_only"] = True
        log.warning("READ-ONLY diagnostics mode: control, writes and cleanup writes are disabled")

    from Samba_controls.service.server import ServiceServer
    import zmq

    server = ServiceServer(cfg, paths.accounts_db_path(), state_dir,
                           control_token=os.environ.pop("SAMBA_MINI_CONTROL_TOKEN", None),
                           connect=not args.no_connect, rpc_port=args.rpc_port, event_port=args.event_port)
    try:
        server.bind()
    except zmq.ZMQError as exc:
        print(f"Cannot bind the service ports ({exc}); is another instance running?", file=sys.stderr)
        return 4

    def _sig(signum, frame):
        server.stop(f"signal {signum}")

    signal.signal(signal.SIGINT, _sig)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _sig)
    signal.signal(signal.SIGTERM, _sig)

    server.start_background()
    print(f"READY rpc={server.rpc_port} events={server.event_port} mode={cfg.mode} pid={os.getpid()}"
          + (" read-only" if server.read_only else ""),
          flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
