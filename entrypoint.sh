#!/bin/sh
set -e
if [ -d /root/.ssh-host ]; then
  mkdir -p /root/.ssh
  cp -a /root/.ssh-host/. /root/.ssh/ 2>/dev/null || true
  chmod 700 /root/.ssh
  chmod 600 /root/.ssh/id_* 2>/dev/null || true
  chmod 644 /root/.ssh/*.pub /root/.ssh/known_hosts 2>/dev/null || true
fi
if [ -f /root/.gitconfig-host ]; then
  cp /root/.gitconfig-host /root/.gitconfig
fi
exec "$@"
