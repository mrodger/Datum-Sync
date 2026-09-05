# 16 — Configuration, Policy, Deployment, Operations

## 1. Environment (`.env`, loaded by `config.py`; exported vars win)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | yes | — | `postgresql://user:pass@host:port/db` |
| `PUBLIC_URL` | yes | — | the origin clients reach; OAuth issuer and audience; no trailing slash |
| `HOST` / `PORT` | | `0.0.0.0` / `8200` | |
| `DG_SECRET_KEY_1`, `_2`, … | for secrets | — | base64url 32 bytes each; `python -m datumgate keys generate` |
| `DG_ACTIVE_KEY_ID` | with keys | highest present | which key seals new secrets |
| `DG_AUTH` | | on | `off` = dev mode; requires loopback bind |
| `REPOSITORIES_PATH` | | `./repositories` | |
| `DATA_PATH` | | `./data` | jobs, uploads, venvs |
| `VAULT_PATH` | | `./vault` | |
| `POLICY_PATH` | | `./policy.yaml` | |
| `DEFAULT_TIMEZONE` | | `Pacific/Auckland` | display and schedule default |
| `REQUIRE_HTTPS` | | false | OAuth redirect/endpoint strictness |
| `ACCESS_TOKEN_TTL_SECONDS` | | 3600 | |
| `REFRESH_TOKEN_TTL_SECONDS` | | 2592000 | |
| `SESSION_TTL_SECONDS` | | 43200 | |
| `AUTH_CODE_TTL_SECONDS` | | 600 | |
| `PASSWORD_MAX_ATTEMPTS` / `PASSWORD_WINDOW_SECONDS` / `PASSWORD_MAX_CONCURRENT` | | 5 / 900 / 4 | |
| `WORKER_POLL_SECONDS` | | 5 | |
| `JOB_LEASE_SECONDS` | | 60 | |
| `WORKER_DRAIN_SECONDS` | | 30 | |
| `API_PROCESSES` | | 1 | >1 switches rate limiting to the table |
| `MAX_UPLOAD_BYTES` | | 268435456 | |
| `VAULT_MAX_READ_BYTES` / `VAULT_MAX_WRITE_BYTES` | | 262144 / 4194304 | |
| `MCP_MAX_INLINE_BYTES` | | 65536 | |
| `PROXY_MAX_RESPONSE_BYTES` | | 1048576 | |
| `AUDIT_QUEUE_MAX` | | 10000 | |
| `LOG_LEVEL` | | info | JSON lines to stdout |

Startup refuses (with a message naming the variable) when `PUBLIC_URL` is
absent, when `DG_AUTH=off` and `HOST` is not loopback, when
`DG_ACTIVE_KEY_ID` names a key that is not present, or when any
`DG_SECRET_KEY_n` is not 32 bytes.

## 2. `policy.yaml`

Loaded at startup and on `POST /rest/v1/policy/reload` (tier 5). Schema
validated; an invalid file refuses to start / refuses to reload and keeps
the previous policy.

```yaml
version: 1

governance_paths:
  - "SOUL.md"
  - "skills/**"
  - "hooks/**"
  - "policy/**"
governance_write_min_tier: 4
governance_verbs:
  - principal.create
  - principal.update
  - principal.delete
  - principal.delegate
  - keys.reseal
  - policy.reload
  - service.register
  - vault.promote.approve
  - connection.create
  - connection.update

promote:
  require_second_principal: true
  approver_tier: 4
  self_approve_tier: 5
  allow_overwrite: false
  pending_ttl_hours: 72

delegation:
  max_depth: 4
  max_token_hours: 168

automation:
  max_chain_depth: 8
  http_timeout_seconds: 15
  max_redirects: 3

proxy:
  per_connection_per_minute: 60
  max_redirects: 5

limits_default:
  calls_per_minute: 120
  jobs_per_hour: 120
  concurrent_jobs: 4

retention:
  job_artifact_days: 90
  job_log_days: 90
  upload_days: 7
  audit_days: 365
  governance_audit_days: 0
  venv_days: 30
  unused_oauth_client_days: 30

vault:
  search_backend: filesystem        # filesystem | external
  search_connection: null

sandbox:
  command_prefix: []                # e.g. ["bwrap", "--ro-bind", "/", "/", ...]
```

