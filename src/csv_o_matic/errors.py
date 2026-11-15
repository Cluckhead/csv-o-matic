"""The one exception the tools raise on purpose."""

from __future__ import annotations

from typing import Any


class ToolFailure(Exception):
    """A request that cannot be answered; becomes an `error` in the response."""

    def __init__(self, kind: str, message: str, **details: Any):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details
