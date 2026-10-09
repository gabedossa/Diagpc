from datetime import datetime, timedelta, timezone

from diagcompleto import health
from diagcompleto.health import CRIT, INFO, OK, UNKNOWN, WARN


def statuses(items):
    return [i.status for i in items]


def test_healthy_ssd_is_ok():
    raw = {"disks": [{"name": "SSD", "media": "SSD", "size": 512e9, "health": "Healthy",
                      "wear": 3, "temperature": 40, "readErrors": 0, "writeErrors": 0}]}
    assert statuses(health.evaluate_disks(raw)) == [OK]


def test_worn_or_unhealthy_disk_is_critical():
    worn = {"disks": [{"name": "SSD", "media": "SSD", "health": "Healthy", "wear": 95}]}
    broken = {"disks": [{"name": "HD", "media": "HDD", "health": "Unhealthy"}]}
    assert statuses(health.evaluate_disks(worn)) == [CRIT]
    assert statuses(health.evaluate_disks(broken)) == [CRIT]


def test_hot_hdd_uses_lower_threshold_than_ssd():
    hdd = {"disks": [{"name": "HD", "media": "HDD", "health": "Healthy", "temperature": 55}]}
    ssd = {"disks": [{"name": "SSD", "media": "SSD", "health": "Healthy", "temperature": 55}]}
    assert statuses(health.evaluate_disks(hdd)) == [WARN]
    assert statuses(health.evaluate_disks(ssd)) == [OK]


def test_smart_failure_prediction_adds_critical_item():
    raw = {"disks": [{"name": "HD", "health": "Healthy"}], "smart": [{"predictFailure": True}]}
    assert CRIT in statuses(health.evaluate_disks(raw))


def test_missing_disk_data_is_unknown():
    assert statuses(health.evaluate_disks({})) == [UNKNOWN]


def test_low_free_space():
    gb = 1024 ** 3
    raw = {"volumes": [{"letter": "C", "total": 100 * gb, "free": 3 * gb},
                       {"letter": "D", "total": 100 * gb, "free": 8 * gb},
                       {"letter": "E", "total": 100 * gb, "free": 50 * gb}]}
    assert statuses(health.evaluate_volumes(raw)) == [CRIT, WARN, OK]


def test_battery_capacity():
    def battery(full):
        return {"battery": {"present": True, "design": 50000, "full": full, "charge": 80}}
    assert statuses(health.evaluate_battery(battery(45000))) == [OK]
    assert statuses(health.evaluate_battery(battery(30000))) == [WARN]
    assert statuses(health.evaluate_battery(battery(20000))) == [CRIT]
    assert health.evaluate_battery({"battery": {"present": False}}) == []


def test_events():
    raw = {"events": {"whea": 0, "bugcheck": 4, "unexpectedShutdown": 1, "diskSevere": 0, "diskRetry": 24}}
    items = {i.name: i.status for i in health.evaluate_events(raw)}
    assert items["Erros de hardware (WHEA)"] == OK
    assert items["Telas azuis (BSOD)"] == CRIT
    assert items["Desligamentos inesperados"] == WARN
    assert items["Operações de disco repetidas"] == INFO


def test_devices_ignore_disabled_and_disconnected():
    raw = {"problemDevices": [{"name": "A", "code": 22}, {"name": "B", "code": 45}]}
    assert statuses(health.evaluate_devices(raw)) == [OK]
    raw = {"problemDevices": [{"name": "Virtual", "code": 32}]}
    assert statuses(health.evaluate_devices(raw)) == [INFO]
    raw = {"problemDevices": [{"name": "Wi-Fi", "code": 10}]}
    assert statuses(health.evaluate_devices(raw)) == [WARN]


def test_security():
    assert statuses(health.evaluate_security({"antivirus": ["Windows Defender"],
                                              "defender": {"realtime": False}})) == [WARN]
    assert statuses(health.evaluate_security({"antivirus": ["Windows Defender"],
                                              "defender": {"realtime": True, "signatureAge": 1}})) == [OK]
    assert statuses(health.evaluate_security({"antivirus": ["Windows Defender", "Kaspersky"],
                                              "defender": {"realtime": False}})) == [OK]
    assert statuses(health.evaluate_security({})) == [WARN]


def test_uptime_suggests_restart():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    raw = {"os": {"lastBoot": (now - timedelta(days=10)).isoformat()}}
    assert statuses(health.evaluate_system(raw, now=now)) == [INFO]


def test_score():
    items = [health.HealthItem("x", "a", OK, ""), health.HealthItem("x", "b", WARN, "")]
    assert health.compute_score(items) == 92
    items.append(health.HealthItem("x", "c", CRIT, ""))
    assert health.compute_score(items) == 62
    assert health.compute_score([health.HealthItem("x", "c", CRIT, "")] * 5) == 0


def test_evaluate_survives_malformed_data():
    items = health.evaluate({"disks": "lixo", "volumes": [{"letter": "C"}], "events": None})
    assert isinstance(items, list)
