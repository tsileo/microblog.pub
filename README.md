# microblog.pub

<p align="center">
  <img 
    src="https://sos-ch-dk-2.exo.io/microblogpub/microblobpub.png" 
    width="200" height="200" border="0" alt="microblog.pub">
</p>
<p align="center">
<a href="https://d.a4.io/tsileo/microblog.pub"><img src="https://d.a4.io/api/badges/tsileo/microblog.pub/status.svg" alt="Build Status"></a>
<a href="https://matrix.to/#/#microblog.pub:matrix.org"><img src="https://img.shields.io/badge/matrix-%23microblog.pub-blue.svg" alt="#microblog.pub on Matrix"></a>
<a href="https://github.com/tsileo/microblog.pub/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-AGPL_3.0-blue.svg?style=flat" alt="License"></a>
<a href="https://github.com/ambv/black"><img alt="Code style: black" src="https://img.shields.io/badge/code%20style-black-000000.svg"></a>
</p>


<p align="center">A self-hosted, single-user, <a href="https://activitypub.rocks">ActivityPub</a> powered microblog.</p>

**Still in early development.**

## /!\ Note to adventurer

If you are running an instance with Celery/RabbitMQ, you will need to [perform a migration](https://github.com/tsileo/microblog.pub/tree/drop-celery#perform-the-drop-celery-migration).

Getting closer to a stable release, it should be the "last" migration.

## Features

 - Implements a basic [ActivityPub](https://activitypub.rocks/) server (with federation)
   - Compatible with [Mastodon](https://joinmastodon.org/) and others ([Pleroma](https://pleroma.social/), Hubzilla...)
   - Also implements a remote follow compatible with Mastodon instances
 - Exposes your outbox as a basic microblog
   - Support all content types from the Fediverse (`Note`, `Article`, `Page`, `Video`, `Image`, `Question`...)
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
 - Deployable with Docker (Docker compose for everything: dev, test and deployment)
 - Implements [IndieAuth](https://indieauth.spec.indieweb.org/) endpoints (authorization and token endpoint)
   - U2F support
   - You can use your ActivityPub identity to login to other websites/app
 - Focused on testing
   - Tested against the [official ActivityPub test suite](https://test.activitypub.rocks/) ([report submitted](https://github.com/w3c/activitypub/issues/308))
   - [CI runs "federation" tests against two instances](https://d.a4.io/tsileo/microblog.pub)
   - Project is running 2 up-to-date instances ([here](https://microblog.pub) and [there](https://a4.io))
   - The core ActivityPub code/tests are in [Little Boxes](https://github.com/tsileo/little-boxes) (but needs some cleanup)
   - Manually tested against other major platforms


## User Guide

Remember that _microblog.pub_ is still in early development.

The easiest and recommended way to run _microblog.pub_ in production is to use the provided docker-compose config.

First install [Docker](https://docs.docker.com/install/) and [Docker Compose](https://docs.docker.com/compose/install/).
Python is not needed on the host system.

Note that all the generated data (config included) will be stored on the host (i.e. not only in Docker) in `config/` and `data/`.


### Installation

```shell
$ git clone https://github.com/tsileo/microblog.pub
$ cd microblog.pub
$ make config
``` 

### Deployment

To spawn the docker-compose project (running this command will also update _microblog.pub_ to latest and restart the project it it's already running):

```shell
$ make run
```

### HTTP API

See [docs/api.md](docs/api.md) for the internal HTTP API documentation.

### Backup

The easiest way to backup all of your data is to backup the `microblog.pub/` directory directly (that's what I do and I have been able to restore super easily).
It should be safe to copy the directory while the Docker compose project is running.

## Development

The project requires Python3.7+.

The most convenient way to hack on _microblog.pub_ is to run the Python server on the host directly, and evetything else in Docker.

```shell
# One-time setup (in a new virtual env)
$ pip install -r requirements.txt
# Start MongoDB and poussetaches
$ make poussetaches
$ env POUSSETACHES_AUTH_KEY="<secret-key>" docker-compose -f docker-compose-dev.yml up -d
# Run the server locally
$ FLASK_DEBUG=1 MICROBLOGPUB_DEBUG=1 FLASK_APP=app.py POUSSETACHES_AUTH_KEY="<secret-key>" flask run -p 5005 --with-threads
```


## Contributions

Contributions/PRs are welcome, please open an issue to start a discussion before your start any work.
