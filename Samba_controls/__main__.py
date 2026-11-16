import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    from core.gui import splash
    from Samba_controls import APP_ID
    splash.start("samba_controls", APP_ID)          # on screen while the modules below load

from Samba_controls.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
