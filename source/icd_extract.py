"""
ICD-10 diagnosis extractor

Point it at a file or a folder. It reads PDF, Word (.docx), text (.txt),
HTML (.htm/.html) and structured clinical XML (.xml - CDA/CCD/IHE XDM
documents), finds every valid ICD-10-CM diagnosis code, and writes a
plain-text report:  file | page/line | code | description | nearest date.

- Codes are VALIDATED against the official CDC ICD-10-CM code set (icd10_data.tsv
  sits next to this program), so nursing-plan codes, form artifacts and stray
  letter-number tokens are discarded. The description printed is the OFFICIAL one,
  so it does not depend on the document's layout.
- Diagnoses coded under ANOTHER system (ICD-9-CM, SNOMED CT) are reported too, tagged
  with that system, so a problem list that predates ICD-10 is not silently dropped.
- SCANNED/IMAGE pages are read by local OCR (RapidOCR, on this machine only); rows
  found that way are tagged (OCR) because character recognition can misread. --no-ocr
  skips them for speed.
- Page numbers are real for PDF. Word/Text/HTML have no pages, so a line (or
  paragraph) number is given instead.
- The date is the nearest one found near the code and is APPROXIMATE - with
  free-form documents there is no reliable way to bind a date to a code. A date
  labeled as the patient's date of birth is never used, and a letterhead date
  repeated in a page's banner yields to a date from the body of the page.
- Two OPTIONAL local reference files add a Potential-VA-Diagnostic-Code research
  section (dc_reference.json + manifest) and an ICD-9-CM bridge into it
  (icd9_gem_reference.json + manifest). Both fail open: missing or damaged files
  cost the extra section and nothing else.

No network, ever. No old .doc.

Usage:
    icd_extract  <file-or-folder>  [-o report.txt]
"""

import sys, os, re, json, gzip, hashlib, argparse, datetime

VERSION = "3.0"

SUPPORTED = (".pdf", ".docx", ".txt", ".htm", ".html", ".xml")

# HL7 codeSystem OIDs a CDA/CCD document tags its <code>/<value>/<translation> elements with.
CDA_ICD10_OID = "2.16.840.1.113883.6.90"
CDA_ICD9_OID = "2.16.840.1.113883.6.103"
CDA_SNOMED_OID = "2.16.840.1.113883.6.96"
CDA_OTHER_SYS = {CDA_ICD9_OID: "ICD-9-CM", CDA_SNOMED_OID: "SNOMED CT"}

# First line of every report this tool writes. It is also how a later run recognises its own
# output and leaves it out of the scan.
REPORT_BANNER = "ICD-10 diagnosis codes extracted"

# The bare-code block at the foot of the report, for pasting into an outside lookup tool.
CODES_HEADING = "ICD-10 CODES"
CODES_URL = "https://ratemyvso.net/dc/icd-codes"

# ---- potential VA diagnostic code, a research cross-reference ---------------
# An OPTIONAL local reference built by build_dc_reference.py from the RateMyVSO ICD-10-to-DC
# crosswalk. It sits beside icd10_data.tsv and is read only when a report has ICD-10 codes to
# look up. It is a research aid and nothing more: it may add a note beside a code that has
# ALREADY been accepted, and it can never accept, reject, relabel or change a diagnosis. The
# CDC code list remains the only authority on whether an ICD-10 code is real.
#
# The whole feature fails open. A missing, unreadable, malformed or wrong-schema file leaves
# extraction and the original report exactly as they were, and the new section says so.
DC_REF_NAME = "dc_reference.json"
DC_MANIFEST_NAME = "dc_reference.manifest.json"
DC_REF_SCHEMA = "icd10-dc-reference/1"
DC_HEADING = "Potential VA Diagnostic Code - research cross-reference"
# Its own section, deliberately not a sub-block of the one above: a procedure or an exposure
# printed under a "Potential VA Diagnostic Code" heading reads as a diagnosis whatever the
# wording next to it says.
SNOMED_HEADING = "SNOMED CT clinical concepts - terminology cross-reference"

# A rollup answers for a three-character CATEGORY and nothing else. This is fixed by the
# schema, not read from the file: a length taken from the artifact would let a damaged artifact
# widen the rule that is supposed to police it. If a future schema ever rolls up at another
# depth, that is a schema version bump, not a value to be trusted at run time.
DC_CATEGORY_LEN = 3

# The agreement bar that separates an answer from a split vote, and the only confidence labels
# this schema defines. Both are fixed HERE rather than read from the artifact, for the same
# reason as the category length: a file must not be able to move the line that judges it.
#
# The floor is applied as exact integer arithmetic (votes * 100 >= floor * total) rather than a
# percentage in floating point, so a vote sitting exactly on the bar cannot fall on one side in
# the builder and the other side in the reader.
DC_FLOOR_PERCENT = 60
DC_CONFIDENCE_VALUES = ("verified", "high")

# Metadata the artifact must carry before any of it is believed. A file that parses as JSON and
# names the right schema can still be truncated, half-written or edited by hand, so the loader
# checks the whole shape rather than the wrapper.
DC_REQUIRED_META = {
    "source": ("name", "sha256", "bytes", "rows"),
    "code_list": ("name", "sha256", "rows"),
    "policy": ("floor_percent", "category_length", "vote_basis"),
    "counts": ("source_rows", "direct_usable", "cdc_missing", "direct_category_rows",
               "derived_categories", "ambiguous_categories", "distinct_dcs"),
}

# ---- ICD-10 code shape -----------------------------------------------------
# Letter (not U), a digit, then a digit/A/B; optionally a dot and 1-4 more.
CODE_RE = re.compile(r'\b([A-TV-Z][0-9][0-9AB])(?:\.([0-9A-TV-Z]{1,4}))?\b')

# The same shape anchored and without the decimal point, for validating a reference file's keys.
CODE_KEY_RE = re.compile(r'^[A-TV-Z][0-9][0-9AB][0-9A-TV-Z]{0,4}$')

# ---- labeled ICD-9-CM bridge, a second research cross-reference ------------
# A SECOND optional local reference, built by build_icd9_gem_reference.py from a frozen ICD-9
# to ICD-10-CM GEM. It sits beside dc_reference.json and is read only when a report already
# holds a diagnosis the RECORD ITSELF labels ICD-9. It bridges that code to its ICD-10-CM
# target or targets and then asks the Chunk 2 reference what those map to, so the answer is a
# research cross-reference two steps removed from the diagnosis and is labeled as such.
#
# It can never accept, reject, relabel or change a diagnosis, and a bridged ICD-10-CM target
# is never substituted for the ICD-9 code in a report row nor added to the paste block: that
# block feeds an outside ICD-10 lookup, and an ICD-9 code converted behind the veteran's back
# would be a confident wrong answer somewhere else.
#
# Like the DC reference, the whole feature fails open. A missing, unreadable, malformed or
# wrong-schema file costs the bridge and nothing else.
GEM_REF_NAME = "icd9_gem_reference.json"
GEM_MANIFEST_NAME = "icd9_gem_reference.manifest.json"
GEM_REF_SCHEMA = "icd9-gem-bridge/1"

# The only outcomes this schema defines for a bridged code. Fixed HERE, not read from the
# artifact, for the same reason as the DC floor: a file must not be able to name an outcome
# the reader has no rule for and have it printed anyway.
GEM_DISPOSITIONS = ("ambiguous", "mapped", "unmapped")

# ICD-9-CM, dotless and anchored: three to five digits, or E plus three or four, or V plus two
# to four. 250.00 -> 25000, E812.0 -> E8120, V18.0 -> V180. Used to validate the artifact's
# keys and to reject a "bridge" row that is not an ICD-9 code at all.
GEM_KEY_RE = re.compile(r'^(?:[0-9]{3,5}|E[0-9]{3,4}|V[0-9]{2,4})$')

# The GEM's own vintage is frozen, so a handful of its targets have since been retired from the
# CDC list. That is recorded in the artifact's ledger and gates nothing; see the builder.
GEM_REQUIRED_META = {
    "source": ("name", "sha256", "bytes", "rows"),
    "code_list": ("name", "sha256", "rows"),
    "policy": ("floor_percent", "dispositions", "vote_basis"),
    "counts": ("source_rows", "bridge_rows", "multi_target_rows", "target_slots",
               "distinct_targets", "targets_outside_code_list", "rows_with_no_current_target"),
}

# How wide the bridge-target column is, and therefore how many targets a row prints before it
# stops listing and says how many more there are. One ICD-9 aftercare code can bridge to over
# five hundred ICD-10 targets, and a row that wraps for a page is not more honest than a row
# that says so.
#
# A WIDTH rather than a count, because a count cannot keep the table aligned: four targets are
# twelve characters wide as "C47.8, C49.8" and forty as "E11.9, Z79.899, Z83.3, V49.88XA", and
# the second overruns the column and jams the diagnostic code onto the end of the target list
# where a reader cannot tell which is which.
GEM_TARGETS_WIDTH = 18

# ---- SNOMED CT to ICD-10-CM map, a third research cross-reference ----------
# A THIRD optional local reference, built by build_snomed_icd10cm_reference.py from the
# licensed SNOMED CT US Edition release. It sits beside dc_reference.json and is read only when
# a report already holds a diagnosis the RECORD ITSELF labels SNOMED CT. It shows the ICD-10-CM
# target or targets the licensed map gives, and looks up only the ones the map resolves without
# needing patient context - each one INDEPENDENTLY, never voted across.
#
# It can never accept, reject, relabel or change a diagnosis. A mapped ICD-10-CM target is
# never substituted for the SNOMED code in a report row nor added to the paste block: that
# block feeds an outside ICD-10 lookup, and a SNOMED code converted behind the veteran's back
# would be a confident wrong answer somewhere else.
#
# Like the other two references, the whole feature fails open. A missing, unreadable, malformed
# or wrong-schema file costs the SNOMED block and nothing else.
#
# The artifact is GZIP, and is read straight out of gzip on every load. Uncompressed it is
# roughly 48 MB, which is not a file to leave expanded beside a program people copy around;
# compressed it is under 5 MB and decompresses in well under a second.
SNOMED_REF_NAME = "snomed_icd10cm_reference.json.gz"
SNOMED_MANIFEST_NAME = "snomed_icd10cm_reference.manifest.json"
# /2 added the concept-type index. /1 files are REFUSED rather than read without types: a /1
# artifact would answer every type lookup with "unavailable", and fail-closed eligibility would
# then quietly drop the SNOMED lane's diagnostic answers with no explanation anyone could read.
SNOMED_REF_SCHEMA = "snomed-icd10cm-map/2"

# The US `ICD-10-CM complex map reference set`, and the international map that must NOT be
# read in its place. 447562003 targets ICD-10, not ICD-10-CM, and this lane ends at an
# ICD-10-CM lookup. Both are fixed HERE and checked against the artifact's declared policy, so
# a file built from the wrong reference set is refused rather than believed.
SNOMED_REFSET_ID = "6011000124106"
SNOMED_INTL_REFSET_ID = "447562003"

# The order of the fields inside one stored map row. The artifact repeats this list in its
# policy block and the loader requires the two to agree, so the positional rows can be read by
# a non-Python consumer without guessing what column 4 means.
SNOMED_ROW_FIELDS = ("mapGroup", "mapPriority", "mapRule", "mapAdvice", "mapTarget",
                     "correlationId", "mapCategoryId")

# The only outcomes this schema defines for a SNOMED code. Fixed HERE, not read from the
# artifact, for the same reason as the DC floor: a file must not be able to name an outcome the
# reader has no rule for and have it printed anyway. `absent` is not stored in the artifact - it
# is what a code that has no row at all resolves to - so the build receipt covers the other four.
SNOMED_STATES = ("absent", "conditional", "direct", "multi_group", "no_classification")
SNOMED_STORED_STATES = ("conditional", "direct", "multi_group", "no_classification")

# "MAP SOURCE CONCEPT CANNOT BE CLASSIFIED WITH AVAILABLE DATA", the map's own category for a
# row it declines to classify. Recorded for provenance; the blank target is what decides.
SNOMED_CAT_NO_CLASSIFICATION = "447638001"

# A SNOMED CT identifier: 6 to 18 digits. Used to validate the artifact's keys and to reject a
# "map" row keyed on something that is not a concept identifier at all.
SNOMED_KEY_RE = re.compile(r'^[0-9]{6,18}$')

# An ICD-10-CM target AS THE LICENSED MAP WRITES IT: dotted, and possibly carrying a trailing
# `?`. Deliberately NOT CODE_KEY_RE, which excludes the letter U so extraction never lifts a
# U-code out of free text; the map legitimately targets U07.0, U07.1 and U09.9.
SNOMED_TARGET_RE = re.compile(r'^[A-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?\??$')

# THE TRAILING `?` IS A PLACEHOLDER, NOT PART OF A CODE, and this is the single most important
# rule in this block. 55,317 active rows of the March 2026 US Edition carry a target such as
# `T43.595?`, where the `?` stands for a 7th character the map cannot supply (initial encounter,
# subsequent encounter, sequela). Every one of those rows also carries the advice EPISODE OF
# CARE INFORMATION NEEDED - a 1:1 correspondence measured on the real release, not assumed.
#
# So a `?` target is a map that stopped short of an answer because it needs patient context,
# which is exactly what "another rule requiring patient context" means: the concept is
# CONDITIONAL and is never resolved. The `?` is also never stripped. Stripping it would turn an
# admittedly incomplete code into one that looks finished, and `dc_for_code` would then happily
# normalise `T43.595?` to `T43595` and print a confident diagnostic code for a mapping the
# source explicitly declined to complete.
SNOMED_PLACEHOLDER = "?"

# ---- a SNOMED code is not automatically a diagnosis ------------------------
# SNOMED CT codes disorders, and it also codes findings, events, situations, procedures,
# substances, organisms and specimens. `840546002` is `Exposure to severe acute respiratory
# syndrome coronavirus 2 (event)`. It maps directly and unconditionally to Z20.822, and it is
# still not a diagnosis of COVID-19: a DIRECT map says the terminology translation needed no
# patient context, never that the record confirmed a disease. Offering that event a potential
# VA diagnostic code would put an exposure on a claim as if it were the illness.
#
# So the artifact carries a second index, concept id to the semantic tag of the concept's
# active fully specified name, taken from the licensed release itself. The record's own
# informal wording is never used to decide a type, and neither is the ICD-10-CM target.
#
# `900000000000003001` is `Fully specified name`. The Description file also carries synonyms,
# which have no semantic tag at all.
SNOMED_FSN_TYPE_ID = "900000000000003001"

# THE ONLY TAG THAT OPENS THE DIAGNOSTIC LANE, and the reason nothing else has to be perfect.
# Eligibility is an exact match on this one word, so an unreadable, misparsed or unheard-of tag
# can never promote a concept to a diagnosis - it can only leave it reported as terminology,
# which is the honest answer anyway.
SNOMED_DIAGNOSIS_TAG = "disorder"

# A semantic tag's shape, checked so a "tag" carrying a tab, a newline or a whole paragraph is
# refused. One line of printable characters, 1 to 120 of them, no parenthesis. The longest in
# this release is 93 characters, and it is a legacy fragment rather than a type. Deliberately
# permissive about content: this release alone uses `regime/therapy`, `religion/philosophy`,
# `SNOMED RT+CTV3`, `record artifact` and 150-odd others, plus legacy fragments like `& [hall]`
# that sit in tag position, and a reader that decided which tags SNOMED is allowed to invent
# would break on the next release rather than on a real defect. What protects the report is not
# this pattern but SNOMED_DIAGNOSIS_TAG: nothing this lets through can spell `disorder`.
# The parenthesis ban is the one content rule, and it is structural - see semantic_tag.
SNOMED_TAG_RE = re.compile(r'^(?![ -~]*[()])[!-~][ -~]{0,119}$')

# Metadata the artifact must carry before any of it is believed.
SNOMED_REQUIRED_META = {
    "source": ("name", "entry", "entry_sha256", "entry_bytes", "release_date",
               "description_entry", "description_entry_sha256", "description_entry_bytes",
               "description_release_date"),
    "policy": ("refset_id", "excluded_refset_id", "row_fields", "states", "active_only",
               "description_type_id", "diagnosis_tag"),
    "counts": ("source_rows", "source_concepts", "distinct_targets", "blank_target_rows",
               "placeholder_target_rows", "typed_concepts", "untyped_concepts"),
}

# Column widths for the SNOMED block. Each is sized to the widest thing that can land in it -
# the basis column to "no ICD-10-CM classification from this map", the target column to its own
# heading - because a column narrower than its contents does not truncate here, it SHIFTS every
# column to its right, and a diagnostic code sitting under the wrong heading is a wrong answer
# that no amount of careful wording upstream can undo.
SNOMED_CODE_WIDTH = 18
SNOMED_TARGET_WIDTH = 19
SNOMED_BASIS_WIDTH = 41
SNOMED_TYPE_WIDTH = 16

# The user-facing name for each concept type, and the field label the Claim File Handoff puts
# in front of the description. Only `disorder` is called a diagnosis. Anything else is named
# for what it is, because a procedure filed as a diagnosis is a wrong claim, not a wording
# quibble. A tag with no entry here is shown as "clinical concept: <tag>" - unrecognised is
# not the same as unavailable, and the licensed release's own word for it is worth printing.
SNOMED_TYPE_NAME = {
    "disorder": "diagnosis",
    "finding": "finding",
    "procedure": "procedure",
    "event": "event",
    "situation": "situation",
}
SNOMED_TYPE_UNAVAILABLE = "clinical entry, type unavailable"
SNOMED_HANDOFF_TYPE_LABEL = {
    "disorder": "Diagnosis",
    "finding": "Finding",
    "procedure": "Procedure",
    "event": "Event",
    "situation": "Situation",
}
SNOMED_HANDOFF_TYPE_DEFAULT = "Clinical entry"

# The wording for each state, in the report. These are the labels the map rules define, and
# they are deliberately long enough to be unambiguous: "conditional map, review required" says
# what a reader has to do, where a bare "conditional" does not.
SNOMED_BASIS = {
    "direct": "direct, unconditional",
    "multi_group": "multiple unconditional map groups",
    "conditional": "conditional map, review required",
    "no_classification": "no ICD-10-CM classification from this map",
    "absent": "not in this SNOMED CT to ICD-10-CM map",
    "unavailable": "map data unavailable",
}

# The shorter vocabulary the Claim File Handoff's "Map basis" field uses.
SNOMED_HANDOFF_BASIS = {
    "direct": "direct",
    "multi_group": "multiple groups",
    "conditional": "conditional",
    "no_classification": "no classification",
    "absent": "not in this map",
    "unavailable": "unavailable",
}

