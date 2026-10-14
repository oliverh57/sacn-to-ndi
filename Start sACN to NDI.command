#!/bin/bash
# Double-click to start sACN to NDI. Close this window (or press Ctrl+C) to stop it.
cd "$(dirname "$0")"
pkill -f "sacn2ndi\.py" 2>/dev/null && sleep 1   # stop any copy already running
exec python3 sacn2ndi.py "$@"
