import subprocess

from app.adb.adb import AdbManager, explain_reverse_error, parse_devices, parse_reverse_list

DEVICES = """List of devices attached
1A2B3C	device usb:1-4 product:husky model:Pixel_8_Pro device:husky transport_id:3
ZY22	unauthorized usb:1-3 transport_id:4
0123	no permissions (user in plugdev group; are your udev rules wrong?); see [http://developer.android.com/tools/device.html] usb:1-2 transport_id:5
192.168.1.5:5555	offline
"""


def test_parse_devices():
    devs = parse_devices(DEVICES)
    assert [d.serial for d in devs] == ["1A2B3C", "ZY22", "0123", "192.168.1.5:5555"]
    assert devs[0].ready and devs[0].name == "Pixel 8 Pro"
    assert devs[1].state == "unauthorized" and "Allow USB debugging" in devs[1].state_message()
    assert devs[2].state == "no permissions" and "udev" in devs[2].state_message()
    assert devs[3].is_network and not devs[3].ready
    assert parse_devices("List of devices attached\n\n") == []
    assert parse_devices("* daemon not running; starting now at tcp:5037\n* daemon started successfully\nList of devices attached\n") == []


def test_parse_reverse_list():
    assert parse_reverse_list("UsbFfs tcp:8765 tcp:8765\n") == [("UsbFfs", "tcp:8765", "tcp:8765")]
    assert parse_reverse_list("tcp:1 tcp:2") == [("", "tcp:1", "tcp:2")]


def test_error_explanations():
    assert "authorized" in explain_reverse_error("error: device unauthorized.")
    assert "No Android device" in explain_reverse_error("error: no devices/emulators found")
    assert "does not support" in explain_reverse_error("error: unknown command reverse")


class FakeAdb:
    def __init__(self, devices=DEVICES):
        self.calls = []
        self.devices = devices
        self.reverses = set()
        self.fail_reverse = ""

    def __call__(self, args, timeout=5.0):
        self.calls.append(args)
        a = args[1:]
        serial = None
        if a[0] == "-s":
            serial, a = a[1], a[2:]
        if a == ["version"]:
            return subprocess.CompletedProcess(args, 0, "Android Debug Bridge version 1.0.41\n", "")
        if a == ["devices", "-l"]:
            return subprocess.CompletedProcess(args, 0, self.devices, "")
        if a[0] == "reverse":
            if a[1] == "--list":
                out = "".join(f"{s} {r} {r}\n" for s, r in self.reverses if s == serial)
                return subprocess.CompletedProcess(args, 0, out, "")
            if a[1] == "--remove":
                self.reverses.discard((serial, a[2]))
                return subprocess.CompletedProcess(args, 0, "", "")
            if self.fail_reverse:
                return subprocess.CompletedProcess(args, 1, "", self.fail_reverse)
            self.reverses.add((serial, a[1]))
            return subprocess.CompletedProcess(args, 0, "8765\n", "")
        return subprocess.CompletedProcess(args, 1, "", "unknown")


def make(fake, port=8765):
    m = AdbManager(lambda: port, runner=fake)
    m.adb_path = "adb"
    m.detect()
    return m


def test_detect_version_and_devices():
    fake = FakeAdb()
    m = make(fake)
    assert m.version == "1.0.41"
    devs = m.refresh()
    assert len(devs) == 4 and m.server_running


def test_enable_reverse_runs_expected_command():
    fake = FakeAdb()
    m = make(fake)
    m.refresh()
    ok = m.enable_reverse("1A2B3C")
    assert ok and m.reversed == {"1A2B3C": 8765}
    assert ["adb", "-s", "1A2B3C", "reverse", "tcp:8765", "tcp:8765"] in fake.calls
    snap = m.snapshot()
    assert snap["devices"][0]["reverse"] is True


def test_reverse_error_is_explained():
    fake = FakeAdb()
    fake.fail_reverse = "error: device unauthorized."
    m = make(fake)
    assert not m.enable_reverse("ZY22")
    assert "authorized" in m.errors["ZY22"]


def test_disconnect_and_reconnect_reapplies_reverse():
    fake = FakeAdb()
    m = make(fake)
    m.refresh()
    m.enable_reverse("1A2B3C")
    fake.devices = "List of devices attached\n"
    m.refresh()
    assert "1A2B3C" not in m.reversed  # unplugged
    fake.reverses.clear()
    fake.devices = DEVICES
    m.refresh()  # plugged in again: reverse re-applied automatically
    assert m.reversed.get("1A2B3C") == 8765
    assert ("1A2B3C", "tcp:8765") in fake.reverses


def test_auto_all_enables_every_authorized_device():
    fake = FakeAdb()
    m = make(fake)
    m.auto_all = True
    m.refresh()
    assert list(m.reversed) == ["1A2B3C"]


def test_disable_and_remove_all():
    fake = FakeAdb()
    m = make(fake)
    m.refresh()
    m.enable_reverse("1A2B3C")
    m.disable_reverse("1A2B3C")
    assert not fake.reverses and not m.reversed
    m.enable_reverse("1A2B3C")
    m.remove_all()
    assert not fake.reverses


def test_missing_adb(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    m = AdbManager(lambda: 8765)
    m.detect()
    assert not m.available and "not found" in m.last_error
    assert m.refresh() == []


def test_port_change_moves_forwarding():
    fake = FakeAdb()
    port = {"p": 8765}
    m = AdbManager(lambda: port["p"], runner=fake)
    m.adb_path = "adb"
    m.refresh()
    m.enable_reverse("1A2B3C")
    port["p"] = 9000
    m.remove_all()  # what AppCore.restart_server does
    m.refresh()
    assert fake.reverses == {("1A2B3C", "tcp:9000")}
    assert m.reversed == {"1A2B3C": 9000}
