"""Emergency (break-glass) admin access FSM."""

from aiogram.fsm.state import State, StatesGroup


class EmergencyAccessStates(StatesGroup):
    """After ``/emergency_admin``: the next message is the password."""

    password = State()
