-- Auth: human credentials, OAuth clients, codes and tokens.
--
-- An OAuth grant mints a token *bound to a service account*, so permissions
-- (max_tier, repo_scope, connection_grants) have exactly one home. There is no
-- second permission model to keep in sync.


-- Human credential ----------------------------------------------------------
-- service_accounts.token_hash authenticates a script. A person standing at a
-- consent screen has no token to present, so the account gains a password.
-- Nullable: the machine accounts (andrew, david) never log in and must not be
-- given a credential they would never use.

ALTER TABLE service_accounts ADD COLUMN password_hash TEXT;
ALTER TABLE service_accounts ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT false;


-- OAuth clients -------------------------------------------------------------
-- Registered dynamically (RFC 7591). Claude.ai will not have a client id
-- before it first sees this server, so pre-registration is not an option.
--
-- redirect_uris is exact-match at authorize time: prefix or wildcard matching
-- is how open redirectors get built.

CREATE TABLE oauth_clients (
    client_id      TEXT PRIMARY KEY,
    client_name    TEXT,
    -- NULL for public clients. Public + PKCE is what MCP clients use; a
    -- confidential client stores sha256(secret) here, never the secret.
    secret_hash    TEXT,
    redirect_uris  TEXT[] NOT NULL,
    grant_types    TEXT[] NOT NULL DEFAULT ARRAY['authorization_code', 'refresh_token'],
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (array_length(redirect_uris, 1) >= 1)
);


-- Authorization codes -------------------------------------------------------
-- Single use and short lived. `used_at` rather than DELETE: replaying a code
-- must be *detectable*, and a row that is gone looks identical to a code that
-- was never issued.

CREATE TABLE oauth_codes (
    code            TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    account_id      INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    redirect_uri    TEXT NOT NULL,
    -- PKCE. S256 only: 'plain' is permitted by RFC 7636 and defeats the point.
    code_challenge  TEXT NOT NULL,
    -- RFC 8707. The audience the token will be minted for; carried from the
    -- authorize request so the token request cannot widen it.
    resource        TEXT,
    scope           TEXT,
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX oauth_codes_expires_idx ON oauth_codes (expires_at);


-- Tokens --------------------------------------------------------------------
-- Access and refresh tokens live in one table, distinguished by `kind`. Only
-- sha256(token) is stored; the raw value is returned once and never again.
--
-- `resource` is the audience. A token minted for this MCP server must not be
-- accepted by anything else, and we must reject tokens not minted for us.

CREATE TABLE oauth_tokens (
    id           BIGSERIAL PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id    TEXT REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    account_id   INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    resource     TEXT,
    scope        TEXT,
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    -- Refresh rotation: the replacement is recorded so that presenting an
    -- already-rotated refresh token is recognised as replay rather than as an
    -- unknown token. OAuth 2.1 requires rotation for public clients.
    rotated_to   BIGINT REFERENCES oauth_tokens(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);

CREATE INDEX oauth_tokens_hash_idx ON oauth_tokens (token_hash);
CREATE INDEX oauth_tokens_account_idx ON oauth_tokens (account_id);
