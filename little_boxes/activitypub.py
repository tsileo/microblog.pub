"""Core ActivityPub classes."""
import logging
import json
import binascii
import os
from datetime import datetime
from enum import Enum

from .errors import BadActivityError
from .errors import UnexpectedActivityTypeError
from .errors import NotFromOutboxError
from . import utils
from .remote_object import OBJECT_FETCHER

from typing import List
from typing import Optional
from typing import Dict
from typing import Any
from typing import Union
from typing import Type

logger = logging.getLogger(__name__)

# Helper/shortcut for typing
ObjectType = Dict[str, Any]
ObjectOrIDType = Union[str, ObjectType]

CTX_AS = 'https://www.w3.org/ns/activitystreams'
CTX_SECURITY = 'https://w3id.org/security/v1'
AS_PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'

COLLECTION_CTX = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
    {
        "Hashtag": "as:Hashtag",
        "sensitive": "as:sensitive",
    }
]

# Will be used to keep track of all the defined activities
_ACTIVITY_CLS: Dict['ActivityTypeEnum', Type['_BaseActivity']] = {}


class ActivityType(Enum):
    """Supported activity `type`."""
    ANNOUNCE = 'Announce'
    BLOCK = 'Block'
    LIKE = 'Like'
    CREATE = 'Create'
    UPDATE = 'Update'
    PERSON = 'Person'
    ORDERED_COLLECTION = 'OrderedCollection'
    ORDERED_COLLECTION_PAGE = 'OrderedCollectionPage'
    COLLECTION_PAGE = 'CollectionPage'
    COLLECTION = 'Collection'
    NOTE = 'Note'
    ACCEPT = 'Accept'
    REJECT = 'Reject'
    FOLLOW = 'Follow'
    DELETE = 'Delete'
    UNDO = 'Undo'
    IMAGE = 'Image'
    TOMBSTONE = 'Tombstone'


def parse_activity(payload: ObjectType, expected: Optional[ActivityType] = None) -> 'BaseActivity':
    t = ActivityType(payload['type'])

    if expected and t != expected:
        raise UnexpectedActivityTypeError(f'expected a {expected.name} activity, got a {payload["type"]}')

    if t not in _ACTIVITY_CLS:
        raise BadActivityError(f'unsupported activity type {payload["type"]}')

    activity = _ACTIVITY_CLS[t](**payload)

    return activity


def random_object_id() -> str:
    """Generates a random object ID."""
    return binascii.hexlify(os.urandom(8)).decode('utf-8')


def _to_list(data: Union[List[Any], Any]) -> List[Any]:
    """Helper to convert fields that can be either an object or a list of objects to a list of object."""
    if isinstance(data, list):
        return data
    return [data]


def clean_activity(activity: ObjectType) -> Dict[str, Any]:
    """Clean the activity before rendering it.
     - Remove the hidden bco and bcc field
    """
    for field in ['bto', 'bcc']:
        if field in activity:
            del(activity[field])
        if activity['type'] == 'Create' and field in activity['object']:
            del(activity['object'][field])
    return activity


def _get_actor_id(actor: ObjectOrIDType) -> str:
    """Helper for retrieving an actor `id`."""
    if isinstance(actor, dict):
        return actor['id']
    return actor


class _ActivityMeta(type):
    """Metaclass for keeping track of subclass."""
    def __new__(meta, name, bases, class_dict):
        cls = type.__new__(meta, name, bases, class_dict)

        # Ensure the class has an activity type defined
        if not cls.ACTIVITY_TYPE:
            raise ValueError(f'class {name} has no ACTIVITY_TYPE')

        # Register it
        _REGISTER[cls.ACTIVITY_TYPE] = cls
        return cls


