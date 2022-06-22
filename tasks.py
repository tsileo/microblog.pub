from typing import Optional

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
    run("poetry run python app/process_outgoing_activities.py", pty=True, echo=True)


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
