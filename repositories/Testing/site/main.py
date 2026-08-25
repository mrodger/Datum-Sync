"""Integration fixture: a workspace whose output is a hosted site.

Every other Testing workspace returns a file. This one returns a *directory*,
which is the only shape a `service/static` output may have, and it is the only
fixture that exercises the path from `run()` through `child._store`'s copytree,
`runner._reconcile`'s service/directory check, `services.register` and finally
a GET on `/serve/`.

It writes a nested file as well as an index, because a static server that only
ever handles `/` looks correct right up until someone links a stylesheet.
"""
import tempfile
from pathlib import Path

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>{heading}</title>
<link rel="stylesheet" href="assets/site.css">
<h1>{heading}</h1>
<p>Served by datum-sync from a job's artifacts.</p>
"""

CSS = "h1 { font-family: system-ui, sans-serif; }\n"


async def run(params, emit, connections):
    # A real workspace would build into its own working directory. mkdtemp
    # because this fixture has nowhere else to put it, and because the copy out
    # of it is exactly what is being tested: the served tree must end up in the
    # job's artifact directory, not wherever the workspace happened to build.
    build = Path(tempfile.mkdtemp(prefix="site-build-"))
    (build / "assets").mkdir()
    (build / "index.html").write_text(PAGE.format(heading=params["HEADING"]))
    (build / "assets" / "site.css").write_text(CSS)

    await emit("log", {"level": "info", "message": f"built site in {build}"})
    return [{"name": "_fixture-site", "type": "service/static", "path": str(build)}]
