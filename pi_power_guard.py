#!/usr/bin/env python3
"""pi-power-guard: PMIC power monitoring and crash-resilient watchdog for Raspberry Pi 5.

Monitors all 12 PMIC power rails, detects voltage drops before they cause
shutdowns, and provides crash-forensic logging that survives power failures.

Requires: Raspberry Pi 5, Raspberry Pi OS Bookworm, Python 3.11+
Dependencies: None (stdlib only)

Copyright (c) 2026 Mahsum Aktas
License: MIT
"""

__version__ = "1.0.0"

import argparse
import collections
import configparser
import glob
import os
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THROTTLE_BITS = {
    0: "under-voltage",
    1: "freq-capped",
    2: "throttled",
    3: "soft-temp-limit",
    16: "under-voltage-occurred",
    17: "freq-capped-occurred",
    18: "throttled-occurred",
    19: "soft-temp-limit-occurred",
}

RSTS_REASONS = {
    0x1000: "POWER_CYCLE",
    0x1020: "SOFTWARE_REBOOT",
    0x1040: "WATCHDOG_RESET",
}

DEFAULT_CONFIG = {
    "general": {
        "log_dir": "/var/log/pi-power-guard",
        "ring_buffer_lines": "100000",
        "max_archives": "5",
        "baseline_interval": "5",
        "poll_interval": "1",
        "sync_interval": "10",
        "state_dir": "/var/lib/pi-power-guard",
    },
    "thresholds": {
        "ext5v_warn": "4.85",
        "ext5v_low": "4.75",
        "ext5v_critical": "4.50",
        "3v3_sys_warn": "3.20",
        "3v3_sys_critical": "3.10",
        "cpu_temp_warn": "75.0",
        "cpu_temp_critical": "85.0",
        "pmic_temp_warn": "70.0",
        "pmic_temp_critical": "80.0",
        "nvme_temp_warn": "60.0",
        "nvme_temp_critical": "70.0",
    },
    "trend": {
        "window_size": "60",
        "ema_alpha": "0.1",
        "drop_threshold": "0.15",
        "min_samples": "10",
    },
    "change_detection": {
        "voltage_epsilon": "0.010",
        "temp_epsilon": "1.0",
    },
}


# ---------------------------------------------------------------------------
# SdNotify — systemd sd_notify via AF_UNIX
# ---------------------------------------------------------------------------

class SdNotify:
    """Pure-stdlib systemd notification."""

    def __init__(self):
        addr = os.environ.get("NOTIFY_SOCKET")
        self._sock = None
        if addr:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            if addr.startswith("@"):
                addr = "\0" + addr[1:]
            self._addr = addr

    def _send(self, msg: str):
        if self._sock:
            try:
                self._sock.sendto(msg.encode(), self._addr)
            except OSError:
                pass

    def ready(self):
        self._send("READY=1")

    def watchdog(self):
        self._send("WATCHDOG=1")

    def status(self, text: str):
        self._send(f"STATUS={text}")

    def stopping(self):
        self._send("STOPPING=1")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    """Load and manage configuration from INI file."""

    def __init__(self, path: str = None):
        self._path = path
        self._cp = configparser.ConfigParser()
        # Load defaults
        self._cp.read_dict(DEFAULT_CONFIG)
        self.reload()

    def reload(self):
        if self._path and os.path.isfile(self._path):
            self._cp.read(self._path)

    def get(self, section: str, key: str, fallback=None) -> str:
        return self._cp.get(section, key, fallback=fallback)

    def getint(self, section: str, key: str, fallback=0) -> int:
        return self._cp.getint(section, key, fallback=fallback)

    def getfloat(self, section: str, key: str, fallback=0.0) -> float:
        return self._cp.getfloat(section, key, fallback=fallback)


# ---------------------------------------------------------------------------
# RingBufferLog — Crash-resilient append-only log
# ---------------------------------------------------------------------------

