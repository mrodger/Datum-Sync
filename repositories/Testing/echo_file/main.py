"""Integration fixture: returns the uploaded file's contents as an artifact.

Deliberately the worst case for a FILE parameter. If the parameter carried a
filesystem path a caller could name any file the worker can read and get it
back in the response, so this is the workspace that proves the id is resolved
against the upload store rather than trusted.
"""


async def run(params, emit, connections):
    path = params["INPUT_FILE"]
    with open(path, "rb") as f:
        body = f.read()
    await emit("log", {"level": "info", "message": f"read {len(body)} bytes"})
    # LABEL is echoed so a test can prove the ordinary text fields of a
    # multipart request reach run(), not merely that the API stored them.
    return [{
        "name": "echoed",
        "type": "text/plain",
        "content": f"LABEL={params['LABEL']}\n" + body.decode("utf-8", "replace"),
    }]
