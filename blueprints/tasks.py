import json
import traceback
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict

import flask
import requests
from flask import current_app as app
from little_boxes import activitypub as ap
from little_boxes.activitypub import _to_list
from little_boxes.errors import ActivityGoneError
from little_boxes.errors import ActivityNotFoundError
from little_boxes.errors import NotAnActivityError
from requests.exceptions import HTTPError

import config
from config import DB
from config import MEDIA_CACHE
from core import gc
from core.activitypub import SIG_AUTH
from core.activitypub import Box
from core.activitypub import _actor_hash
from core.activitypub import _add_answers_to_question
from core.activitypub import _cache_actor_icon
from core.activitypub import is_from_outbox
from core.activitypub import new_context
from core.activitypub import post_to_outbox
from core.activitypub import save_reply
from core.activitypub import update_cached_actor
from core.db import find_one_activity
from core.db import update_one_activity
from core.inbox import process_inbox
from core.meta import MetaKey
from core.meta import by_object_id
from core.meta import by_remote_id
from core.meta import by_type
from core.meta import inc
from core.meta import upsert
from core.notifications import _NewMeta
from core.notifications import set_inbox_flags
from core.outbox import process_outbox
from core.remote import track_failed_send
from core.remote import track_successful_send
from core.shared import MY_PERSON
from core.shared import _Response
from core.shared import back
from core.shared import p
from core.tasks import Tasks
from utils import now
from utils import opengraph
from utils.media import is_video
from utils.webmentions import discover_webmention_endpoint

blueprint = flask.Blueprint("tasks", __name__)


class TaskError(Exception):
    """Raised to log the error for poussetaches."""

    def __init__(self):
        self.message = traceback.format_exc()


@blueprint.route("/task/update_question", methods=["POST"])
def task_update_question() -> _Response:
    """Sends an Update."""
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        app.logger.info(f"Updating question {iri}")
        cc = [config.ID + "/followers"]
        doc = DB.activities.find_one({"box": Box.OUTBOX.value, "remote_id": iri})
        _add_answers_to_question(doc)
        question = ap.Question(**doc["activity"]["object"])

        raw_update = dict(
            actor=question.id,
            object=question.to_dict(embed=True),
            attributedTo=MY_PERSON.id,
            cc=list(set(cc)),
            to=[ap.AS_PUBLIC],
        )
        raw_update["@context"] = config.DEFAULT_CTX

        update = ap.Update(**raw_update)
        print(update)
        print(update.to_dict())
        post_to_outbox(update)

    except HTTPError as err:
        app.logger.exception("request failed")
        if 400 >= err.response.status_code >= 499:
            app.logger.info("client error, no retry")
            return ""

        raise TaskError() from err
    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    return ""


@blueprint.route("/task/send_actor_update", methods=["POST"])
def task_send_actor_update() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    try:
        update = ap.Update(
            actor=MY_PERSON.id,
            object=MY_PERSON.to_dict(),
            to=[MY_PERSON.followers],
            cc=[ap.AS_PUBLIC],
            published=now(),
            context=new_context(),
        )

        post_to_outbox(update)
    except Exception as err:
        app.logger.exception(f"failed to send actor update")
        raise TaskError() from err

    return ""


@blueprint.route("/task/fetch_og_meta", methods=["POST"])
def task_fetch_og_meta() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")
        if activity.has_type(ap.ActivityType.CREATE):
            note = activity.get_object()
            links = opengraph.links_from_note(note.to_dict())
            og_metadata = opengraph.fetch_og_metadata(config.USER_AGENT, links)
            for og in og_metadata:
                if not og.get("image"):
                    continue
                config.MEDIA_CACHE.cache_og_image(og["image"], iri)

            app.logger.debug(f"OG metadata {og_metadata!r}")
            DB.activities.update_one(
                {"remote_id": iri}, {"$set": {"meta.og_metadata": og_metadata}}
            )

        app.logger.info(f"OG metadata fetched for {iri}: {og_metadata}")
    except (ActivityGoneError, ActivityNotFoundError):
        app.logger.exception(f"dropping activity {iri}, skip OG metedata")
        return ""
    except requests.exceptions.HTTPError as http_err:
        if 400 <= http_err.response.status_code < 500:
            app.logger.exception("bad request, no retry")
            return ""
        app.logger.exception("failed to fetch OG metadata")
        raise TaskError() from http_err
    except Exception as err:
        app.logger.exception(f"failed to fetch OG metadata for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/cache_object", methods=["POST"])
