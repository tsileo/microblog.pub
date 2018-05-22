css:
	python -c "import sass; sass.compile(dirname=('sass', 'static/css'), output_style='compressed')"

password:
	python -c "import bcrypt; from getpass import getpass; print(bcrypt.hashpw(getpass().encode('utf-8'), bcrypt.gensalt()).decode('utf-8'))"

update:
	docker-compose stop
	git pull
	docker-compose up -d --force-recreate --build
