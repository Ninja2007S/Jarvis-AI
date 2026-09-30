"""Local BLE beacon used for phone handoff proximity (Windows desktop)."""
from __future__ import annotations

import json
import platform
import time
import uuid
from pathlib import Path


class PhoneProximityBeacon:
    """Advertise a stable, per-install BLE service UUID; never advertises user data."""

    def __init__(self, config_dir: Path):
        self.config_path = Path(config_dir) / "phone_handoff.json"
        self.service_uuid = self._load_uuid()
        self.publisher = None
        self.error = ""

    def _load_uuid(self) -> str:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            value = str(uuid.UUID(data.get("service_uuid", "")))
        except Exception:
            value = str(uuid.uuid4())
            try:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(json.dumps({"service_uuid": value}, indent=2), encoding="utf-8")
            except OSError:
                pass
        return value

    def start(self) -> bool:
        if platform.system() != "Windows":
            self.error = "BLE desktop advertising is currently supported on Windows."
            return False
        try:
            from winrt.windows.devices.bluetooth.advertisement import (
                BluetoothLEAdvertisement, BluetoothLEAdvertisementPublisher,
                BluetoothLEManufacturerData,
            )
            from winrt.windows.storage.streams import DataWriter
            advertisement = BluetoothLEAdvertisement()
            # Windows does not allow an app publisher to set the reserved
            # 128-bit service-UUID-list AD types. Carry our private UUID as
            # manufacturer-specific bytes instead (company ID 0xFFFE).
            data_writer = DataWriter()
            data_writer.write_bytes(uuid.UUID(self.service_uuid).bytes)
            manufacturer_data = BluetoothLEManufacturerData()
            manufacturer_data.company_id = 0xFFFE
            manufacturer_data.data = data_writer.detach_buffer()
            advertisement.manufacturer_data.append(manufacturer_data)
            publisher = BluetoothLEAdvertisementPublisher(advertisement)
            publisher.start()
            # Start() can return before the radio service reports the result.
            # Waiting is valid: Windows may begin advertising once radio time is available.
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                status = getattr(publisher, "status", None)
                status_code = getattr(status, "value", status)
                if status_code == 5:
                    raise RuntimeError("Windows aborted BLE advertising (status: Aborted).")
                if status_code in (1, 2):
                    self.publisher = publisher
                    return True
                time.sleep(0.05)
            status = getattr(publisher, "status", None)
            status_code = getattr(status, "value", status)
            raise RuntimeError(f"Windows did not start BLE advertising (publisher status: {status_code}).")
        except Exception as exc:
            self.error = str(exc)
            self.publisher = None
            return False

    def stop(self) -> None:
        publisher, self.publisher = self.publisher, None
        if publisher is not None:
            try:
                publisher.stop()
            except Exception:
                pass

    @property
    def enabled(self) -> bool:
        if self.publisher is None:
            return False
        status = getattr(self.publisher, "status", None)
        return getattr(status, "value", status) in (1, 2)
