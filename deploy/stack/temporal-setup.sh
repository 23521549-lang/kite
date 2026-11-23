#!/bin/sh
# Temporal's databases and schema in Kite's Postgres. Runs on every start of the stack: a database whose
# schema is already set up only gets the versions it misses (update-schema); a new one is created first.
set -eu
: "${POSTGRES_SEEDS:?}" "${POSTGRES_USER:?}" "${POSTGRES_PWD:?}"
export SQL_PASSWORD="$POSTGRES_PWD"
tool() {
  temporal-sql-tool --plugin postgres12 --ep "$POSTGRES_SEEDS" -u "$POSTGRES_USER" -p "${DB_PORT:-5432}" "$@"
}
for db in temporal temporal_visibility; do
  case "$db" in
    temporal) dir=/etc/temporal/schema/postgresql/v12/temporal/versioned ;;
    *) dir=/etc/temporal/schema/postgresql/v12/visibility/versioned ;;
  esac
  if tool --db "$db" update-schema -d "$dir" >/tmp/update.log 2>&1; then
    echo "$db: schema up to date"
    continue
  fi
  echo "$db: new database, setting it up"
  tool --db "$db" create
  tool --db "$db" setup-schema -v 0.0
  tool --db "$db" update-schema -d "$dir"
done
