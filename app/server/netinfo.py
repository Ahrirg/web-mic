"""Discover usable local IP addresses and decide which remote clients are local."""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

VIRTUAL_PREFIXES = ("docker", "br-", "veth", "virbr", "vmnet", "vboxnet", "podman", "cni", "flannel", "lxc", "lxd")
VPN_PREFIXES = ("tailscale", "tun", "tap", "wg", "zt", "ppp", "nordlynx", "proton")


@dataclass(frozen=True)
class LocalAddress:
    interface: str
    address: str
    family: int  # 4 or 6
    prefixlen: int
    kind: str  # lan | wifi | vpn | virtual | loopback

    @property
    def network(self) -> ipaddress._BaseNetwork:
        return ipaddress.ip_network(f"{self.address}/{self.prefixlen}", strict=False)

    def url(self, port: int, scheme: str = "http") -> str:
        host = f"[{self.address}]" if self.family == 6 else self.address
        return f"{scheme}://{host}:{port}/"


def classify_interface(name: str) -> str:
    if name == "lo":
        return "loopback"
    if name.startswith(VIRTUAL_PREFIXES):
        return "virtual"
    if name.startswith(VPN_PREFIXES):
        return "vpn"
    if name.startswith(("wl", "wlan", "wifi")):
        return "wifi"
    return "lan"


def parse_ip_json(text: str) -> list[LocalAddress]:
    out: list[LocalAddress] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return out
    for iface in data:
        name = iface.get("ifname", "")
        flags = iface.get("flags", [])
        operstate = iface.get("operstate", "")
        kind = classify_interface(name)
        if kind != "loopback" and ("UP" not in flags or operstate == "DOWN" or "NO-CARRIER" in flags):
            continue
        for a in iface.get("addr_info", []):
            fam = a.get("family")
            local = a.get("local")
            if not local or fam not in ("inet", "inet6"):
                continue
            ip = ipaddress.ip_address(local)
            if fam == "inet6" and (ip.is_link_local or a.get("deprecated") or a.get("tentative")):
                continue  # link-local needs a zone id that browsers do not accept
            out.append(LocalAddress(name, local, 4 if fam == "inet" else 6, int(a.get("prefixlen", 0)), kind))
    return out


def _fallback_addresses() -> list[LocalAddress]:
    out = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # No packet is sent; this only asks the kernel which source address it would use.
            s.connect(("10.255.255.255", 1))
            addr = s.getsockname()[0]
            if not addr.startswith("127."):
                out.append(LocalAddress("default", addr, 4, 24, "lan"))
    except OSError:
        pass
    return out


def local_addresses(include_ipv6: bool = True) -> list[LocalAddress]:
    try:
        res = subprocess.run(["ip", "-j", "addr", "show"], capture_output=True, text=True, timeout=3)
        addrs = parse_ip_json(res.stdout) if res.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        addrs = []
    if not addrs:
        addrs = _fallback_addresses()
    if not include_ipv6:
        addrs = [a for a in addrs if a.family == 4]
    return addrs


def usable_lan_addresses(addrs: list[LocalAddress]) -> list[LocalAddress]:
    """Addresses a phone on the same network could use, best first."""
    order = {"wifi": 0, "lan": 1, "vpn": 3, "virtual": 4}
    picked = [a for a in addrs if a.kind in ("lan", "wifi", "vpn")]
    # Stable IPv6 privacy addresses rotate; prefer IPv4 and list IPv6 last.
    return sorted(picked, key=lambda a: (a.family, order.get(a.kind, 9)))


def normalize_ip(remote: str) -> ipaddress._BaseAddress | None:
    try:
        ip = ipaddress.ip_address((remote or "").split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def is_loopback(remote: str) -> bool:
    ip = normalize_ip(remote)
    return bool(ip and ip.is_loopback)


def is_local_client(remote: str, local: list[LocalAddress], allow_public: bool = False) -> bool:
    """True if the remote address is loopback, private, link-local or on a local subnet."""
    if allow_public:
        return True
    ip = normalize_ip(remote)
    if ip is None:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_private:
        return True
    for a in local:
        try:
            if ip.version == a.family and ip in a.network and a.prefixlen < (32 if a.family == 4 else 128):
                return True
        except (TypeError, ValueError):
            continue
    return False


def hostname() -> str:
    return socket.gethostname()
