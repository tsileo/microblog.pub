from little_boxes.activitypub import use_backend
from little_boxes.activitypub import BaseBackend
from little_boxes.activitypub import Outbox
from little_boxes.activitypub import Person
from little_boxes.activitypub import Follow

def test_little_boxes_follow():
    back = BaseBackend()
    use_backend(back)

    me = back.setup_actor('Thomas', 'tom')

    other = back.setup_actor('Thomas', 'tom2')

    outbox = Outbox(me)
    f = Follow(
        actor=me.id,
        object=other.id,
    )

    outbox.post(f)
    assert back.followers(other) == [me.id]
    assert back.following(other) == []

    assert back.followers(me) == []
    assert back.following(me) == [other.id]