class RingBufferLog:
    """Append-only log with fdatasync thread and rotation."""

    def __init__(self, log_dir: str, max_lines: int = 100000,
                 sync_interval: int = 10, max_archives: int = 5):
        self._log_dir = log_dir
        self._max_lines = max_lines
        self._sync_interval = sync_interval
        self._max_archives = max_archives
        self._line_count = 0
        self._lock = threading.Lock()
        self._shutdown = False

        os.makedirs(log_dir, exist_ok=True)
        self._path = os.path.join(log_dir, "current.log")

        # Count existing lines
        if os.path.isfile(self._path):
            with open(self._path, "r") as f:
                self._line_count = sum(1 for _ in f)

        self._fd = open(self._path, "a")

        # Start sync thread
        self._sync_thread = threading.Thread(
            target=self._sync_loop, daemon=True, name="log-sync"
        )
        self._sync_thread.start()

    def write(self, line: str):
        with self._lock:
            self._fd.write(line + "\n")
            self._line_count += 1
            if self._line_count >= self._max_lines:
                self._rotate()

    def sync(self):
        with self._lock:
            try:
                self._fd.flush()
                os.fdatasync(self._fd.fileno())
            except OSError:
                pass

    def close(self):
        self._shutdown = True
        self.sync()
        with self._lock:
            self._fd.close()

    def _rotate(self):
        self._fd.close()
        # Shift archives
        for i in range(self._max_archives - 1, 0, -1):
            src = os.path.join(self._log_dir, f"archive.{i}.log")
            dst = os.path.join(self._log_dir, f"archive.{i + 1}.log")
            if os.path.isfile(src):
                os.rename(src, dst)
        # Current -> archive.1
        archive = os.path.join(self._log_dir, "archive.1.log")
        os.rename(self._path, archive)
        # Remove oldest if over limit
        oldest = os.path.join(self._log_dir, f"archive.{self._max_archives + 1}.log")
        if os.path.isfile(oldest):
            os.remove(oldest)
        # Open new current
        self._fd = open(self._path, "a")
        self._line_count = 0

    def _sync_loop(self):
        while not self._shutdown:
            time.sleep(self._sync_interval)
            self.sync()


# ---------------------------------------------------------------------------
# VoltageTracker — EMA + half-window slope trend detection
# ---------------------------------------------------------------------------

class VoltageTracker:
    """Per-rail moving average and trend detection."""

    def __init__(self, window_size: int = 60, ema_alpha: float = 0.1,
                 drop_threshold: float = 0.15, min_samples: int = 10):
        self._window = collections.deque(maxlen=window_size)
        self._ema = None
        self._alpha = ema_alpha
        self._drop_threshold = drop_threshold
        self._min_samples = min_samples
        self._warned = False

    def add(self, value: float):
        self._window.append(value)
        if self._ema is None:
            self._ema = value
        else:
            self._ema = self._alpha * value + (1 - self._alpha) * self._ema

    def trend(self):
        """Returns (ema, slope, is_dropping, newly_warned)."""
        if len(self._window) < self._min_samples:
            return (self._ema or 0.0, 0.0, False, False)

        samples = list(self._window)
        mid = len(samples) // 2
        first_half = statistics.mean(samples[:mid])
        second_half = statistics.mean(samples[mid:])
        slope = second_half - first_half

        is_dropping = slope < -self._drop_threshold
        newly_warned = False

        if is_dropping and not self._warned:
            self._warned = True
            newly_warned = True
        elif not is_dropping and self._warned:
            self._warned = False

        return (self._ema, slope, is_dropping, newly_warned)


# ---------------------------------------------------------------------------
# SensorReader — All hardware interaction
# ---------------------------------------------------------------------------

