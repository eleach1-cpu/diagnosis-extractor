# Diagnosis Extractor
Author: Eric Leach — https://ratemyvso.net

A small offline Windows tool that reads medical records and lists the diagnosis codes in them.

Point it at a PDF, Word, text, HTML file, or a whole folder and it writes a plain text report:

```
file | page/line | code | description | nearest date
```

Every code is validated against the CDC ICD-10-CM code set, so look-alike tokens (device models,
account numbers, dosages) are dropped. Diagnoses coded under ICD-9-CM or SNOMED CT are listed too,
tagged with the system they came from. Diagnoses written in plain words with no code beside them
are reported with `-` in the code column. Scanned pages are read with OCR.

The report ends with a paste-ready list of just the ICD-10 codes; the window has a **Copy ICD-10
codes** button for the same list.

## This is not medical or legal advice

The tool reports what it can find. It misses things like scanned pages, handwriting, unusual layouts,
faint print and photographed paper records all defeat it, dates are approximate, and codes can be
misread or tied to the wrong date. Treat every report as a starting point and confirm each item
against the source document. Provided as-is, with no warranty. The program will not run until the
terms have been read and accepted; that acceptance is recorded locally.

## Privacy

Runs entirely on your computer. No network calls, no telemetry, no cloud OCR. The OCR model runs
locally. Nothing is sent anywhere.

**No medical records are in this repository, and none should ever be committed to it.** The
`.gitignore` blocks documents, reports and local state files for that reason.

## Running from source

Python 3.11+:

```
pip install pymupdf python-docx beautifulsoup4 rapidocr-onnxruntime pillow
python source/icd_gui.py
```

`icd10_data.tsv` (the CDC ICD-10-CM code table) must sit beside the program. It is not in this
repository, build it from the CDC order file with `source/build_icd_data.py`, as described in
`source/HOW_TO_REBUILD.txt`.

The engine also has a command-line entry point in `source/icd_extract.py`, which refuses to run
until the terms have been accepted in the window at least once.

## Building the Windows executable

See `source/HOW_TO_REBUILD.txt` for the exact PyInstaller command. The result is a single
`icd_extract_gui.exe` that is shipped alongside `icd10_data.tsv`.

## Layout

| Path | What |
|---|---|
| `source/icd_extract.py` | Extraction engine, code validation, report writing, CLI |
| `source/icd_gui.py` | The window |
| `source/icd_disclaimer.py` | Terms text, consent gate, acceptance record |
| `source/icd_theme.py` | Shared look: palette, fonts, ttk styles, widget helpers |
| `source/build_icd_data.py` | Builds `icd10_data.tsv` from the CDC order file |
| `docs/USER_GUIDE.txt` | The guide that ships next to the executable |

## Tests

The test suite is not published here: its fixtures were pasted out of real medical records. A
scrubbed version using invented data can be added later.
