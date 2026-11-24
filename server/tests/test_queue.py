import asyncio

import pytest

from phylactere.queue import PriorityRunner


def test_visible_before_prefetch_and_duplicates_merged():
    order: list[str] = []
    calls: dict[str, int] = {}

    async def go():
        runner = PriorityRunner("test")
        gate = asyncio.Event()

        def job(name):
            async def work():
                calls[name] = calls.get(name, 0) + 1
                if name == "bloque":
                    await gate.wait()
                order.append(name)
                return name.upper()

            return work

        runner.start()
        first = asyncio.create_task(runner.run("bloque", "visible", job("bloque")))
        await asyncio.sleep(0)  # le worker prend « bloque »
        tasks = [
            asyncio.create_task(runner.run("p1", "prefetch", job("p1"))),
            asyncio.create_task(runner.run("p2", "prefetch", job("p2"))),
            asyncio.create_task(runner.run("v1", "visible", job("v1"))),
            asyncio.create_task(runner.run("v1", "visible", job("v1"))),  # doublon
            asyncio.create_task(runner.run("p2", "visible", job("p2"))),  # promue
        ]
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(first, *tasks)
        await runner.stop()
        return results

    results = asyncio.run(go())

    assert order == ["bloque", "v1", "p2", "p1"]
    assert calls == {"bloque": 1, "v1": 1, "p2": 1, "p1": 1}
    assert results == ["BLOQUE", "P1", "P2", "V1", "V1", "P2"]


def test_errors_reach_every_waiter_and_queue_keeps_working():
    async def go():
        runner = PriorityRunner("test")
        runner.start()

        async def boom():
            raise ValueError("raté")

        async def ok():
            return 42

        results = await asyncio.gather(
            runner.run("k", "visible", boom),
            runner.run("k", "visible", boom),
            return_exceptions=True,
        )
        after = await runner.run("k2", "visible", ok)
        await runner.stop()
        return results, after

    results, after = asyncio.run(go())
    assert all(isinstance(r, ValueError) for r in results)
    assert after == 42


def test_same_key_runs_again_once_finished():
    async def go():
        runner = PriorityRunner("test")
        runner.start()
        count = 0

        async def job():
            nonlocal count
            count += 1
            return count

        first = await runner.run("k", "visible", job)
        second = await runner.run("k", "visible", job)
        await runner.stop()
        return first, second

    assert asyncio.run(go()) == (1, 2)


@pytest.mark.parametrize("priority", ["visible", "prefetch"])
def test_pending_count(priority):
    async def go():
        runner = PriorityRunner("test")
        task = asyncio.create_task(runner.run("k", priority, lambda: asyncio.sleep(0)))
        await asyncio.sleep(0)
        pending = runner.pending()
        runner.start()
        await task
        await runner.stop()
        return pending

    assert asyncio.run(go()) == 1


def test_background_served_after_prefetch():
    """Propositions de glossaire (jalon 10) : après toutes les images, même préchargées."""
    order: list[str] = []

    async def go():
        runner = PriorityRunner("test")
        gate = asyncio.Event()

        def job(name):
            async def work():
                if name == "bloque":
                    await gate.wait()
                order.append(name)

            return work

        runner.start()
        first = asyncio.create_task(runner.run("bloque", "visible", job("bloque")))
        await asyncio.sleep(0)  # le worker prend « bloque »
        tasks = [
            asyncio.create_task(runner.run("fond", "background", job("fond"))),
            asyncio.create_task(runner.run("p1", "prefetch", job("p1"))),
            asyncio.create_task(runner.run("v1", "visible", job("v1"))),
        ]
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(first, *tasks)
        await runner.stop()

    asyncio.run(go())
    assert order == ["bloque", "v1", "p1", "fond"]
