import os
import time
from typing import List
from typing import Tuple

import requests
from html2text import html2text
from little_boxes.collection import parse_collection


def resp2plaintext(resp):
    """Convert the body of a requests reponse to plain text in order to make basic assertions."""
    return html2text(resp.text)


class Instance(object):
    """Test instance wrapper."""

    def __init__(self, name, host_url, docker_url=None):
        self.host_url = host_url
        self.docker_url = docker_url or host_url
        self._create_delay = 10
        with open(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                f"fixtures/{name}/config/admin_api_key.key",
            )
        ) as f:
            api_key = f.read()
        self._auth_headers = {"Authorization": f"Bearer {api_key}"}

    def _do_req(self, url):
        """Used to parse collection."""
        url = url.replace(self.docker_url, self.host_url)
        resp = requests.get(url, headers={"Accept": "application/activity+json"})
        resp.raise_for_status()
        return resp.json()

    def _parse_collection(self, payload=None, url=None):
        """Parses a collection (go through all the pages)."""
        return parse_collection(url=url, payload=payload, fetcher=self._do_req)

    def ping(self):
        """Ensures the homepage is reachable."""
        resp = requests.get(f"{self.host_url}/")
        resp.raise_for_status()
        assert resp.status_code == 200

    def debug(self):
        """Returns the debug infos (number of items in the inbox/outbox."""
        resp = requests.get(
            f"{self.host_url}/api/debug",
            headers={**self._auth_headers, "Accept": "application/json"},
        )
        resp.raise_for_status()

        return resp.json()

    def drop_db(self):
        """Drops the MongoDB DB."""
        resp = requests.delete(
            f"{self.host_url}/api/debug",
            headers={**self._auth_headers, "Accept": "application/json"},
        )
        resp.raise_for_status()

        return resp.json()

    def block(self, actor_url) -> None:
        """Blocks an actor."""
        # Instance1 follows instance2
        resp = requests.post(
            f"{self.host_url}/api/block",
            params={"actor": actor_url},
            headers=self._auth_headers,
        )
        assert resp.status_code == 201

        # We need to wait for the Follow/Accept dance
        time.sleep(self._create_delay / 2)
        return resp.json().get("activity")

    def follow(self, instance: "Instance") -> str:
        """Follows another instance."""
        # Instance1 follows instance2
        resp = requests.post(
            f"{self.host_url}/api/follow",
            json={"actor": instance.docker_url},
            headers=self._auth_headers,
        )
        assert resp.status_code == 201

        # We need to wait for the Follow/Accept dance
        time.sleep(self._create_delay)
        return resp.json().get("activity")

    def new_note(self, content, reply=None) -> str:
        """Creates a new note."""
        params = {"content": content}
        if reply:
            params["reply"] = reply

        resp = requests.post(
            f"{self.host_url}/api/new_note", json=params, headers=self._auth_headers
        )
        assert resp.status_code == 201

        time.sleep(self._create_delay)
        return resp.json().get("activity")

    def boost(self, oid: str) -> str:
        """Creates an Announce activity."""
        resp = requests.post(
            f"{self.host_url}/api/boost", json={"id": oid}, headers=self._auth_headers
        )
        assert resp.status_code == 201

        time.sleep(self._create_delay)
        return resp.json().get("activity")

    def like(self, oid: str) -> str:
        """Creates a Like activity."""
        resp = requests.post(
            f"{self.host_url}/api/like", json={"id": oid}, headers=self._auth_headers
        )
        assert resp.status_code == 201

        time.sleep(self._create_delay)
        return resp.json().get("activity")

    def delete(self, oid: str) -> str:
        """Creates a Delete activity."""
        resp = requests.post(
            f"{self.host_url}/api/note/delete",
            json={"id": oid},
            headers=self._auth_headers,
        )
        assert resp.status_code == 201

        time.sleep(self._create_delay)
        return resp.json().get("activity")

    def undo(self, oid: str) -> str:
        """Creates a Undo activity."""
        resp = requests.post(
            f"{self.host_url}/api/undo", json={"id": oid}, headers=self._auth_headers
        )
        assert resp.status_code == 201

        # We need to wait for the Follow/Accept dance
        time.sleep(self._create_delay)
        return resp.json().get("activity")

    def followers(self) -> List[str]:
        """Parses the followers collection."""
        resp = requests.get(
            f"{self.host_url}/followers",
            headers={"Accept": "application/activity+json"},
        )
        resp.raise_for_status()

        data = resp.json()

        return self._parse_collection(payload=data)

    def following(self):
        """Parses the following collection."""
        resp = requests.get(
            f"{self.host_url}/following",
            headers={"Accept": "application/activity+json"},
        )
        resp.raise_for_status()

        data = resp.json()

        return self._parse_collection(payload=data)

    def outbox(self):
        """Returns the instance outbox."""
        resp = requests.get(
            f"{self.host_url}/following",
            headers={"Accept": "application/activity+json"},
        )
        resp.raise_for_status()
        return resp.json()

    def outbox_get(self, aid):
        """Fetches a specific item from the instance outbox."""
        resp = requests.get(
            aid.replace(self.docker_url, self.host_url),
            headers={"Accept": "application/activity+json"},
        )
        resp.raise_for_status()
        return resp.json()

    def stream_jsonfeed(self):
        """Returns the "stream"'s JSON feed."""
        resp = requests.get(
            f"{self.host_url}/api/stream",
            headers={**self._auth_headers, "Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


def _instances() -> Tuple[Instance, Instance]:
    """Initializes the client for the two test instances."""
    instance1 = Instance("instance1", "http://docker:5006", "http://instance1_web:5005")
    instance1.ping()

    instance2 = Instance("instance2", "http://docker:5007", "http://instance2_web:5005")
    instance2.ping()

    # Return the DB
    instance1.drop_db()
    instance2.drop_db()

    return instance1, instance2


def test_follow() -> None:
    """instance1 follows instance2."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance1_debug = instance1.debug()
    assert instance1_debug["inbox"] == 1  # An Accept activity should be there
    assert instance1_debug["outbox"] == 1  # We've sent a Follow activity

    instance2_debug = instance2.debug()
    assert instance2_debug["inbox"] == 1  # An Follow activity should be there
    assert instance2_debug["outbox"] == 1  # We've sent a Accept activity

    assert instance2.followers() == [instance1.docker_url]
    assert instance1.following() == [instance2.docker_url]


def test_follow_unfollow():
    """instance1 follows instance2, then unfollows it."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    follow_id = instance1.follow(instance2)
    instance1_debug = instance1.debug()
    assert instance1_debug["inbox"] == 1  # An Accept activity should be there
    assert instance1_debug["outbox"] == 1  # We've sent a Follow activity

    instance2_debug = instance2.debug()
    assert instance2_debug["inbox"] == 1  # An Follow activity should be there
    assert instance2_debug["outbox"] == 1  # We've sent a Accept activity

    assert instance2.followers() == [instance1.docker_url]
    assert instance1.following() == [instance2.docker_url]

    instance1.undo(follow_id)

    assert instance2.followers() == []
    assert instance1.following() == []

    instance1_debug = instance1.debug()
    assert instance1_debug["inbox"] == 1  # An Accept activity should be there
    assert instance1_debug["outbox"] == 2  # We've sent a Follow and a Undo activity

    instance2_debug = instance2.debug()
    assert instance2_debug["inbox"] == 2  # An Follow and Undo activity should be there
    assert instance2_debug["outbox"] == 1  # We've sent a Accept activity


def test_post_content():
    """Instances follow each other, and instance1 creates a note."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 0

    create_id = instance1.new_note("hello")
    instance2_debug = instance2.debug()
    assert (
        instance2_debug["inbox"] == 3
    )  # An Follow, Accept and Create activity should be there
    assert instance2_debug["outbox"] == 2  # We've sent a Accept and a Follow  activity

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 1
    assert inbox_stream["items"][0]["id"] == create_id


def test_block_and_post_content():
    """Instances follow each other, instance2 blocks instance1, instance1 creates a new note."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 0

    instance2.block(instance1.docker_url)

    instance1.new_note("hello")
    instance2_debug = instance2.debug()
    assert (
        instance2_debug["inbox"] == 2
    )  # An Follow, Accept activity should be there, Create should have been dropped
    assert (
        instance2_debug["outbox"] == 3
    )  # We've sent a Accept and a Follow  activity + the Block activity

    # Ensure the post is not visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 0


def test_post_content_and_delete():
    """Instances follow each other, instance1 creates a new note, then deletes it."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 0

    create_id = instance1.new_note("hello")
    instance2_debug = instance2.debug()
    assert (
        instance2_debug["inbox"] == 3
    )  # An Follow, Accept and Create activity should be there
    assert instance2_debug["outbox"] == 2  # We've sent a Accept and a Follow  activity

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 1
    assert inbox_stream["items"][0]["id"] == create_id

    instance1.delete(f"{create_id}/activity")
    instance2_debug = instance2.debug()
    assert (
        instance2_debug["inbox"] == 4
    )  # An Follow, Accept and Create and Delete activity should be there
    assert instance2_debug["outbox"] == 2  # We've sent a Accept and a Follow  activity

    # Ensure the post has been delete from instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 0


def test_post_content_and_like():
    """Instances follow each other, instance1 creates a new note, instance2 likes it."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    create_id = instance1.new_note("hello")

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 1
    assert inbox_stream["items"][0]["id"] == create_id

    # Now, instance2 like the note
    like_id = instance2.like(f"{create_id}/activity")

    instance1_debug = instance1.debug()
    assert instance1_debug["inbox"] == 3  # Follow, Accept and Like
    assert instance1_debug["outbox"] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f"{create_id}/activity")
    assert "likes" in note
    assert note["likes"]["totalItems"] == 1
    likes = instance1._parse_collection(url=note["likes"]["first"])
    assert len(likes) == 1
    assert likes[0]["id"] == like_id


def test_post_content_and_like_unlike() -> None:
    """Instances follow each other, instance1 creates a new note, instance2 likes it, then unlikes it."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    create_id = instance1.new_note("hello")

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 1
    assert inbox_stream["items"][0]["id"] == create_id

    # Now, instance2 like the note
    like_id = instance2.like(f"{create_id}/activity")

    instance1_debug = instance1.debug()
    assert instance1_debug["inbox"] == 3  # Follow, Accept and Like
    assert instance1_debug["outbox"] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f"{create_id}/activity")
    assert "likes" in note
    assert note["likes"]["totalItems"] == 1
    likes = instance1._parse_collection(url=note["likes"]["first"])
    assert len(likes) == 1
    assert likes[0]["id"] == like_id

    instance2.undo(like_id)

    instance1_debug = instance1.debug()
    assert instance1_debug["inbox"] == 4  # Follow, Accept and Like and Undo
    assert instance1_debug["outbox"] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f"{create_id}/activity")
    assert "likes" in note
    assert note["likes"]["totalItems"] == 0


def test_post_content_and_boost() -> None:
    """Instances follow each other, instance1 creates a new note, instance2 "boost" it."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    create_id = instance1.new_note("hello")

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 1
    assert inbox_stream["items"][0]["id"] == create_id

    # Now, instance2 like the note
    boost_id = instance2.boost(f"{create_id}/activity")

    instance1_debug = instance1.debug()
    assert instance1_debug["inbox"] == 3  # Follow, Accept and Announce
    assert instance1_debug["outbox"] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f"{create_id}/activity")
    assert "shares" in note
    assert note["shares"]["totalItems"] == 1
    shares = instance1._parse_collection(url=note["shares"]["first"])
    assert len(shares) == 1
    assert shares[0]["id"] == boost_id


