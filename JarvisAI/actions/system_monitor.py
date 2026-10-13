"""
System Monitor — background metric checks with voice alert support.
Zero subprocess calls on all platforms — uses ctypes/pynvml/psutil/wmi only.
"""
import ctypes
import platform
import threading
import time
from functools import lru_cache
from pathlib import Path

import psutil

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

DEFAULT_THRESHOLDS = {
    "cpu":  90.0,
    "ram":  90.0,
    "temp": 85.0,
    "gpu":  95.0,
}

_COOLDOWN   = 300
_CPU_STREAK = 3

# ── NVML DLL cache (Windows: nvml.dll, Linux: libnvidia-ml.so.1) ─────────────
_nvml_lib: object = None
_nvml_ok:  object = None   # None=untested  True=works  False=unavailable
_pynvml_module = None
_pynvml_device = None
_pynvml_ok: bool | None = None
_wmi_gpu_connection = None
_wmi_gpu_ok: bool | None = None
_wmi_temp_connection = None
_wmi_temp_ok: bool | None = None
_wmi_gpu_counter_ok: bool | None = None
_network_lock = threading.Lock()
_network_previous = None
_network_previous_at = 0.0
_status_sensor_lock = threading.Lock()
_status_sensor_values: dict[str, object] = {}
_status_sensor_jobs: set[str] = set()
_status_sensor_last_started: dict[str, float] = {}


def _status_sensor_job(name: str, getter) -> None:
    """Refresh one optional/driver-backed metric without delaying callers."""
    try:
        value = getter()
    except Exception:
        value = None
    with _status_sensor_lock:
        _status_sensor_values[name] = value
        _status_sensor_jobs.discard(name)


def _kick_status_sensor(name: str, getter, interval: float = 8.0) -> None:
    now = time.monotonic()
    with _status_sensor_lock:
        if name in _status_sensor_jobs:
            return
        if now - _status_sensor_last_started.get(name, 0.0) < interval:
            return
        _status_sensor_jobs.add(name)
        _status_sensor_last_started[name] = now
    threading.Thread(
        target=_status_sensor_job,
        args=(name, getter),
        daemon=True,
        name=f"JarvisMetric-{name}",
    ).start()


def _nvml_gpu() -> float:
    """GPU utilisation via NVML — zero subprocess on all platforms."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            if _OS == "Windows":
                candidates = ("nvml", r"C:\Windows\System32\nvml.dll")
                _load = ctypes.WinDLL
            else:
                candidates = (
                    "libnvidia-ml.so.1",
                    "libnvidia-ml.so",
                    "libnvidia-ml.dylib",
                )
                _load = ctypes.CDLL
            for name in candidates:
                try:
                    lib = _load(name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            _nvml_ok = False
            return -1.0

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        u = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
        _nvml_ok = True
        return float(u.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


def _get_gpu_usage() -> float:
    """Read NVIDIA utilization without reinitializing NVML every poll."""
    global _pynvml_module, _pynvml_device, _pynvml_ok
    if _pynvml_ok is not False:
        try:
            if _pynvml_device is None:
                import pynvml  # type: ignore
                pynvml.nvmlInit()
                _pynvml_module = pynvml
                _pynvml_device = pynvml.nvmlDeviceGetHandleByIndex(0)
                _pynvml_ok = True
            return float(_pynvml_module.nvmlDeviceGetUtilizationRates(_pynvml_device).gpu)
        except Exception:
            _pynvml_ok = False

    value = _nvml_gpu()
    if value >= 0:
        return value
    return _windows_gpu_usage()


def _get_wmi_gpu_connection():
    global _wmi_gpu_connection, _wmi_gpu_ok
    if _wmi_gpu_ok is False:
        return None
    try:
        if _wmi_gpu_connection is None:
            import wmi  # type: ignore
            _wmi_gpu_connection = wmi.WMI(namespace="root/cimv2")
        _wmi_gpu_ok = True
        return _wmi_gpu_connection
    except Exception:
        _wmi_gpu_ok = False
        return None


def _windows_gpu_usage() -> float:
    """Use Windows' built-in GPU engine counters for integrated adapters."""
    global _wmi_gpu_counter_ok
    if _OS != "Windows":
        return -1.0
    if _wmi_gpu_counter_ok is False:
        return -1.0
    try:
        wmi_conn = _get_wmi_gpu_connection()
        if wmi_conn is None:
            _wmi_gpu_counter_ok = False
            return -1.0
        rows = wmi_conn.Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine()
        three_d = [
            max(0.0, float(row.UtilizationPercentage))
            for row in rows
            if "engtype_3d" in str(getattr(row, "Name", "")).lower()
        ]
        if three_d:
            # The counter is per process/engine. Sum those rows, capped at the
            # full scale, so shared use is visible without reporting >100%.
            return min(100.0, sum(three_d))
        _wmi_gpu_counter_ok = False
    except Exception:
        _wmi_gpu_counter_ok = False
    return -1.0