class _BaseActivity(object, metaclass=_ActivityMeta):
    """Base class for ActivityPub activities."""

    ACTIVITY_TYPE: Optional[ActivityType] = None  # the ActivityTypeEnum the class will represent
    OBJECT_REQUIRED = False  # Whether the object field is required or note
    ALLOWED_OBJECT_TYPES: List[ActivityType] = []  # 
    ACTOR_REQUIRED = True  # Most of the object requires an actor, so this flag in on by default

    def __init__(self, **kwargs) -> None:
        if kwargs.get('type') and kwargs.pop('type') != self.ACTIVITY_TYPE.value:
            raise UnexpectedActivityTypeError(f'Expect the type to be {self.ACTIVITY_TYPE.value!r}')

        # Initialize the dict that will contains all the activity fields
        self._data: Dict[str, Any] = {
            'type': self.ACTIVITY_TYPE.value
        }
        logger.debug(f'initializing a {self.ACTIVITY_TYPE.value} activity: {kwargs}')

        # The id may not be present for new activities
        if 'id' in kwargs:
            self._data['id'] = kwargs.pop('id')

        if self.ACTIVITY_TYPE != ActivityType.PERSON and self.ACTOR_REQUIRED:
            actor = kwargs.get('actor')
            if actor:
                kwargs.pop('actor')
                actor = self._validate_person(actor)
                self._data['actor'] = actor
            else:
                raise BadActivityError('missing actor')

        if self.OBJECT_REQUIRED and 'object' in kwargs:
            obj = kwargs.pop('object')
            if isinstance(obj, str):
                # The object is a just a reference the its ID/IRI
                # FIXME(tsileo): fetch the ref
                self._data['object'] = obj
            else:
                if not self.ALLOWED_OBJECT_TYPES:
                    raise UnexpectedActivityTypeError('unexpected object')
                if 'type' not in obj or (self.ACTIVITY_TYPE != ActivityType.CREATE and 'id' not in obj):
                    raise BadActivityError('invalid object, missing type')
                if ActivityType(obj['type']) not in self.ALLOWED_OBJECT_TYPES:
                    raise UnexpectedActivityTypeError(
                        f'unexpected object type {obj["type"]} (allowed={self.ALLOWED_OBJECT_TYPES!r})'
                    )
                self._data['object'] = obj

        if '@context' not in kwargs:
	    self._data['@context'] = CTX_AS
        else:
            self._data['@context'] = kwargs.pop('@context')

        # @context check
        if not isinstance(self._data['@context'], list):
            self._data['@context'] = [self._data['@context']]
        if CTX_SECURITY not in self._data['@context']:
            self._data['@context'].append(CTX_SECURITY)
        if isinstance(self._data['@context'][-1], dict):
            self._data['@context'][-1]['Hashtag'] = 'as:Hashtag'
            self._data['@context'][-1]['sensitive'] = 'as:sensitive'
        else:
            self._data['@context'].append({'Hashtag': 'as:Hashtag', 'sensitive': 'as:sensitive'})

        # FIXME(tsileo): keys required for all subclasses?
        allowed_keys = None
        try:
            allowed_keys = self._init(**kwargs)
            logger.debug('calling custom init')
        except NotImplementedError:
            pass

        if allowed_keys:
            # Allows an extra to (like for Accept and Follow)
            kwargs.pop('to', None)
            if len(set(kwargs.keys()) - set(allowed_keys)) > 0:
                raise BadActivityError(f'extra data left: {kwargs!r}')
        else:
            # Remove keys with `None` value
            valid_kwargs = {}
            for k, v in kwargs.items():
                if v is None:
                    continue
                valid_kwargs[k] = v
            self._data.update(**valid_kwargs)

    def _init(self, **kwargs) -> Optional[List[str]]:
        """Optional init callback that may returns a list of allowed keys."""
        raise NotImplementedError

    def __repr__(self) -> str:
        """Pretty repr."""
        return '{}({!r})'.format(self.__class__.__qualname__, self._data.get('id'))

    def __str__(self) -> str:
        """Returns the ID/IRI when castign to str."""
        return str(self._data.get('id', f'[new {self.ACTIVITY_TYPE} activity]'))

    def __getattr__(self, name: str) -> Any:
        """Allow to access the object field as regular attributes."""
        if self._data.get(name):
            return self._data.get(name)

    def _outbox_set_id(self, uri: str, obj_id: str) -> None:
        """Optional callback for subclasses to so something with a newly generated ID (for outbox activities)."""
        raise NotImplementedError

    def outbox_set_id(self, uri: str, obj_id: str) -> None:
        """Set the ID for a new activity."""
        logger.debug(f'setting ID {uri} / {obj_id}')
        self._data['id'] = uri
        try:
            self._set_id(uri, obj_id)
        except NotImplementedError:
            pass

    def _actor_id(self, obj: ObjectOrIDType) -> str:
        if isinstance(obj, dict) and obj['type'] == ActivityType.PERSON.value:
            obj_id = obj.get('id')
            if not obj_id:
                raise BadActivityError(f'missing object id: {obj!r}')
            return obj_id
        elif isinstance(obj, str):
            return obj
        else:
            raise BadActivityError(f'invalid "actor" field: {obj!r}')

    def _validate_person(self, obj: ObjectOrIDType) -> str:
        obj_id = self._actor_id(obj)
        try:
            actor = OBJECT_FETCHER.fetch(obj_id)
        except Exception:
            raise BadActivityError(f'failed to validate actor {obj!r}')

        if not actor or not 'id' in actor:
            raise BadActivityError(f'invalid actor {actor}')

        return actor['id']

    def get_object(self) -> 'BaseActivity':
        """Returns the object as a BaseActivity instance."""
        if self.__obj:
            return self.__obj
        if isinstance(self._data['object'], dict):
            p = parse_activity(self._data['object'])
        else:
            obj = OBJECT_FETCHER.fetch(self._data['object'])
            if ActivityType(obj.get('type')) not in self.ALLOWED_OBJECT_TYPES:
                raise UnexpectedActivityTypeError(f'invalid object type {obj.get("type")!r}')
                p = parse_activity(obj)

        self.__obj: Optional[BaseActivity] = p
        return p

    def reset_object_cache(self) -> None:
        self.__obj = None

    def to_dict(self, embed: bool = False, embed_object_id_only: bool = False) -> ObjectType:
        """Serializes the activity back to a dict, ready to be JSON serialized."""
        data = dict(self._data)
        if embed:
            for k in ['@context', 'signature']:
                if k in data:
                    del(data[k])
        if data.get('object') and embed_object_id_only and isinstance(data['object'], dict):
            try:
                data['object'] = data['object']['id']
            except KeyError:
                raise BadActivityError(f'embedded object {data["object"]!r} should have an id')

        return data

    def get_actor(self) -> 'BaseActivity':
        # FIXME(tsileo): cache the actor (same way as get_object)
        actor = self._data.get('actor')
        if not actor and self.ACTOR_REQUIRED:
            # Quick hack for Note objects
            if self.ACTIVITY_TYPE == ActivityType.NOTE:
                actor = str(self._data.get('attributedTo'))
            else:
                raise BadActivityError(f'failed to fetch actor: {self._data!r}')

        actor_id = self._actor_id(actor)
        return Person(**OBJECT_FETCHER.fetch(actor_id))

    def _pre_post_to_outbox(self) -> None:
        raise NotImplementedError

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]) -> None:
        raise NotImplementedError

    def _undo_outbox(self) -> None:
        raise NotImplementedError

    def _pre_process_from_inbox(self) -> None:
        raise NotImplementedError

    def _process_from_inbox(self) -> None:
        raise NotImplementedError

    def _undo_inbox(self) -> None:
        raise NotImplementedError

    # FIXME(tsileo): delete these?
    def _undo_should_purge_cache(self) -> bool:
        raise NotImplementedError

    def _should_purge_cache(self) -> bool:
        raise NotImplementedError

    def process_from_inbox(self) -> None:
        logger.debug(f'calling main process from inbox hook for {self}')
        actor = self.get_actor()

        # Check for Block activity
        # ABC
        if self.outbox_is_blocked(actor.id):
            # TODO(tsileo): raise ActorBlockedError
            logger.info(f'actor {actor!r} is blocked, dropping the received activity {self!r}')
            return

        # ABC
        if self.inbox_get_by_id(self.id):
            # The activity is already in the inbox
            logger.info(f'received duplicate activity {self}, dropping it')
            return

        try:
            self._pre_process_from_inbox()
            logger.debug('called pre process from inbox hook')
        except NotImplementedError:
            logger.debug('pre process from inbox hook not implemented')

        # ABC
        self.inbox_create(self)
        logger.info('activity {self!r} saved')

        try:
            self._process_from_inbox()
            logger.debug('called process from inbox hook')
        except NotImplementedError:
            logger.debug('process from inbox hook not implemented')

    def post_to_outbox(self) -> None:
        logger.debug(f'calling main post to outbox hook for {self}')

        # Assign create a random ID
        obj_id = random_object_id()
        self.set_id(f'{ID}/outbox/{obj_id}', obj_id)

        try:
            self._pre_post_to_outbox()
            logger.debug(f'called pre post to outbox hook')
        except NotImplementedError:
            logger.debug('pre post to outbox hook not implemented')

        # ABC
        self.outbox_create(self)

        recipients = self.recipients()
        logger.info(f'recipients={recipients}')
        activity = clean_activity(activity)

        try:
            self._post_to_outbox(obj_id, activity, recipients)
            logger.debug(f'called post to outbox hook')
        except NotImplementedError:
            logger.debug('post to outbox hook not implemented')

        payload = json.dumps(activity)
        for recp in recipients:
            logger.debug(f'posting to {recp}')

            # ABC
            self.post_to_remote_inbox(payload, recp)

    def _post_to_inbox(self, payload: str, to: str):
        tasks.post_to_inbox.delay(payload, to)

    def _recipients(self) -> List[str]:
        return []

    def recipients(self) -> List[str]:
        recipients = self._recipients()
        actor_id = self.get_actor().id

        out: List[str] = []
        for recipient in recipients:
            # if recipient in PUBLIC_INSTANCES:
            #    if recipient not in out:
            #        out.append(str(recipient))
            #    continue
            if recipient in [actor_id, AS_PUBLIC, None]:
                continue
            if isinstance(recipient, Person):
                if recipient.id == actor_id:
                    continue
                actor = recipient
            else:
                raw_actor = OBJECT_FETCHER.fetch(recipient)
                if raw_actor['type'] == ActiivtyType.PERSON.name:
                    actor = Person(**raw_actor)

                    if actor.endpoints:
                        shared_inbox = actor.endpoints.get('sharedInbox')
                        if shared_inbox not in out:
                            out.append(shared_inbox)
                            continue

                    if actor.inbox and actor.inbox not in out:
                        out.append(actor.inbox)

                # Is the activity a `Collection`/`OrderedCollection`?
                elif raw_actor['type'] in [ActivityType.COLLECTION.value,
                                             ActivityType.ORDERED_COLLECTION.value]:
                        for item in parse_collection(raw_actor):
                            if item in [ME, AS_PUBLIC]:
                                continue
                            try:
                                col_actor = Person(**OBJECT_FETCHER.fetch(item))
                            except NotAnActorError:
                                pass

                            if col_actor.endpoints:
                                shared_inbox = col_actor.endpoints.get('sharedInbox')
                                if shared_inbox not in out:
                                    out.append(shared_inbox)
                                    continue
                            if col_actor.inbox and col_actor.inbox not in out:
                                out.append(col_actor.inbox)
                else:
                    raise BadActivityError(f'failed to parse {raw_actor!r}')

        return out

    def build_undo(self) -> 'BaseActivity':
        raise NotImplementedError

    def build_delete(self) -> 'BaseActivity':
        raise NotImplementedError


