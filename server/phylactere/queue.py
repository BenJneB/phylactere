"""Files à priorité (voir docs/SPEC.md, section 5.6).

Une file par ressource lente (OCR, traduction), traitée par un seul worker : les
images visibles passent avant celles préchargées, puis les tâches de fond (propositions de
glossaire, jalon 10), et deux demandes identiques en cours sont fusionnées (elles
attendent le même résultat).
"""

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

logger = logging.getLogger("phylactere.queue")

# Priorités qu'un client peut demander pour une image.
ImagePriority = Literal["visible", "prefetch"]
# « background » : tâche interne au serveur, servie après toutes les images.
Priority = Literal["visible", "prefetch", "background"]
PRIORITY_ORDER: dict[str, int] = {"visible": 0, "prefetch": 1, "background": 2}


class PriorityRunner:
    def __init__(self, name: str) -> None:
        self.name = name
        self._queue: asyncio.PriorityQueue[tuple[int, int, str]] = asyncio.PriorityQueue()
        self._order = itertools.count()
        self._futures: dict[str, asyncio.Future] = {}
        self._factories: dict[str, Callable[[], Awaitable[Any]]] = {}
        self._worker: asyncio.Task | None = None

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run(), name=f"queue-{self.name}")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        for future in self._futures.values():
            future.cancel()

    async def run(self, key: str, priority: Priority, factory: Callable[[], Awaitable[Any]]):
        """Exécute ``factory()`` à son tour ; même ``key`` en cours = même résultat."""
        future = self._futures.get(key)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._futures[key] = future
            self._factories[key] = factory
        # Une nouvelle entrée par demande : une demande « visible » fait passer devant une
        # tâche déjà en file en « prefetch » (l'entrée obsolète sera ignorée).
        self._queue.put_nowait((PRIORITY_ORDER[priority], next(self._order), key))
        # shield : si le client abandonne, la tâche continue pour les autres demandeurs.
        return await asyncio.shield(future)

    def pending(self) -> int:
        return len(self._factories)

    async def _run(self) -> None:
        while True:
            _, _, key = await self._queue.get()
            factory = self._factories.pop(key, None)
            future = self._futures.get(key)
            if factory is None or future is None:
                continue  # déjà traitée (entrée en double)
            try:
                result = await factory()
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)
            finally:
                self._futures.pop(key, None)
            # Évite l'avertissement « exception never retrieved » si tous les demandeurs
            # ont abandonné.
            if future.done() and not future.cancelled():
                future.exception()