class SensorReader:
    """Reads all Pi 5 hardware sensors."""

    def __init__(self):
        self._cpu_thermal = "/sys/class/thermal/thermal_zone0/temp"
        self._volt_alarm_path = self._find_hwmon("rpi_volt", "in0_lcrit_alarm")
        self._nvme_temp_path = self._find_hwmon("nvme", "temp1_input")

    @staticmethod
    def _find_hwmon(name: str, sensor_file: str = None):
        for path in glob.glob("/sys/class/hwmon/hwmon*/name"):
            try:
                with open(path) as f:
                    if f.read().strip() == name:
                        d = os.path.dirname(path)
                        if sensor_file:
                            full = os.path.join(d, sensor_file)
                            return full if os.path.isfile(full) else None
                        return d
            except OSError:
                pass
        return None

    @staticmethod
    def _sysfs_read(path: str):
        if not path:
            return None
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return None

    @staticmethod
    def _run_vcgencmd(*args, timeout=3):
        try:
            r = subprocess.run(
                ["vcgencmd", *args],
                capture_output=True, text=True, timeout=timeout,
            )
            return r.stdout.strip() if r.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def read_pmic_adc(self) -> dict:
        """Parse vcgencmd pmic_read_adc output into {rail: value} dict."""
        out = self._run_vcgencmd("pmic_read_adc")
        if not out:
            return {}
        result = {}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # Format: "EXT5V_V volt=5.10268V" or "EXT5V_A curr=0.47400A"
            parts = line.split()
            if len(parts) < 2:
                continue
            rail = parts[0].lower()
            val_part = parts[1]
            # Extract numeric value: "volt=5.10268V" -> 5.10268
            if "=" in val_part:
                val_str = val_part.split("=")[1]
                # Remove trailing unit letter (V, A)
                val_str = val_str.rstrip("VAvAmAW")
                try:
                    result[rail] = float(val_str)
                except ValueError:
                    pass
        return result

    def read_throttled(self):
        """Returns (raw_int, set_of_flag_names) or (None, set())."""
        out = self._run_vcgencmd("get_throttled")
        if not out:
            return (None, set())
        # "throttled=0x50005"
        try:
            raw = int(out.split("=")[1], 0)
        except (IndexError, ValueError):
            return (None, set())
        flags = set()
        for bit, name in THROTTLE_BITS.items():
            if raw & (1 << bit):
                flags.add(name)
        return (raw, flags)

    def read_cpu_temp(self) -> float | None:
        val = self._sysfs_read(self._cpu_thermal)
        if val is None:
            return None
        try:
            return int(val) / 1000.0
        except ValueError:
            return None

    def read_pmic_temp(self) -> float | None:
        out = self._run_vcgencmd("measure_temp", "pmic")
        if not out:
            return None
        # "temp=42.0'C"
        try:
            return float(out.split("=")[1].split("'")[0])
        except (IndexError, ValueError):
            return None

    def read_nvme_temp(self) -> float | None:
        val = self._sysfs_read(self._nvme_temp_path)
        if val is None:
            return None
        try:
            return int(val) / 1000.0
        except ValueError:
            return None

    def read_pm_rsts(self) -> int | None:
        out = self._run_vcgencmd("get_rsts")
        if not out:
            return None
        try:
            return int(out.split("=")[1], 0)
        except (IndexError, ValueError):
            return None

    def read_volt_alarm(self) -> bool | None:
        val = self._sysfs_read(self._volt_alarm_path)
        if val is None:
            return None
        return val.strip() != "0"


# ---------------------------------------------------------------------------
# CrashDetector — Boot-time previous shutdown analysis
# ---------------------------------------------------------------------------