def test_post_content_and_boost_unboost() -> None:
    """Instances follow each other, instance1 creates a new note, instance2 "boost" it, then "unboost" it."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    create_id = instance1.new_note("hello")

    # Ensure the post is visible in instance2's stream
    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 1
    assert inbox_stream["items"][0]["id"] == create_id

    # Now, instance2 like the note
    boost_id = instance2.boost(f"{create_id}/activity")

    instance1_debug = instance1.debug()
    assert instance1_debug["inbox"] == 3  # Follow, Accept and Announce
    assert instance1_debug["outbox"] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f"{create_id}/activity")
    assert "shares" in note
    assert note["shares"]["totalItems"] == 1
    shares = instance1._parse_collection(url=note["shares"]["first"])
    assert len(shares) == 1
    assert shares[0]["id"] == boost_id

    instance2.undo(boost_id)

    instance1_debug = instance1.debug()
    assert instance1_debug["inbox"] == 4  # Follow, Accept and Announce and Undo
    assert instance1_debug["outbox"] == 3  # Folllow, Accept, and Create

    note = instance1.outbox_get(f"{create_id}/activity")
    assert "shares" in note
    assert note["shares"]["totalItems"] == 0


def test_post_content_and_post_reply() -> None:
    """Instances follow each other, instance1 creates a new note, instance2 replies to it."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 0

    instance1_create_id = instance1.new_note("hello")
    instance2_debug = instance2.debug()
    assert (
        instance2_debug["inbox"] == 3
    )  # An Follow, Accept and Create activity should be there
    assert instance2_debug["outbox"] == 2  # We've sent a Accept and a Follow  activity

    # Ensure the post is visible in instance2's stream
    instance2_inbox_stream = instance2.stream_jsonfeed()
    assert len(instance2_inbox_stream["items"]) == 1
    assert instance2_inbox_stream["items"][0]["id"] == instance1_create_id

    instance2_create_id = instance2.new_note(
        f"hey @instance1@{instance1.docker_url}",
        reply=f"{instance1_create_id}/activity",
    )
    instance2_debug = instance2.debug()
    assert (
        instance2_debug["inbox"] == 3
    )  # An Follow, Accept and Create activity should be there
    assert (
        instance2_debug["outbox"] == 3
    )  # We've sent a Accept and a Follow and a Create  activity

    instance1_debug = instance1.debug()
    assert (
        instance1_debug["inbox"] == 3
    )  # An Follow, Accept and Create activity should be there
    assert (
        instance1_debug["outbox"] == 3
    )  # We've sent a Accept and a Follow and a Create  activity

    instance1_inbox_stream = instance1.stream_jsonfeed()
    assert len(instance1_inbox_stream["items"]) == 1
    assert instance1_inbox_stream["items"][0]["id"] == instance2_create_id

    instance1_note = instance1.outbox_get(f"{instance1_create_id}/activity")
    assert "replies" in instance1_note
    assert instance1_note["replies"]["totalItems"] == 1
    replies = instance1._parse_collection(url=instance1_note["replies"]["first"])
    assert len(replies) == 1
    assert replies[0]["id"] == f"{instance2_create_id}/activity"


