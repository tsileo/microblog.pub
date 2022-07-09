# microblog.pub

[![builds.sr.ht status](https://builds.sr.ht/~tsileo/microblog.pub.svg)](https://builds.sr.ht/~tsileo/microblog.pub?)
[![AGPL 3.0](https://img.shields.io/badge/license-AGPL_3.0-blue.svg?style=flat)](https://git.sr.ht/~tsileo/microblog.pub/tree/v2/item/LICENSE)

This branch is a complete rewrite of the original microblog.pub server.

Check out the test instance here: [testing.microblog.pub](https://testing.microblog.pub/).

The original server became hard to debug, maintain and is not super easy to deploy (due to the dependecies like MongoDB).

This rewrite is built using "modern" Python 3.10, SQLite and does not need any external tasks queue service.

It is still in early development, this README will be updated when I get to deploy a personal instance in the wild.

## Features

 - Implements the [ActivityPub](https://activitypub.rocks/) server to server protocol
    - Federate with all the other popular ActivityPub servers like Pleroma, PixelFed, PeerTube, Mastodon...
    - Consume most of the content types available (notes, articles, videos, pictures...)
 - Exposes your ActivityPub profile as a minimalist microblog
    - Author notes in Markdown, with code highlighting support
 - Lightweight
    - Can be deployed on small VPS
 - Privacy-aware
    - EXIF metadata (like GPS location) are stripped before storage
    - Every media is proxied through the server
    - Strict access control for your outbox enforced via HTTP signature
 - **No** Javascript
    - The UI is pure HTML/CSS
    - Except a tiny bit of hand-written JS in the note composer to insert emoji
 - IndieWeb citizen
    - Microformats everywhere
    - Webmentions support
    - RSS/Atom/JSON feed
