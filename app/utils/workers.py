import asyncio
import signal
from typing import Generic
from typing import TypeVar

from loguru import logger

from app.database import AsyncSession
from app.database import async_session

T = TypeVar("T")


class Worker(Generic[T]):
    def __init__(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._stop_event = asyncio.Event()

    async def process_message(self, db_session: AsyncSession, message: T) -> None:
        raise NotImplementedError

    async def get_next_message(self, db_session: AsyncSession) -> T | None:
        raise NotImplementedError

    async def startup(self, db_session: AsyncSession) -> None:
        return None

    async def _main_loop(self, db_session: AsyncSession) -> None:
        while not self._stop_event.is_set():
            next_message = await self.get_next_message(db_session)
            if next_message:
                await self.process_message(db_session, next_message)
                await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(2)

    async def _until_stopped(self) -> None:
        await self._stop_event.wait()

    async def run_forever(self) -> None:
        signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        for s in signals:
            self._loop.add_signal_handler(
                s,
                lambda s=s: asyncio.create_task(self._shutdown(s)),
            )

        async with async_session() as db_session:
            await self.startup(db_session)
            task = self._loop.create_task(self._main_loop(db_session))
            stop_task = self._loop.create_task(self._until_stopped())

            done, pending = await asyncio.wait(
                {task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            logger.info(f"Waiting for tasks to finish {done=}/{pending=}")
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            logger.info(f"Cancelling {len(tasks)} tasks")
            [task.cancel() for task in tasks]

        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=15,
            )
        except asyncio.TimeoutError:
            logger.info("Tasks failed to cancel")

        logger.info("stopping loop")

    async def _shutdown(self, sig: signal.Signals) -> None:
        logger.info(f"Caught {sig=}")
        self._stop_event.set()
