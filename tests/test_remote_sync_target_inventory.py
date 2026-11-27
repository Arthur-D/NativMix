from __future__ import annotations

from types import SimpleNamespace

import pytest

from nativmix.remote_sync.target_inventory import ReceiverTargetInventory


class _Config:
    def all_channels(self):
        return [{"app_names": ["Configured App", "System Master"]}]


class _Backend:
    def get_active_streams(self):
        return [
            SimpleNamespace(
                app_name="Discord",
                index=987,
                pid=1234,
                props={"application.process.binary": "/app/bin/discord"},
            )
        ]

    def get_real_sinks(self):
        return [("USB Headset", "alsa_output.usb-secret-path")]

    def get_real_sources(self):
        return [("USB Microphone", "alsa_input.usb-secret-path")]


def test_inventory_exposes_only_stable_keys_and_normalized_names() -> None:
    inventory = ReceiverTargetInventory(_Config(), _Backend())
    wire = [item.to_canonical() for item in inventory()]

    assert {item["label"] for item in wire} == {
        "System Master",
        "Other Apps",
        "Configured App",
        "Discord",
        "USB Headset",
        "USB Microphone",
    }
    encoded = repr(wire)
    assert "987" not in encoded
    assert "1234" not in encoded
    assert "/app/bin" not in encoded
    assert "alsa_output.usb-secret-path" not in encoded


def test_inventory_resolves_only_typed_current_keys() -> None:
    inventory = ReceiverTargetInventory(_Config(), _Backend())
    by_label = {item.label: item.key for item in inventory()}

    assert inventory.resolve_mapping_keys(
        [by_label["Discord"], by_label["System Master"]]
    ) == ["Discord", "System Master"]
    assert inventory.resolve_hardware_key(by_label["USB Headset"]) == "sink:alsa_output.usb-secret-path"
    assert inventory.resolve_hardware_key(by_label["USB Microphone"]) == "source:alsa_input.usb-secret-path"
    assert next(item.kind for item in inventory() if item.label == "USB Headset") == "output"
    assert next(item.kind for item in inventory() if item.label == "USB Microphone") == "input"
    with pytest.raises(KeyError):
        inventory.resolve_mapping_keys([by_label["USB Headset"]])
    with pytest.raises(KeyError):
        inventory.resolve_hardware_key(by_label["Discord"])


def test_hardware_key_stays_stable_when_configured_device_becomes_unavailable() -> None:
    class Config:
        def all_channels(self):
            return [{"app_names": [], "hardware_id": "alsa_output.usb-stable"}]

    class Backend:
        sinks = [("USB Headset", "alsa_output.usb-stable")]

        def get_active_streams(self):
            return []

        def get_real_sinks(self):
            return self.sinks

        def get_real_sources(self):
            return []

    backend = Backend()
    inventory = ReceiverTargetInventory(Config(), backend)
    available = next(item for item in inventory() if item.label == "USB Headset")
    backend.sinks = []
    assert inventory.refresh()
    unavailable = next(item for item in inventory() if item.key == available.key)
    assert unavailable.label == "USB Headset"
    assert not unavailable.available


def test_prefixed_configured_hardware_matches_receiver_sink_inventory() -> None:
    class Config:
        def all_channels(self):
            return [
                {
                    "app_names": [],
                    "hardware_id": "sink:alsa_output.usb-scarlett",
                }
            ]

    class Backend(_Backend):
        def get_real_sinks(self):
            return [("Scarlett 4i4", "alsa_output.usb-scarlett")]

    inventory = ReceiverTargetInventory(Config(), Backend())
    scarlett = next(item for item in inventory() if item.label == "Scarlett 4i4")

    assert scarlett.available
    assert scarlett.key == inventory.key_for_hardware_value("sink:alsa_output.usb-scarlett")
