-- Graph mutation log baseline (issue #205): the insert-only, authoritative record of graph
-- mutations, in its own schema so that dropping `public` can never take it down.
--
-- Rule: migrations are append-only from the first deployment onward -- never edit, renumber
-- or delete an applied file, add a new file instead (see ps_service.persistence.migration_runner).
--
-- This file is NOT applied by the ordinary startup runner. It is applied by the privileged
-- runner (ps_service.persistence.privileged_migration_runner) with ADMIN credentials, which
-- renders the two role tokens below as quoted identifiers. Every object is created by the
-- admin and then handed to the non-login owner role, of which the application role is NOT a
-- member: the application role holds only INSERT and SELECT on the log, payload and
-- checkpoint tables (plus UPDATE on the applied-marker table), so Postgres itself rejects
-- every rewrite, however the application behaves.
--
-- Beyond privileges, sequence integrity is enforced by the database even against a rogue
-- application session: each position must be exactly one above the graph's current maximum,
-- a group's recorded range must match its entries at commit, and an applied marker can only
-- advance and never pass the last logged position. The trigger functions are SECURITY INVOKER
-- and owned by the owner role, which the application role cannot disable or replace.
--
-- Payloads are content-addressed: the primary key is the SHA-256 of the exact stored bytes,
-- checked by the database. Embeddings are stored as lossless 64-bit binary, never as JSON.
-- The audit link is a foreign key to the audit trail, so an audit row that anchors a log
-- group can never be deleted.

CREATE SCHEMA graph_log AUTHORIZATION @@OWNER_ROLE@@;

CREATE TABLE graph_log.payloads (
    payload_hash text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('float64le', 'json')),
    byte_length integer NOT NULL CHECK (byte_length >= 0),
    body bytea NOT NULL,
    CONSTRAINT payloads_byte_length_matches_body CHECK (octet_length(body) = byte_length),
    CONSTRAINT payloads_hash_matches_body
        CHECK (payload_hash = 'sha256:' || encode(sha256(body), 'hex'))
);

CREATE TABLE graph_log.groups (
    group_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    graph text NOT NULL CHECK (graph <> ''),
    first_position bigint NOT NULL,
    last_position bigint NOT NULL,
    audit_event_id uuid REFERENCES public.audit_events (id),
    committed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT groups_range_valid CHECK (first_position >= 1 AND last_position >= first_position),
    CONSTRAINT groups_id_graph_key UNIQUE (group_id, graph)
);

CREATE INDEX groups_audit_event_idx ON graph_log.groups (audit_event_id);

CREATE TABLE graph_log.entries (
    graph text NOT NULL CHECK (graph <> ''),
    position bigint NOT NULL,
    group_id uuid NOT NULL,
    name text NOT NULL CHECK (name <> ''),
    identity text NOT NULL CHECK (identity <> ''),
    content jsonb,
    content_payload_hash text REFERENCES graph_log.payloads (payload_hash),
    embedding_payload_hash text REFERENCES graph_log.payloads (payload_hash),
    PRIMARY KEY (graph, position),
    CONSTRAINT entries_position_positive CHECK (position >= 1),
    CONSTRAINT entries_exactly_one_content_source
        CHECK (num_nonnulls(content, content_payload_hash) = 1),
    CONSTRAINT entries_group_graph_fkey
        FOREIGN KEY (group_id, graph) REFERENCES graph_log.groups (group_id, graph)
);

CREATE INDEX entries_group_idx ON graph_log.entries (group_id);

CREATE TABLE graph_log.checkpoints (
    graph text NOT NULL CHECK (graph <> ''),
    position bigint NOT NULL CHECK (position >= 0),
    canonical_digest text NOT NULL CHECK (canonical_digest <> ''),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (graph, position)
);

CREATE TABLE graph_log.applied_markers (
    graph text PRIMARY KEY CHECK (graph <> ''),
    applied_position bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT applied_position_nonneg CHECK (applied_position >= 0)
);

CREATE FUNCTION graph_log.entries_next_position() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, graph_log AS $$
DECLARE
    expected bigint;
BEGIN
    SELECT coalesce(max(position), 0) + 1 INTO expected
    FROM graph_log.entries WHERE graph = NEW.graph;
    IF NEW.position <> expected THEN
        RAISE EXCEPTION 'graph_log position must be % for this graph', expected
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER entries_next_position BEFORE INSERT ON graph_log.entries
    FOR EACH ROW EXECUTE FUNCTION graph_log.entries_next_position();

CREATE FUNCTION graph_log.groups_match_entries() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, graph_log AS $$
DECLARE
    lowest bigint;
    highest bigint;
    entry_count bigint;
BEGIN
    SELECT min(position), max(position), count(*) INTO lowest, highest, entry_count
    FROM graph_log.entries WHERE group_id = NEW.group_id;
    IF lowest IS DISTINCT FROM NEW.first_position
       OR highest IS DISTINCT FROM NEW.last_position
       OR entry_count <> NEW.last_position - NEW.first_position + 1 THEN
        RAISE EXCEPTION 'graph_log group range disagrees with its entries'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER groups_match_entries AFTER INSERT ON graph_log.groups
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION graph_log.groups_match_entries();

CREATE FUNCTION graph_log.applied_marker_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, graph_log AS $$
DECLARE
    last_logged bigint;
BEGIN
    SELECT coalesce(max(position), 0) INTO last_logged
    FROM graph_log.entries WHERE graph = NEW.graph;
    IF NEW.applied_position > last_logged THEN
        RAISE EXCEPTION 'applied marker may not pass the last logged position'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.applied_position < OLD.applied_position THEN
            RAISE EXCEPTION 'applied marker may only advance'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER applied_marker_guard BEFORE INSERT OR UPDATE ON graph_log.applied_markers
    FOR EACH ROW EXECUTE FUNCTION graph_log.applied_marker_guard();

ALTER TABLE graph_log.payloads OWNER TO @@OWNER_ROLE@@;
ALTER TABLE graph_log.groups OWNER TO @@OWNER_ROLE@@;
ALTER TABLE graph_log.entries OWNER TO @@OWNER_ROLE@@;
ALTER TABLE graph_log.checkpoints OWNER TO @@OWNER_ROLE@@;
ALTER TABLE graph_log.applied_markers OWNER TO @@OWNER_ROLE@@;
ALTER FUNCTION graph_log.entries_next_position() OWNER TO @@OWNER_ROLE@@;
ALTER FUNCTION graph_log.groups_match_entries() OWNER TO @@OWNER_ROLE@@;
ALTER FUNCTION graph_log.applied_marker_guard() OWNER TO @@OWNER_ROLE@@;

REVOKE ALL ON SCHEMA graph_log FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA graph_log FROM PUBLIC;
GRANT USAGE ON SCHEMA graph_log TO @@APP_ROLE@@;
GRANT SELECT, INSERT ON graph_log.payloads TO @@APP_ROLE@@;
GRANT SELECT, INSERT ON graph_log.groups TO @@APP_ROLE@@;
GRANT SELECT, INSERT ON graph_log.entries TO @@APP_ROLE@@;
GRANT SELECT, INSERT ON graph_log.checkpoints TO @@APP_ROLE@@;
GRANT SELECT, INSERT, UPDATE ON graph_log.applied_markers TO @@APP_ROLE@@;
