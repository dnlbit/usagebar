#!/bin/sh
dir=$(cd "$(dirname "$0")" && pwd)
exec "$dir/.venv/bin/python" "$dir/monitor.py"
