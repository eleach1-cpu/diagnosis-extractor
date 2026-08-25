"""Build the local SNOMED CT to ICD-10-CM map from the licensed US Edition release.

    python build_snomed_icd10cm_reference.py --zip <path to the US Edition release ZIP> \
                                             --out <folder to write into>

or, when the two RF2 files have already been extracted somewhere:

    python build_snomed_icd10cm_reference.py --rf2 <path to der2_iisssccRefset_Extended...txt> \
                                             --desc <path to sct2_Description_Snapshot-en_...txt> \
                                             --out <folder to write into>

Writes two files into --out:

    snomed_icd10cm_reference.json.gz       the data the extractor reads, gzip, read in place
    snomed_icd10cm_reference.manifest.json the detached build record, with the artifact's SHA-256

WHAT THIS IS, AND WHAT IT IS NOT
    The US Edition's `ICD-10-CM complex map reference set` is a licensed terminology map from
    SNOMED CT concepts to ICD-10-CM. It is NOT an official VA crosswalk, not a rating, not an
    entitlement decision and not advice about what to claim. Reading a SNOMED code's ICD-10-CM
    target and then looking that target up in the Potential-DC reference produces a RESEARCH
    CROSS-REFERENCE and nothing more.

    Nothing here changes which diagnoses the extractor finds. A mapped ICD-10-CM target is
    never substituted for the SNOMED code in a diagnosis row, and never enters the paste block.

WHICH REFERENCE SET, AND WHY IT MATTERS
    Only refset 6011000124106, the US `ICD-10-CM complex map reference set`, is read. The same
    file also carries 447562003, the INTERNATIONAL SNOMED CT to ICD-10 map, whose targets are
    ICD-10 and not ICD-10-CM. Mixing the two would put non-ICD-10-CM codes into a lane that
    ends at an ICD-10-CM lookup, so the international refset is excluded and its exclusion is
    counted in the manifest rather than left implicit.

    Only `active=1` rows are read. An inactive row is a retired version of a map, and the
    Snapshot file carries both.

A SNOMED CODE IS NOT AUTOMATICALLY A DIAGNOSIS
    SNOMED CT codes disorders, but it also codes findings, events, situations, procedures,
    substances, organisms, specimens and much else. `840546002` is `Exposure to severe acute
    respiratory syndrome coronavirus 2 (event)`: it maps directly and unconditionally to
    Z20.822, and it is still not a diagnosis of COVID-19. A direct map says the TERMINOLOGY
    translation needed no patient context. It never says the record confirmed a disease.

    So the artifact carries a second index: every ACTIVE concept in the release paired with the
    semantic tag of its fully specified name, read from

        Snapshot/Terminology/sct2_Description_Snapshot-en_<ext>_<date>.txt

    It covers ALL active concepts, not only the ones in the map, because a procedure that has
    no map row at all still has to be reported as a procedure rather than as an unexplained
    number. Only the TAG is stored, never the fully specified name: the description shown to a
    reader stays the one the record itself wrote.

    THE TAG IS THE TRAILING PARENTHESIS AT DEPTH ONE and never contains a parenthesis itself -
    see icd_extract.semantic_tag, which owns the rule so the builder and the reader cannot
    disagree about it. The release still carries legacy terms like `Osteotomy first metatarsal
    base (& for hallux valgus (& Golden))`, whose trailing group closes inside another and is a
    fragment rather than a type; those come back empty. Others, such as `... (flat foot NOS)`,
    put a bracket-free fragment in tag position that no structural rule can distinguish from a
    tag SNOMED might legitimately add next year, so they are stored as written.

    NONE OF THAT CAN PROMOTE ANYTHING TO A DIAGNOSIS, because eligibility is fail-closed on an
    exact match to `disorder` and no malformed term produces that word by accident. A concept
    whose tag cannot be read is stored with an EMPTY tag, counted in the receipt and printed as
    a build warning; the reader then calls it a clinical entry of unavailable type and refuses
    it a diagnostic code. Measured on the March 2026 US Edition, every one of the 139,598
    mapped concepts carries a clean tag - disorder 93,370, finding 37,255, situation 5,584,
    event 3,389 - so the untyped and malformed terms all sit outside the map entirely, and the
    build fails rather than ships if a future release stops being able to type its own map.

THE MAP IS NOT A FLAT TABLE
    A source concept can carry several map GROUPS (all of which apply, together) and several
    PRIORITIES inside one group (of which exactly one applies, chosen by a rule that needs
    patient context). Every field that decides this is preserved verbatim - mapGroup,
    mapPriority, mapRule, mapAdvice, mapTarget, correlationId and mapCategoryId - so the
    reader classifies from the source's own words rather than from a summary made here.

    THE TRAILING `?` IS A PLACEHOLDER, NOT PART OF A CODE. 55,317 active US rows carry a target
    like `T43.595?`, where the `?` stands for a 7th character the map cannot supply (initial
    encounter, subsequent encounter, sequela). Every one of those rows also carries the advice
    `EPISODE OF CARE INFORMATION NEEDED`, a 1:1 correspondence measured on this release. The
    `?` is preserved exactly as written. It is never stripped: stripping it would turn an
    admittedly incomplete code into one that looks finished, and the reader would then look
    that finished-looking code up and print a confident diagnostic code for a mapping the
    source explicitly declined to complete.

DETERMINISM
    The artifact is byte-identical for identical inputs, compressed included. `--generated` is
    an input, not a clock read, so a rebuild can be reproduced exactly; it defaults to today in
    UTC. The gzip member is written with a fixed modification time and no stored filename, so
    the same JSON always compresses to the same bytes regardless of when or where it is built.

This never fetches, scrapes, infers or repairs a clinical mapping. It only reshapes the file it
is given, and fails loudly rather than guessing.
"""

