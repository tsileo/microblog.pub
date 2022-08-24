import subprocess


def get_version_commit() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "--short=8", "v2"])
            .split()[0]
            .decode()
        )
    except Exception:
        return "dev"