class CrashDetector:
    """Detect and report previous unclean shutdowns."""

    def __init__(self, state_dir: str, sensor: SensorReader):
        self._state_dir = state_dir
        self._state_file = os.path.join(state_dir, "last-state")
        self._sensor = sensor
        os.makedirs(state_dir, exist_ok=True)

    def check(self) -> dict:
        """Analyze previous shutdown. Returns report dict."""
        report = {
            "pm_rsts": None,
            "pm_rsts_hex": "unknown",
            "type": "UNKNOWN",
            "prev_state": "unknown",
            "prev_time": "unknown",
            "ext4_recovery": False,
        }

        # 1. PM_RSTS register
        rsts = self._sensor.read_pm_rsts()
        if rsts is not None:
            report["pm_rsts"] = rsts
            report["pm_rsts_hex"] = f"0x{rsts:x}"
            report["type"] = RSTS_REASONS.get(rsts, f"UNKNOWN_0x{rsts:x}")

        # 2. State file
        if os.path.isfile(self._state_file):
            try:
                with open(self._state_file) as f:
                    lines = f.read().strip().splitlines()
                if lines:
                    report["prev_state"] = lines[0]
                if len(lines) > 1:
                    report["prev_time"] = lines[1]
            except OSError:
                pass
        else:
            report["prev_state"] = "missing"

        # 3. ext4 recovery check
        try:
            r = subprocess.run(
                ["journalctl", "-b", "-g", "EXT4-fs.*recovery",
                 "--no-pager", "-q", "--output=short"],
                capture_output=True, text=True, timeout=5,
            )
            if r.stdout.strip():
                report["ext4_recovery"] = True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # 4. Mark current boot
        self._write_state("booted")

        return report

    def write_clean_state(self):
        """Called on SIGTERM for clean shutdown."""
        self._write_state("clean")

    def _write_state(self, state: str):
        try:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            with open(self._state_file, "w") as f:
                f.write(f"{state}\n{ts}\n")
                f.flush()
                os.fdatasync(f.fileno())
        except OSError:
            pass


# ---------------------------------------------------------------------------
# PowerGuardDaemon — Main orchestrator
# ---------------------------------------------------------------------------

