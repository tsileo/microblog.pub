import base64
import json
import os
from typing import Any
from dataclasses import dataclass
import flask
import requests

POUSSETACHES_AUTH_KEY = os.getenv("POUSSETACHES_AUTH_KEY")


@dataclass
class Task:
    req_id: str
    tries: int

    payload: Any


class PousseTaches:
    def __init__(self, api_url: str, base_url: str) -> None:
        self.api_url = api_url
        self.base_url = base_url

    def push(self, payload: Any, path: str, expected=200) -> str:
        # Encode our payload
        p = base64.b64encode(json.dumps(payload).encode()).decode()

        # Queue/push it
        resp = requests.post(
            self.api_url,
            json={"url": self.base_url + path, "payload": p, "expected": expected},
        )
        resp.raise_for_status()

        return resp.headers["Poussetaches-Task-ID"]

    def parse(self, req: flask.Request) -> Task:
        if req.headers.get("Poussetaches-Auth-Key") != POUSSETACHES_AUTH_KEY:
            raise ValueError("Bad auth key")

        # Parse the "envelope"
        envelope = json.loads(req.data)
        print(req)
        print(f"envelope={envelope!r}")
        payload = json.loads(base64.b64decode(envelope["payload"]))

        return Task(req_id=envelope["req_id"], tries=envelope["tries"], payload=payload)
