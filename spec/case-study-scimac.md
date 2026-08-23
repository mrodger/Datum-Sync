# Case Study — SCIMAC Ltd

Reference implementation showing how Datum-Sync maps to a real small business.

## Organisation

SCIMAC Ltd is a small geotechnical company. Staff:

| Person | Role | Technical level | Datum-Sync access |
|---|---|---|---|
| Simone | Geologist (FTE) | High — builds workspaces, uses Claude.ai | Full access, all repos |
| Marcus | Admin / GIS / Software (part-time) | High | Full access, all repos |
| Robert | Geotech Engineer (consultant) | High — uses Claude.ai | MCP access, viewer |
| Andrew | Geotech Engineer (consultant) | Low — prefers email | Email delivery only |
| David | Structural Engineer (consultant) | Low — prefers email | Email delivery only |

---

## Repositories

```
SCIMAC/
  core/          Site plans, core logs, 3D viewer, soil test download
  mobile/        PWA
  reports/       Soil report drafting and delivery
```

---

## Service Accounts

| Account | Repos | Max tier | Notes |
|---|---|---|---|
| `simone` | `SCIMAC/*` | 4 | Build, publish, run anything |
| `marcus` | `SCIMAC/*` | 4 | Build, publish, run anything |
| `robert` | `SCIMAC/*` | 2 | MCP via Claude.ai — runs workspaces, views outputs |
| `andrew` | — | 1 | No UI login. Receives email deliveries only |
| `david` | — | 1 | No UI login. Receives email deliveries only |

Robert connects Claude.ai to `https://geofabnz.com/mcp`. He can ask:
- "Run site plan for job 70045" → Claude calls the MCP endpoint, submits the job, streams progress
- "Get me the soil test download for job 70023" → download artifact returned inline
- "Is the report draft ready for job 70012?" → queries job status

Andrew and David receive email. They never interact with Datum-Sync directly.

---

## Connections

| Name | Type | Tier | Scope | Access |
|---|---|---|---|---|
| `StratumDB` | PostgreSQL/PostGIS | 2 | `SCIMAC/core` | read |
| `GDriveDelivery` | Google Drive | 2 | `SCIMAC/*` | write |
| `SMTPStratum` | Email SMTP | 1 | global | write |
| `MSSharePoint` | SharePoint | 2 | `SCIMAC/reports` | read+write |

---

## Workspaces

### SCIMAC/core/site_plan
- **Inputs:** `JOB_ID` (STRING, required), `OUTPUT_FORMAT` (LOOKUP_CHOICE: HTML|PDF)
- **Outputs:** `site_plan_pdf` (application/pdf), `site_plan_html` (text/html)
- **Services:** job_submitter, data_download
- **Connections:** StratumDB (read)

### SCIMAC/core/site_plan_interactive
- **Inputs:** `JOB_ID` (STRING, required)
- **Outputs:** `{"type": "service/interactive", ...}` — persistent review site at `/serve/scimac-review-{JOB_ID}/`
- **Connections:** StratumDB (read)

### SCIMAC/core/core_log_builder
- **Inputs:** `JOB_ID` (STRING, required), `FORMAT` (LOOKUP_CHOICE: PDF|HTML)
- **Outputs:** `core_log_pdf` (application/pdf), charts (text/html)
- **Services:** job_submitter, data_download
- **Connections:** StratumDB (read)

### SCIMAC/core/3d_viewer
- **Inputs:** `JOB_ID` (STRING, required)
- **Outputs:** `{"type": "service/static", ...}` — persistent viewer at `/serve/scimac-3d-{JOB_ID}/`
- **Connections:** StratumDB (read)

### SCIMAC/core/soil_test_download
- **Inputs:** `JOB_ID` (STRING, required), `FORMAT` (LOOKUP_CHOICE: CSV|ZIP|JSON)
- **Outputs:** `soil_tests` (application/zip)
- **Services:** data_download
- **Connections:** StratumDB (read)

### SCIMAC/mobile/mobile_pwa
- **Inputs:** none
- **Outputs:** `{"type": "service/pwa", ...}` — persistent PWA at `/serve/scimac-mobile/`
- **Connections:** StratumDB (read)

### SCIMAC/reports/soil_report_draft
- **Inputs:** `JOB_ID` (STRING, required)
- **Outputs:** `report_docx` (application/vnd.openxmlformats-officedocument.wordprocessingml.document)
- **Connections:** StratumDB (read), MSSharePoint (write)
- **Side effect:** uploads DOCX to SharePoint and returns the SharePoint URL as a log event

---

## Automations

```yaml
# Deliver completed site plan to Andrew and David by email
name: site-plan-email-delivery
enabled: true
trigger:
  type: job_complete
  repository: SCIMAC
  workspace: site_plan
  status: success
actions:
  - type: deliver
    channel: email
    to: [andrew@scimac.co.nz, david@scimac.co.nz]
    artifacts: [site_plan_pdf]
    subject: "Site plan ready — Job {{params.JOB_ID}}"
    body: "Please find the site plan for job {{params.JOB_ID}} attached."
```

```yaml
# Notify Robert when a report draft lands in SharePoint
name: report-draft-notify
enabled: true
trigger:
  type: job_complete
  repository: SCIMAC
  workspace: soil_report_draft
  status: success
actions:
  - type: deliver
    channel: email
    to: [robert@scimac.co.nz]
    subject: "Report draft ready — Job {{params.JOB_ID}}"
    body: "Review link: {{job.log.sharepoint_url}}"
```

---

## Collaborative Word report workflow

1. Simone runs `SCIMAC/reports/soil_report_draft` for a job ID
2. Workspace queries StratumDB, builds structured DOCX, uploads to SharePoint
3. Automation emails Robert (and Andrew if needed) with the SharePoint link
4. Robert and Andrew comment in Word Online — no Datum-Sync interaction
5. Simone runs a finalisation workspace (future: `soil_report_final`), delivers signed PDF
6. Automation emails the final PDF to Andrew, David, and the client

Datum-Sync orchestrates around SharePoint. SharePoint handles collaborative editing.

---

## User experience by role

**Simone:** Builds and publishes workspaces from her machine. Runs jobs via the web UI or
Claude.ai. Monitors job queue. Manages connections and service accounts.

**Robert:** Adds `https://geofabnz.com/mcp` to Claude.ai settings, completes the OAuth
consent once. After that: asks Claude.ai natural language questions, gets results inline.
Never touches the Datum-Sync UI.

**Andrew:** Receives email. Replies with comments. That is the complete interface.

**David:** Same as Andrew.
