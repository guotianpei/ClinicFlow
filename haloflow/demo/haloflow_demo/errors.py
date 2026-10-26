"""Exceptions shared by the demo's layers (no third-party imports)."""

from __future__ import annotations


class OwnerLost(Exception):
    """This process is no longer the demo owner; no further effects allowed."""