@lru_cache(maxsize=1)
def _cpu_name() -> str:
    """Best-effort CPU model discovery across Windows, macOS and Linux."""
    if _OS == "Windows":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            if value:
                return " ".join(str(value).split())
        except Exception:
            pass
    elif _OS == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore").splitlines():
                key, sep, value = line.partition(":")
                if sep and key.strip().lower() in {"model name", "hardware"} and value.strip():
                    return " ".join(value.split())
        except Exception:
            pass
    elif _OS == "Darwin":
        # On macOS, ask the system's sysctl API directly instead of launching
        # `system_profiler` on each refresh. Intel Macs expose the brand string;
        # Apple Silicon systems may expose it on newer OS versions.
        try:
            libc = ctypes.CDLL(None)
            query = libc.sysctlbyname
            query.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                              ctypes.c_size_t]
            query.restype = ctypes.c_int
            size = ctypes.c_size_t(0)
            if query(b"machdep.cpu.brand_string", None, ctypes.byref(size), None, 0) == 0 and size.value:
                buffer = ctypes.create_string_buffer(size.value)
                if query(b"machdep.cpu.brand_string", buffer, ctypes.byref(size), None, 0) == 0:
                    brand = buffer.value.decode("utf-8", "replace").strip()
                    if brand:
                        return brand
        except Exception:
            pass
    name = platform.processor().strip()
    return name or platform.machine() or "CPU details unavailable"


@lru_cache(maxsize=1)
def _gpu_name() -> str:
    """Return a GPU model when the installed driver API exposes one."""
    global _pynvml_module, _pynvml_device, _pynvml_ok
    if _pynvml_ok is not False:
        try:
            if _pynvml_device is None:
                import pynvml  # type: ignore
                pynvml.nvmlInit()
                _pynvml_module = pynvml
                _pynvml_device = pynvml.nvmlDeviceGetHandleByIndex(0)
                _pynvml_ok = True
            value = _pynvml_module.nvmlDeviceGetName(_pynvml_device)
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
        except Exception:
            _pynvml_ok = False
    if _OS == "Windows":
        try:
            wmi_conn = _get_wmi_gpu_connection()
            names = [str(item.Name).strip() for item in wmi_conn.Win32_VideoController() if getattr(item, "Name", None)] if wmi_conn else []
            if names:
                return " / ".join(dict.fromkeys(names))
        except Exception:
            pass
    if _OS == "Linux":
        try:
            devices = []
            for vendor_file in Path("/sys/class/drm").glob("card[0-9]/device/vendor"):
                vendor = vendor_file.read_text(encoding="ascii", errors="ignore").strip().lower()
                label = {"0x8086": "Intel graphics", "0x1002": "AMD graphics", "0x10de": "NVIDIA graphics"}.get(vendor)
                if label and label not in devices:
                    devices.append(label)
            if devices:
                return " / ".join(devices)
        except Exception:
            pass
    return "GPU model unavailable from installed driver APIs"


