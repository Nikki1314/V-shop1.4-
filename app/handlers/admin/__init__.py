"""Admin handlers."""

from aiogram import Router

from app.config import Settings, get_settings
from app.filters.admin import IsAdmin
from app.handlers.admin import (
    broadcast,
    categories,
    loyalty,
    orders,
    panel,
    product_manage,
    products,
    statistics,
    subcategories,
    wizard_guard,
)
from app.handlers.admin import (
    settings as admin_settings,
)
from app.middlewares.admin import AdminOnlyMiddleware


def get_admin_router(settings: Settings | None = None) -> Router:
    """
    Aggregate admin routers behind authorization gates.

    Every message / callback on this tree requires ``ADMIN_IDS`` membership:
    1. Router-level :class:`IsAdmin` filters
    2. :class:`AdminOnlyMiddleware` as a hard stop for non-admins
    """
    cfg = settings or get_settings()
    router = Router(name="admin")

    router.message.filter(IsAdmin(cfg))
    router.callback_query.filter(IsAdmin(cfg))

    router.message.middleware(AdminOnlyMiddleware(cfg))
    router.callback_query.middleware(AdminOnlyMiddleware(cfg))

    # Wizard guard before section menus so mid-FSM menu taps are blocked.
    router.include_router(wizard_guard.router)
    router.include_router(products.router)
    router.include_router(product_manage.router)
    router.include_router(categories.router)
    router.include_router(subcategories.router)
    router.include_router(orders.router)
    router.include_router(broadcast.router)
    router.include_router(statistics.router)
    router.include_router(loyalty.router)
    router.include_router(admin_settings.router)
    router.include_router(panel.router)
    return router
