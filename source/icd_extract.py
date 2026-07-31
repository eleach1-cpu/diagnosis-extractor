"""
ICD-10 diagnosis extractor  (v1)

Point it at a file or a folder. It reads PDF, Word (.docx), text (.txt) and
HTML (.htm/.html), finds every valid ICD-10-CM diagnosis code, and writes a
plain-text report:  file | page/line | code | description | nearest date.

- Codes are VALIDATED against the official CDC ICD-10-CM code set (icd10_data.tsv
  sits next to this program), so nursing-plan codes, form artifacts and stray
  letter-number tokens are discarded. The description printed is the OFFICIAL one,
  so it does not depend on the document's layout.
- Diagnoses coded under ANOTHER system (ICD-9-CM, SNOMED CT) are reported too, tagged
  with that system, so a problem list that predates ICD-10 is not silently dropped.
- Page numbers are real for PDF. Word/Text/HTML have no pages, so a line (or
  paragraph) number is given instead.
- The date is the nearest one found near the code and is APPROXIMATE - with
  free-form documents there is no reliable way to bind a date to a code.

No network. No OCR (scanned/image-only PDFs yield no text). No old .doc.

Usage:
    icd_extract  <file-or-folder>  [-o report.txt]
"""

import sys, os, re, argparse, datetime

VERSION = "2.0"

SUPPORTED = (".pdf", ".docx", ".txt", ".htm", ".html")

# First line of every report this tool writes. It is also how a later run recognises its own
# output and leaves it out of the scan.
REPORT_BANNER = "ICD-10 diagnosis codes extracted"

# The bare-code block at the foot of the report, for pasting into an outside lookup tool.
CODES_HEADING = "ICD-10 CODES"
CODES_URL = "https://ratemyvso.net/dc/icd-codes"

# ---- ICD-10 code shape -----------------------------------------------------
# Letter (not U), a digit, then a digit/A/B; optionally a dot and 1-4 more.
CODE_RE = re.compile(r'\b([A-TV-Z][0-9][0-9AB])(?:\.([0-9A-TV-Z]{1,4}))?\b')

# ---- date shapes (approximate binding) -------------------------------------
DATE_RES = [
    re.compile(r'\b(\d{1,2}/\d{1,2}/\d{2,4})\b'),
    re.compile(r'\b(\d{4}-\d{2}-\d{2})\b'),
    re.compile(r'\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b', re.I),
]


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
    dates = all_dates(text)
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
        # From OCR, a bare 3-character category code (no decimal) is the most common false
        # positive - stray characters on a chart happen to spell a valid category (F20, S31...).
        # Require a decimal for OCR'd codes; specific codes are almost never fabricated by OCR.
        if ocr and sub is None:
            continue
        # A BARE (no-decimal) code is the big source of false positives: device models
        # ("VALLEYLAB / F10 / ..."), IDs, nursing-goal codes, a stray "B12". A real bare
        # diagnosis is written next to its NAME, so require the description to appear nearby.
        # Decimal codes are specific enough to trust on their own (they may sit in a table cell
        # away from their text).
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
            "date": nearest_date(dates, m.start()),
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
ROW_META = re.compile(r'^(?:confirmed|active|inactive|resolved|chronic|acute|principal|primary|'
                      r'secondary|final|working|suspected|probable|ruled\s*out)\.?$', re.I)


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
            results.append({
                "loc": loc,
                "code": m.group(2),
                "desc": f"{name}  [{_sys_name(m.group(1))}]",
                "date": "",
                "ocr": ocr,
                "sys": "other",
            })


# ---- provenance ------------------------------------------------------------
# Records carry their OWN page numbering ("Page 437 of 1,019") and a review stamp naming the
# clinician and when they signed off. Neither is a diagnosis, but both are how you cite a record,
# so they are lifted out and shown as a heading over the diagnoses they belong to - never dropped,
# and never reported as findings in their own right.
META_PAGE = re.compile(r'\bPage\s+([\d,]+)\s+of\s+([\d,]+)\b', re.I)
META_REVIEWED = re.compile(r'\bLast\s+Reviewed\s+Date\s*:\s*(.+)$', re.I)
META_SKIP = re.compile(r'^\s*(?:Page\s+[\d,]+\s+of\s+[\d,]+\s*$|Last\s+Reviewed\s+Date\s*:)', re.I)


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


READERS = {
    ".pdf": read_pdf, ".docx": read_docx,
    ".txt": read_text, ".htm": read_html, ".html": read_html,
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
    global _OCR_ENABLED, _PROGRESS, _STOP
    _OCR_ENABLED = not no_ocr
    _PROGRESS = progress
    _STOP = should_stop

    base = os.path.dirname(os.path.abspath(sys.argv[0]))
    codes = load_codes(base)                       # FileNotFoundError bubbles up to the caller

    files = gather(target, output)
    if not files:
        raise ValueError("No supported files (.pdf .docx .txt .htm .html) to scan. "
                         "(Reports written by this tool are skipped.)")

    total = 0
    stopped = False
    icd10_codes = []                               # every distinct ICD-10 code, for the paste block
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
                head = f"   {group}" if grain == "page" else "   diagnoses found"
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
            lines.append(f"      {where}{r['code']:<12} {(r['date'] or '-'):<12} {r['desc']}{tag}")
            total += 1
            if r.get("sys") == "icd10" and r["code"] not in icd10_codes:
                icd10_codes.append(r["code"])

    lines.append("")
    lines.append("=" * 78)
    lines.append(f"{total} diagnosis line(s) across {len(files)} file(s).")
    if stopped:
        lines.append("STOPPED EARLY - partial results, NOT a complete scan of the target.")
    if ocr_needed:
        lines.append(f"{len(ocr_needed)} file(s) have scanned/image pages that need OCR:")
        for p in ocr_needed:
            lines.append(f"   - {p}")

    # The bare code list goes LAST so it is easy to select, and so codes_from_report can simply
    # read to the end of the file.
    lines.append("")
    lines.append(f"{CODES_HEADING}  (paste into {CODES_URL})")
    lines.append("-" * 78)
    if icd10_codes:
        lines.extend(sorted(icd10_codes))
    else:
        lines.append("(none found - every diagnosis above was named without an ICD-10 code, "
                     "or coded under another system)")

    report = "\n".join(lines)
    with open(output, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    _emit(f"Done - {total} diagnosis line(s). Report saved.")
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
