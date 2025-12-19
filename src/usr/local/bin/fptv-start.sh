#!/usr/bin/env bash

# Used by /etc/systemd/system/fptv.service
# Save as authorized user (HTV_USER)
#
# Start service:  sudo systemctl start fptv.service
# Reload service: sudo systemctl daemon-reload
# Service status: systemctl status fptv.service
# Read logs:      journalctl -u fptv.service

set -euo pipefail

TAG='fptv'

log () {
	echo "[$TAG] $*"
}

err() {
	echo "[$TAG] $*" >&2
}

log "Starting FPTV"

log "FPTV Exiting"

exit 0

