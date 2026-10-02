"""Derive a friendly device name from browser-provided information."""

from __future__ import annotations

import re

_ANDROID_MODEL = re.compile(r"Android [\d.]+;\s*(?:[a-z]{2}[-_][A-Za-z]{2};\s*)?([^;)]+?)(?:\s+Build/[^;)]*)?[;)]")
_BRANDS = {
    "SM-": "Samsung ",
    "Pixel": "",
    "moto": "Motorola ",
    "M2": "Xiaomi ",
    "CPH": "OPPO ",
    "RMX": "realme ",
    "ONEPLUS": "OnePlus ",
}


def browser_name(ua: str) -> str:
    ua = ua or ""
    for token, name in (
        ("SamsungBrowser", "Samsung Internet"),
        ("EdgA", "Edge"),
        ("Edg/", "Edge"),
        ("OPR/", "Opera"),
        ("Firefox", "Firefox"),
        ("FxiOS", "Firefox"),
        ("CriOS", "Chrome"),
        ("Chrome", "Chrome"),
        ("Safari", "Safari"),
    ):
        if token in ua:
            return name
    return "Browser"


def platform_name(ua: str) -> str:
    ua = ua or ""
    if "Android" in ua:
        return "Android"
    if "iPhone" in ua or "iPad" in ua:
        return "iOS"
    if "Windows" in ua:
        return "Windows"
    if "Mac OS X" in ua or "Macintosh" in ua:
        return "macOS"
    if "CrOS" in ua:
        return "ChromeOS"
    if "Linux" in ua:
        return "Linux"
    return "Unknown"


def android_model(ua: str) -> str:
    m = _ANDROID_MODEL.search(ua or "")
    if not m:
        return ""
    model = m.group(1).strip()
    # Chrome's reduced UA uses the literal model "K"
    if model in ("K", "Mobile", "wv") or len(model) < 2:
        return ""
    for prefix, brand in _BRANDS.items():
        if model.startswith(prefix) and not model.startswith(brand.strip()):
            return f"{brand}{model}".strip()
    return model


def friendly_name(device_name: str, user_agent: str, platform: str = "", browser: str = "") -> str:
    """Pick the most descriptive name available. A user alias always wins (handled by caller)."""
    if device_name:
        return device_name
    model = android_model(user_agent)
    plat = platform or platform_name(user_agent)
    brw = browser or browser_name(user_agent)
    if model:
        return f"{model} ({brw})"
    if "iPhone" in (user_agent or ""):
        return f"iPhone ({brw})"
    if "iPad" in (user_agent or ""):
        return f"iPad ({brw})"
    if plat and plat != "Unknown":
        return f"{plat} {brw}"
    return brw or "Unknown device"