import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import icd_extract as X                                          # noqa: E402

SCHEMA = X.SNOMED_REF_SCHEMA
ARTIFACT_NAME = X.SNOMED_REF_NAME
MANIFEST_NAME = X.SNOMED_MANIFEST_NAME

# The refset ids, the row field order, the state vocabulary and the classifier itself all come
# from the extractor, so the builder and the reader cannot disagree about them. A rule defined
# twice is a rule that will eventually be changed once.
US_REFSET = X.SNOMED_REFSET_ID
INTL_REFSET = X.SNOMED_INTL_REFSET_ID
ROW_FIELDS = X.SNOMED_ROW_FIELDS
STATES = X.SNOMED_STATES
KEY_RE = X.SNOMED_KEY_RE

# The RF2 ExtendedMap columns, in the order the release writes them. The header is checked
# against this exactly: a column added, removed or reordered upstream would otherwise be read
# by position and quietly shift every field by one.
COLUMNS = ("id", "effectiveTime", "active", "moduleId", "refsetId", "referencedComponentId",
           "mapGroup", "mapPriority", "mapRule", "mapAdvice", "mapTarget", "correlationId",
           "mapCategoryId")
C = {name: i for i, name in enumerate(COLUMNS)}

# The RF2 Description columns, same discipline: checked exactly, never read by position alone.
DESC_COLUMNS = ("id", "effectiveTime", "active", "moduleId", "conceptId", "languageCode",
                "typeId", "term", "caseSignificanceId")
D = {name: i for i, name in enumerate(DESC_COLUMNS)}

# `Fully specified name`. The Description file also carries synonyms, which have no semantic
# tag and would otherwise overwrite a concept's real type with an untagged term.
FSN_TYPE_ID = X.SNOMED_FSN_TYPE_ID

# The RF2 entries inside the release ZIP, and the shape of their names. The release date is
# taken from each name and cross-checked against the package's own effectiveTime and against
# the other file, so a file renamed by hand cannot quietly relabel the vintage of the data.
RF2_ENTRY_RE = re.compile(
    r'(?:^|/)Snapshot/Refset/Map/der2_iisssccRefset_ExtendedMapSnapshot_'
    r'(?P<ext>[A-Za-z0-9]+)_(?P<date>\d{8})\.txt$')
DESC_ENTRY_RE = re.compile(
    r'(?:^|/)Snapshot/Terminology/sct2_Description_Snapshot-en_'
    r'(?P<ext>[A-Za-z0-9]+)_(?P<date>\d{8})\.txt$')
PACKAGE_INFO_RE = re.compile(r'(?:^|/)release_package_information\.json$')