# ---- date shapes (approximate binding) -------------------------------------
DATE_RES = [
    re.compile(r'\b(\d{1,2}/\d{1,2}/\d{2,4})\b'),
    re.compile(r'\b(\d{4}-\d{2}-\d{2})\b'),
    re.compile(r'\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b', re.I),
]

# A date-of-birth LABEL sitting immediately before a date. The label, not the date's age,
# is what disqualifies it: a 1972 date is a perfectly good visit date on a Vietnam-era record,
# and nothing here may reject a date merely for being old.
#
# [o0] because OCR routinely reads the O of DOB as a zero: the real records this was built
# against print "D0B: 03/19/1969" and "DO.B.:" in their banners. Anchored to the END of the
# window before the date so "Dobson 3/19/2020" (a name, not a label) cannot match.
DOB_LABEL_RE = re.compile(
    r'(?:\bd\.?\s?[o0]\.?\s?b\.?|\bdate\s+of\s+birth|\bbirth\s*date|\bborn)'
    r'\s*[:;.,-]?\s*$', re.I)

# How much of a block counts as its header/banner when classifying dates: the first
# HEADER_LINES lines, but never more than half the block, so a short page is not all header.
# Real pages put the patient banner (name, DOB, date of service) in the electronic text layer
# plus the OCR of the printed letterhead, which together land inside the first ten lines.
HEADER_LINES = 10

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _date_value(s):
    """(year, month, day) for any DATE_RES shape, or None. Used to recognise ONE date written
    two ways ("3/19/1969", "03/19/69"), never to judge or reject the date itself."""
    s = s.strip()
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y < 50 else 1900
        return (y, mo, d)
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r'^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$', s)
    if m and m.group(1)[:3].lower() in MONTHS:
        return (int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    return None


def date_candidates(text):
    """The block's dates split into (body, header) candidate lists, DOB removed from both.

    Two rules, and only these two:

    DATE OF BIRTH is never a diagnosis date. A date is dropped when a DOB label sits right
    before it, and any other date on the block carrying the SAME value is dropped too - the
    banner writes the birth date three times and labels it once, and an unlabeled copy of a
    labeled birth date is still the birth date.

    A REPEATED BANNER DATE yields to a body date. A value that appears more than once on the
    block with at least one copy in the header zone is the letterhead's date, printed by the
    template rather than beside any diagnosis. Those are kept - on a page whose only date is
    the encounter date in its banner, that date is the right answer - but a body date in the
    same at-or-before position wins over it. The at-or-before rule itself is UNCHANGED and
    still comes first: a banner date above the diagnosis still beats a body date below it,
    because the one date most pages carry BELOW every diagnosis is the fax or print stamp in
    the footer, and preferring that over the encounter date would replace a right answer with
    a wrong one. Measured on the real records this was built against, exactly that swap is
    what an unconditional prefer-the-body rule produced. No date is ever invented.
    """
    found = all_dates(text)
    if not found:
        return [], []

    dob_values = set()
    kept = []
    for pos, ds in found:
        if DOB_LABEL_RE.search(text[max(0, pos - 24):pos]):
            val = _date_value(ds)
            if val:
                dob_values.add(val)
            continue
        kept.append((pos, ds))
    if dob_values:
        kept = [(pos, ds) for pos, ds in kept if _date_value(ds) not in dob_values]
    if not kept:
        return [], []

    lines = text.splitlines()
    zone_lines = min(HEADER_LINES, max(1, len(lines) // 2))
    zone_end = sum(len(ln) + 1 for ln in lines[:zone_lines])

    counts, in_zone = {}, set()
    for pos, ds in kept:
        val = _date_value(ds) or ds
        counts[val] = counts.get(val, 0) + 1
        if pos < zone_end:
            in_zone.add(val)
    banner = {v for v, n in counts.items() if n >= 2 and v in in_zone}

    body = [(pos, ds) for pos, ds in kept if (_date_value(ds) or ds) not in banner]
    header = [(pos, ds) for pos, ds in kept if (_date_value(ds) or ds) in banner]
    return body, header


def load_codes(base_dir):
    """code (dotless) -> official description."""
    path = os.path.join(base_dir, "icd10_data.tsv")
    codes = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            code, _, desc = line.rstrip("\n").partition("\t")
            if code:
                codes[code] = desc
    return codes


def _dc_names_ok(names):
    """Every DC number maps to a non-empty name."""
    for dc, name in names.items():
        if not isinstance(dc, str) or not dc.isdigit():
            return False
        if not isinstance(name, str) or not name.strip():
            return False
    return True


def _direct_ok(rows, names):
    """Each direct row is exactly [diagnostic code, confidence], both real strings."""
    for code, row in rows.items():
        if not isinstance(code, str) or not CODE_KEY_RE.match(code):
            return False
        if not isinstance(row, list) or len(row) != 2:
            return False
        dc, conf = row
        if not isinstance(dc, str) or dc not in names:
            return False
        # Only the labels this schema defines. A non-empty string was not enough: an invented
        # label like "official" would have printed as `direct (official)`, claiming an authority
        # this reference does not have and must never appear to have.
        if conf not in DC_CONFIDENCE_VALUES:
            return False
    return True


def _rollup_ok(rows, names, must_meet_floor):
    """Each rollup row is exactly [diagnostic code, votes, total] and the vote is possible.

    THE KEY MUST BE A THREE-CHARACTER CATEGORY. A rollup is the answer to "its children agree",
    which is only a question a category can be asked. Accepting a detailed key here was a real
    defect: a row like `"M2041": ["7816", 11, 12]` is well formed in every other respect, so it
    validated, and then a specific code printed a borrowed category vote and an unrelated
    diagnostic code as though the reference had answered for it. Hammer toe would have read as
    Psoriasis, with a percentage next to it.

    THE SECTION A ROW SITS IN IS A CLAIM ABOUT ITS VOTE, and the vote has to back it up.
    `derived` means "these children agree well enough to answer", `ambiguous` means "they do
    not". Checking only that a vote was arithmetically possible let the two swap: a 4-of-9 row
    filed under `derived` printed `category, 4 of 9 agree (44%)` beside a diagnostic code, which
    hands over an answer the data explicitly does not support.

    `bool` is rejected explicitly because in Python it is a subclass of int, so True would
    otherwise sail through as a vote count of 1.
    """
    for code, row in rows.items():
        if not isinstance(code, str) or not CODE_KEY_RE.match(code):
            return False
        if len(code) != DC_CATEGORY_LEN:
            return False
        if not isinstance(row, list) or len(row) != 3:
            return False
        dc, votes, total = row
        if not isinstance(dc, str) or dc not in names:
            return False
        if isinstance(votes, bool) or isinstance(total, bool):
            return False
        if not isinstance(votes, int) or not isinstance(total, int):
            return False
        if votes < 1 or total < 1 or votes > total:
            return False
        # Exact integer form of "votes/total >= floor%", so a row sitting precisely on the bar
        # cannot be classified one way by the builder and the other way here.
        meets = votes * 100 >= DC_FLOOR_PERCENT * total
        if meets != must_meet_floor:
            return False
    return True


def _manifest_agrees(base_dir, digest):
    """True when the detached manifest vouches for exactly these bytes.

    The manifest is REQUIRED, not optional. Its whole job is to be the thing that can say the
    artifact is intact, and a missing witness is not the same as a passed check: half a written
    file can still parse as JSON and name the right schema, and a wrong mapping printed beside
    a veteran's diagnosis is worse than no mapping at all.
    """
    try:
        with open(os.path.join(base_dir, DC_MANIFEST_NAME), encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(man, dict) or man.get("schema") != DC_REF_SCHEMA:
        return False
    art = man.get("artifact")
    if not isinstance(art, dict):
        return False
    return art.get("name") == DC_REF_NAME and art.get("sha256") == digest


def load_dc_reference(base_dir):
    """The optional DC cross-reference from beside the program, or None.

    NEVER RAISES, and never returns half-trusted data. Anything wrong answers None, which the
    report renders as "unavailable": missing file, unreadable, not JSON, wrong schema, absent
    or malformed metadata, a corrupted row in any section, a count that disagrees with the
    section it counts, a missing manifest, or bytes the manifest does not vouch for.

    The whole structure is checked before any of it is used. Validating only the wrapper was a
    real defect: a row like `{"L400": "oops"}` passed, and then indexing that string produced a
    confident, entirely fictional diagnostic code in the report. A reference problem must cost
    a veteran the reference and nothing else, and it must never cost them a wrong answer.
    """
    path = os.path.join(base_dir, DC_REF_NAME)
    try:
        with open(path, "rb") as f:
            raw = f.read()
        digest = hashlib.sha256(raw).hexdigest()
        ref = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None

    if not isinstance(ref, dict) or ref.get("schema") != DC_REF_SCHEMA:
        return None
    if not isinstance(ref.get("generated"), str) or not ref["generated"].strip():
        return None
    for block, fields in DC_REQUIRED_META.items():
        meta = ref.get(block)
        if not isinstance(meta, dict) or any(meta.get(f) in (None, "") for f in fields):
            return None
    # The file must agree with the rollup depth and the agreement bar this reader enforces. A
    # file declaring some other policy is not one this schema version knows how to read, and
    # trusting its numbers would let it move the line that judges its own contents.
    if ref["policy"].get("category_length") != DC_CATEGORY_LEN:
        return None
    if ref["policy"].get("floor_percent") != DC_FLOOR_PERCENT:
        return None
    for section in ("direct", "derived", "ambiguous", "dc_names"):
        if not isinstance(ref.get(section), dict):
            return None

    names = ref["dc_names"]
    if not _dc_names_ok(names):
        return None
    if not _direct_ok(ref["direct"], names):
        return None
    if not _rollup_ok(ref["derived"], names, must_meet_floor=True):
        return None
    if not _rollup_ok(ref["ambiguous"], names, must_meet_floor=False):
        return None

    # A count that disagrees with what it counts means the file was cut short or edited, even
    # when every surviving row is individually well formed.
    counts = ref["counts"]
    if counts.get("direct_usable") != len(ref["direct"]) or \
            counts.get("derived_categories") != len(ref["derived"]) or \
            counts.get("ambiguous_categories") != len(ref["ambiguous"]) or \
            counts.get("distinct_dcs") != len(names):
        return None
    # A direct row always beats a rollup, so the two must never both answer for one code.
    if set(ref["direct"]) & (set(ref["derived"]) | set(ref["ambiguous"])):
        return None

    if not _manifest_agrees(base_dir, digest):
        return None
    return ref


def dc_for_code(ref, code):
    """Look one extracted ICD-10 code up in the reference.

    Returns (state, dc, name, basis). State is one of:
      mapped      the reference answers for this exact code, or for its category
      ambiguous   the category's children do not agree enough to answer
      unmapped    the reference is fine and simply has no row for this code
      unavailable no usable reference file
    A specific code is NEVER replaced by its category: the rollup is consulted only when the
    code itself has no direct row, which for a 3-character category IS the code itself.
    """
    if ref is None:
        return "unavailable", "", "", ""
    dotless = re.sub(r'[^0-9A-Za-z]', '', code).upper()
    row = ref["direct"].get(dotless)
    if row:
        dc, conf = row[0], row[1]
        return "mapped", dc, ref["dc_names"].get(dc, ""), f"direct ({conf})"
    row = ref["derived"].get(dotless)
    if row:
        dc, votes, total = row[0], row[1], row[2]
        # Owner rule, 2026-08-23: a category answer is labeled for what it IS - a match at the
        # category level - without vote arithmetic in the reader's face. Unanimous says just
        # that; a majority carries its percentage; the numerator, denominator and audit trail
        # all stay in the artifact, which this wording change does not touch. Never relabeled
        # as direct: 100% agreement among children is still a category-level answer.
        if votes == total:
            basis = "Category match only"
        else:
            basis = f"Category match only - {round(100.0 * votes / total)}% confidence"
        return "mapped", dc, ref["dc_names"].get(dc, ""), basis
    if dotless in ref["ambiguous"]:
        # Below the floor the children disagree too much to answer. The vote lives in the
        # artifact for audit; the reader sees a refusal, not arithmetic to second-guess.
        return "ambiguous", "", "", "No reliable category match"
    return "unmapped", "", "", ""


def _gem_bridge_ok(bridge):
    """Every row of the bridge section: an ICD-9 key mapping to distinct ICD-10 targets.

    Checked in full before any of it is used. A row like `{"25000": "E119"}` would otherwise
    iterate as the characters of a string and vote five times for nothing; a row like
    `{"25000": ["M2041"]}` would print Hammer toe beside a diabetes diagnosis. The first is a
    crash, the second is a wrong answer, and only the second is worth being careful about.
    """
    for code, targets in bridge.items():
        if not isinstance(code, str) or not GEM_KEY_RE.match(code):
            return False
        # A str is iterable and a bool is an int; neither is a list of targets.
        if isinstance(targets, (str, bytes)) or not isinstance(targets, list) or not targets:
            return False
        seen = set()
        for t in targets:
            if not isinstance(t, str) or not CODE_KEY_RE.match(t) or t in seen:
                return False
            seen.add(t)
    return True


def _gem_manifest_agrees(base_dir, digest):
    """The detached manifest must vouch for exactly this filename and these bytes."""
    try:
        with open(os.path.join(base_dir, GEM_MANIFEST_NAME), "rb") as f:
            man = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(man, dict):
        return False
    art = man.get("artifact")
    if not isinstance(art, dict):
        return False
    return art.get("name") == GEM_REF_NAME and art.get("sha256") == digest


def load_gem_reference(base_dir):
    """The optional labeled-ICD-9 bridge from beside the program, or None.

    NEVER RAISES, and never returns half-trusted data. Anything wrong answers None, which the
    report renders as "unavailable": missing file, unreadable, not JSON, wrong schema, absent
    or malformed metadata, a corrupted row, a count that disagrees with what it counts, a
    disposition vocabulary this reader has no rule for, a missing manifest, or bytes the
    manifest does not vouch for.

    The artifact's `dc_audit` block is a BUILD RECEIPT and is validated for shape but never
    used as an answer. Every diagnostic code printed for a bridged ICD-9 row is looked up live
    in the DC reference installed beside the program, so a rebuilt DC reference changes the
    answers without this file having to be rebuilt, and a DC reference that is missing or
    refused cannot be papered over by a stale summary recorded here.
    """
    path = os.path.join(base_dir, GEM_REF_NAME)
    try:
        with open(path, "rb") as f:
            raw = f.read()
        digest = hashlib.sha256(raw).hexdigest()
        gem = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None

    if not isinstance(gem, dict) or gem.get("schema") != GEM_REF_SCHEMA:
        return None
    if not isinstance(gem.get("generated"), str) or not gem["generated"].strip():
        return None
    for block, fields in GEM_REQUIRED_META.items():
        meta = gem.get(block)
        if not isinstance(meta, dict) or any(meta.get(f) in (None, "") for f in fields):
            return None
    # The file must agree with the agreement bar and the outcome vocabulary this reader
    # enforces. A file declaring some other policy is not one this schema version knows how to
    # read, and trusting its numbers would let it move the line that judges its own contents.
    if gem["policy"].get("floor_percent") != DC_FLOOR_PERCENT:
        return None
    if gem["policy"].get("dispositions") != list(GEM_DISPOSITIONS):
        return None
    if not isinstance(gem.get("bridge"), dict) or not gem["bridge"]:
        return None
    if not isinstance(gem.get("ledger"), dict):
        return None
    if not _gem_bridge_ok(gem["bridge"]):
        return None

    # A count that disagrees with what it counts means the file was cut short or edited, even
    # when every surviving row is individually well formed. source_rows must equal bridge_rows
    # because nothing is quarantined: every source row bridges, so a shortfall is a loss.
    bridge = gem["bridge"]
    counts = gem["counts"]
    if counts.get("bridge_rows") != len(bridge) or counts.get("source_rows") != len(bridge):
        return None
    if counts.get("multi_target_rows") != sum(1 for t in bridge.values() if len(t) > 1):
        return None
    if counts.get("target_slots") != sum(len(t) for t in bridge.values()):
        return None
    if counts.get("distinct_targets") != len({t for ts in bridge.values() for t in ts}):
        return None
    if gem["source"].get("rows") != len(bridge):
        return None

    # The build receipt: shape only, and it must account for every bridged row under exactly
    # the outcomes this reader knows. A receipt naming an outcome with no rule behind it means
    # the file was built by something this reader does not understand.
    audit = gem.get("dc_audit")
    if not isinstance(audit, dict) or not isinstance(audit.get("reference"), dict):
        return None
    ref_meta = audit["reference"]
    if any(ref_meta.get(f) in (None, "") for f in ("name", "sha256")):
        return None
    tally = audit.get("counts")
    if not isinstance(tally, dict) or sorted(tally) != sorted(GEM_DISPOSITIONS):
        return None
    for value in tally.values():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
    if sum(tally.values()) != len(bridge):
        return None

    if not _gem_manifest_agrees(base_dir, digest):
        return None
    return gem


def gem_bridge(gem, ref, code):
    """Bridge one code the record LABELS ICD-9-CM to a potential diagnostic code.

    Returns (state, targets, dc, name, basis). `targets` is every ICD-10-CM target the GEM
    gives, dotted for display and never collapsed, so the reader can see what was crossed
    rather than only what it produced. State is one of:
      mapped       the targets that map agree on a DC at or above the floor
      ambiguous    the targets that map do not agree enough to answer
      unmapped     the GEM is fine; this code is absent from it, or bridges only to targets
                   the DC reference has no row for
      unavailable  no usable bridge or DC reference

    The 60% floor is applied as exact integer arithmetic over the MAPPED targets only. A
    target the DC reference does not map casts no vote, so a code bridging to one mapped and
    nine unmapped targets answers 1 of 1 rather than 1 of 10: the nine are silent, not
    dissenting, and counting silence as dissent would refuse an answer that is not in doubt.
    """
    if gem is None or ref is None:
        return "unavailable", [], "", "", "bridge reference unavailable"
    dotless = re.sub(r'[^0-9A-Za-z]', '', code).upper()
    targets = gem["bridge"].get(dotless)
    if not targets:
        return "unmapped", [], "", "", "not in the ICD-9 to ICD-10-CM GEM"

    shown = [dotted(t[:3], t[3:]) for t in targets]
    tally, order = {}, []
    for target in targets:
        state, dc, _name, _basis = dc_for_code(ref, target)
        if state != "mapped":
            continue
        if dc not in tally:
            order.append(dc)
        tally[dc] = tally.get(dc, 0) + 1
    if not tally:
        return "unmapped", shown, "", "", "no diagnostic code for the bridge target(s)"

    total = sum(tally.values())
    # Ties break to the lower DC number, matching the builder, so the two never disagree.
    dc, votes = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    pct = round(100.0 * votes / total)
    if votes * 100 >= DC_FLOOR_PERCENT * total:
        return "mapped", shown, dc, ref["dc_names"].get(dc, ""), \
            f"bridged, {votes} of {total} agree ({pct}%)"
    return "ambiguous", shown, "", "", \
        f"bridge split, top {dc} with {votes} of {total} ({pct}%)"


def _gem_targets_text(targets, width=GEM_TARGETS_WIDTH):
    """The bridge targets for one row: as many whole ones as fit, then how many were left out.

    Never truncates a code itself - a half-printed ICD-10 code is worse than an honest count of
    the ones not shown. A single target wider than the column is printed anyway, because a row
    that lists nothing at all says less than a row that runs slightly long.
    """
    if not targets:
        return "-"
    shown = []
    for target in targets:
        left = len(targets) - len(shown) - 1
        trial = ", ".join(shown + [target]) + (f" +{left} more" if left else "")
        if shown and len(trial) > width:
            break
        shown.append(target)
    left = len(targets) - len(shown)
    return ", ".join(shown) + (f" +{left} more" if left else "")


def semantic_tag(term):
    """The semantic tag of a SNOMED CT fully specified name, or "" when it has none.

    The tag is the trailing parenthesis AT DEPTH ONE, found by walking the term backwards and
    counting brackets, and A REAL SEMANTIC TAG NEVER CONTAINS A PARENTHESIS OF ITS OWN. Both
    halves are needed because the release still carries legacy terms like

        Osteotomy first metatarsal base (& for hallux valgus (& Golden))

    whose trailing group closes inside another. Counting brackets finds that whole group
    honestly; the no-nested-parenthesis rule then rejects it, because a group containing
    brackets is a legacy fragment sitting in tag position, not a type. Three active concepts in
    the March 2026 US Edition are shaped that way and all three come back empty.

    THE LEGACY JUNK IS NOT ALL REACHABLE THIS WAY. Terms such as `... (flat foot NOS)` put a
    bracket-free fragment in tag position and no structural rule can tell it from a tag SNOMED
    might legitimately add next year. That is survivable, and it is why eligibility is an exact
    match on SNOMED_DIAGNOSIS_TAG rather than a list of types to exclude: junk cannot spell
    `disorder` by accident, so the worst it can do is have a concept described oddly, never
    have a procedure offered a diagnostic code. Measured on this release, every one of the
    139,598 mapped concepts carries a clean tag and none of the junk is in the map at all.

    Empty is safe by construction, for the same reason: a tag that cannot be read leaves a
    concept reported as a clinical entry and never as a diagnosis.
    """
    t = str(term).rstrip()
    if not t.endswith(")"):
        return ""
    depth = 0
    for i in range(len(t) - 1, -1, -1):
        if t[i] == ")":
            depth += 1
        elif t[i] == "(":
            depth -= 1
            if depth == 0:
                tag = t[i + 1:-1].strip()
                return "" if ("(" in tag or ")" in tag) else tag
    return ""


def snomed_type(snomed, code):
    """The licensed semantic tag for one SNOMED CT concept, or "" when it is not known.

    "" covers every reason at once - no map installed, the concept is not in the release, the
    release types it with a term that has no tag - because all of them lead to the same place:
    the concept is reported as a clinical entry of unavailable type and is never treated as a
    diagnosis. Distinguishing them would let a caller think one of them was safer than another.
    """
    if snomed is None:
        return ""
    key = str(code).strip()
    if not SNOMED_KEY_RE.match(key):
        return ""
    tag = snomed["types"].get(key, "")
    return tag if isinstance(tag, str) else ""


def snomed_is_diagnosis(snomed, code):
    """True only when the licensed release types this concept as a disorder.

    FAILS CLOSED. Anything else - a finding, a procedure, an event, a situation, a tag this
    program has never heard of, a concept the release does not type, a missing map - answers
    False. An unknown type is never a diagnosis by default, because the cost of the two
    mistakes is not symmetric: refusing a real disorder costs a research lookup, while
    accepting a procedure puts it on a claim as if a doctor had diagnosed it.
    """
    return snomed_type(snomed, code) == SNOMED_DIAGNOSIS_TAG


def snomed_type_name(snomed, code):
    """What to call this concept in front of a veteran."""
    tag = snomed_type(snomed, code)
    if not tag:
        return SNOMED_TYPE_UNAVAILABLE
    return SNOMED_TYPE_NAME.get(tag, f"clinical concept: {tag}")


def snomed_row_tag(snomed, code):
    """The bracketed system tag for a SNOMED row in the report's diagnosis table.

    `[SNOMED CT procedure]` when the release names a type, plain `[SNOMED CT]` when it does
    not. The plain form is deliberate: with no map installed there is nothing to claim, and
    inventing a word for the gap would be worse than leaving the row exactly as it reads today.
    """
    tag = snomed_type(snomed, code)
    return f"SNOMED CT {tag}" if tag else "SNOMED CT"


def snomed_state(rows):
    """Which of the four stored states one concept's map rows are in.

    THE MAP IS NOT A FLAT TABLE, and this is the whole reason the ICD-9 bridge's 60% vote is
    not reused here. A concept carries map GROUPS, all of which apply together, and PRIORITIES
    inside a group, of which exactly one applies - chosen by a rule that usually needs patient
    context. Voting across those would average together codes that are meant to be used
    together, and would silently pick one arm of a choice the map deliberately left open.

      direct             one group, one row, rule TRUE, a resolved target
      multi_group        several groups, each one unconditional row; ALL targets apply
      conditional        a choice or a context the map cannot make on its own
      no_classification  the map says this concept cannot be classified with available data

    Anything conditional is REPORTED, never resolved. Four things make a concept conditional
    and the last one is the one that is easy to miss:
      - more than one row in a group (competing priorities);
      - a rule that is not plain TRUE (IFA ..., OTHERWISE TRUE);
      - a target carrying the `?` 7th-character placeholder - see SNOMED_PLACEHOLDER, this is
        a map that stopped short because it needs the episode of care;
      - a blank target sitting beside a real one, so the concept is only partly classified.
        That combination does not occur in the March 2026 US Edition (every mixed-blank concept
        there is already conditional for one of the reasons above), but a future release is not
        obliged to keep it that way, and the honest answer to "half of this classified" is that
        a human has to look.
    """
    groups = {}
    for row in rows:
        groups.setdefault(row[0], []).append(row)
    if any(len(g) > 1 for g in groups.values()):
        return "conditional"
    if any(row[2] != "TRUE" for row in rows):
        return "conditional"
    if any(row[4].endswith(SNOMED_PLACEHOLDER) for row in rows):
        return "conditional"
    if all(not row[4] for row in rows):
        return "no_classification"
    if any(not row[4] for row in rows):
        return "conditional"
    return "direct" if len(groups) == 1 else "multi_group"


def _snomed_advice_parts(advice):
    """The advice segments split out, as the map writes them.

    mapAdvice is a pipe-separated list whose first segment restates the rule ("ALWAYS R07.9",
    "IF ... CHOOSE P36.9") and whose remaining segments are the official flags a coder is meant
    to act on ("EPISODE OF CARE INFORMATION NEEDED"). Returns (rule_segment, [flags]).
    """
    parts = [p.strip() for p in str(advice).split("|")]
    parts = [p for p in parts if p]
    if not parts:
        return "", []
    head = parts[0]
    if head.startswith("ALWAYS ") or head.startswith("IF "):
        return head, parts[1:]
    return "", parts


def _snomed_condition(rule, advice):
    """When one row of a conditional concept applies, in the map's own words.

    "IF AGE AT ONSET OF CLINICAL FINDING BEFORE 29.0 DAYS CHOOSE P36.9" is the map telling a
    coder which arm to take, so the trailing CHOOSE is dropped (the target is already printed
    in its own column) and the condition itself is kept verbatim. Nothing is paraphrased: a
    reworded clinical condition is a new clinical claim.
    """
    if rule == "OTHERWISE TRUE":
        return "otherwise"
    head, _flags = _snomed_advice_parts(advice)
    if head.startswith("IF "):
        return re.sub(r'\s*CHOOSE\s+\S+\s*$', '', head)
    return ""


def _snomed_manifest_agrees(base_dir, digest):
    """The detached manifest must vouch for exactly this filename and these bytes.

    The digest is over the COMPRESSED file, because the .json.gz is what sits on disk and what
    anything editing it would have to edit.
    """
    try:
        with open(os.path.join(base_dir, SNOMED_MANIFEST_NAME), "rb") as f:
            man = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(man, dict):
        return False
    art = man.get("artifact")
    if not isinstance(art, dict):
        return False
    return art.get("name") == SNOMED_REF_NAME and art.get("sha256") == digest


def _snomed_map_ok(rows_by_code):
    """Every row of the map section, checked in full before any of it is used.

    Same reasoning as the ICD-9 bridge's row check, with more to get wrong: a row here is a
    POSITIONAL list of seven fields, so a short row would index out of range and a row of
    strings where the group number belongs would sort in a way that quietly reorders
    priorities. A wrong order is not a crash, it is a different answer.
    """
    for code, rows in rows_by_code.items():
        if not isinstance(code, str) or not SNOMED_KEY_RE.match(code):
            return False
        if isinstance(rows, (str, bytes)) or not isinstance(rows, list) or not rows:
            return False
        slots = set()
        for row in rows:
            if isinstance(row, (str, bytes)) or not isinstance(row, list):
                return False
            if len(row) != len(SNOMED_ROW_FIELDS):
                return False
            group, priority, rule, advice, target, correlation, category = row
            # bool is a subclass of int, so True would otherwise pass as group 1.
            if isinstance(group, bool) or isinstance(priority, bool):
                return False
            if not isinstance(group, int) or not isinstance(priority, int):
                return False
            if group < 1 or priority < 1:
                return False
            for value in (rule, advice, target, correlation, category):
                if not isinstance(value, str):
                    return False
            if not rule:
                return False
            if target and not SNOMED_TARGET_RE.match(target):
                return False
            if not correlation.isdigit() or not category.isdigit():
                return False
            if (group, priority) in slots:
                return False
            slots.add((group, priority))
    return True


def _snomed_types_ok(types):
    """Every row of the concept-type index, checked in full before any of it is used.

    A key that is not a concept identifier would answer for the wrong concept; a value that is
    not a string would crash a comparison or, worse, compare unequal to `disorder` and silently
    demote a real diagnosis. An EMPTY tag is allowed on purpose - the release carries a handful
    of legacy terms with no semantic tag, and refusing the whole file over them would cost the
    types of half a million concepts to protect against six.
    """
    if not isinstance(types, dict) or not types:
        return False
    for code, tag in types.items():
        if not isinstance(code, str) or not SNOMED_KEY_RE.match(code):
            return False
        if not isinstance(tag, str):
            return False
        if tag and not SNOMED_TAG_RE.match(tag):
            return False
    return True


def load_snomed_reference(base_dir):
    """The optional SNOMED CT to ICD-10-CM map from beside the program, or None.

    NEVER RAISES, and never returns half-trusted data. Anything wrong answers None, which the
    report renders as "unavailable": missing file, unreadable, not gzip, not JSON, wrong
    schema, absent or malformed metadata, a corrupted row, a count that disagrees with what it
    counts, a state vocabulary this reader has no rule for, a policy naming the wrong reference
    set, a missing manifest, or bytes the manifest does not vouch for.

    THE REFSET CHECK IS NOT A FORMALITY. The same source file also carries 447562003, the
    international SNOMED CT to ICD-10 map, whose targets are ICD-10 rather than ICD-10-CM. An
    artifact built from that would be structurally perfect and clinically wrong at the end of a
    lane that finishes in an ICD-10-CM lookup, so the artifact has to say which reference set
    it came from and it has to be the US one.

    The `states` block is a BUILD RECEIPT and is validated for shape but never used as an
    answer: every concept is classified live by snomed_state() when it is looked up, so the
    receipt cannot make the program say something the rows do not support.
    """
    path = os.path.join(base_dir, SNOMED_REF_NAME)
    try:
        with open(path, "rb") as f:
            blob = f.read()
        digest = hashlib.sha256(blob).hexdigest()
        ref = json.loads(gzip.decompress(blob).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, EOFError, gzip.BadGzipFile):
        return None

    if not isinstance(ref, dict) or ref.get("schema") != SNOMED_REF_SCHEMA:
        return None
    if not isinstance(ref.get("generated"), str) or not ref["generated"].strip():
        return None
    for block, fields in SNOMED_REQUIRED_META.items():
        meta = ref.get(block)
        if not isinstance(meta, dict) or any(meta.get(f) in (None, "") for f in fields):
            return None
    policy = ref["policy"]
    if policy.get("refset_id") != SNOMED_REFSET_ID:
        return None
    if policy.get("excluded_refset_id") != SNOMED_INTL_REFSET_ID:
        return None
    if policy.get("active_only") is not True:
        return None
    # The artifact must lay its rows out in the order this reader unpacks them, and must name
    # the same outcome vocabulary. Both are what let a non-Python consumer read the same file.
    if policy.get("row_fields") != list(SNOMED_ROW_FIELDS):
        return None
    if policy.get("states") != list(SNOMED_STATES):
        return None
    # The artifact must agree about which description type carries the semantic tag, and about
    # which single tag opens the diagnostic lane. A file that declared `finding` eligible would
    # otherwise put every symptom on the claim side of the report.
    if policy.get("description_type_id") != SNOMED_FSN_TYPE_ID:
        return None
    if policy.get("diagnosis_tag") != SNOMED_DIAGNOSIS_TAG:
        return None
    if not isinstance(ref.get("map"), dict) or not ref["map"]:
        return None
    if not isinstance(ref.get("ledger"), dict):
        return None
    if not _snomed_map_ok(ref["map"]):
        return None
    if not _snomed_types_ok(ref.get("types")):
        return None

    # A count that disagrees with what it counts means the file was cut short or edited, even
    # when every surviving row is individually well formed.
    rows_by_code = ref["map"]
    all_rows = [row for rows in rows_by_code.values() for row in rows]
    counts = ref["counts"]
    if counts.get("source_concepts") != len(rows_by_code):
        return None
    if counts.get("source_rows") != len(all_rows):
        return None
    if counts.get("distinct_targets") != len({r[4] for r in all_rows if r[4]}):
        return None
    if counts.get("blank_target_rows") != sum(1 for r in all_rows if not r[4]):
        return None
    if counts.get("placeholder_target_rows") != sum(
            1 for r in all_rows if r[4].endswith(SNOMED_PLACEHOLDER)):
        return None

    # The build receipt: shape only, and it must account for every stored concept under exactly
    # the states this reader knows.
    tally = ref.get("states")
    if not isinstance(tally, dict) or sorted(tally) != sorted(SNOMED_STORED_STATES):
        return None
    for value in tally.values():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
    if sum(tally.values()) != len(rows_by_code):
        return None

    # The same discipline for the type index: a count that disagrees with what it counts means
    # the file was cut short or edited, even when every surviving row is well formed.
    types = ref["types"]
    typed = sum(1 for tag in types.values() if tag)
    if counts.get("typed_concepts") != typed:
        return None
    if counts.get("untyped_concepts") != len(types) - typed:
        return None
    type_tally = ref.get("type_counts")
    if not isinstance(type_tally, dict):
        return None
    recomputed = {}
    for tag in types.values():
        if tag:
            recomputed[tag] = recomputed.get(tag, 0) + 1
    if type_tally != recomputed:
        return None

    if not _snomed_manifest_agrees(base_dir, digest):
        return None
    return ref


def snomed_map(snomed, ref, code):
    """Cross one code the record LABELS SNOMED CT to its potential ICD-10-CM target(s).

    Returns (state, findings, advice). State is one of SNOMED_STATES, or "unavailable" when
    there is no usable MAP. A missing DC reference does NOT make the state unavailable: the
    target and the map basis still answer, and only the diagnostic code at the end of the lane
    reports itself unavailable through dc_for_code.

    `findings` is one dict per target the reader should see:
        target     the ICD-10-CM target exactly as the map writes it, `?` and all
        condition  when the map only takes this arm under a condition, that condition
        dc_state   mapped / ambiguous / unmapped, or "" when the target was never looked up
        dc, name, basis   the Potential-DC answer for THIS target alone

    EACH TARGET IS LOOKED UP INDEPENDENTLY AND THE RESULTS ARE NEVER COLLAPSED. Two map groups
    mean both ICD-10-CM codes apply together, so two different diagnostic codes is the correct
    answer, not a disagreement to be resolved by a vote. This is the one place where this lane
    deliberately behaves differently from the ICD-9 bridge, where several targets are competing
    renderings of ONE old code and a vote is the right shape.

    A conditional or unclassifiable concept is never resolved and never looked up at all.
    """
    # THE TWO REFERENCES ARE INDEPENDENT AND ONLY THE MAP DECIDES THIS STATE. `ref` is the
    # separate Potential-DC crosswalk, and it used to be checked here too, which meant a
    # missing dc_reference.json reported the SNOMED MAP as unavailable - hiding a perfectly
    # good terminology answer behind an unrelated file's absence, and telling the reader the
    # wrong thing about which of their reference files is missing. A missing `ref` now costs
    # exactly one thing: the diagnostic code at the END of the lane. The ICD-10-CM target and
    # the map basis are still reported, and dc_for_code answers "unavailable" for itself.
    if snomed is None:
        return "unavailable", [], []
    # A SNOMED CT concept identifier is ALL DIGITS, so the code is matched as written rather
    # than scrubbed into shape. Stripping non-digits the way the ICD-9 lane does would be
    # actively harmful here: the label pattern can capture a leading letter, and "SNOMED CT
    # A123456" would then be looked up as concept 123456 and answer for a completely different
    # concept than the one the record names. Anything that is not a concept identifier is
    # simply not in the map, which is the honest answer.
    key = str(code).strip()
    rows = snomed["map"].get(key) if SNOMED_KEY_RE.match(key) else None
    if not rows:
        return "absent", [], []

    state = snomed_state(rows)
    advice, seen_advice = [], set()
    for row in rows:
        for flag in _snomed_advice_parts(row[3])[1]:
            if flag not in seen_advice:
                seen_advice.add(flag)
                advice.append(flag)

    findings = []
    for group, priority, rule, row_advice, target, _corr, _cat in rows:
        if not target:
            continue
        item = {"target": target, "condition": "", "dc_state": "", "dc": "", "name": "",
                "basis": ""}
        if state in ("direct", "multi_group"):
            item["dc_state"], item["dc"], item["name"], item["basis"] = dc_for_code(ref, target)
        else:
            item["condition"] = _snomed_condition(rule, row_advice)
        findings.append(item)
    return state, findings, advice


def _snomed_lines(snomed_found, ref, snomed, snomed_missing_reason=""):
    """The SNOMED CT half of the cross-reference block, DISORDERS ONLY.

    The SNOMED code stays exactly as the record wrote it, the ICD-10-CM target the licensed map
    gives is printed beside it, and a diagnostic code is reached through that target only when
    the map resolved it without needing patient context. A reader who disagrees with the map
    can see the map.

    ONLY A CONCEPT THE LICENSED RELEASE TYPES AS A DISORDER GETS THIS FAR. Owner ruling,
    2026-08-24: a procedure is a procedure and an exposure is an exposure, and neither belongs
    in a section headed "Potential VA Diagnostic Code" no matter how cleanly it maps. Everything
    the record labelled SNOMED CT, of every type, is still reported in full in its own
    terminology section below - see snomed_terminology_section - so nothing is hidden; what
    changes is which of them is offered a diagnostic code.
    """
    out = ["",
           "   Codes the record labels SNOMED CT and the licensed US Edition types as a",
           "   DISORDER, crossed through the SNOMED CT to ICD-10-CM map and then looked up",
           "   above. That map is a terminology map, not a VA document, and the SNOMED code in",
           "   the rows above is unchanged. A target is looked up only where the map resolves",
           "   it without needing patient context; anything conditional is reported for review,",
           "   never answered. Findings, procedures, events and situations are NOT listed here;",
           "   they are reported in the SNOMED CT terminology section below."]
    if snomed is None:
        out.append("   Map data unavailable, so these codes could not be crossed. EVERY")
        out.append("   DIAGNOSIS ABOVE IS UNAFFECTED.")
        if snomed_missing_reason:
            out.append(f"   Reason:{snomed_missing_reason}")
        return out
    snomed_found = [c for c in snomed_found if snomed_is_diagnosis(snomed, c)]
    if not snomed_found:
        out.append("   No SNOMED CT code in this report is typed as a disorder by the licensed")
        out.append("   release, so none is eligible for a potential diagnostic code. See the")
        out.append("   SNOMED CT terminology section below for what each one actually is.")
        return out
    head = (f"   {'SNOMED CT':<{SNOMED_CODE_WIDTH}} "
            f"{'POTENTIAL ICD-10-CM':<{SNOMED_TARGET_WIDTH}} "
            f"{'MAP BASIS':<{SNOMED_BASIS_WIDTH}} DC / RESULT")
    out.append(head)
    # The rule is measured from the heading rather than fixed at 75 like the narrower blocks
    # above, so widening a column can never leave a rule that stops short of its own table.
    out.append("   " + "-" * (len(head) - 3))
    for code in snomed_found:
        state, findings, advice = snomed_map(snomed, ref, code)
        basis = SNOMED_BASIS[state]
        if not findings:
            result = {"no_classification": "(none - the map declines to classify)",
                      "absent": "(none)"}.get(state, "(none)")
            out.append(f"   {code:<{SNOMED_CODE_WIDTH}} {'-':<{SNOMED_TARGET_WIDTH}} "
                       f"{basis:<{SNOMED_BASIS_WIDTH}} {result}")
        for n, item in enumerate(findings):
            # The code and the basis are stated once; a continuation line carries only the
            # extra target, so two targets never read as two separate SNOMED codes.
            shown_code = code if n == 0 else ""
            shown_basis = basis if n == 0 else ""
            if item["dc_state"] == "mapped":
                result = f"{item['dc']:<6} {item['name']}"
            elif item["dc_state"] == "ambiguous":
                result = item["basis"]
            elif item["dc_state"] == "unmapped":
                result = "no mapping in this reference"
            elif item["dc_state"] == "unavailable":
                # The map answered; the SEPARATE Potential-DC reference is the missing one, and
                # saying so names the file the reader has to go and find.
                result = "reference data unavailable"
            else:
                result = "(not resolved)"
            out.append(f"   {shown_code:<{SNOMED_CODE_WIDTH}} "
                       f"{item['target']:<{SNOMED_TARGET_WIDTH}} "
                       f"{shown_basis:<{SNOMED_BASIS_WIDTH}} {result}")
        for item in findings:
            if item["condition"]:
                out.append(f"        {item['target']} applies when: {item['condition']}")
        for flag in advice:
            out.append(f"        Map advice: {flag}")
    return out


def snomed_terminology_section(snomed_found, snomed, snomed_missing_reason=""):
    """Every SNOMED CT code the record labelled, with what the licensed release says it IS.

    A SEPARATE SECTION FROM THE POTENTIAL-DC ONE, and separate on purpose. The terminology map
    is useful for a procedure, an event and a finding alike - knowing that
    `Exposure to severe acute respiratory syndrome coronavirus 2 (event)` translates to Z20.822
    is worth reporting - but that is a translation between coding systems, not a diagnosis, and
    a reader who sees it under a "Potential VA Diagnostic Code" heading will reasonably read it
    as one. So the translation is reported here, for every type, and the diagnostic lane above
    admits disorders only.

    Nothing here is ever added to the paste-ready ICD-10 block. The block feeds an outside
    ICD-10 lookup, and a procedure's ICD-10-CM translation pasted into it would come back as a
    confident diagnosis somewhere else.
    """
    out = ["", SNOMED_HEADING, "-" * 78]
    out.append("What the licensed SNOMED CT US Edition says each of these codes IS, and what it")
    out.append("translates to in ICD-10-CM. A TERMINOLOGY MAP, not a VA document and not a")
    out.append("diagnosis: a direct map means the translation needed no patient context, never")
    out.append("that the record confirmed a disease. Only a concept typed as a DISORDER is")
    out.append("eligible for the potential diagnostic codes above. Nothing here is pasted into")
    out.append("the ICD-10 list at the end.")
    out.append("")
    if snomed is None:
        out.append("Map data unavailable, so these codes could not be typed or crossed. EVERY")
        out.append("ENTRY ABOVE IS UNAFFECTED: this section is the only thing missing.")
        if snomed_missing_reason:
            out.append(f"Reason:{snomed_missing_reason}")
        for code in snomed_found:
            out.append(f"   {code}")
        return out
    head = (f"   {'SNOMED CT':<{SNOMED_CODE_WIDTH}} {'TYPE':<{SNOMED_TYPE_WIDTH}} "
            f"{'POTENTIAL ICD-10-CM':<{SNOMED_TARGET_WIDTH}} "
            f"{'MAP BASIS':<{SNOMED_BASIS_WIDTH}}")
    out.append(head.rstrip())
    # Measured from the PADDED header, not the trimmed one, so the rule still reaches the end
    # of the widest basis this table can print rather than stopping under the word MAP BASIS.
    out.append("   " + "-" * (len(head) - 3))
    for code in snomed_found:
        state, findings, advice = snomed_map(snomed, None, code)
        kind = snomed_type_name(snomed, code)
        basis = SNOMED_BASIS[state]
        targets = [item["target"] for item in findings] or ["-"]
        for n, target in enumerate(targets):
            out.append(f"   {(code if n == 0 else ''):<{SNOMED_CODE_WIDTH}} "
                       f"{(kind if n == 0 else ''):<{SNOMED_TYPE_WIDTH}} "
                       f"{target:<{SNOMED_TARGET_WIDTH}} {basis if n == 0 else ''}".rstrip())
        for item in findings:
            if item["condition"]:
                out.append(f"        {item['target']} applies when: {item['condition']}")
        for flag in advice:
            out.append(f"        Map advice: {flag}")
    return out


def consolidate_codes(codes):
    """Final-result consolidation: drop a bare 3-character parent when a more specific child
    from the same category was extracted anywhere in the SAME RUN.

    Owner rule, 2026-08-23. "L40" beside an extracted "L40.0" adds nothing to a final list and
    reads as a second condition; the specific child is the better record of the same finding.
    Runs over the whole selected run, not per page or per file. Children never suppress each
    other (L40.0 and L40.50 both stay), a parent with no extracted child stays, and only codes
    actually EXTRACTED from the records participate - an ICD-9 bridge target is a conversion,
    not a finding, and never suppresses anything.

    Detailed page rows are untouched: what appeared on each page stays reported there.
    """
    norm = {c: re.sub(r'[^0-9A-Za-z]', '', c).upper() for c in codes}
    parented = {n[:3] for n in norm.values() if len(n) > 3}
    return [c for c in codes if not (len(norm[c]) == 3 and norm[c] in parented)]


def dc_section(codes_found, ref, ref_missing_reason="", icd9_found=(), gem=None,
               gem_missing_reason="", snomed_found=(), snomed=None, snomed_missing_reason=""):
    """The report's cross-reference block, as a list of lines.

    codes_found is the distinct ICD-10 codes the report holds, in the order they should print.
    icd9_found is the distinct codes the RECORD ITSELF labeled ICD-9-CM, which are bridged
    through the GEM to ICD-10-CM and only then looked up here. snomed_found is the distinct
    codes the record labeled SNOMED CT, crossed through the licensed US map the same way.

    Each of the three lanes is independent and each fails open on its own: a broken bridge
    costs the ICD-9 block, a broken map costs the SNOMED block, and neither costs a diagnosis.
    """
    out = ["", DC_HEADING, "-" * 78]
    if not codes_found and not icd9_found and not snomed_found:
        out.append("No ICD-10 codes were found above, so there is nothing to look up.")
        return out
    out.append("A LOCAL RESEARCH AID ONLY. This is not an official VA crosswalk, not a rating,")
    out.append("not an entitlement decision, and not advice about what to claim. It reports")
    out.append("which VA diagnostic code a diagnosis is commonly rated under, nothing more.")
    out.append("Confirm anything you rely on against the source document and 38 CFR Part 4.")
    out.append("")
    if ref is None:
        out.append("Reference data unavailable, so no potential diagnostic codes could be")
        out.append("looked up. EVERY DIAGNOSIS ABOVE IS UNAFFECTED: this section is the only")
        out.append("thing missing.")
        if ref_missing_reason:
            out.append(f"Reason:{ref_missing_reason}")
        return out
    if codes_found:
        out.append(f"   {'ICD-10':<12} {'DC':<6} {'BASIS':<34} VA DIAGNOSTIC CODE")
        out.append("   " + "-" * 75)
        for code in codes_found:
            state, dc, name, basis = dc_for_code(ref, code)
            if state == "mapped":
                out.append(f"   {code:<12} {dc:<6} {basis:<34} {name}")
            elif state == "ambiguous":
                out.append(f"   {code:<12} {'-':<6} {basis}")
            else:
                out.append(f"   {code:<12} {'-':<6} no mapping in this reference")
    if icd9_found:
        out.extend(_icd9_bridge_lines(icd9_found, ref, gem, gem_missing_reason))
    if snomed_found:
        out.extend(_snomed_lines(snomed_found, ref, snomed, snomed_missing_reason))
    return out


def _icd9_bridge_lines(icd9_found, ref, gem, gem_missing_reason=""):
    """The labeled-ICD-9-CM half of the cross-reference block.

    Two steps are shown rather than one, because two steps were taken. The ICD-9 code stays
    exactly as the record wrote it, the ICD-10-CM target it was crossed to is printed beside
    it, and the diagnostic code - when there is one - is reached through that target. A reader
    who disagrees with the bridge can see the bridge.
    """
    out = ["",
           "   Codes the record labels ICD-9-CM, bridged through a frozen ICD-9 to ICD-10-CM",
           "   GEM and then looked up above. The GEM is a conversion table, not a VA document,",
           "   and the ICD-9 code in the diagnosis rows above is unchanged."]
    if gem is None:
        out.append("   Bridge data unavailable, so these codes could not be crossed. EVERY")
        out.append("   DIAGNOSIS ABOVE IS UNAFFECTED.")
        if gem_missing_reason:
            out.append(f"   Reason:{gem_missing_reason}")
        return out
    out.append(f"   {'ICD-9-CM':<10} {'ICD-10-CM':<18} {'DC':<6} {'BASIS':<30} "
               f"VA DIAGNOSTIC CODE")
    out.append("   " + "-" * 75)
    for code in icd9_found:
        state, targets, dc, name, basis = gem_bridge(gem, ref, code)
        shown = _gem_targets_text(targets)
        if state == "mapped":
            out.append(f"   {code:<10} {shown:<18} {dc:<6} {basis:<30} {name}")
        else:
            out.append(f"   {code:<10} {shown:<18} {'-':<6} {basis}")
    return out


def all_dates(text):
    """(position, date-string) for every date in a block of text."""
    out = []
    for rx in DATE_RES:
        for m in rx.finditer(text):
            out.append((m.start(), m.group(1)))
    return out


def nearest_date(dates, pos):
    """The date closest to pos, preferring one at or before it."""
    if not dates:
        return ""
    before = [(pos - p, d) for p, d in dates if p <= pos]
    if before:
        return min(before, key=lambda x: x[0])[1]
    return min(dates, key=lambda x: abs(x[0] - pos))[1]


def pick_date(body, header, pos):
    """One date for a code at pos, from the body/header candidate split.

    The order is the old at-or-before rule with one refinement inside each half:
        1. nearest BODY date at or before the code
        2. nearest HEADER date at or before it     (the banner still beats anything below)
        3. nearest BODY date after it
        4. nearest HEADER date after it
    "" when there are no candidates at all - an empty date is honest, an invented one is not.
    """
    for pool, want_before in ((body, True), (header, True), (body, False), (header, False)):
        if want_before:
            side = [(pos - p, d) for p, d in pool if p <= pos]
        else:
            side = [(p - pos, d) for p, d in pool if p > pos]
        if side:
            return min(side, key=lambda x: x[0])[1]
    return ""


def dotted(cat, sub):
    return f"{cat}.{sub}" if sub else cat


# Words in an official description that carry meaning (skip short/filler words). Used to tell a
# real bare code from a look-alike: a diagnosis is written next to its NAME, a device model or
# an ID is not.
_DESC_STOP = {"unspecified", "other", "with", "without", "acute", "chronic", "left", "right",
              "site", "disease", "disorder", "disorders", "type", "and"}


def desc_keywords(desc):
    return {w for w in re.findall(r'[a-z]{4,}', desc.lower()) if w not in _DESC_STOP}


def looks_coded_diagnosis(text, start, end, desc):
    """True if the words of the code's official description appear near where it sits - i.e. the
    code is written next to its diagnosis name, not used as a device model / ID / stray token."""
    kw = desc_keywords(desc)
    if not kw:
        return False
    window = set(re.findall(r'[a-z]{4,}', text[max(0, start - 70):end + 70].lower()))
    return bool(kw & window)


# Nursing "care plan" blocks label goals with tokens (O01, X38, I011...) that HAPPEN to be
# valid ICD-10 codes but are not diagnoses - "O01 The patient is free", "X38 Pain". They are
# always written without a decimal, while the real diagnoses in the same documents (M20.41,
# L97.512) carry one. So on a page that is clearly a care plan, we accept only decimal codes.
CAREPLAN_MARKERS = (
    "nursing diagnosis", "desired outcome", "nursing intervention",
    "plan of care", "care plan",
)


def scan_block(text, codes, loc, results, seen, ocr=False):
    """Find valid ICD-10 codes in one text block (a page, or a whole file).

    loc is the label for where this block is ('p3', 'line 40', ...). seen keys
    on (loc, code) so a code repeated on the same page is reported once. ocr marks
    rows that came from OCR of a scanned page (lower confidence).
    """
    dates_body, dates_header = date_candidates(text)
    low = text.lower()
    careplan = any(mk in low for mk in CAREPLAN_MARKERS)
    for m in CODE_RE.finditer(text):
        cat, sub = m.group(1), m.group(2)
        dotless = cat + (sub or "")
        if dotless not in codes:
            continue
        # On a care-plan page, a decimal-less token is almost certainly a nursing goal code,
        # not a diagnosis - drop it.
        if careplan and sub is None:
            continue
        # A BARE (no-decimal) code is the big source of false positives: device models
        # ("VALLEYLAB / F10 / ..."), IDs, nursing-goal codes, a stray "B12". A real bare
        # diagnosis is written next to its NAME, so require the description to appear nearby.
        # Decimal codes are specific enough to trust on their own (they may sit in a table cell
        # away from their text).
        #
        # This is the ONLY bare-code gate, and a scanned page is judged by it on the same terms
        # as a text one. A blanket "reject every bare code that came from OCR" rule used to sit
        # above this and short-circuit it. It cost ten rows of L40 (Psoriasis) on a dermatology
        # record that codes every psoriasis entry bare, and made one whole file report finding
        # nothing at all. A stray token OCR happens to read as a valid category still has no
        # diagnosis name beside it, so the check below already rejects it; the ban added no
        # protection this did not have, and lost real diagnoses.
        if sub is None and not looks_coded_diagnosis(text, m.start(), m.end(), codes[dotless]):
            continue
        # An identifier, not a diagnosis: a code immediately followed by a long run of digits
        # is part of an accession / MRN string (e.g. "P01 0000000000"), not "P01 <disease>".
        tail = text[m.end():m.end() + 12]
        if re.match(r'[\s:/-]*\d{5,}', tail):
            continue
        # Skip a code the document itself labels ICD-9 (e.g. "ICD-9-CM V18.0").
        pre = text[max(0, m.start() - 12):m.start()].lower()
        if "icd-9" in pre or "icd9" in pre:
            continue
        key = (loc, dotless)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "loc": loc,
            "code": dotted(cat, sub),
            "desc": codes[dotless],
            "date": pick_date(dates_body, dates_header, m.start()),
            "ocr": ocr,
            "sys": "icd10",
        })


# ---- diagnoses coded under another system ----------------------------------
# Records routinely carry problems coded in ICD-9-CM or SNOMED CT instead of ICD-10
# ("Hypertension / SNOMED CT 1215744012 / Confirmed"). Those ARE coded diagnoses, so they are
# reported as well, tagged with their system so they are never confused with an ICD-10 code.
OTHER_SYS_RE = re.compile(
    r'\b(ICD[-\s]?9(?:[-\s]?CM)?|SNOMED(?:[-\s]?CT)?)\s*[:#]?\s*'
    r'([A-Z]?\d{2,}(?:\.\d{1,3})?)\b', re.I)

# Trailing status field of a problem-list row - metadata, not part of the diagnosis name.
ROW_META = re.compile(r'^(?:confirmed|(?:no\s*longer\s*)?active|inactive|resolved|chronic|acute|'
                      r'principal|primary|secondary|final|working|suspected|probable|ruled\s*out|'
                      r'rank|unranked|low|medium|high|mild|moderate|severe)\.?$', re.I)


def _sys_name(raw):
    s = re.sub(r'[-\s]+', '', raw).upper()
    if s.startswith("SNOMED"):
        return "SNOMED CT"
    return "ICD-9-CM" if s.endswith("CM") else "ICD-9"


def trim_fields(s):
    """A Cerner problem row is "Name / <system> <code> / Confirmed". Keep just the name."""
    parts = [p.strip() for p in re.split(r'\s+/\s+', s)]
    if len(parts) > 1 and parts[0] and any(OTHER_SYS_RE.search(p) or ROW_META.match(p)
                                           for p in parts[1:]):
        return parts[0]
    return s


def scan_other_codes(lines, loc_for, results, seen, ocr=False):
    """Report diagnoses coded under a non-ICD-10 system. seen is SHARED with scan_diagnoses so
    the same problem is not also listed a second time as un-coded."""
    for i, raw in enumerate(lines):
        line = clean_dx(raw)
        for m in OTHER_SYS_RE.finditer(line):
            name = clean_dx(line[:m.start()]).rstrip('/').strip()
            j = i
            while not name and j > 0:        # code on its own line - its name is the line above
                j -= 1
                name = clean_dx(lines[j]).rstrip('/').strip()
            name = trim_fields(name)
            if not looks_like_diagnosis(name):
                continue
            loc = loc_for(i)
            key = (loc, name.lower())
            if key in seen:
                continue
            seen.add(key)
            system = _sys_name(m.group(1))
            results.append({
                "loc": loc,
                "code": m.group(2),
                "desc": f"{name}  [{system}]",
                "date": "",
                "ocr": ocr,
                "sys": "other",
                # The code system as its own field, not parsed back out of desc later. desc is
                # display text and picks up a "(x3)" count in --unique mode; deciding whether a
                # row may be bridged by re-reading display text is how a repeat diagnosis
                # quietly stops being an ICD-9 row.
                "csys": system,
            })


# ---- provenance ------------------------------------------------------------
# Records carry their OWN page numbering ("Page 437 of 1,019") and a review stamp naming the
# clinician and when they signed off. Neither is a diagnosis, but both are how you cite a record,
# so they are lifted out and shown as a heading over the diagnoses they belong to - never dropped,
# and never reported as findings in their own right.
# Both footer forms real records print: "Page 437 of 1,019" and "Page 157/191". Only this
# exact shape is metadata - a rule dropping every line with the word "page" would eat a
# genuine diagnosis note, so the number-of/slash-number shape is required.
META_PAGE = re.compile(r'\bPage\s+([\d,]+)\s*(?:of|/)\s*([\d,]+)\b', re.I)
META_REVIEWED = re.compile(r'\bLast\s+Reviewed\s+Date\s*:\s*(.+)$', re.I)
META_SKIP = re.compile(r'^\s*(?:Page\s+[\d,]+\s*(?:of|/)\s*[\d,]+\s*$|'
                       r'Last\s+Reviewed\s+Date\s*:)', re.I)


def scan_page_meta(lines):
    """Pull the document's own page number and review stamp out of one page of text."""
    meta = {}
    for i, raw in enumerate(lines):
        line = clean_dx(raw)
        m = META_PAGE.search(line)
        if m and "docpage" not in meta:
            meta["docpage"] = f"{m.group(1)} of {m.group(2)}"
        m = META_REVIEWED.search(line)
        if m and "reviewed" not in meta:
            # "6/22/2025 19:34 EDT; Surname DO, Forename" - the surname-first name routinely wraps
            # onto the next line, leaving the forename stranded by itself.
            tail, j = m.group(1).strip(), i
            while tail.endswith(",") and j + 1 < len(lines):
                j += 1
                tail = (tail + " " + clean_dx(lines[j])).strip()
            when, _, who = tail.partition(";")
            meta["reviewed"] = when.strip()
            if who.strip():
                meta["reviewer"] = who.strip()
    return meta


def _norm_dx(desc):
    """A diagnosis name reduced to letters and digits, for comparing two spellings of one name
    ("Anal fissure, unspecified" vs "Anal fissure,unspecified")."""
    desc = re.sub(r'\s*\[[^\]]+\]\s*$', '', desc)
    return re.sub(r'[^a-z0-9]+', '', desc.lower())


def merge_duplicate_names(rows, grain="page"):
    """Drop a bare-name row when the SAME diagnosis nearby already carries a code.

    A problem-list table puts the code in one column and the name in another; the two passes
    each catch one of them, so one diagnosis arrives twice. The coded row is the better record,
    so that is the one kept.

    grain says how close "nearby" is. A PDF has real pages, so two entries on one page are the
    same entry; matching across a 1,000-page file would wrongly merge a diagnosis coded on page
    10 with a different mention on page 900. A text file has no pages - its loc is a line number,
    which would never match - so there the whole file is one scope.
    """
    def scope(row):
        return row["loc"] if grain == "page" else ""

    coded = {}
    for r in rows:
        if r["code"] != "-":
            coded.setdefault(scope(r), set()).add(_norm_dx(r["desc"]))
    return [r for r in rows
            if not (r["code"] == "-" and _norm_dx(r["desc"]) in coded.get(scope(r), ()))]


# ---- code-less diagnoses ---------------------------------------------------
# Many records list diagnoses by NAME with no code attached, under a heading such as
# "Visit Diagnoses" / "Diagnosis" / "Assessment". We capture those names too, so a diagnosis
# is not missed just because nobody wrote its code. A line that already carries a valid ICD-10
# code is left to the code pass above (so it is not reported twice).

# A line that STARTS a diagnosis list.
# STRONG: the plural word alone is an encounter diagnosis list, with or without a colon.
DX_HEADERS = re.compile(r'^(?:visit\s+)?diagnoses\b', re.I)
# WEAK: the singular forms that operative reports and H&Ps actually use - "Diagnosis:",
# "PREOPERATIVE DIAGNOSIS:", "Problem list:". A BARE singular "Diagnosis" is often just a table
# column label (Cerner prints "PreOp Diagnosis" / "SN - Diagnosis" as grid labels) and opening a
# list on it swallows the whole table - so a weak header must carry a COLON to count.
DX_HEADERS_WEAK = re.compile(
    r'^(?:(?:pre|post)[\s-]*(?:operative|op)\s*)?'
    r'(?:associated|admission|admitting|discharge|final|principal|primary|working)?\s*'
    r'diagnos[ei]s\s*:'
    r'|^problem\s+list\s*:', re.I)
DX_HEADER_SKIP = re.compile(r'nursing', re.I)
# Numbering on a list item: "1. Left hammertoes 2,3,4", "4) HTN", "a. ...".
LIST_NUM = re.compile(r'^\s*(?:\d{1,2}[.)]|[a-z][.)])\s+')
# The same numbering with the dot lost in the PDF text ("3 Psoriasis"). Only trusted once the
# list has already shown a properly punctuated item, so a name that merely starts with a number
# is not mangled.
LIST_NUM_BARE = re.compile(r'^\s*\d{1,2}\s+')
# In an operative report every section is an ALL-CAPS label with a colon ("PROCEDURE:",
# "IMPLANTS:", "ANESTHESIA:"). Any of them means the diagnosis list has ended.
CAPS_SECTION = re.compile(r"^[A-Z][A-Z0-9 ,/()&.'-]{1,60}:")
# Placeholder text where a diagnosis would go - "Associated Diagnoses: None".
NON_DX = {"none", "n/a", "na", "nka", "nkda", "unknown", "notapplicable", "<none>",
          "nonelisted", "noneknown", "nonereported", "seebelow", "deferred"}


def is_dx_header(line):
    if DX_HEADER_SKIP.search(line):
        return False
    return bool(DX_HEADERS.match(line) or DX_HEADERS_WEAK.match(line))
# A medication / dosing / instruction line - a diagnosis block does not contain these, so the
# first one means the block has ended.
MED_CUE = re.compile(
    r'(\b\d+\s*(?:MG|MCG|ML|G|IU|UNIT|MG/DOSE)\b|\b(?:tablet|capsule|solution|powder|spray|'
    r'inhaler|injection|suspension|ointment|cream|patch)\b|\boral;|\bduration\s*:|'
    r'\btake\s|\bplace\s|\binhale\b|\bapply\b|\bDOB\s*:|\bacc\s*no|\bMRN\b)', re.I)

# These records come out garbled ("Dia nosis", "I nsu ranee", "Resu Its"), so section markers
# are matched with the spaces SQUASHED OUT. A section header that ends the diagnosis list.
STOP_PREFIX = (
    "insuran", "member", "subscriber", "plan/", "name:", "orders", "vital", "medication",
    "immuniz", "results", "allerg", "history", "reason", "referred", "note", "bluecross",
    "payer", "group", "policy", "signed", "electronically", "provider", "physician",
    "department", "encounter", "specialty", "careteam", "results", "labresult", "component",
    "narrative", "authorizing", "performing", "collection", "specimen", "comment",
    # a "Laboratories" section heading ends a named-diagnosis list: what follows is lab
    # result wrappers, not diagnoses
    "laborator",
    # lab / imaging table column labels that otherwise leak into the list
    "analysis", "performed", "time", "value", "laterality", "modality", "anatomical",
    "impressions", "testmethod", "refrange", "range", "resulttype", "resultstatus",
    # operative-report sections that follow the pre/post-op diagnosis list
    "procedure", "anesthesia", "estimatedblood", "injectib", "complication", "descriptionof",
    "chiefcomplaint", "reviewofsystems", "healthstatus", "physicalexam", "condition:",
)
# A sub-header / column label INSIDE the block - skip it but keep reading the list.
NOISE_SQUASH = re.compile(r'^(?:visit)?dia?g?nos[ei]s$|^status$|^\(?icdcode\)?$|^code$|'
                          r'^type$|^date$|^description$|^primary$|^secondary$|'
                          r'^allproblems$|^problemlist$', re.I)

# The same column labels again, but matched on LETTERS ONLY, so a label the PDF text layer
# chewed up still gets recognised - "[)iagnosis" reduces to "iagnosis". NOISE_SQUASH only
# squashes spaces, so it never sees these.
LABEL_LETTERS = {"diagnosis", "diagnoses", "iagnosis", "iagnoses", "dagnosis", "dianosis",
                 "visitdiagnoses", "status", "date", "code", "icdcode", "type", "description",
                 "primary", "secondary", "problemlist", "allproblems", "problems"}

# Cerner prints its problem list as a field grid, one field per line, and the wrapped cells
# arrive as "Severity Class: ; Certainty:" / "0 ; Diagnosis Code:" / "ICD-10-CM ;". A diagnosis
# NAME never ends on a colon or semicolon, so that tail alone identifies a field line.
FIELD_TAIL = re.compile(r'[:;]\s*$')
# The same grid's fields that DO carry a value - "Status: Active", "Recorder: Bertoli,MA".
# The value is record-keeping metadata, not a diagnosis.
META_FIELD = re.compile(
    r'^(?:status|certainty|probability|recorder|severity(?:\s+class)?|life\s+cycle\s+status|'
    r'last\s+(?:updated|reviewed)|responsible\s+provider|onset|resolved|classification|source|'
    r'entered\s+by|confirmation|diagnosis\s+(?:date|type|code)|clinical\s+dx|code|date|'
    r'secondary\s+description)\s*:', re.I)
# In the same grid the diagnosis itself is a labelled field too. Keep the value, drop the label.
DX_FIELD = re.compile(r'^(?:problem\s+name|dx\s+name|condition)\s*:\s*', re.I)
# ...and when THAT label is the one left dangling on its colon, the line under it is a diagnosis
# name, not metadata - so it must not be swallowed as a wrapped field value.
DX_DANGLE = re.compile(r'(?:problem\s+name|dx\s+name|condition)\s*:\s*$', re.I)

# Clinic letterhead that sits inside a diagnosis block on faxed records. None of it is a
# diagnosis: "Suite 100", "SPRINGFIELD, IL 60000-1234", "Phone: tel:+ 1-555-0100".
ADDRESS_CUE = re.compile(
    r'^(?:ste|suite|apt|unit|rm|room|fl|floor|bldg)\b\s*[\d#]'      # "Ste 110"
    r'|,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$'                          # "SPRINGFIELD, IL 60000-1234"
    r'|\b(?:phone|fax|tel)\b\s*:'                                    # "Phone: tel:+ 1-..."
    r'|\btel:\+', re.I)

# Fax cover sheets rule their blanks with underscores and dashes. The text layer returns the
# rule along with the label, giving quote-and-dash soup such as
# "R--'e\"-fe'-'-r--'ra\"-I \"'ID __" or "R_e_fe_rr~_d By Con_t~_c_t ____". A real diagnosis
# name never contains a run of underscores, nor a run of dashes in the MIDDLE of the text
# (a run at either end is just a table rule and is stripped off first, see clean_dx).
FORM_RULE = re.compile(r'__|(?<=\S)-{3,}(?=\S)|-{3,}\s')
# The same soup without a long enough run to trip FORM_RULE ("--'R--'e\"-fe'-'-r--'ra\"-I").
# Diagnosis names do use dashes and apostrophes, so this only fires when they crowd out the
# words: "Non-pressure chronic ulcer of right heel and midfoot" is 1 in 52 characters.
RULE_PUNCT = re.compile(r"['\"\-_]")
# Text so badly decoded it is not worth showing. U+FFFD is the decoder's own "I could not
# read this" marker, so its presence is decisive on its own.
UNREADABLE = re.compile(r'�|^\s*\.{2,}')


# A laboratory RESULT wrapper posing as a diagnosis: "LIPID PANEL (80061) Final, Reviewed
# (Collected: 02/01/2018)". The safe signature is the COMBINATION of a parenthesized bare
# five-digit CPT code and lab result/status/collection wording - either alone is not enough.
# Deliberately narrow: a diagnosis carrying parentheses ("Dyslipidemia (high LDL; low HDL)")
# has no bare five-digit group, and a five-digit number without the status wording is left
# for the existing filters, so neither broad shape is rejected on its own.
LAB_WRAPPER = re.compile(
    r'\(\s*\d{5}\s*\)[^\n]*\b(?:final|reviewed|collected\s*:)|'
    r'\b(?:final|reviewed|collected\s*:)[^\n]*\(\s*\d{5}\s*\)', re.I)

# The other two lines of the same print footer the page counter belongs to, confirmed leaking
# as diagnoses on real records ("03/25/2026 11:58 am" / "SAMPLE, PATIENT DOB 01/01/1970"):
# a line that is NOTHING BUT a date with an optional clock time, and a line carrying a DOB
# label immediately followed by its date. No real diagnosis has either shape; a diagnosis
# that merely CONTAINS a date ("Fracture, seen 03/25/2026 for follow-up") matches neither.
TIMESTAMP_LINE = re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}'
                            r'(?:\s*[ap]\.?m\.?)?)?$', re.I)
