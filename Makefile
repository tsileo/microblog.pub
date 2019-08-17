SHELL := /bin/bash
PYTHON=python
SETUP_WIZARD_IMAGE=microblogpub-setup-wizard:latest
PWD=$(shell pwd)

# Build the config (will error if an existing config/me.yml is found) via a Docker container
.PHONY: config
config:
	# Build the container for the setup wizard on-the-fly
	cd setup_wizard && docker build . -t $(SETUP_WIZARD_IMAGE)
	# Run and remove instantly
	-docker run -e MICROBLOGPUB_WIZARD_PROJECT_NAME --rm -it --volume $(PWD):/app/out $(SETUP_WIZARD_IMAGE)
	# Finally, remove the tagged image
	docker rmi $(SETUP_WIZARD_IMAGE)

# Reload the federation test instances (for local dev)
.PHONY: reload-fed
reload-fed:
	docker build . -t microblogpub:latest
	docker-compose -p instance2 -f docker-compose-tests.yml stop
	docker-compose -p instance1 -f docker-compose-tests.yml stop
	WEB_PORT=5006 CONFIG_DIR=./tests/fixtures/instance1/config docker-compose -p instance1 -f docker-compose-tests.yml up -d --force-recreate --build
	WEB_PORT=5007 CONFIG_DIR=./tests/fixtures/instance2/config docker-compose -p instance2 -f docker-compose-tests.yml up -d --force-recreate --build

# Reload the local dev instance
.PHONY: reload-dev
reload-dev:
	docker build . -t microblogpub:latest
	docker-compose -f docker-compose-dev.yml up -d --force-recreate

# Build the microblogpub Docker image
.PHONY: microblogpub
microblogpub:
	# Update microblog.pub
	git pull
	# Rebuild the Docker image
	docker build . --no-cache -t microblogpub:latest

.PHONY: css
css:
	# Download pure.css if needed
	if [[ ! -f static/pure.css ]]; then curl https://unpkg.com/purecss@1.0.1/build/pure-min.css > static/pure.css; fi
	# Download the emojis from twemoji if needded
	if [[ ! -d static/twemoji ]]; then wget https://github.com/twitter/twemoji/archive/v12.1.2.tar.gz && tar xvzf v12.1.2.tar.gz  && mv twemoji-12.1.2/assets/svg static/twemoji && rm -rf twemoji-12.1.2 && rm -f v12.1.2.tar.gz; fi

# Run the docker-compose project locally (will perform a update if the project is already running)
.PHONY: run
run: microblogpub css
	# (poussetaches and microblogpub Docker image will updated)
	# Update MongoDB
	docker pull mongo:3
	docker pull poussetaches/poussetaches
	# Restart the project
	docker-compose stop
	docker-compose up -d --force-recreate --build
