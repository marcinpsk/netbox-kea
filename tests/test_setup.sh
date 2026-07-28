#!/bin/bash

set -euxo pipefail

echo "Generating certs"
mkdir ./tests/docker/certs/
openssl req -new -newkey rsa:2048 -sha256 -days 365 -nodes -x509 -keyout ./tests/docker/certs/netbox.key -out ./tests/docker/certs/netbox.crt -addext "subjectAltName=DNS:netbox" -subj "/CN=netbox"
openssl req -new -newkey rsa:2048 -sha256 -days 365 -nodes -x509 -keyout ./tests/docker/certs/nginx.key -out ./tests/docker/certs/nginx.crt -addext "subjectAltName=DNS:nginx" -subj "/CN=nginx"
chmod -R 0777 ./tests/docker/certs/

# Reservation management needs a Kea host database, and Kea refuses a schema whose
# version is not exactly the one it was built against, so take the schema from the
# release tarball of the version the daemons run (see docker-compose.override.yml).
# Exported so docker-compose.override.yml pins the daemon images to the same version.
export KEA_VERSION="${KEA_VERSION:-3.2.0}"
KEA_TARBALL_SHA256="${KEA_TARBALL_SHA256:-14bf695d37b65b9b1bf550fea5d0adaf9806c50e5419ef2a176a4b8e9aade3df}"
echo "Fetching Kea $KEA_VERSION host database schema"
mkdir -p ./tests/docker/kea_schema/
# Private temp dir: a fixed /tmp path is guessable, so another user could plant the
# tarball (or symlink it elsewhere) before curl writes it.
tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT
tarball="$tmp_dir/kea-$KEA_VERSION.tar.xz"
curl -fsSL -o "$tarball" \
    "https://downloads.isc.org/isc/kea/$KEA_VERSION/kea-$KEA_VERSION.tar.xz"
echo "$KEA_TARBALL_SHA256  $tarball" | sha256sum -c -
tar xJf "$tarball" -C ./tests/docker/kea_schema/ --strip-components=6 \
    "kea-$KEA_VERSION/src/share/database/scripts/pgsql/dhcpdb_create.pgsql"

echo "Copying whl"
WHL_FILE=$(ls ./dist/ | grep .whl)
cp  "./dist/$WHL_FILE" ./tests/docker/

echo "Running docker compose up"
cd ./tests/docker/
docker compose build --build-arg "FROM=netboxcommunity/netbox:$NETBOX_CONTAINER_TAG" --build-arg "WHL_FILE=$WHL_FILE"
docker compose up -d
