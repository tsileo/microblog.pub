import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List

import flask
import requests

POUSSETACHES_AUTH_KEY = os.getenv("POUSSETACHES_AUTH_KEY")


@dataclass
class Task:
    req_id: str
    tries: int

    payload: Any


@dataclass
class GetTask:
    payload: Any
    expected: int
    schedule: str
    task_id: str
    next_run: datetime
    tries: int
    url: str
    last_error_status_code: int
    last_error_body: str


class PousseTaches:
    def __init__(self, api_url: str, base_url: str) -> None:
        self.api_url = api_url
        self.base_url = base_url

    def push(
        self,
        payload: Any,
        path: str,
        expected: int = 200,
        schedule: str = "",
        delay: int = 0,
    ) -> str:
        # Encode our payload
        p = base64.b64encode(json.dumps(payload).encode()).decode()

        # Queue/push it
        resp = requests.post(
            self.api_url,
            json={
                "url": self.base_url + path,
                "payload": p,
                "expected": expected,
                "schedule": schedule,
                "delay": delay,
            },
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

        return Task(
            req_id=envelope["req_id"], tries=envelope["tries"], payload=payload
        )  # type: ignore

    @staticmethod
    def _expand_task(t: Dict[str, Any]) -> None:
        try:
            t["payload"] = json.loads(base64.b64decode(t["payload"]))
        except json.JSONDecodeError:
            t["payload"] = base64.b64decode(t["payload"]).decode()

        if t["last_error_body"]:
            t["last_error_body"] = base64.b64decode(t["last_error_body"]).decode()

        t["next_run"] = datetime.fromtimestamp(float(t["next_run"] / 1e9))
        if t["last_run"]:
            t["last_run"] = datetime.fromtimestamp(float(t["last_run"] / 1e9))
        else:
            del t["last_run"]

    def _get(self, where: str) -> List[GetTask]:
        out = []

        resp = requests.get(self.api_url + f"/{where}")
        resp.raise_for_status()
        dat = resp.json()
        for t in dat["tasks"]:
            self._expand_task(t)
            out.append(
                GetTask(
                    task_id=t["id"],
                    payload=t["payload"],
                    expected=t["expected"],
                    schedule=t["schedule"],
                    tries=t["tries"],
                    url=t["url"],
                    last_error_status_code=t["last_error_status_code"],
                    last_error_body=t["last_error_body"],
                    next_run=t["next_run"],
                )
            )

        return out

    def get_cron(self) -> List[GetTask]:
        return self._get("cron")

    def get_success(self) -> List[GetTask]:
        return self._get("success")

    def get_waiting(self) -> List[GetTask]:
        return self._get("waiting")

    def get_dead(self) -> List[GetTask]:
        return self._get("dead")