class Person(BaseActivity):
    ACTIVITY_TYPE = ActivityType.PERSON
    OBJECT_REQUIRED = False
    ACTOR_REQUIRED = False


class Block(BaseActivity):
    ACTIVITY_TYPE = ActivityType.BLOCK
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True


class Collection(BaseActivity):
    ACTIVITY_TYPE = ActivityType.COLLECTION
    OBJECT_REQUIRED = False
    ACTOR_REQUIRED = False


class Image(BaseActivity):
    ACTIVITY_TYPE = ActivityType.IMAGE
    OBJECT_REQUIRED = False
    ACTOR_REQUIRED = False

    def _init(self, **kwargs):
        self._data.update(
            url=kwargs.pop('url'),
        )

    def __repr__(self):
        return 'Image({!r})'.format(self._data.get('url'))


class Follow(BaseActivity):
    ACTIVITY_TYPE = ActivityType.FOLLOW
    ALLOWED_OBJECT_TYPES = [ActivityType.PERSON]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True


    def _build_reply(self, reply_type: ActivityType) -> BaseActivity:
        if reply_type == ActivityType.ACCEPT:
            return Accept(
                object=self.to_dict(embed=True),
            )

        raise ValueError(f'type {reply_type} is invalid for building a reply')

    def _recipients(self) -> List[str]:
        return [self.get_object().id]

    def _process_from_inbox(self) -> None:
        """Receiving a Follow should trigger an Accept."""
        accept = self.build_accept()
        accept.post_to_outbox()

        remote_actor = self.get_actor().id

        # ABC
        self.new_follower(remote_actor)

    def _undo_inbox(self) -> None:
        # ABC
        self.undo_new_follower(self.get_actor().id)

    def _undo_outbox(self) -> None:
        # ABC
        self.undo_new_following(self.get_object().id)

    def build_accept(self) -> BaseActivity:
        return self._build_reply(ActivityType.ACCEPT)

    def build_undo(self) -> BaseActivity:
        return Undo(object=self.to_dict(embed=True))


