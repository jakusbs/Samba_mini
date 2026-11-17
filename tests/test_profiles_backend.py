"""Backend rules for presets and calibration records (account store and service methods).

* Preset rename / copy are atomic and never overwrite another preset.
* Calibration records are immutable, versioned, owned by an account and carry their
  provenance; the old ``bd_calibration_<setup>`` preferences migrate with a backup and
  without inventing a sample, mode or source.
"""

import json
import sqlite3
import time

import pytest

from core.protocol import ErrorCode, ServiceError
from tests.conftest import ADMIN, OPERATOR

CAL = [-4.0, -2.5, -0.5, 1.0, 2.5, 4.5]


def _store(path):
    from Samba_controls.service.auth import AccountStore
    return AccountStore(path)


# --------------------------------------------------------------------------- presets
def test_preset_rename_and_copy_refuse_collisions(tmp_path):
    a = _store(tmp_path / "acc.sqlite3")
    u = a.create_user("bob", "correct horse 1", "admin")
    wf = "samba_mini/longitudinal"
    a.save_preset(u["id"], wf, "A", {"workflow": "time", "params": {"b": 1.0}})
    a.save_preset(u["id"], wf, "B", {"workflow": "time", "params": {"b": 2.0}})

    with pytest.raises(ServiceError) as exc:                  # A -> existing B: refused, B intact
        a.rename_preset(u["id"], wf, "A", "B")
    assert exc.value.code == ErrorCode.CONFLICT
    assert a.get_preset(u["id"], wf, "B")["value"]["params"]["b"] == 2.0
    assert a.get_preset(u["id"], wf, "A")["value"]["params"]["b"] == 1.0

    with pytest.raises(ServiceError) as exc:                  # copy onto an existing name: refused
        a.copy_preset(u["id"], wf, "A", "B")
    assert exc.value.code == ErrorCode.CONFLICT
    with pytest.raises(ServiceError) as exc:                  # "New" with a taken name: refused
        a.save_preset(u["id"], wf, "B", {"workflow": "time", "params": {}}, mode="create")
    assert exc.value.code == ErrorCode.CONFLICT
    assert a.get_preset(u["id"], wf, "B")["value"]["params"]["b"] == 2.0

    renamed = a.rename_preset(u["id"], wf, "A", "C")          # one transaction: no window with both/neither
    assert renamed["name"] == "C" and a.get_preset(u["id"], wf, "A") is None
    copied = a.copy_preset(u["id"], wf, "C", "D")             # copies the STORED value
    assert copied["value"] == renamed["value"]
    with pytest.raises(ServiceError) as exc:                  # Save into a preset deleted meanwhile
        a.save_preset(u["id"], wf, "gone", {"workflow": "time"}, mode="update")
    assert exc.value.code == ErrorCode.CONFLICT
    assert a.get_preset(u["id"], wf, "gone") is None
    with pytest.raises(ServiceError):
        a.rename_preset(u["id"], wf, "C", " padded ")
    a.close()


def test_preset_rename_through_service(service):
    c = service.client("ui")
    c.hello()
    assert c.has_feature("presets-atomic-1") and c.has_feature("calibration-records-1")
    c.bootstrap(*ADMIN)
    c.login(*ADMIN)
    wf = "samba_mini/polar"
    c.call("presets.save", {"workflow": wf, "name": "A", "value": {"workflow": "time"}, "mode": "create"})
    c.call("presets.save", {"workflow": wf, "name": "B", "value": {"workflow": "x_sweep"}, "mode": "create"})
    with pytest.raises(ServiceError) as exc:
        c.call("presets.rename", {"workflow": wf, "name": "A", "new_name": "B"})
    assert exc.value.code == ErrorCode.CONFLICT
    names = {p["name"]: p["value"]["workflow"] for p in c.call("presets.list", {"workflow": wf})}
    assert names == {"A": "time", "B": "x_sweep"}


