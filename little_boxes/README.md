# Little Boxes

Tiny ActivityPub framework.

## Getting Started

```python
from little_boxes.activitypub import BaseBackend
from little_boxes.activitypub import use_backend
from little_boxes.activitypub import Outbox
from little_boxes.activitypub import Person
from little_boxes.activitypub import Follow

from mydb import db_client


class MyBackend(BaseBackend):

    def __init__(self, db_connection):
        self.db_connection = db_connection    

    def inbox_new(self, as_actor, activity):
        # Save activity as "as_actor"
        # [...]

    def post_to_remote_inbox(self, as_actor, payload, recipient):
        # Send the activity to the remote actor
        # [...]


db_con = db_client()
my_backend = MyBackend(db_con)

use_backend(my_backend)

me = Person({})  # Init an actor
outbox = Outbox(me)

follow = Follow(actor=me, object='http://iri-i-want-follow')
outbox.post(follow)
```