## 3. Processes

| Unit | Command | Count |
|---|---|---|
| `datumgate-api.service` | `python -m datumgate api` (uvicorn, `--workers API_PROCESSES`) | 1+ |
| `datumgate-worker.service` | `python -m datumgate worker --concurrency 4` | 1+ (leases make N safe) |

```ini
[Unit]
Description=Datum-Gate API
After=network-online.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=datumgate
WorkingDirectory=/opt/datumgate
EnvironmentFile=/opt/datumgate/.env
ExecStart=/opt/datumgate/.venv/bin/python -m datumgate api
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/datumgate/data /vault
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

The worker unit is identical with `worker --concurrency 4` and
`After=datumgate-api.service`. `ReadWritePaths` must include `DATA_PATH`
and `VAULT_PATH`; the worker also needs `REPOSITORIES_PATH` readable.

## 4. Database setup

```
sudo -u postgres psql -c "CREATE ROLE datumgate LOGIN PASSWORD '…';"
sudo -u postgres psql -c "CREATE DATABASE datumgate OWNER datumgate;"
sudo -u postgres psql -d datumgate -c "CREATE EXTENSION postgis; CREATE EXTENSION pgcrypto;"
python -m datumgate migrate
python -m datumgate migrate --status
```

Extensions are created by a superuser once; migration `000` uses
`IF NOT EXISTS` and tolerates the role lacking superuser.

## 5. Bootstrap

```
python -m datumgate keys generate >> .env          # DG_SECRET_KEY_1
python -m datumgate principals create marcus --kind human --tier 5
python -m datumgate principals passwd marcus
python -m datumgate principals token marcus --label laptop
python -m datumgate sync
```

There is no HTTP route that creates the first principal.

## 6. CLI

```
python -m datumgate api | worker | migrate [--status] | sync [...]
python -m datumgate principals create NAME --kind human|agent --tier N [--parent P] [--grant FILE|JSON] [--description]
python -m datumgate principals token NAME --label L [--expires-hours H]
python -m datumgate principals passwd NAME
python -m datumgate principals disable|enable NAME
python -m datumgate principals list [--tree]
python -m datumgate principals show NAME            # effective grant
python -m datumgate keys generate | reseal | status
python -m datumgate audit export --since --until --format jsonl|csv
python -m datumgate retention sweep [--dry-run]
python -m datumgate health
```

CLI actions run as `system:local` unless `--as NAME` is given, and are
audited with `via='cli'`.

## 7. Backups

- `pg_dump datumgate` nightly to `VAULT_PATH/backups/datumgate/` (or
  another off-box location).
- The secret keys are **not** in the database and must be backed up
  separately; losing them loses every stored secret.
- `DATA_PATH/jobs` is artifacts; back up per retention appetite.

## 8. Health and monitoring

`GET /health` (public):

```json
{"status":"ok","db":"ok","secrets":"ok","workers":[{"id":"vm112:4123:ab12","heartbeat_at":"…","concurrency":4}],
 "queue":{"queued":3,"running":2,"pending_deliveries":0,"dead_deliveries":1},
 "audit_dropped":0,"version":"1.0.0"}
```

`status` is `degraded` when no worker has heartbeated in 60s or
`audit_dropped > 0`; `503` when the database is unreachable. Structured
JSON logs to stdout carry `trace_id` on every line that has one.

## 9. Upgrade

```
git pull; .venv/bin/pip install -r requirements.txt
python -m datumgate migrate
systemctl restart datumgate-api datumgate-worker
```

Migrations are forward-only. A migration that would drop or rewrite a
column ships in two releases: add-and-backfill, then drop.

## 10. Development mode

`DG_AUTH=off` with `HOST=127.0.0.1` serves everything as a synthetic tier-5
principal named `auth-disabled`; the UI shows a red NO AUTH badge; every
request's peer address is checked for loopback regardless of how uvicorn
was started. `docker-compose.yml` provides a PostGIS 16 container on `5435`.

## 11. Requirements

```
asyncpg fastapi uvicorn[standard] python-dotenv pydantic>=2 python-multipart
argon2-cffi croniter PyYAML httpx cryptography jsonschema
pytest pytest-asyncio playwright (tests only)
```

No `authlib`, no ORM, no task queue library.
