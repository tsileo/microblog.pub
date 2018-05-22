import logging

logger = logging.getLogger(__name__)


def strtobool(s: str) -> bool:
    if s in ['y', 'yes', 'true', 'on', '1']:
        return True
    if s in ['n', 'no', 'false', 'off', '0']:
        return False

    raise ValueError(f'cannot convert {s} to bool')
