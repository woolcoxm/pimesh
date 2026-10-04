#!/bin/bash
# Scrubs live credentials from deployment copies before they enter git.
# Run from the repo root after copying files from a live node.
set -e
sed -i "s|^PASS='[^']*'|PASS=\"\${MESH_SSH_PASS:?set MESH_SSH_PASS}\"|" scripts/deeplane.sh
sed -i "s|echo '[^']*' | sudo -S apt-get|echo \"\$PASS\" | sudo -S apt-get|" scripts/deeplane.sh
sed -i 's|"sshpass", "-p", "[^"]*", "ssh",|"sshpass", "-p", os.environ.get("MESH_SSH_PASS",""), "ssh",|' gateway.py
sed -i "s|password='[^']*'|password=os.environ.get('MESH_SSH_PASS','')|" scripts/ssh_run.py
echo "sanitized"
