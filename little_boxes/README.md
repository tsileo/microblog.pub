# Little Boxes

Tiny ActivityPub framework written in Python, both database and server agnostic.

## Getting Started

```python
from little_boxes import activitypub as ap

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

ap.use_backend(my_backend)

me = ap.Person({})  # Init an actor
outbox = ap.Outbox(me)

follow = ap.Follow(actor=me, object='http://iri-i-want-follow')
outbox.post(follow)
```
