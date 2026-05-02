"""Vicilus connector SDK — wrap any seller agent and run it on the Vicilus platform."""

from dunc_connector.client import DuncClient
from dunc_connector.errors import (
    DuncAuthError,
    DuncConnectorError,
    DuncRunError,
    DuncTransportError,
    DuncValidationError,
)
from dunc_connector.service import DuncService

__all__ = [
    "DuncClient",
    "DuncService",
    "DuncConnectorError",
    "DuncTransportError",
    "DuncAuthError",
    "DuncRunError",
    "DuncValidationError",
]