# An ICD-10-CM target AS THE SOURCE WRITES IT: dotted, and possibly carrying the trailing `?`
# placeholder for a 7th character the map cannot supply. Deliberately NOT the extractor's own
# CODE_KEY_RE, which excludes the letter U on purpose so extraction never lifts a U-code out of
# free text. Three real targets in this release are U07.0, U07.1 and U09.9, and rejecting the
# source's own data because the extraction-side pattern is narrower would fail a build over a
# file that is perfectly well formed.
TARGET_RE = re.compile(r'^[A-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?\??$')
SCTID_RE = re.compile(r'^[0-9]{6,18}$')

# A semantic tag as this release writes them, checked for shape only. Deliberately permissive -
# the release uses `regime/therapy`, `religion/philosophy`, `SNOMED RT+CTV3`, `record artifact`
# and 150-odd more, and a builder that decided which tags SNOMED is allowed to invent would
# fail on the next release rather than on a real defect. What protects the reader is not this
# pattern but the fail-closed eligibility rule: only the exact word `disorder` opens the
# diagnostic lane, so a tag this pattern lets through can never become a diagnosis by accident.
TAG_RE = X.SNOMED_TAG_RE


class BuildError(Exception):
    """A problem that must stop the build rather than produce a quietly wrong artifact."""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256_stream(fh):
    h = hashlib.sha256()
    for block in iter(lambda: fh.read(1 << 20), b""):
        h.update(block)
    return h.hexdigest()


def _int_field(value, what, lineno):
    if not value.isdigit():
        raise BuildError(f"line {lineno}: {what} is {value!r}, which is not a whole number")
    return int(value)


def read_rows(open_entry, entry_name):
    """Every ACTIVE US-refset row, grouped by source concept, validated on the way in.

    `open_entry` returns a fresh binary stream over the RF2 file. Raises rather than guessing:
    a file this builder does not fully understand must not be reshaped into an artifact that
    looks authoritative.
    """
    rows = {}
    counts = {"lines": 0, "us_active": 0, "us_inactive": 0,
              "intl_excluded": 0, "other_refset_excluded": 0}
    seen_slots = set()
    with open_entry() as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", newline="")
        header = text.readline().rstrip("\r\n").split("\t")
        if tuple(header) != COLUMNS:
            raise BuildError(f"{entry_name}: unexpected RF2 header. Expected {list(COLUMNS)}, "
                             f"got {header}")
        for lineno, line in enumerate(text, start=2):
            line = line.rstrip("\r\n")
            if not line:
                continue
            counts["lines"] += 1
            f = line.split("\t")
            if len(f) != len(COLUMNS):
                raise BuildError(f"line {lineno}: {len(f)} columns, expected {len(COLUMNS)}")
            refset = f[C["refsetId"]]
            active = f[C["active"]]
            if active not in ("0", "1"):
                raise BuildError(f"line {lineno}: active is {active!r}, expected 0 or 1")
            if refset == INTL_REFSET:
                counts["intl_excluded"] += 1
                continue
            if refset != US_REFSET:
                counts["other_refset_excluded"] += 1
                continue
            if active == "0":
                counts["us_inactive"] += 1
                continue
            counts["us_active"] += 1

            concept = f[C["referencedComponentId"]]
            if not KEY_RE.match(concept):
                raise BuildError(f"line {lineno}: referencedComponentId {concept!r} is not a "
                                 f"SNOMED CT concept identifier")
            group = _int_field(f[C["mapGroup"]], "mapGroup", lineno)
            priority = _int_field(f[C["mapPriority"]], "mapPriority", lineno)
            if group < 1 or priority < 1:
                raise BuildError(f"line {lineno}: mapGroup/mapPriority must be 1 or more, "
                                 f"got {group}/{priority}")
            rule = f[C["mapRule"]]
            advice = f[C["mapAdvice"]]
            target = f[C["mapTarget"]]
            if not rule:
                raise BuildError(f"line {lineno}: mapRule is empty")
            if target and not TARGET_RE.match(target):
                raise BuildError(f"line {lineno}: mapTarget {target!r} is not an ICD-10-CM "
                                 f"code shape")
            correlation = f[C["correlationId"]]
            category = f[C["mapCategoryId"]]
            for name, value in (("correlationId", correlation), ("mapCategoryId", category)):
                if not SCTID_RE.match(value):
                    raise BuildError(f"line {lineno}: {name} {value!r} is not a SNOMED CT "
                                     f"identifier")
            # One concept must not carry two rows for the same group and priority: the pair is
            # what says which single row a group's rule chain lands on, so a duplicate makes
            # the map ambiguous in a way no reader could resolve honestly.
            slot = (concept, group, priority)
            if slot in seen_slots:
                raise BuildError(f"line {lineno}: concept {concept} already has a row for "
                                 f"group {group} priority {priority}")
            seen_slots.add(slot)
            rows.setdefault(concept, []).append(
                [group, priority, rule, advice, target, correlation, category])

    if not rows:
        raise BuildError(f"{entry_name}: no active rows found for refset {US_REFSET}")
    for concept in rows:
        rows[concept].sort(key=lambda r: (r[0], r[1]))
    return rows, counts


