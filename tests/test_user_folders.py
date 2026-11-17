"""Per-account data folders: <root>\\<user name>, created at login, used by runs and the Data Browser."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import paths  # noqa: E402
from tests.conftest import ADMIN, OPERATOR  # noqa: E402

TEST = ("test", "test-password-1")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_until(app, cond, timeout=20.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    return cond()


def _window(service):
    from core.gui.bridge import ServiceBridge
    from Samba_mini.app import MainWindow
    bridge = ServiceBridge("Samba_mini", service.rpc_port, service.event_port)
    w = MainWindow(bridge, auto_login=False)
    bridge.probe()
    return w


def _login(w, user):
    w.bridge.client.login(*user)
    w._after_login()


def _settle(qapp, w):
    assert wait_until(qapp, lambda: w.store.state == "saved" and not w.store.pending()
                      and not w._ws_timer.isActive(), 10), w.store.state


def _close(w):
    orig = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard)
    try:
        w.close()
    finally:
        QMessageBox.question = orig


def test_each_account_gets_its_own_data_folder(qapp, service):
    root = paths.default_measurement_dir()
    boot = service.client("boot")
    boot.hello()
    boot.bootstrap(*ADMIN)
    boot.login(*ADMIN)
    boot.call("auth.create_user", {"username": TEST[0], "password": TEST[1]})
    boot.call("auth.create_user", {"username": OPERATOR[0], "password": OPERATOR[1]})
    # admin saved the shared root as its folder in an earlier version (the old default), and the
    # lab notebook in that root
    boot.call("prefs.set", {"values": {"data_dir": str(root),
                                       "lab_notebook": str(root / "lab_notebook_SambaMini.csv")}})
    w = _window(service)
    _login(w, ADMIN)
    own = root / "admin"
    assert own.is_dir()                                                  # created at login
    assert paths.same_path(w.dir_edit.text(), own) and paths.same_path(w.profile.data_dir.text(), own)
    assert paths.same_path(w.browser._save_dir_getter(), own)            # the Data Browser shows it
    assert w.store.get("lab_notebook") is None                          # follows the data folder now
    _settle(qapp, w)
    stored = boot.call("prefs.get")
    assert paths.same_path(stored["data_dir"], own) and "lab_notebook" not in stored
    w._login_logout()

    # "test" has no folder yet: its own is created, and its measurement goes there
    _login(w, TEST)
    own_test = root / "test"
    assert own_test.is_dir() and paths.same_path(w._data_dir(), own_test)
    w.trajectory.set_workflow("time")
    w.trajectory.set_params("time", {"duration": 0.2, "interval": 0.05, "acq_time": 0.01, "time_const": 0.1,
                                     "volt": 0.0, "position_stage": False})
    w.bottom_tabs.setCurrentWidget(w.trajectory)
    w.start_run()
    assert wait_until(qapp, lambda: w.engine is not None and w.engine.result is not None and w.start_btn.isEnabled(), 60)
    assert w.engine.result["status"] == "completed"
    assert paths.same_path(os.path.commonpath([w.engine.result["path"], str(own_test)]), own_test)
    assert not list(own.rglob("*.h5"))                                   # nothing in admin's folder
    w._login_logout()

    # a folder chosen by the user is kept; "Own folder" goes back
    _login(w, OPERATOR)
    custom = root.parent / "alice-elsewhere"
    w.dir_edit.setText(str(custom))
    _settle(qapp, w)
    w._login_logout()
    _login(w, OPERATOR)
    assert paths.same_path(w._data_dir(), custom) and custom.is_dir()
    w.profile.own_btn.click()
    assert paths.same_path(w._data_dir(), root / "alice")
    _close(w)


def test_user_folder_names_are_safe():
    assert paths.user_measurement_dir("bob.smith").name == "bob.smith"
    assert paths.user_measurement_dir("..").name == "user"
    assert paths.same_path("C:/Data/x/", "c:\\data\\X") if os.name == "nt" else True
