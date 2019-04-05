#!/bin/bash
python -c "import config; config.create_indexes()"
gunicorn -t 300 -w 5 -b 0.0.0.0:5005 --log-level debug app:app
