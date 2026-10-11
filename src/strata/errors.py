class StrataError(Exception):
    """An error meant for the caller: what went wrong and what to do about it."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message} ({self.hint})" if self.hint else self.message


class NotFound(StrataError):
    pass


class Forbidden(StrataError):
    pass


class Conflict(StrataError):
    pass