def read_types(open_entry, entry_name):
    """Every ACTIVE concept paired with the semantic tag of its fully specified name.

    Returns (types, counts). `types` maps concept id to tag, with an EMPTY tag for a term this
    release writes without one - see the module docstring: those are legacy terms, there are
    six of them, none is in the map, and an empty tag fails closed rather than guessing.

    Raises rather than guessing on anything structural: an unexpected header, a second active
    fully specified name for one concept (which would make the type depend on file order), a
    referenced id that is not a concept identifier, or a tag whose shape this builder cannot
    account for.
    """
    types = {}
    counts = {"lines": 0, "active_fsn": 0, "inactive_fsn": 0, "non_fsn": 0, "untyped": 0}
    untyped = []
    with open_entry() as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", newline="")
        header = text.readline().rstrip("\r\n").split("\t")
        if tuple(header) != DESC_COLUMNS:
            raise BuildError(f"{entry_name}: unexpected RF2 Description header. Expected "
                             f"{list(DESC_COLUMNS)}, got {header}")
        for lineno, line in enumerate(text, start=2):
            line = line.rstrip("\r\n")
            if not line:
                continue
            counts["lines"] += 1
            f = line.split("\t")
            if len(f) != len(DESC_COLUMNS):
                raise BuildError(f"line {lineno}: {len(f)} columns, expected "
                                 f"{len(DESC_COLUMNS)}")
            active = f[D["active"]]
            if active not in ("0", "1"):
                raise BuildError(f"line {lineno}: active is {active!r}, expected 0 or 1")
            if f[D["typeId"]] != FSN_TYPE_ID:
                counts["non_fsn"] += 1
                continue
            if active == "0":
                counts["inactive_fsn"] += 1
                continue
            counts["active_fsn"] += 1
            concept = f[D["conceptId"]]
            if not KEY_RE.match(concept):
                raise BuildError(f"line {lineno}: conceptId {concept!r} is not a SNOMED CT "
                                 f"concept identifier")
            if concept in types:
                raise BuildError(f"line {lineno}: concept {concept} already has an active "
                                 f"fully specified name; its type would depend on file order")
            tag = X.semantic_tag(f[D["term"]])
            if tag and not TAG_RE.match(tag):
                raise BuildError(f"line {lineno}: concept {concept} semantic tag {tag!r} is "
                                 f"not a shape this builder can account for")
            if not tag:
                counts["untyped"] += 1
                if len(untyped) < 20:
                    untyped.append(f"{concept}: {f[D['term']]}")
            types[concept] = tag

    if not types:
        raise BuildError(f"{entry_name}: no active fully specified names found")
    counts["untyped_examples"] = untyped
    return types, counts


