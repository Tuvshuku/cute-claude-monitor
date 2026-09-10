#!/usr/bin/env python3
"""Native Windows host for the collector and WPF desktop pet."""

from __future__ import annotations

import ctypes
import os
import shutil
# The child is a fixed system PowerShell path and shell mode is never used.
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

import collector


APP_TITLE = "Cute Claude Monitor"


def bundled_asset(name: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / name


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def install_example_config() -> None:
    """Put an editable example next to native state on the first run."""
    source = bundled_asset("config.example.json")
    destination = collector.APP_DIR / "config.example.json"
    if source.is_file() and not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def launch_widget(data_path: Path) -> subprocess.Popen:
    widget = bundled_asset("widget.ps1")
    if not widget.is_file():
        raise FileNotFoundError(f"Widget asset not found: {widget}")

    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    if not powershell.is_file():
        raise FileNotFoundError(f"Windows PowerShell not found: {powershell}")

    command = [
        str(powershell),
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", str(widget),
        "-DataPath", str(data_path),
        "-NativeMode",
        "-CollectorDataDir", str(collector.APP_DIR),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(command, creationflags=flags)  # nosec B603


def main() -> int:
    if os.name != "nt":
        show_error("The desktop executable currently supports Windows 10 and 11.")
        return 2

    try:
        if not collector.acquire_native_loop_lock():
            show_error(
                "Cute Claude Monitor is already running, or its data folder "
                "is not writable."
            )
            return 1
        install_example_config()
        cfg = collector.load_config()
        output_path = collector.resolve_output_path(cfg)
        state = collector.load_state()
        collector.run_once(state, cfg, output_path)
        widget_process = launch_widget(output_path)
        interval = max(float(cfg["interval_seconds"]), 1.0)
    except Exception as exc:
        show_error(f"Cute Claude Monitor could not start.\n\n{exc}")
        return 1

    next_pass = time.monotonic() + interval
    try:
        while widget_process.poll() is None:
            remaining = next_pass - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, 0.25))
                continue
            try:
                collector.run_once(state, cfg, output_path)
            except Exception as exc:
                collector.log(f"pass failed: {exc!r}")
            next_pass = time.monotonic() + interval
    except KeyboardInterrupt:
        widget_process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