def task_cache_object() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")
        obj = activity.get_object()
        Tasks.cache_emojis(obj)

        # Refetch the object actor (without cache)
        obj_actor = ap.fetch_remote_activity(obj.get_actor().id, no_cache=True)

        cache = {MetaKey.OBJECT: obj.to_dict(embed=True)}

        if activity.get_actor().id != obj_actor.id:
            # Cache the object actor
            obj_actor_hash = _actor_hash(obj_actor)
            cache[MetaKey.OBJECT_ACTOR] = obj_actor.to_dict(embed=True)
            cache[MetaKey.OBJECT_ACTOR_ID] = obj_actor.id
            cache[MetaKey.OBJECT_ACTOR_HASH] = obj_actor_hash

            # Update the actor cache for the other activities
            update_cached_actor(obj_actor)

        update_one_activity(by_remote_id(activity.id), upsert(cache))

    except (ActivityGoneError, ActivityNotFoundError, NotAnActivityError):
        DB.activities.update_one({"remote_id": iri}, {"$set": {"meta.deleted": True}})
        app.logger.exception(f"flagging activity {iri} as deleted, no object caching")
    except Exception as err:
        app.logger.exception(f"failed to cache object for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/finish_post_to_outbox", methods=["POST"])  # noqa:C901
def task_finish_post_to_outbox() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        recipients = activity.recipients()

        process_outbox(activity, {})

        app.logger.info(f"recipients={recipients}")
        activity = ap.clean_activity(activity.to_dict())

        payload = json.dumps(activity)
        for recp in recipients:
            app.logger.debug(f"posting to {recp}")
            Tasks.post_to_remote_inbox(payload, recp)
    except (ActivityGoneError, ActivityNotFoundError):
        app.logger.exception(f"no retry")
    except Exception as err:
        app.logger.exception(f"failed to post to remote inbox for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/finish_post_to_inbox", methods=["POST"])  # noqa: C901
def task_finish_post_to_inbox() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        process_inbox(activity, {})

    except (ActivityGoneError, ActivityNotFoundError, NotAnActivityError):
        app.logger.exception(f"no retry")
    except Exception as err:
        app.logger.exception(f"failed to cfinish post to inbox for {iri}")
        raise TaskError() from err

    return ""


def select_video_to_cache(links):
    """Try to find the 360p version from a video urls, or return the smallest one."""
    videos = []
    for link in links:
        if link.get("mimeType", "").startswith("video/") or is_video(link["href"]):
            videos.append({"href": link["href"], "height": link["height"]})

    if not videos:
        app.logger.warning(f"failed to select a video from {links!r}")
        return None

    videos = sorted(videos, key=lambda l: l["height"])
    for video in videos:
        if video["height"] == 360:
            return video

    return videos[0]


@blueprint.route("/task/cache_attachments", methods=["POST"])
def task_cache_attachments() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"caching attachment for activity={activity!r}")
        # Generates thumbnails for the actor's icon and the attachments if any

        if activity.has_type([ap.ActivityType.CREATE, ap.ActivityType.ANNOUNCE]):
            obj = activity.get_object()
        else:
            obj = activity

        if obj.has_type(ap.ActivityType.VIDEO):
            if isinstance(obj.url, list):
                # TODO: filter only videogt
                link = select_video_to_cache(obj.url)
                if link:
                    Tasks.cache_attachment({"url": link["href"]}, iri)
            elif isinstance(obj.url, str):
                Tasks.cache_attachment({"url": obj.url}, iri)
            else:
                app.logger.warning(f"failed to parse video link {obj!r} for {iri}")

        # Iter the attachments
        for attachment in obj._data.get("attachment", []):
            Tasks.cache_attachment(attachment, iri)

        app.logger.info(f"attachments cached for {iri}")

    except (ActivityGoneError, ActivityNotFoundError, NotAnActivityError):
        app.logger.exception(f"dropping activity {iri}, no attachment caching")
    except Exception as err:
        app.logger.exception(f"failed to cache attachments for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/cache_attachment", methods=["POST"])