class Accept(BaseActivity):
    ACTIVITY_TYPE = ActivityType.ACCEPT
    ALLOWED_OBJECT_TYPES = [ActivityType.FOLLOW]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True

    def _recipients(self) -> List[str]:
        return [self.get_object().get_actor().id]

    def _pre_process_from_inbox(self) -> None:
        # FIXME(tsileo): ensure the actor match the object actor 

    def _process_from_inbox(self) -> None:
        # ABC
        self.new_following(self.get_actor().id)


class Undo(BaseActivity):
    ACTIVITY_TYPE = ActivityType.UNDO
    ALLOWED_OBJECT_TYPES = [ActivityType.FOLLOW, ActivityType.LIKE, ActivityType.ANNOUNCE]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True

    def _recipients(self) -> List[str]:
        obj = self.get_object()
        if obj.type_enum == ActivityType.FOLLOW:
            return [obj.get_object().id]
        else:
            return [obj.get_object().get_actor().id]
            # TODO(tsileo): handle like and announce
            raise Exception('TODO')

    def _pre_process_from_inbox(self) -> None:
        """Ensures an Undo activity comes from the same actor as the updated activity."""
        obj = self.get_object()
        actor = self.get_actor()
        if actor.id != obj.get_actor().id:
            raise BadActivityError(f'{actor!r} cannot update {obj!r}')

    def _process_from_inbox(self) -> None:
        obj = self.get_object()
        # FIXME(tsileo): move this to _undo_inbox impl
        # DB.inbox.update_one({'remote_id': obj.id}, {'$set': {'meta.undo': True}})

        try:
            obj._undo_inbox()
        except NotImplementedError:
            pass

    def _pre_post_to_outbox(self) -> None:
        """Ensures an Undo activity references an activity owned by the instance."""
        obj = self.get_object()
        if not obj.id.startswith(ID):
            raise NotFromOutboxError(f'object {obj["id"]} is not owned by this instance')

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]) -> None:
        logger.debug('processing undo to outbox')
        logger.debug('self={}'.format(self))
        obj = self.get_object()
        logger.debug('obj={}'.format(obj))

        # FIXME(tsileo): move this to _undo_inbox impl
        # DB.outbox.update_one({'remote_id': obj.id}, {'$set': {'meta.undo': True}})

        try:
            obj._undo_outbox()
            logger.debug(f'_undo_outbox called for {obj}')
        except NotImplementedError:
            logger.debug(f'_undo_outbox not implemented for {obj}')
            pass


