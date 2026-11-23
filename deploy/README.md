# Local Temporal and PostgreSQL services

This is the Temporal stack of the development machine (bootstrap.sh, systemd services). A machine of its
own runs all of Kite from images with Docker Compose and updates itself: see [stack/README.md](stack/README.md).

This Compose stack follows Temporal's PostgreSQL deployment example and pins the service images. The server and UI bind to loopback only; PostgreSQL is available only on the internal Compose network. PostgreSQL data is stored at `KITE_DATA_DIR/postgres` (default `/var/lib/kite/postgres`).

From the repository root:

```sh
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
# Edit TEMPORAL_POSTGRES_PASSWORD before starting services.
docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
```

The Temporal UI is optional and starts only with `--profile operator-ui`; then it is available at `http://127.0.0.1:8080`. The gRPC endpoint for a Kite process on the same host is `127.0.0.1:7233`. Do not open these ports in the EC2 security group.

To stop services without removing persistent data:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml down
```

Do not pass `--volumes`/`-v` to `down`; that deletes PostgreSQL workflow history. Keep `deploy/.env` private and out of source control. This single-host Compose deployment is intended for the personal EC2 control plane; it is not a high-availability Temporal cluster.
