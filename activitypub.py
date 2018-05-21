import json
import binascii
import os
from datetime import datetime
from enum import Enum

import requests
from bson.objectid import ObjectId
from html2text import html2text
from feedgen.feed import FeedGenerator

from utils.linked_data_sig import generate_signature
from utils.actor_service import NotAnActorError
from config import USERNAME, BASE_URL, ID
from config import CTX_AS, CTX_SECURITY, AS_PUBLIC
from config import KEY, DB, ME, ACTOR_SERVICE
from config import OBJECT_SERVICE
from config import PUBLIC_INSTANCES
import tasks

from typing import List, Optional, Dict, Any, Union
from typing import TypeVar

A = TypeVar('A', bound='BaseActivity')
ObjectType = Dict[str, Any]
ObjectOrIDType = Union[str, ObjectType]


class ActivityTypes(Enum):
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


def random_object_id() -> str:
    return binascii.hexlify(os.urandom(8)).decode('utf-8')


def _remove_id(doc: ObjectType) -> ObjectType:
    doc = doc.copy()
    if '_id' in doc:
        del(doc['_id'])
    return doc


def _to_list(data: Union[List[Any], Any]) -> List[Any]:
    if isinstance(data, list):
        return data
    return [data]


def clean_activity(activity: ObjectType) -> Dict[str, Any]:
    # Remove the hidden bco and bcc field
    for field in ['bto', 'bcc']:
        if field in activity:
            del(activity[field])
        if activity['type'] == 'Create' and field in activity['object']:
            del(activity['object'][field])
    return activity


def _get_actor_id(actor: ObjectOrIDType) -> str:
    if isinstance(actor, dict):
        return actor['id']
    return actor


