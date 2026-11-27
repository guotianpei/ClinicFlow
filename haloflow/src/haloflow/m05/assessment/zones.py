"""Caller-supplied zone transition data (architecture v2 A4, test packet T1).

The package never reads system zone data: the caller loads and verifies the pinned IANA
release outside the package and passes its transitions here as immutable data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class ZoneData:
    """Per zone, ascending (utc_epoch_second, offset_seconds) transition pairs."""

    release: str
    digest: str
    transitions: Mapping[str, tuple[tuple[int, int], ...]]


class ZoneMissing(LookupError):
    pass


def local_datetime(zones: ZoneData, zone: str, instant: datetime) -> datetime:
    """The naive local wall-clock time of a UTC instant in `zone`."""
    pairs = zones.transitions.get(zone)
    if not pairs:
        raise ZoneMissing(zone)
    epoch = int((instant - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() // 1)
    offset: int | None = None
    for start, seconds in pairs:
        if start <= epoch:
            offset = seconds
        else:
            break
    if offset is None:
        raise ZoneMissing(zone)
    return (instant + timedelta(seconds=offset)).replace(tzinfo=None)