DOB_BANNER = re.compile(r'\b(?:d\.?\s?[o0]\.?\s?b\.?|date\s+of\s+birth)\b\s*:?\s*'
                        r'\d{1,2}/\d{1,2}/\d{2,4}', re.I)


def is_block_noise(name):
    """Packaging that sits INSIDE a diagnosis list: a column label the text layer mangled,
    clinic letterhead, the ruled blanks off a fax form, undecodable crumbs.

    Kept apart from looks_like_diagnosis() on purpose. A looks_like_diagnosis() failure means
    we have left the list and scan_diagnoses() stops reading; these lines are interleaved WITH
    the entries, so hitting one must skip the line and read on - the same treatment
    NOISE_SQUASH already gives a garbled "Dia nosis" header.
    """
    if letters_only(name) in LABEL_LETTERS:
        return True
    if LAB_WRAPPER.search(name):
        return True
    if TIMESTAMP_LINE.match(name) or DOB_BANNER.search(name):
        return True
    if FIELD_TAIL.search(name) or META_FIELD.match(name):
        return True
    if ADDRESS_CUE.search(name) or FORM_RULE.search(name):
        return True
    if UNREADABLE.search(name):
        return True
    marks = len(RULE_PUNCT.findall(name))
    return marks >= 4 and marks >= 0.15 * len(name)


