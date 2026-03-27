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

- **26 PMIC Rail Monitoring** -- reads every power rail voltage and current via `vcgencmd pmic_read_adc`
- **Total Power Consumption** -- real-time wattage calculated from all V*A rail pairs
- **Voltage Trend Detection** -- EMA-based slope analysis warns before hardware under-voltage triggers
- **Crash-Resilient Logging** -- `fdatasync` every 10 seconds with ring buffer rotation (max ~10s data loss vs 5 minutes with journald)
- **Boot Crash Detection** -- analyzes PM_RSTS register, state files, and ext4 recovery to determine what happened
- **Previous Session Summary** -- on boot, reports ext5v min/max/avg, warn/alert counts, and power consumption from the previous session
- **Throttle State Decoding** -- human-readable `get_throttled` bitmask with change tracking
- **CPU/GPU Frequency Tracking** -- detects frequency changes to correlate with throttle events
- **Temperature + Fan Monitoring** -- CPU, PMIC, NVMe temps and fan RPM with configurable thresholds
- **Prometheus Export** -- optional textfile collector `.prom` output for Grafana integration
- **systemd Integration** -- `Type=notify` with sd_notify watchdog, security hardening
- **Zero Dependencies** -- Python 3.11+ stdlib only, single file (~1100 lines)
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
pi-power-guard v1.1.0 -- One-Shot Check
================================================

Boot Analysis:
  PM_RSTS:           0x1000 (POWER_CYCLE)
  Previous Shutdown: BOOTED
  Previous Time:     2026-03-27T16:25:33+0300
  ext4 Recovery:     Yes

Power Rails (PMIC):
  0v8_aon_a           0.003 A
  0v8_aon_v           0.801 V
  0v8_sw_a            0.368 A
  0v8_sw_v            0.803 V
  1v1_sys_a           0.177 A
  1v1_sys_v           1.104 V
  1v8_sys_a           0.198 A
  1v8_sys_v           1.797 V
  3v3_sys_a           0.121 A
  3v3_sys_v           3.307 V
  3v7_wl_sw_a         0.082 A
  3v7_wl_sw_v         3.704 V
  ext5v_v             5.140 V [OK]
  hdmi_a              0.015 A
  hdmi_v              5.146 V
  vdd_core_a          1.161 A
  vdd_core_v          0.836 V
  ...

Throttle State:
  Raw:    0x0
  Flags:  None

Temperatures:
  CPU        47.4 C  [OK]
  PMIC       47.8 C  [OK]
  NVMe       30.9 C  [OK]

Total Power:   2.60 W
Fan Speed:     2730 RPM

Frequencies:
  CPU        1500 MHz
  GPU         500 MHz

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
| prometheus | `textfile_dir` | _(empty)_ | Path for `.prom` file output (e.g. `/var/lib/node_exporter/textfile_collector`) |

See [config.ini](config.ini) for all options with descriptions.

## Log Format

Each line follows: `TIMESTAMP LEVEL SUBSYSTEM key=value ...`

Real output from a Pi 5 after a power cycle:
```
2026-03-27T16:25:32+0300 BOOT SYSTEM version=1.1.0 hostname=raspberrypi python=3.11.2
2026-03-27T16:25:33+0300 BOOT CRASH pm_rsts=0x1000 type=POWER_CYCLE prev_state=clean ext4_recovery=true
2026-03-27T16:25:33+0300 BOOT PREV_SESSION ext5v_min=5.101 ext5v_max=5.166 ext5v_avg=5.138 samples=5431 warns=0 alerts=0
2026-03-27T16:25:35+0300 INFO PMIC ext5v_v=5.135 ... 3v3_sys_v=3.310 total_w=2.18
2026-03-27T16:25:35+0300 INFO THROTTLE raw=0x0 flags=none
2026-03-27T16:25:35+0300 INFO TEMP cpu=46.3 pmic=47.8 nvme=30.9 fan=2736rpm
2026-03-27T16:25:35+0300 INFO FREQ cpu=1600MHz gpu=500MHz
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

# Power consumption history
grep "total_w=" /var/log/pi-power-guard/current.log

# CPU frequency changes (throttle indicator)
grep "FREQ" /var/log/pi-power-guard/current.log

# Previous session summaries
grep "PREV_SESSION" /var/log/pi-power-guard/current.log
```

## How It Works

### Monitoring Loop

Every second, pi-power-guard reads all sensors (PMIC rails, temperatures, fan speed, CPU/GPU frequency, throttle state). It compares with the previous reading and logs when:
- Any voltage changes by more than 10mV
- Any temperature changes by more than 1C
- Throttle state or CPU frequency changes
- Or every 5 seconds as a baseline (configurable)

Each log cycle also calculates total power consumption (sum of V*A for all rail pairs).

### Crash Detection

On every boot, pi-power-guard checks:

1. **PM_RSTS register** (`vcgencmd get_rsts`) -- the hardware reset reason:
   - `0x1000` = Power cycle (PSU failure, cable disconnect)
   - `0x1020` = Software reboot (clean reboot or kernel panic)
   - `0x1040` = Watchdog reset (system hang detected)

2. **State file** (`/var/lib/pi-power-guard/last-state`) -- written on clean shutdown with timestamp. If the file says "booted" instead of "clean", the previous shutdown was unclean.

3. **ext4 recovery** -- checks `journalctl` for "EXT4-fs recovery" messages, confirming filesystem was dirty.

4. **Previous session summary** -- analyzes the previous log file and reports EXT5V min/max/average, total samples, warning/alert counts, and average power consumption. This gives you a complete picture of the previous session's health at a glance.

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
Yes. pi-power-guard writes to its own log files and doesn't interfere with other monitoring. For Prometheus integration, set `textfile_dir` in config.ini to your node_exporter's textfile collector directory and all metrics will be exported automatically.

**How is total power calculated?**
For each PMIC rail pair (e.g. `EXT5V_V` and `EXT5V_A`), it multiplies voltage by current and sums all pairs. This gives the total power drawn through the PMIC, which covers most of the Pi's consumption (CPU, GPU, memory, I/O). Note: USB peripherals powered externally are not included.

## Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing`)
3. Commit your changes
4. Push to the branch
5. Open a Pull Request

## License

MIT License. See [LICENSE](LICENSE) for details.