class Like(BaseActivity):
    ACTIVITY_TYPE = ActivityType.LIKE
    ALLOWED_OBJECT_TYPES = [ActivityType.NOTE]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True

    def _recipients(self) -> List[str]:
        return [self.get_object().get_actor().id]

    def _process_from_inbox(self):
        obj = self.get_object()
        # Update the meta counter if the object is published by the server
        # FIXME(tsileo): continue here
        DB.outbox.update_one({'activity.object.id': obj.id}, {
            '$inc': {'meta.count_like': 1},
        })
        # XXX(tsileo): notification??

    def _undo_inbox(self) -> None:
        obj = self.get_object()
        # Update the meta counter if the object is published by the server
        DB.outbox.update_one({'activity.object.id': obj.id}, {
            '$inc': {'meta.count_like': -1},
        })

    def _undo_should_purge_cache(self) -> bool:
        # If a like coutn was decremented, we need to purge the application cache
        return self.get_object().id.startswith(BASE_URL)

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]):
        obj = self.get_object()
        # Unlikely, but an actor can like it's own post
        DB.outbox.update_one({'activity.object.id': obj.id}, {
            '$inc': {'meta.count_like': 1},
        })

        # Keep track of the like we just performed
        DB.inbox.update_one({'activity.object.id': obj.id}, {'$set': {'meta.liked': obj_id}})

    def _undo_outbox(self) -> None:
        obj = self.get_object()
        # Unlikely, but an actor can like it's own post
        DB.outbox.update_one({'activity.object.id': obj.id}, {
            '$inc': {'meta.count_like': -1},
        })

        DB.inbox.update_one({'activity.object.id': obj.id}, {'$set': {'meta.liked': False}})

    def build_undo(self) -> BaseActivity:
        return Undo(object=self.to_dict(embed=True, embed_object_id_only=True))


