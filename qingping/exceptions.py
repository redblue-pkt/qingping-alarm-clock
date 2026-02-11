"""Library exceptions.

This codebase originally lived inside a Home Assistant integration and inherited
HomeAssistantError. For standalone use we keep a small local hierarchy.
"""

class QingpingError(Exception):
    """Base exception for this library."""

class NotConnectedError(QingpingError):
    """Raised when an operation requires an active BLE connection."""

class NoConfigurationError(QingpingError):
    """Raised when configuration is required but missing."""

class ValidationError(QingpingError):
    """Raised when user input is invalid for a device operation."""
