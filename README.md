# Diagnosis Extractor
Author: Eric Leach — https://ratemyvso.net

A small offline Windows tool that reads medical records and lists the diagnosis codes in them.

Point it at a PDF, Word, text, HTML, or clinical XML (CDA/CCD) file, or a whole folder and it writes a plain text report:

```
file | page/line | code | description | nearest date
```

Every code is validated against the CDC ICD-10-CM code set, so look-alike tokens (device models,
account numbers, dosages) are dropped. Entries coded under ICD-9-CM or SNOMED CT are listed too,
tagged with the system they came from - and a SNOMED tag also says whether the entry is a
disorder, a finding, a procedure, an event or a situation. Each can optionally be crossed to
ICD-10-CM through a local reference file. Diagnoses written in plain words with no code beside them
are reported with `-` in the code column. Scanned pages are read with OCR.

The report ends with a paste-ready list of just the ICD-10 codes; the window has a **Copy ICD-10
codes** button for the same list.

The date column is approximate - the nearest date to the code. A date labeled as the patient's
date of birth is never used, and a letterhead date repeated in a page's banner yields to a date
from the body of the page. A date is never rejected for being old, and none is ever invented.

## Claim File Handoff (optional)

After a run finishes, **Create Claim File Handoff** writes a second document beside the report,
`<report name>_claim_handoff.txt`. It organizes what the run already found, in five sections: a
run summary with a short alias for each source file; the entries grouped by the VA diagnostic
code the local reference returned for them; the coded entries the reference had no result for,
said once instead of after every row; the entries that are **not** diagnoses and are no longer
presented as ones (Z-code encounters and status codes, SNOMED procedures and events, and named
entries the record did not say enough about, including anything that reads as an ordered
laboratory test); and the complete page-by-page evidence as an appendix.

Grouping means the reference returned the same diagnostic code, not that this document says the
entries are the same condition. Every original code and description is kept as the record wrote
it, and nothing is removed to shorten the sections: every retained entry appears once above and
once in the appendix. `F1 p5/d2` is source file F1, extracted page 5, the page the document
itself numbers 2; a trailing `*` on a date means the nearest date on that page.

Nothing is rescanned and nothing is added. It is not medical or legal advice, not an official VA
mapping, not a rating prediction, and not a recommendation about what to claim.

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

## Labeled ICD-9-CM codes (optional)

Older records code diagnoses in ICD-9-CM. Those rows are already reported, tagged `[ICD-9-CM]`
so they are never mistaken for ICD-10. If `icd9_gem_reference.json` sits beside the program as
well, the cross-reference section gains a second block that carries them the rest of the way:

```
   ICD-9-CM   ICD-10-CM          DC     BASIS                          VA DIAGNOSTIC CODE
   138        B91, G14           8011   bridged, 2 of 2 agree (100%)   Poliomyelitis, anterior
   250.00     E11.9              7913   bridged, 1 of 1 agree (100%)   Diabetes mellitus
   171.8      C47.8, C49.8       -      bridge split, top 5329 with 1 of 2 (50%)
   E812.0     V49.88XA           -      no diagnostic code for the bridge target(s)
   013.8      -                  -      not in the ICD-9 to ICD-10-CM GEM
```

The ICD-9 code is crossed to its ICD-10-CM target or targets through a **GEM** (General
Equivalence Mapping, a frozen conversion table), and those targets are then looked up in the
Potential-DC reference above. Both steps are shown, because both were taken.

**Only codes the record itself labels are bridged**: an `ICD-9` or `ICD-9-CM` label in the text,
or a CDA element whose `codeSystem` is the ICD-9-CM OID. An unlabeled `250.00` in a record is a
dose, a weight or an account number far more often than it is a diagnosis, and nothing here
treats a bare number as a diagnosis. SNOMED CT rows are never bridged through the GEM either;
they have their own map and their own rules, described in the next section.

One ICD-9 code often crosses to several ICD-10 codes. Every target that maps to a diagnostic
code casts one vote and the dominant code wins at 60% or better, the same bar the category
rollup uses. Below that the split is reported instead of an answer. A target that maps to
nothing is silent rather than dissenting, so one mapped target out of ten reads `1 of 1`, not
`1 of 10`. A row lists as many targets as the column fits and then says how many more there
are.

**A GEM is a conversion table, not a VA document.** Bridging is two removes from the diagnosis,
so the result is a research cross-reference and never a rating, an entitlement decision, or
advice about what to claim.

