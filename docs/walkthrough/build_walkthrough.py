"""Build the five-page Datum Sync product brochure with OfficeCLI."""
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
NAVY = "172633"
INK = "233746"
GOLD = "C99A35"
BLUE = "244B6B"
CREAM = "F5F2E9"
MIST = "E8EEF2"
WHITE = "FFFFFF"
GREY = "667085"
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


def addp(text: str = "", *, size: str = "10pt", bold: bool = False, color: str = INK,
         align: str | None = None, before: str = "0pt", after: str = "4pt",
         font: str = "Aptos", fill: str | None = None) -> str:
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
    addp(text.upper(), size="8.5pt", bold=True, color=GOLD, after="3pt")


def title(text: str, subtitle: str | None = None) -> None:
    addp(text, size="23pt", bold=True, color=NAVY, after="2pt")
    if subtitle:
        addp(subtitle, size="10.5pt", color=GREY, after="7pt")


def picture(filename: str, alt: str, width: str, *, after: str = "4pt") -> None:
    path = addp("", size="1pt", align="center", after=after)
    qadd(path, "picture", {"src": str(ASSETS / filename), "width": width, "alt": alt})


def picture_row(items: list[tuple[str, str]], width: str, *, after: str = "4pt") -> None:
    path = addp("", size="1pt", align="center", after=after)
    for filename, alt in items:
        qadd(path, "picture", {"src": str(ASSETS / filename), "width": width, "alt": alt})


def table(rows: list[list[str]], *, col_widths: list[int], fills: list[str] | None = None,
          colors: list[str] | None = None, font_size: str = "9pt", bold: bool = False,
          margin_y: str = "90", caption: str = "Datum Sync brochure panel") -> str:
    global TABLE_INDEX
    TABLE_INDEX += 1
    qadd("/body", "table", {"rows": len(rows), "cols": len(rows[0]), "width": "100%",
          "colWidths": ",".join(map(str, col_widths)), "caption": caption})
    base = f"/body/tbl[{TABLE_INDEX}]"
    for r, values in enumerate(rows, 1):
        qset(f"{base}/tr[{r}]", {f"c{c}": value for c, value in enumerate(values, 1)})
        for c in range(1, len(values) + 1):
            fill = fills[c - 1] if fills else CREAM
            color = colors[c - 1] if colors else INK
            cell = f"{base}/tr[{r}]/tc[{c}]"
            qset(cell, {"fill": fill, "verticalAlign": "center", "marginTop": margin_y,
                        "marginBottom": margin_y, "marginLeft": "120", "marginRight": "120"})
            qset(f"{cell}/p[1]", {"align": "left", "spaceBefore": "0pt", "spaceAfter": "0pt"})
            qset(f"{cell}/p[1]/r[1]", {"font": "Aptos", "size": font_size,
                                      "bold": str(bold).lower(), "color": color})
    return base


