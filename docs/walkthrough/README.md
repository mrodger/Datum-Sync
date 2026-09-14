# Datum Sync product overview and demo media

Source and generated forms of the six-page Datum Sync brochure, plus the per-scene
demonstration videos. Everything is rebuilt from the running local stack.

- `output/Datum-Sync-Walkthrough.docx` — editable Word document (OfficeCLI).
- `output/Datum-Sync-Walkthrough.pdf` — six-page landscape delivery copy, brand fonts embedded.
- `assets/` — annotated screenshots (`01`–`13`) captured by `record_demo.py`.
- `video/NN-slug.webm` — one clip per walkthrough scene; `video/manifest.json` lists each clip with its narration line.
- `record_demo.py` — drives the ten walkthrough scenes with Playwright, recording a video and taking the stills.
- `build_walkthrough.py` — assembles the DOCX through [OfficeCLI](https://github.com/iOfficeAI/OfficeCLI) using the Datum design tokens.
- `export_pdf.py` — renders the DOCX to HTML and PDF, embedding DM Sans, Space Grotesk and JetBrains Mono from `worker/static/branding/fonts`.
- `source/walkthrough-batch.json` — the generated OfficeCLI command batch.

## Rebuild everything

On the host that runs the stack and has the Codex worker signed in:

```bash
cd <repository root>
python demo/manage.py start
python demo/provision_personas.py --clean-test-artifacts --register   # clean fleet, seeded providers

python docs/walkthrough/record_demo.py                 # videos + screenshots (about 12 minutes)
OFFICECLI_SKIP_UPDATE=1 python docs/walkthrough/build_walkthrough.py
officecli validate docs/walkthrough/output/Datum-Sync-Walkthrough.docx
python docs/walkthrough/export_pdf.py
```

`record_demo.py --list` prints the scene index; `record_demo.py 3 7` re-records only
those scenes. A scene that cannot complete is reported and skipped, and the manifest
records its error, so one failure never costs the whole run. Scenes 4, 5, 6, 7, 9 and 10
send prompts to Codex and wait up to four minutes per turn.

Clips are WebM (VP8). For slides or a video editor that wants MP4:

```bash
for f in docs/walkthrough/video/*.webm; do ffmpeg -y -i "$f" -c:v libx264 -pix_fmt yuv420p -crf 20 "${f%.webm}.mp4"; done
```

The build enforces exactly six rendered pages and rejects page overflow. The capture
script reads the private operator password at runtime and never writes it, an agent
token, or any upstream secret to this folder.
