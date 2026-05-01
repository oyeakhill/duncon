from dunc_connector.errors import (
    DuncAuthError,
    DuncConnectorError,
    DuncRunError,
    DuncTransportError,
    DuncValidationError,
)


def test_error_hierarchy() -> None:
    for cls in (DuncTransportError, DuncAuthError, DuncRunError, DuncValidationError):
        assert issubclass(cls, DuncConnectorError)
        assert issubclass(cls, Exception)


def test_errors_carry_message() -> None:
    err = DuncTransportError("network down")
    assert str(err) == "network down"
