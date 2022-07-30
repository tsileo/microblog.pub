# Installing

[TOC]

## Docker edition

Assuming Docker and [Docker Compose](https://docs.docker.com/compose/install/) are already installed.

For now, there's no image published on Docker Hub, this means you will have to build the image locally.

Clone the repository, replace `you-domain.tld` by your own domain.

Note that if you want to serve static assets via your reverse proxy (like nginx), clone it in a place
where accessible by your reverse proxy user.

```bash
git clone https://git.sr.ht/~tsileo/microblog.pub your-domain.tld
```

Build the Docker image locally.

```bash
make build
```

Run the configuration wizard.

```bash
make config
```

Update `data/profile.toml` and add this line in order to process headers from the reverse proxy:

```toml
trusted_hosts = ["*"]
```

Start the app with Docker Compose, it will listen on port 8000 by default.
The port can be tweaked in the `docker-compose.yml` file.

```bash
docker compose up -d
```

Setup a reverse proxy (see the [Reverse Proxy section](/installing.html#reverse-proxy)).

### Updating 

To update microblogpub, pull the latest changes, rebuild the Docker image and restart the process with `docker compose`.

```bash
git pull
make build
docker compose stop
docker compose up -d
```

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

### Updating 

To update microblogpub locally, pull the remote changes and run the `update` task to regeneratee the CSS and run any DB migrations.

```bash
git pull
poetry run inv update
```

## Reverse proxy

You will also want to setup a reverse proxy like NGINX, see [uvicorn documentation](https://www.uvicorn.org/deployment/#running-behind-nginx):

If you don't have a reverse proxy setup yet, [NGINX + certbot](https://www.nginx.com/blog/using-free-ssltls-certificates-from-lets-encrypt-with-nginx/) is recommended.

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

Optionally, you can serve static files using NGINX directly, with an additional `location` block.
This will require the NGINX user to have access to the `static/` directory.

```nginx
server {
    # [...]

    location / {
        # [...]
    }

    location /static {
       # path for static files
       rewrite ^/static/(.*) /$1 break;
       root /path/to/your-domain.tld/app/static/;
    }

    # [...]
}
```
