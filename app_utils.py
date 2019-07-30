from flask_wtf.csrf import CSRFProtect
from little_boxes import activitypub as ap

import activitypub
from activitypub import Box
from config import ME
from tasks import Tasks

csrf = CSRFProtect()


back = activitypub.MicroblogPubBackend()
ap.use_backend(back)

MY_PERSON = ap.Person(**ME)


def post_to_outbox(activity: ap.BaseActivity) -> str:
    if activity.has_type(ap.CREATE_TYPES):
        activity = activity.build_create()

    # Assign create a random ID
    obj_id = back.random_object_id()

    activity.set_id(back.activity_url(obj_id), obj_id)

    back.save(Box.OUTBOX, activity)
    Tasks.cache_actor(activity.id)
    Tasks.finish_post_to_outbox(activity.id)
    return activity.id
