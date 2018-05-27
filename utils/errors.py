
class Error(Exception):
    pass


class BadActivityError(Error):
    pass


class RecursionLimitExceededError(BadActivityError):
    pass


class UnexpectedActivityTypeError(BadActivityError):
    pass
