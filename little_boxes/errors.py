"""Errors raised by this package."""
from typing import Optional
from typing import Dict
from typing import Any


class Error(Exception):
    """HTTP-friendly base error, with a status code, a message and an optional payload."""
    status_code = 400

    def __init__(self, message: str, status_code: Optional[int] = None, payload: Optional[Dict[str, Any]] = None) -> None:
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self) -> Dict[str, Any]:
        rv = dict(self.payload or ())
        rv['message'] = self.message
        return rv

    def __repr__(self) -> str:
        return f'{self.__class__.__qualname__}({self.message!r}, payload={self.payload!r}, status_code={self.status_code})'


class ActorBlockedError(Error):
    """Raised when an activity from a blocked actor is received."""


class NotFromOutboxError(Error):
    """Raised when an activity targets an object from the inbox when an object from the oubox was expected."""


class ActivityNotFoundError(Error):
    """Raised when an activity is not found."""
    status_code = 404


class BadActivityError(Error):
    """Raised when an activity could not be parsed/initialized."""


class RecursionLimitExceededError(BadActivityError):
    """Raised when the recursion limit for fetching remote object was exceeded (likely a collection)."""


class UnexpectedActivityTypeError(BadActivityError):
    """Raised when an another activty was expected."""
