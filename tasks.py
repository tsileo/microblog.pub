import io
import tarfile
from pathlib import Path
from typing import Optional

import httpx
from invoke import Context  # type: ignore
from invoke import run  # type: ignore
from invoke import task  # type: ignore


@task
def generate_db_migration(ctx, message):
    # type: (Context, str) -> None
    run(f'poetry run alembic revision --autogenerate -m "{message}"', echo=True)


@task
def migrate_db(ctx):
    # type: (Context) -> None
    run("poetry run alembic upgrade head", echo=True)


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
    vars_file = Path("app/scss/vars.scss")
    if not vars_file.exists():
        vars_file.write_text("")

    if watch:
        run("poetry run boussole watch", echo=True)
    else:
        run("poetry run boussole compile", echo=True)


@task
def uvicorn(ctx):
    # type: (Context) -> None
    run("poetry run uvicorn app.main:app --no-server-header", pty=True, echo=True)


@task
def process_outgoing_activities(ctx):
    # type: (Context) -> None
    from app.outgoing_activities import loop

    loop()


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


@task(generate_requirements_txt)
def build_configuration_wizard_image(ctx):
    # type: (Context) -> None
    run("docker build -t testmpw -f configuration_wizard.dockerfile .")


@task
def build_docs(ctx):
    # type: (Context) -> None
    run("PYTHONPATH=. poetry run python scripts/generate_docs.py", pty=True, echo=True)


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


@task(download_twemoji, compile_scss, migrate_db)
def configuration_wizard(ctx):
    # type: (Context) -> None
    run("PYTHONPATH=. poetry run python scripts/config_wizard.py", pty=True, echo=True)
