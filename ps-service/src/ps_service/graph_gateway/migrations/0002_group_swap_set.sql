-- Swap set link (issue #214): a nullable id on each logged group that was applied together with
-- other groups by swapping whole graphs in, so recovery can tell which graphs belong together.
--
-- Rule: migrations are append-only from the first deployment onward -- never edit, renumber
-- or delete an applied file, add a new file instead (see ps_service.persistence.migration_runner).
--
-- Applied by the privileged runner (ps_service.persistence.privileged_migration_runner) with
-- ADMIN credentials, like 0001. The groups table is owned by the owner role, so the statements
-- run as that role and the role is reset before the runner records this file in its tracking
-- table, which the owner role cannot write. The column is additive and nullable: the previous
-- release ignores it, so rolling the image back leaves it in place. The application role already
-- holds INSERT and SELECT on the whole table, so no grant is needed, and the row guards of 0001
-- do not name the column.

SET LOCAL ROLE @@OWNER_ROLE@@;

ALTER TABLE graph_log.groups ADD COLUMN swap_set_id uuid;

CREATE INDEX groups_swap_set_idx ON graph_log.groups (swap_set_id) WHERE swap_set_id IS NOT NULL;

RESET ROLE;
