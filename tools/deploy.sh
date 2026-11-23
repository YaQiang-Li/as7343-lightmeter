#!/bin/sh
# Copy the project to the Raspberry Pi over ssh (key or password prompt).
#   PI=pi@raspberrypi.local tools/deploy.sh
# Local data (cal/user.json, data/) on the Pi is left untouched.
set -eu
PI=${PI:?set PI=user@host}
DST=${DST:-as7343-lightmeter}
cd "$(dirname "$0")/.."
# $DST is meant to expand locally, before the command reaches the Pi
# shellcheck disable=SC2029
tar cf - --exclude=.git --exclude=cal/user.json --exclude=__pycache__ \
	--exclude=./data --exclude=./.venv . \
	| ssh "$PI" "mkdir -p '$DST' && tar xf - -C '$DST'"
echo "deployed to $PI:$DST"
