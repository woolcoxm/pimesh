#!/bin/bash
exec >>~/update.log 2>&1
set -x
P="${MESH_SSH_PASS:?export MESH_SSH_PASS}"
# armor: freeze axclhost package version, neutralize __DATE__/__TIME__ in DKMS sources
echo "$P" | sudo -S apt-mark hold axclhost
echo "$P" | sudo -S bash -c 'grep -rl "__DATE__\|__TIME__" /usr/src/axclhost-3.6.5/ 2>/dev/null | while read f; do sed -i "s/__DATE__/\"Sep 28 2026\"/g; s/__TIME__/\"00:00:00\"/g" "$f"; done; echo DKMS-SOURCES-PATCHED'
apt-get update 2>&1 | tail -2
echo "$P" | sudo -S env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get -y full-upgrade -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" 2>&1 | tail -8
NEWK=$(ls /lib/modules | grep -E '^6\.' | sort -V | tail -1)
echo "newest kernel: $NEWK (running: $(uname -r))"
echo '--- eeprom ---'
echo "$P" | sudo -S rpi-eeprom-update -a 2>&1 | tail -4
sync
echo UPDATE-DONE-HENRY-NO-REBOOT