def zip_entry(zip_path):
    """(map entry, description entry, release date, package effectiveTime) inside the ZIP.

    Both files must be present and must carry the SAME release date. A map from one release
    typed by the concepts of another would be structurally perfect and quietly wrong: a concept
    retired between the two would be typed from a vintage that no longer describes it.
    """
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if RF2_ENTRY_RE.search(n)]
        if len(names) != 1:
            raise BuildError(f"{zip_path}: expected exactly one Snapshot ExtendedMap entry, "
                             f"found {len(names)}: {names}")
        entry = names[0]
        release = RF2_ENTRY_RE.search(entry).group("date")
        descs = [n for n in z.namelist() if DESC_ENTRY_RE.search(n)]
        if len(descs) != 1:
            raise BuildError(f"{zip_path}: expected exactly one Snapshot Description entry, "
                             f"found {len(descs)}: {descs}")
        desc_entry = descs[0]
        desc_release = DESC_ENTRY_RE.search(desc_entry).group("date")
        if desc_release != release:
            raise BuildError(f"{zip_path}: release date disagreement - the map is {release}, "
                             f"the Description file is {desc_release}")
        package_time = ""
        info = [n for n in z.namelist() if PACKAGE_INFO_RE.search(n)]
        if info:
            try:
                package_time = str(json.loads(z.read(info[0]).decode("utf-8"))
                                   .get("effectiveTime", ""))
            except (ValueError, UnicodeDecodeError) as e:
                raise BuildError(f"{zip_path}: {info[0]} is not readable JSON: {e}")
        # The package says what release it is; the file name says the same thing in another
        # place. Disagreement means the archive was assembled or renamed by hand, and the
        # vintage recorded in the manifest would then be a guess.
        if package_time and package_time != release:
            raise BuildError(f"{zip_path}: release date disagreement - entry name says "
                             f"{release}, release_package_information.json says {package_time}")
        return entry, desc_entry, release, package_time


