#!/bin/sh
# macOS / Linux 一键入口：./panel.sh start | stop | status | once
cd "$(dirname "$0")" || exit 1
exec python3 panel.py "$@"