def squash(s):
    return re.sub(r'\s+', '', s).lower()


def letters_only(s):
    return re.sub(r'[^a-z]', '', s.lower())


def clean_dx(s):
    s = s.replace('\xa0', ' ').strip().rstrip('.').strip()
    # Table rules bleed into the text either side of a real name - "Acute right ankle
    # pain----------------------------------". Shave them off so the name itself survives.
    return s.strip('-_ ').strip()


def is_stop(line):
    sq = squash(line)
    # Every "... - documented in/as of this encounter" section header carries this word, even
    # when the rest is garbled - the single most reliable end-of-list signal.
    if "documented" in sq:
        return True
    if CAPS_SECTION.match(line):
        return True
    return any(sq.startswith(p) for p in STOP_PREFIX)


def looks_like_diagnosis(name, min_len=4):
    # "Associated Diagnoses: None" - the header is real, the list is empty.
    if squash(name).strip('.:') in NON_DX:
        return False
    if len(name) < min_len or len(name) > 90:
        return False
    if not re.search(r'[A-Za-z]', name):
        return False
    # Dosing / instruction text is not a diagnosis.
    if MED_CUE.search(name):
        return False
    # An ALL-CAPS run of words is an instruction ("TAKE ONE TABLET..."), not a diagnosis name.
    letters = re.sub(r'[^A-Za-z]', '', name)
    if len(letters) > 12 and letters.isupper():
        return False
    return True


