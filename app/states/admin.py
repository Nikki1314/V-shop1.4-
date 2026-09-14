"""Admin FSM states."""

from aiogram.fsm.state import State, StatesGroup


class AddProductStates(StatesGroup):
    photo = State()
    name_ru = State()
    name_en = State()
    name_de = State()
    name_uk = State()
    description_ru = State()
    description_en = State()
    description_de = State()
    description_uk = State()
    category = State()
    subcategory = State()
    flavor = State()
    volume = State()
    nicotine_strength = State()
    price = State()
    confirmation = State()


class EditProductStates(StatesGroup):
    photo = State()
    name_ru = State()
    name_en = State()
    name_de = State()
    name_uk = State()
    description_ru = State()
    description_en = State()
    description_de = State()
    description_uk = State()
    category = State()
    subcategory = State()
    flavor = State()
    volume = State()
    nicotine_strength = State()
    price = State()
    confirmation = State()


class EditPriceStates(StatesGroup):
    price = State()


class EditDescriptionStates(StatesGroup):
    description_ru = State()
    description_en = State()
    description_de = State()
    description_uk = State()


class CreateCategoryStates(StatesGroup):
    """Four-language category creation."""

    name_ru = State()
    name_en = State()
    name_de = State()
    name_uk = State()


class RenameCategoryStates(StatesGroup):
    """Edit one localized category name; the language lives in FSM data."""

    name = State()


class CreateSubcategoryStates(StatesGroup):
    """Four-language brand creation."""

    name_ru = State()
    name_en = State()
    name_de = State()
    name_uk = State()


class RenameSubcategoryStates(StatesGroup):
    """Edit one localized brand name; the language lives in FSM data."""

    name = State()


class SearchOrderStates(StatesGroup):
    query = State()


class BroadcastStates(StatesGroup):
    compose = State()
    preview = State()


class AdjustStampsStates(StatesGroup):
    """Manual stamp credit: who, how many, confirm."""

    target = State()
    amount = State()
    confirmation = State()


# Used to exclude admin menu handlers while any product wizard is active.
PRODUCT_WIZARD_STATES = (
    AddProductStates,
    EditProductStates,
    EditPriceStates,
    EditDescriptionStates,
)

CATEGORY_WIZARD_STATES = (
    CreateCategoryStates,
    RenameCategoryStates,
)

SUBCATEGORY_WIZARD_STATES = (
    CreateSubcategoryStates,
    RenameSubcategoryStates,
)

ORDER_WIZARD_STATES = (SearchOrderStates,)

BROADCAST_WIZARD_STATES = (BroadcastStates,)

LOYALTY_WIZARD_STATES = (AdjustStampsStates,)

# Exclude reply-menu handlers while any admin wizard is active.
# A new wizard MUST be registered here or menu taps will corrupt its state.
ADMIN_WIZARD_STATES = (
    PRODUCT_WIZARD_STATES
    + CATEGORY_WIZARD_STATES
    + SUBCATEGORY_WIZARD_STATES
    + ORDER_WIZARD_STATES
    + BROADCAST_WIZARD_STATES
    + LOYALTY_WIZARD_STATES
)
