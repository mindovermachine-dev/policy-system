#!/bin/sh
# PS Postgres first-start provisioning (issue #130). Executed by the postgres image
# from /docker-entrypoint-initdb.d on an EMPTY data directory only (it does NOT re-run
# against an existing PGDATA; re-running by hand fails loudly on CREATE ROLE).
#
# Creates two databases on one server, each with its own least-privilege role:
#   state   (default ps_state)   - audit, authz, runtime config
#   signing (default ps_signing) - Passkey Signing
# plus a third, NOLOGIN role (default ps_state_graph_owner) that will own the insert-only
# graph mutation log tables of the state database (issue #205). It is granted to nobody:
# the state role is deliberately NOT a member, so it can never SET ROLE to it. The tables
# themselves are created later by the admin-credential provisioning step
# (`python -m ps_service.graph_gateway.provision`), which also creates this role on an
# existing cluster, because this script never runs against a non-empty PGDATA.
# The two databases are isolated from each other: CONNECT is revoked from PUBLIC on
# both databases and granted only to the database's own role; CREATE on schema public
# is revoked from PUBLIC in each database.
#
# Names and passwords come from the environment; they reach SQL only as psql
# variables (:"ident" / :'literal'), never shell-interpolated. Passwords are never
# echoed and tracing is never enabled.
set -e

: "${POSTGRES_USER:=postgres}"
: "${PS_STATE_DATABASE:=ps_state}"
: "${PS_STATE_USER:=ps_state}"
: "${PS_SIGNING_DATABASE:=ps_signing}"
: "${PS_SIGNING_USER:=ps_signing}"
: "${PS_STATE_GRAPH_OWNER_ROLE:=ps_state_graph_owner}"
: "${PS_STATE_POSTGRES_PASSWORD:?PS_STATE_POSTGRES_PASSWORD is required}"
: "${PS_PASSKEYSIGNING_POSTGRES_PASSWORD:?PS_PASSKEYSIGNING_POSTGRES_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v state_db="$PS_STATE_DATABASE" -v state_user="$PS_STATE_USER" \
  -v state_pw="$PS_STATE_POSTGRES_PASSWORD" -v owner_role="$PS_STATE_GRAPH_OWNER_ROLE" \
  -v signing_db="$PS_SIGNING_DATABASE" -v signing_user="$PS_SIGNING_USER" \
  -v signing_pw="$PS_PASSKEYSIGNING_POSTGRES_PASSWORD" <<'PSQL'
CREATE ROLE :"state_user" LOGIN PASSWORD :'state_pw';
CREATE ROLE :"signing_user" LOGIN PASSWORD :'signing_pw';
CREATE ROLE :"owner_role" NOLOGIN NOINHERIT;

CREATE DATABASE :"state_db" OWNER :"state_user";
REVOKE CONNECT ON DATABASE :"state_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"state_db" TO :"state_user";

CREATE DATABASE :"signing_db" OWNER :"signing_user";
REVOKE CONNECT ON DATABASE :"signing_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"signing_db" TO :"signing_user";

\connect :"state_db"
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

\connect :"signing_db"
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
PSQL

echo "ps-postgres-init: created roles (including NOLOGIN $PS_STATE_GRAPH_OWNER_ROLE) and databases $PS_STATE_DATABASE and $PS_SIGNING_DATABASE with isolated CONNECT grants"
