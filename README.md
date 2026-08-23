# Diagnosis Extractor
Author: Eric Leach — https://ratemyvso.net

A small offline Windows tool that reads medical records and lists the diagnosis codes in them.

Point it at a PDF, Word, text, HTML, or clinical XML (CDA/CCD) file, or a whole folder and it writes a plain text report:

```
file | page/line | code | description | nearest date
```

Every code is validated against the CDC ICD-10-CM code set, so look-alike tokens (device models,
account numbers, dosages) are dropped. Diagnoses coded under ICD-9-CM or SNOMED CT are listed too,
tagged with the system they came from. Diagnoses written in plain words with no code beside them
are reported with `-` in the code column. Scanned pages are read with OCR.

The report ends with a paste-ready list of just the ICD-10 codes; the window has a **Copy ICD-10
codes** button for the same list.

## Potential VA Diagnostic Code (optional)

If `dc_reference.json` sits beside the program, the report gains one extra section listing, for
each ICD-10 code found, the VA diagnostic code that condition is commonly rated under:

```
   ICD-10       DC     BASIS                              VA DIAGNOSTIC CODE
   L40          7816   category, 11 of 12 agree (92%)     Psoriasis
   M20.41       5282   direct (verified)                  Hammer toe
   K62          -      category vote split, top 7332 with 4 of 9 (44%)
   Z79.899      -      no mapping in this reference
```

**This is a research cross-reference, not an official VA mapping.** It is not a rating, not an
entitlement decision, and not advice about what to claim. A three-character category with no
mapping of its own is answered by its more specific children only when at least 60% of them
agree; below that it reports the split instead of picking. Codes the reference does not cover
say so rather than guessing.

The section is entirely optional and fails open. With the file missing, unreadable, malformed,
or altered since it was built, the program extracts exactly as before and the section reports
the reference as unavailable. Nothing else in the report changes, and the final ICD-10 code
block is never affected.

`dc_reference.json` and `dc_reference.manifest.json` ship together or not at all. The manifest
is checked at run time: the program hashes the reference and uses it only if the manifest
vouches for exactly those bytes, and it shape-checks every row before believing any of it. The
risk being managed is not a crash. A damaged file can still be valid JSON with the right schema,
and the cost of trusting it is a confident, wrong diagnostic code printed beside a real
diagnosis.

The file is generated offline by `source/build_dc_reference.py` from the ICD-10-to-DC crosswalk
published at [ratemyvso.net/dc/icd-codes](https://ratemyvso.net/dc/icd-codes). It is not in this
repository; see `source/HOW_TO_REBUILD.txt`. The program makes no network call to build or use
it.

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
| `source/build_dc_reference.py` | Builds the optional `dc_reference.json` cross-reference |
| `docs/USER_GUIDE.txt` | The guide that ships next to the executable |

## Tests

The test suite is not published here: its fixtures were pasted out of real medical records. A
scrubbed version using invented data can be added later.
