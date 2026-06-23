"""
Transfera v2 — Source Reference Types
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Source References — discriminated union for transfer source selection
# ---------------------------------------------------------------------------
class SourceRefLocal(BaseModel):
    """Source is a local folder on this PC."""
    type: Literal["local_folder"] = "local_folder"
    path: str = Field(..., min_length=1, description="Absolute path to local directory")


class SourceRefDevice(BaseModel):
    """Source is a connected device (e.g. iPhone via AFC)."""
    type: Literal["device"] = "device"
    device_id: str = Field(..., min_length=1, description="Stable device identifier (serial/UDID)")
    device_path: str = Field("/", description="Filesystem path on the device (e.g. /DCIM/100APPLE)")
    device_name: str | None = Field(None, description="Human-readable device name for display")


# Discriminated union: tagged source reference
SourceRef = Annotated[
    SourceRefLocal | SourceRefDevice,
    Field(discriminator="type"),
]


def source_ref_to_legacy_string(ref: SourceRef) -> str:
    """
    Convert a SourceRef to the legacy string format used in the database.

    local_folder  -> "C:\\Users\\...\\Photos"
    device        -> "ios://<device_id><device_path>"

    This is needed because the database schema stores source_root and
    source_path as plain strings. New code should prefer SourceRef;
    this conversion exists solely for DB persistence compatibility.
    """
    if isinstance(ref, SourceRefLocal):
        return ref.path
    # SourceRefDevice -> ios:// serial/path format (matches existing IOS_SOURCE_PREFIX)
    serial = ref.device_id
    path = ref.device_path
    if path.startswith("/"):
        return f"ios://{serial}{path}"
    return f"ios://{serial}/{path}"


def legacy_string_to_source_ref(source_string: str) -> SourceRef:
    """
    Parse a legacy source path string into a SourceRef.

    Detects ios:// prefix and creates SourceRefDevice; otherwise creates
    SourceRefLocal with the raw path. Used when reading from the database.
    """
    if source_string.startswith("ios://"):
        # ios://<serial>/path/on/device
        without_prefix = source_string[len("ios://"):]
        slash_idx = without_prefix.find("/")
        if slash_idx == -1:
            return SourceRefDevice(
                type="device",
                device_id=without_prefix,
                device_path="/",
            )
        return SourceRefDevice(
            type="device",
            device_id=without_prefix[:slash_idx],
            device_path="/" + without_prefix[slash_idx + 1:],
        )
    return SourceRefLocal(type="local_folder", path=source_string)
