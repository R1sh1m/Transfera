#!/usr/bin/env python3
"""
Transfera v2 — iOS Connection Diagnostic & Auto-Recovery
=========================================================

Standalone script that verifies usbmuxd can communicate with a connected
iPhone and **automatically attempts self-healing** when something is missing:

  - If pymobiledevice3 is missing → pip-installs it.
  - If the Apple driver service is not running → starts it (elevated if
    needed via ShellExecuteW).
  - If the Apple driver is not installed at all → falls back to usbipd
    passthrough (Tier 2 / WSL bridge).

This script can be run OUTSIDE the Transfera FastAPI app — it does NOT
depend on any backend server being running.

Usage:
    python -m backend.debug_ios_connection
    python -m backend.debug_ios_connection --serial <UDID>
    python -m backend.debug_ios_connection --check-bridge
    python -m backend.debug_ios_connection --skip-auto-recover

Exit codes:
    0  — All checks passed (device ready, AFC accessible)
    1  — Device found but not accessible (locked / not trusted / error)
    2  — No usbmuxd or no device detected
    3  — pymobiledevice3 not installed and install refused or failed
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import textwrap

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)-8s | %(name)s | %(message)s",
)

# ---------------------------------------------------------------------------
# ANSI colour helpers (safe ASCII symbols + fallback for narrow consoles)
# ---------------------------------------------------------------------------
class _Style:
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _ok(msg: str) -> str:
    return f"{_Style.GREEN}OK{_Style.RESET} {msg}"


def _warn(msg: str) -> str:
    return f"{_Style.YELLOW}!!{_Style.RESET} {msg}"


def _fail(msg: str) -> str:
    return f"{_Style.RED}XX{_Style.RESET} {msg}"


def _info(msg: str) -> str:
    return f"{_Style.CYAN}..{_Style.RESET} {msg}"


def _header(msg: str) -> str:
    return f"\n{_Style.BOLD}{msg}{_Style.RESET}\n{'--' * int(len(msg) / 2 + 0.5)}"


# ---------------------------------------------------------------------------
# Auto-recovery helpers
# ---------------------------------------------------------------------------
async def _pip_install_pymobiledevice3() -> bool:
    """Install pymobiledevice3 via pip."""
    print(_info("Attempting pip install pymobiledevice3 ..."))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "pymobiledevice3",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            print(_ok("pymobiledevice3 installed via pip"))
            return True
        detail = stderr.decode("utf-8", errors="replace").strip()
        print(_fail(f"pip install failed: {detail.splitlines()[-1]}"))
        return False
    except Exception as exc:
        print(_fail(f"pip install exception: {exc}"))
        return False


async def _ensure_apple_service_running() -> dict:
    """Check and (if needed) start Apple Mobile Device Service.

    Returns a dict with keys:
      state — "running", "started", "elevation_required", "not_installed", "error"
      elevation_command — list of args for ShellExecuteW when elevation is needed
    """
    import asyncio.subprocess as asp

    # Check current state
    proc = await asyncio.create_subprocess_exec(
        "sc", "query", "Apple Mobile Device Service",
        stdout=asp.PIPE, stderr=asp.PIPE,
    )
    stdout, _ = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace")

    if "RUNNING" in out:
        print(_ok("Apple Mobile Device Service is RUNNING"))
        return {"state": "running"}

    if "1060" in out or "SERVICE_NAME" not in out:
        print(_warn("Apple Mobile Device Service is NOT INSTALLED"))
        return {"state": "not_installed"}

    # Service exists but not running — try to start
    print(_info("Apple Mobile Device Service is STOPPED — attempting start ..."))
    proc2 = await asyncio.create_subprocess_exec(
        "sc", "start", "Apple Mobile Device Service",
        stdout=asp.PIPE, stderr=asp.PIPE,
    )
    _, stderr2 = await proc2.communicate()

    if proc2.returncode == 0:
        print(_ok("Apple Mobile Device Service STARTED"))
        return {"state": "started"}

    err2 = stderr2.decode("utf-8", errors="replace")
    if "5" in err2 or "ACCESS_DENIED" in err2.upper():
        print(_warn("Apple Mobile Device Service needs elevation to start"))
        return {"state": "elevation_required", "elevation_command": ["sc", "start", "Apple Mobile Device Service"]}

    print(_fail(f"sc start failed (exit {proc2.returncode}): {err2.strip()}"))
    return {"state": "error", "message": err2.strip()}


async def _elevate_and_start_service() -> bool:
    """Attempt to start the Apple service with ShellExecuteW runas."""
    import ctypes

    print(_info("Requesting admin elevation to start Apple Mobile Device Service ..."))
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "sc", "start Apple Mobile Device Service",
            None, 0,
        )
        # ShellExecuteW returns > 32 on success
        if ret > 32:
            # Wait for service to come up
            await asyncio.sleep(2)
            proc = await asyncio.create_subprocess_exec(
                "sc", "query", "Apple Mobile Device Service",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if "RUNNING" in stdout.decode("utf-8", errors="replace"):
                print(_ok("Apple Mobile Device Service STARTED (elevated)"))
                return True
            print(_warn("Elevated start issued but service not yet RUNNING"))
            return False
        if ret == 0:
            print(_fail("Elevation cancelled by user"))
            return False
        print(_fail(f"ShellExecuteW returned {ret} — elevation may have failed"))
        return False
    except Exception as exc:
        print(_fail(f"Elevation failed: {exc}"))
        return False


async def _auto_recover_usb_passthrough() -> dict:
    """Try to attach Apple devices via usbipd for WSL bridge (Tier 2)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "usbipd", "list",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        print(_warn("usbipd not found on PATH — install usbipd-win from https://github.com/dorssel/usbipd-win"))
        return {"success": False, "reason": "usbipd_not_found"}
    lines = stdout.decode("utf-8", errors="replace").splitlines()

    apple_devices = []
    for line in lines:
        if "05ac" in line.lower():
            apple_devices.append(line.strip())

    if not apple_devices:
        print(_warn("No Apple devices (VID 05ac) found in usbipd list"))
        return {"success": False, "reason": "no_apple_devices"}

    print(_info(f"Found {len(apple_devices)} Apple device(s) in usbipd list"))

    for dev_line in apple_devices:
        parts = dev_line.split()
        busid = parts[0] if parts else ""
        state = " ".join(parts[1:]) if len(parts) > 1 else ""

        if "Not attached" in state or "Attached" not in state:
            print(_info(f"Attaching busid {busid} to WSL ..."))
            attach_proc = await asyncio.create_subprocess_exec(
                "usbipd", "attach", "--wsl", "--busid", busid,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            _, err = await attach_proc.communicate()
            if attach_proc.returncode == 0:
                print(_ok(f"usbipd attached {busid} to WSL"))
            else:
                err_text = err.decode("utf-8", errors="replace").strip()
                print(_warn(f"usbipd attach {busid} failed: {err_text}"))
        else:
            print(_ok(f"usbipd device {busid} already attached"))

    return {"success": True, "devices_attached": len(apple_devices)}


# ---------------------------------------------------------------------------
# Diagnostic checks
# ---------------------------------------------------------------------------
async def check_pymobiledevice3(skip_recover: bool = False) -> bool:
    """Check that pymobiledevice3 is importable; auto-install if missing."""
    try:
        import pymobiledevice3  # noqa: F401
        print(_ok("pymobiledevice3 is installed"))
        return True
    except ImportError:
        if skip_recover:
            print(_fail("pymobiledevice3 is NOT installed"))
            return False

        print(_warn("pymobiledevice3 is NOT installed — attempting auto-install ..."))
        ok = await _pip_install_pymobiledevice3()
        if ok:
            # Re-import to verify
            try:
                import pymobiledevice3  # noqa: F401
                return True
            except ImportError:
                pass
        return False


async def check_usbmuxd_socket() -> tuple[bool, str | None]:
    """Check that the usbmuxd socket is reachable.

    On Windows, usbmuxd (Apple Mobile Device Support) listens on
    127.0.0.1:27015.  On Linux/WSL, it uses a Unix socket at
    /var/run/usbmuxd.
    """
    import socket

    # Try Windows path first
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(("127.0.0.1", 27015))
        sock.close()
        print(_ok("usbmuxd reachable on 127.0.0.1:27015 (Apple driver)"))
        return True, "tcp:127.0.0.1:27015"
    except (TimeoutError, ConnectionRefusedError, OSError):
        pass

    # Try Unix socket (WSL / native Linux)
    for path in ("/var/run/usbmuxd", "/run/usbmuxd"):
        if os.path.exists(path):
            print(_ok(f"usbmuxd socket found at {path}"))
            return True, path

    print(_warn("usbmuxd socket not reachable on TCP 27015 or /var/run/usbmuxd"))
    return False, None


async def list_devices() -> list[dict]:
    """Enumerate all devices visible to usbmux."""
    from pymobiledevice3.exceptions import ConnectionFailedToUsbmuxdError
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.usbmux import list_devices

    try:
        mux_devices = list_devices()
    except ConnectionFailedToUsbmuxdError as exc:
        print(_fail(f"usbmuxd connection failed: {exc}"))
        return []
    except Exception as exc:
        print(_fail(f"Failed to list usbmux devices: {exc}"))
        return []

    if not mux_devices:
        print(_warn("No devices connected via usbmux"))
        return []

    print(_ok(f"usbmux reports {len(mux_devices)} connected device(s)"))
    results: list[dict] = []

    for mux_dev in mux_devices:
        serial = mux_dev.serial
        entry: dict = {
            "serial": serial,
            "mux_connection": getattr(mux_dev, "connection_type", "USB"),
            "status": "unknown",
        }

        print(f"\n  {_Style.BOLD}Device: {serial}{_Style.RESET}")

        try:
            lockdown = await asyncio.wait_for(
                asyncio.to_thread(create_using_usbmux, serial=serial, autopair=False),
                timeout=6.0,
            )
        except TimeoutError:
            print(f"    {_fail('lockdown timed out — device is LOCKED')}")
            entry["status"] = "locked"
            results.append(entry)
            continue
        except Exception as exc:
            exc_str = str(exc).lower()
            if "not paired" in exc_str or "trust" in exc_str:
                print(f"    {_fail('device NOT TRUSTED — pair record missing')}")
                entry["status"] = "not_trusted"
            else:
                print(f"    {_fail(f'lockdown failed: {exc}')}")
                entry["status"] = "error"
            results.append(entry)
            continue

        # Device responded to lockdown — extract device info
        try:
            info = lockdown.short_info
            entry["name"] = info.get("DeviceName", "Unknown")
            entry["model"] = info.get("ProductType", "Unknown")
            entry["version"] = info.get("ProductVersion", "Unknown")
            entry["status"] = "ready"

            print(f"    {_ok('lockdown connected')}")
            print(f"    {_info('Name:    ')}{entry.get('name', '?')}")
            print(f"    {_info('Model:   ')}{entry.get('model', '?')}")
            print(f"    {_info('iOS:     ')}{entry.get('version', '?')}")
            print(f"    {_info('Conn:    ')}{entry.get('mux_connection', '?')}")

            # Trust check — all_values requires an established trust relationship
            trust_ok = False
            try:
                _ = lockdown.all_values
                trust_ok = True
            except Exception:
                trust_ok = False

            if trust_ok:
                print(f"    {_ok('trust handshake established')}")
            else:
                print(f"    {_warn('trust handshake NOT confirmed (limited access)')}")
                entry["status"] = "not_trusted"

        finally:
            lockdown.close()

        results.append(entry)

    return results


async def check_afc_browse(results: list[dict]) -> None:
    """For each 'ready' device, open an AFC session and list the root directory."""
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    for dev in results:
        if dev["status"] not in ("ready",):
            continue

        print(f"\n  {_info('AFC session for ')}{dev.get('name', dev['serial'])}")

        try:
            lockdown = await asyncio.wait_for(
                asyncio.to_thread(create_using_usbmux, serial=dev["serial"], autopair=True),
                timeout=8.0,
            )
        except Exception as exc:
            print(f"    {_fail(f'lockdown for AFC failed: {exc}')}")
            continue

        afc = None
        try:
            afc = AfcService(lockdown=lockdown)
            await afc.__aenter__()

            # List device info (free space, etc.)
            try:
                dev_info = await asyncio.to_thread(afc.get_device_info)
                print(f"    {_ok('AFC service opened')}")
                fs_size = dev_info.get("FSTotalSize", "?")
                fs_free = dev_info.get("FSFreeBytes", "?")
                print(f"    {_info('FS total: ')}{fs_size}")
                print(f"    {_info('FS free:  ')}{fs_free}")
            except Exception as exc:
                print(f"    {_warn(f'device info failed: {exc}')}")

            # List root directory
            try:
                root_entries = await asyncio.to_thread(afc.listdir, "/")
                print(f"    {_ok(f'AFC root contains {len(root_entries)} entries')}")
                print(f"    {_info('Top-level folders:')}")
                shown = 0
                for name in sorted(root_entries):
                    if name.startswith("."):
                        continue
                    if shown >= 20:
                        print(f"      ... and {len(root_entries) - shown - 1} more")
                        break
                    try:
                        stat = await asyncio.to_thread(afc.stat, f"/{name}")
                        is_dir = stat.get("st_ifmt") == "S_IFDIR"
                        suffix = "/" if is_dir else ""
                        print(f"      {name}{suffix}")
                    except Exception:
                        print(f"      {name}")
                    shown += 1

                if "DCIM" in root_entries:
                    print(f"    {_ok('DCIM directory found — media accessible')}")
                else:
                    print(f"    {_warn('DCIM directory NOT found in AFC root')}")

            except Exception as exc:
                print(f"    {_fail(f'AFC root listing failed: {exc}')}")

        except Exception as exc:
            print(f"    {_fail(f'AFC failed: {exc}')}")
        finally:
            if afc is not None:
                try:
                    await afc.__aexit__(None, None, None)
                except Exception:
                    pass
            try:
                lockdown.close()
            except Exception:
                pass


async def check_wsl_bridge() -> dict | None:
    """Probe the WSL2 bridge health endpoint if available."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session, session.get(
            "http://127.0.0.1:18920/health",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as resp:
            if resp.status == 200:
                body = await resp.json()
                print(_ok(f"WSL bridge reachable at 127.0.0.1:18920 (tier={body.get('tier', '?')})"))
                return body
            print(_warn(f"WSL bridge returned HTTP {resp.status}"))
            return None
    except Exception as exc:
        print(_warn(f"WSL bridge not reachable: {exc}"))
        return None


# ---------------------------------------------------------------------------
# Auto-recovery: attempt to fix missing connectivity
# ---------------------------------------------------------------------------
async def _auto_recover(skip_recover: bool) -> bool:
    """Try to recover usbmuxd connectivity by starting the Apple service.

    Returns True if connectivity was restored.
    """
    if skip_recover:
        return False

    print(_header("Auto-Recovery"))
    print(_info("usbmuxd not reachable — attempting self-healing ..."))

    # Phase 1: try to start the Apple service
    service = await _ensure_apple_service_running()
    if service["state"] in ("running", "started"):
        import socket
        for attempt in range(5):
            await asyncio.sleep(1)
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect(("127.0.0.1", 27015))
                sock.close()
                print(_ok("usbmuxd reachable after service start"))
                return True
            except (TimeoutError, ConnectionRefusedError, OSError):
                continue
        print(_warn("usbmuxd still not reachable after service start"))
        return False

    # Phase 2: elevation required
    if service.get("state") == "elevation_required":
        elevated = await _elevate_and_start_service()
        if elevated:
            import socket
            for attempt in range(5):
                await asyncio.sleep(1)
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1.0)
                    sock.connect(("127.0.0.1", 27015))
                    sock.close()
                    print(_ok("usbmuxd reachable after elevated service start"))
                    return True
                except (TimeoutError, ConnectionRefusedError, OSError):
                    continue
        return False

    # Phase 3: service not installed — try usbipd passthrough
    if service.get("state") == "not_installed":
        print(_info("Apple driver not installed — falling back to usbipd passthrough (Tier 2)"))
        await _auto_recover_usb_passthrough()
        # Check if the WSL bridge is running
        bridge = await check_wsl_bridge()
        if bridge:
            print(_ok("WSL bridge reachable via usbipd passthrough"))
            return True
        return False

    return False


# ---------------------------------------------------------------------------
# Main diagnostic runner
# ---------------------------------------------------------------------------
async def diagnose(
    serial_filter: str | None = None,
    check_bridge: bool = False,
    skip_auto_recover: bool = False,
) -> int:
    """Run the full diagnostic and return an exit code."""
    print(f"{_Style.BOLD}Transfera iOS Connection Diagnostic{_Style.RESET}")
    print(f"{_Style.DIM}======================================{_Style.RESET}\n")

    # 1. Check pymobiledevice3 (auto-install if missing)
    if not await check_pymobiledevice3(skip_recover=skip_auto_recover):
        print(f"\n{_fail('pymobiledevice3 is required for iOS device access')}")
        if not skip_auto_recover:
            print("  Auto-install attempted and failed — try:")
            print("    pip install pymobiledevice3")
        return 3

    # 2. Check usbmuxd socket
    usbmux_ok, usbmux_path = await check_usbmuxd_socket()

    # If socket not reachable, try auto-recovery
    if not usbmux_ok:
        recovered = await _auto_recover(skip_recover=skip_auto_recover)
        if not recovered:
            print(f"\n{_fail('Could not establish usbmuxd connectivity automatically')}")
            print("  Manual steps:")
            print("    1. Install Apple Mobile Device Support (iTunes or Apple Devices from Microsoft Store)")
            print("    2. Or ensure usbipd passthrough is set up for WSL bridge (Tier 2)")
            return 2
        # Refresh the socket check after recovery
        usbmux_ok, usbmux_path = await check_usbmuxd_socket()
        if not usbmux_ok:
            return 2

    # 3. List devices via usbmux
    print(_header("Device Enumeration"))
    devices = await list_devices()

    if not devices:
        return 2

    if serial_filter:
        devices = [d for d in devices if d["serial"] == serial_filter]
        if not devices:
            print(f"\n{_fail(f'No device found with serial {serial_filter}')}")
            return 2

    ready_devices = [d for d in devices if d["status"] == "ready"]
    locked_devices = [d for d in devices if d["status"] == "locked"]
    untrusted_devices = [d for d in devices if d["status"] == "not_trusted"]
    error_devices = [d for d in devices if d["status"] == "error"]

    if locked_devices:
        print(f"\n{_warn(f'{len(locked_devices)} device(s) are LOCKED')}")
        print("  Unlock the device and tap 'Trust This Computer' if prompted.")

    if untrusted_devices:
        print(f"\n{_warn(f'{len(untrusted_devices)} device(s) are NOT TRUSTED')}")
        print("  Tap 'Trust This Computer' on the device and enter the passcode.")

    if error_devices:
        print(f"\n{_fail(f'{len(error_devices)} device(s) have errors')}")
        for d in error_devices:
            print(f"  {d.get('serial', '?')}")

    if ready_devices:
        print(_header("AFC Filesystem Access"))
        await check_afc_browse(ready_devices)
    else:
        print(f"\n{_warn('No devices in READY state — skipping AFC probe')}")

    if check_bridge:
        print(_header("WSL Bridge (Tier 2)"))
        await check_wsl_bridge()

    print(_header("Summary"))
    print(f"  Total devices detected:  {len(devices)}")
    print(f"  Ready (AFC accessible):  {len(ready_devices)}")
    print(f"  Locked:                  {len(locked_devices)}")
    print(f"  Not trusted:             {len(untrusted_devices)}")
    print(f"  Error:                   {len(error_devices)}")

    if ready_devices:
        print(f"\n{_ok('Diagnostic PASSED — device ready for transfer')}")
        return 0
    elif locked_devices:
        print(f"\n{_warn('Diagnostic PARTIAL — device found but LOCKED')}")
        return 1
    elif untrusted_devices:
        print(f"\n{_warn('Diagnostic PARTIAL — device found but NOT TRUSTED')}")
        return 1
    else:
        print(f"\n{_fail('Diagnostic FAILED — no accessible devices')}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transfera iOS Connection Diagnostic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python -m backend.debug_ios_connection
              python -m backend.debug_ios_connection --serial 00008100-1234ABCD
              python -m backend.debug_ios_connection --check-bridge
              python -m backend.debug_ios_connection --skip-auto-recover
        """),
    )
    parser.add_argument(
        "--serial", "-s",
        type=str,
        default=None,
        help="Filter diagnostics to a specific device serial/UDID",
    )
    parser.add_argument(
        "--check-bridge", "-b",
        action="store_true",
        help="Also probe the WSL2 bridge health endpoint (if running)",
    )
    parser.add_argument(
        "--skip-auto-recover",
        action="store_true",
        help="Skip automatic self-healing (just diagnose)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging from pymobiledevice3",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("pymobiledevice3").setLevel(logging.INFO)

    try:
        return asyncio.run(diagnose(
            serial_filter=args.serial,
            check_bridge=args.check_bridge,
            skip_auto_recover=args.skip_auto_recover,
        ))
    except KeyboardInterrupt:
        print("\nDiagnostic interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
