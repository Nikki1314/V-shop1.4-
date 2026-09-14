"""Admin domain exceptions."""

from __future__ import annotations


class ProductInUseError(Exception):
    """Raised when a product cannot be deleted because of order history."""


class CategoryInUseError(Exception):
    """Raised when a category still has subcategories or products."""


class SubcategoryInUseError(Exception):
    """Raised when a subcategory cannot be deleted because it still has products."""


class InvalidStatusTransitionError(Exception):
    """Raised when an order status change is not permitted by the lifecycle."""

    def __init__(self, current: object, target: object) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Cannot move order from {current} to {target}")


class CustomerLookupError(ValueError):
    """An operator's customer identifier could not be turned into exactly one customer."""


class MalformedCustomerIdentifierError(CustomerLookupError):
    """Neither a Telegram id nor a username."""


class CustomerNotFoundError(CustomerLookupError):
    """No registered customer has that id or username."""


class AmbiguousCustomerError(CustomerLookupError):
    """More than one stored row carries that username; only the id can decide."""
