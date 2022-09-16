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

.PHONY: move-to
move-to:
	-docker run --volume `pwd`/data:/app/data --volume `pwd`/app/static:/app/app/static microblogpub/microblogpub inv move-to $(account)

.PHONY: self-destruct
move-to:
	-docker run --volume `pwd`/data:/app/data --volume `pwd`/app/static:/app/app/static microblogpub/microblogpub inv self-destruct

.PHONY: reset-password
reset-password:
	-docker run --volume `pwd`/data:/app/data --volume `pwd`/app/static:/app/app/static microblogpub/microblogpub inv reset-password

.PHONY: check-config
check-config:
	-docker run --volume `pwd`/data:/app/data --volume `pwd`/app/static:/app/app/static microblogpub/microblogpub inv check-config

.PHONY: compile-scss
compile-scss:
	-docker run --volume `pwd`/data:/app/data --volume `pwd`/app/static:/app/app/static microblogpub/microblogpub inv compile-scss