`icd9_gem_reference.json` and `icd9_gem_reference.manifest.json` ship together or not at all,
and are checked at run time exactly as the DC pair is. The bridge also needs the DC reference:
without it there is nothing to look the targets up in, and the section says the reference is
unavailable rather than presenting a missing diagnostic code as an ICD-9 fact. With the bridge
file missing or broken the program extracts exactly as before, every `[ICD-9-CM]` row is
untouched, and only the bridge block reports itself unavailable.

A bridged ICD-10-CM target is **never** substituted for the ICD-9 code in a diagnosis row and
**never** enters the final ICD-10 code block. That block feeds an outside ICD-10 lookup, which
would answer for a silently converted code confidently and wrongly.

The file is generated offline by `source/build_icd9_gem_reference.py` from a local GEM file,
whose path is given explicitly. It is not in this repository; see `source/HOW_TO_REBUILD.txt`.
The program makes no network call to build or use it.

## Labeled SNOMED CT codes (optional)

Many records code their problem list in SNOMED CT rather than ICD-10. Those rows are already
reported, tagged `[SNOMED CT]`. If `snomed_icd10cm_reference.json.gz` sits beside the program as
well, two things happen: every one of those rows gains the concept's **type**, and the report
gains a section saying what each concept is and what it translates to.

### A SNOMED code is not automatically a diagnosis

SNOMED CT codes disorders. It also codes findings, events, situations, procedures, substances,
organisms and specimens. `840546002` is `Exposure to severe acute respiratory syndrome
coronavirus 2 (event)`: it maps **directly and unconditionally** to `Z20.822`, and it is still
not a diagnosis of COVID-19. A direct map means the terminology translation needed no patient
context. It never means the record confirmed a disease.

So the concept's type comes from the licensed release itself, the semantic tag of the concept's
fully specified name, and it decides two things:

| Semantic tag | Shown as | May reach the Potential VA Diagnostic Code section? |
| --- | --- | --- |
| `disorder` | diagnosis | **Yes** |
| `finding` | finding | No |
| `procedure` | procedure | No |
| `event` | event | No |
| `situation` | situation | No |
| any other tag | `clinical concept: <tag>` | No |
| unreadable or absent | `clinical entry, type unavailable` | No |

**Eligibility fails closed.** Only the exact tag `disorder` opens the diagnostic lane. An
unknown or unreadable type is never treated as a diagnosis by default, because refusing a real
disorder costs a research lookup while accepting a procedure puts it on a claim as though a
doctor had diagnosed it. The record's own wording is never used to decide a type, and neither
is the ICD-10-CM target.

The record's entry is never deleted or reworded. Its row keeps the record's own description and
its own code; what changes is the label on it and whether it is offered a diagnostic code.

### The two sections

Disorders reach the existing cross-reference block:

```
   SNOMED CT          POTENTIAL ICD-10-CM MAP BASIS                                 DC / RESULT
   10001005           P36.9               conditional map, review required          (not resolved)
                      A41.9                                                         (not resolved)
        P36.9 applies when: IF AGE AT ONSET OF CLINICAL FINDING BEFORE 29.0 DAYS
        A41.9 applies when: otherwise
   100171000119109    J31.0               multiple unconditional map groups         6523   Bacterial rhinitis
                      J32.9                                                         6511   Sinusitis, ethmoid, chronic
```

Every SNOMED code, of every type, is reported in a separate terminology section. The map is
useful for a procedure and an event too; it is just not a diagnosis, and printing it under a
heading that says "Potential VA Diagnostic Code" would read as one:

```
   SNOMED CT          TYPE             POTENTIAL ICD-10-CM MAP BASIS
   10000006           finding          R07.9               direct, unconditional
   10001005           diagnosis        P36.9               conditional map, review required
                                       A41.9
   80146002           procedure        -                   not in this SNOMED CT to ICD-10-CM map
   840546002          event            Z20.822             direct, unconditional
```

The source is the **`ICD-10-CM complex map reference set`** and the **Description Snapshot**,
both from the licensed **SNOMED CT US Edition, March 2026 release**, and both must be from the
same release or the build fails. Only that reference set is read. The same release also carries
the international SNOMED CT to ICD-10 map, whose targets are ICD-10 rather than ICD-10-CM, and
reading it here would put the wrong code system into a lane that ends at an ICD-10-CM lookup.

