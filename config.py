import os
import yaml
from pymongo import MongoClient
import requests

from utils.key import Key
from utils.actor_service import ActorService
from utils.object_service import ObjectService


VERSION = '1.0.0'

CTX_AS = 'https://www.w3.org/ns/activitystreams'
CTX_SECURITY = 'https://w3id.org/security/v1'
AS_PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
HEADERS = [
    'application/activity+json',
    'application/ld+json;profile=https://www.w3.org/ns/activitystreams',
    'application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
    'application/ld+json',
]


with open('config/me.yml') as f:
    conf = yaml.load(f)

    USERNAME = conf['username']
    NAME = conf['name']
    DOMAIN = conf['domain']
    SCHEME = 'https' if conf.get('https', True) else 'http'
    BASE_URL = SCHEME + '://' + DOMAIN
    ID = BASE_URL
    SUMMARY = conf['summary']
    ICON_URL = conf['icon_url']
    PASS = conf['pass']
    PUBLIC_INSTANCES = conf.get('public_instances')

USER_AGENT = (
        f'{requests.utils.default_user_agent()} '
        f'(microblog.pub/{VERSION}; +{BASE_URL})'
)

# TODO(tsileo): use 'mongo:27017;
# mongo_client = MongoClient(host=['mongo:27017'])
mongo_client = MongoClient(
        host=[os.getenv('MICROBLOGPUB_MONGODB_HOST', 'localhost:27017')],
)

DB = mongo_client['{}_{}'.format(USERNAME, DOMAIN.replace('.', '_'))]
KEY = Key(USERNAME, DOMAIN, create=True)

ME = {
    "@context": [
        CTX_AS,
        CTX_SECURITY,
    ],
    "type": "Person",
    "id": ID,
    "following": ID+"/following",
    "followers": ID+"/followers",
    "liked": ID+"/liked",
    "inbox": ID+"/inbox",
    "outbox": ID+"/outbox",
    "preferredUsername": USERNAME,
    "name": NAME,
    "summary": SUMMARY,
    "endpoints": {},
    "url": ID,
    "icon": {
        "mediaType": "image/png",
        "type": "Image",
        "url": ICON_URL,
    },
    "publicKey": {
        "id": ID+"#main-key",
        "owner": ID,
        "publicKeyPem": KEY.pubkey_pem,
    },
}
print(ME)

ACTOR_SERVICE = ActorService(USER_AGENT, DB.actors_cache, ID, ME, DB.instances)
OBJECT_SERVICE = ObjectService(USER_AGENT, DB.objects_cache, DB.inbox, DB.outbox, DB.instances)