class BaseActivity(object):
    ACTIVITY_TYPE: Optional[ActivityTypes] = None
    ALLOWED_OBJECT_TYPES: List[ActivityTypes] = []

    def __init__(self, **kwargs) -> None:
        if not self.ACTIVITY_TYPE:
            raise ValueError('Missing ACTIVITY_TYPE')

        if kwargs.get('type') is not None and kwargs.pop('type') != self.ACTIVITY_TYPE.value:
            raise ValueError('Expect the type to be {}'.format(self.ACTIVITY_TYPE))

        self._data: Dict[str, Any] = {'type': self.ACTIVITY_TYPE.value}

        if 'id' in kwargs:
            self._data['id'] = kwargs.pop('id')

        if self.ACTIVITY_TYPE != ActivityTypes.PERSON:
            actor = kwargs.get('actor')
            if actor:
                kwargs.pop('actor')
                actor = self._validate_person(actor)
                self._data['actor'] = actor
            else:
                if not self.NO_CONTEXT:
                    actor = ID
                    self._data['actor'] = actor

        if 'object' in kwargs:
            obj = kwargs.pop('object')
            if isinstance(obj, str):
                self._data['object'] = obj
            else:
                if not self.ALLOWED_OBJECT_TYPES:
                    raise ValueError('unexpected object')
                if 'type' not in obj or (self.ACTIVITY_TYPE != ActivityTypes.CREATE and 'id' not in obj):
                    raise ValueError('invalid object')
                if ActivityTypes(obj['type']) not in self.ALLOWED_OBJECT_TYPES:
                    print(self, kwargs)
                    raise ValueError(f'unexpected object type {obj["type"]} (allowed={self.ALLOWED_OBJECT_TYPES})')
                self._data['object'] = obj

        if '@context' not in kwargs:
            if not self.NO_CONTEXT:
                self._data['@context'] = CTX_AS
        else:
            self._data['@context'] = kwargs.pop('@context')

        # @context check
        if not self.NO_CONTEXT:
            if not isinstance(self._data['@context'], list):
                self._data['@context'] = [self._data['@context']]
            if CTX_SECURITY not in self._data['@context']:
                self._data['@context'].append(CTX_SECURITY)
            if isinstance(self._data['@context'][-1], dict):
                self._data['@context'][-1]['Hashtag'] = 'as:Hashtag'
                self._data['@context'][-1]['sensitive'] = 'as:sensitive'
            else:
                self._data['@context'].append({'Hashtag': 'as:Hashtag', 'sensitive': 'as:sensitive'})

        allowed_keys = None
        try:
            allowed_keys = self._init(**kwargs)
        except NotImplementedError:
            pass

        if allowed_keys:
            # Allows an extra to (like for Accept and Follow)
            kwargs.pop('to', None)
            if len(set(kwargs.keys()) - set(allowed_keys)) > 0:
                raise ValueError('extra data left: {}'.format(kwargs))
        else:
            # Remove keys with `None` value
            valid_kwargs = {}
            for k, v in kwargs.items():
                if v is None:
                    break
                valid_kwargs[k] = v
            self._data.update(**valid_kwargs)

    def _init(self, **kwargs) -> Optional[List[str]]:
        raise NotImplementedError

    def _verify(self) -> None:
        raise NotImplementedError

    def verify(self) -> None:
        try:
            self._verify()
        except NotImplementedError:
            pass

    def __repr__(self) -> str:
        return '{}({!r})'.format(self.__class__.__qualname__, self._data.get('id'))

    def __str__(self) -> str:
        return str(self._data['id'])

    def __getattr__(self, name: str) -> Any:
        if self._data.get(name):
            return self._data.get(name)

    @property
    def type_enum(self) -> ActivityTypes:
        return ActivityTypes(self.type)

    def _set_id(self, uri: str, obj_id: str) -> None:
        raise NotImplementedError

    def set_id(self, uri: str, obj_id: str) -> None:
        self._data['id'] = uri
        try:
            self._set_id(uri, obj_id)
        except NotImplementedError:
            pass

    def _actor_id(self, obj: ObjectOrIDType) -> str:
        if isinstance(obj, dict) and obj['type'] == ActivityTypes.PERSON.value:
            obj_id = obj.get('id')
            if not obj_id:
                raise ValueError('missing object id')
            return obj_id
        else:
            return str(obj)

    def _validate_person(self, obj: ObjectOrIDType) -> str:
        obj_id = self._actor_id(obj)
        try:
            actor = ACTOR_SERVICE.get(obj_id)
        except Exception:
            return obj_id  # FIXME(tsileo): handle this
        if not actor:
            raise ValueError('Invalid actor')
        return actor['id']

    def get_object(self) -> 'BaseActivity':
        if self.__obj:
            return self.__obj
        if isinstance(self._data['object'], dict):
            p = parse_activity(self._data['object'])
        else:
            if self.ACTIVITY_TYPE == ActivityTypes.FOLLOW:
                p = Person(**ACTOR_SERVICE.get(self._data['object']))
            else:
                obj = OBJECT_SERVICE.get(self._data['object'])
                if ActivityTypes(obj.get('type')) not in self.ALLOWED_OBJECT_TYPES:
                    raise ValueError('invalid object type')

                p = parse_activity(obj)

        self.__obj: BaseActivity = p
        return p

    def _to_dict(self, data: ObjectType) -> ObjectType:
        return data

    def to_dict(self, embed: bool = False) -> ObjectType:
        data = dict(self._data)
        if embed:
            for k in ['@context', 'signature']:
                if k in data:
                    del(data[k])
        return self._to_dict(data)

    def get_actor(self) -> 'BaseActivity':
        actor = self._data.get('actor')
        if not actor:
            if self.type_enum == ActivityTypes.NOTE:
                actor = str(self._data.get('attributedTo'))
            else:
                raise ValueError('failed to fetch actor')

        actor_id = self._actor_id(actor)
        return Person(**ACTOR_SERVICE.get(actor_id))

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]) -> None:
        raise NotImplementedError

    def _undo_outbox(self) -> None:
        raise NotImplementedError

    def _process_from_inbox(self) -> None:
        raise NotImplementedError

    def _undo_inbox(self) -> None:
        raise NotImplementedError

    def _should_purge_cache(self) -> bool:
        raise NotImplementedError

    def process_from_inbox(self) -> None:
        self.verify()
        actor = self.get_actor()

        if DB.outbox.find_one({'type': ActivityTypes.BLOCK.value,
                               'activity.object': actor.id,
                               'meta.undo': False}):
            print('actor is blocked, drop activity')
            return

        if DB.inbox.find_one({'remote_id': self.id}):
            # The activity is already in the inbox
            print('received duplicate activity')
            return

        activity = self.to_dict()
        DB.inbox.insert_one({
            'activity': activity,
            'type': self.type,
            'remote_id': self.id,
            'meta': {'undo': False, 'deleted': False},
        })

        try:
            self._process_from_inbox()
        except NotImplementedError:
            pass

    def post_to_outbox(self) -> None:
        obj_id = random_object_id()
        self.set_id(f'{ID}/outbox/{obj_id}', obj_id)
        self.verify()
        activity = self.to_dict()
        DB.outbox.insert_one({
            'id': obj_id,
            'activity': activity,
            'type': self.type,
            'remote_id': self.id,
            'meta': {'undo': False, 'deleted': False},
        })

        recipients = self.recipients()
        activity = clean_activity(activity)

        try:
            self._post_to_outbox(obj_id, activity, recipients)
        except NotImplementedError:
            pass

        generate_signature(activity, KEY.privkey)
        payload = json.dumps(activity)
        print('will post')
        for recp in recipients:
            self._post_to_inbox(payload, recp)
        print('done')

    def _post_to_inbox(self, payload: str, to: str):
        tasks.post_to_inbox.delay(payload, to)

    def _recipients(self) -> List[str]:
        return []

    def recipients(self) -> List[str]:
        recipients = self._recipients()

        out: List[str] = [] 
        for recipient in recipients:
            if recipient in PUBLIC_INSTANCES:
                if recipient not in out:
                    out.append(str(recipient))
                continue
            if recipient in [ME, AS_PUBLIC, None]:
                continue
            if isinstance(recipient, Person):
                if recipient.id == ME:
                    continue
                actor = recipient
            else:
                try:
                    actor = Person(**ACTOR_SERVICE.get(recipient))
                except NotAnActorError as error:
                    # Is the activity a `Collection`/`OrderedCollection`?
                    if error.activity and error.activity['type'] in [ActivityTypes.COLLECTION.value,
                                                                     ActivityTypes.ORDERED_COLLECTION.value]:
                        for item in parse_collection(error.activity):
                            if item in [ME, AS_PUBLIC]:
                                continue
                            try:
                                col_actor = Person(**ACTOR_SERVICE.get(item))
                            except NotAnActorError:
                                pass

                            if col_actor.endpoints:
                                shared_inbox = col_actor.endpoints.get('sharedInbox')
                                if shared_inbox not in out:
                                    out.append(shared_inbox)
                                    continue
                                if col_actor.inbox and col_actor.inbox not in out:
                                    out.append(col_actor.inbox)

                        continue

            if actor.endpoints:
                shared_inbox = actor.endpoints.get('sharedInbox')
                if shared_inbox not in out:
                    out.append(shared_inbox)
                    continue

            if actor.inbox and actor.inbox not in out:
                out.append(actor.inbox)

        return out

    def build_undo(self) -> 'BaseActivity':
        raise NotImplementedError


