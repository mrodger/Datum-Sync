"""Record the Datum Sync demonstration as one video per scene, plus annotated screenshots.

Run against the live local stack (`python demo/manage.py start`) on the host that has
the Codex worker signed in. Each scene records into its own browser context so the
result is a set of short, independently usable clips:

    python docs/walkthrough/record_demo.py            # every scene
    python docs/walkthrough/record_demo.py 3 4 7      # only these scene numbers
    python docs/walkthrough/record_demo.py --list     # scene index

Outputs:
    docs/walkthrough/video/NN-slug.webm     one clip per scene (1600x1000)
    docs/walkthrough/video/manifest.json    scene, file, duration, narration
    docs/walkthrough/assets/*.png           annotated stills used by the brochure

A scene that cannot complete (for example the worker is not signed in to Codex)
is reported and skipped; the others still record. Nothing here prints or stores
tokens, the operator password, or upstream secrets.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
VIDEO = HERE / "video"
PORTAL = "http://127.0.0.1:8210"
WORKER = "http://127.0.0.1:8220"
SIZE = {"width": 1600, "height": 1000}
BEAT = 1400          # ms pause after a meaningful UI state, so a viewer can read it
MODEL_TIMEOUT = 240_000  # ms to wait for a Codex turn

# Prompts from the walkthrough script. Keep them identical to what the persona bundles allow.
PROMPT_OFFICE = "Review the quarterly brief and test its claims against the available operations data."
PROMPT_POSTGRES = "Summarize the available operations data and call out the regional differences."
PROMPT_WORMS = ("Resolve Crassostrea gigas and Perna canaliculus with WoRMS, compare their classifications, "
                "and use the persona catalogue to recommend who should continue the work.")
PROMPT_MAP = "Create a Wellington Leaflet map and explain which evidence you would add next."
PROMPT_ARTIFACT = "Create a small HTML artifact that summarizes the Auckland results and save it for this agent."
PROMPT_SHARE = "Share this artifact with designer."
REVOKED_TOOL = "postgres__orders_summary"

STYLE = """
.__walk_note{position:fixed;z-index:2147483647;display:flex;align-items:flex-start;gap:7px;max-width:300px;padding:7px 10px 7px 7px;border-radius:8px;background:#1d3a5c;color:#fff;border:1px solid rgba(255,255,255,.25);box-shadow:0 6px 18px rgba(15,25,35,.3);font:600 12px/1.35 'DM Sans',system-ui,sans-serif;pointer-events:none}
.__walk_note b{display:grid;place-items:center;flex:0 0 23px;height:23px;border-radius:50%;background:#c89632;color:#0f1923;font:700 13px 'Space Grotesk',system-ui,sans-serif}
.__walk_target{position:fixed;z-index:2147483646;border:3px solid #c89632;border-radius:7px;box-shadow:0 0 0 3px rgba(200,150,50,.25);pointer-events:none}
.__walk_caption{position:fixed;z-index:2147483647;left:24px;bottom:24px;max-width:640px;padding:10px 14px;border-radius:8px;background:rgba(29,58,92,.94);color:#fff;font:500 15px/1.4 'DM Sans',system-ui,sans-serif;box-shadow:0 6px 18px rgba(15,25,35,.3);pointer-events:none}
.__walk_caption b{color:#c89632;font-family:'Space Grotesk',system-ui,sans-serif;letter-spacing:.04em;text-transform:uppercase;font-size:11px;display:block;margin-bottom:3px}
"""


# ---------------------------------------------------------------- helpers
def clear(page: Page) -> None:
    page.evaluate("document.querySelectorAll('.__walk_note,.__walk_target,.__walk_caption,#__walk_style').forEach(e=>e.remove())")


def _style(page: Page) -> None:
    if not page.evaluate("!!document.getElementById('__walk_style')"):
        page.evaluate("css=>{const s=document.createElement('style');s.id='__walk_style';s.textContent=css;document.head.append(s)}", STYLE)


def caption(page: Page, kicker: str, text: str, hold: int = BEAT) -> None:
    """On-screen narration card for the video; removed after `hold` ms."""
    _style(page)
    page.evaluate("v=>{document.querySelectorAll('.__walk_caption').forEach(e=>e.remove());const c=document.createElement('div');c.className='__walk_caption';const b=document.createElement('b');b.textContent=v.k;c.append(b,document.createTextNode(v.t));document.body.append(c)}",
                  {"k": kicker, "t": text})
    page.wait_for_timeout(hold)


def annotate(page: Page, specs) -> None:
    clear(page)
    _style(page)
    for number, selector, label, position in specs:
        box = page.locator(selector).first.bounding_box()
        if not box:
            continue
        page.evaluate("""v=>{const t=document.createElement('div');t.className='__walk_target';Object.assign(t.style,{left:(v.x-3)+'px',top:(v.y-3)+'px',width:v.w+'px',height:v.h+'px'});document.body.append(t);const n=document.createElement('div');n.className='__walk_note';n.innerHTML='<b>'+v.number+'</b><span></span>';n.querySelector('span').textContent=v.label;Object.assign(n.style,{left:v.nx+'px',top:v.ny+'px'});document.body.append(n)}""",
                      {"x": box["x"], "y": box["y"], "w": box["width"], "h": box["height"],
                       "number": number, "label": label, "nx": position[0], "ny": position[1]})


def shot(page: Page, name: str, specs=()) -> None:
    annotate(page, specs)
    page.wait_for_timeout(600)
    page.screenshot(path=str(ASSETS / name))
    page.wait_for_timeout(BEAT)
    clear(page)


def operator_password() -> str:
    return (ROOT / ".runtime/operator.txt").read_text().split("Password: ")[1].strip()


def sign_in(page: Page) -> None:
    page.goto(PORTAL)
    if page.locator("#signin-password").count():
        page.locator("#signin-password").fill(operator_password())
        page.locator("#signin-submit").click()
    expect(page.locator("#view")).to_have_attribute("data-ready", "overview")


def portal(page: Page, label: str, ready: str) -> None:
    page.get_by_role("link", name=label, exact=True).click()
    expect(page.locator("#view")).to_have_attribute("data-ready", ready)
    page.wait_for_timeout(500)


def row(page: Page, text: str):
    return page.locator("tr", has_text=text).first


def connect_worker(page: Page, agent: str = "researcher", duration: str = "900") -> None:
    page.goto(WORKER + "/connect")
    expect(page.locator("#dialog")).to_be_visible()
    page.locator("#dialog select").first.select_option(agent)
    page.locator("#dialog select[aria-label='Agent session length']").select_option(duration)
    page.wait_for_timeout(BEAT)
    page.get_by_role("button", name="Approve connection", exact=True).click()
    page.wait_for_url(WORKER + "/")
    expect(page.locator("#input")).to_be_enabled(timeout=30_000)


def ask(page: Page, prompt: str) -> None:
    """Type a prompt into the worker and wait for the Codex turn to finish."""
    page.locator("#input").fill(prompt)
    page.wait_for_timeout(700)
    page.locator("#btn-send").click()
    expect(page.locator("#run-state")).to_have_text("Running", timeout=15_000)
    expect(page.locator("#run-state")).to_have_text("Idle", timeout=MODEL_TIMEOUT)
    page.wait_for_timeout(BEAT)


def worker_card(page: Page, name: str, hold: int = 2500) -> None:
    page.locator(f"[data-card='{name}']").click()
    expect(page.locator("#worker-card-overlay")).to_be_visible()
    page.wait_for_timeout(hold)


def close_card(page: Page) -> None:
    page.locator("#worker-card-close").click()
    page.wait_for_timeout(400)


def open_latest_timeline(page: Page) -> None:
    portal(page, "MCP Activity", "mcpactivity")
    page.get_by_role("button", name="Timeline").first.click()
    expect(page.locator("#dialog")).to_be_visible()


def close_dialog(page: Page) -> None:
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)


# ---------------------------------------------------------------- scenes
def scene_01_problem(page: Page) -> None:
    sign_in(page)
    shot(page, "01-portal-overview.png", [
        (1, "#nav", "Operator navigation: governance surfaces stay in Datum Sync.", (264, 92)),
        (2, ".tiles", "Start with enrolment, access testing, approvals or proxy management.", (360, 270)),
        (3, ".dash-rail", "Live authority and gateway state at a glance.", (1250, 620))])
    portal(page, "Principals", "principals")
    caption(page, "Establish the problem", "The Researcher has its own identity and permission bundle, but owns no Office credential, database password or upstream token.", 3500)
    shot(page, "02-principals.png", [
        (1, ".auth-panel", "Each persona is a registered principal with an independent bundle.", (350, 165)),
        (2, ".toolbar", "Search and filter the single-user agent fleet.", (1120, 160))])
    row(page, "researcher").get_by_role("button", name="MCP access").click()
    expect(page.locator("#dialog")).to_be_visible()
    caption(page, "MCP access", "Effective grants, live sessions and recent connections for one agent.", 3000)
    shot(page, "09-mcp-access.png", [(1, "#dialog", "Which servers, which credential grants, what it called recently.", (1180, 120))])
    close_dialog(page)


def scene_02_boundary(page: Page) -> None:
    sign_in(page)
    portal(page, "Secrets & Proxies", "integrations")
    caption(page, "The service boundary", "Operators register MCP servers and store upstream credentials once. Agents see only the tools their grants allow.", 3500)
    shot(page, "03-secrets-proxies.png", [
        (1, ".auth-panel", "MCP servers, tool states and managed upstream identities.", (340, 170)),
        (2, ".page-head button", "Register another governed MCP endpoint here.", (1180, 105))])
    page.mouse.wheel(0, 600)
    page.wait_for_timeout(BEAT)
    caption(page, "Per-tool control", "Every discovered tool has its own enable state and minimum tier.", 3000)
    page.mouse.wheel(0, -600)


def scene_03_connect(page: Page) -> None:
    sign_in(page)
    page.goto(WORKER)
    caption(page, "The federated worker", "A small Codex client. Four status cards: agent, model, the Datum Sync MCP route, and disabled local tools.", 3500)
    page.goto(WORKER + "/connect")
    expect(page.locator("#dialog")).to_be_visible()
    caption(page, "Consent", "Choose the agent and a 15-minute demo or 2, 4 or 8-hour session. Datum Sync enforces the deadline.", 3000)
    shot(page, "10-consent.png", [(1, "#dialog", "One short-lived agent token for one worker session.", (1130, 140))])
    page.locator("#dialog select").first.select_option("researcher")
    page.locator("#dialog select[aria-label='Agent session length']").select_option("900")
    page.wait_for_timeout(BEAT)
    page.get_by_role("button", name="Approve connection", exact=True).click()
    page.wait_for_url(WORKER + "/")
    expect(page.locator("#input")).to_be_enabled(timeout=30_000)
    caption(page, "Connected", "Governed session indicator, Researcher display name, persona-specific starter prompts.", 3000)
    shot(page, "06-worker-connected.png", [
        (1, ".worker-context", "The selected Datum agent defines the worker identity.", (270, 110)),
        (2, ".worker-nav", "Session, MCP, proxy and principal cards remain worker-local views.", (270, 390)),
        (3, "#input-area", "Chat is ready immediately after the agent connects.", (520, 840)),
        (4, "#artifact-pane", "The inspection pane remains beside the conversation.", (1195, 210))])


def scene_04_office(page: Page) -> None:
    sign_in(page)
    connect_worker(page)
    caption(page, "OfficeCLI task", "Codex chooses an MCP tool. The client shows only server, tool, state and duration.", 2500)
    ask(page, PROMPT_OFFICE)
    shot(page, "11-worker-trace.png", [(1, ".worker-trace", "Arguments, document text and credentials never enter this trace.", (1060, 300))])
    worker_card(page, "activity")
    caption(page, "Session-local MCP activity", "The worker sees its own calls; the operator sees the deterministic timeline.", 2500)
    close_card(page)
    open_latest_timeline(page)
    caption(page, "Evidence", "Receipt, authentication, session validation, authorization, upstream dispatch, completion.", 4000)
    shot(page, "12-mcp-timeline.png", [(1, "#dialog", "No prompts, arguments or results are stored.", (1180, 120))])
    close_dialog(page)
    shot(page, "04-mcp-activity.png", [
        (1, ".auth-panel", "Every endpoint flow receives a deterministic trace.", (340, 170)),
        (2, "tbody tr", "Expand a flow to inspect authentication, authorization and dispatch.", (900, 330))])


def scene_05_postgres(page: Page) -> None:
    sign_in(page)
    connect_worker(page)
    caption(page, "PostgreSQL task", "A fixed read-only operation, not arbitrary SQL. The database credential never enters the worker.", 3000)
    ask(page, PROMPT_POSTGRES)
    worker_card(page, "proxies")
    caption(page, "Agent-visible credentials", "Labels, fingerprints and versions. Never the secret value.", 3000)
    close_card(page)


def scene_06_harness(page: Page) -> None:
    sign_in(page)
    connect_worker(page)
    caption(page, "Real harness tools", "Persona discovery, a bounded WoRMS lookup and local Leaflet rendering, all through the same plane.", 3000)
    ask(page, PROMPT_WORMS)
    ask(page, PROMPT_MAP)
    page.locator("[data-view='trace']").click()
    page.wait_for_timeout(2000)


def scene_07_revocation(page: Page) -> None:
    sign_in(page)
    portal(page, "Secrets & Proxies", "integrations")
    target = row(page, REVOKED_TOOL)
    target.scroll_into_view_if_needed()
    caption(page, "Live revocation", f"Disable {REVOKED_TOOL}. No worker restart, no credential to rotate inside the agent.", 3000)
    target.get_by_role("button", name="Disable tool").click()
    expect(target).to_contain_text("disabled")
    shot(page, "13-revocation.png", [(1, f"tr:has-text('{REVOKED_TOOL}')", "Applies at the next catalogue or call.", (900, 200))])
    connect_worker(page)
    ask(page, PROMPT_POSTGRES)
    caption(page, "Denied", "The tool is absent or refused. The agent has no direct credential to bypass the decision.", 3500)
    page.goto(PORTAL + "/#mcpactivity")
    expect(page.locator("#view")).to_have_attribute("data-ready", "mcpactivity")
    caption(page, "Recorded", "The denied call is in the same evidence stream as the successful ones.", 3000)
    portal(page, "Secrets & Proxies", "integrations")
    target = row(page, REVOKED_TOOL)
    target.scroll_into_view_if_needed()
    target.get_by_role("button", name="Enable tool").click()
    expect(target).to_contain_text("active")
    caption(page, "Restored", "Server state, tool state and per-agent credential grants are separate controls.", 3000)


def scene_08_fleet_review(page: Page) -> None:
    sign_in(page)
    portal(page, "Principals", "principals")
    row(page, "researcher").get_by_role("button", name="MCP access").click()
    expect(page.locator("#dialog")).to_be_visible()
    caption(page, "Fleet review", "Which servers can this agent reach, which grants make that effective, what has it called recently.", 4500)
    close_dialog(page)
    portal(page, "Resources", "resources")
    shot(page, "05-resources.png", [
        (1, ".auth-panel", "Inventory shows ownership, type, version, size and expiry.", (340, 170)),
        (2, ".auth-panel:nth-of-type(2)", "Explicit agent-to-agent grants can be revoked by an operator.", (870, 520))])


def scene_09_artifacts(page: Page) -> None:
    sign_in(page)
    connect_worker(page)
    caption(page, "Governed artifacts", "Output is saved through Datum Sync as a versioned, private resource.", 2500)
    ask(page, PROMPT_ARTIFACT)
    page.locator("[data-view='artifacts']").click()
    page.locator(".artifact-item").first.click()
    expect(page.locator("#artifact-frame")).to_be_visible()
    shot(page, "07-artifact-split.png", [
        (1, ".artifact-catalogue", "Only resources owned by or shared with this agent appear.", (1040, 220)),
        (2, ".artifact-meta", "Owner, MIME type, logical path and access mode stay visible.", (1040, 575)),
        (3, ".artifact-frame", "HTML renders in a network-isolated sandbox.", (1060, 820))])
    page.locator("#btn-pane-focus").click()
    expect(page.locator("#artifact-pane")).to_have_class(re.compile(r".*focused.*"))
    shot(page, "08-artifact-focused.png", [
        (1, ".artifact-catalogue", "Browse governed artifacts while keeping the active preview.", (80, 220)),
        (2, ".artifact-preview", "The same pane object expands for detailed work.", (760, 130)),
        (3, "#btn-pane-focus", "Restore the split layout with this control or Escape.", (1210, 74))])
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    caption(page, "Share", "A named, time-bounded read grant to one agent in the same fleet.", 2500)
    ask(page, PROMPT_SHARE)
    portal(page, "Resources", "resources")
    caption(page, "Operator view", "Owner, version, type, active share, expiry and payload-free events. Revoke here; Designer loses access on the next refresh.", 4500)


def scene_10_close(page: Page) -> None:
    sign_in(page)
    connect_worker(page)
    caption(page, "Close the session", "Disconnect terminates this browser's Codex worker and discards its short-lived token. Upstream credentials stay in Datum Sync.", 4000)
    page.locator("#disconnect").click()
    page.wait_for_timeout(BEAT)
    page.goto(PORTAL + "/#mcpactivity")
    expect(page.locator("#view")).to_have_attribute("data-ready", "mcpactivity")
    caption(page, "End", "Completed and denied calls, one evidence stream.", 3500)


SCENES = [
    ("problem", "Establish the problem", scene_01_problem,
     "Datum Sync is an auth and governance plane for an agent fleet. The Researcher has an identity and a permission bundle, not credentials."),
    ("boundary", "Show the service boundary", scene_02_boundary,
     "MCP servers and upstream credentials are registered once. Agents see only granted tools; the secret stays behind the proxy."),
    ("connect", "Connect the federated worker", scene_03_connect,
     "The client is intentionally small. Codex owns the model loop; Datum Sync owns every operational capability."),
    ("office-task", "Run an OfficeCLI task", scene_04_office,
     "Codex chose an MCP tool. The trace shows server, tool, state and duration, never arguments or content."),
    ("postgres-task", "Run a PostgreSQL task", scene_05_postgres,
     "A fixed read-only operation, not arbitrary SQL. The database credential never enters the worker."),
    ("harness-tools", "Use real Datum Harness tools", scene_06_harness,
     "Persona discovery, WoRMS taxonomy and Leaflet rendering are real capabilities behind the same governance plane."),
    ("revocation", "Demonstrate live revocation", scene_07_revocation,
     "Disable a tool, repeat the request, watch the denial land in the evidence stream, restore it."),
    ("fleet-review", "Show fleet review", scene_08_fleet_review,
     "Which servers, which grants, what was called. Every record already attaches to an explicit principal."),
    ("artifacts", "Create and share an artifact", scene_09_artifacts,
     "Versioned, private by default, shared by explicit grant, revocable by the operator."),
    ("close", "Close the session", scene_10_close,
     "Disconnect discards the worker's token; upstream credentials never left Datum Sync."),
]


def record(pw, number: int, slug: str, title: str, fn, narration: str) -> dict:
    VIDEO.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    browser = pw.chromium.launch(slow_mo=180)
    context = browser.new_context(viewport=SIZE, device_scale_factor=1, bypass_csp=True,
                                  record_video_dir=str(VIDEO / ".tmp"), record_video_size=SIZE)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    started = time.time()
    status, detail = "ok", ""
    try:
        fn(page)
    except Exception as exc:  # noqa: BLE001 - one scene must not stop the others
        status, detail = "failed", f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
    finally:
        page.wait_for_timeout(800)
        video = page.video
        context.close()
        browser.close()
    target = VIDEO / f"{number:02d}-{slug}.webm"
    if video:
        shutil.move(video.path(), target)
    for leftover in (VIDEO / ".tmp").glob("*.webm"):
        leftover.unlink()
    entry = {"scene": number, "slug": slug, "title": title, "file": target.name,
             "seconds": round(time.time() - started, 1), "status": status, "narration": narration}
    if detail:
        entry["error"] = detail
    if errors:
        entry["page_errors"] = errors[:5]
    print(f"[{status:6}] {number:02d} {title} ({entry['seconds']}s){' - ' + detail if detail else ''}")
    return entry


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for i, (slug, title, _, _) in enumerate(SCENES, 1):
            print(f"{i:2d}  {slug:14}  {title}")
        return 0
    wanted = {int(a) for a in argv if a.isdigit()} or set(range(1, len(SCENES) + 1))
    (VIDEO / ".tmp").mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    with sync_playwright() as pw:
        for i, (slug, title, fn, narration) in enumerate(SCENES, 1):
            if i in wanted:
                manifest.append(record(pw, i, slug, title, fn, narration))
    shutil.rmtree(VIDEO / ".tmp", ignore_errors=True)
    existing = {}
    path = VIDEO / "manifest.json"
    if path.exists():
        existing = {e["scene"]: e for e in json.loads(path.read_text())}
    for entry in manifest:
        existing[entry["scene"]] = entry
    path.write_text(json.dumps([existing[k] for k in sorted(existing)], indent=2))
    failed = [e for e in manifest if e["status"] != "ok"]
    print(f"\n{len(manifest) - len(failed)} of {len(manifest)} scenes recorded -> {VIDEO}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
