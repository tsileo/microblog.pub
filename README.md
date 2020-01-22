# microblog.pub

<p align="center">
  <img 
    src="https://sos-ch-dk-2.exo.io/microblogpub/microblobpub.png" 
    width="200" height="200" border="0" alt="microblog.pub">
</p>
<p align="center">A self-hosted, single-user, <a href="https://activitypub.rocks">ActivityPub</a> powered microblog.</p>
<p align="center">
<a href="https://d.a4.io/tsileo/microblog.pub"><img src="https://d.a4.io/api/badges/tsileo/microblog.pub/status.svg" alt="Build Status"></a>
<a href="https://matrix.to/#/#microblog.pub:matrix.org"><img src="https://img.shields.io/badge/matrix-%23microblog.pub-blue.svg" alt="#microblog.pub on Matrix"></a>
<a href="https://github.com/tsileo/microblog.pub/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-AGPL_3.0-blue.svg?style=flat" alt="License"></a>
<a href="https://github.com/ambv/black"><img alt="Code style: black" src="https://img.shields.io/badge/code%20style-black-000000.svg"></a>
</p>

**Still in early development/I do not recommend to run an instance yet.**

<!-- start -->

## Features

 - Implements a basic [ActivityPub](https://activitypub.rocks/) server (with federation)
   - S2S (Server to Server) and C2S (Client to Server) protocols
   - Compatible with [Mastodon](https://joinmastodon.org/) and others ([Pleroma](https://pleroma.social/), Misskey, Plume, PixelFed, Hubzilla...)
 - Exposes your outbox as a basic microblog
   - Support all content types from the Fediverse (`Note`, `Article`, `Page`, `Video`, `Image`, `Question`...)
   - Markdown support
   - Server-side code syntax highlighting
 - Comes with an admin UI with notifications and the stream of people you follow
   - Private "bookmark" support
   - List support
   - Allows you to attach files to your notes
   - Custom emojis support
 - Cares about your privacy
   - The image upload endpoint strips EXIF meta data before storing the file
   - Every attachment/media is cached (or proxied) by the server
 - No JavaScript, **that's it**. Even the admin UI is pure HTML/CSS
   - (well except for the Emoji picker within the admin, but it's only few line of hand-written JavaScript)
 - Easy to customize (the theme is written Sass)
   - mobile-friendly theme
   - with dark and light version
 - IndieWeb citizen
   - Microformats aware (exports `h-feed`, `h-entry`, `h-cards`, ...)
     - Export a feed in the HTML that is WebSub compatible
   - Partial [Micropub](https://www.w3.org/TR/micropub/) support ([implementation report](https://micropub.rocks/implementation-reports/servers/416/s0BDEXZiX805btoa47sz))
   - Implements [IndieAuth](https://indieauth.spec.indieweb.org/) endpoints (authorization and token endpoint)
     - You can use your ActivityPub identity to login to other websites/app (with U2F support)
   - Send [Webmentions](https://www.w3.org/TR/webmention/) to linked website (only for public notes)
   - Exports RSS/Atom/[JSON](https://jsonfeed.org/) feeds
    - You stream/timeline is also available in an (authenticated) JSON feed
  - Comes with a tiny HTTP API to help posting new content and and read your inbox/notifications
 - Deployable with Docker (Docker compose for everything: dev, test and deployment)
 - Focused on testing
   - Tested against the [official ActivityPub test suite](https://test.activitypub.rocks/), see [the results](https://activitypub.rocks/implementation-report/)
   - [CI runs "federation" tests against two instances](https://d.a4.io/tsileo/microblog.pub)
   - Project is running 2 up-to-date instances ([here](https://microblog.pub) and [there](https://a4.io))
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

Once the initial configuration is done, you can still tweak the config by editing `config/me.yml` directly.


### Deployment

To spawn the docker-compose project (running this command will also update _microblog.pub_ to latest and restart everything if it's already running):

```shell
$ make run
```

By default, the server will listen on `localhost:5005` (http://localhost:5005 should work if you're running locally).

For production, you need to setup a reverse proxy (nginx, caddy) to forward your domain to the local server 
(and check [certbot](https://certbot.eff.org/) for getting a free TLS certificate).

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