def task_cache_attachment() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload["iri"]
    attachment = task.payload["attachment"]
    try:
        app.logger.info(f"caching attachment {attachment!r} for {iri}")

        config.MEDIA_CACHE.cache_attachment(attachment, iri)

        app.logger.info(f"attachment {attachment!r} cached for {iri}")
    except Exception as err:
        app.logger.exception(f"failed to cache attachment {attachment!r} for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/send_webmention", methods=["POST"])
def task_send_webmention() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    note_url = task.payload["note_url"]
    link = task.payload["link"]
    remote_id = task.payload["remote_id"]
    try:
        app.logger.info(f"trying to send webmention source={note_url} target={link}")
        webmention_endpoint = discover_webmention_endpoint(link)
        if not webmention_endpoint:
            app.logger.info("no webmention endpoint")
            return ""

        resp = requests.post(
            webmention_endpoint,
            data={"source": note_url, "target": link},
            headers={"User-Agent": config.USER_AGENT},
        )
        app.logger.info(f"webmention endpoint resp={resp}/{resp.text}")
        resp.raise_for_status()
    except HTTPError as err:
        app.logger.exception("request failed")
        if 400 >= err.response.status_code >= 499:
            app.logger.info("client error, no retry")
            return ""

        raise TaskError() from err
    except Exception as err:
        app.logger.exception(f"failed to cache actor for {link}/{remote_id}/{note_url}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/cache_actor", methods=["POST"])
def task_cache_actor() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload["iri"]
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        # Reload the actor without caching (in case it got upated)
        actor = ap.fetch_remote_activity(activity.get_actor().id, no_cache=True)

        # Fetch the Open Grah metadata if it's a `Create`
        if activity.has_type(ap.ActivityType.CREATE):
            obj = activity.get_object()
            links = opengraph.links_from_note(obj.to_dict())
            if links:
                Tasks.fetch_og_meta(iri)

                # Send Webmentions only if it's from the outbox, and public
                if (
                    is_from_outbox(obj)
                    and ap.get_visibility(obj) == ap.Visibility.PUBLIC
                ):
                    Tasks.send_webmentions(activity, links)

        if activity.has_type(ap.ActivityType.FOLLOW):
            if actor.id == config.ID:
                # It's a new following, cache the "object" (which is the actor we follow)
                DB.activities.update_one(
                    by_remote_id(iri),
                    upsert({MetaKey.OBJECT: activity.get_object().to_dict(embed=True)}),
                )

        # Cache the actor info
        update_cached_actor(actor)

        app.logger.info(f"actor cached for {iri}")
        if not activity.has_type([ap.ActivityType.CREATE, ap.ActivityType.ANNOUNCE]):
            return ""

        if activity.get_object()._data.get(
            "attachment", []
        ) or activity.get_object().has_type(ap.ActivityType.VIDEO):
            Tasks.cache_attachments(iri)

    except (ActivityGoneError, ActivityNotFoundError):
        DB.activities.update_one({"remote_id": iri}, {"$set": {"meta.deleted": True}})
        app.logger.exception(f"flagging activity {iri} as deleted, no actor caching")
    except Exception as err:
        app.logger.exception(f"failed to cache actor for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/cache_actor_icon", methods=["POST"])
def task_cache_actor_icon() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    actor_iri = task.payload["actor_iri"]
    icon_url = task.payload["icon_url"]
    try:
        MEDIA_CACHE.cache_actor_icon(icon_url)
    except Exception as exc:
        err = f"failed to cache actor icon {icon_url} for {actor_iri}"
        app.logger.exception(err)
        raise TaskError() from exc

    return ""


@blueprint.route("/task/cache_emoji", methods=["POST"])
def task_cache_emoji() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload["iri"]
    url = task.payload["url"]
    try:
        MEDIA_CACHE.cache_emoji(url, iri)
    except Exception as exc:
        err = f"failed to cache emoji {url} at {iri}"
        app.logger.exception(err)
        raise TaskError() from exc

    return ""


