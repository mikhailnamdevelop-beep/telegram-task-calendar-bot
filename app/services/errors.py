from __future__ import annotations

from app.domain import Item


class ItemServiceError(RuntimeError):
    pass


class ItemNotFoundError(ItemServiceError, LookupError):
    pass


class AmbiguousItemError(ItemServiceError):
    def __init__(self, candidates: list[Item]):
        self.candidates = candidates
        super().__init__(f"Item reference matches {len(candidates)} items")


class InvalidItemCommandError(ItemServiceError, ValueError):
    pass