def test_post_content_and_post_reply_and_delete() -> None:
    """Instances follow each other, instance1 creates a new note, instance2 replies to it, then deletes its reply."""
    instance1, instance2 = _instances()
    # Instance1 follows instance2
    instance1.follow(instance2)
    instance2.follow(instance1)

    inbox_stream = instance2.stream_jsonfeed()
    assert len(inbox_stream["items"]) == 0

    instance1_create_id = instance1.new_note("hello")
    instance2_debug = instance2.debug()
    assert (
        instance2_debug["inbox"] == 3
    )  # An Follow, Accept and Create activity should be there
    assert instance2_debug["outbox"] == 2  # We've sent a Accept and a Follow  activity

    # Ensure the post is visible in instance2's stream
    instance2_inbox_stream = instance2.stream_jsonfeed()
    assert len(instance2_inbox_stream["items"]) == 1
    assert instance2_inbox_stream["items"][0]["id"] == instance1_create_id

    instance2_create_id = instance2.new_note(
        f"hey @instance1@{instance1.docker_url}",
        reply=f"{instance1_create_id}/activity",
    )
    instance2_debug = instance2.debug()
    assert (
        instance2_debug["inbox"] == 3
    )  # An Follow, Accept and Create activity should be there
    assert (
        instance2_debug["outbox"] == 3
    )  # We've sent a Accept and a Follow and a Create  activity

    instance1_debug = instance1.debug()
    assert (
        instance1_debug["inbox"] == 3
    )  # An Follow, Accept and Create activity should be there
    assert (
        instance1_debug["outbox"] == 3
    )  # We've sent a Accept and a Follow and a Create  activity

    instance1_inbox_stream = instance1.stream_jsonfeed()
    assert len(instance1_inbox_stream["items"]) == 1
    assert instance1_inbox_stream["items"][0]["id"] == instance2_create_id

    instance1_note = instance1.outbox_get(f"{instance1_create_id}/activity")
    assert "replies" in instance1_note
    assert instance1_note["replies"]["totalItems"] == 1

    instance2.delete(f"{instance2_create_id}/activity")

    instance1_debug = instance1.debug()
    assert (
        instance1_debug["inbox"] == 4
    )  # An Follow, Accept and Create and Delete activity should be there
    assert (
        instance1_debug["outbox"] == 3
    )  # We've sent a Accept and a Follow and a Create  activity

    instance1_note = instance1.outbox_get(f"{instance1_create_id}/activity")
    assert "replies" in instance1_note
    assert instance1_note["replies"]["totalItems"] == 0