def band(labels: list[str], *, font_size: str = "9pt") -> None:
    count = len(labels)
    table([labels], col_widths=[10800 // count] * count, fills=[NAVY] * count,
          colors=[WHITE] * count, font_size=font_size, bold=True)


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
    qset("/", {"docDefaults.font": "Aptos", "docDefaults.fontSize": "10pt"})
    qset("/section[1]", {"pageWidth": "29.7cm", "pageHeight": "21cm", "marginTop": "0.8cm",
                          "marginBottom": "0.8cm", "marginLeft": "1cm", "marginRight": "1cm",
                          "titlePage": "true"})

    # 1 — Positioning
    logo = addp("", size="1pt", after="2pt")
    qadd(logo, "picture", {"src": str(ASSETS / "datum-mark.png"), "width": "0.62in", "alt": "Datum mark"})
    eyebrow("Datum Sync · Product overview")
    addp("Govern every agent.", size="27pt", bold=True, color=NAVY, after="0pt")
    addp("Keep every worker lightweight.", size="27pt", bold=True, color=BLUE, after="4pt")
    addp("A single authority plane for agent identity, MCP access, managed credentials, deterministic evidence and reusable artifacts.",
         size="11pt", color=GREY, after="5pt")
    picture("06-worker-connected.png", "Connected Datum worker showing the Researcher agent, ready chat composer, governed tools and inspection pane", "7.55in", after="3pt")
    band(["AGENTS\nPermission-bound personas", "MCP\nOne governed route",
          "SECRETS\nNever exposed to workers", "ARTIFACTS\nDurable and shareable"], font_size="8.5pt")
    page_break()

    # 2 — Agent model
    eyebrow("01 · Agent fleet")
    title("An agent is a governed bundle",
          "Users connect to a ready persona. Datum Sync supplies the identity, prompt, tools, credentials and limits behind it.")
    picture("02-principals.png", "Principals screen showing Manager, Developer, Researcher, Designer, Security, GIS Analyst and Data Enginer", "8.65in", after="4pt")
    table([["CONNECT\nChoose an approved persona in the worker", "AUTHORIZE\nIssue a short-lived principal session",
            "EVALUATE\nRecheck tool and credential policy on every call", "OPERATE\nReview, restrict or revoke from one portal"]],
          col_widths=[2700] * 4, fills=[NAVY, BLUE, NAVY, BLUE], colors=[WHITE] * 4,
          font_size="8.5pt", bold=True)
    addp("Seven demo personas prove the single-user fleet model today; the existing parent-owner boundary provides the path to separate user fleets.",
         size="9pt", bold=True, color=BLUE, align="center", before="3pt", after="0pt")
    page_break()

    # 3 — MCP and credentials
    eyebrow("02 · Governed access")
    title("One route for MCP and credentials",
          "OfficeCLI, PostgreSQL and Datum Harness capabilities are registered once, then exposed per agent through the Datum Sync gateway.")
    picture_row([
        ("03-secrets-proxies.png", "Secrets and Proxies registry with MCP servers, tools and managed service identities"),
        ("04-mcp-activity.png", "MCP Activity view with deterministic connection, authorization, dispatch and completion records")],
        "5.22in", after="3pt")
    table([["REGISTER\nAdd endpoints and discover tools", "GRANT\nBind selected tools and service identities to an agent",
            "TRACE\nRecord the endpoint flow without prompts, arguments or results", "REVOKE\nDisable a tool, server or grant before the next call"]],
          col_widths=[2500, 2900, 3000, 2400], fills=[CREAM, MIST, CREAM, MIST], colors=[INK] * 4,
          font_size="9pt", bold=True, margin_y="175")
    table([["OPERATOR VISIBILITY\nServer and tool state · auth type · credential fingerprint and version · effective agents · decision timing",
            "PROTECTED BY DESIGN\nNo secret values · no prompts · no tool arguments · no database rows · no returned document text"]],
          col_widths=[5400, 5400], fills=[NAVY, BLUE], colors=[WHITE, WHITE],
          font_size="9pt", bold=True, margin_y="150", caption="Visible and protected MCP data")
    addp("Operators see labels, fingerprints, versions, effective agents and use state. Upstream credential values never enter the browser, model context or worker process.",
         size="9pt", bold=True, color=BLUE, align="center", before="3pt", after="0pt")
    page_break()

    # 4 — Resources
    eyebrow("03 · Governed outputs")
    title("Artifacts become shared resources",
          "A worker can place generated output in a pane, save it through Datum Sync and share it with another named agent without copying files through chat.")
    picture_row([
        ("07-artifact-split.png", "Datum worker split pane showing a governed HTML artifact beside the conversation"),
        ("05-resources.png", "Datum Sync resource inventory showing ownership, metadata and a Designer share")],
        "5.22in", after="3pt")
    table([["1 · CREATE\nPrivate to the producing agent", "2 · INSPECT\nPreview in split or focused pane",
            "3 · SHARE\nNamed, time-bounded read access", "4 · REVOKE\nEffective on the next list or read"]],
          col_widths=[2700] * 4, fills=[NAVY, BLUE, NAVY, BLUE], colors=[WHITE] * 4,
          font_size="9pt", bold=True, margin_y="175")
    table([["DURABLE BY DESIGN\nImmutable versions retain ownership, logical path, content hash, MIME type, source trace and expiry.",
            "SAFE BY DEFAULT\nResources start private. Named shares are read-only and HTML previews run inside a restricted sandbox."]],
          col_widths=[5400, 5400], fills=[CREAM, MIST], colors=[INK, INK],
          font_size="9pt", bold=True, margin_y="150", caption="Governed resource properties")
    addp("Each immutable version carries an owner, logical path, content hash, MIME type, source trace and expiry. HTML previews run in a restricted sandbox.",
         size="9pt", bold=True, color=BLUE, align="center", before="3pt", after="0pt")
    page_break()

    # 5 — Demonstration journey
    eyebrow("04 · Demonstration")
    title("Show the complete control loop in ten minutes",
          "Move between the operator portal and federated worker to make each policy decision visible and immediate.")
    picture_row([("01-portal-overview.png", "Datum Sync operator overview"),
                 ("06-worker-connected.png", "Connected Researcher worker"),
                 ("08-artifact-focused.png", "Focused governed artifact pane")], "3.45in", after="3pt")
    table([
        ["01", "Connect Researcher", "Choose a 15-minute demo or 2, 4, or 8-hour session; the composer is ready immediately."],
        ["02", "Call a real MCP tool", "Ask for an Auckland PostgreSQL summary or an OfficeCLI document listing."],
        ["03", "Inspect the evidence", "Open MCP Activity and follow authentication, policy, dispatch and completion."],
        ["04", "Prove control", "Disable a tool or revoke a credential grant, repeat the request, then restore it."],
        ["05", "Create and share", "Save an HTML artifact, focus its pane, share it with Designer, then revoke access."],
    ], col_widths=[650, 2500, 7650], fills=[GOLD, CREAM, MIST], colors=[NAVY, NAVY, INK],
       font_size="9pt", bold=False, margin_y="165", caption="Ten-minute Datum Sync demonstration sequence")
    table([["START · repository root · python demo/manage.py start · Portal 127.0.0.1:8210 · Worker 127.0.0.1:8220"]],
          col_widths=[10800], fills=[NAVY], colors=[WHITE], font_size="8.5pt", bold=True,
          margin_y="130", caption="Local demonstration start command")
    table([["DATUM SYNC", "Connect to a useful agent while identity, MCP access, credentials, evidence and artifacts remain governed in one place."]],
          col_widths=[2200, 8600], fills=[GOLD, BLUE], colors=[NAVY, WHITE], font_size="11pt",
          bold=True, margin_y="165", caption="Datum Sync product statement")

    qadd("/", "header", {"type": "default", "text": "DATUM SYNC  /  GOVERNED AGENT FLEET", "size": "7.5pt", "color": GREY})
    qadd("/", "footer", {"type": "first", "text": "Product overview  ·  September 2026", "size": "7pt", "color": GREY, "align": "center"})
    qadd("/", "footer", {"type": "default", "text": "Datum Sync product overview  ·  Page ", "field": "page", "size": "7pt", "color": GREY, "align": "center"})
    qset("/settings", {"updateFields": "true"})

    batch = HERE / "source" / "walkthrough-batch.json"
    batch.write_text(json.dumps(COMMANDS, ensure_ascii=False, indent=2))
    run("batch", DOCX, "--input", batch, "--json")
    run("save", DOCX)
    run("close", DOCX)
    print(DOCX)


if __name__ == "__main__":
    build()