def scan_diagnoses(lines, codes, loc_for, results, seen, ocr=False):
    """Capture NAMED diagnoses (no code) under a real diagnosis heading. lines is a list of
    strings; loc_for(i) gives the location label for line i."""
    in_list = False
    captured = 0
    last_row = None                      # the last diagnosis row, for merging wrapped lines
    drop_wrap = False                    # the last row was a repeat - drop its wrapped tail too
    numbered = False                     # this list writes its items as "1." / "2." / ...
    meta_wrap = False                    # inside a wrapped page-footer / review stamp
    field_wrap = False                   # the line before was a field label left on its colon
    for i, raw in enumerate(lines):
        line = clean_dx(raw)
        if not line:
            in_list = False
            last_row = None
            drop_wrap = False
            continue
        if is_dx_header(line):
            in_list = True
            captured = 0
            last_row = None
            drop_wrap = False
            numbered = False
            after = re.sub(r'^[^:]*:\s*', '', line) if ':' in line else ''
            line = clean_dx(after)
            if not line:
                continue
        if not in_list:
            continue
        if is_stop(line) or MED_CUE.search(line):
            in_list = False
            last_row = None
            drop_wrap = False
            continue
        if NOISE_SQUASH.match(squash(line)):
            continue                     # a garbled column header ("Dia nosis") - skip, keep going
        m_field = DX_FIELD.match(line)   # "Problem Name: Acid reflux" - the value is the name
        if m_field:
            line = clean_dx(line[m_field.end():])
            if not line:
                continue
        elif field_wrap:
            # The line before ended on a bare "...; Responsible Provider:", so this line is that
            # field's value wrapped onto its own line ("Al Fayyadh MD,Mohammed Jaafar Atta").
            # Without this it reads as a nameless list entry and lands in the report as a
            # diagnosis. Same trick as meta_wrap above.
            field_wrap = False
            continue
        if is_block_noise(line):
            # A field label left dangling on its colon takes its wrapped value with it.
            field_wrap = bool(re.search(r':\s*$', line)) and not DX_DANGLE.search(line)
            last_row = None              # letterhead / form rule / crumb - skip, keep going
            continue
        field_wrap = False
        # A page footer or review stamp sits INSIDE the list; step over it and keep reading, and
        # swallow the wrapped remainder of a reviewer's name with it ("Surname DO," / "Forename").
        if META_SKIP.match(line):
            meta_wrap = line.endswith(",")
            last_row = None
            continue
        if meta_wrap:
            meta_wrap = line.endswith(",")
            continue
        # A real code on this line means the code pass already reported it - don't duplicate.
        if any((m.group(1) + (m.group(2) or "")) in codes for m in CODE_RE.finditer(line)):
            continue
        # A line that starts lower-case is the tail of the diagnosis on the line before, wrapped
        # by the PDF ("Tear of peroneus longus tendon, right" / "ankle."). Join it back on - and
        # if that diagnosis was a repeat we already dropped, drop its tail too instead of
        # letting the fragment stand as a diagnosis of its own.
        if line[:1].islower():
            if last_row is not None:
                last_row["desc"] = (last_row["desc"] + " " + line).strip()
                continue
            if drop_wrap:
                continue
        # Drop the list numbering ("1. Left hammertoes 2,3,4") and any trailing code/status
        # fields. A NUMBERED item is unambiguously a list entry, so a very short name is
        # trusted there - real records write "3. HTN" and "4. DM". Once a list has shown proper
        # "1." numbering, an item whose dot was lost by the PDF ("3 Psoriasis") is numbered too.
        m_num = LIST_NUM.match(line)
        if m_num:
            numbered = True
        elif numbered:
            m_num = LIST_NUM_BARE.match(line)
        name = line[m_num.end():] if m_num else line
        name = re.sub(r'\s*-\s*(primary|secondary)\s*$', '', name, flags=re.I).strip()
        name = trim_fields(name)
        if not looks_like_diagnosis(name, min_len=2 if m_num else 4):
            in_list = False             # we have clearly left the diagnosis list
            last_row = None
            drop_wrap = False
            continue
        if captured >= 30:              # a real encounter list is not this long - safety stop
            in_list = False
            continue
        loc = loc_for(i)
        key = (loc, name.lower())
        if key in seen:
            last_row = None
            drop_wrap = True
            continue
        seen.add(key)
        captured += 1
        row = {"loc": loc, "code": "-", "desc": name, "date": "", "ocr": ocr, "sys": None}
        results.append(row)
        last_row = row
        drop_wrap = False


