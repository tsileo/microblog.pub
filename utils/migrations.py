"""Automatic migration tools for the da:ta stored in MongoDB."""
import logging
from abc import ABC
from abc import abstractmethod
from typing import List
from typing import Type

from config import DB

logger = logging.getLogger(__name__)

# Used to keep track of all the defined migrations
_MIGRATIONS: List[Type["Migration"]] = []


def perform() -> None:
    """Perform all the defined migration."""
    for migration in _MIGRATIONS:
        migration().perform()


class Migration(ABC):
    """Abstract class for migrations."""

    def __init__(self) -> None:
        self.name = self.__class__.__qualname__
        self._col = DB.migrations

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _MIGRATIONS.append(cls)

    def _apply(self) -> None:
        self._col.insert_one({"name": self.name})

    def _reset(self) -> None:
        self._col.delete_one({"name": self.name})

    def _is_applied(self) -> bool:
        return bool(self._col.find_one({"name": self.name}))

    @abstractmethod
    def migrate(self) -> None:
        """Expected to be implemented by actual migrations."""
        pass

    def perform(self) -> None:
        if self._is_applied():
            logger.info(f"Skipping migration {self.name} (already applied)")
            return

        logger.info(f"Performing migration {self.name}...")
        self.migrate()

        self._apply()
        logger.info("Done")
