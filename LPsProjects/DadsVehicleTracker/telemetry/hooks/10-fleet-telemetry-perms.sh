#!/bin/sh
# Runs inside the certbot container after each successful renewal.
# fleet-telemetry runs as uid 65532 and needs to traverse live/ + archive/
# and read the (new) private key. Keep the key 0600, just owned by that uid.
set -e
chmod 755 /etc/letsencrypt/live /etc/letsencrypt/archive
chmod 755 /etc/letsencrypt/live/* /etc/letsencrypt/archive/*
chown 65532:65532 /etc/letsencrypt/archive/*/privkey*.pem
chmod 600 /etc/letsencrypt/archive/*/privkey*.pem
