"""Exceptions raised by the dunc_connector SDK."""


class DuncConnectorError(Exception):
    """Base for all connector errors."""


class DuncTransportError(DuncConnectorError):
    """Network or HTTP error talking to the platform."""


class DuncAuthError(DuncConnectorError):
    """Connection token rejected by the platform (401/403)."""


class DuncRunError(DuncConnectorError):
    """Run-level failure raised by the seller's handler."""


class DuncValidationError(DuncConnectorError):
    """Handler returned a value the platform won't accept (e.g. not a dict, too large)."""