class Announce(BaseActivity):
    ACTIVITY_TYPE = ActivityType.ANNOUNCE
    ALLOWED_OBJECT_TYPES = [ActivityType.NOTE]

    def _recipients(self) -> List[str]:
        recipients = []

        for field in ['to', 'cc']:
            if field in self._data:
                recipients.extend(_to_list(self._data[field]))

        return recipients

    def _process_from_inbox(self) -> None:
        if isinstance(self._data['object'], str) and not self._data['object'].startswith('http'):
            # TODO(tsileo): actually drop it without storing it and better logging, also move the check somewhere else
            logger.warn(
                f'received an Annouce referencing an OStatus notice ({self._data["object"]}), dropping the message'
            )
            return
        # Save/cache the object, and make it part of the stream so we can fetch it
        if isinstance(self._data['object'], str):
            raw_obj = OBJECT_SERVICE.get(
                self._data['object'],
                reload_cache=True,
                part_of_stream=True,
                announce_published=self._data['published'],
            )
            obj = parse_activity(raw_obj)
        else:
            obj = self.get_object()

        DB.outbox.update_one({'activity.object.id': obj.id}, {
            '$inc': {'meta.count_boost': 1},
        })

    def _undo_inbox(self) -> None:
        obj = self.get_object()
        # Update the meta counter if the object is published by the server
        DB.outbox.update_one({'activity.object.id': obj.id}, {
            '$inc': {'meta.count_boost': -1},
        })

    def _undo_should_purge_cache(self) -> bool:
        # If a like coutn was decremented, we need to purge the application cache
        return self.get_object().id.startswith(BASE_URL)

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]) -> None:
        if isinstance(self._data['object'], str):
            # Put the object in the cache
            OBJECT_SERVICE.get(
                self._data['object'],
                reload_cache=True,
                part_of_stream=True,
                announce_published=self._data['published'],
            )

        obj = self.get_object()
        DB.inbox.update_one({'activity.object.id': obj.id}, {'$set': {'meta.boosted': obj_id}})

    def _undo_outbox(self) -> None:
        obj = self.get_object()
        DB.inbox.update_one({'activity.object.id': obj.id}, {'$set': {'meta.boosted': False}})

    def build_undo(self) -> BaseActivity:
        return Undo(object=self.to_dict(embed=True))


class Delete(BaseActivity):
    ACTIVITY_TYPE = ActivityType.DELETE
    ALLOWED_OBJECT_TYPES = [ActivityType.NOTE, ActivityType.TOMBSTONE]
    OBJECT_REQUIRED = True

    def _get_actual_object(self) -> BaseActivity:
        obj = self.get_object()
        if obj.type_enum == ActivityType.TOMBSTONE:
            obj = parse_activity(OBJECT_SERVICE.get(obj.id))
        return obj

    def _recipients(self) -> List[str]:
        obj = self._get_actual_object()
        return obj._recipients()

    def _pre_process_from_inbox(self) -> None:
        """Ensures a Delete activity comes from the same actor as the deleted activity."""
        obj = self._get_actual_object()
        actor = self.get_actor()
        if actor.id != obj.get_actor().id:
            raise BadActivityError(f'{actor!r} cannot delete {obj!r}')

    def _process_from_inbox(self) -> None:
        DB.inbox.update_one({'activity.object.id': self.get_object().id}, {'$set': {'meta.deleted': True}})
        obj = self._get_actual_object()
        if obj.type_enum == ActivityType.NOTE:
            obj._delete_from_threads()

        # TODO(tsileo): also purge the cache if it's a reply of a published activity

    def _pre_post_to_outbox(self) -> None:
        """Ensures the Delete activity references a activity from the outbox (i.e. owned by the instance)."""
        obj = self._get_actual_object()
        if not obj.id.startswith(ID):
            raise NotFromOutboxError(f'object {obj["id"]} is not owned by this instance')

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]) -> None:
        DB.outbox.update_one({'activity.object.id': self.get_object().id}, {'$set': {'meta.deleted': True}})