def _get_cpu_temp() -> float:
    # psutil — works on Linux; occasionally Windows with proper drivers
    try:
        temps = psutil.sensors_temperatures()
        for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                     "cpu-thermal", "zenpower", "it8688"]:
            if name in temps and temps[name]:
                return temps[name][0].current
        for entries in temps.values():
            if entries:
                return entries[0].current
    except Exception:
        pass

    # Windows: wmi module (pure Python COM, zero subprocess)
    global _wmi_temp_connection, _wmi_temp_ok
    if _OS == "Windows" and _wmi_temp_ok is not False:
        try:
            if _wmi_temp_connection is None:
                import wmi  # type: ignore
                _wmi_temp_connection = wmi.WMI(namespace="root/wmi")
            tz = _wmi_temp_connection.MSAcpi_ThermalZoneTemperature()
            if tz:
                _wmi_temp_ok = True
                return (tz[0].CurrentTemperature / 10.0) - 273.15
        except Exception:
            _wmi_temp_ok = False

    return -1.0


def _network_rates() -> tuple[float, float]:
    """Return aggregate RX/TX throughput in Mbps from monotonic byte deltas."""
    global _network_previous, _network_previous_at
    try:
        counters = psutil.net_io_counters()
        now = time.monotonic()
        with _network_lock:
            previous, previous_at = _network_previous, _network_previous_at
            _network_previous = counters
            _network_previous_at = now
        if previous is None:
            return 0.0, 0.0
        elapsed = max(0.001, now - previous_at)
        rx = max(0.0, (counters.bytes_recv - previous.bytes_recv) * 8 / elapsed / 1_000_000)
        tx = max(0.0, (counters.bytes_sent - previous.bytes_sent) * 8 / elapsed / 1_000_000)
        return rx, tx
    except Exception:
        return 0.0, 0.0


def _gpu_status() -> tuple[float, str]:
    # Keep the two probes together because their cached NVML/WMI handles are shared.
    usage = _get_gpu_usage()
    name = _gpu_name()
    return usage, name


