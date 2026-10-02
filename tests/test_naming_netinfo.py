import json

from app.devices.naming import android_model, browser_name, friendly_name, platform_name
from app.server import netinfo

UA_PIXEL = "Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP1A.240305.019) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
UA_REDUCED = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36"
UA_SAMSUNG = "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0 Mobile Safari/537.36"
UA_FIREFOX = "Mozilla/5.0 (Android 14; Mobile; rv:125.0) Gecko/125.0 Firefox/125.0"
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"


def test_browser_and_platform():
    assert browser_name(UA_PIXEL) == "Chrome" and browser_name(UA_SAMSUNG) == "Samsung Internet"
    assert browser_name(UA_FIREFOX) == "Firefox" and browser_name(UA_IPHONE) == "Safari"
    assert platform_name(UA_PIXEL) == "Android" and platform_name(UA_IPHONE) == "iOS"


def test_android_model():
    assert android_model(UA_PIXEL) == "Pixel 8"
    assert android_model(UA_SAMSUNG) == "Samsung SM-S911B"
    assert android_model(UA_REDUCED) == ""


def test_friendly_name():
    assert friendly_name("", UA_PIXEL) == "Pixel 8 (Chrome)"
    assert friendly_name("My Phone", UA_PIXEL) == "My Phone"
    assert friendly_name("", UA_REDUCED) == "Android Chrome"
    assert friendly_name("", UA_FIREFOX) == "Android Firefox"
    assert friendly_name("", UA_IPHONE) == "iPhone (Safari)"
    assert friendly_name("", "") == "Browser"


IP_JSON = json.dumps([
    {"ifname": "lo", "flags": ["LOOPBACK", "UP"], "operstate": "UNKNOWN",
     "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
    {"ifname": "enp5s0", "flags": ["BROADCAST", "UP", "LOWER_UP"], "operstate": "UP",
     "addr_info": [{"family": "inet", "local": "192.168.1.50", "prefixlen": 24},
                   {"family": "inet6", "local": "fd00::5", "prefixlen": 64},
                   {"family": "inet6", "local": "fe80::1", "prefixlen": 64}]},
    {"ifname": "wlan0", "flags": ["BROADCAST", "UP", "LOWER_UP"], "operstate": "UP",
     "addr_info": [{"family": "inet", "local": "10.0.0.5", "prefixlen": 8}]},
    {"ifname": "docker0", "flags": ["NO-CARRIER", "BROADCAST", "UP"], "operstate": "DOWN",
     "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]},
    {"ifname": "tailscale0", "flags": ["UP"], "operstate": "UNKNOWN",
     "addr_info": [{"family": "inet", "local": "100.68.62.62", "prefixlen": 32}]},
])


def test_parse_ip_json_and_ordering():
    addrs = netinfo.parse_ip_json(IP_JSON)
    names = [(a.interface, a.address) for a in addrs]
    assert ("docker0", "172.17.0.1") not in names  # down
    assert ("enp5s0", "fe80::1") not in names  # link-local skipped
    usable = netinfo.usable_lan_addresses(addrs)
    assert [a.address for a in usable] == ["10.0.0.5", "192.168.1.50", "100.68.62.62", "fd00::5"]
    assert usable[-1].url(8765) == "http://[fd00::5]:8765/"


def test_is_local_client():
    local = netinfo.parse_ip_json(IP_JSON)
    assert netinfo.is_local_client("127.0.0.1", local)
    assert netinfo.is_local_client("::ffff:192.168.1.77", local)
    assert netinfo.is_local_client("192.168.1.77", local)
    assert netinfo.is_local_client("fd00::9", local)
    assert not netinfo.is_local_client("8.8.8.8", local)
    assert not netinfo.is_local_client("100.70.1.1", local)  # CGNAT/VPN peer not on a local subnet
    assert netinfo.is_local_client("8.8.8.8", local, allow_public=True)
    assert not netinfo.is_local_client("garbage", local)


def test_loopback():
    assert netinfo.is_loopback("::1") and netinfo.is_loopback("::ffff:127.0.0.1")
    assert not netinfo.is_loopback("192.168.0.2")