# ---- per-format readers ----------------------------------------------------

# OCR is optional and heavy, so the engine is built once, lazily, only when a scanned page is
# actually met. Import lives inside the getter so a text-only run never loads onnxruntime.
_OCR_ENABLED = True
_OCR_DPI = 200
# Only used to turn a page count into a rough "this will take N minutes" before a run starts.
# Measured at about 6s a page on the machine this was built on.
_OCR_SECS_PER_PAGE = 6
_ENGINE = None
# Structured results of the most recent completed run_extraction, for the optional claim
# handoff document. None until a run completes; cleared at the start of every run so a
# failed run cannot leave last week's results behind a working button.
LAST_RUN = None
_PROGRESS = None            # a GUI/caller can set this to receive progress lines
_STOP = None                # a GUI/caller can set this to a callable meaning "stop early"


def _emit(msg):
    if _PROGRESS:
        _PROGRESS(msg)
    else:
        print(msg, file=sys.stderr, flush=True)


def debug(msg):
    """Breadcrumbs for diagnosing a frozen --windowed build, which has no console to print to.
    Silent unless ICD_DEBUG=1 is set in the environment."""
    if os.environ.get("ICD_DEBUG") != "1":
        return
    try:
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
        with open(os.path.join(base, "icd_tool_debug.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%H:%M:%S.%f}  {msg}\n")
    except OSError:
        pass


def _stopped():
    """True once the caller has asked for the scan to end. Checked between pages and between
    files, so Stop lands promptly even in the middle of a long OCR run."""
    return bool(_STOP and _STOP())


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def ocr_page(page):
    """Render a PDF page to an image and return the OCR'd text (empty on any failure)."""
    try:
        pix = page.get_pixmap(dpi=_OCR_DPI)
        result, _ = _engine()(pix.tobytes("png"))
        return "\n".join(b[1] for b in (result or []))
    except Exception:
        return ""


# A "searchable scan" is a picture of the page with a thin electronic text layer laid over it -
# usually just the patient banner and a page number, while every word of the record lives in the
# picture. Anything under this many characters on a page that is mostly image is treated as one.
# The real banners run about 120 characters; a genuine text page runs into the thousands.
THIN_TEXT_CHARS = 300
THIN_IMAGE_COVER = 0.5


def image_cover(page):
    """Fraction of the page covered by images, 0.0 when that cannot be worked out.

    Measured off the page's own layout blocks (type 1 is an image) rather than get_image_bbox,
    which raises on any image the page lists but never places - and prints the whole traceback
    to stderr on its way out, even when the caller catches it.
    """
    try:
        total = abs(page.rect.get_area())
        if not total:
            return 0.0
        covered = 0.0
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") == 1:
                x0, y0, x1, y1 = block["bbox"]
                covered += abs((x1 - x0) * (y1 - y0))
        return covered / total
    except Exception:
        return 0.0


def needs_ocr(page, text):
    """True when the page's words are in a picture rather than in its text layer.

    Two cases: no text at all (a plain scan), or a text layer so thin against so much image
    that it can only be a banner printed over a scan. The second case used to read as a normal
    page, so the record inside the picture went into the report as a name and a page number.
    """
    if not page.get_images():
        return False
    if not text.strip():
        return True
    return len(text.strip()) < THIN_TEXT_CHARS and image_cover(page) >= THIN_IMAGE_COVER


def read_pdf(path, codes, results, meta):
    import fitz
    meta["grain"] = "page"               # real pages, so provenance and merging work per page
    seen = set()
    seen_dx = set()
    doc = fitz.open(path)
    scanned = [i for i in range(doc.page_count) if needs_ocr(doc[i], doc[i].get_text())]
    ocr_done = 0
    name = os.path.basename(path)
    if scanned and _OCR_ENABLED:
        # OCR runs at seconds per page, so on a records set this is the difference between a
        # half-minute run and a long one. Say so before it starts rather than looking hung.
        _emit(f"   {name}: {len(scanned)} scanned page(s) to read by OCR - "
              f"roughly {max(1, round(len(scanned) * _OCR_SECS_PER_PAGE / 60))} min. "
              f"Stop ends the run early and keeps what was found.")
    for i in range(doc.page_count):
        if _stopped():
            meta["stopped"] = True
            break
        page = doc[i]
        text = page.get_text()
        is_scan = needs_ocr(page, text)
        if is_scan:
            if not _OCR_ENABLED:
                continue
            ocr_done += 1
            _emit(f"   OCR {name} page {i+1} ({ocr_done}/{len(scanned)})...")
            # Keep the banner text and add the OCR to it. The banner is where the page's date
            # and doctor usually sit, and provenance reads them off this same text.
            text = (text.rstrip() + "\n" + ocr_page(page)).strip()
            if not text.strip():
                continue
        elif i % 5 == 0 or i + 1 == doc.page_count:
            _emit(f"   {name}: page {i+1} of {doc.page_count}")
        loc = f"p{i+1}"
        scan_block(text, codes, loc, results, seen, ocr=is_scan)
        lines = text.splitlines()
        info = scan_page_meta(lines)
        if info:
            meta.setdefault("pages", {})[loc] = info
        # Other-system codes first: it seeds seen_dx, so a problem coded in ICD-9/SNOMED is not
        # then reported a second time as an un-coded name.
        scan_other_codes(lines, lambda k, L=loc: L, results, seen_dx, ocr=is_scan)
        scan_diagnoses(lines, codes, lambda k, L=loc: L, results, seen_dx, ocr=is_scan)
    doc.close()
    if scanned and not _OCR_ENABLED:
        meta["ocr_pages"] = len(scanned)
    elif scanned:
        meta["ocr_read"] = len(scanned)


def read_docx(path, codes, results, meta):
    import docx
    seen = set()
    seen_dx = set()
    d = docx.Document(path)
    paras = [p.text for p in d.paragraphs]
    for idx, txt in enumerate(paras, start=1):
        if txt.strip():
            scan_block(txt, codes, f"para {idx}", results, seen)
    meta["default"] = scan_page_meta(paras)
    scan_other_codes(paras, lambda i: f"para {i+1}", results, seen_dx)
    scan_diagnoses(paras, codes, lambda i: f"para {i+1}", results, seen_dx)


def read_text(path, codes, results, meta):
    seen = set()
    seen_dx = set()
    # utf-8-sig, not utf-8: Notepad / Excel / PowerShell write a BOM, and left in place it sticks
    # to the front of line 1 so a heading there ("Diagnosis:") no longer matches at ^.
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    for idx, line in enumerate(lines, start=1):
        scan_block(line, codes, f"line {idx}", results, seen)
    meta["default"] = scan_page_meta(lines)
    scan_other_codes(lines, lambda i: f"line {i+1}", results, seen_dx)
    scan_diagnoses(lines, codes, lambda i: f"line {i+1}", results, seen_dx)


def read_html(path, codes, results, meta):
    from bs4 import BeautifulSoup
    seen = set()
    seen_dx = set()
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        soup = BeautifulSoup(f, "html.parser")
    lines = soup.get_text("\n").splitlines()
    for idx, line in enumerate(lines, start=1):
        if line.strip():
            scan_block(line, codes, f"line {idx}", results, seen)
    meta["default"] = scan_page_meta(lines)
    scan_other_codes(lines, lambda i: f"line {i+1}", results, seen_dx)
    scan_diagnoses(lines, codes, lambda i: f"line {i+1}", results, seen_dx)


def _hl7_date(v):
    """HL7 TS value ("20250714091500-0400", "20250714") to "YYYY-MM-DD". Unparseable -> as-is."""
    v = (v or "")[:8]
    if len(v) == 8 and v.isdigit():
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    return v


def read_cda(path, codes, results, meta):
    """A CDA/CCD/IHE-XDM document codes each diagnosis explicitly - <code>, <value> (an
    observation's coded result) or <translation> (a secondary coding of the same concept)
    element carries a codeSystem OID, a code and its displayName. There is no free text to
    search: the structure already says exactly what a diagnosis is, so this reads the tree
    instead of running the text-based scanners the other formats need.
    """
    import xml.etree.ElementTree as ET
    meta["grain"] = "file"
    seen = set()
    seen_dx = set()

    tree = ET.parse(path)
    root = tree.getroot()
    parent_map = {child: p for p in root.iter() for child in p}

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    def find_date(el):
        # The code's date is never on the code element itself - walk up to the enclosing
        # act/observation/entry and take the first effectiveTime found among its children.
        node = el
        while node in parent_map:
            node = parent_map[node]
            for child in node:
                if local(child.tag) == "effectiveTime":
                    val = child.get("value")
                    if val:
                        return _hl7_date(val)
                    low = next((c for c in child if local(c.tag) == "low"), None)
                    if low is not None and low.get("value"):
                        return _hl7_date(low.get("value"))
        return ""

    idx = 0
    for el in root.iter():
        if local(el.tag) not in ("code", "value", "translation"):
            continue
        cs = el.get("codeSystem")
        code_val = el.get("code")
        if not cs or not code_val:
            continue
        disp = (el.get("displayName") or "").strip()
        idx += 1
        loc = f"entry {idx}"

        if cs == CDA_ICD10_OID:
            dotless = code_val.replace(".", "")
            desc = codes.get(dotless) or disp
            if not desc:
                continue
            key = (loc, dotless)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "loc": loc, "code": code_val, "desc": desc,
                "date": find_date(el), "ocr": False, "sys": "icd10",
            })
        elif cs in CDA_OTHER_SYS:
            # SNOMED CT also codes a problem's STATUS/severity/likelihood as a <value> next to
            # its diagnosis ("Active", "Confirmed", "Rank") - same shape as a real diagnosis
            # code, but not one. ROW_META already lists the status words a Cerner-style row
            # trims off; reuse it here to drop the same noise.
            if not disp or ROW_META.match(disp):
                continue
            key = (loc, disp.lower())
            if key in seen_dx:
                continue
            seen_dx.add(key)
            results.append({
                "loc": loc, "code": code_val,
                "desc": f"{disp}  [{CDA_OTHER_SYS[cs]}]",
                "date": find_date(el), "ocr": False, "sys": "other",
                "csys": CDA_OTHER_SYS[cs],
            })


READERS = {
    ".pdf": read_pdf, ".docx": read_docx,
    ".txt": read_text, ".htm": read_html, ".html": read_html,
    ".xml": read_cda,
}


def process_file(path, codes):
    ext = os.path.splitext(path)[1].lower()
    reader = READERS.get(ext)
    if not reader:
        return None
    results = []
    meta = {}
    try:
        reader(path, codes, results, meta)
    except Exception as e:
        return {"error": str(e), "rows": [], "meta": meta}
    rows = merge_duplicate_names(results, meta.get("grain", "file"))
    return {"error": None, "rows": rows, "meta": meta}


def is_own_report(path):
    """True if this file is a report THIS tool wrote. Scanning a folder would otherwise pick up
    the previous run's report and read its findings back in as if they were a medical record."""
    if os.path.splitext(path)[1].lower() not in (".txt", ".htm", ".html"):
        return False
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.readline().strip() == REPORT_BANNER
    except OSError:
        return False


def gather(target, output=None):
    """Every supported file under target, minus this run's own output file and any report left
    behind by a previous run."""
    out = os.path.abspath(output) if output else None

    def keep(path):
        if out and os.path.abspath(path) == out:
            return False
        return not is_own_report(path)

    if os.path.isfile(target):
        return [target] if keep(target) else []
    files = []
    for root, _, names in os.walk(target):
        for n in names:
            if n.startswith("~$"):            # Word/Excel lock & temp files
                continue
            if os.path.splitext(n)[1].lower() not in SUPPORTED:
                continue
            path = os.path.join(root, n)
            if keep(path):
                files.append(path)
    return sorted(files)


HANDOFF_BOUNDARY = """This document organizes references found in the selected records. It is not medical or legal
advice, an official VA mapping, a rating prediction, proof of service connection, or a
recommendation about what to claim. Confirm every item against the source record."""


# The ORDINARY refusal: the reference is fine and simply has no row for this code. It is the
# commonest outcome in a real record, and printing it beside every entry is what made the old
# document unreadable - fifty-odd identical lines saying the same nothing. It is now stated ONCE
# at the head of the section that holds those entries, and the per-row cell says what the entry
# IS instead. Every OTHER refusal still prints per row, because "the category vote split" and
# "the map needs patient context" send a reader somewhere different.
HANDOFF_ORDINARY_NO_MATCH = "no mapping in the local reference"

# The reason a SNOMED concept produced no diagnostic code, in the map's own terms. A refusal
# has to say WHICH refusal it is: "not in the map" and "the map needs patient context" send a
# reader to two completely different next steps.
SNOMED_NO_DC_REASON = {
    "conditional": "none - the map needs patient context, so no target was resolved",
    "no_classification": "none - the map cannot classify this concept with available data",
    "absent": "none - this concept is not in the map",
    "unavailable": "map or reference data unavailable",
}

# How wide EVERY field label in the appendix is. Sized to the longest one that can appear,
# "Potential ICD-10-CM", because the alternative is a label like "Status/encounter" pushing its
# own colon a column to the right of the "Code" line directly beneath it. One width, one
# alignment, no ragged entries.
SNOMED_FIELD_WIDTH = 19
HANDOFF_FIELD_WIDTH = SNOMED_FIELD_WIDTH


def _handoff_dc_facts(run, row):
    """Everything the Potential-DC lane knows about ONE row, as data rather than as text.

    One function, so the grouped summary at the top of the handoff and the evidence appendix at
    the bottom can never disagree about what a row's research result was. Reuses dc_for_code,
    gem_bridge and snomed_map, so neither surface can say more than the report's own
    cross-reference section says: an ambiguous vote is a refusal, a conditional map is
    unresolved, a missing reference is unavailable, and none of the three is ever an answer.

    Returns a dict:
        matches   [(dc, name, basis), ...] - EVERY diagnostic code this row reached. A list
                  because a SNOMED concept with two unconditional map groups reaches two codes
                  that apply TOGETHER; collapsing them would drop half of what the map said.
        status    one honest line when there is no match, "" when there is
        lines     the appendix's field lines for this row, already rendered
    """
    ref, gem, snomed = run["ref"], run["gem"], run.get("snomed")
    if row.get("sys") == "icd10":
        state, dc, name, basis = dc_for_code(ref, row["code"])
        if state == "mapped":
            return {"matches": [(dc, name, basis)], "status": "",
                    "lines": [f"{'Potential VA DC':<{HANDOFF_FIELD_WIDTH}} : {dc} {name}  "
                              f"({basis}) [research cross-reference]"]}
        status = {"ambiguous": basis,
                  "unmapped": "no mapping in the local reference"}.get(
                      state, "reference data unavailable")
        return {"matches": [], "status": status,
                "lines": [f"{'Potential VA DC':<{HANDOFF_FIELD_WIDTH}} : {status}"]}
    csys = str(row.get("csys", ""))
    if csys.startswith("ICD-9"):
        state, targets, dc, name, basis = gem_bridge(gem, ref, row["code"])
        via = f" via ICD-10-CM {_gem_targets_text(targets)}" if targets else ""
        if state == "mapped":
            return {"matches": [(dc, name, f"{basis}{via}")], "status": "",
                    "lines": [f"{'Potential VA DC':<{HANDOFF_FIELD_WIDTH}} : {dc} {name}  "
                              f"({basis}{via}) [research cross-reference]"]}
        if state == "unavailable":
            return {"matches": [], "status": "bridge/reference data unavailable",
                    "lines": [f"{'Potential VA DC':<{HANDOFF_FIELD_WIDTH}} : bridge/reference data "
                          f"unavailable"]}
        return {"matches": [], "status": f"{basis}{via}",
                "lines": [f"{'Potential VA DC':<{HANDOFF_FIELD_WIDTH}} : none - {basis}{via}"]}
    if csys.startswith("SNOMED"):
        state, findings, _advice = snomed_map(snomed, ref, row["code"])
        kind = snomed_type_name(snomed, row["code"])
        targets = ", ".join(f["target"] for f in findings)
        if state in ("direct", "multi_group"):
            shown = targets or "none"
        elif state == "conditional":
            shown = f"{targets} (candidates, not resolved)" if targets else "none"
        elif state == "unavailable":
            shown = "not available"
        else:
            shown = "none"
        matches, answers = [], []
        for item in findings:
            if item["dc_state"] == "mapped":
                matches.append((item["dc"], item["name"],
                                f"{item['basis']} via ICD-10-CM {item['target']}"))
                answers.append(f"{item['target']} -> {item['dc']} {item['name']} "
                               f"({item['basis']})")
            elif item["dc_state"] == "ambiguous":
                answers.append(f"{item['target']} -> {item['basis']}")
            elif item["dc_state"] == "unmapped":
                answers.append(f"{item['target']} -> no mapping in the local reference")
        if answers:
            dc_text = "; ".join(answers) + "  [research cross-reference]"
        elif any(item["dc_state"] == "unavailable" for item in findings):
            # The MAP answered. It is the separate Potential-DC reference that is missing, and
            # the two are never merged into one vague excuse.
            dc_text = "reference data unavailable"
        else:
            dc_text = SNOMED_NO_DC_REASON.get(state, "none")
        lines = [
            f"{'SNOMED concept type':<{SNOMED_FIELD_WIDTH}} : {kind}",
            f"{'Potential ICD-10-CM':<{SNOMED_FIELD_WIDTH}} : {shown}",
            f"{'Map basis':<{SNOMED_FIELD_WIDTH}} : {SNOMED_HANDOFF_BASIS[state]}",
        ]
        # THE LINE IS OMITTED, NOT SET TO "none". Owner ruling, 2026-08-24: a
        # `Potential VA DC : none` under a procedure still tells a reader the procedure was
        # weighed as a possible diagnosis and came up short, which is not what happened. It was
        # never eligible. The terminology above says what it is; the silence says the rest.
        eligible = snomed_is_diagnosis(snomed, row["code"])
        if eligible:
            lines.append(f"{'Potential VA DC':<{SNOMED_FIELD_WIDTH}} : {dc_text}")
        return {"matches": matches if eligible else [],
                "status": "" if (eligible and matches) else (dc_text if eligible else ""),
                "lines": lines}
    return {"matches": [], "status": "", "lines": []}