class Update(BaseActivity):
    ACTIVITY_TYPE = ActivityType.UPDATE
    ALLOWED_OBJECT_TYPES = [ActivityType.NOTE, ActivityType.PERSON]
    OBJECT_REQUIRED = True

    def _pre_process_from_inbox(self) -> None:
        """Ensures an Update activity comes from the same actor as the updated activity."""
        obj = self.get_object()
        actor = self.get_actor()
        if actor.id != obj.get_actor().id:
            raise BadActivityError(f'{actor!r} cannot update {obj!r}')

    def _process_from_inbox(self):
        obj = self.get_object()
        if obj.type_enum == ActivityType.NOTE:
            DB.inbox.update_one({'activity.object.id': obj.id}, {'$set': {'activity.object': obj.to_dict()}})
            return

        # If the object is a Person, it means the profile was updated, we just refresh our local cache
        ACTOR_SERVICE.get(obj.id, reload_cache=True)

        # TODO(tsileo): implements _should_purge_cache if it's a reply of a published activity (i.e. in the outbox)

    def _pre_post_to_outbox(self) -> None:
        obj = self.get_object()
        if not obj.id.startswith(ID):
            raise NotFromOutboxError(f'object {obj["id"]} is not owned by this instance')

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]) -> None:
        obj = self._data['object']

        update_prefix = 'activity.object.'
        update: Dict[str, Any] = {'$set': dict(), '$unset': dict()}
        update['$set'][f'{update_prefix}updated'] = datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
        for k, v in obj.items():
            if k in ['id', 'type']:
                continue
            if v is None:
                update['$unset'][f'{update_prefix}{k}'] = ''
            else:
                update['$set'][f'{update_prefix}{k}'] = v

        if len(update['$unset']) == 0:
            del(update['$unset'])

        print(f'updating note from outbox {obj!r} {update}')
        logger.info(f'updating note from outbox {obj!r} {update}')
        DB.outbox.update_one({'activity.object.id': obj['id']}, update)
        # FIXME(tsileo): should send an Update (but not a partial one, to all the note's recipients
        # (create a new Update with the result of the update, and send it without saving it?)


class Create(BaseActivity):
    ACTIVITY_TYPE = ActivityType.CREATE
    ALLOWED_OBJECT_TYPES = [ActivityType.NOTE]
    OBJECT_REQUIRED = True

    def _set_id(self, uri: str, obj_id: str) -> None:
        self._data['object']['id'] = uri + '/activity'
        self._data['object']['url'] = ID + '/' + self.get_object().type.lower() + '/' + obj_id
        self.reset_object_cache()

    def _init(self, **kwargs):
        obj = self.get_object()
        if not obj.attributedTo:
            self._data['object']['attributedTo'] = self.get_actor().id
        if not obj.published:
            if self.published:
                self._data['object']['published'] = self.published
            else:
                now = datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
                self._data['published'] = now
                self._data['object']['published'] = now

    def _recipients(self) -> List[str]:
        # TODO(tsileo): audience support?
        recipients = []
        for field in ['to', 'cc', 'bto', 'bcc']:
            if field in self._data:
                recipients.extend(_to_list(self._data[field]))

        recipients.extend(self.get_object()._recipients())

        return recipients

    def _update_threads(self) -> None:
        logger.debug('_update_threads hook')
        obj = self.get_object()

        # TODO(tsileo): re-enable me
        # tasks.fetch_og.delay('INBOX', self.id)

        threads = []
        reply = obj.get_local_reply()
        print(f'initial_reply={reply}')
        print(f'{obj}')
        logger.debug(f'initial_reply={reply}')
        reply_id = None
        direct_reply = 1
        while reply is not None:
            if not DB.inbox.find_one_and_update({'activity.object.id': reply.id}, {
                '$inc': {
                    'meta.count_reply': 1,
                    'meta.count_direct_reply': direct_reply,
                },
                '$addToSet': {'meta.thread_children': obj.id},
            }):
                DB.outbox.update_one({'activity.object.id': reply.id}, {
                    '$inc': {
                        'meta.count_reply': 1,
                        'meta.count_direct_reply': direct_reply,
                    },
                    '$addToSet': {'meta.thread_children': obj.id},
                })

            direct_reply = 0
            reply_id = reply.id
            reply = reply.get_local_reply()
            logger.debug(f'next_reply={reply}')
            threads.append(reply_id)
        # FIXME(tsileo): obj.id is None!!
        print(f'reply_id={reply_id} {obj.id} {obj._data} {self.id}')

        if reply_id:
            if not DB.inbox.find_one_and_update({'activity.object.id': obj.id}, {
                '$set': {
                    'meta.thread_parents': threads,
                    'meta.thread_root_parent': reply_id,
                },
            }):
                DB.outbox.update_one({'activity.object.id': obj.id}, {
                    '$set': {
                        'meta.thread_parents': threads,
                        'meta.thread_root_parent': reply_id,
                    },
                })
        logger.debug('_update_threads done')

    def _process_from_inbox(self) -> None:
        self._update_threads()

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]) -> None:
        self._update_threads()

    def _should_purge_cache(self) -> bool:
        # TODO(tsileo): handle reply of a reply...
        obj = self.get_object()
        in_reply_to = obj.inReplyTo
        if in_reply_to:
            local_activity = DB.outbox.find_one({'activity.type': 'Create', 'activity.object.id': in_reply_to})
            if local_activity:
                return True

        return False


