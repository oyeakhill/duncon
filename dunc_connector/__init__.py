"""Vicilus connector SDK — wrap any seller agent and run it on the Vicilus platform."""

from dunc_connector.client import DuncClient
from dunc_connector.errors import (
    DuncAuthError,
    DuncConnectorError,
    DuncRunError,
    DuncRunFinalizedError,
    DuncTransportError,
    DuncValidationError,
)
from dunc_connector.service import DuncService

__version__ = "0.1.1"

__all__ = [
    "DuncClient",
    "DuncService",
    "DuncConnectorError",
    "DuncTransportError",
    "DuncAuthError",
    "DuncRunError",
    "DuncRunFinalizedError",
    "DuncValidationError",
    "__version__",
]
