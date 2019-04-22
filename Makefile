PYTHON=python
SETUP_WIZARD_IMAGE=microblogpub-setup-wizard:latest
PWD=$(shell pwd)

.PHONY: config
config:
	# Build the container for the setup wizard on-the-fly
	cd setup_wizard && docker build . -t $(SETUP_WIZARD_IMAGE)
	# Run and remove instantly
	-docker run --rm -it --volume $(PWD):/app/out $(SETUP_WIZARD_IMAGE)
	# Finally, remove the tagged image
	docker rmi $(SETUP_WIZARD_IMAGE)

.PHONY: reload-fed
reload-fed:
	docker build . -t microblogpub:latest
	docker-compose -p instance2 -f docker-compose-tests.yml stop
	docker-compose -p instance1 -f docker-compose-tests.yml stop
	WEB_PORT=5006 CONFIG_DIR=./tests/fixtures/instance1/config docker-compose -p instance1 -f docker-compose-tests.yml up -d --force-recreate --build
	WEB_PORT=5007 CONFIG_DIR=./tests/fixtures/instance2/config docker-compose -p instance2 -f docker-compose-tests.yml up -d --force-recreate --build

.PHONY: poussetaches
poussetaches:
	git clone https://github.com/tsileo/poussetaches.git pt && cd pt && docker build . -t poussetaches:latest && cd - && rm -rf pt

.PHONY: reload-dev
reload-dev:
	docker build . -t microblogpub:latest
	docker-compose -f docker-compose-dev.yml up -d --force-recreate

.PHONY: run
run:
	git pull
	docker build . -t microblogpub:latest
	docker-compose stop
	docker-compose up -d --force-recreate --build
