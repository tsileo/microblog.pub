PYTHON=python

css:
	$(PYTHON) -c "import sass; sass.compile(dirname=('sass', 'static/css'), output_style='compressed')"

password:
	$(PYTHON) -c "import bcrypt; from getpass import getpass; print(bcrypt.hashpw(getpass().encode('utf-8'), bcrypt.gensalt()).decode('utf-8'))"

docker:
	mypy . --ignore-missing-imports
	docker build . -t microblogpub:latest

reload-fed:
	docker-compose -p instance2 -f docker-compose-tests.yml stop
	docker-compose -p instance1 -f docker-compose-tests.yml stop
	WEB_PORT=5006 CONFIG_DIR=./tests/fixtures/instance1/config docker-compose -p instance1 -f docker-compose-tests.yml up -d --force-recreate --build
	WEB_PORT=5007 CONFIG_DIR=./tests/fixtures/instance2/config docker-compose -p instance2 -f docker-compose-tests.yml up -d --force-recreate --build

update:
	docker-compose stop
	git pull
	docker build . -t microblogpub:latest
	docker-compose up -d --force-recreate --build