**This map is not a flat table, and that is the whole reason this block reads differently from
the ICD-9 one.** A SNOMED concept can carry several map GROUPS, all of which apply together,
and several PRIORITIES inside a group, of which exactly one applies. So:

- **`direct, unconditional`** - one group, one rule, one target. Looked up.
- **`multiple unconditional map groups`** - several groups, all of which apply. Every target is
  looked up **independently** and the results are kept **separate**. Two ICD-10-CM codes that
  are meant to be used together are two answers, not a disagreement to be settled by a vote.
- **`conditional map, review required`** - the map needs something only the record can say
  (a patient's age at onset, type 1 versus type 2 diabetes, which episode of care this is).
  The candidate targets and the map's own conditions are shown; **nothing is chosen** and no
  diagnostic code is printed. A tool that picked one arm to look decisive would be handing over
  an answer the source explicitly declined to give.
- **`no ICD-10-CM classification from this map`** - the map says this concept cannot be
  classified with the data available. No target, no diagnostic code, and the reason is printed.
- **`not in this SNOMED CT to ICD-10-CM map`** - the identifier is simply absent from the map.

A trailing `?` on a target, such as `T14.8XX?`, is the map's placeholder for a 7th character it
cannot supply (initial encounter, subsequent encounter, sequela). It is preserved exactly as
written and the concept counts as conditional. Stripping the `?` would turn an admittedly
incomplete code into one that looks finished, and the finished-looking code would then be looked
up and answered for.

**Only codes the record itself labels are crossed**: a `SNOMED` or `SNOMED CT` label in the
text, or a CDA element whose `codeSystem` is the SNOMED CT OID `2.16.840.1.113883.6.96`. An
unlabeled long number in a record is an accession, an account or a device serial far more often
than it is a diagnosis, and nothing here treats a bare number as one.

A mapped ICD-10-CM target is **never** substituted for the SNOMED code in a diagnosis row and
**never** enters the final ICD-10 code block, for the same reason the ICD-9 bridge stays out of
it: that block feeds an outside ICD-10 lookup, which would answer for a silently converted code
confidently and wrongly.

`snomed_icd10cm_reference.json.gz` and `snomed_icd10cm_reference.manifest.json` ship together or
not at all, and are checked at run time exactly as the other two pairs are. With the map file
missing or broken the program extracts exactly as before, every `[SNOMED CT]` row is untouched,
and only the SNOMED sections report themselves unavailable.

**The two references are independent.** Without `dc_reference.json` the SNOMED concept's type,
its ICD-10-CM target and the map basis are all still reported; only the diagnostic code at the
end of the lane says `reference data unavailable`. A missing file is named for the file it is.

The file is **gzip and is read in place**: about 6.7 MB on disk, roughly 61 MB once decompressed,
so it is never expanded beside the executable. It adds about 7 MB to a download and is loaded
only when a report actually holds a SNOMED row.

It is generated offline by `source/build_snomed_icd10cm_reference.py`, which reads both files
straight out of the licensed release ZIP. Neither the ZIP nor the generated pair is in this
repository; see `source/HOW_TO_REBUILD.txt`. The program makes no network call to build or use
it.

### SNOMED CT license and attribution

The US Edition of SNOMED CT is developed and maintained by the U.S. National Library of Medicine
and is available to authorized UMLS Metathesaurus Licensees from the UTS Downloads site at
<https://uts.nlm.nih.gov>. **You must have a UMLS license and a UTS account to obtain these
files**; they are not distributed here.

This material includes SNOMED Clinical Terms (SNOMED CT) which is used by permission of the
International Health Terminology Standards Development Organisation (IHTSDO). All rights
reserved. SNOMED CT was originally created by the College of American Pathologists. "SNOMED" and
"SNOMED CT" are registered trademarks of International Health Terminology Standards Development
Organisation, trading as SNOMED International.

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
| `source/build_icd9_gem_reference.py` | Builds the optional `icd9_gem_reference.json` ICD-9 bridge |
| `source/build_snomed_icd10cm_reference.py` | Builds the optional `snomed_icd10cm_reference.json.gz` SNOMED CT map |
| `docs/USER_GUIDE.txt` | The guide that ships next to the executable |

## Tests

The test suite is not published here: its fixtures were pasted out of real medical records. A
scrubbed version using invented data can be added later.