class Person(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.PERSON

    def _init(self, **kwargs):
        # if 'icon' in kwargs:
        #     self._data['icon'] = Image(**kwargs.pop('icon'))
        pass

    def _verify(self) -> None:
        ACTOR_SERVICE.get(self._data['id'])

    def _to_dict(self, data):
        # if 'icon' in data:
        #     data['icon'] = data['icon'].to_dict()
        return data


class Block(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.BLOCK


class Collection(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.COLLECTION


class Image(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.IMAGE
    NO_CONTEXT = True

    def _init(self, **kwargs):
        self._data.update(
            url=kwargs.pop('url'),
        )

    def __repr__(self):
        return 'Image({!r})'.format(self._data.get('url'))


class Follow(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.FOLLOW
    ALLOWED_OBJECT_TYPES = [ActivityTypes.PERSON]

    def _build_reply(self, reply_type: ActivityTypes) -> BaseActivity:
        if reply_type == ActivityTypes.ACCEPT:
            return Accept(
                object=self.to_dict(embed=True),
            )

        raise ValueError(f'type {reply_type} is invalid for building a reply')

    def _recipients(self) -> List[str]:
        return [self.get_object().id]

    def _process_from_inbox(self) -> None:
        accept = self.build_accept()
        accept.post_to_outbox()

        remote_actor = self.get_actor().id

        if DB.followers.find({'remote_actor': remote_actor}).count() == 0:
            DB.followers.insert_one({'remote_actor': remote_actor})

    def _undo_inbox(self) -> None:
        DB.followers.delete_one({'remote_actor': self.get_actor().id})

    def build_accept(self) -> BaseActivity:
        return self._build_reply(ActivityTypes.ACCEPT)

    def build_undo(self) -> BaseActivity:
        return Undo(object=self.to_dict(embed=True))

    def _should_purge_cache(self) -> bool:
        # Receiving a follow activity in the inbox should reset the application cache
        return True


class Accept(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.ACCEPT
    ALLOWED_OBJECT_TYPES = [ActivityTypes.FOLLOW]

    def _recipients(self) -> List[str]:
        return [self.get_object().get_actor().id]

    def _process_from_inbox(self) -> None:
        remote_actor = self.get_actor().id
        if DB.following.find({'remote_actor': remote_actor}).count() == 0:
            DB.following.insert_one({'remote_actor': remote_actor})

    def _should_purge_cache(self) -> bool:
        # Receiving an accept activity in the inbox should reset the application cache
        # (a follow request has been accepted)
        return True



class Undo(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.UNDO
    ALLOWED_OBJECT_TYPES = [ActivityTypes.FOLLOW, ActivityTypes.LIKE, ActivityTypes.ANNOUNCE]

    def _recipients(self) -> List[str]:
        obj = self.get_object()
        if obj.type_enum == ActivityTypes.FOLLOW:
            return [obj.get_object().id]
        else:
            return [obj.get_object().get_actor().id]
            # TODO(tsileo): handle like and announce
            raise Exception('TODO')

    def _process_from_inbox(self) -> None:
        obj = self.get_object()
        DB.inbox.update_one({'remote_id': obj.id}, {'$set': {'meta.undo': True}})

        try:
            obj._undo_inbox()
        except NotImplementedError:
            pass

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]) -> None:
        obj = self.get_object()
        DB.outbox.update_one({'remote_id': obj.id}, {'$set': {'meta.undo': True}})

        try:
            obj._undo_outbox()
        except NotImplementedError:
            pass


class Like(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.LIKE
    ALLOWED_OBJECT_TYPES = [ActivityTypes.NOTE]

    def _recipients(self) -> List[str]:
        return [self.get_object().get_actor().id]

    def _process_from_inbox(self):
        obj = self.get_object()
        # Update the meta counter if the object is published by the server
        DB.outbox.update_one({'activity.object.id': obj.id}, {'$inc': {'meta.count_like': 1}})

    def _undo_inbox(self) -> None:
        obj = self.get_object()
        # Update the meta counter if the object is published by the server
        DB.outbox.update_one({'activity.object.id': obj.id}, {'$inc': {'meta.count_like': -1}})

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]):
        obj = self.get_object()
        # Unlikely, but an actor can like it's own post
        DB.outbox.update_one({'activity.object.id': obj.id}, {'$inc': {'meta.count_like': 1}})

        DB.inbox.update_one({'activity.object.id': obj.id}, {'$set': {'meta.liked': obj_id}})

    def _undo_outbox(self) -> None:
        obj = self.get_object()
        # Unlikely, but an actor can like it's own post
        DB.outbox.update_one({'activity.object.id': obj.id}, {'$inc': {'meta.count_like': -1}})

        DB.inbox.update_one({'activity.object.id': obj.id}, {'$set': {'meta.liked': False}})

    def build_undo(self) -> BaseActivity:
        return Undo(object=self.to_dict(embed=True))


class Announce(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.ANNOUNCE
    ALLOWED_OBJECT_TYPES = [ActivityTypes.NOTE]

    def _recipients(self) -> List[str]:
        recipients = []

        for field in ['to', 'cc']:
            if field in self._data:
                recipients.extend(_to_list(self._data[field]))

        return recipients

    def _process_from_inbox(self) -> None:
        if isinstance(self._data['object'], str) and not self._data['object'].startswith('http'):
            # TODO(tsileo): actually drop it without storing it and better logging, also move the check somewhere else
            print(f'received an Annouce referencing an OStatus notice ({self._data["object"]}), dropping the message')
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
        DB.outbox.update_one({'activity.object.id': obj.id}, {'$inc': {'meta.count_boost': 1}})

    def _undo_inbox(self) -> None:
        obj = self.get_object()
        DB.inbox.update_one({'remote_id': obj.id}, {'$set': {'meta.undo': True}})
        DB.outbox.update_one({'activity.object.id':  obj.id}, {'$inc': {'meta.count_boost': -1}})

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
    ACTIVITY_TYPE = ActivityTypes.DELETE
    ALLOWED_OBJECT_TYPES = [ActivityTypes.NOTE, ActivityTypes.TOMBSTONE]

    def _recipients(self) -> List[str]:
        return self.get_object().recipients()

    def _process_from_inbox(self):
        DB.inbox.update_one({'activity.object.id': self.get_object().id}, {'$set': {'meta.deleted': True}})
        # TODO(tsileo): also delete copies stored in parents' `meta.replies`

    def _post_to_outbox(self, obj_id, activity, recipients):
        DB.outbox.update_one({'activity.object.id': self.get_object().id}, {'$set': {'meta.deleted': True}})


class Update(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.UPDATE
    ALLOWED_OBJECT_TYPES = [ActivityTypes.NOTE, ActivityTypes.PERSON]

    # TODO(tsileo): ensure the actor updating is the same as the orinial activity
    # (ensuring that the Update and its object are of same origin)

    def _process_from_inbox(self):
        obj = self.get_object()
        if obj.type_enum == ActivityTypes.NOTE:
            DB.inbox.update_one({'activity.object.id': obj.id}, {'$set': {'activity.object': obj.to_dict()}})
            return

        # If the object is a Person, it means the profile was updated, we just refresh our local cache
        ACTOR_SERVICE.get(obj.id, reload_cache=True)

    def _post_to_outbox(self, obj_id: str, activity: ObjectType, recipients: List[str]) -> None:
        obj = self.get_object()

        update_prefix = 'activity.object.'
        update: Dict[str, Any] = {'$set': dict(), '$unset': dict()}
        update['$set'][f'{update_prefix}updated'] = datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
        for k, v in obj._data.items():
            if k in ['id', 'type']:
                continue
            if v is None:
                update['$unset'][f'{update_prefix}{k}'] = ''
            else:
                update['$set'][f'{update_prefix}{k}'] = v

        if len(update['$unset']) == 0:
            del(update['$unset'])

        DB.outbox.update_one({'remote_id': obj.id.replace('/activity', '')}, update)
        # FIXME(tsileo): should send an Update (but not a partial one, to all the note's recipients
        # (create a new Update with the result of the update, and send it without saving it?)


class Create(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.CREATE
    ALLOWED_OBJECT_TYPES = [ActivityTypes.NOTE]

    def _set_id(self, uri: str, obj_id: str) -> None:
        self._data['object']['id'] = uri + '/activity'
        self._data['object']['url'] = ID + '/' + self.get_object().type.lower() + '/' + obj_id

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

    def _process_from_inbox(self):
        obj = self.get_object()

        tasks.fetch_og.delay('INBOX', self.id)

        in_reply_to = obj.inReplyTo
        if in_reply_to:
            parent = DB.inbox.find_one({'activity.type': 'Create', 'activity.object.id': in_reply_to})
            if not parent:
                DB.outbox.update_one(
                    {'activity.object.id': in_reply_to},
                    {'$inc': {'meta.count_reply': 1}},
                )
                return

            # If the note is a "reply of a reply" update the parent message
            # TODO(tsileo): review this code
            while parent:
                DB.inbox.update_one({'_id': parent['_id']}, {'$push': {'meta.replies': self.to_dict()}})
                in_reply_to = parent.get('activity', {}).get('object', {}).get('inReplyTo')
                if in_reply_to:
                    parent = DB.inbox.find_one({'activity.type': 'Create', 'activity.object.id': in_reply_to})
                    if parent is None:
                        # The reply is a note from the outbox
                        DB.outbox.update_one(
                            {'activity.object.id': in_reply_to},
                            {'$inc': {'meta.count_reply': 1}},
                        )
                else:
                    parent = None


class Tombstone(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.TOMBSTONE


class Note(BaseActivity):
    ACTIVITY_TYPE = ActivityTypes.NOTE

    def _init(self, **kwargs):
        print(self._data)
        # Remove the `actor` field as `attributedTo` is used for `Note` instead
        if 'actor' in self._data:
            del(self._data['actor'])
        # FIXME(tsileo): use kwarg
        # TODO(tsileo): support mention tag
        # TODO(tisleo): implement the tag endpoint
        if 'sensitive' not in kwargs:
            self._data['sensitive'] = False

        # FIXME(tsileo): add the tag in CC
        # for t in kwargs.get('tag', []):
        #     if t['type'] == 'Mention':
        #         cc -> c['href']

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


_ACTIVITY_TYPE_TO_CLS = {
    ActivityTypes.IMAGE: Image,
    ActivityTypes.PERSON: Person,
    ActivityTypes.FOLLOW: Follow,
    ActivityTypes.ACCEPT: Accept,
    ActivityTypes.UNDO: Undo,
    ActivityTypes.LIKE: Like,
    ActivityTypes.ANNOUNCE: Announce,
    ActivityTypes.UPDATE: Update,
    ActivityTypes.DELETE: Delete,
    ActivityTypes.CREATE: Create,
    ActivityTypes.NOTE: Note,
    ActivityTypes.BLOCK: Block,
    ActivityTypes.COLLECTION: Collection,
    ActivityTypes.TOMBSTONE: Tombstone,
}


def parse_activity(payload: ObjectType) -> BaseActivity:
    t = ActivityTypes(payload['type'])
    if t not in _ACTIVITY_TYPE_TO_CLS:
        raise ValueError('unsupported activity type')

    return _ACTIVITY_TYPE_TO_CLS[t](**payload)


def gen_feed():
    fg = FeedGenerator()
    fg.id(f'{ID}')
    fg.title(f'{USERNAME} notes')
    fg.author({'name': USERNAME, 'email': 't@a4.io'})
    fg.link(href=ID, rel='alternate')
    fg.description(f'{USERNAME} notes')
    fg.logo(ME.get('icon', {}).get('url'))
    fg.language('en')
    for item in DB.outbox.find({'type': 'Create'}, limit=50):
        fe = fg.add_entry()
        fe.id(item['activity']['object'].get('url'))
        fe.link(href=item['activity']['object'].get('url'))
        fe.title(item['activity']['object']['content'])
        fe.description(item['activity']['object']['content'])
    return fg


def json_feed(path: str) -> Dict[str, Any]:
    """JSON Feed (https://jsonfeed.org/) document."""
    data = []
    for item in DB.outbox.find({'type': 'Create'}, limit=50):
        data.append({
            "id": item["id"],
            "url": item['activity']['object'].get('url'),
            "content_html": item['activity']['object']['content'],
            "content_text": html2text(item['activity']['object']['content']),
            "date_published": item['activity']['object'].get('published'),
        })
    return {
        "version": "https://jsonfeed.org/version/1",
        "user_comment": ("This is a microblog feed. You can add this to your feed reader using the following URL: "
                         + ID + path),
        "title": USERNAME,
        "home_page_url": ID,
        "feed_url": ID + path,
        "author": {
            "name": USERNAME,
            "url": ID,
            "avatar": ME.get('icon', {}).get('url'),
        },
        "items": data,
    }


def build_inbox_json_feed(path: str, request_cursor: Optional[str] = None) -> Dict[str, Any]:
    data = []
    cursor = None

    q: Dict[str, Any] = {'type': 'Create'}
    if request_cursor:
        q['_id'] = {'$lt': request_cursor}

    for item in DB.inbox.find(q, limit=50).sort('_id', -1):
        actor = ACTOR_SERVICE.get(item['activity']['actor'])
        data.append({
            "id": item["activity"]["id"],
            "url": item['activity']['object'].get('url'),
            "content_html": item['activity']['object']['content'],
            "content_text": html2text(item['activity']['object']['content']),
            "date_published": item['activity']['object'].get('published'),
            "author": {
                "name": actor.get('name', actor.get('preferredUsername')),
                "url": actor.get('url'),
                'avatar': actor.get('icon', {}).get('url'),
            },
        })
        cursor = str(item['_id'])

    resp = {
        "version": "https://jsonfeed.org/version/1",
        "title": f'{USERNAME}\'s stream',
        "home_page_url": ID,
        "feed_url": ID + path,
        "items": data,
    }
    if cursor and len(data) == 50:
        resp['next_url'] = ID + path + '?cursor=' + cursor

    return resp


def parse_collection(payload: Optional[Dict[str, Any]] = None, url: Optional[str] = None) -> List[str]:
    """Resolve/fetch a `Collection`/`OrderedCollection`."""
    # Resolve internal collections via MongoDB directly
    if url == ID + '/followers':
        return [doc['remote_actor'] for doc in DB.followers.find()]
    elif url == ID + '/following':
        return [doc['remote_actor'] for doc in DB.following.find()]

    # Go through all the pages
    out: List[str] = []
    if url:
        resp = requests.get(url, headers={'Accept': 'application/activity+json'})
        resp.raise_for_status()
        payload = resp.json()

    if not payload:
        raise ValueError('must at least prove a payload or an URL')

    if payload['type'] in ['Collection', 'OrderedCollection']:
        if 'orderedItems' in payload:
            return payload['orderedItems']
        if 'items' in payload:
            return payload['items']
        if 'first' in payload:
            if 'orderedItems' in payload['first']:
                out.extend(payload['first']['orderedItems'])
            if 'items' in payload['first']:
                out.extend(payload['first']['items'])
            n = payload['first'].get('next')
            if n:
                out.extend(parse_collection(url=n))
        return out

    while payload:
        if payload['type'] in ['CollectionPage', 'OrderedCollectionPage']:
            if 'orderedItems' in payload:
                out.extend(payload['orderedItems'])
            if 'items' in payload:
                out.extend(payload['items'])
            n = payload.get('next')
            if n is None:
                break
            resp = requests.get(n, headers={'Accept': 'application/activity+json'})
            resp.raise_for_status()
            payload = resp.json()
        else:
            raise Exception('unexpected activity type {}'.format(payload['type']))

    return out


def build_ordered_collection(col, q=None, cursor=None, map_func=None, limit=50, col_name=None):
    col_name = col_name or col.name
    if q is None:
        q = {}

    if cursor:
        q['_id'] = {'$lt': ObjectId(cursor)}
    data = list(col.find(q, limit=limit).sort('_id', -1))

    if not data:
        return {
            'id': BASE_URL + '/' + col_name,
            'totalItems': 0,
            'type': 'OrderedCollection',
            'orederedItems': [],
        }

    start_cursor = str(data[0]['_id'])
    next_page_cursor = str(data[-1]['_id'])
    total_items = col.find(q).count()

    data = [_remove_id(doc) for doc in data]
    if map_func:
        data = [map_func(doc) for doc in data]

    # No cursor, this is the first page and we return an OrderedCollection
    if not cursor:
        resp = {
            '@context': CTX_AS,
            'id': f'{BASE_URL}/{col_name}',
            'totalItems': total_items,
            'type': 'OrderedCollection',
            'first': {
                'id': f'{BASE_URL}/{col_name}?cursor={start_cursor}',
                'orderedItems': data,
                'partOf': f'{BASE_URL}/{col_name}',
                'totalItems': total_items,
                'type': 'OrderedCollectionPage'
            },
        }

        if len(data) == limit:
            resp['first']['next'] = BASE_URL + '/' + col_name + '?cursor=' + next_page_cursor

        return resp

    # If there's a cursor, then we return an OrderedCollectionPage
    resp = {
        '@context': CTX_AS,
        'type': 'OrderedCollectionPage',
        'id': BASE_URL + '/' + col_name + '?cursor=' + start_cursor,
        'totalItems': total_items,
        'partOf': BASE_URL + '/' + col_name,
        'orderedItems': data,
    }
    if len(data) == limit:
        resp['next'] = BASE_URL + '/' + col_name + '?cursor=' + next_page_cursor

    return resp
