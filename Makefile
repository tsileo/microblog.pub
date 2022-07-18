SHELL := /bin/bash
PWD=$(shell pwd)

.PHONY: config
config:
	# Run and remove instantly
	-docker run --rm -it --volume `pwd`/data:/app/data microblogpub/microblogpub inv configuration-wizard
