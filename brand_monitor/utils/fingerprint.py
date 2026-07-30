"""Stable device fingerprint generation for Telethon clients."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceFingerprint:
    device_model: str
    system_version: str
    app_version: str
    lang_code: str


# Realistic mobile fingerprints — generated once per agent and persisted.
_FINGERPRINT_POOL: tuple[DeviceFingerprint, ...] = (
    DeviceFingerprint("iPhone 13", "iOS 16.6", "10.2.2", "ru"),
    DeviceFingerprint("iPhone 14 Pro", "iOS 17.2", "10.5.1", "ru"),
    DeviceFingerprint("iPhone 15", "iOS 17.5", "10.14.5", "en"),
    DeviceFingerprint("Samsung Galaxy S23", "SDK 34", "10.14.5", "ru"),
    DeviceFingerprint("Samsung Galaxy A54", "SDK 33", "10.8.1", "ru"),
    DeviceFingerprint("Xiaomi Redmi Note 12", "SDK 33", "10.6.2", "ru"),
    DeviceFingerprint("Xiaomi 13T", "SDK 34", "10.13.1", "ru"),
    DeviceFingerprint("Google Pixel 7", "SDK 34", "10.12.0", "en"),
    DeviceFingerprint("Huawei P60", "SDK 31", "10.3.2", "ru"),
    DeviceFingerprint("OnePlus 11", "SDK 34", "10.11.3", "en"),
    DeviceFingerprint("iPhone 12 Mini", "iOS 16.3", "9.7.6", "ru"),
    DeviceFingerprint("Samsung Galaxy S22", "SDK 33", "10.2.5", "ru"),
)


def generate_fingerprint(rng: random.Random | None = None) -> DeviceFingerprint:
    """Pick a random but realistic device fingerprint."""
    chooser = rng.choice if rng is not None else random.choice
    return chooser(_FINGERPRINT_POOL)


def fingerprint_dict(fp: DeviceFingerprint) -> dict[str, str]:
    return {
        "device_model": fp.device_model,
        "system_version": fp.system_version,
        "app_version": fp.app_version,
        "lang_code": fp.lang_code,
    }