class Tombstone(BaseActivity):
    ACTIVITY_TYPE = ActivityType.TOMBSTONE


class Note(BaseActivity):
    ACTIVITY_TYPE = ActivityType.NOTE

    def _init(self, **kwargs):
        print(self._data)
        # Remove the `actor` field as `attributedTo` is used for `Note` instead
        if 'actor' in self._data:
            del(self._data['actor'])
        if 'sensitive' not in kwargs:
            self._data['sensitive'] = False

    def _recipients(self) -> List[str]:
        # TODO(tsileo): audience support?
        recipients: List[str] = []

        # If the note is public, we publish it to the defined "public instances"
        if AS_PUBLIC in self._data.get('to', []):
            recipients.extend(PUBLIC_INSTANCES)
            print('publishing to public instances')
            print(recipients)

        for field in ['to', 'cc', 'bto', 'bcc']:
            if field in self._data:
                recipients.extend(_to_list(self._data[field]))

        return recipients

    def _delete_from_threads(self) -> None:
        logger.debug('_delete_from_threads hook')

        reply = self.get_local_reply()
        logger.debug(f'initial_reply={reply}')
        direct_reply = -1
        while reply is not None:
            if not DB.inbox.find_one_and_update({'activity.object.id': reply.id}, {
                '$inc': {
                    'meta.count_reply': -1,
                    'meta.count_direct_reply': direct_reply,
                },
                '$pull': {'meta.thread_children': self.id},

            }):
                DB.outbox.update_one({'activity.object.id': reply.id}, {
                    '$inc': {
                        'meta.count_reply': 1,
                        'meta.count_direct_reply': direct_reply,
                    },
                    '$pull': {'meta.thread_children': self.id},
                })

            direct_reply = 0
            reply = reply.get_local_reply()
            logger.debug(f'next_reply={reply}')

        logger.debug('_delete_from_threads done')
        return None

    def get_local_reply(self) -> Optional[BaseActivity]:
        "Find the note reply if any."""
        in_reply_to = self.inReplyTo
        if not in_reply_to:
            # This is the root comment
            return None

        inbox_parent = DB.inbox.find_one({'activity.type': 'Create', 'activity.object.id': in_reply_to})
        if inbox_parent:
            return parse_activity(inbox_parent['activity']['object'])

        outbox_parent = DB.outbox.find_one({'activity.type': 'Create', 'activity.object.id': in_reply_to})
        if outbox_parent:
            return parse_activity(outbox_parent['activity']['object'])

        # The parent is no stored on this instance
        return None

    def build_create(self) -> BaseActivity:
        """Wraps an activity in a Create activity."""
        create_payload = {
            'object': self.to_dict(embed=True),
            'actor': self.attributedTo or ME,
        }
        for field in ['published', 'to', 'bto', 'cc', 'bcc', 'audience']:
            if field in self._data:
                create_payload[field] = self._data[field]

        return Create(**create_payload)

    def build_like(self) -> BaseActivity:
        return Like(object=self.id)

    def build_announce(self) -> BaseActivity:
        return Announce(
            object=self.id,
            to=[AS_PUBLIC],
            cc=[ID+'/followers', self.attributedTo],
            published=datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
        )

    def build_delete(self) -> BaseActivity:
        return Delete(object=Tombstone(id=self.id).to_dict(embed=True))

    def get_tombstone(self, deleted: Optional[str]) -> BaseActivity:
        return Tombstone(
            id=self.id,
            published=self.published,
            deleted=deleted,
            updated=deleted,
        )