def build(rows, counts, types, type_counts, source, generated):
    """The artifact, with every count recomputed from the rows rather than carried in."""
    concepts = sorted(rows)
    all_rows = [r for c in concepts for r in rows[c]]
    targets = sorted({r[4] for r in all_rows if r[4]})
    blank_rows = sum(1 for r in all_rows if not r[4])
    placeholder_rows = sum(1 for r in all_rows if r[4].endswith("?"))

    # The STATE COUNTS come from the extractor's own classifier, so this receipt cannot drift
    # from what the program will actually print for the same concept.
    tally = {s: 0 for s in STATES if s != "absent"}
    for concept in concepts:
        state = X.snomed_state(rows[concept])
        if state not in tally:
            raise BuildError(f"concept {concept}: classifier returned unknown state {state!r}")
        tally[state] += 1
    if sum(tally.values()) != len(concepts):
        raise BuildError("state imbalance: the summary does not cover every source concept")

    # The type census, and the one cross-check that matters: a concept the map answers for but
    # the release does not type would be reported as a clinical entry of unknown kind, which is
    # honest but is also a symptom of two files from different vintages. It does not occur in
    # the March 2026 release - all 139,598 mapped concepts are typed - so it is worth saying
    # out loud rather than discovering later in a report.
    type_tally = {}
    for tag in types.values():
        if tag:
            type_tally[tag] = type_tally.get(tag, 0) + 1
    untyped_total = sum(1 for tag in types.values() if not tag)
    mapped_untyped = sorted(c for c in concepts if not types.get(c))

    artifact = {
        "schema": SCHEMA,
        "generated": generated,
        "policy": {
            "refset_id": US_REFSET,
            "refset_name": "ICD-10-CM complex map reference set (SNOMED CT US Edition)",
            "excluded_refset_id": INTL_REFSET,
            "excluded_refset_reason": "447562003 is the INTERNATIONAL SNOMED CT to ICD-10 map. "
                                      "Its targets are ICD-10, not ICD-10-CM, and this lane "
                                      "ends at an ICD-10-CM lookup.",
            "active_only": True,
            "row_fields": list(ROW_FIELDS),
            "states": list(STATES),
            "no_classification_category_id": X.SNOMED_CAT_NO_CLASSIFICATION,
            "placeholder_suffix": "?",
            "placeholder_meaning": "A trailing ? stands for a 7th character the map cannot "
                                   "supply (episode of care). It is preserved verbatim and a "
                                   "target carrying it is never treated as resolved.",
            "resolution_basis": "Each resolved ICD-10-CM target is looked up in the "
                                "Potential-DC reference INDEPENDENTLY. Targets are never "
                                "voted across and different results are never collapsed.",
            "detection_basis": "codes the record itself labels SNOMED CT, or a CDA element "
                               "whose codeSystem is the SNOMED CT OID "
                               f"{X.CDA_SNOMED_OID}; never an unlabeled number",
            "description_type_id": FSN_TYPE_ID,
            "type_basis": "The semantic tag of the concept's ACTIVE fully specified name, read "
                          "from the release's own Description Snapshot. It is the trailing "
                          "parenthesis at depth one. The record's own wording and the "
                          "ICD-10-CM target are never used to decide a concept's type.",
            "diagnosis_tag": X.SNOMED_DIAGNOSIS_TAG,
            "diagnosis_eligibility": "FAIL CLOSED. Only a concept whose licensed semantic tag "
                                     "is exactly 'disorder' may reach the Potential VA "
                                     "Diagnostic Code section. A finding, procedure, event or "
                                     "situation, and any concept whose type cannot be read, is "
                                     "reported as terminology and never as a diagnosis. A "
                                     "direct map means the terminology translation needed no "
                                     "patient context; it never means the record confirmed a "
                                     "disease.",
            "note": "Research cross-reference only. A licensed terminology map is not an "
                    "official VA crosswalk, not a rating, not an entitlement or filing "
                    "decision.",
        },
        "source": source,
        "counts": {
            "source_rows": len(all_rows),
            "source_concepts": len(concepts),
            "distinct_targets": len(targets),
            "blank_target_rows": blank_rows,
            "placeholder_target_rows": placeholder_rows,
            "multi_group_concepts": sum(1 for c in concepts
                                        if len({r[0] for r in rows[c]}) > 1),
            "multi_priority_concepts": sum(1 for c in concepts
                                           if len(rows[c]) > len({r[0] for r in rows[c]})),
            "typed_concepts": len(types) - untyped_total,
            "untyped_concepts": untyped_total,
            "mapped_concepts_without_a_type": len(mapped_untyped),
        },
        "states": tally,
        "types": {c: types[c] for c in sorted(types)},
        "type_counts": type_tally,
        "map": {c: rows[c] for c in concepts},
        "ledger": {
            "explanation": "Every active row of the US refset is preserved, blank targets and "
                           "7th-character placeholders included, because the REASON a source "
                           "concept cannot be classified is part of the answer. Nothing is "
                           "quarantined and nothing is repaired.",
            "excluded_international_refset_rows": counts["intl_excluded"],
            "excluded_inactive_us_rows": counts["us_inactive"],
            "excluded_other_refset_rows": counts["other_refset_excluded"],
            "total_lines_read": counts["lines"],
            "description_lines_read": type_counts["lines"],
            "description_active_fsn": type_counts["active_fsn"],
            "excluded_inactive_fsn": type_counts["inactive_fsn"],
            "excluded_non_fsn_descriptions": type_counts["non_fsn"],
            "untyped_examples": type_counts["untyped_examples"],
        },
    }
    if counts["us_active"] != len(all_rows):
        raise BuildError(f"row imbalance: read {counts['us_active']} active US rows but "
                         f"stored {len(all_rows)}")
    if type_counts["active_fsn"] != len(types):
        raise BuildError(f"type imbalance: read {type_counts['active_fsn']} active fully "
                         f"specified names but stored {len(types)}")
    if sum(type_tally.values()) + untyped_total != len(types):
        raise BuildError("type imbalance: the type census does not cover every concept")
    # THIS ONE FAILS THE BUILD, where an untyped concept outside the map only warns, and the
    # difference is what it would cost silently. A concept the map answers for but the release
    # does not type has its diagnostic lane closed by the fail-closed rule: it would be
    # reported as a clinical entry of unavailable kind, with no diagnostic code, and nothing in
    # the report would say that a file pairing went wrong. It does not occur in the March 2026
    # release - all 139,598 mapped concepts are typed - so if it ever does, the two files came
    # from different vintages or the Description file was cut short, and that is worth stopping
    # for rather than shipping.
    if mapped_untyped:
        raise BuildError(f"{len(mapped_untyped)} concept(s) in the map have no semantic tag, "
                         f"e.g. {mapped_untyped[:5]}. The map and the Description file do not "
                         f"describe the same release, or the Description file is incomplete.")
    return artifact