def get_system_status() -> dict:
    """Return a quick snapshot; optional driver/WMI probes update in background.

    ``cpu_percent(interval=0.2)`` used to pause every request, while the GPU,
    temperature and device-name probes can be slow on Windows (notably WMI).
    The dashboard and voice tool now get immediately available metrics, and
    those optional values fill in as their independent background probes finish.
    """
    cpu  = psutil.cpu_percent(interval=None)
    ram  = psutil.virtual_memory()

    _kick_status_sensor("cpu_temp_c", _get_cpu_temp)
    _kick_status_sensor("gpu_status", _gpu_status)
    _kick_status_sensor("cpu_name", _cpu_name)
    _kick_status_sensor("process_count", lambda: len(psutil.pids()))
    with _status_sensor_lock:
        sensors = dict(_status_sensor_values)
    gpu_status = sensors.get("gpu_status")
    gpu_percent, gpu_name = gpu_status if isinstance(gpu_status, tuple) and len(gpu_status) == 2 else (None, None)

    boot_time   = psutil.boot_time()
    uptime_secs = time.time() - boot_time
    uptime_h    = int(uptime_secs // 3600)
    uptime_m    = int((uptime_secs % 3600) // 60)

    disk_root = Path.home().anchor or "/"
    try:
        disk = psutil.disk_usage(disk_root)
        disk_percent = round(disk.percent, 1)
        disk_used_gb = round(disk.used / 1024 ** 3, 1)
        disk_total_gb = round(disk.total / 1024 ** 3, 1)
        disk_free_gb = round(disk.free / 1024 ** 3, 1)
    except Exception:
        disk_percent = disk_used_gb = disk_total_gb = disk_free_gb = None
    battery = psutil.sensors_battery()
    try:
        freq = psutil.cpu_freq()
        current_mhz = round(float(freq.current), 0) if freq and freq.current else None
    except Exception:
        current_mhz = None
    network_rx, network_tx = _network_rates()

    return {
        "cpu_percent":   round(cpu, 1),
        "cpu_name":      sensors.get("cpu_name") or "Detecting CPU model…",
        "cpu_cores":     psutil.cpu_count(logical=False),
        "cpu_threads":   psutil.cpu_count(logical=True),
        "cpu_freq_mhz":  current_mhz,
        "ram_percent":   round(ram.percent, 1),
        "ram_used_gb":   round(ram.used   / 1024 ** 3, 1),
        "ram_total_gb":  round(ram.total  / 1024 ** 3, 1),
        "ram_available_gb": round(ram.available / 1024 ** 3, 1),
        "cpu_temp_c":    round(float(sensors["cpu_temp_c"]), 1) if sensors.get("cpu_temp_c") is not None and float(sensors["cpu_temp_c"]) > 0 else None,
        "gpu_percent":   round(float(gpu_percent), 1) if gpu_percent is not None and float(gpu_percent) >= 0 else None,
        "gpu_name":      gpu_name or "Detecting graphics adapter…",
        "disk_percent":  disk_percent,
        "disk_used_gb":  disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "disk_free_gb":  disk_free_gb,
        "battery_percent": round(float(battery.percent), 1) if battery else None,
        "battery_plugged": bool(battery.power_plugged) if battery else None,
        "network_rx_mbps": round(network_rx, 2),
        "network_tx_mbps": round(network_tx, 2),
        "os_name":       f"{platform.system()} {platform.release()}".strip(),
        "architecture":  platform.machine() or "Unknown",
        "uptime":        f"{uptime_h}h {uptime_m}m",
        "uptime_seconds": round(uptime_secs),
        "process_count": sensors.get("process_count"),
    }


class SystemMonitor:
    """
    Stateful monitor — cooldown state persists across session reconnections.
    Call check() periodically; returns a [SYSTEM_ALERT] string or None.
    """

    def __init__(self, thresholds: dict | None = None):
        self.thresholds   = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._last_alert: dict[str, float] = {}
        self._cpu_streak  = 0

    def _can_alert(self, key: str) -> bool:
        return (time.monotonic() - self._last_alert.get(key, 0)) > _COOLDOWN

    def _record(self, key: str):
        self._last_alert[key] = time.monotonic()

    def check(self) -> str | None:
        try:
            cpu  = psutil.cpu_percent(interval=None)
            ram  = psutil.virtual_memory().percent
            temp = _get_cpu_temp()
            gpu  = _get_gpu_usage()
        except Exception:
            return None

        alerts: list[str] = []

        if cpu >= self.thresholds["cpu"]:
            self._cpu_streak += 1
            if self._cpu_streak >= _CPU_STREAK and self._can_alert("cpu"):
                alerts.append(
                    f"[SYSTEM_ALERT] CPU usage has been critically high ({cpu:.0f}%) "
                    "for several seconds. Warn the user in their language and suggest "
                    "closing heavy applications."
                )
                self._record("cpu")
                self._cpu_streak = 0
        else:
            self._cpu_streak = 0

        if ram >= self.thresholds["ram"] and self._can_alert("ram"):
            alerts.append(
                f"[SYSTEM_ALERT] RAM is at {ram:.0f}% — nearly exhausted. "
                "Warn the user in their language and suggest freeing memory."
            )
            self._record("ram")

        if temp > 0 and temp >= self.thresholds["temp"] and self._can_alert("temp"):
            alerts.append(
                f"[SYSTEM_ALERT] CPU temperature is {temp:.0f}°C — above the safe limit. "
                "Warn the user in their language and advise reducing system load "
                "or checking cooling."
            )
            self._record("temp")

        if gpu >= 0 and gpu >= self.thresholds["gpu"] and self._can_alert("gpu"):
            alerts.append(
                f"[SYSTEM_ALERT] GPU load is at {gpu:.0f}%. "
                "Briefly inform the user in their language."
            )
            self._record("gpu")

        return " ".join(alerts) if alerts else None