class PowerGuardDaemon:
    """Main daemon: orchestrates sensors, logging, trend detection."""

    def __init__(self, config_path: str = None):
        self._config = Config(config_path)
        self._sensor = SensorReader()
        self._sd = SdNotify()
        self._shutdown = False
        self._prev_snapshot = {}
        self._prev_throttle_flags = set()
        self._baseline_counter = 0

        # Ring buffer log
        log_dir = self._config.get("general", "log_dir")
        self._log = RingBufferLog(
            log_dir=log_dir,
            max_lines=self._config.getint("general", "ring_buffer_lines", fallback=100000),
            sync_interval=self._config.getint("general", "sync_interval", fallback=10),
            max_archives=self._config.getint("general", "max_archives", fallback=5),
        )

        # Crash detector
        state_dir = self._config.get("general", "state_dir")
        self._crash_detector = CrashDetector(state_dir, self._sensor)

        # Voltage trackers for key rails
        ws = self._config.getint("trend", "window_size", fallback=60)
        alpha = self._config.getfloat("trend", "ema_alpha", fallback=0.1)
        drop = self._config.getfloat("trend", "drop_threshold", fallback=0.15)
        ms = self._config.getint("trend", "min_samples", fallback=10)
        self._trackers = {
            "ext5v_v": VoltageTracker(ws, alpha, drop, ms),
            "3v3_sys_v": VoltageTracker(ws, alpha, drop, ms),
            "vdd_core_v": VoltageTracker(ws, alpha, drop, ms),
            "1v8_sys_v": VoltageTracker(ws, alpha, drop, ms),
        }

        # Config values
        self._baseline_interval = self._config.getint(
            "general", "baseline_interval", fallback=5
        )
        self._poll_interval = self._config.getint(
            "general", "poll_interval", fallback=1
        )
        self._volt_eps = self._config.getfloat(
            "change_detection", "voltage_epsilon", fallback=0.010
        )
        self._temp_eps = self._config.getfloat(
            "change_detection", "temp_epsilon", fallback=1.0
        )

    def run(self):
        """Main entry point."""
        # Signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_sighup)

        # Startup banner
        self._log_line("BOOT", "SYSTEM",
                       f'version={__version__} hostname={socket.gethostname()} '
                       f'python={sys.version.split()[0]}')

        # Crash detection
        report = self._crash_detector.check()
        unclean = report["prev_state"] != "clean"
        self._log_line("BOOT", "CRASH",
                       f'pm_rsts={report["pm_rsts_hex"]} type={report["type"]} '
                       f'prev_state={report["prev_state"]} '
                       f'prev_time="{report["prev_time"]}" '
                       f'ext4_recovery={str(report["ext4_recovery"]).lower()}')

        if unclean and report["type"] != "UNKNOWN":
            self._sd.status(f"Boot: {report['type']} detected")

        # Notify systemd we're ready
        self._sd.ready()

        # Main loop
        try:
            self._main_loop()
        finally:
            self._crash_detector.write_clean_state()
            self._log_line("INFO", "SYSTEM", "shutdown=clean")
            self._log.close()
            self._sd.stopping()

    def _main_loop(self):
        while not self._shutdown:
            try:
                snapshot = self._read_all_sensors()
                changes = self._detect_changes(snapshot)
                self._baseline_counter += 1

                # Log on change or baseline interval
                baseline_due = self._baseline_counter >= self._baseline_interval
                if changes or baseline_due:
                    self._log_snapshot(snapshot, changes)
                    self._baseline_counter = 0

                # Update trends and check alerts
                self._update_trends(snapshot)
                self._check_thresholds(snapshot)

                # Feed systemd watchdog
                self._sd.watchdog()

                self._prev_snapshot = snapshot
            except Exception:
                pass  # Never crash from sensor errors

            time.sleep(self._poll_interval)

    def _read_all_sensors(self) -> dict:
        snap = {}

        # PMIC ADC
        pmic = self._sensor.read_pmic_adc()
        snap["pmic"] = pmic

        # Throttle
        raw, flags = self._sensor.read_throttled()
        snap["throttle_raw"] = raw
        snap["throttle_flags"] = flags

        # Temperatures
        snap["cpu_temp"] = self._sensor.read_cpu_temp()
        snap["pmic_temp"] = self._sensor.read_pmic_temp()
        snap["nvme_temp"] = self._sensor.read_nvme_temp()

        # Voltage alarm
        snap["volt_alarm"] = self._sensor.read_volt_alarm()

        return snap

    def _detect_changes(self, snap: dict) -> list:
        if not self._prev_snapshot:
            return ["initial"]

        changes = []
        prev = self._prev_snapshot

        # Throttle state change
        if snap.get("throttle_flags") != prev.get("throttle_flags"):
            new_flags = snap.get("throttle_flags", set()) - prev.get("throttle_flags", set())
            if new_flags:
                changes.append(f"throttle:+{','.join(sorted(new_flags))}")
            cleared = prev.get("throttle_flags", set()) - snap.get("throttle_flags", set())
            if cleared:
                changes.append(f"throttle:-{','.join(sorted(cleared))}")

        # Voltage alarm change
        if snap.get("volt_alarm") != prev.get("volt_alarm"):
            changes.append(f"volt_alarm:{snap.get('volt_alarm')}")

        # Significant voltage changes
        for rail in ["ext5v_v", "3v3_sys_v", "vdd_core_v", "1v8_sys_v"]:
            curr = snap.get("pmic", {}).get(rail)
            prev_val = prev.get("pmic", {}).get(rail)
            if curr is not None and prev_val is not None:
                if abs(curr - prev_val) > self._volt_eps:
                    changes.append(f"{rail}:{prev_val:.3f}->{curr:.3f}")

        # Significant temperature changes
        for key in ["cpu_temp", "pmic_temp", "nvme_temp"]:
            curr = snap.get(key)
            prev_val = prev.get(key)
            if curr is not None and prev_val is not None:
                if abs(curr - prev_val) > self._temp_eps:
                    changes.append(f"{key}:{prev_val:.1f}->{curr:.1f}")

        return changes

    def _log_snapshot(self, snap: dict, changes: list):
        pmic = snap.get("pmic", {})

        # PMIC line
        pmic_parts = []
        for rail in ["ext5v_v", "ext5v_a", "vdd_core_v", "vdd_core_a",
                      "3v3_sys_v", "3v3_sys_a", "1v8_sys_v", "1v8_sys_a",
                      "ddr_vdd2_v", "ddr_vddq_v", "hdmi_v", "3v7_wl_sw_v"]:
            val = pmic.get(rail)
            if val is not None:
                pmic_parts.append(f"{rail}={val:.3f}")
        if pmic_parts:
            self._log_line("INFO", "PMIC", " ".join(pmic_parts))

        # Throttle line
        raw = snap.get("throttle_raw")
        flags = snap.get("throttle_flags", set())
        flags_str = ",".join(sorted(flags)) if flags else "none"
        raw_str = f"0x{raw:x}" if raw is not None else "unknown"
        level = "WARN" if flags else "INFO"
        change_str = ""
        throttle_changes = [c for c in changes if c.startswith("throttle:")]
        if throttle_changes:
            change_str = f' changed="{";".join(throttle_changes)}"'
        self._log_line(level, "THROTTLE",
                       f"raw={raw_str} flags={flags_str}{change_str}")

        # Temperature line
        temps = []
        for key, label in [("cpu_temp", "cpu"), ("pmic_temp", "pmic"),
                           ("nvme_temp", "nvme")]:
            val = snap.get(key)
            if val is not None:
                temps.append(f"{label}={val:.1f}")
        if temps:
            self._log_line("INFO", "TEMP", " ".join(temps))

    def _update_trends(self, snap: dict):
        pmic = snap.get("pmic", {})
        for rail, tracker in self._trackers.items():
            val = pmic.get(rail)
            if val is not None:
                tracker.add(val)
                ema, slope, dropping, newly_warned = tracker.trend()
                if newly_warned:
                    self._log_line(
                        "WARN", "TREND",
                        f'rail={rail} ema={ema:.3f} slope={slope:.4f} '
                        f'msg="voltage trending down"'
                    )

    def _check_thresholds(self, snap: dict):
        pmic = snap.get("pmic", {})
        cfg = self._config

        # EXT5V voltage
        ext5v = pmic.get("ext5v_v")
        if ext5v is not None:
            crit = cfg.getfloat("thresholds", "ext5v_critical", fallback=4.50)
            low = cfg.getfloat("thresholds", "ext5v_low", fallback=4.75)
            warn = cfg.getfloat("thresholds", "ext5v_warn", fallback=4.85)
            if ext5v < crit:
                self._log_line("ALERT", "PMIC",
                               f'ext5v_v={ext5v:.3f} threshold={crit} '
                               f'msg="EXT5V CRITICAL - shutdown imminent"')
            elif ext5v < low:
                self._log_line("ALERT", "PMIC",
                               f'ext5v_v={ext5v:.3f} threshold={low} '
                               f'msg="EXT5V below low threshold"')
            elif ext5v < warn:
                self._log_line("WARN", "PMIC",
                               f'ext5v_v={ext5v:.3f} threshold={warn} '
                               f'msg="EXT5V below warning threshold"')

        # 3V3 SYS
        v3v3 = pmic.get("3v3_sys_v")
        if v3v3 is not None:
            crit = cfg.getfloat("thresholds", "3v3_sys_critical", fallback=3.10)
            warn = cfg.getfloat("thresholds", "3v3_sys_warn", fallback=3.20)
            if v3v3 < crit:
                self._log_line("ALERT", "PMIC",
                               f'3v3_sys_v={v3v3:.3f} threshold={crit} '
                               f'msg="3V3_SYS CRITICAL"')
            elif v3v3 < warn:
                self._log_line("WARN", "PMIC",
                               f'3v3_sys_v={v3v3:.3f} threshold={warn} '
                               f'msg="3V3_SYS below warning"')

        # Temperatures
        for key, label, warn_key, crit_key in [
            ("cpu_temp", "CPU", "cpu_temp_warn", "cpu_temp_critical"),
            ("pmic_temp", "PMIC", "pmic_temp_warn", "pmic_temp_critical"),
            ("nvme_temp", "NVMe", "nvme_temp_warn", "nvme_temp_critical"),
        ]:
            val = snap.get(key)
            if val is None:
                continue
            crit = cfg.getfloat("thresholds", crit_key, fallback=85.0)
            warn = cfg.getfloat("thresholds", warn_key, fallback=75.0)
            if val > crit:
                self._log_line("ALERT", "TEMP",
                               f'{key}={val:.1f} threshold={crit} '
                               f'msg="{label} temperature CRITICAL"')
            elif val > warn:
                self._log_line("WARN", "TEMP",
                               f'{key}={val:.1f} threshold={warn} '
                               f'msg="{label} temperature high"')

    def _log_line(self, level: str, subsystem: str, data: str):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        line = f"{ts} {level} {subsystem} {data}"
        self._log.write(line)

    def _handle_signal(self, signum, frame):
        self._shutdown = True

    def _handle_sighup(self, signum, frame):
        self._config.reload()


