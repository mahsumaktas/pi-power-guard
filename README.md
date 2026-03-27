# pi-power-guard

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-green.svg)](https://www.python.org)
[![Raspberry Pi 5](https://img.shields.io/badge/Raspberry%20Pi-5-red.svg)](https://www.raspberrypi.com/products/raspberry-pi-5/)

**Your Pi 5 shut down and you don't know why? This tool makes sure that never happens again.**

pi-power-guard is a lightweight daemon that monitors all power rails on your Raspberry Pi 5's PMIC (Power Management IC) every second. It does three things:

1. **Warns before crashes** -- detects voltage drops and trends before the hardware under-voltage alarm triggers
2. **Preserves evidence** -- crash-resilient logging ensures you have data from the seconds before a power failure (journald loses up to 5 minutes)
3. **Post-crash forensics** -- on every boot, automatically determines why your Pi shut down (power cycle, watchdog reset, or software reboot)

Zero dependencies. Single Python file. Just install and forget.

## Why?

Raspberry Pi 5 can shut down suddenly due to PSU issues, USB-C cable problems, or power fluctuations. When this happens:

- **journald loses up to 5 minutes of logs** (default `SyncIntervalSec` is 5 minutes)
- **No under-voltage warning** appears if power drops too fast for the firmware to react
- **pstore/ramoops don't survive power loss** on Pi (DRAM-based, not persistent)
- You're left with zero evidence of what happened

This tool was born after investigating a real Pi 5 that shut down mysteriously -- no kernel panic, no OOM, no under-voltage in logs, nothing. Other devices on the same network were fine. The existing monitoring (5-minute cron job) left a 5-minute data gap right when it mattered most.

## Features

- **12 PMIC Rail Monitoring** -- reads every power rail via `vcgencmd pmic_read_adc` (EXT5V, VDD_CORE, 3V3_SYS, 1V8_SYS, DDR, HDMI, and more)
- **Voltage Trend Detection** -- EMA-based slope analysis warns before hardware under-voltage triggers
- **Crash-Resilient Logging** -- `fdatasync` every 10 seconds with ring buffer rotation (max ~10s data loss vs 5 minutes with journald)
- **Boot Crash Detection** -- analyzes PM_RSTS register, state files, and ext4 recovery to determine what happened
- **Throttle State Decoding** -- human-readable `get_throttled` bitmask with change tracking
- **Temperature Monitoring** -- CPU, PMIC, and NVMe temperatures with configurable thresholds
- **systemd Integration** -- `Type=notify` with sd_notify watchdog, security hardening
- **Zero Dependencies** -- Python 3.11+ stdlib only, single file (~700 lines)
- **One-Shot Diagnostic** -- `--check` flag for instant health report

## Requirements

- Raspberry Pi 5 (any RAM variant)
- Raspberry Pi OS Bookworm (Debian 12, aarch64)
- Python 3.11+

## Quick Start

**One-command install:**

```bash
git clone https://github.com/mahsumaktas/pi-power-guard.git
cd pi-power-guard
sudo bash install.sh
```

The installer will:
1. Verify Pi 5 hardware and Python version
2. Install the daemon and config
3. Optionally enable hardware watchdog, tune journald, and configure systemd watchdog
4. Start the service

**Verify it's running:**

```bash
systemctl status pi-power-guard
tail -f /var/log/pi-power-guard/current.log
```

## One-Shot Diagnostic

Run a single health check without starting the daemon:

```bash
sudo python3 /opt/pi-power-guard/pi_power_guard.py --check
```

Example output (real Pi 5 data):
```
pi-power-guard v1.0.0 -- One-Shot Check
================================================

Boot Analysis:
  PM_RSTS:           0x1000 (POWER_CYCLE)
  Previous Shutdown: BOOTED
  Previous Time:     2026-03-27T14:25:49+0300
  ext4 Recovery:     Yes

Power Rails (PMIC):
  0v8_aon_a           0.003 A
  0v8_aon_v           0.801 V
  0v8_sw_a            0.364 A
  0v8_sw_v            0.802 V
  1v1_sys_a           0.178 A
  1v1_sys_v           1.106 V
  1v8_sys_a           0.196 A
  1v8_sys_v           1.807 V
  3v3_sys_a           0.126 A
  3v3_sys_v           3.303 V
  3v7_wl_sw_a         0.084 A
  3v7_wl_sw_v         3.710 V
  ext5v_v             5.136 V [OK]
  hdmi_a              0.014 A
  hdmi_v              5.143 V
  vdd_core_a          1.018 A
  vdd_core_v          0.796 V
  ...

Throttle State:
  Raw:    0x0
  Flags:  None

Temperatures:
  CPU        46.9 C  [OK]
  PMIC       48.6 C  [OK]
  NVMe       31.9 C  [OK]

Voltage Alarm: No
```

## Configuration

Edit `/etc/pi-power-guard/config.ini`. Changes take effect after `sudo systemctl restart pi-power-guard`.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| general | `poll_interval` | `1` | Seconds between sensor reads |
| general | `baseline_interval` | `5` | Seconds between full log entries (even without changes) |
| general | `sync_interval` | `10` | Seconds between fdatasync (10 for NVMe, 30 for SD card) |
| general | `ring_buffer_lines` | `100000` | Max lines before log rotation |
| thresholds | `ext5v_warn` | `4.85` | EXT5V warning threshold (V) |
| thresholds | `ext5v_low` | `4.75` | EXT5V low threshold (V) |
| thresholds | `ext5v_critical` | `4.50` | EXT5V critical threshold (V) |
| thresholds | `cpu_temp_warn` | `75.0` | CPU temperature warning (C) |
| thresholds | `cpu_temp_critical` | `85.0` | CPU temperature critical (C) |
| trend | `window_size` | `60` | Samples in trend analysis window |
| trend | `drop_threshold` | `0.15` | Voltage drop (V) that triggers warning |

See [config.ini](config.ini) for all options with descriptions.

## Log Format

Each line follows: `TIMESTAMP LEVEL SUBSYSTEM key=value ...`

Real output from a Pi 5 after a power cycle:
```
2026-03-27T14:25:48+0300 BOOT SYSTEM version=1.0.0 hostname=raspberrypi python=3.11.2
2026-03-27T14:25:49+0300 BOOT CRASH pm_rsts=0x1000 type=POWER_CYCLE prev_state=clean prev_time="2026-03-27T14:25:47+0300" ext4_recovery=true
2026-03-27T14:25:49+0300 INFO PMIC ext5v_v=5.121 vdd_core_v=0.756 vdd_core_a=0.875 3v3_sys_v=3.322 1v8_sys_v=1.814 ddr_vdd2_v=1.112 hdmi_v=5.142
2026-03-27T14:25:49+0300 INFO THROTTLE raw=0x0 flags=none
2026-03-27T14:25:49+0300 INFO TEMP cpu=46.9 pmic=48.7 nvme=32.9
```

What a voltage drop event looks like:
```
2026-03-27T14:35:16+0300 WARN TREND rail=ext5v_v ema=4.890 slope=-0.0820 msg="voltage trending down"
2026-03-27T14:35:17+0300 WARN PMIC ext5v_v=4.842 threshold=4.85 msg="EXT5V below warning threshold"
2026-03-27T14:35:22+0300 ALERT PMIC ext5v_v=4.720 threshold=4.75 msg="EXT5V below low threshold"
2026-03-27T14:35:22+0300 WARN THROTTLE raw=0x50005 flags=throttled,under-voltage changed="throttle:+throttled,under-voltage"
```

| Level | Meaning |
|-------|---------|
| `BOOT` | Startup info and crash detection (once per boot) |
| `INFO` | Normal baseline snapshot |
| `WARN` | Voltage trending down, temperature rising, throttle flags active |
| `ALERT` | Voltage below threshold, temperature critical |

**Useful grep commands:**

```bash
# All alerts
grep ALERT /var/log/pi-power-guard/current.log

# Crash reports
grep "BOOT CRASH" /var/log/pi-power-guard/current.log

# EXT5V voltage history
grep "ext5v_v=" /var/log/pi-power-guard/current.log

# Throttle events only
grep "THROTTLE" /var/log/pi-power-guard/current.log | grep -v "flags=none"
```

## How It Works

### Monitoring Loop

Every second, pi-power-guard reads all sensors. It compares with the previous reading and logs when:
- Any voltage changes by more than 10mV
- Any temperature changes by more than 1C
- Throttle state changes (under-voltage, frequency cap, etc.)
- Or every 5 seconds as a baseline (configurable)

### Crash Detection

On every boot, pi-power-guard checks:

1. **PM_RSTS register** (`vcgencmd get_rsts`) -- the hardware reset reason:
   - `0x1000` = Power cycle (PSU failure, cable disconnect)
   - `0x1020` = Software reboot (clean reboot or kernel panic)
   - `0x1040` = Watchdog reset (system hang detected)

2. **State file** (`/var/lib/pi-power-guard/last-state`) -- written on clean shutdown with timestamp. If the file says "booted" instead of "clean", the previous shutdown was unclean.

3. **ext4 recovery** -- checks `journalctl` for "EXT4-fs recovery" messages, confirming filesystem was dirty.

### Voltage Trend Detection

Each monitored rail (EXT5V, 3V3_SYS, VDD_CORE, 1V8_SYS) has its own tracker using:

- **Exponential Moving Average (EMA)** with alpha=0.1 for noise-resistant current estimate
- **Half-window slope**: compares average of last 30 samples vs previous 30 samples
- Warns when slope exceeds -0.15V (voltage dropping 150mV over 30 seconds)

This catches gradual PSU degradation that absolute thresholds would miss until the last moment.

### Crash-Resilient Logging

- Logs written to ring buffer file with `fdatasync` every 10 seconds
- On sudden power loss, at most ~10 seconds of data is lost (vs 5 minutes with default journald)
- Automatic rotation when file exceeds 100k lines, keeping up to 5 archives
- Total disk usage: ~50MB maximum

## systemd Integration

```bash
# Service status
systemctl status pi-power-guard

# View live logs
tail -f /var/log/pi-power-guard/current.log

# Reload config without restart (SIGHUP)
sudo systemctl reload pi-power-guard

# Restart after config changes
sudo systemctl restart pi-power-guard
```

The service runs with security hardening (`ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`) and resource limits (`CPUQuota=5%`, `MemoryMax=50M`).

## Uninstall

```bash
cd pi-power-guard
sudo bash uninstall.sh
```

## Resource Usage

Measured on a real Pi 5 (8GB, NVMe):

| Metric | Value |
|--------|-------|
| CPU | ~0.3% average |
| RAM | ~15 MB RSS |
| Disk | ~50 MB max (ring buffer + archives) |
| I/O | 1 fdatasync per 10 seconds |

## FAQ

**Does it work on Raspberry Pi 4?**
No. `vcgencmd pmic_read_adc` is Pi 5 specific (Renesas DA9091 PMIC). Pi 4 doesn't have this command.

**Is it safe on SD card?**
Yes. Set `sync_interval = 30` in config.ini to reduce writes. At 100 bytes per log line, even per-second logging produces only ~8MB/day.

**How much CPU does it use?**
~0.5% average (25ms work per 1-second cycle). Three `vcgencmd` subprocess calls and a few sysfs reads.

**What's the difference between this and `vcgencmd get_throttled`?**
`get_throttled` tells you the *current* state. pi-power-guard gives you the *history* -- what happened in the seconds before a crash, voltage trends over time, and forensic boot analysis.

**Can I use it alongside RPi-Monitor or Prometheus?**
Yes. pi-power-guard writes to its own log files and doesn't interfere with other monitoring.

## Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing`)
3. Commit your changes
4. Push to the branch
5. Open a Pull Request

## License

MIT License. See [LICENSE](LICENSE) for details.
