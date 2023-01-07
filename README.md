# microblog.pub

A self-hosted, single-user, ActivityPub powered microblog.

[![builds.sr.ht status](https://builds.sr.ht/~tsileo/microblog.pub.svg)](https://builds.sr.ht/~tsileo/microblog.pub?)
[![AGPL 3.0](https://img.shields.io/badge/license-AGPL_3.0-blue.svg?style=flat)](https://git.sr.ht/~tsileo/microblog.pub/tree/v2/item/LICENSE)

Instances in the wild:

 - [microblog.pub](https://microblog.pub/) (follow to get updated about the project)
 - [hexa.ninja](https://hexa.ninja) (theme customization example)
 - [testing.microblog.pub](https://testing.microblog.pub/)
 - [Irish Left Archive](https://posts.leftarchive.ie/) (another theme customization example)

There are still some rough edges, but the server is mostly functional.

## Features

 - Implements the [ActivityPub](https://activitypub.rocks/) server to server protocol
    - Federate with all the other popular ActivityPub servers like Pleroma, PixelFed, PeerTube, Mastodon...
    - Consume most of the content types available (notes, articles, videos, pictures...)
 - Exposes your ActivityPub profile as a minimalist microblog
    - Author notes in Markdown, with code highlighting support
    - Dedicated section for articles/blog posts (enabled when the first article is posted)
 - Lightweight
    - Uses SQLite, and Python 3.10+
    - Can be deployed on small VPS
 - Privacy-aware
    - EXIF metadata (like GPS location) are stripped before storage
    - Every media is proxied through the server
    - Strict access control for your outbox enforced via HTTP signature
 - **No** Javascript
    - The UI is pure HTML/CSS
    - Except tiny bits of hand-written JS in the note composer to insert emoji and add alt text to images
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

Contributions are welcomed, check out the [contributing section of the documentation](https://docs.microblog.pub/developer_guide.html#contributing) for more details.


## License

The project is licensed under the GNU AGPL v3 LICENSE (see the LICENSE file).
