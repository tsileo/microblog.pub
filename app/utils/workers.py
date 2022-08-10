import asyncio
import signal
from typing import Generic
from typing import TypeVar

from loguru import logger

from app.database import AsyncSession
from app.database import async_session

T = TypeVar("T")


class Worker(Generic[T]):
    def __init__(self, workers_count: int) -> None:
        self._loop = asyncio.get_event_loop()
        self._in_flight: set[int] = set()
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=1)
        self._stop_event = asyncio.Event()
        self._workers_count = workers_count

    async def _consumer(self, db_session: AsyncSession) -> None:
        while not self._stop_event.is_set():
            message = await self._queue.get()
            try:
                await self.process_message(db_session, message)
            finally:
                self._in_flight.remove(message.id)  # type: ignore
                self._queue.task_done()

    async def _producer(self, db_session: AsyncSession) -> None:
        while not self._stop_event.is_set():
            next_message = await self.get_next_message(db_session)
            if next_message:
                self._in_flight.add(next_message.id)  # type: ignore
                await self._queue.put(next_message)
            else:
                await asyncio.sleep(1)

    async def process_message(self, db_session: AsyncSession, message: T) -> None:
        raise NotImplementedError

    async def get_next_message(self, db_session: AsyncSession) -> T | None:
        raise NotImplementedError

    async def startup(self, db_session: AsyncSession) -> None:
        return None

    def in_flight_ids(self) -> set[int]:
        return self._in_flight

    async def run_forever(self) -> None:
        signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        for s in signals:
            self._loop.add_signal_handler(
                s,
                lambda s=s: asyncio.create_task(self._shutdown(s)),
            )

        async with async_session() as db_session:
            await self.startup(db_session)
            self._loop.create_task(self._producer(db_session))
            for _ in range(self._workers_count):
                self._loop.create_task(self._consumer(db_session))

            await self._stop_event.wait()
            logger.info("Waiting for tasks to finish")
            await self._queue.join()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            logger.info(f"Cancelling {len(tasks)} tasks")
            [task.cancel() for task in tasks]

        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("stopping loop")

    async def _shutdown(self, sig: signal.Signals) -> None:
        logger.info(f"Caught {signal=}")
        self._stop_event.set()
