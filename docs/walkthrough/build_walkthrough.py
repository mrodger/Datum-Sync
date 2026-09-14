"""Build the six-page Datum Sync product brochure with OfficeCLI, on the Datum design system.

Palette, type and spacing follow docs/design (Datum Design System v2): navy #1D3A5C,
amber #C89632, warm off-white surfaces, Space Grotesk for headings, DM Sans for body,
JetBrains Mono for labels. Screenshots come from record_demo.py.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
OUT = HERE / "output"
DOCX = OUT / "Datum-Sync-Walkthrough.docx"
ENV = {**os.environ, "OFFICECLI_SKIP_UPDATE": "1", "OFFICECLI_RESIDENT_FLUSH": "each"}

# Datum design tokens (hex without '#', as OfficeCLI expects)
NAVY = "1D3A5C"        # --primary
NAVY_MID = "2B567E"    # --primary-mid
AMBER = "C89632"       # --accent
AMBER_BG = "FDF6E8"    # --accent-bg
BG = "F0F1F0"          # --bg
SURFACE = "F8F8F7"     # --surface
SURFACE2 = "EAEFF5"    # --surface2
BORDER = "D8DCE0"
TEXT = "0F1923"
TEXT_MID = "5A6F80"
TEXT_DIM = "8E9FAD"
SUCCESS = "3A7A4A"
DANGER = "B84030"
WHITE = "FFFFFF"

HEADING = "Space Grotesk"
BODY = "DM Sans"
MONO = "JetBrains Mono"

PAGES_EXPECTED = 6
TABLE_INDEX = 0
PARA_INDEX = 0
COMMANDS: list[dict] = []


def run(*args: object) -> str:
    result = subprocess.run(["officecli", *map(str, args)], env=ENV, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("officecli " + " ".join(map(str, args)) + "\n" + result.stdout + "\n" + result.stderr)
    return result.stdout


def qadd(parent: str, kind: str, props: dict) -> None:
    COMMANDS.append({"op": "add", "parent": parent, "type": kind, "props": props})


def qset(path: str, props: dict) -> None:
    COMMANDS.append({"op": "set", "path": path, "props": props})


def addp(text: str = "", *, size: str = "10pt", bold: bool = False, color: str = TEXT,
         align: str | None = None, before: str = "0pt", after: str = "4pt",
         font: str = BODY, fill: str | None = None) -> str:
    global PARA_INDEX
    PARA_INDEX += 1
    props = {"text": text, "size": size, "font": font, "color": color,
             "spaceBefore": before, "spaceAfter": after}
    if bold:
        props["bold"] = "true"
    if align:
        props["align"] = align
    if fill:
        props["fill"] = fill
    qadd("/body", "paragraph", props)
    return f"/body/p[{PARA_INDEX}]"


def page_break() -> None:
    global PARA_INDEX
    PARA_INDEX += 1
    qadd("/body", "pagebreak", {"type": "page"})


def eyebrow(text: str) -> None:
    addp(text.upper(), size="8.5pt", bold=True, color=AMBER, font=MONO, after="3pt")


def title(text: str, subtitle: str | None = None) -> None:
    addp(text, size="23pt", bold=True, color=NAVY, font=HEADING, after="2pt")
    if subtitle:
        addp(subtitle, size="10.5pt", color=TEXT_MID, after="7pt")


def note(text: str) -> None:
    addp(text, size="9pt", bold=True, color=NAVY_MID, font=HEADING, align="center", before="3pt", after="0pt")


def picture(filename: str, alt: str, width: str, *, after: str = "4pt") -> None:
    path = addp("", size="1pt", align="center", after=after)
    qadd(path, "picture", {"src": str(ASSETS / filename), "width": width, "alt": alt})


def picture_row(items: list[tuple[str, str]], width: str, *, after: str = "4pt") -> None:
    path = addp("", size="1pt", align="center", after=after)
    for filename, alt in items:
        qadd(path, "picture", {"src": str(ASSETS / filename), "width": width, "alt": alt})


def table(rows: list[list[str]], *, col_widths: list[int], fills: list[str] | None = None,
          colors: list[str] | None = None, font_size: str = "9pt", bold: bool = False,
          font: str = BODY, margin_y: str = "90", caption: str = "Datum Sync brochure panel") -> str:
    global TABLE_INDEX
    TABLE_INDEX += 1
    qadd("/body", "table", {"rows": len(rows), "cols": len(rows[0]), "width": "100%",
          "colWidths": ",".join(map(str, col_widths)), "caption": caption})
    base = f"/body/tbl[{TABLE_INDEX}]"
    for r, values in enumerate(rows, 1):
        qset(f"{base}/tr[{r}]", {f"c{c}": value for c, value in enumerate(values, 1)})
        for c in range(1, len(values) + 1):
            fill = fills[c - 1] if fills else SURFACE2
            color = colors[c - 1] if colors else TEXT
            cell = f"{base}/tr[{r}]/tc[{c}]"
            qset(cell, {"fill": fill, "verticalAlign": "center", "marginTop": margin_y,
                        "marginBottom": margin_y, "marginLeft": "120", "marginRight": "120"})
            qset(f"{cell}/p[1]", {"align": "left", "spaceBefore": "0pt", "spaceAfter": "0pt"})
            qset(f"{cell}/p[1]/r[1]", {"font": font, "size": font_size,
                                      "bold": str(bold).lower(), "color": color})
    return base


def band(labels: list[str], *, font_size: str = "9pt", fills: list[str] | None = None) -> None:
    count = len(labels)
    table([labels], col_widths=[10800 // count] * count, fills=fills or [NAVY] * count,
          colors=[WHITE] * count, font_size=font_size, bold=True, font=HEADING)


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    global TABLE_INDEX, PARA_INDEX, COMMANDS
    TABLE_INDEX = 0
    PARA_INDEX = 0
    COMMANDS = []
    if DOCX.exists():
        subprocess.run(["officecli", "close", str(DOCX)], env=ENV, capture_output=True)
        DOCX.unlink()
    run("create", DOCX)
    qset("/", {"docDefaults.font": BODY, "docDefaults.fontSize": "10pt"})
    qset("/section[1]", {"pageWidth": "29.7cm", "pageHeight": "21cm", "marginTop": "0.8cm",
                          "marginBottom": "0.8cm", "marginLeft": "1cm", "marginRight": "1cm",
                          "titlePage": "true"})

    # 1 — Positioning
    logo = addp("", size="1pt", after="2pt")
    qadd(logo, "picture", {"src": str(ASSETS / "datum-mark.png"), "width": "0.62in", "alt": "Datum mark"})
    eyebrow("Datum Sync · Product overview")
    addp("Govern every agent.", size="27pt", bold=True, color=NAVY, font=HEADING, after="0pt")
    addp("Keep every worker lightweight.", size="27pt", bold=True, color=AMBER, font=HEADING, after="4pt")
    addp("One authority plane for agent identity, MCP access, managed credentials, deterministic evidence and reusable artifacts. "
         "The model runs in a small worker; every capability it uses is issued, watched and revocable in Datum Sync.",
         size="11pt", color=TEXT_MID, after="5pt")
    picture("06-worker-connected.png", "Connected Datum worker showing the Researcher agent, ready chat composer, governed tools and inspection pane", "7.4in", after="3pt")
    band(["AGENTS\nPermission-bound personas", "MCP\nOne governed route",
          "SECRETS\nNever exposed to workers", "ARTIFACTS\nDurable and shareable"], font_size="8.5pt")
    page_break()

    # 2 — Architecture: Datum Sync vs the worker
    eyebrow("01 · Architecture")
    title("Two processes, one trust boundary",
          "Datum Sync is the deterministic authority plane. The worker is a replaceable model client that holds nothing worth stealing.")
    table([
        ["", "DATUM SYNC  ·  authority plane", "FEDERATED WORKER  ·  model client"],
        ["Owns", "Principals, grants, sessions, MCP server registry, managed upstream credentials, resources, audit and MCP flow evidence.",
         "The conversation loop, the model (Codex, gpt-5.6-luna), the chat UI and a session-local view of its own activity."],
        ["Holds", "Sealed upstream secrets (AES-GCM, key-id prefixed), the operator identity, the authority policy.",
         "One opaque, short-lived agent token bound to the consented session. No Office, database or API credential."],
        ["Decides", "Which agent, which server, which tool, which credential, for how long; every call is re-checked before dispatch.",
         "Nothing about access. It asks; Datum Sync answers, forwards and records."],
        ["Never sees", "Prompts, model reasoning, tool arguments, returned document text or database rows.",
         "Secret values, other agents' resources, the fleet inventory, or the operator's audit trail."],
    ], col_widths=[1400, 4700, 4700],
       fills=[NAVY, SURFACE2, AMBER_BG], colors=[WHITE, TEXT, TEXT], font_size="8.5pt", margin_y="110",
       caption="Responsibilities of Datum Sync and the federated worker")
    addp("Request flow", size="9pt", bold=True, color=NAVY, font=HEADING, before="5pt", after="2pt")
    band(["1 CONSENT\nOperator picks agent and 15m–8h session", "2 TOKEN\nShort-lived, rotating, bound to the session",
          "3 MCP CALL\nWorker → /mcp with the token only", "4 POLICY\nAgent · grant · server · tool · tier re-checked",
          "5 DISPATCH\nCredential injected server-side, pinned egress", "6 EVIDENCE\nPayload-free timeline, replayable"],
         font_size="7.5pt", fills=[NAVY, NAVY_MID, NAVY, NAVY_MID, NAVY, NAVY_MID])
    note("The worker can be swapped for any MCP-capable client. The policy, secrets and evidence do not move with it.")
    page_break()

    # 3 — Agent model
    eyebrow("02 · Agent fleet")
    title("An agent is a governed bundle",
          "Users connect to a ready persona. Datum Sync supplies the identity, prompt, tools, credentials and limits behind it.")
    picture_row([
        ("02-principals.png", "Principals screen listing the persona agents"),
        ("09-mcp-access.png", "MCP access view for one agent: servers, grants, sessions, recent flows")],
        "5.22in", after="3pt")
    table([["ENROL\nSingle-use enrolment, human approval", "CONNECT\nChoose an approved persona in the worker",
            "AUTHORIZE\nIssue a short-lived principal session", "EVALUATE\nRecheck tool and credential policy on every call",
            "OPERATE\nReview, restrict, restore or revoke from one portal"]],
          col_widths=[2160] * 5, fills=[NAVY, NAVY_MID, NAVY, NAVY_MID, NAVY], colors=[WHITE] * 5,
          font_size="8pt", bold=True, font=HEADING)
    note("Seven demo personas prove the single-user fleet model today; the parent-owner boundary is the seam for separate user fleets.")
    page_break()

    # 4 — MCP, credentials, evidence
    eyebrow("03 · Governed access")
    title("One route for MCP, credentials and evidence",
          "Providers are registered once, then exposed per agent through the gateway. Every call leaves a deterministic, payload-free trace.")
    picture_row([
        ("03-secrets-proxies.png", "Secrets and Proxies registry with MCP servers, tools and managed service identities"),
        ("12-mcp-timeline.png", "One MCP flow expanded: receipt, authentication, session, authorization, dispatch, completion")],
        "5.22in", after="3pt")
    table([["REGISTER\nAdd endpoints and discover tools", "GRANT\nBind selected tools and service identities to an agent",
            "TRACE\nRecord the endpoint flow without prompts, arguments or results", "REVOKE\nDisable a tool, server or grant before the next call"]],
          col_widths=[2500, 2900, 3000, 2400], fills=[SURFACE2, AMBER_BG, SURFACE2, AMBER_BG], colors=[TEXT] * 4,
          font_size="9pt", bold=True, font=HEADING, margin_y="150")
    table([["OPERATOR VISIBILITY\nServer and tool state · auth type · credential fingerprint and version · effective agents · decision timing",
            "PROTECTED BY DESIGN\nNo secret values · no prompts · no tool arguments · no database rows · no returned document text"]],
          col_widths=[5400, 5400], fills=[NAVY, NAVY_MID], colors=[WHITE, WHITE],
          font_size="9pt", bold=True, font=HEADING, margin_y="140", caption="Visible and protected MCP data")
    note("Upstream credential values never enter the browser, the model context or the worker process.")
    page_break()

    # 5 — Revocation and artifacts
    eyebrow("04 · Control and outputs")
    title("Revocation is immediate. Outputs are governed.",
          "Disable a tool, a server or a grant and the next call is refused without restarting anything. Artifacts become versioned, shareable resources.")
    picture_row([
        ("13-revocation.png", "A tool disabled in Secrets and Proxies"),
        ("07-artifact-split.png", "Datum worker split pane showing a governed HTML artifact beside the conversation"),
        ("05-resources.png", "Datum Sync resource inventory showing ownership, metadata and a Designer share")],
        "3.45in", after="3pt")
    table([["TOOL\nEnable or disable one discovered tool", "SERVER\nTake a whole provider offline",
            "GRANT\nRemove one agent's credential use", "SESSION\nRevoke every credential and close sessions"]],
          col_widths=[2700] * 4, fills=[NAVY, NAVY_MID, NAVY, NAVY_MID], colors=[WHITE] * 4,
          font_size="8.5pt", bold=True, font=HEADING, margin_y="130", caption="Four independent revocation controls")
    table([["DURABLE BY DESIGN\nImmutable versions retain ownership, logical path, content hash, MIME type, source trace and expiry.",
            "SAFE BY DEFAULT\nResources start private. Named shares are read-only and time-bounded; HTML previews run in a restricted sandbox."]],
          col_widths=[5400, 5400], fills=[SURFACE2, AMBER_BG], colors=[TEXT, TEXT],
          font_size="9pt", bold=True, font=HEADING, margin_y="130", caption="Governed resource properties")
    page_break()

    # 6 — Production considerations and the demo path
    eyebrow("05 · Production and demonstration")
    title("What a production agent gateway must get right",
          "The prototype already holds the shape of each answer. The right-hand column is the honest gap list.")
    table([
        ["CONCERN", "WHAT DATUM SYNC DOES TODAY", "TO PRODUCTION"],
        ["Identity", "Every agent is a first-class principal with owner, state, grant and audit trail.", "Tenant ownership and operator roles on the same records."],
        ["Least privilege", "Grants name exact tools; tiers cap what a token can ever do; sessions expire on an absolute deadline.", "Policy as reviewed configuration, not portal clicks."],
        ["Credential brokering", "Write-only upstream secrets, versioned, rotated after a live probe, injected server-side.", "HSM or KMS-backed keys; per-tenant key sets."],
        ["Egress control", "Pinned-IP transport, private ranges refused, redirects refused, response size capped.", "Network policy enforced outside the process too."],
        ["Evidence", "Deterministic, payload-free timeline per call; denied calls recorded alongside allowed ones.", "Export to SIEM; retention and legal hold."],
        ["Isolation", "Worker holds no secrets; Codex local tools disabled; artifacts sandboxed.", "Container or OS identity per worker; no shared host."],
    ], col_widths=[1800, 5200, 3800], fills=[NAVY, SURFACE2, AMBER_BG], colors=[WHITE, TEXT, TEXT],
       font_size="8pt", margin_y="95", caption="Production considerations for an agentic gateway")
    table([
        ["01", "Connect", "Choose Researcher and a 15-minute session; the composer is ready immediately."],
        ["02", "Call", "Ask for the quarterly brief or an Auckland PostgreSQL summary."],
        ["03", "Inspect", "Open MCP Activity and follow receipt, policy, dispatch and completion."],
        ["04", "Revoke", "Disable postgres__orders_summary, repeat the request, restore it."],
        ["05", "Share", "Save an HTML artifact, share it with Designer, revoke the share."],
    ], col_widths=[650, 1600, 8550], fills=[AMBER, SURFACE2, SURFACE], colors=[NAVY, NAVY, TEXT],
       font_size="8.5pt", margin_y="95", caption="Ten-minute Datum Sync demonstration sequence")
    table([["DATUM SYNC", "Connect to a useful agent while identity, MCP access, credentials, evidence and artifacts remain governed in one place."]],
          col_widths=[2200, 8600], fills=[AMBER, NAVY], colors=[NAVY, WHITE], font_size="10.5pt",
          bold=True, font=HEADING, margin_y="140", caption="Datum Sync product statement")

    qadd("/", "header", {"type": "default", "text": "DATUM SYNC  /  GOVERNED AGENT FLEET", "size": "7.5pt", "color": TEXT_DIM, "font": MONO})
    qadd("/", "footer", {"type": "first", "text": "Product overview  ·  September 2026", "size": "7pt", "color": TEXT_DIM, "align": "center"})
    qadd("/", "footer", {"type": "default", "text": "Datum Sync product overview  ·  Page ", "field": "page", "size": "7pt", "color": TEXT_DIM, "align": "center"})
    qset("/settings", {"updateFields": "true"})

    batch = HERE / "source" / "walkthrough-batch.json"
    batch.write_text(json.dumps(COMMANDS, ensure_ascii=False, indent=2), encoding="utf-8")
    run("batch", DOCX, "--input", batch, "--json")
    run("save", DOCX)
    run("close", DOCX)
    print(DOCX)


if __name__ == "__main__":
    build()