def _handoff_dc_lines(run, row):
    """The appendix's Potential-DC field lines for one row."""
    return _handoff_dc_facts(run, row)["lines"]


# ---- how a handoff entry is sorted into a section --------------------------
# A CLAIM WORKER SHOULD NOT HAVE TO READ 191 PAGES TO FIND THE USEFUL PART, and should never be
# handed a laboratory order under the word "Diagnosis". So every retained row is placed in
# exactly one section, by a rule that can be stated in a sentence and reproduced by hand. There
# is no clustering, no similarity, no inference: a row is grouped with another only because the
# EXISTING research reference returned the same diagnostic code for both.
#
# Nothing is deleted. A row that is not presented as a diagnosis still appears in its section
# above and again, in full, in the source-page appendix.
HANDOFF_ADMIN = "admin"
HANDOFF_PROCEDURE = "procedure"
HANDOFF_VERIFY = "verify"
HANDOFF_MATCH = "match"
HANDOFF_OTHER = "other"

# An ICD-10 Z-code is "factors influencing health status and contact with health services":
# an encounter, a screening, a status, an immunization, a long-term drug therapy. It is a real
# ICD-10 code and it is not a diagnosis of a disease, so it is separated rather than listed
# beside one. Matched on the extracted code as written, which always starts with its letter.
HANDOFF_Z_RE = re.compile(r'^Z', re.I)

# An ICD-10 R-code is "symptoms, signs and abnormal clinical and laboratory findings, not
# elsewhere classified". Snoring, chest pain and dizziness are things the record OBSERVED, not
# diseases a clinician diagnosed, and calling them diagnoses on a claim organizer is the same
# error the SNOMED type work exists to fix one code system over. The label changes; the entry,
# its code and its research result do not, and an R-code that the reference DID answer for
# still appears under its diagnostic code so nothing is hidden by the rename.
HANDOFF_R_RE = re.compile(r'^R', re.I)
HANDOFF_R_LABEL = "Finding/symptom"

# A laboratory ORDER named in a record, not a diagnosis. Two real examples from a veteran's
# records, both of which the handoff used to label "Diagnosis":
#     HGB A1C (Tosoh G8) (83036) Ordered
#     MICROALBUMIN URINE QUANT (82043) Ordered
# The rule is deliberately NARROW and needs BOTH halves of the evidence the record itself
# printed: a parenthesised 5-digit procedure-code-shaped number AND a trailing order/result
# status word. A looser rule would start quarantining real diagnoses that happen to carry a
# number, and the cost of that is worse than the cost of leaving one lab order in place.
#
# This changes NOTHING about extraction. The row is still found, still reported, still in the
# appendix. It is placed under "entries to verify" with the reason printed, so a reader can
# disagree with this rule in one second by looking at the source page.
HANDOFF_ORDER_RE = re.compile(r'\(\d{5}\)')
HANDOFF_ORDER_WORDS = ("ordered", "order", "collected", "resulted", "pending", "specimen")
HANDOFF_ORDER_REASON = "appears to be an ordered laboratory test"
HANDOFF_UNTYPED_REASON = "type not established from the record"
HANDOFF_SNOMED_UNTYPED_REASON = "the licensed release does not type this concept"


def _handoff_looks_like_an_order(desc):
    """True only for a named entry the record itself wrote as a laboratory order."""
    text = str(desc).strip()
    if not HANDOFF_ORDER_RE.search(text):
        return False
    tail = re.sub(r'[^a-z ]', ' ', text.lower()).split()
    return bool(tail) and tail[-1] in HANDOFF_ORDER_WORDS


def _handoff_place(run, row, facts):
    """(section, label, reason) for one row. The only place this decision is made."""
    snomed = run.get("snomed")
    csys = str(row.get("csys", ""))
    code = row.get("code", "-")
    if row.get("sys") == "icd10" and HANDOFF_Z_RE.match(code):
        return HANDOFF_ADMIN, "Status/encounter", ""
    if row.get("sys") == "icd10" and HANDOFF_R_RE.match(code):
        return (HANDOFF_MATCH if facts["matches"] else HANDOFF_OTHER), HANDOFF_R_LABEL, ""
    if code == "-" or not row.get("csys") and row.get("sys") != "icd10":
        if _handoff_looks_like_an_order(row.get("desc", "")):
            return HANDOFF_VERIFY, "Test order", HANDOFF_ORDER_REASON
        return HANDOFF_VERIFY, "Clinical entry", HANDOFF_UNTYPED_REASON
    if csys.startswith("SNOMED"):
        tag = snomed_type(snomed, code)
        label = SNOMED_HANDOFF_TYPE_LABEL.get(tag, SNOMED_HANDOFF_TYPE_DEFAULT)
        if not tag:
            # FAIL CLOSED. An untyped concept is not assumed to be a diagnosis, and it is not
            # filed under "procedures and events" either, because neither is known to be true.
            return HANDOFF_VERIFY, label, HANDOFF_SNOMED_UNTYPED_REASON
        if tag != SNOMED_DIAGNOSIS_TAG and tag != "finding":
            return HANDOFF_PROCEDURE, label, ""
        return (HANDOFF_MATCH if facts["matches"] else HANDOFF_OTHER), label, ""
    return (HANDOFF_MATCH if facts["matches"] else HANDOFF_OTHER), "Diagnosis", ""


def _handoff_split_repeat(desc):
    """("Type 2 diabetes", "x85") out of "Type 2 diabetes  (x85)"."""
    m = re.search(r'\s*\(x(\d+)\)\s*$', str(desc))
    if not m:
        return str(desc).strip(), ""
    return str(desc)[:m.start()].strip(), f"x{m.group(1)}"


def _handoff_entries(run):
    """Every retained row of the run, with its section, its research result and its source.

    Built ONCE and used by all five sections, so a row cannot be summarized one way at the top
    and evidenced another way at the bottom, and so the counts reconcile by construction rather
    than by a second pass that could drift.
    """
    # The handoff is a FINAL-RESULT surface, so the same consolidation applies as the report's
    # paste block: a bare parent whose specific child was extracted anywhere in the run is
    # omitted here, while the page rows of the report keep it as source evidence.
    all_icd10 = [r["code"] for f in run["files"] for r in f["rows"] if r.get("sys") == "icd10"]
    keep = set(consolidate_codes(all_icd10))

    entries = []
    for n, f in enumerate(run["files"]):
        alias = f"F{n + 1}"
        if f["error"] or not f["rows"]:
            continue
        for r in f["rows"]:
            if r.get("sys") == "icd10" and r["code"] not in keep:
                continue
            info = f["pages"].get(r["loc"]) or f["default"]
            facts = _handoff_dc_facts(run, r)
            section, label, reason = _handoff_place(run, r, facts)
            desc, repeat = _handoff_split_repeat(
                re.sub(r'\s*\[[^\]]+\]\s*$', '', r["desc"]) if r.get("csys") else r["desc"])
            loc = r["loc"]
            docpage = info.get("docpage", "")
            source = f"{alias} {loc}"
            # p5/d2 is "extracted page 5, the page the document numbers 2". The two differ
            # whenever a record has a cover sheet, and citing the wrong one sends whoever
            # checks this to the wrong page. Only added when the location IS a page: a text
            # file's "line 7" is not page 5 of anything, and "line 7/d5" would say it was.
            if docpage and f["grain"] == "page":
                source += f"/d{str(docpage).split(' of ')[0]}"
            entries.append({
                "alias": alias, "file": f, "loc": loc, "docpage": docpage,
                "reviewer": info.get("reviewer", ""), "reviewed": info.get("reviewed", ""),
                "code": r["code"], "sys": r.get("sys", ""), "csys": str(r.get("csys", "")),
                "desc": desc, "repeat": repeat, "date": r["date"], "ocr": bool(r.get("ocr")),
                "row": r, "facts": facts, "section": section, "label": label,
                "reason": reason, "source": source,
            })
    return entries


def _handoff_width(values, low, high):
    """A column exactly as wide as its widest value, within limits.

    Derived from the data rather than fixed, so a document of plain ICD-10 codes does not carry
    ten blank columns reserved for an 18-digit SNOMED identifier that never appears, and a
    document that does carry one does not have its columns collide.
    """
    return max(low, min(high, max((len(str(v)) for v in values), default=low)))


def _handoff_table(rows, headings, widths, indent="   "):
    """A plain-text table whose long cells WRAP onto an indented continuation line.

    Truncation is not an option here: the cell that overflows is the clinical entry's own name,
    and half a diagnosis is a different diagnosis.
    """
    out = [indent + " ".join(h[:w].ljust(w) for h, w in zip(headings, widths)).rstrip(),
           indent + " ".join("-" * w for w in widths)]
    for cells in rows:
        cells = [str(c) for c in cells]
        first, rest = [], []
        for cell, w in zip(cells, widths):
            if len(cell) <= w:
                first.append(cell.ljust(w))
                rest.append("")
                continue
            cut = cell.rfind(" ", 0, w + 1)
            cut = cut if cut > w // 2 else w
            first.append(cell[:cut].ljust(w))
            rest.append(cell[cut:].strip())
        out.append(indent + " ".join(first).rstrip())
        while any(rest):
            line, nxt = [], []
            for cell, w in zip(rest, widths):
                if len(cell) <= w:
                    line.append(cell.ljust(w))
                    nxt.append("")
                    continue
                cut = cell.rfind(" ", 0, w + 1)
                cut = cut if cut > w // 2 else w
                line.append(cell[:cut].ljust(w))
                nxt.append(cell[cut:].strip())
            out.append(indent + " ".join(line).rstrip())
            rest = nxt
    return out


def _handoff_date(entry):
    """A date, marked once as approximate rather than explained on every row."""
    return f"{entry['date']}*" if entry["date"] else "-"


def _handoff_entry_rows(entries, extra=None):
    """The shared CODE / CLINICAL ENTRY / DATE / SOURCE / SEEN table."""
    heads = ["CODE", "CLINICAL ENTRY", "DATE", "SOURCE"]
    cells = [[e["code"], e["desc"] + ("  (OCR)" if e["ocr"] else ""),
              _handoff_date(e), e["source"]] for e in entries]
    widths = [_handoff_width([c[0] for c in cells], 8, 18),
              _handoff_width([c[1] for c in cells], 20, 46),
              _handoff_width([c[2] for c in cells], 10, 12),
              _handoff_width([c[3] for c in cells], 8, 16)]
    # SEEN only exists when duplicates were collapsed. A column of dashes is five characters of
    # width spent telling a reader nothing, on every row.
    if any(e["repeat"] for e in entries):
        heads.append("SEEN")
        for cell, e in zip(cells, entries):
            cell.append(e["repeat"] or "-")
        widths.append(_handoff_width([c[-1] for c in cells], 4, 6))
    if extra:
        heads.append(extra[0])
        for cell, e in zip(cells, entries):
            cell.append(extra[1](e))
        widths.append(_handoff_width([c[-1] for c in cells], 8, 34))
    return _handoff_table(cells, heads, widths)


def _handoff_sort(entries):
    """Clinical entry name, then code, then source. Deterministic and explainable."""
    return sorted(entries, key=lambda e: (e["desc"].lower(), e["code"], e["source"]))


def _handoff_dc_groups(entries):
    """{(dc, name): [entry, ...]} for every entry that reached a diagnostic code.

    An entry appears in EVERY group it reached. A SNOMED concept with two unconditional map
    groups reaches two diagnostic codes that apply together, and filing it under only one of
    them would quietly drop the other half of the map's answer.
    """
    groups = {}
    for e in entries:
        for dc, name, basis in e["facts"]["matches"]:
            groups.setdefault((dc, name), []).append((e, basis))
    return groups


def claim_handoff_text(run=None):
    """The optional claim-organization handoff document, from the LAST completed run.

    ORGANIZES ONLY. Every field below was already collected during extraction; nothing is
    rescanned, inferred, or added. Raises ValueError when no completed extraction exists, so
    the caller can say so instead of writing an empty file.

    Five sections, in the order a person actually needs them: the run summary and the source
    legend; the research matches grouped by diagnostic code; the coded entries the local
    reference had no answer for; the entries that are not diagnoses and should never have been
    presented as ones; and the complete page-by-page evidence as an appendix. The evidence did
    not shrink. It stopped being the first thing anyone had to read.
    """
    run = run or LAST_RUN
    if not run:
        raise ValueError("No completed extraction to organize - run an extraction first.")

    entries = _handoff_entries(run)
    by_section = {}
    for e in entries:
        by_section.setdefault(e["section"], []).append(e)
    matched = by_section.get(HANDOFF_MATCH, [])
    other = by_section.get(HANDOFF_OTHER, [])
    flagged = (by_section.get(HANDOFF_ADMIN, []) + by_section.get(HANDOFF_PROCEDURE, [])
               + by_section.get(HANDOFF_VERIFY, []))
    nameless = [e for e in entries if e["code"] == "-"]

    out = []

    # ---- 1. run summary and source legend ---------------------------------------------------
    out.append("CLAIM FILE HANDOFF")
    out.append("=" * 78)
    out.append(HANDOFF_BOUNDARY)
    out.append("")
    out.append(f"Scanned: {run['target']}")
    out.append(f"When:    {run['when']}")
    out.append(f"Report:  {run['output']}")
    if run.get("unique"):
        out.append("Options: duplicate entries were collapsed (the SEEN counts show repeats).")
    if run.get("stopped"):
        out.append("STOPPED EARLY - this run was stopped by the user, so this is a PARTIAL")
        out.append("organization of a partial scan, NOT a complete review of the records.")
    out.append("")
    out.append("1. RUN SUMMARY")
    label_w = 36
    for text, n in [("Files reviewed", len(run["files"])),
                    ("Clinical record entries retained", len(entries))]:
        out.append(f"{text + ':':<{label_w}}{n}")
    for text, n in [("Potential VA DC research matches", len(matched)),
                    ("Other coded clinical entries", len(other)),
                    ("Entries flagged for review", len(flagged))]:
        out.append(f"  {text + ':':<{label_w - 2}}{n}")
    out.append(f"    {'of those, named with no code:':<{label_w - 4}}{len(nameless)}")
    out.append("The three indented counts add up to the entries retained. The fourth is a")
    out.append("subset of the entries flagged for review, not a fifth pile.")
    out.append("")
    out.append("SOURCE FILES")
    for n, f in enumerate(run["files"]):
        line = f"F{n + 1} = {f['path']}"
        if f["error"]:
            line += f"   (could not read: {f['error']})"
        out.append(line)
    out.append("")
    out.append("HOW TO READ THIS")
    out.append("SOURCE  F1 p5/d2 means source file F1, extracted page 5, the page the document")
    out.append("        itself numbers 2. Without /d the document printed no page number.")
    out.append("DATE    a trailing * means APPROXIMATE: the nearest date on that page, which is")
    out.append("        not necessarily the date of the entry. Confirm against the record.")
    out.append("SEEN    how many times the entry appeared. Shown only when duplicates were")
    out.append("        collapsed for this run.")

    # ---- 2. potential VA diagnostic-code research matches ------------------------------------
    out.append("")
    out.append("=" * 78)
    out.append("2. POTENTIAL VA DIAGNOSTIC-CODE RESEARCH MATCHES")
    out.append("=" * 78)
    out.append("A LOCAL RESEARCH CROSS-REFERENCE ONLY. Not an official VA crosswalk, not a")
    out.append("rating, not an entitlement decision, and not advice about what to claim.")
    out.append("Verify every group against 38 CFR Part 4 and the source record.")
    out.append("")
    out.append("Entries are grouped because the reference returned the SAME diagnostic code for")
    out.append("them, not because this document says they are the same condition. Every source")
    out.append("code and description is kept as the record wrote it. One entry can appear in")
    out.append("two groups when the map gives two codes that apply together.")
    groups = _handoff_dc_groups(entries)
    if not groups:
        out.append("")
        out.append("   (no entry in this run reached a diagnostic code in the local reference)")
    for dc, name in sorted(groups, key=lambda k: (0, int(k[0]), k[1])
                           if str(k[0]).isdigit() else (1, 0, k[1])):
        members = groups[(dc, name)]
        bases = {basis for _e, basis in members}
        out.append("")
        out.append(f"POTENTIAL DC {dc} - {name}")
        if len(bases) == 1:
            out.append(f"   basis: {bases.pop()}")
        out.append("")
        ordered = _handoff_sort([e for e, _b in members])
        basis_of = {id(e): b for e, b in members}
        extra = None if len(bases) <= 1 else ("BASIS", lambda e: basis_of[id(e)])
        out.extend(_handoff_entry_rows(ordered, extra))

    # ---- 3. coded entries with no local diagnostic-code result -------------------------------
    out.append("")
    out.append("=" * 78)
    out.append("3. OTHER DIAGNOSES AND FINDINGS WITH NO LOCAL VA DC MATCH")
    out.append("=" * 78)
    out.append("The local research reference has no VA diagnostic-code result for the entries")
    out.append("below. That does not determine whether an entry matters to a claim. It is said")
    out.append("once here rather than repeated beside every row.")
    out.append("")
    out.append("Each entry keeps its own code system and type. They are not all diagnoses: the")
    out.append("TYPE column says what each one is, and only a disorder is called a diagnosis.")
    if not other:
        out.append("")
        out.append("   (none)")
    else:
        out.append("")
        out.extend(_handoff_entry_rows(_handoff_sort(other),
                                       ("TYPE / REASON", lambda e: _handoff_kind(e))))

    # ---- 4. administrative, procedural, and entries to verify --------------------------------
    out.append("")
    out.append("=" * 78)
    out.append("4. ADMINISTRATIVE, PROCEDURAL, AND ENTRIES TO VERIFY")
    out.append("=" * 78)
    out.append("These are NOT presented as diagnoses. Nothing here was deleted: every one of")
    out.append("them appears again, in full, in the source-page appendix below.")
    for title, key, note in [
            ("ADMINISTRATIVE OR STATUS CODES", HANDOFF_ADMIN,
             "ICD-10 Z-codes: encounters, screening, status, immunization, long-term therapy."),
            ("PROCEDURES AND EVENTS", HANDOFF_PROCEDURE,
             "SNOMED CT concepts the licensed release types as something other than a "
             "disorder."),
            ("NAMED ENTRIES TO VERIFY", HANDOFF_VERIFY,
             "The record named these without enough to establish what they are.")]:
        rows = by_section.get(key, [])
        out.append("")
        out.append(title)
        out.append(f"   {note}")
        if not rows:
            out.append("")
            out.append("   (none)")
            continue
        out.append("")
        out.extend(_handoff_entry_rows(_handoff_sort(rows),
                                       ("TYPE / REASON", lambda e: _handoff_kind(e))))

    # ---- 5. the detailed source-page appendix ------------------------------------------------
    out.append("")
    out.append("=" * 78)
    out.append("5. DETAILED SOURCE-PAGE INDEX")
    out.append("=" * 78)
    out.append("The complete evidence trail, in the order the records were read. Every retained")
    out.append("entry above appears here exactly once, with its page and its full research")
    out.append("status. An entry with NO 'Potential VA DC' line is one the local reference")
    out.append("simply has no row for. That is said here once instead of under every entry.")
    for n, f in enumerate(run["files"]):
        alias = f"F{n + 1}"
        out.append("")
        out.append("-" * 78)
        out.append(f"{alias} - {f['path']}")
        if f["error"]:
            out.append(f"   could not read: {f['error']}")
            continue
        rows = [e for e in entries if e["file"] is f]
        if not rows:
            out.append("   (no ICD-10 codes or named diagnoses found)")
            continue
        current = object()
        for e in rows:
            group = e["loc"] if f["grain"] == "page" else "(whole file)"
            if group != current:
                current = group
                head = f"--- {group}"
                if e["docpage"]:
                    head += f"   |   document page {e['docpage']}"
                head += " ---"
                out.append("")
                out.append(head)
                if e["reviewer"] or e["reviewed"]:
                    out.append(f"{'Provider/reviewer':<{HANDOFF_FIELD_WIDTH}} : "
                               f"{e['reviewer'] or '(name not given)'}"
                               + (f"  (reviewed {e['reviewed']})" if e["reviewed"] else ""))
            out.append("")
            desc = e["desc"] + (f"  ({e['repeat']})" if e["repeat"] else "")
            out.append(f"{e['label']:<{HANDOFF_FIELD_WIDTH}} : {desc}"
                       + ("  (OCR - confirm against the source)" if e["ocr"] else ""))
            if e["sys"] == "icd10":
                code_line = f"{e['code']} (ICD-10-CM)"
            elif e["csys"]:
                code_line = f"{e['code']} [{e['csys']}] - NOT an ICD-10 code"
            else:
                code_line = "(named in the record with no code)"
            out.append(f"{'Code':<{HANDOFF_FIELD_WIDTH}} : {code_line}")
            out.append(f"{'Where':<{HANDOFF_FIELD_WIDTH}} : {e['source']}")
            date = e["date"] or "(none found near this entry)"
            out.append(f"{'Date':<{HANDOFF_FIELD_WIDTH}} : {date}*" if e["date"]
                       else f"{'Date':<{HANDOFF_FIELD_WIDTH}} : {date}")
            if e["reason"]:
                out.append(f"{'Note':<{HANDOFF_FIELD_WIDTH}} : {e['reason']}")
            # The ordinary refusal is the convention stated at the head of this appendix, not a
            # line repeated under every entry that has it. Every other status still prints.
            out.extend(ln for ln in e["facts"]["lines"]
                       if not ln.endswith(f": {HANDOFF_ORDINARY_NO_MATCH}"))

    out.append("")
    out.append("=" * 78)
    out.append(f"{len(entries)} clinical record entry reference(s) across "
               f"{len(run['files'])} file(s), organized")
    out.append("from the report named above. Nothing here is advice about what to claim.")
    return "\n".join(out) + "\n"

