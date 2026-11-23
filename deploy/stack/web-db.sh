#!/bin/sh
# The web's database (kite_web) and its own role, in the stack's Postgres. Runs on every start: the role
# gets the password from stack.env again, nothing else changes.
set -eu
: "${KITE_WEB_DB_PASSWORD:?}"
until pg_isready -q; do sleep 1; done
psql -v ON_ERROR_STOP=1 -v pw="$KITE_WEB_DB_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE kite_web LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kite_web')\gexec
ALTER ROLE kite_web WITH LOGIN PASSWORD :'pw';
SELECT 'CREATE DATABASE kite_web OWNER kite_web' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kite_web')\gexec
SQL
echo "kite_web ready"
