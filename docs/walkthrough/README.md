# Datum Sync product overview

This folder contains the source and generated forms of the five-page Datum Sync product brochure.

- `output/Datum-Sync-Walkthrough.docx` is the editable Word document.
- `output/Datum-Sync-Walkthrough.pdf` is the matching five-page landscape delivery copy.
- `assets/` contains the eight annotated screenshots captured from the running prototype.
- `capture_screenshots.py` signs into the local prototype, connects the Researcher worker and recreates the screenshots.
- `build_walkthrough.py` assembles the DOCX through [OfficeCLI](https://github.com/iOfficeAI/OfficeCLI).
- `source/walkthrough-batch.json` is the generated atomic OfficeCLI command batch.

## Rebuild

Start the prototype first:

```bash
cd <repository root>
python demo/manage.py start
```

Capture the current UI and rebuild the Word document:

```bash
python docs/walkthrough/capture_screenshots.py
OFFICECLI_SKIP_UPDATE=1 python docs/walkthrough/build_walkthrough.py
officecli validate docs/walkthrough/output/Datum-Sync-Walkthrough.docx
python docs/walkthrough/export_pdf.py
```

The PDF is exported from OfficeCLI's self-contained HTML rendering with the repository Playwright Chromium. The build enforces exactly five pages and rejects page overflow. The checked-in PDF and DOCX use the same document content and embedded images. The capture script reads the private operator password at runtime and never writes it or an agent token to the walkthrough folder.

The checked-in outputs and `source/walkthrough-batch.json` were generated on the prototype's Ubuntu host before the port; the start command in their final table still names that host's paths (`local/manage.py`). The next rebuild from this branch picks up `demo/manage.py`.