def _handoff_kind(entry):
    """The short TYPE / REASON cell: what the entry is, or why it needs a look."""
    if entry["reason"]:
        return entry["reason"]
    status = entry["facts"]["status"]
    if status == HANDOFF_ORDINARY_NO_MATCH:
        status = ""                      # said once, at the head of the section
    if entry["csys"].startswith("SNOMED"):
        kind = f"SNOMED {entry['label'].lower()}"
        why = re.sub(r'^none - ', '', status)
        return f"{kind}, {why}" if why else kind
    if entry["csys"].startswith("ICD-9"):
        return f"ICD-9-CM, {status}" if status else "ICD-9-CM"
    if entry["section"] == HANDOFF_ADMIN:
        return "ICD-10 status or encounter code"
    return status or f"ICD-10 {entry['label'].lower()}"


def codes_from_report(report):
    """The bare ICD-10 codes out of a finished report - what the GUI's Copy button puts on the
    clipboard. Returns [] if the report's code block is empty."""
    out, seen_heading = [], False
    for ln in report.splitlines():
        if not seen_heading:
            seen_heading = ln.startswith(CODES_HEADING)
            continue
        ln = ln.strip()
        if not ln or set(ln) == {"-"} or ln.startswith("("):
            continue
        out.append(ln)
    return out


def run_extraction(target, output, no_ocr=False, unique=False, progress=None, should_stop=None):
    """Scan target (file or folder), write the report to output, and return the report text.
    progress(msg) - if given - receives OCR/status lines as the run proceeds.
    should_stop() - if given - is polled between pages and files; when it returns True the scan
    ends early and the report is marked as partial. Raises FileNotFoundError if the code list is
    missing, ValueError if nothing to scan."""
    global _OCR_ENABLED, _PROGRESS, _STOP, LAST_RUN
    _OCR_ENABLED = not no_ocr
    _PROGRESS = progress
    _STOP = should_stop
    LAST_RUN = None                    # cleared first: a failed run must not leave stale results

    base = os.path.dirname(os.path.abspath(sys.argv[0]))
    codes = load_codes(base)                       # FileNotFoundError bubbles up to the caller

    files = gather(target, output)
    if not files:
        raise ValueError("No supported files (.pdf .docx .txt .htm .html .xml) to scan. "
                         "(Reports written by this tool are skipped.)")

    total = 0
    stopped = False
    captured = []                      # per-file structured results, for the claim handoff
    icd10_codes = []                               # every distinct ICD-10 code, for the paste block
    # Every distinct code the RECORD ITSELF labeled ICD-9-CM, for the bridge. Kept apart from
    # icd10_codes on purpose: these must never reach the paste block, which feeds an outside
    # ICD-10 lookup that would answer for them confidently and wrongly.
    icd9_codes = []
    # The same, for codes the record labeled SNOMED CT. Kept in its own list for the same
    # reason, and because the two lanes cross their codes through different maps under
    # different rules.
    snomed_codes = []

    # Loaded ONCE, on the first SNOMED row that needs it, and reused for the rest of the run.
    # It has to be available while the diagnosis rows are still being written, because those
    # rows carry the concept's TYPE - `[SNOMED CT procedure]` - and a type worked out after the
    # rows were printed would arrive too late to appear on them. A records set with no SNOMED
    # coding never touches it, which is what the laziness is for: the map is ~48 MB once
    # decompressed and nobody should pay for it to be told there was nothing to look up.
    _snomed_box = []

    def get_snomed():
        if not _snomed_box:
            _snomed_box.append(load_snomed_reference(base))
        return _snomed_box[0]

    lines = []
    lines.append(REPORT_BANNER)
    lines.append(f"Scanned: {target}")
    lines.append(f"When:    {datetime.datetime.now():%Y-%m-%d %H:%M}")
    lines.append("Date column is APPROXIMATE (nearest date to the code).")
    lines.append('CODE "-" means a diagnosis was named in the record with no code at all.')
    lines.append('A code tagged [ICD-9-CM] or [SNOMED CT] is coded under that system, NOT ICD-10.')
    lines.append("=" * 78)

    ocr_needed = []
    for path in files:
        if _stopped():
            stopped = True
            break
        _emit(f"Reading {os.path.basename(path)} ...")
        res = process_file(path, codes)
        if res is None:
            continue
        if res["meta"].get("stopped"):
            stopped = True
        lines.append("")
        lines.append(path)
        file_data = {"path": path, "error": res["error"], "rows": [],
                     "grain": res["meta"].get("grain", "file"),
                     "pages": res["meta"].get("pages", {}),
                     "default": res["meta"].get("default") or {}}
        captured.append(file_data)
        if res["error"]:
            lines.append(f"   could not read: {res['error']}")
            continue
        ocr_pages = res["meta"].get("ocr_pages")
        ocr_read = res["meta"].get("ocr_read")
        if ocr_pages:
            lines.append(f"   NOTE: {ocr_pages} scanned page(s) skipped (--no-ocr). "
                         f"Run without --no-ocr to read them.")
            ocr_needed.append(path)
        elif ocr_read:
            lines.append(f"   NOTE: {ocr_read} scanned page(s) read by OCR - rows marked (OCR) "
                         f"may contain read errors; confirm against the source.")
        rows = res["rows"]
        if unique:
            # One line per distinct diagnosis in this file: by code where there is one, else by
            # name. The first occurrence is kept (its page/date), and how many times it appeared
            # is shown, so nothing is silently hidden.
            counts, order, keep = {}, [], {}
            for r in rows:
                k = r["code"] if r["code"] != "-" else "name:" + r["desc"].lower()
                if k not in keep:
                    keep[k] = r
                    order.append(k)
                counts[k] = counts.get(k, 0) + 1
            rows = []
            for k in order:
                r = dict(keep[k])
                if counts[k] > 1:
                    r["desc"] = f"{r['desc']}  (x{counts[k]})"
                rows.append(r)
        file_data["rows"] = rows
        if not rows:
            lines.append("   (no ICD-10 codes or named diagnoses found)")
            continue
        # The page numbers and the review stamp go in a HEADING, stated once, instead of
        # repeating on every row - they are how a record gets cited, so they are kept, but they
        # are not findings and should not sit in the findings list.
        # A PDF groups per page and the page is named in the heading. A text file has no pages,
        # so it gets one heading and keeps a WHERE column for the line numbers.
        grain = res["meta"].get("grain", "file")
        page_meta = res["meta"].get("pages", {})
        default_meta = res["meta"].get("default") or {}
        current = object()                       # sentinel: the first row always opens a group
        for r in rows:
            group = r["loc"] if grain == "page" else "(whole file)"
            if group != current:
                current = group
                info = page_meta.get(r["loc"]) or default_meta
                head = f"   {group}" if grain == "page" else "   clinical entries found"
                if info.get("docpage"):
                    head += f"   |   document page {info['docpage']}"
                lines.append("")
                lines.append(head)
                if info.get("reviewed") or info.get("reviewer"):
                    stamp = ("   Reviewed " + info.get("reviewed", "")).rstrip()
                    if info.get("reviewer"):
                        stamp += f" by {info['reviewer']}"
                    lines.append(stamp)
                if grain == "page":
                    lines.append(f"      {'CODE':<12} {'DATE':<12} DESCRIPTION")
                else:
                    lines.append(f"      {'WHERE':<10} {'CODE':<12} {'DATE':<12} DESCRIPTION")
                lines.append("      " + "-" * 70)
            tag = "  (OCR)" if r.get("ocr") else ""
            where = "" if grain == "page" else f"{r['loc']:<10} "
            desc = r["desc"]
            # The row keeps the record's own description and its own code; only the SYSTEM tag
            # grows a word, so a reader sees at a glance that 80146002 is a procedure. The
            # stored row is left alone - `csys` stays the lane name, and the claim handoff
            # builds its own label from the type index rather than re-reading display text.
            if str(r.get("csys", "")).startswith("SNOMED"):
                desc = desc.replace("[SNOMED CT]",
                                    f"[{snomed_row_tag(get_snomed(), r['code'])}]", 1)
            lines.append(f"      {where}{r['code']:<12} {(r['date'] or '-'):<12} {desc}{tag}")
            total += 1
            if r.get("sys") == "icd10" and r["code"] not in icd10_codes:
                icd10_codes.append(r["code"])
            # Only a row the record LABELED may be crossed, and each label goes to its own map:
            # ICD-9 through the GEM, SNOMED CT through the licensed US map. An unlabeled number
            # reaches neither, which is the whole point of keeping `csys` as its own field.
            elif str(r.get("csys", "")).startswith("ICD-9") and r["code"] not in icd9_codes:
                icd9_codes.append(r["code"])
            elif str(r.get("csys", "")).startswith("SNOMED") and r["code"] not in snomed_codes:
                snomed_codes.append(r["code"])

    lines.append("")
    lines.append("=" * 78)
    # NOT "diagnosis line(s)". This total counts every row the run produced, and those rows are
    # a mix: ICD-10 diagnoses, diagnoses named with no code, and SNOMED concepts that may be
    # procedures, events or findings. Calling the whole mixed pile diagnoses is the same error
    # the SNOMED type work exists to fix, one line further down the page.
    lines.append(f"{total} clinical record entry line(s) across {len(files)} file(s).")
    if stopped:
        lines.append("STOPPED EARLY - partial results, NOT a complete scan of the target.")
    if ocr_needed:
        lines.append(f"{len(ocr_needed)} file(s) have scanned/image pages that need OCR:")
        for p in ocr_needed:
            lines.append(f"   - {p}")

    # The cross-reference sits ABOVE the paste block, never below it: codes_from_report() reads
    # from the CODES_HEADING line to the end of the file, so anything added underneath would be
    # handed to whoever pressed Copy as if it were an ICD-10 code.
    final_codes = consolidate_codes(icd10_codes)
    ref = load_dc_reference(base) if (final_codes or icd9_codes or snomed_codes) else None
    gem = load_gem_reference(base) if icd9_codes else None
    # Already loaded by the row loop if there was a SNOMED row to type; this reuses that one
    # load rather than reading and re-validating the same 48 MB a second time.
    snomed = get_snomed() if snomed_codes else None
    LAST_RUN = {
        "target": target, "output": output,
        "when": f"{datetime.datetime.now():%Y-%m-%d %H:%M}",
        "stopped": stopped, "unique": unique,
        "files": captured, "ref": ref, "gem": gem, "snomed": snomed,
    }
    lines.extend(dc_section(sorted(final_codes), ref,
                            f" ({DC_REF_NAME} is missing, damaged, or not vouched for by "
                            f"{DC_MANIFEST_NAME})",
                            icd9_found=sorted(icd9_codes), gem=gem,
                            gem_missing_reason=f" ({GEM_REF_NAME} is missing, damaged, or not "
                                               f"vouched for by {GEM_MANIFEST_NAME})",
                            snomed_found=sorted(snomed_codes), snomed=snomed,
                            snomed_missing_reason=f" ({SNOMED_REF_NAME} is missing, damaged, or "
                                                  f"not vouched for by "
                                                  f"{SNOMED_MANIFEST_NAME})"))
    if snomed_codes:
        lines.extend(snomed_terminology_section(
            sorted(snomed_codes), snomed,
            f" ({SNOMED_REF_NAME} is missing, damaged, or not vouched for by "
            f"{SNOMED_MANIFEST_NAME})"))

    # The bare code list goes LAST so it is easy to select, and so codes_from_report can simply
    # read to the end of the file.
    lines.append("")
    lines.append(f"{CODES_HEADING}  (paste into {CODES_URL})")
    lines.append("-" * 78)
    if final_codes:
        lines.extend(sorted(final_codes))
    else:
        lines.append("(none found - every diagnosis above was named without an ICD-10 code, "
                     "or coded under another system)")

    report = "\n".join(lines)
    with open(output, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    _emit(f"Done - {total} clinical record entry line(s). Report saved.")
    return report


def terms_accepted():
    """True if THIS version's terms have been accepted on this computer.

    The window is the only place the terms can be agreed to - a tick-box is a clearer record of
    consent than anything a console prompt could capture, and it keeps one acceptance file as the
    single source of truth. So the command line only ever CHECKS; it never asks. Anything that
    goes wrong here answers False, so the failure is always "refuse to run", never "run anyway".
    """
    try:
        import icd_disclaimer
        return icd_disclaimer.accepted_version(icd_disclaimer.acceptance_path()) == VERSION
    except Exception:
        return False


TERMS_MESSAGE = """\
The terms of use have not been accepted on this computer.

Before this command-line version will run, the terms have to be accepted once,
in the window version:

   1. Double-click  icd_extract_gui.exe
   2. Read the terms
   3. Tick the box and click Accept

After that, this command-line version will work and will not ask again."""


def main():
    ap = argparse.ArgumentParser(description="Extract ICD-10 diagnosis codes from documents.")
    ap.add_argument("target", nargs="?", help="a file or a folder to scan")
    ap.add_argument("-o", "--output", default="icd10_report.txt", help="report file to write")
    ap.add_argument("--no-ocr", action="store_true",
                    help="skip scanned/image pages instead of reading them with OCR (faster)")
    ap.add_argument("--unique", action="store_true",
                    help="collapse repeats of the same diagnosis within a file to one line")
    ap.add_argument("--gui", action="store_true", help="open the graphical window")
    args = ap.parse_args()

    # No target, or --gui: open the window instead of the command line. The window runs its own
    # acceptance gate, so it is not checked here.
    if args.gui or not args.target:
        import icd_gui
        icd_gui.launch()
        return

    if not terms_accepted():
        print(f"ERROR: {TERMS_MESSAGE}", file=sys.stderr)
        sys.exit(3)

    try:
        report = run_extraction(args.target, args.output, args.no_ocr, args.unique)
    except FileNotFoundError:
        print("ERROR: icd10_data.tsv not found next to the program.", file=sys.stderr)
        sys.exit(2)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(report)
    print(f"\nWritten to {args.output}")


if __name__ == "__main__":
    main()
