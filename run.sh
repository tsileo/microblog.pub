#!/bin/bash
python -c "import logging; logging.basicConfig(level=logging.DEBUG); from core import migrations; migrations.perform()"
python -c "from core import indexes; indexes.create_indexes()"
python startup.py
(sleep 5 && curl -X POST -u :$POUSETACHES_AUTH_KEY $MICROBLOGPUB_POUSSETACHES_HOST/resume)&
gunicorn -t 600 -w 5 -b 0.0.0.0:5005 --log-level debug app:app
