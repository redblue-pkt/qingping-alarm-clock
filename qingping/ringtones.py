"""Ringtone signatures and custom slot selection for Qingping CGD1."""

from __future__ import annotations


# Built-in ringtone signatures (4 bytes each)
RINGTONE_SIGNATURES: dict[str, bytes] = {
    "beep": bytes.fromhex("fdc366a5"),
    "digital_1": bytes.fromhex("0961bb77"),
    "digital_2": bytes.fromhex("ba2c2c8c"),
    "cuckoo": bytes.fromhex("ea2d4c02"),
    "telephone": bytes.fromhex("791bacb3"),
    "exotic_guitar": bytes.fromhex("1d019fd6"),
    "lively_piano": bytes.fromhex("6e70b659"),
    "story_piano": bytes.fromhex("8f004886"),
    "forest_piano": bytes.fromhex("26522519"),
}

# Custom ringtone slot signatures (the app alternates between them)
CUSTOM_SLOT_DEAD = bytes.fromhex("deaddead")  # de ad de ad
CUSTOM_SLOT_BEEF = bytes.fromhex("beefbeef")  # be ef be ef


def parse_slot_signature(slot: str) -> bytes:
    """Parse slot argument into a 4-byte signature.

    Accepts:
      - 'dead' / 'deaddead'
      - 'beef' / 'beefbeef'
      - any 8-hex string (signature)
    """
    s = (slot or "").strip().lower()
    if s in ("dead", "deaddead"):
        return CUSTOM_SLOT_DEAD
    if s in ("beef", "beefbeef"):
        return CUSTOM_SLOT_BEEF

    # raw hex
    s_hex = "".join(ch for ch in s if ch in "0123456789abcdef")
    if len(s_hex) != 8:
        raise ValueError("slot must be 'dead', 'beef' or 8 hex chars (e.g. deaddead)")
    return bytes.fromhex(s_hex)


def choose_next_custom_slot(current_signature: bytes | None) -> bytes:
    """Alternate between dead/beef slots based on the current signature."""
    if current_signature == CUSTOM_SLOT_DEAD:
        return CUSTOM_SLOT_BEEF
    # default / unknown -> dead
    return CUSTOM_SLOT_DEAD


# Backwards-compatible name used in earlier drafts
def get_custom_slot_signature(current_signature: bytes | None) -> bytes:
    return choose_next_custom_slot(current_signature)
