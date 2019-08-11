import os
from datetime import datetime
from datetime import timezone

from poussetaches import PousseTaches

from utils import parse_datetime

p = PousseTaches(
    os.getenv("MICROBLOGPUB_POUSSETACHES_HOST", "http://localhost:7991"),
    os.getenv("MICROBLOGPUB_INTERNAL_HOST", "http://localhost:5000"),
)


class Tasks:
    @staticmethod
    def cache_object(iri: str) -> None:
        p.push(iri, "/task/cache_object")

    @staticmethod
    def cache_actor(iri: str, also_cache_attachments: bool = True) -> None:
        p.push(
            {"iri": iri, "also_cache_attachments": also_cache_attachments},
            "/task/cache_actor",
        )

    @staticmethod
    def cache_actor_icon(icon_url: str, actor_iri: str):
        p.push({"icon_url": icon_url, "actor_iri": actor_iri}, "/task/cache_actor_icon")

    @staticmethod
    def post_to_remote_inbox(payload: str, recp: str) -> None:
        p.push({"payload": payload, "to": recp}, "/task/post_to_remote_inbox")

    @staticmethod
    def forward_activity(iri: str) -> None:
        p.push(iri, "/task/forward_activity")

    @staticmethod
    def fetch_og_meta(iri: str) -> None:
        p.push(iri, "/task/fetch_og_meta")

    @staticmethod
    def process_new_activity(iri: str) -> None:
        p.push(iri, "/task/process_new_activity")

    @staticmethod
    def cache_attachments(iri: str) -> None:
        p.push(iri, "/task/cache_attachments")

    @staticmethod
    def finish_post_to_inbox(iri: str) -> None:
        p.push(iri, "/task/finish_post_to_inbox")

    @staticmethod
    def finish_post_to_outbox(iri: str) -> None:
        p.push(iri, "/task/finish_post_to_outbox")

    @staticmethod
    def update_question_outbox(iri: str, open_for: int) -> None:
        p.push(
            iri, "/task/update_question", delay=open_for
        )  # XXX: delay expects minutes

    @staticmethod
    def fetch_remote_question(question) -> None:
        now = datetime.now(timezone.utc)
        dt = parse_datetime(question.closed or question.endTime)
        minutes = int((dt - now).total_seconds() / 60)

        if minutes > 0:
            # Only push the task if the poll is not ended yet
            p.push(
                question.id, "/task/fetch_remote_question", delay=minutes
            )  # XXX: delay expects minutes