def dumps(obj):
    """Stable JSON: sorted keys, no incidental whitespace, one trailing newline."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def gzip_bytes(raw):
    """Deterministic gzip: fixed mtime, no stored filename, so identical input gives identical
    bytes no matter when or into which path it is built.

    Both arguments are load-bearing. Without `mtime=0` the header carries the clock, and two
    builds of the same data would differ. Without `filename=""` GzipFile takes the name from
    the file object it is writing to, so the artifact's bytes would depend on the --out path.
    """
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0, filename="") as g:
        g.write(raw)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(
        description="Build the local SNOMED CT to ICD-10-CM map artifact.")
    ap.add_argument("--zip", dest="zip_path",
                    help="the licensed SNOMED CT US Edition release ZIP; the RF2 map is read "
                         "from inside it and never extracted to disk")
    ap.add_argument("--rf2", help="path to an already-extracted "
                                  "der2_iisssccRefset_ExtendedMapSnapshot_*.txt")
    ap.add_argument("--desc", help="path to the matching already-extracted "
                                   "sct2_Description_Snapshot-en_*.txt; required with --rf2, "
                                   "because a map without concept types cannot say whether a "
                                   "code is a diagnosis or a procedure")
    ap.add_argument("--out", required=True, help="folder to write the artifact and manifest into")
    ap.add_argument("--generated", default=datetime.datetime.now(datetime.timezone.utc)
                    .strftime("%Y-%m-%d"),
                    help="generation date recorded in the artifact (default: today, UTC). It is "
                         "an input so a build can be reproduced byte for byte.")
    args = ap.parse_args()

    if bool(args.zip_path) == bool(args.rf2):
        ap.error("give exactly one of --zip or --rf2")
    if args.rf2 and not args.desc:
        ap.error("--rf2 needs --desc as well")
    if args.zip_path and args.desc:
        ap.error("--desc belongs with --rf2; --zip reads both files from inside the ZIP")
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', args.generated):
        print("ERROR: --generated must be YYYY-MM-DD", file=sys.stderr)
        return 2
    path = args.zip_path or args.rf2
    for p in (path, args.desc):
        if p and not os.path.isfile(p):
            print(f"ERROR: not found: {p}", file=sys.stderr)
            return 2

    try:
        if args.zip_path:
            entry, desc_entry, release, package_time = zip_entry(args.zip_path)

            def open_entry():
                return zipfile.ZipFile(args.zip_path).open(entry)

            def open_desc():
                return zipfile.ZipFile(args.zip_path).open(desc_entry)

            with open_entry() as fh:
                entry_sha = sha256_stream(fh)
            with open_desc() as fh:
                desc_sha = sha256_stream(fh)
            with zipfile.ZipFile(args.zip_path) as z:
                entry_bytes = z.getinfo(entry).file_size
                desc_bytes = z.getinfo(desc_entry).file_size
            source = {
                "name": os.path.basename(args.zip_path),
                "sha256": sha256_file(args.zip_path),
                "bytes": os.path.getsize(args.zip_path),
                "entry": entry,
                "entry_sha256": entry_sha,
                "entry_bytes": entry_bytes,
                "description_entry": desc_entry,
                "description_entry_sha256": desc_sha,
                "description_entry_bytes": desc_bytes,
                "description_release_date": release,
                "release_date": release,
                "package_effective_time": package_time,
            }
        else:
            m = RF2_ENTRY_RE.search(os.path.basename(args.rf2).replace(os.sep, "/")) or \
                re.search(r'ExtendedMapSnapshot_[A-Za-z0-9]+_(?P<date>\d{8})\.txt$', args.rf2)
            if not m:
                raise BuildError(f"{args.rf2}: name does not look like "
                                 f"der2_iisssccRefset_ExtendedMapSnapshot_<ext>_<YYYYMMDD>.txt")
            entry = os.path.basename(args.rf2)
            dm = DESC_ENTRY_RE.search(os.path.basename(args.desc).replace(os.sep, "/")) or \
                re.search(r'Description_Snapshot-en_[A-Za-z0-9]+_(?P<date>\d{8})\.txt$',
                          args.desc)
            if not dm:
                raise BuildError(f"{args.desc}: name does not look like "
                                 f"sct2_Description_Snapshot-en_<ext>_<YYYYMMDD>.txt")
            if dm.group("date") != m.group("date"):
                raise BuildError(f"release date disagreement - the map is {m.group('date')}, "
                                 f"the Description file is {dm.group('date')}")
            desc_entry = os.path.basename(args.desc)

            def open_entry():
                return open(args.rf2, "rb")

            def open_desc():
                return open(args.desc, "rb")

            source = {
                "name": entry,
                "sha256": "",
                "bytes": 0,
                "entry": entry,
                "entry_sha256": sha256_file(args.rf2),
                "entry_bytes": os.path.getsize(args.rf2),
                "description_entry": desc_entry,
                "description_entry_sha256": sha256_file(args.desc),
                "description_entry_bytes": os.path.getsize(args.desc),
                "description_release_date": dm.group("date"),
                "release_date": m.group("date"),
                "package_effective_time": "",
            }

        rows, counts = read_rows(open_entry, source["entry"])
        types, type_counts = read_types(open_desc, source["description_entry"])
        artifact = build(rows, counts, types, type_counts, source, args.generated)
    except BuildError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as e:
        print(f"ERROR: could not read {path}: {e}", file=sys.stderr)
        return 1

    raw = dumps(artifact).encode("utf-8")
    blob = gzip_bytes(raw)
    os.makedirs(args.out, exist_ok=True)
    artifact_path = os.path.join(args.out, ARTIFACT_NAME)
    with open(artifact_path, "wb") as f:
        f.write(blob)

    # The artifact's own hash goes in the DETACHED manifest, over the COMPRESSED bytes: the
    # .json.gz is what sits on disk and what the program hashes on load, so that is the thing
    # the witness has to vouch for. Writing a file's hash inside that same file cannot be done.
    manifest = {
        "schema": SCHEMA,
        "artifact": {
            "name": ARTIFACT_NAME,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob),
            "uncompressed_bytes": len(raw),
            "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
        },
        "generated": artifact["generated"],
        "built_at_utc": datetime.datetime.now(datetime.timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": artifact["source"],
        "counts": artifact["counts"],
        "states": artifact["states"],
        "type_counts": artifact["type_counts"],
        "policy": artifact["policy"],
        "ledger_summary": {k: v for k, v in artifact["ledger"].items() if k != "explanation"},
    }
    with open(os.path.join(args.out, MANIFEST_NAME), "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(manifest))

    # Read the artifact back through the PROGRAM'S OWN LOADER before claiming success. A build
    # the extractor would refuse at run time is a failed build, and finding that out here is
    # the difference between a caught mistake and a silently missing report section.
    if X.load_snomed_reference(args.out) is None:
        print(f"ERROR: the extractor's own loader refused the artifact just written to "
              f"{args.out}. Nothing shipped.", file=sys.stderr)
        return 1

    c, s, led = artifact["counts"], artifact["states"], artifact["ledger"]
    ratio = len(raw) / len(blob)
    print(f"{ARTIFACT_NAME}: {len(blob):,} bytes ({len(blob) / 1048576:.2f} MiB)  "
          f"sha256 {manifest['artifact']['sha256'][:16]}...")
    print(f"  uncompressed     {len(raw):,} bytes ({len(raw) / 1048576:.2f} MiB), "
          f"{ratio:.1f}x")
    print(f"  source           {source['name']} release {source['release_date']}")
    print(f"  refset {US_REFSET}  {c['source_rows']:,} active rows, "
          f"{c['source_concepts']:,} concepts, {c['distinct_targets']:,} distinct targets")
    print(f"    direct                {s['direct']:,}")
    print(f"    multi_group           {s['multi_group']:,}")
    print(f"    conditional           {s['conditional']:,}")
    print(f"    no_classification     {s['no_classification']:,}")
    print(f"  blank targets    {c['blank_target_rows']:,} row(s); "
          f"7th-character placeholders {c['placeholder_target_rows']:,} row(s)")
    print(f"  excluded         {led['excluded_international_refset_rows']:,} international "
          f"refset row(s), {led['excluded_inactive_us_rows']:,} inactive US row(s)")

    t = artifact["type_counts"]
    print(f"  concept types    {source['description_entry_bytes']:,} byte Description "
          f"Snapshot, {c['typed_concepts']:,} typed concept(s)")
    for tag in sorted(t, key=lambda k: (-t[k], k))[:6]:
        print(f"    {tag:<21} {t[tag]:,}")
    print(f"    other tags            {len(t) - min(len(t), 6):,} more tag(s)")
    # A warning, on stderr, not a failure. These are legacy terms with no semantic tag; an
    # empty type is reported to the reader as an entry of unavailable kind and can never
    # become a diagnosis. It is printed loudly so a release that starts losing types in bulk
    # is noticed on the build, not in somebody's report months later.
    if c["untyped_concepts"]:
        print(f"  WARNING: {c['untyped_concepts']:,} active concept(s) have no readable "
              f"semantic tag and are stored untyped.", file=sys.stderr)
        for example in led["untyped_examples"]:
            print(f"    {example}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
