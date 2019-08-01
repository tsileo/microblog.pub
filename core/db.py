from enum import Enum
from enum import unique
from typing import Any
from typing import Dict
from typing import Optional

from config import DB

_Q = Dict[str, Any]
_Doc = Optional[Dict[str, Any]]


@unique
class CollectionName(Enum):
    ACTIVITIES = "activities"


def find_one_activity(q: _Q) -> _Doc:
    return DB[CollectionName.ACTIVITIES.value].find_one(q)