@blueprint.route("/task/forward_activity", methods=["POST"])
def task_forward_activity() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        recipients = back.followers_as_recipients()
        app.logger.debug(f"Forwarding {activity!r} to {recipients}")
        activity = ap.clean_activity(activity.to_dict())
        payload = json.dumps(activity)
        for recp in recipients:
            app.logger.debug(f"forwarding {activity!r} to {recp}")
            Tasks.post_to_remote_inbox(payload, recp)
    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    return ""


@blueprint.route("/task/post_to_remote_inbox", methods=["POST"])
def task_post_to_remote_inbox() -> _Response:
    """Post an activity to a remote inbox."""
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    payload, to = task.payload["payload"], task.payload["to"]
    try:
        app.logger.info("payload=%s", payload)
        app.logger.info("generating sig")
        signed_payload = json.loads(payload)

        app.logger.info("to=%s", to)
        resp = requests.post(
            to,
            data=json.dumps(signed_payload),
            auth=SIG_AUTH,
            headers={
                "Content-Type": config.HEADERS[1],
                "Accept": config.HEADERS[1],
                "User-Agent": config.USER_AGENT,
            },
        )
        app.logger.info("resp=%s", resp)
        app.logger.info("resp_body=%s", resp.text)
        resp.raise_for_status()
    except HTTPError as err:
        track_failed_send(to)

        app.logger.exception("request failed")
        if 400 >= err.response.status_code >= 499:
            app.logger.info("client error, no retry")
            return ""

        raise TaskError() from err
    except requests.RequestException:
        track_failed_send(to)

        app.logger.exception("request failed")

    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    track_successful_send(to)

    return ""


@blueprint.route("/task/fetch_remote_question", methods=["POST"])
def task_fetch_remote_question() -> _Response:
    """Fetch a remote question for implementation that does not send Update."""
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        app.logger.info(f"Fetching remote question {iri}")
        local_question = DB.activities.find_one(
            {
                "box": Box.INBOX.value,
                "type": ap.ActivityType.CREATE.value,
                "activity.object.id": iri,
            }
        )
        remote_question = ap.get_backend().fetch_iri(iri, no_cache=True)
        # FIXME(tsileo): compute and set `meta.object_visiblity` (also update utils.py to do it)
        if (
            local_question
            and (
                local_question["meta"].get("voted_for")
                or local_question["meta"].get("subscribed")
            )
            and not DB.notifications.find_one({"activity.id": remote_question["id"]})
        ):
            DB.notifications.insert_one(
                {
                    "type": "question_ended",
                    "datetime": datetime.now(timezone.utc).isoformat(),
                    "activity": remote_question,
                }
            )

        # Update the Create if we received it in the inbox
        if local_question:
            DB.activities.update_one(
                {"remote_id": local_question["remote_id"], "box": Box.INBOX.value},
                {"$set": {"activity.object": remote_question}},
            )

        # Also update all the cached copies (Like, Announce...)
        DB.activities.update_many(
            {"meta.object.id": remote_question["id"]},
            {"$set": {"meta.object": remote_question}},
        )

    except HTTPError as err:
        app.logger.exception("request failed")
        if 400 >= err.response.status_code >= 499:
            app.logger.info("client error, no retry")
            return ""

        raise TaskError() from err
    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    return ""


@blueprint.route("/task/cleanup", methods=["POST"])
def task_cleanup() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    gc.perform()
    return ""


def _is_local_reply(activity: ap.BaseActivity) -> bool:
    for dest in _to_list(activity.to or []):
        if dest.startswith(config.BASE_URL):
            return True

    for dest in _to_list(activity.cc or []):
        if dest.startswith(config.BASE_URL):
            return True

    return False


