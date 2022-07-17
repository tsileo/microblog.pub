# microblog.pub

A self-hosted, single-user, ActivityPub powered microblog.

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
    - [IndieAuth](https://www.w3.org/TR/indieauth/) support (OAuth2 extension)
    - [Microformats](http://microformats.org/wiki/Main_Page) everywhere
    - [Micropub](https://www.w3.org/TR/micropub/) support
    - Sends and processes [Webmentions](https://www.w3.org/TR/webmention/)
    - RSS/Atom/[JSON](https://www.jsonfeed.org/) feed
 - Easy to backup
    - Everything is stored in the `data/` directory: config, uploads, secrets and the SQLite database.

## Getting started

Check out the [online documentation](https://docs.microblog.pub).   

## Credits

 - Emoji from [Twemoji](https://twemoji.twitter.com/)
 - Awesome custom goose emoji from [@pamela@bsd.network](https://bsd.network/@pamela)


## Contributing

All the development takes place on [sourcehut](https://sr.ht/~tsileo/microblog.pub/), GitHub is only used as a mirror:

 - [Project](https://sr.ht/~tsileo/microblog.pub/)
 - [Issue tracker](https://todo.sr.ht/~tsileo/microblog.pub)
 - [Mailing list](https://sr.ht/~tsileo/microblog.pub/lists)

Contributions are welcomed, check out the [documentation](https://docs.microblog.pub) for more details.


## License

The project is licensed under the GNU AGPL v3 LICENSE (see the LICENSE file).
