"""Errors shown to the user. Messages must never contain an API key or full URL."""


class AriadneError(Exception):
    """Base class for expected failures."""


class NotFound(AriadneError, KeyError):
    """A project or paper does not exist."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "Not found."


class BadInput(AriadneError, ValueError):
    """The user supplied something unusable."""


class MissingKey(AriadneError):
    """A required API key is not set."""


class BudgetExceeded(AriadneError):
    """A daily API limit has been reached."""


class UpstreamError(AriadneError):
    """An external API failed or rejected the request."""