@blueprint.route("/task/process_reply", methods=["POST"])
def task_process_reply() -> _Response:
    """Process `Announce`d posts from Pleroma relays in order to process replies of activities that are in the inbox."""
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"checking for reply activity={activity!r}")

        # Some AP server always return Create when requesting an object
        if activity.has_type(ap.ActivityType.CREATE):
            activity = activity.get_object()

        in_reply_to = activity.get_in_reply_to()
        if not in_reply_to:
            # If it's not reply, we can drop it
            app.logger.info(f"activity={activity!r} is not a reply, dropping it")
            return ""

        root_reply = in_reply_to

        # Fetch the activity reply
        reply = ap.fetch_remote_activity(in_reply_to)
        if reply.has_type(ap.ActivityType.CREATE):
            reply = reply.get_object()

        new_replies = [activity, reply]

        while 1:
            in_reply_to = reply.get_in_reply_to()
            if not in_reply_to:
                break

            root_reply = in_reply_to
            reply = ap.fetch_remote_activity(root_reply)

            if reply.has_type(ap.ActivityType.CREATE):
                reply = reply.get_object()

            new_replies.append(reply)

        app.logger.info(f"root_reply={reply!r} for activity={activity!r}")

        # In case the activity was from the inbox
        update_one_activity(
            {**by_object_id(activity.id), **by_type(ap.ActivityType.CREATE)},
            upsert({MetaKey.THREAD_ROOT_PARENT: root_reply}),
        )

        for (new_reply_idx, new_reply) in enumerate(new_replies):
            if find_one_activity(
                {**by_object_id(new_reply.id), **by_type(ap.ActivityType.CREATE)}
            ) or DB.replies.find_one(by_remote_id(new_reply.id)):
                continue

            actor = new_reply.get_actor()
            is_root_reply = new_reply_idx == len(new_replies) - 1
            if is_root_reply:
                reply_flags: Dict[str, Any] = {}
            else:
                reply_actor = new_replies[new_reply_idx + 1].get_actor()
                is_in_reply_to_self = actor.id == reply_actor.id
                reply_flags = {
                    MetaKey.IN_REPLY_TO_SELF.value: is_in_reply_to_self,
                    MetaKey.IN_REPLY_TO.value: new_reply.get_in_reply_to(),
                }
                if not is_in_reply_to_self:
                    reply_flags[MetaKey.IN_REPLY_TO_ACTOR.value] = reply_actor.to_dict(
                        embed=True
                    )

            # Save the reply with the cached actor and the thread flag/ID
            save_reply(
                new_reply,
                {
                    **reply_flags,
                    MetaKey.THREAD_ROOT_PARENT.value: root_reply,
                    MetaKey.ACTOR.value: actor.to_dict(embed=True),
                    MetaKey.ACTOR_HASH.value: _actor_hash(actor),
                },
            )

            # Update the reply counters
            if new_reply.get_in_reply_to():
                update_one_activity(
                    {
                        **by_object_id(new_reply.get_in_reply_to()),
                        **by_type(ap.ActivityType.CREATE),
                    },
                    inc(MetaKey.COUNT_REPLY, 1),
                )
                DB.replies.update_one(
                    by_remote_id(new_reply.get_in_reply_to()),
                    inc(MetaKey.COUNT_REPLY, 1),
                )

            # Cache the actor icon
            _cache_actor_icon(actor)
            # And cache the attachments
            Tasks.cache_attachments(new_reply.id)
    except (ActivityGoneError, ActivityNotFoundError):
        app.logger.exception(f"dropping activity {iri}, skip processing")
        return ""
    except Exception as err:
        app.logger.exception(f"failed to process new activity {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/process_new_activity", methods=["POST"])  # noqa:c901
def task_process_new_activity() -> _Response:
    """Process an activity received in the inbox"""
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        flags: _NewMeta = {}

        set_inbox_flags(activity, flags)
        app.logger.info(f"a={activity}, flags={flags!r}")
        if flags:
            DB.activities.update_one({"remote_id": activity.id}, {"$set": flags})

        app.logger.info(f"new activity {iri} processed")
    except (ActivityGoneError, ActivityNotFoundError):
        app.logger.exception(f"dropping activity {iri}, skip processing")
        return ""
    except Exception as err:
        app.logger.exception(f"failed to process new activity {iri}")
        raise TaskError() from err

    return ""
