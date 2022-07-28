# Installing

[TOC]

## Docker edition

Assuming Docker and [Docker Compose](https://docs.docker.com/compose/install/) are already installed.

For now, there's no image published on Docker Hub.

Clone the repository.

```bash
git clone https://git.sr.ht/~tsileo/microblog.pub your-domain.tld
```

Build the Docker image.

```bash
make build
```

Run the configuration wizard.

```bash
make config
```

Build static assets.

```bash
make update
```

Start the app with Docker Compose, it will listen on port 8000 by default.

```bash
docker compose up -d
```

Setup a reverse proxy (see the [Reverse Proxy section](/installing.html#reverse-proxy)).

## Python developer edition

Assuming you have a working **Python 3.10+** environment. 

Setup [Poetry](https://python-poetry.org/docs/master/#installing-with-the-official-installer).

```bash
curl -sSL https://install.python-poetry.org | python3 -
```

Clone the repository.

```bash
git clone https://git.sr.ht/~tsileo/microblog.pub testing.microblog.pub
```

Install deps.

```bash
poetry install
```

Setup config.

```bash
poetry run inv configuration-wizard
```

Grab your virtualenv path.

```bash
poetry env info
```

Run the two processes with supervisord.

```bash
VENV_DIR=/home/ubuntu/.cache/pypoetry/virtualenvs/microblogpub-chx-y1oE-py3.10 poetry run supervisord -c misc/supervisord.conf -n
```

Setup a reverse proxy (see the next section).

## Reverse proxy

You will also want to setup a reverse proxy like Nginx, see [uvicorn documentation](https://www.uvicorn.org/deployment/#running-behind-nginx):

```nginx
server {
    client_max_body_size 4G;

    location / {
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_redirect off;
      proxy_buffering off;
      proxy_pass http://localhost:8000;
    }

    # [...]
}

```
