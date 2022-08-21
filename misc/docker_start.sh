#!/bin/sh
inv update --no-update-deps
exec supervisord -n -c misc/docker-supervisord.conf
