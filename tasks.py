import os
import json
import logging
import random

import requests
from celery import Celery
from requests.exceptions import HTTPError

from config import HEADERS
from config import ID
from config import DB
from config import KEY
from config import USER_AGENT
from utils.httpsig import HTTPSigAuth
from utils.opengraph import fetch_og_metadata
from utils.linked_data_sig import generate_signature


log = logging.getLogger(__name__)
app = Celery('tasks', broker=os.getenv('MICROBLOGPUB_AMQP_BROKER', 'pyamqp://guest@localhost//'))
# app = Celery('tasks', broker='pyamqp://guest@rabbitmq//')
SigAuth = HTTPSigAuth(ID+'#main-key', KEY.privkey)


@app.task(bind=True, max_retries=12)
def post_to_inbox(self, payload: str, to: str) -> None:
    try:
        log.info('payload=%s', payload)
        log.info('generating sig')
        signed_payload = json.loads(payload)
        generate_signature(signed_payload, KEY.privkey)
        log.info('to=%s', to)
        resp = requests.post(to, data=json.dumps(signed_payload), auth=SigAuth, headers={
            'Content-Type': HEADERS[1],
            'Accept': HEADERS[1],
            'User-Agent': USER_AGENT,    
        })
        log.info('resp=%s', resp)
        log.info('resp_body=%s', resp.text)
        resp.raise_for_status()
    except HTTPError as err:
        log.exception('request failed')
        if 400 >= err.response.status_code >= 499:
            log.info('client error, no retry')
            return
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))


@app.task(bind=True, max_retries=12)
def fetch_og(self, col, remote_id):
    try:
        log.info('fetch_og_meta remote_id=%s col=%s', remote_id, col)
        if col == 'INBOX':
            log.info('%d links saved', fetch_og_metadata(USER_AGENT, DB.inbox, remote_id))
        elif col == 'OUTBOX':
            log.info('%d links saved', fetch_og_metadata(USER_AGENT, DB.outbox, remote_id))
    except Exception as err:
        self.log.exception('failed')
        self.retry(exc=err, countdown=int(random.uniform(2, 4) ** self.request.retries))
