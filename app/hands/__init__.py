"""Hands Layer — execute(name, input) → string

Every agent implements the same Hand protocol.
Cattle, not pets: interchangeable, stateless executors.
"""

from app.hands.registry import HandRegistry, hand_registry
from app.hands.base import Hand, HandResult

__all__ = ["Hand", "HandResult", "HandRegistry", "hand_registry"]
