# microblog.pub

<p align="center">
  <img 
    src="https://sos-ch-dk-2.exo.io/microblogpub/microblobpub.png" 
    width="200" height="200" border="0" alt="microblog.pub">
</p>
<p align="center">
<a href="https://github.com/tsileo/microblog.pub/releases"><img src="https://img.shields.io/badge/version-1.0.0-green.svg?" alt="Version"></a>
<a href="https://travis-ci.org/tsileo/microblog.pub"><img src="https://travis-ci.org/tsileo/microblog.pub.svg?branch=master" alt="Build Status"></a>
<a href="https://github.com/tsileo/microblog.pub/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-AGPL_3.0-green.svg?style=flat" alt="License"></a>
</p>

<p align="center">A self-hosted, single-user, <a href="https://activitypub.rocks">ActivityPub</a> powered microblog.</p>

## Features

 - Implements a basic [ActivityPub](https://activitypub.rocks/) server (with federation)
   - Compatible with [Mastodon](https://github.com/tootsuite/mastodon) and others (Pleroma, Hubzilla...)
   - Also implements a remote follow compatible with Mastodon instances
 - Expose your outbox as a basic microblog
 - [IndieAuth](https://indieauth.spec.indieweb.org/) endpoints (authorization and token endpoint)
   - U2F support
   - You can use your ActivityPub identity to login to other websites/app
 - Admin UI with notifications and the stream of people you follow
 - Attach files to your notes
   - Privacy-aware upload that strip EXIF meta data before storing the file
 - No JavaScript, that's it, even the admin UI is pure HTML/CSS
 - Easy to customize (the theme is written Sass)
 - Microformats aware (exports `h-feed`, `h-entry`, `h-cards`, ...)
 - Exports RSS/Atom feeds
 - Comes with a tiny HTTP API to help posting new content and performing basic actions
 - Deployable with Docker

## Running your instance

### Installation

```shell
$ git clone
$ make css
``` 

### Configuration

```shell
$ make password
```

### Deployment

```shell
$ docker-compose up -d
```

You should use a reverse proxy...

## Development

The most convenient way to hack on microblog.pub is to run the server locally, and run


```shell
# One-time setup
$ pip install -r requirements.txt
# Start the Celery worker, RabbitMQ and MongoDB
$ docker-compose -f docker-compose-dev.yml up -d
# Run the server locally
$ FLASK_APP=app.py flask run -p 5005 --with-threads
```

## Contributions

PRs are welcome, please open an issue to start a discussion before your start any work.
