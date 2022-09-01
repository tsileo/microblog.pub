SHELL := /bin/bash
PWD=$(shell pwd)

.PHONY: build
build:
	docker build -t microblogpub/microblogpub .

.PHONY: config
config:
	# Run and remove instantly
	-docker run --rm -it --volume `pwd`/data:/app/data microblogpub/microblogpub inv configuration-wizard

.PHONY: update
update:
	-docker run --volume `pwd`/data:/app/data --volume `pwd`/app/static:/app/app/static microblogpub/microblogpub inv update --no-update-deps

.PHONY: prune-old-data
prune-old-data:
	-docker run --volume `pwd`/data:/app/data --volume `pwd`/app/static:/app/app/static microblogpub/microblogpub inv prune-old-data

.PHONY: webfinger
webfinger:
	-docker run --volume `pwd`/data:/app/data --volume `pwd`/app/static:/app/app/static microblogpub/microblogpub inv webfinger $(account)
