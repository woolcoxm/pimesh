#!/bin/bash
exec >>~/update.log 2>&1
set -x
P="${MESH_SSH_PASS:?export MESH_SSH_PASS}"
echo "$P" | sudo -S apt-get update 2>&1 | tail -2
echo "$P" | sudo -S env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get -y full-upgrade -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" 2>&1 | tail -8
echo '--- eeprom ---'
echo "$P" | sudo -S rpi-eeprom-update -a 2>&1 | tail -4
sync
echo UPDATE-DONE
sleep 2
echo "$P" | sudo -S reboot
