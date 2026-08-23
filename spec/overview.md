# Datum-Sync — Project Overview

## What it is

Datum-Sync is a workspace runner and hosting platform for data processing pipelines.
It exposes workspaces as callable services via REST API, MCP (Claude.ai, ChatGPT, Copilot),
and a web UI. It manages scheduling, automation, delivery, and persistent hosted services.

## Design principles

- **Familiar shape** — terminology and URL structure follow established workflow platform
  conventions. Users experienced with similar tools should feel at home immediately.
- **MCP-native from day one** — AI clients (Claude.ai, ChatGPT, Copilot) are first-class
  callers, not an afterthought. OAuth 2.0 PKCE is built in, not bolted on.
- **Intentional publish friction** — workspaces must declare their interface before they
  can be published. This enforces modularity and consistency without being hostile.
- **No accidental friction** — no desktop application dependency, no XML region issues,
  no proprietary runtime required. Python all the way down.
- **Email as a first-class delivery channel** — not a plugin. Dedicated SMTP/IMAP
  connections, email delivery actions in automations, email triggers for external users
  who never touch the UI.

## Target deployment

- Host: Stratum VM (192.168.88.112), isolated service
- Public access: geofabnz tunnel (HTTPS termination at tunnel edge)
- MCP endpoint: `https://geofabnz.com/mcp`
- Database: PostgreSQL + PostGIS, dedicated instance on vm112
- Language: Python 3.12
- Framework: FastAPI

## Build order

Build in this sequence — each layer is usable before the next is started:

1. Database schema + migrations
2. Repository + workspace loader (core runtime)
3. Job Engine (asyncpg + pg_notify + SSE)
4. REST API skeleton (`/rest/v1/` + service paths)
5. OAuth 2.0 server + MCP endpoint (Claude.ai day 1)
6. Web UI
7. Schedules + Automations
8. Connections (encrypted store)
9. Publish gate
10. Hosted Services
