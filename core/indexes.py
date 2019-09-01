import pymongo

from config import DB
from config import MEDIA_CACHE
from core.meta import MetaKey
from core.meta import _meta


def create_indexes():
    if "trash" not in DB.collection_names():
        DB.create_collection("trash", capped=True, size=50 << 20)  # 50 MB

    if "activities" in DB.collection_names():
        DB.command("compact", "activities")

    try:
        MEDIA_CACHE.fs._GridFS__database.command("compact", "fs.files")
        MEDIA_CACHE.fs._GridFS__database.command("compact", "fs.chunks")
    except Exception:
        pass

    DB.activities.create_index([(_meta(MetaKey.NOTIFICATION), pymongo.ASCENDING)])
    DB.activities.create_index(
        [(_meta(MetaKey.NOTIFICATION_UNREAD), pymongo.ASCENDING)]
    )
    DB.activities.create_index([("remote_id", pymongo.ASCENDING)])
    DB.activities.create_index([("meta.actor_id", pymongo.ASCENDING)])
    DB.activities.create_index([("meta.object_id", pymongo.ASCENDING)])
    DB.activities.create_index([("meta.mentions", pymongo.ASCENDING)])
    DB.activities.create_index([("meta.hashtags", pymongo.ASCENDING)])
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

    # For the is_actor_icon_cached query
    MEDIA_CACHE.fs._GridFS__files.create_index([("url", 1), ("kind", 1)])

    # Replies index
    DB.replies.create_index([("remote_id", pymongo.ASCENDING)])
    DB.replies.create_index([("meta.thread_root_parent", pymongo.ASCENDING)])
    DB.replies.create_index(
        [
            ("meta.thread_root_parent", pymongo.ASCENDING),
            ("meta.deleted", pymongo.ASCENDING),
        ]
    )
