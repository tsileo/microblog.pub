## Python developer edition

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