# ---------------------------------------------------------------------------
# One-Shot Check Mode
# ---------------------------------------------------------------------------

def run_check(config_path: str = None):
    """Print a single snapshot and crash report, then exit."""
    sensor = SensorReader()
    config = Config(config_path)
    state_dir = config.get("general", "state_dir")
    crash = CrashDetector(state_dir, sensor)

    print(f"\npi-power-guard v{__version__} -- One-Shot Check")
    print("=" * 48)

    # Boot analysis
    report = crash.check()
    print(f"\nBoot Analysis:")
    print(f"  PM_RSTS:           {report['pm_rsts_hex']} ({report['type']})")
    print(f"  Previous Shutdown: {report['prev_state'].upper()}")
    print(f"  Previous Time:     {report['prev_time']}")
    print(f"  ext4 Recovery:     {'Yes' if report['ext4_recovery'] else 'No'}")

    # Power rails
    pmic = sensor.read_pmic_adc()
    print(f"\nPower Rails (PMIC):")
    cfg = config
    ext5v = pmic.get("ext5v_v")
    for rail, val in sorted(pmic.items()):
        unit = "A" if rail.endswith("_a") else "V"
        status = ""
        if rail == "ext5v_v" and val is not None:
            if val < cfg.getfloat("thresholds", "ext5v_critical", fallback=4.50):
                status = " [CRITICAL]"
            elif val < cfg.getfloat("thresholds", "ext5v_low", fallback=4.75):
                status = " [LOW]"
            elif val < cfg.getfloat("thresholds", "ext5v_warn", fallback=4.85):
                status = " [WARN]"
            else:
                status = " [OK]"
        print(f"  {rail:16s} {val:>8.3f} {unit}{status}")

    # Throttle
    raw, flags = sensor.read_throttled()
    raw_str = f"0x{raw:x}" if raw is not None else "unknown"
    flags_str = ", ".join(sorted(flags)) if flags else "None"
    print(f"\nThrottle State:")
    print(f"  Raw:    {raw_str}")
    print(f"  Flags:  {flags_str}")

    # Temperatures
    cpu = sensor.read_cpu_temp()
    pmic_t = sensor.read_pmic_temp()
    nvme_t = sensor.read_nvme_temp()
    print(f"\nTemperatures:")
    for label, val, warn_key in [
        ("CPU", cpu, "cpu_temp_warn"),
        ("PMIC", pmic_t, "pmic_temp_warn"),
        ("NVMe", nvme_t, "nvme_temp_warn"),
    ]:
        if val is not None:
            warn = cfg.getfloat("thresholds", warn_key, fallback=75.0)
            status = "[OK]" if val < warn else "[HIGH]"
            print(f"  {label:8s} {val:>6.1f} C  {status}")
        else:
            print(f"  {label:8s}    N/A")

    # Voltage alarm
    alarm = sensor.read_volt_alarm()
    print(f"\nVoltage Alarm: {'YES' if alarm else 'No'}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="pi-power-guard",
        description="PMIC power monitoring and crash-resilient watchdog for Raspberry Pi 5",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="/etc/pi-power-guard/config.ini",
                        help="Path to config file (default: /etc/pi-power-guard/config.ini)")
    parser.add_argument("--check", action="store_true",
                        help="One-shot: print snapshot and crash report, then exit")
    parser.add_argument("--dump-config", action="store_true",
                        help="Print active configuration and exit")

    args = parser.parse_args()

    if args.dump_config:
        cfg = Config(args.config)
        cfg._cp.write(sys.stdout)
        return

    if args.check:
        run_check(args.config)
        return

    daemon = PowerGuardDaemon(args.config)
    daemon.run()


if __name__ == "__main__":
    main()
