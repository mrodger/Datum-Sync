-- 005_connections.sql
-- Scoped credential store (build step 8).
--
-- The central decision here is the split between `config` and `secret`.
--
-- `config` holds the fields that identify a connection -- host, port, database,
-- username, base URL -- and is returned by the API and rendered in the UI.
-- `secret` holds the fields that authenticate it -- password, token, key -- as
-- one AES-GCM sealed blob, and is never returned by anything. Nothing in the
-- serialisation path has to remember which key is sensitive, because the
-- sensitive ones are not in the same column as the rest. A rule that says
-- "don't log the password" is obeyed until someone adds a field; a column that
-- holds ciphertext is obeyed by construction.
--
-- BYTEA rather than TEXT: the sealed value is nonce || ciphertext || tag, and
-- storing raw bytes avoids a base64 round trip whose only purpose would be to
-- make the column look readable, which is the opposite of the point.


CREATE TABLE connections (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    type          TEXT NOT NULL
                      CHECK (type IN ('database', 'http', 'email_smtp',
                                      'email_imap', 'file', 'oauth_client')),

    -- Sensitivity tier. Stored and displayed; NOT yet enforced against
    -- service_accounts.max_tier -- that check belongs with the publish gate in
    -- step 9, and pretending it exists now would be worse than its absence.
    tier          INTEGER NOT NULL DEFAULT 1 CHECK (tier BETWEEN 1 AND 4),

    -- Scope decides which workspaces can resolve this connection by name. It
    -- is enforced, because it is not a permission so much as an addressing
    -- rule: two repositories can each hold a connection called "MainDB" and
    -- mean different databases.
    scope         TEXT NOT NULL DEFAULT 'global'
                      CHECK (scope IN ('global', 'repository', 'workspace')),
    scope_targets TEXT[] NOT NULL DEFAULT '{}',

    access        TEXT NOT NULL DEFAULT 'read' CHECK (access IN ('read', 'write')),
    description   TEXT,

    config        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- non-secret, returned
    secret        BYTEA,                                -- sealed, never returned

    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Result of the last POST /connections/{name}/test. Kept on the row rather
    -- than in a log table: the question the UI asks is "did this last work",
    -- which is one value, and a history nobody reads is a table nobody prunes.
    last_test_at    TIMESTAMPTZ,
    last_test_ok    BOOLEAN,
    last_test_error TEXT,

    -- Makes resolution total: no row can reach the scope check with a scope
    -- that has no targets to match against, or a global scope carrying targets
    -- that would silently never be consulted. Same reasoning as
    -- schedules_one_trigger in 004.
    CONSTRAINT connections_scope_targets CHECK (
        (scope = 'global' AND cardinality(scope_targets) = 0)
        OR (scope <> 'global' AND cardinality(scope_targets) > 0)
    )
);

-- Resolution looks up by name and then filters on scope, so the UNIQUE index on
-- name already carries it. This index serves the Connections list screen, which
-- orders by name.
CREATE INDEX connections_scope_idx ON connections (scope);