# --------------------------------------------------------------------------- calibration records
def test_calibration_records_are_versioned_owned_and_carry_provenance(service):
    admin = service.client("admin")
    admin.hello()
    admin.bootstrap(*ADMIN)
    admin.login(*ADMIN)
    admin.call("auth.create_user", {"username": OPERATOR[0], "password": OPERATOR[1]})
    t0 = time.time()
    rec = admin.call("calibration.save", {"record": {
        "setup": "longitudinal", "sample_id": "S1", "values": CAL, "unit": "mV", "mode": "simulation",
        "origin": "fit", "source_file": "D:/data/x_calibration.h5", "fit": {"n_plateaus": 6},
        "name": "S1 λ/2", "creator": "somebody-else"}})                     # creator comes from the session
    assert rec["version"] == 1 and rec["series"] == rec["id"]
    assert rec["creator"] == ADMIN[0] and rec["created"] >= t0 - 1
    assert rec["values"] == CAL and rec["unit"] == "mV" and rec["mode"] == "simulation"
    assert rec["sample_id"] == "S1" and rec["source_file"].endswith("x_calibration.h5")
    assert rec["service_mode"] == "simulation" and rec["fit"] == {"n_plateaus": 6}

    v2 = admin.call("calibration.save", {"record": dict(rec, values=[v + 1 for v in CAL], origin="manual"),
                                         "base": rec["id"]})
    assert v2["series"] == rec["series"] and v2["version"] == 2 and v2["id"] != rec["id"]
    assert admin.call("calibration.get", {"id": rec["id"]})["values"] == CAL     # v1 unchanged
    with pytest.raises(ServiceError):                                            # setups never mix
        admin.call("calibration.save", {"record": dict(rec, setup="polar"), "base": rec["id"]})
    with pytest.raises(ServiceError):
        admin.call("calibration.save", {"record": dict(rec, values=CAL[:5])})
    with pytest.raises(ServiceError):
        admin.call("calibration.activate", {"setup": "polar", "id": rec["id"], "sample_id": "S1"})

    act = admin.call("calibration.activate", {"setup": "longitudinal", "id": v2["id"], "sample_id": "S1"})
    assert act["record"]["id"] == v2["id"] and act["sample_id"] == "S1"
    assert admin.call("calibration.active", {"setup": "polar"})["record"] is None

    op = service.client("op")                                                    # another account
    op.hello()
    op.login(*OPERATOR)
    assert op.call("calibration.list", {"setup": "longitudinal"}) == []
    with pytest.raises(ServiceError):
        op.call("calibration.get", {"id": rec["id"]})
    assert op.call("calibration.active", {"setup": "longitudinal"})["record"] is None

    admin.call("calibration.activate", {"setup": "longitudinal", "id": None})
    assert admin.call("calibration.active", {"setup": "longitudinal"})["record"] is None


def _v1_database(path):
    """An accounts database as the previous release left it (schema 1)."""
    from Samba_controls.service import auth
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied REAL NOT NULL)")
    for stmt in auth._MIGRATIONS[1]:
        db.execute(stmt)
    db.execute("INSERT INTO schema_version VALUES (1, 0)")
    db.execute("INSERT INTO users (username, pw_hash, role, created) VALUES ('bob', 'x', 'admin', 0)")
    db.execute("INSERT INTO users (username, pw_hash, role, created) VALUES ('eve', 'x', 'operator', 0)")
    prefs = [(1, "bd_calibration_longitudinal", CAL), (1, "bd_calibration_date_longitudinal", "2026-11-10 14:05"),
             (1, "bd_calibration_polar", [0.0] * 6), (1, "data_dir", "D:/bob"),
             (2, "bd_calibration_polar", [1, 2, 3, 4, 5, 6])]            # no date saved
    for uid, key, value in prefs:
        db.execute("INSERT INTO prefs VALUES (?,?,?,0)", (uid, key, json.dumps(value)))
    db.commit()
    db.close()


def test_calibration_preferences_migrate_with_backup_and_unknown_provenance(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    _v1_database(path)
    a = _store(path)
    assert a.schema_version() == 2
    backups = list(tmp_path.glob("accounts.sqlite3.v1.*.bak"))
    assert len(backups) == 1                                         # copied before migrating
    old = sqlite3.connect(backups[0])
    assert old.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
    old.close()

    bob = a.active_calibration(1, "longitudinal")["record"]
    assert bob["values"] == CAL and bob["origin"] == "migrated" and bob["creator"] == "bob"
    assert bob["sample_id"] is None and bob["mode"] is None and bob["source_file"] is None   # unknown
    assert bob["fit"] is None
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(bob["created"])) == "2026-11-10 14:05"
    assert a.active_calibration(1, "longitudinal")["sample_id"] is None
    assert a.active_calibration(1, "polar")["record"] is None             # all zeros meant "none"
    eve = a.active_calibration(2, "polar")["record"]
    assert eve["values"] == [1, 2, 3, 4, 5, 6] and eve["created"] is None  # no date: unknown, not "now"
    assert a.get_prefs(1)["data_dir"] == "D:/bob"                          # other preferences untouched
    assert a.get_prefs(1)["bd_calibration_longitudinal"] == CAL            # old keys kept (downgrade)
    a.close()
    b = _store(path)                                                       # reopening migrates nothing twice
    assert len(b.list_calibrations(1)) == 1 and len(b.list_calibrations(2)) == 1
    assert len(list(tmp_path.glob("accounts.sqlite3.v1.*.bak"))) == 1
    b.close()
