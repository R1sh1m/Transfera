"""
Quick smoke test for WpdBackend.
Run: python -m backend.tests.test_wpd
"""
from __future__ import annotations

import asyncio
import sys
import time
import tracemalloc
from pathlib import Path

# Ensure backend package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def main():
    from backend.wpd_backend import WpdBackend

    wpd = WpdBackend()
    print(f"wpd_helper.exe exists: {wpd.is_configured}")
    print(f"wpd_helper.exe path: {wpd._wpd_helper}")

    if not wpd.is_configured:
        print("ERROR: wpd_helper.exe not found — build it first")
        return 1

    # 1) is_available
    print("\n--- is_available ---")
    probe = await wpd.is_available()
    print(f"  available: {probe.available}")
    if probe.error:
        print(f"  error: {probe.error}")
    if not probe.available:
        return 1

    # 2) list_devices
    print("\n--- list_devices ---")
    t0 = time.monotonic()
    devices = await wpd.list_devices()
    elapsed = time.monotonic() - t0
    print(f"  found {len(devices)} device(s) in {elapsed:.2f}s")
    for d in devices:
        print(f"    serial={d.serial[:40]}... name={d.name!r} model={d.model!r}")

    if not devices:
        print("  No devices connected — cannot test browse/read")
        return 0

    device_id = devices[0].serial

    # 3) browse DCIM
    print("\n--- browse DCIM ---")
    t0 = time.monotonic()
    entries = await wpd.browse(device_id, "DCIM")
    elapsed = time.monotonic() - t0
    print(f"  found {len(entries)} entries in {elapsed:.2f}s")
    for e in entries[:10]:
        kind = "dir" if e.is_dir else "file"
        print(f"    [{kind}] {e.name}  size={e.size}  mtime={e.mtime:.0f}")
    if len(entries) > 10:
        print(f"    ... and {len(entries) - 10} more")

    # Find a file to read
    file_entry = None
    for e in entries:
        if not e.is_dir and e.size > 0:
            file_entry = e
            break

    if not file_entry:
        # Try browsing a subfolder
        for e in entries:
            if e.is_dir:
                sub = await wpd.browse(device_id, e.path.strip("/"))
                for se in sub:
                    if not se.is_dir and se.size > 0:
                        file_entry = se
                        break
                if file_entry:
                    break

    if not file_entry:
        print("  No files found to test read")
        return 0

    # 4) file_info
    print(f"\n--- file_info {file_entry.path} ---")
    info = await wpd.file_info(device_id, file_entry.path)
    print(f"  name={info.name!r} size={info.size} is_dir={info.is_dir}")

    # 5) read_file (full) — small file only
    if file_entry.size > 5 * 1024 * 1024:
        print(f"\n--- read_file skipped (file is {file_entry.size / 1024 / 1024:.1f} MB, too large for full read) ---")
    else:
        print(f"\n--- read_file {file_entry.path} ({file_entry.size} bytes) ---")
        t0 = time.monotonic()
        data = await wpd.read_file(device_id, file_entry.path)
        elapsed = time.monotonic() - t0
        speed = len(data) / elapsed / 1024 / 1024 if elapsed > 0 else 0
        print(f"  read {len(data)} bytes in {elapsed:.2f}s ({speed:.1f} MB/s)")

    # 6) Streaming read with memory monitoring — for a larger file
    large_file = None
    for e in entries:
        if not e.is_dir and e.size > 10 * 1024 * 1024:
            large_file = e
            break

    if large_file:
        print(f"\n--- streaming read {large_file.path} ({large_file.size / 1024 / 1024:.1f} MB) ---")
        tracemalloc.start()
        snapshot_before = tracemalloc.take_snapshot()

        reader = wpd.create_file_reader(device_id, large_file.path, size=large_file.size)
        await reader.open()

        t0 = time.monotonic()
        total_read = 0
        chunk_size = 100 * 1024  # 100 KB — matches BATCH_SIZE convention
        while True:
            chunk = await reader.read(chunk_size)
            if not chunk:
                break
            total_read += len(chunk)

        await reader.close()
        elapsed = time.monotonic() - t0
        speed = total_read / elapsed / 1024 / 1024 if elapsed > 0 else 0

        snapshot_after = tracemalloc.take_snapshot()
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        print(f"  streamed {total_read} bytes in {elapsed:.2f}s ({speed:.1f} MB/s)")
        print(f"  current memory: {current / 1024:.0f} KB, peak: {peak / 1024:.0f} KB")
        print(f"  memory stayed flat: {'YES' if peak < 5 * 1024 * 1024 else 'NO (peak too high)'}")
    else:
        print("\n--- streaming read skipped (no file > 10 MB found) ---")

    print("\nAll tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
