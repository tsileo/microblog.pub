#!/bin/sh
inv update
exec supervisord -n -c misc/docker-supervisord.conf
