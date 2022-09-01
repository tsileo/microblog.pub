import asyncio
import io
import tarfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from typing import Optional

import httpx
from invoke import Context  # type: ignore
from invoke import run  # type: ignore
from invoke import task  # type: ignore


@task
def generate_db_migration(ctx, message):
    # type: (Context, str) -> None
    run(f'alembic revision --autogenerate -m "{message}"', echo=True)


@task
def migrate_db(ctx):
    # type: (Context) -> None
    run("alembic upgrade head", echo=True)


@task
def autoformat(ctx):
    # type: (Context) -> None
    run("black .", echo=True)
    run("isort -sl .", echo=True)


@task
def lint(ctx):
    # type: (Context) -> None
    run("black --check .", echo=True)
    run("isort -sl --check-only .", echo=True)
    run("flake8 .", echo=True)
    run("mypy .", echo=True)


@task
def compile_scss(ctx, watch=False):
    # type: (Context, bool) -> None
    from app.utils.favicon import build_favicon

    build_favicon()
    theme_file = Path("data/_theme.scss")
    if not theme_file.exists():
        theme_file.write_text("// override vars for theming here")

    if watch:
        run("boussole watch", echo=True)
    else:
        run("boussole compile", echo=True)


@task
def uvicorn(ctx):
    # type: (Context) -> None
    run("uvicorn app.main:app --no-server-header", pty=True, echo=True)


@task
def process_outgoing_activities(ctx):
    # type: (Context) -> None
    from app.outgoing_activities import loop

    asyncio.run(loop())


@task
def process_incoming_activities(ctx):
    # type: (Context) -> None
    from app.incoming_activities import loop

    asyncio.run(loop())


@task
def tests(ctx, k=None):
    # type: (Context, Optional[str]) -> None
    pytest_args = " -vvv"
    if k:
        pytest_args += f" -k {k}"
    run(
        f"MICROBLOGPUB_CONFIG_FILE=tests.toml pytest tests{pytest_args}",
        pty=True,
        echo=True,
    )


@task
def generate_requirements_txt(ctx, where="requirements.txt"):
    # type: (Context, str) -> None
    run(
        f"poetry export -f requirements.txt --without-hashes > {where}",
        pty=True,
        echo=True,
    )


@task
def build_docs(ctx):
    # type: (Context) -> None
    with embed_version():
        run("PYTHONPATH=. python scripts/build_docs.py", pty=True, echo=True)


@task
def download_twemoji(ctx):
    # type: (Context) -> None
    resp = httpx.get(
        "https://github.com/twitter/twemoji/archive/refs/tags/v14.0.2.tar.gz",
        follow_redirects=True,
    )
    resp.raise_for_status()
    tf = tarfile.open(fileobj=io.BytesIO(resp.content))
    members = [
        member
        for member in tf.getmembers()
        if member.name.startswith("twemoji-14.0.2/assets/svg/")
    ]
    for member in members:
        emoji_name = Path(member.name).name
        with open(f"app/static/twemoji/{emoji_name}", "wb") as f:
            f.write(tf.extractfile(member).read())  # type: ignore


@task(download_twemoji, compile_scss)
def configuration_wizard(ctx):
    # type: (Context) -> None
    run("MICROBLOGPUB_CONFIG_FILE=tests.toml alembic upgrade head", echo=True)
    run(
        "MICROBLOGPUB_CONFIG_FILE=tests.toml PYTHONPATH=. python scripts/config_wizard.py",  # noqa: E501
        pty=True,
        echo=True,
    )


@task
def install_deps(ctx):
    # type: (Context) -> None
    run("poetry install", pty=True, echo=True)


@task(pre=[compile_scss], post=[migrate_db])
def update(ctx, update_deps=True):
    # type: (Context, bool) -> None
    if update_deps:
        run("poetry install", pty=True, echo=True)
    print("Done")


@task
def stats(ctx):
    # type: (Context) -> None
    from app.utils.stats import print_stats

    print_stats()


@contextmanager
def embed_version() -> Generator[None, None, None]:
    from app.utils.version import get_version_commit

    version_file = Path("app/_version.py")
    version_file.unlink(missing_ok=True)
    version_commit = get_version_commit()
    version_file.write_text(f'VERSION_COMMIT = "{version_commit}"')
    try:
        yield
    finally:
        version_file.unlink()


@task
def build_docker_image(ctx):
    # type: (Context) -> None
    with embed_version():
        run("docker build -t microblogpub/microblogpub .")


@task
def prune_old_data(ctx):
    # type: (Context) -> None
    from app.prune import run_prune_old_data

    asyncio.run(run_prune_old_data())


@task
def webfinger(ctx, account):
    # type: (Context, str) -> None
    import traceback

    from loguru import logger

    from app.source import _MENTION_REGEX
    from app.webfinger import get_actor_url

    logger.disable("app")
    if not account.startswith("@"):
        account = f"@{account}"
    if not _MENTION_REGEX.match(account):
        print(f"Invalid acccount {account}")
        return

    print(f"Resolving {account}")
    try:
        maybe_actor_url = asyncio.run(get_actor_url(account))
        if maybe_actor_url:
            print(f"SUCCESS: {maybe_actor_url}")
        else:
            print(f"ERROR: Failed to resolve {account}")
    except Exception as exc:
        print(f"ERROR: Failed to resolve {account}")
        print("".join(traceback.format_exception(exc)))


@task
def yunohost_config(
    ctx,
    domain,
    username,
    name,
    summary,
    password,
):
    # type: (Context, str, str, str, str, str) -> None
    from app.utils import yunohost

    yunohost.setup_config_file(
        domain=domain,
        username=username,
        name=name,
        summary=summary,
        password=password,
    )
