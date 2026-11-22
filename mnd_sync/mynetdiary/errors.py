"""Exception types for the MyNetDiary client."""


class MndError(Exception):
    """Base class for all MyNetDiary client errors."""


class MndAuthError(MndError):
    """Authentication/session problem (missing, invalid, or expired session)."""


class MndRequestError(MndError):
    """A request to an mui*.do endpoint failed or returned an error payload."""

    def __init__(self, message, *, endpoint=None, status=None, payload=None):
        super().__init__(message)
        self.endpoint = endpoint
        self.status = status
        self.payload = payload


class MndSearchError(MndRequestError):
    """A food search returned no usable result."""
