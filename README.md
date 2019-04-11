# microblog.pub

<p align="center">
  <img 
    src="https://sos-ch-dk-2.exo.io/microblogpub/microblobpub.png" 
    width="200" height="200" border="0" alt="microblog.pub">
</p>
<p align="center">
<a href="https://travis-ci.org/tsileo/microblog.pub"><img src="https://travis-ci.org/tsileo/microblog.pub.svg?branch=master" alt="Build Status"></a>
<a href="https://matrix.to/#/#microblog.pub:matrix.org"><img src="https://img.shields.io/badge/matrix-%23microblog.pub-blue.svg" alt="#microblog.pub on Matrix"></a>
<a href="https://github.com/tsileo/microblog.pub/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-AGPL_3.0-blue.svg?style=flat" alt="License"></a>
</p>

<p align="center">A self-hosted, single-user, <a href="https://activitypub.rocks">ActivityPub</a> powered microblog.</p>

**Still in early development.**

## Perform the "drop-celery" migration

The project is dropping Celery/RabbitMQ in favor of [poussetaches](https://github.com/tsileo/poussetaches), written specifically for microblog.pub.

First you need to know your Docker compose project name, it should be the name of the project directory, without dot.
If you haven't renamed the directory it should be `microblogpub`.
If you're not sure you can run `$ docker-compose ps`, and get `{project_name}_{service}_{version}` in the output, like `microblogpub_web_1`

Generate a secret for poussetaches (you can use whatever secret you want):

```
$ python -c "import os, binascii; print(binascii.hexlify(os.urandom(32)).decode())"
```

Update the `.env` file and append:

```
POUSSETACHES_AUTH_KEY=yourgeneratedsecret
COMPOSE_PROJECT_NAME=microblogpub
```

Then switch to the `drop-celery` branch:

```
$ git checkout drop-celery
# Build the poussetaches container
$ make poussetaches
$ make update
```

And you should be good!

## Features

 - Implements a basic [ActivityPub](https://activitypub.rocks/) server (with federation)
   - Compatible with [Mastodon](https://github.com/tootsuite/mastodon) and others (Pleroma, Hubzilla...)
   - Also implements a remote follow compatible with Mastodon instances
 - Exposes your outbox as a basic microblog
 - Implements [IndieAuth](https://indieauth.spec.indieweb.org/) endpoints (authorization and token endpoint)
   - U2F support
   - You can use your ActivityPub identity to login to other websites/app
 - Comes with an admin UI with notifications and the stream of people you follow
 - Allows you to attach files to your notes
   - Privacy-aware image upload endpoint that strip EXIF meta data before storing the file
 - No JavaScript, **that's it**. Even the admin UI is pure HTML/CSS
 - Easy to customize (the theme is written Sass)
   - mobile-friendly theme
   - with dark and light version
 - Microformats aware (exports `h-feed`, `h-entry`, `h-cards`, ...)
 - Exports RSS/Atom/[JSON](https://jsonfeed.org/) feeds
    - You stream/timeline is also available in an (authenticated) JSON feed
 - Comes with a tiny HTTP API to help posting new content and and read your inbox/notifications
 - Easy to "cache" (the external/public-facing microblog part)
   - With a good setup, cached content can be served most of the time
   - You can setup a "purge" hook to let you invalidate cache when the microblog was updated
 - Deployable with Docker (Docker compose for everything: dev, test and deployment)
 - Focused on testing
   - The core ActivityPub code/tests are in [Little Boxes](https://github.com/tsileo/little-boxes)
   - Tested against the [official ActivityPub test suite](https://test.activitypub.rocks/) ([report submitted](https://github.com/w3c/activitypub/issues/308))
   - CI runs "federation" tests against two instances
   - Manually tested against [Mastodon](https://github.com/tootsuite/mastodon)
   - Project is running an up-to-date instance

## ActivityPub

microblog.pub implements an [ActivityPub](http://activitypub.rocks/) server, it implements both the client to server API and the federated server to server API.

Activities are verified using HTTP Signatures or by fetching the content on the remote server directly.

## Running your instance

### Installation

```shell
$ git clone https://github.com/tsileo/microblog.pub
$ cd microblog.pub
$ pip install -r requirements.txt
$ make css
$ cp -r config/me.sample.yml config/me.yml
``` 

### Configuration

```shell
$ make password
Password: <enter a password; nothing will show on screen>
$2b$12$iW497g...
```

Edit `config/me.yml` to add the above-generated password, like so:

```
username: 'username'
name: 'Your Name'
icon_url: 'https://you-avatar-url'
domain: 'your-domain.tld'
summary: 'your summary'
https: true
pass: $2b$12$iW497g...
```

### Deployment

Note: some of the docker yml files use version 3 of [docker-compose](https://docs.docker.com/compose/install/).

```shell
$ docker-compose up -d
```

## Development

The most convenient way to hack on microblog.pub is to run the server locally, and run


```shell
# One-time setup
$ pip install -r requirements.txt
# Start the Celery worker, RabbitMQ and MongoDB
$ docker-compose -f docker-compose-dev.yml up -d
# Run the server locally
$ FLASK_DEBUG=1 MICROBLOGPUB_DEBUG=1 FLASK_APP=app.py flask run -p 5005 --with-threads
```

## API

Your admin API key can be found at `config/admin_api_key.key`.

## ActivityPub API

### GET /

Returns the actor profile, with links to all the "standard" collections.

### GET /tags/:tag

Special collection that reference notes with the given tag.

### GET /stream

Special collection that returns the stream/inbox as displayed in the UI.

## User API

The user API is used by the admin UI (and requires a CSRF token when used with a regular user session), but it can also be accessed with an API key.

All the examples are using [HTTPie](https://httpie.org/).

### POST /api/note/delete{?id}

Deletes the given note `id` (the note must from the instance outbox).

Answers a **201** (Created) status code.

You can pass the `id` via JSON, form data or query argument.

#### Example

```shell
$ http POST https://microblog.pub/api/note/delete Authorization:'Bearer <token>' id=http://microblob.pub/outbox/<note_id>/activity
```

#### Response

```json
{
    "activity": "https://microblog.pub/outbox/<delete_id>"
}
```

### POST /api/note/pin{?id}

Adds the given note `id` (the note must from the instance outbox) to the featured collection (and pins it on the homepage).

Answers a **201** (Created) status code.

You can pass the `id` via JSON, form data or query argument.

#### Example

```shell
$ http POST https://microblog.pub/api/note/pin Authorization:'Bearer <token>' id=http://microblob.pub/outbox/<note_id>/activity
```

#### Response

```json
{
    "pinned": true
}
```

### POST /api/note/unpin{?id}

Removes the given note `id` (the note must from the instance outbox) from the featured collection (and un-pins it).

Answers a **201** (Created) status code.

You can pass the `id` via JSON, form data or query argument.

#### Example

```shell
$ http POST https://microblog.pub/api/note/unpin Authorization:'Bearer <token>' id=http://microblob.pub/outbox/<note_id>/activity
```

#### Response

```json
{
    "pinned": false
}
```

### POST /api/like{?id}

Likes the given activity.

Answers a **201** (Created) status code.

You can pass the `id` via JSON, form data or query argument.

#### Example

```shell
$ http POST https://microblog.pub/api/like Authorization:'Bearer <token>' id=http://activity-iri.tld
```

#### Response

```json
{
    "activity": "https://microblog.pub/outbox/<like_id>"
}
```

### POST /api/boost{?id}

Boosts/Announces the given activity.

Answers a **201** (Created) status code.

You can pass the `id` via JSON, form data or query argument.

#### Example

```shell
$ http POST https://microblog.pub/api/boost Authorization:'Bearer <token>' id=http://activity-iri.tld
```

#### Response

```json
{
    "activity": "https://microblog.pub/outbox/<announce_id>"
}
```

### POST /api/block{?actor}

Blocks the given actor, all activities from this actor will be dropped after that.

Answers a **201** (Created) status code.

You can pass the `id` via JSON, form data or query argument.

#### Example

```shell
$ http POST https://microblog.pub/api/block Authorization:'Bearer <token>' actor=http://actor-iri.tld/
```

#### Response

```json
{
    "activity": "https://microblog.pub/outbox/<block_id>"
}
```

### POST /api/follow{?actor}

Follows the given actor.

Answers a **201** (Created) status code.

You can pass the `id` via JSON, form data or query argument.

#### Example

```shell
$ http POST https://microblog.pub/api/follow Authorization:'Bearer <token>' actor=http://actor-iri.tld/
```

#### Response

```json
{
    "activity": "https://microblog.pub/outbox/<follow_id>"
}
```

### POST /api/new_note{?content,reply}

Creates a new note. `reply` is the IRI of the "replied" note if any.

Answers a **201** (Created) status code.

You can pass the `content` and `reply` via JSON, form data or query argument.

#### Example

```shell
$ http POST https://microblog.pub/api/new_note Authorization:'Bearer <token>' content=hello
```

#### Response

```json
{
    "activity": "https://microblog.pub/outbox/<create_id>"
}
```


### GET /api/stream


#### Example

```shell
$ http GET https://microblog.pub/api/stream Authorization:'Bearer <token>'
```

#### Response

```json
```


## Contributions

PRs are welcome, please open an issue to start a discussion before your start any work.
