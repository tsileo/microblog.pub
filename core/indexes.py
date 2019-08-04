import pymongo

from config import DB
from core.meta import MetaKey
from core.meta import _meta


def create_indexes():
    if "trash" not in DB.collection_names():
        DB.create_collection("trash", capped=True, size=50 << 20)  # 50 MB

    if "activities" in DB.collection_names():
        DB.command("compact", "activities")

    DB.activities.create_index([(_meta(MetaKey.NOTIFICATION), pymongo.ASCENDING)])
    DB.activities.create_index(
        [(_meta(MetaKey.NOTIFICATION_UNREAD), pymongo.ASCENDING)]
    )
    DB.activities.create_index([("remote_id", pymongo.ASCENDING)])
    DB.activities.create_index([("meta.actor_id", pymongo.ASCENDING)])
    DB.activities.create_index([("meta.object_id", pymongo.ASCENDING)])
    DB.activities.create_index([("meta.thread_root_parent", pymongo.ASCENDING)])
    DB.activities.create_index(
        [
            ("meta.thread_root_parent", pymongo.ASCENDING),
            ("meta.deleted", pymongo.ASCENDING),
        ]
    )
    DB.activities.create_index(
        [("activity.object.id", pymongo.ASCENDING), ("meta.deleted", pymongo.ASCENDING)]
    )
    DB.activities.create_index(
        [("meta.object_id", pymongo.ASCENDING), ("type", pymongo.ASCENDING)]
    )

    # Index for the block query
    DB.activities.create_index(
        [
            ("box", pymongo.ASCENDING),
            ("type", pymongo.ASCENDING),
            ("meta.undo", pymongo.ASCENDING),
        ]
    )

    # Index for count queries
    DB.activities.create_index(
        [
            ("box", pymongo.ASCENDING),
            ("type", pymongo.ASCENDING),
            ("meta.undo", pymongo.ASCENDING),
            ("meta.deleted", pymongo.ASCENDING),
        ]
    )

    DB.activities.create_index([("box", pymongo.ASCENDING)])

    # Outbox query
    DB.activities.create_index(
        [
            ("box", pymongo.ASCENDING),
            ("type", pymongo.ASCENDING),
            ("meta.undo", pymongo.ASCENDING),
            ("meta.deleted", pymongo.ASCENDING),
            ("meta.public", pymongo.ASCENDING),
        ]
    )

    DB.activities.create_index(
        [
            ("type", pymongo.ASCENDING),
            ("activity.object.type", pymongo.ASCENDING),
            ("activity.object.inReplyTo", pymongo.ASCENDING),
            ("meta.deleted", pymongo.ASCENDING),
        ]
    )
