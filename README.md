# microblog.pub

[![builds.sr.ht status](https://builds.sr.ht/~tsileo/microblog.pub.svg)](https://builds.sr.ht/~tsileo/microblog.pub?)
[![AGPL 3.0](https://img.shields.io/badge/license-AGPL_3.0-blue.svg?style=flat)](https://git.sr.ht/~tsileo/microblog.pub/tree/v2/item/LICENSE)

This branch is a complete rewrite of the original microblog.pub server.

Check out the test instance here: [testing.microblog.pub](https://testing.microblog.pub/).

The original server became hard to debug, maintain and is not super easy to deploy (due to the dependecies like MongoDB).

This rewrite is built using "modern" Python 3.10, SQLite and does not need any external tasks queue service.

It is still in early development, this README will be updated when I get to deploy a personal instance in the wild.
