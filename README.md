# Datum-Sync

A workspace runner and hosting platform for data processing pipelines, built with MCP-native connectivity from the ground up.

## What it does

Datum-Sync lets you publish Python workspaces as callable services — accessible via REST API, MCP (Claude.ai, ChatGPT, Copilot), or a web UI. Workspaces declare their inputs, outputs, and connection requirements in a manifest. The platform handles scheduling, automation, delivery, and hosting.

## Key concepts

- **Workspaces** — Python modules with a typed parameter interface and structured output
- **Services** — stream, download, or upload data from a workspace run
- **Connections** — scoped, tiered credential store (database, HTTP, email, file)
- **Automations** — YAML-configured triggers and actions (schedule, webhook, email)
- **Hosted services** — publish a workspace output as a persistent web service (static, PWA, interactive, dashboard)
- **MCP endpoint** — expose workspaces as tools callable from any MCP-compatible AI client

## Status

Design phase. Not yet buildable.

## Architecture

- FastAPI backend, PostgreSQL + PostGIS
- MCP Streamable HTTP transport with OAuth 2.0 (PKCE)
- APScheduler for cron-based triggers
- pg_notify for durable SSE streaming
- Vanilla JS web UI
