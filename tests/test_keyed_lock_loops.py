"""
``keyed_lock`` across event loops.

An ``asyncio.Lock`` binds to the loop it is first contended on. The registry is
process-global, the bot's loop lives as long as the process — but every test
gets a loop of its own, and a key contended in one test (an operator's confirm,
say) used to hand a later test a lock bound to a loop that no longer runs:
``RuntimeError: ... is bound to a different event loop``, seen only when the
same key was contended in both. The registry now starts afresh on a new loop.
"""

from __future__ import annotations

import asyncio

from app.utils import concurrency
from app.utils.concurrency import keyed_lock

KEY = "loop-test:1"


async def _contend(marks: list[str]) -> None:
    async def one(tag: str) -> None:
        async with keyed_lock(KEY):
            marks.append(tag)
            await asyncio.sleep(0)  # still inside when the other arrives

    await asyncio.gather(one("a"), one("b"))


def test_a_key_contended_on_one_loop_can_be_contended_on_the_next() -> None:
    first: list[str] = []
    asyncio.run(_contend(first))  # binds the key's lock to this loop
    assert sorted(first) == ["a", "b"]
    stale = concurrency._locks.get(KEY)

    second: list[str] = []
    asyncio.run(_contend(second))  # a new loop: must not reuse the bound lock

    assert sorted(second) == ["a", "b"]
    assert concurrency._locks.get(KEY) is not stale or stale is None


async def test_within_one_loop_the_registry_is_kept() -> None:
    async with keyed_lock(KEY):
        lock = concurrency._locks[KEY]
    async with keyed_lock(KEY):
        assert concurrency._locks[KEY] is lock
    assert concurrency._users.get(KEY) is None  # released and pruned from the users map
