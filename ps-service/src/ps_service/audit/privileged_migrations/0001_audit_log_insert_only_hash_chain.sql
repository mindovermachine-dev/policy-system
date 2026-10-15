-- Audit integrity (issue #151): the audit trail moves out of `public` into its own schema,
-- owned by the non-login owner role, so the application role can no longer rewrite it.
--
-- Rule: migrations are append-only from the first deployment onward -- never edit, renumber
-- or delete an applied file, add a new file instead (see ps_service.persistence.migration_runner).
-- This one file is deliberately a single transaction, so that the schema move, and everything
-- the integrity work adds to it, either all take effect or none does.
--
-- This file is NOT applied by the ordinary startup runner. It is applied by the privileged
-- runner (ps_service.persistence.privileged_migration_runner) with ADMIN credentials, after the
-- ordinary audit migrations and after graph_gateway (whose first migration references the
-- table by its old name -- the reference follows the table through the move). The runner
-- renders the two role tokens below as quoted identifiers.
--
-- Statement order is load-bearing. Comments in this file never contain a semicolon or a dollar
-- sign, and no quoted literal contains a semicolon, because the statement splitter does not
-- understand comments or quoted text (tests/audit/test_privileged_migration_split.py).

-- Fail fast and roll back rather than queue an ACCESS EXCLUSIVE request behind a long
-- transaction, which would block every audit reader and writer meanwhile.
SET LOCAL lock_timeout = '30s';

DO $$
BEGIN
    -- The canonical form relies on extract(epoch ...) returning numeric (Postgres 14 or later).
    IF current_setting('server_version_num')::int < 140000 THEN
        RAISE EXCEPTION 'the audit integrity migration needs Postgres 14 or later'
            USING ERRCODE = 'feature_not_supported';
    END IF;
    IF to_regclass('public.audit_events') IS NULL THEN
        RAISE EXCEPTION 'public.audit_events is missing: apply the ordinary audit migrations first'
            USING ERRCODE = 'undefined_table';
    END IF;
END
$$;

CREATE SCHEMA audit_log AUTHORIZATION @@OWNER_ROLE@@;

-- The table keeps its indexes, constraints and the graph_log.groups reference through the move.
-- The move holds ACCESS EXCLUSIVE until commit, so no row is written while later steps run.
ALTER TABLE public.audit_events SET SCHEMA audit_log;

ALTER TABLE audit_log.audit_events OWNER TO @@OWNER_ROLE@@;

-- Chain columns. They stay nullable here: rows written before the integrity work are chained by
-- the backfill, and only then can the columns be required.
ALTER TABLE audit_log.audit_events
    ADD COLUMN chain_seq bigint,
    ADD COLUMN prev_row_hash text,
    ADD COLUMN row_hash text;

-- Canonical form ps-audit-v1 and the hash. These functions are the only definition of the
-- format: the backfill, the chain trigger and the verifier all call them, so a stored hash can
-- never be computed two ways. All are IMMUTABLE with a pinned search_path, and none of them
-- depends on a session setting (TimeZone, DateStyle, IntervalStyle, extra_float_digits,
-- lc_messages, bytea_output, standard_conforming_strings or client_encoding).

CREATE FUNCTION audit_log.genesis_hash() RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$ SELECT repeat('0', 64) $$;

-- One field: the decimal UTF-8 byte length, a colon, then the value. No separator is needed,
-- so no value can shift bytes into its neighbour.
CREATE FUNCTION audit_log.canonical_field(p_value text) RETURNS bytea
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT convert_to(octet_length(convert_to(p_value, 'UTF8'))::text, 'UTF8')
        || '\x3a'::bytea
        || convert_to(p_value, 'UTF8')
$$;

CREATE FUNCTION audit_log.canonical_row(
    p_chain_seq bigint,
    p_id uuid,
    p_occurred_at timestamptz,
    p_actor_subject text,
    p_actor_issuer text,
    p_action text,
    p_resource_type text,
    p_resource_id text,
    p_outcome text,
    p_details jsonb
) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF p_chain_seq IS NULL OR p_id IS NULL OR p_occurred_at IS NULL
        OR p_actor_subject IS NULL OR p_actor_issuer IS NULL OR p_action IS NULL
        OR p_resource_type IS NULL OR p_resource_id IS NULL OR p_outcome IS NULL
        OR p_details IS NULL THEN
        RAISE EXCEPTION 'audit_log.canonical_row: no input may be NULL'
            USING ERRCODE = 'null_value_not_allowed';
    END IF;
    -- The timestamp is a UTC instant in whole microseconds. An infinite value fails the cast.
    RETURN convert_to('ps-audit-v1', 'UTF8')
        || audit_log.canonical_field(p_chain_seq::text)
        || audit_log.canonical_field(lower(p_id::text))
        || audit_log.canonical_field(
            floor(extract(epoch FROM p_occurred_at) * 1000000)::bigint::text)
        || audit_log.canonical_field(p_actor_subject)
        || audit_log.canonical_field(p_actor_issuer)
        || audit_log.canonical_field(p_action)
        || audit_log.canonical_field(p_resource_type)
        || audit_log.canonical_field(p_resource_id)
        || audit_log.canonical_field(p_outcome)
        || audit_log.canonical_field(p_details::text);
END
$$;

-- row_hash = SHA-256 of the previous row hash (64 hex characters as ASCII) followed by the
-- canonical bytes of the row.
CREATE FUNCTION audit_log.chain_hash(p_prev text, p_canonical bytea) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$ SELECT encode(sha256(convert_to(p_prev, 'UTF8') || p_canonical), 'hex') $$;

ALTER FUNCTION audit_log.genesis_hash() OWNER TO @@OWNER_ROLE@@;
ALTER FUNCTION audit_log.canonical_field(text) OWNER TO @@OWNER_ROLE@@;
ALTER FUNCTION audit_log.canonical_row(
    bigint, uuid, timestamptz, text, text, text, text, text, text, jsonb
) OWNER TO @@OWNER_ROLE@@;
ALTER FUNCTION audit_log.chain_hash(text, bytea) OWNER TO @@OWNER_ROLE@@;

-- The chain head: one row naming the last chained row. Inserting a row locks it, and that lock
-- is held until the inserting transaction ends. This is what serialises every writer into one
-- gap-free chain, and why the head is updated in the same transaction as the audit row.
CREATE TABLE audit_log.audit_chain_head (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    last_chain_seq bigint NOT NULL CHECK (last_chain_seq >= 0),
    last_row_hash text NOT NULL CHECK (last_row_hash ~ '^[0-9a-f]{64}$')
);

ALTER TABLE audit_log.audit_chain_head OWNER TO @@OWNER_ROLE@@;

-- An empty chain: sequence 0 and the genesis hash. Rows that already exist are chained later in
-- this same file, which then replaces this seed with the real head.
INSERT INTO audit_log.audit_chain_head (singleton, last_chain_seq, last_row_hash)
VALUES (true, 0, audit_log.genesis_hash());

ALTER TABLE audit_log.audit_events
    ADD CONSTRAINT audit_events_chain_seq_key UNIQUE (chain_seq),
    ADD CONSTRAINT audit_events_chain_seq_positive CHECK (chain_seq >= 1),
    ADD CONSTRAINT audit_events_prev_row_hash_format CHECK (prev_row_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT audit_events_row_hash_format CHECK (row_hash ~ '^[0-9a-f]{64}$');

-- Backfill: chain the rows that exist before this file ran, in the order (occurred_at, id),
-- with the very functions the chain trigger uses for new rows. It runs before any trigger exists
-- on the table, because the insert-only triggers would reject its updates. The head then names
-- the last backfilled row, replacing the seed. An empty table leaves the seed untouched.
DO $$
DECLARE
    v_row record;
    v_seq bigint := 0;
    v_prev text := audit_log.genesis_hash();
    v_hash text;
BEGIN
    FOR v_row IN
        SELECT id, occurred_at, actor_subject, actor_issuer, action, resource_type, resource_id,
               outcome, details
            FROM audit_log.audit_events
            ORDER BY occurred_at, id
    LOOP
        v_seq := v_seq + 1;
        v_hash := audit_log.chain_hash(
            v_prev,
            audit_log.canonical_row(
                v_seq, v_row.id, v_row.occurred_at, v_row.actor_subject, v_row.actor_issuer,
                v_row.action, v_row.resource_type, v_row.resource_id, v_row.outcome, v_row.details
            )
        );
        UPDATE audit_log.audit_events
            SET chain_seq = v_seq, prev_row_hash = v_prev, row_hash = v_hash
            WHERE id = v_row.id;
        v_prev := v_hash;
    END LOOP;
    IF v_seq > 0 THEN
        UPDATE audit_log.audit_chain_head
            SET last_chain_seq = v_seq, last_row_hash = v_prev
            WHERE singleton;
    END IF;
END
$$;

-- Every row is chained now, so the chain columns become mandatory.
ALTER TABLE audit_log.audit_events
    ALTER COLUMN chain_seq SET NOT NULL,
    ALTER COLUMN prev_row_hash SET NOT NULL,
    ALTER COLUMN row_hash SET NOT NULL;

-- The chain trigger. SECURITY DEFINER because the application role has no privilege at all on
-- the head table, and a trigger function needs no EXECUTE grant for the role that fires it. The
-- search_path is pinned so nothing the caller controls can shadow a name used here. The
-- function-level lock_timeout turns a stalled earlier writer into a named error (55P03) instead
-- of an unbounded wait, and the caller's transaction rolls back with it.
-- Values the caller put in the three chain columns are overwritten, never trusted.
CREATE FUNCTION audit_log.audit_events_chain() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET lock_timeout = '10s'
AS $$
DECLARE
    v_head_seq bigint;
    v_head_hash text;
BEGIN
    SELECT last_chain_seq, last_row_hash INTO STRICT v_head_seq, v_head_hash
        FROM audit_log.audit_chain_head
        WHERE singleton
        FOR UPDATE;
    NEW.chain_seq := v_head_seq + 1;
    NEW.prev_row_hash := v_head_hash;
    NEW.row_hash := audit_log.chain_hash(
        v_head_hash,
        audit_log.canonical_row(
            NEW.chain_seq, NEW.id, NEW.occurred_at, NEW.actor_subject, NEW.actor_issuer,
            NEW.action, NEW.resource_type, NEW.resource_id, NEW.outcome, NEW.details
        )
    );
    UPDATE audit_log.audit_chain_head
        SET last_chain_seq = NEW.chain_seq, last_row_hash = NEW.row_hash
        WHERE singleton;
    RETURN NEW;
END
$$;

-- The head may only move forward by one, and may never be deleted or truncated. This is a guard
-- against slips by the owner role, not a boundary against a superuser.
CREATE FUNCTION audit_log.audit_chain_head_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.last_chain_seq = OLD.last_chain_seq + 1 THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'audit_log.audit_chain_head only moves forward by one row at a time'
        USING ERRCODE = 'restrict_violation';
END
$$;

ALTER FUNCTION audit_log.audit_events_chain() OWNER TO @@OWNER_ROLE@@;
ALTER FUNCTION audit_log.audit_chain_head_guard() OWNER TO @@OWNER_ROLE@@;

-- Insert-only. The application role has no UPDATE, DELETE or TRUNCATE privilege, so Postgres
-- refuses it before any trigger runs. These triggers are the second layer, for the owner role
-- and for a superuser who leaves them enabled. They run before the chain trigger can matter and
-- change nothing: the statement fails as a whole.
CREATE FUNCTION audit_log.audit_events_reject_mutation() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'audit_log.audit_events is insert-only: % is not allowed', TG_OP
        USING ERRCODE = 'restrict_violation';
END
$$;

ALTER FUNCTION audit_log.audit_events_reject_mutation() OWNER TO @@OWNER_ROLE@@;

CREATE TRIGGER audit_events_chain BEFORE INSERT ON audit_log.audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_log.audit_events_chain();
CREATE TRIGGER audit_chain_head_guard_row BEFORE UPDATE OR DELETE ON audit_log.audit_chain_head
    FOR EACH ROW EXECUTE FUNCTION audit_log.audit_chain_head_guard();
CREATE TRIGGER audit_chain_head_guard_truncate BEFORE TRUNCATE ON audit_log.audit_chain_head
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log.audit_chain_head_guard();

CREATE TRIGGER audit_events_reject_update BEFORE UPDATE ON audit_log.audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_log.audit_events_reject_mutation();
CREATE TRIGGER audit_events_reject_delete BEFORE DELETE ON audit_log.audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_log.audit_events_reject_mutation();
CREATE TRIGGER audit_events_reject_truncate BEFORE TRUNCATE ON audit_log.audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log.audit_events_reject_mutation();

-- ALWAYS: the triggers also fire in a session with session_replication_role set to replica.
ALTER TABLE audit_log.audit_events ENABLE ALWAYS TRIGGER audit_events_chain;
ALTER TABLE audit_log.audit_events ENABLE ALWAYS TRIGGER audit_events_reject_update;
ALTER TABLE audit_log.audit_events ENABLE ALWAYS TRIGGER audit_events_reject_delete;
ALTER TABLE audit_log.audit_events ENABLE ALWAYS TRIGGER audit_events_reject_truncate;
ALTER TABLE audit_log.audit_chain_head ENABLE ALWAYS TRIGGER audit_chain_head_guard_row;
ALTER TABLE audit_log.audit_chain_head ENABLE ALWAYS TRIGGER audit_chain_head_guard_truncate;

-- Verification. Walks the chain in chain_seq order and recomputes every row with the same
-- functions the chain trigger uses, then compares the last row with the head. A row is broken
-- by the first applicable kind: sequence_gap (chain_seq is not the previous one plus one, so a
-- row is missing or the chain does not start at 1), prev_hash_mismatch (the stored link is not
-- the previous row hash, or the genesis hash for the first row), row_hash_mismatch (the stored
-- hash is not the hash of the stored link and the row content). The lowest broken chain_seq is
-- reported with its id and time. A row holding an infinite time is a row_hash_mismatch, and is
-- reported with its id and no time, because an infinite time has no timestamp to report. When
-- every row verifies, a head that names a different last row means the tail was removed,
-- reported as head_mismatch with no row. SECURITY DEFINER because the application role cannot
-- read the head table. It insists on a REPEATABLE READ (or stricter) snapshot, so the rows and the
-- head are read at one instant even while writers continue. It reads only and returns exactly
-- one row.
CREATE FUNCTION audit_log.verify_chain() RETURNS TABLE (
    intact boolean,
    rows_checked bigint,
    head_chain_seq bigint,
    break_kind text,
    broken_chain_seq bigint,
    broken_id uuid,
    broken_occurred_at timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_head_seq bigint;
    v_head_hash text;
    v_rows bigint;
    v_last_seq bigint;
    v_last_hash text;
    v_kind text;
    v_seq bigint;
    v_id uuid;
    v_at timestamptz;
BEGIN
    IF current_setting('transaction_isolation') NOT IN ('repeatable read', 'serializable') THEN
        RAISE EXCEPTION 'audit_log.verify_chain needs a REPEATABLE READ or SERIALIZABLE transaction'
            USING ERRCODE = 'invalid_transaction_state';
    END IF;
    SELECT h.last_chain_seq, h.last_row_hash INTO STRICT v_head_seq, v_head_hash
        FROM audit_log.audit_chain_head h
        WHERE h.singleton;
    WITH walk AS MATERIALIZED (
        SELECT e.chain_seq, e.id, e.occurred_at, e.row_hash,
               CASE
                   WHEN e.chain_seq <> coalesce(lag(e.chain_seq) OVER o, 0) + 1
                       THEN 'sequence_gap'
                   WHEN e.prev_row_hash <> coalesce(lag(e.row_hash) OVER o,
                                                    audit_log.genesis_hash())
                       THEN 'prev_hash_mismatch'
                   -- The chain trigger refuses an infinite time, so no chained row ever had
                   -- one. A row that holds one now was rewritten. It is decided here, before
                   -- the hash is recomputed, because the canonical form cannot encode it.
                   WHEN NOT isfinite(e.occurred_at)
                       THEN 'row_hash_mismatch'
                   WHEN e.row_hash <> audit_log.chain_hash(
                            e.prev_row_hash,
                            audit_log.canonical_row(
                                e.chain_seq, e.id, e.occurred_at, e.actor_subject,
                                e.actor_issuer, e.action, e.resource_type, e.resource_id,
                                e.outcome, e.details))
                       THEN 'row_hash_mismatch'
               END AS kind
            FROM audit_log.audit_events e
            WINDOW o AS (ORDER BY e.chain_seq)
    ),
    totals AS (
        SELECT count(*) AS n, max(w.chain_seq) AS last_seq,
               (array_agg(w.row_hash ORDER BY w.chain_seq DESC))[1] AS last_hash
            FROM walk w
    ),
    first_bad AS (
        SELECT w.kind, w.chain_seq, w.id,
               CASE WHEN isfinite(w.occurred_at) THEN w.occurred_at END AS occurred_at
            FROM walk w
            WHERE w.kind IS NOT NULL
            ORDER BY w.chain_seq
            LIMIT 1
    )
    SELECT t.n, t.last_seq, t.last_hash, b.kind, b.chain_seq, b.id, b.occurred_at
        INTO STRICT v_rows, v_last_seq, v_last_hash, v_kind, v_seq, v_id, v_at
        FROM totals t LEFT JOIN first_bad b ON true;
    IF v_kind IS NOT NULL THEN
        RETURN QUERY SELECT false, v_rows, v_head_seq, v_kind, v_seq, v_id, v_at;
    ELSIF coalesce(v_last_seq, 0) <> v_head_seq
        OR coalesce(v_last_hash, audit_log.genesis_hash()) <> v_head_hash THEN
        RETURN QUERY SELECT false, v_rows, v_head_seq, 'head_mismatch',
            NULL::bigint, NULL::uuid, NULL::timestamptz;
    ELSE
        RETURN QUERY SELECT true, v_rows, v_head_seq, NULL::text,
            NULL::bigint, NULL::uuid, NULL::timestamptz;
    END IF;
END
$$;

ALTER FUNCTION audit_log.verify_chain() OWNER TO @@OWNER_ROLE@@;

-- Final privileges. This block stays LAST: it is the catch-all that applies to every object
-- created above it. Owner and privileges are stated per object where the object is created,
-- and this block then closes anything left open. The application role gets read and append
-- on the audit table and nothing else. Superusers are out of scope (they bypass any grant).
REVOKE ALL ON SCHEMA audit_log FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA audit_log FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA audit_log FROM @@APP_ROLE@@;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA audit_log FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA audit_log FROM @@APP_ROLE@@;

GRANT USAGE ON SCHEMA audit_log TO @@APP_ROLE@@;
GRANT SELECT, INSERT ON audit_log.audit_events TO @@APP_ROLE@@;
GRANT EXECUTE ON FUNCTION audit_log.verify_chain() TO @@APP_ROLE@@;
