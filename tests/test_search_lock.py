import asyncio

import pytest

from app.core.search_lock import MediaItemLockManager, create_conflict_response


@pytest.mark.asyncio
async def test_search_lock_manager_acquire_and_release():
    mgr = MediaItemLockManager()
    assert mgr.is_locked(101) is False

    # First acquire succeeds
    assert mgr.try_acquire(101) is True
    assert mgr.is_locked(101) is True

    # Second acquire for same item fails
    assert mgr.try_acquire(101) is False

    # Release frees the item
    mgr.release(101)
    assert mgr.is_locked(101) is False

    # Can be re-acquired
    assert mgr.try_acquire(101) is True
    mgr.release(101)


@pytest.mark.asyncio
async def test_search_lock_manager_multiple_items_independent():
    mgr = MediaItemLockManager()
    assert mgr.try_acquire(1) is True
    assert mgr.try_acquire(2) is True
    assert mgr.try_acquire(3) is True

    assert mgr.is_locked(1) is True
    assert mgr.is_locked(2) is True
    assert mgr.is_locked(3) is True

    mgr.release(2)
    assert mgr.is_locked(1) is True
    assert mgr.is_locked(2) is False
    assert mgr.is_locked(3) is True

    mgr.release(1)
    mgr.release(3)


@pytest.mark.asyncio
async def test_search_lock_manager_context_manager():
    mgr = MediaItemLockManager()

    async with mgr.lock(50):
        assert mgr.is_locked(50) is True
        assert mgr.try_acquire(50) is False

    assert mgr.is_locked(50) is False
    assert mgr.try_acquire(50) is True
    mgr.release(50)


@pytest.mark.asyncio
async def test_search_lock_manager_context_manager_exception_cleanup():
    mgr = MediaItemLockManager()

    with pytest.raises(RuntimeError):
        async with mgr.lock(99):
            assert mgr.is_locked(99) is True
            raise RuntimeError("Simulation error")

    assert mgr.is_locked(99) is False
    assert mgr.try_acquire(99) is True
    mgr.release(99)


@pytest.mark.asyncio
async def test_search_lock_manager_concurrent_wait():
    mgr = MediaItemLockManager()
    execution_order = []

    async def worker(item_id: int, name: str, delay: float):
        async with mgr.lock(item_id):
            execution_order.append(f"{name}_start")
            await asyncio.sleep(delay)
            execution_order.append(f"{name}_end")

    # worker 1 and worker 2 contend on item 10, worker 3 runs independently on item 20
    await asyncio.gather(
        worker(10, "w1", 0.05),
        worker(10, "w2", 0.02),
        worker(20, "w3", 0.01),
    )

    assert "w1_start" in execution_order
    assert "w1_end" in execution_order
    assert "w2_start" in execution_order
    assert "w2_end" in execution_order
    assert "w3_start" in execution_order

    # w1 must completely finish before w2 starts because both target item 10
    assert execution_order.index("w1_end") < execution_order.index("w2_start")


def test_create_conflict_response():
    resp = create_conflict_response(
        "A search is already in progress for this show. Please wait for it to complete."
    )
    assert resp.status_code == 409
    assert "A search is already in progress for this show." in resp.body.decode("utf-8")
    assert "HX-Trigger" in resp.headers
    assert "showToast" in resp.headers["HX-Trigger"]


@pytest.mark.asyncio
async def test_search_lock_manager_event_pruning():
    mgr = MediaItemLockManager()
    assert mgr.try_acquire(42) is True
    mgr.release(42)
    # Event and waiter entries should be pruned when idle
    assert 42 not in mgr._events
    assert 42 not in mgr._waiters

    # Also test with scoped lock
    async with mgr.lock(42):
        assert mgr.is_locked(42) is True
    assert 42 not in mgr._events
    assert 42 not in mgr._waiters
