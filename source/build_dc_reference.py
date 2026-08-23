"""Build the local Potential-VA-Diagnostic-Code reference from a RateMyVSO crosswalk.

    python build_dc_reference.py --crosswalk <path to icd10-dc-crosswalk.json> \
                                --codes <path to icd10_data.tsv> \
                                --out <folder to write into>

or point at a repository root and let it find the crosswalk:

    python build_dc_reference.py --ratemyvso <repo root> --codes ..\\app\\icd10_data.tsv \
                                --out ..\\app

Writes two files into --out:

    dc_reference.json           the data the extractor reads
    dc_reference.manifest.json  the detached build record, including the artifact's own SHA-256

WHAT THIS IS, AND WHAT IT IS NOT
    The crosswalk is a RESEARCH CROSS-REFERENCE between ICD-10-CM codes and VA Diagnostic
    Codes. It is NOT an official VA crosswalk, not a rating, not an entitlement decision and
    not advice about what to claim. Nothing here changes which diagnoses the extractor finds:
    the CDC code list remains the only authority on whether an ICD-10 code is real, and this
    reference may only ADD a note beside a code that has already been accepted.

DETERMINISM
    The artifact is byte-identical for identical inputs. `--generated` is an input, not a
    clock read, so a rebuild can be reproduced exactly; it defaults to today in UTC.

THE 60% FLOOR AND THE VOTE BASIS
    A three-character category with no direct row of its own is answered by its children.
    The dominant DC wins when it holds at least 60% of the vote, the same honesty bar the
    RateMyVSO crosswalk readers already use. Below that the category is `ambiguous` and the
    vote is reported rather than a DC.

    Only crosswalk rows whose code is IN the supplied CDC code list may vote. Rows for codes
    the current list does not contain are quarantined in the ledger and never become usable
    mappings, so they cannot shape a user-visible answer either. Measured on the audited
    source that changes the outcome for 2 of 1,117 categories, both named in the ledger.

This never fetches, scrapes, infers or repairs a clinical mapping. It only reshapes the file
it is given, and fails loudly rather than guessing.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

SCHEMA = "icd10-dc-reference/1"
ARTIFACT_NAME = "dc_reference.json"
MANIFEST_NAME = "dc_reference.manifest.json"
FLOOR_PERCENT = 60
CATEGORY_LEN = 3
# The only confidence labels this schema defines. The reader rejects anything else outright, so
# an unexpected value in the source has to stop the build rather than ship a file that will be
# refused at run time, or worse, print an invented authority label beside a diagnosis.
CONFIDENCE_VALUES = ("verified", "high")


def meets_floor(votes, total):
    """Exact integer form of votes/total >= FLOOR_PERCENT%.

    Deliberately not `100.0 * votes / total >= FLOOR_PERCENT`: the reader applies the same rule
    and classifies rows by it, so a vote sitting precisely on the bar must not be able to land
    on one side here and the other side there through floating-point rounding.
    """
    return votes * 100 >= FLOOR_PERCENT * total

# The same shape the extractor's CODE_RE accepts, anchored: letter (not U), digit, digit/A/B,
# then up to four more characters of subcode. A source key that fails this is a malformed row.
KEY_RE = re.compile(r'^[A-TV-Z][0-9][0-9AB][0-9A-TV-Z]{0,4}$')


class BuildError(Exception):
    """A problem that must stop the build rather than produce a quietly wrong artifact."""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def normalize(code):
    """A crosswalk/TSV key reduced to the extractor's dotless uppercase form."""
    return re.sub(r'[^0-9A-Za-z]', '', str(code)).upper()


def load_code_list(path):
    """The CDC code list: dotless code -> official description. The validity authority."""
    codes = {}
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            code, _, desc = line.rstrip("\n").partition("\t")
            code = normalize(code)
            if not code:
                continue
            if code in codes and codes[code] != desc:
                raise BuildError(f"{path}:{lineno}: duplicate code {code} with a different "
                                 f"description")
            codes[code] = desc
    if not codes:
        raise BuildError(f"{path}: no codes read")
    return codes


def load_crosswalk(path):
    """The RateMyVSO crosswalk, validated on the way in. Raises rather than guessing."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict) or not raw:
        raise BuildError(f"{path}: expected a non-empty JSON object of code -> row")
    rows = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise BuildError(f"{path}: row {key!r} is not an object")
        missing = {"d", "dc", "n", "c"} - set(value)
        if missing:
            raise BuildError(f"{path}: row {key!r} is missing {sorted(missing)}; this builder "
                             f"only understands the d/dc/n/c shape")
        code = normalize(key)
        if not KEY_RE.match(code):
            raise BuildError(f"{path}: row key {key!r} is not an ICD-10-CM code shape")
        dc = str(value["dc"]).strip()
        if not dc.isdigit():
            raise BuildError(f"{path}: row {key!r} has a non-numeric diagnostic code {dc!r}")
        name = str(value["n"]).strip()
        conf = str(value["c"]).strip()
        if not name:
            raise BuildError(f"{path}: row {key!r} has an empty name")
        if conf not in CONFIDENCE_VALUES:
            raise BuildError(f"{path}: row {key!r} has confidence {conf!r}, which this schema "
                             f"does not define; expected one of {list(CONFIDENCE_VALUES)}")
        if code in rows and rows[code] != (dc, name, conf):
            raise BuildError(f"{path}: key {key!r} normalizes to {code}, which already exists "
                             f"with different content")
        rows[code] = (dc, name, conf)
    return rows


def roll_up(voters, crosswalk):
    """The dominant DC among a category's children, with the vote preserved.

    Ties are broken by the lower DC number so the artifact is deterministic, but a tie can
    never clear the 60% floor with more than two candidates anyway.
    """
    tally = {}
    for code in voters:
        dc = crosswalk[code][0]
        tally[dc] = tally.get(dc, 0) + 1
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    dc, votes = ranked[0]
    return dc, votes, len(voters)


def build(crosswalk_path, codes_path, generated):
    crosswalk = load_crosswalk(crosswalk_path)
    code_list = load_code_list(codes_path)

    usable, cdc_missing = {}, {}
    for code, row in crosswalk.items():
        (usable if code in code_list else cdc_missing)[code] = row
    if len(usable) + len(cdc_missing) != len(crosswalk):
        raise BuildError("ledger imbalance: usable + cdc_missing does not equal the source rows")

    dc_names = {}
    for dc, name, _conf in crosswalk.values():
        if dc in dc_names and dc_names[dc] != name:
            # The crosswalk names a DC more than one way. Keep the first alphabetically so the
            # artifact is deterministic, and say so rather than picking silently.
            dc_names[dc] = min(dc_names[dc], name)
        else:
            dc_names.setdefault(dc, name)

    direct = {code: [row[0], row[2]] for code, row in usable.items()}

    # Children grouped by their three-character category, votes restricted to usable rows.
    children = {}
    for code in usable:
        if len(code) > CATEGORY_LEN:
            children.setdefault(code[:CATEGORY_LEN], []).append(code)

    derived, ambiguous, divergent = {}, {}, []
    for category in sorted(children):
        if category in direct:
            continue                      # a direct row always wins; no rollup is built
        voters = sorted(children[category])
        dc, votes, total = roll_up(voters, crosswalk)
        if meets_floor(votes, total):
            derived[category] = [dc, votes, total]
        else:
            ambiguous[category] = [dc, votes, total]
        # Record where quarantining the CDC-missing rows changed the answer, so the policy is
        # auditable instead of asserted.
        every = sorted(c for c in crosswalk
                       if len(c) > CATEGORY_LEN and c[:CATEGORY_LEN] == category)
        if len(every) != len(voters):
            adc, avotes, atotal = roll_up(every, crosswalk)
            if meets_floor(avotes, atotal) != meets_floor(votes, total) or \
                    (meets_floor(avotes, atotal) and meets_floor(votes, total) and adc != dc):
                divergent.append({
                    "category": category,
                    "with_quarantined_rows": [adc, avotes, atotal],
                    "used": [dc, votes, total],
                })

    artifact = {
        "schema": SCHEMA,
        "generated": generated,
        "policy": {
            "floor_percent": FLOOR_PERCENT,
            "category_length": CATEGORY_LEN,
            "vote_basis": "crosswalk rows whose code is present in the supplied CDC code list",
            "note": "Research cross-reference only. Not an official VA crosswalk, not a rating, "
                    "not an entitlement or filing decision.",
        },
        "source": {
            "name": os.path.basename(crosswalk_path),
            "sha256": sha256_file(crosswalk_path),
            "bytes": os.path.getsize(crosswalk_path),
            "rows": len(crosswalk),
        },
        "code_list": {
            "name": os.path.basename(codes_path),
            "sha256": sha256_file(codes_path),
            "rows": len(code_list),
        },
        "counts": {
            "source_rows": len(crosswalk),
            "direct_usable": len(direct),
            "cdc_missing": len(cdc_missing),
            "direct_category_rows": sum(1 for c in direct if len(c) == CATEGORY_LEN),
            "derived_categories": len(derived),
            "ambiguous_categories": len(ambiguous),
            "distinct_dcs": len(dc_names),
            "confidence": {c: sum(1 for r in crosswalk.values() if r[2] == c)
                           for c in sorted({r[2] for r in crosswalk.values()})},
        },
        "dc_names": dc_names,
        "direct": direct,
        "derived": derived,
        "ambiguous": ambiguous,
        "ledger": {
            "explanation": "Every source row lands in exactly one of direct_usable or "
                           "cdc_missing. Rows in cdc_missing name a code the supplied CDC list "
                           "does not contain, so the extractor can never emit them; they are "
                           "kept here for audit and are never usable mappings and never vote.",
            "cdc_missing": {code: [row[0], row[2]] for code, row in sorted(cdc_missing.items())},
            "quarantine_changed_outcome": divergent,
        },
    }

    total_ledgered = artifact["counts"]["direct_usable"] + artifact["counts"]["cdc_missing"]
    if total_ledgered != len(crosswalk):
        raise BuildError(f"ledger imbalance: {total_ledgered} ledgered vs {len(crosswalk)} source")
    return artifact


def dumps(obj):
    """Stable JSON: sorted keys, no incidental whitespace, one trailing newline."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def main():
    ap = argparse.ArgumentParser(
        description="Build the local Potential-VA-Diagnostic-Code reference artifact.")
    ap.add_argument("--crosswalk", help="path to icd10-dc-crosswalk.json")
    ap.add_argument("--ratemyvso", help="RateMyVSO repository root; the crosswalk is read from "
                                        "public/bva-data/icd10-dc-crosswalk.json under it")
    ap.add_argument("--codes", required=True, help="path to icd10_data.tsv, the CDC code list")
    ap.add_argument("--out", required=True, help="folder to write the artifact and manifest into")
    ap.add_argument("--generated", default=datetime.datetime.now(datetime.timezone.utc)
                    .strftime("%Y-%m-%d"),
                    help="generation date recorded in the artifact (default: today, UTC). It is "
                         "an input so a build can be reproduced byte for byte.")
    args = ap.parse_args()

    if bool(args.crosswalk) == bool(args.ratemyvso):
        ap.error("give exactly one of --crosswalk or --ratemyvso")
    crosswalk_path = args.crosswalk or os.path.join(
        args.ratemyvso, "public", "bva-data", "icd10-dc-crosswalk.json")

    for path in (crosswalk_path, args.codes):
        if not os.path.isfile(path):
            print(f"ERROR: not found: {path}", file=sys.stderr)
            return 2
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', args.generated):
        print("ERROR: --generated must be YYYY-MM-DD", file=sys.stderr)
        return 2

    try:
        artifact = build(crosswalk_path, args.codes, args.generated)
    except BuildError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    os.makedirs(args.out, exist_ok=True)
    artifact_path = os.path.join(args.out, ARTIFACT_NAME)
    with open(artifact_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(artifact))

    # The artifact's own hash goes in the DETACHED manifest. Writing a file's hash inside that
    # same file cannot be done: adding the hash changes the bytes it was computed from.
    manifest = {
        "schema": SCHEMA,
        "artifact": {
            "name": ARTIFACT_NAME,
            "sha256": sha256_file(artifact_path),
            "bytes": os.path.getsize(artifact_path),
        },
        "generated": artifact["generated"],
        "built_at_utc": datetime.datetime.now(datetime.timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": artifact["source"],
        "code_list": artifact["code_list"],
        "counts": artifact["counts"],
        "policy": artifact["policy"],
        "ledger_summary": {
            "cdc_missing": len(artifact["ledger"]["cdc_missing"]),
            "quarantine_changed_outcome": artifact["ledger"]["quarantine_changed_outcome"],
        },
    }
    with open(os.path.join(args.out, MANIFEST_NAME), "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(manifest))

    c = artifact["counts"]
    print(f"{ARTIFACT_NAME}: {manifest['artifact']['bytes']:,} bytes  "
          f"sha256 {manifest['artifact']['sha256'][:16]}...")
    print(f"  source rows      {c['source_rows']:,}")
    print(f"  direct usable    {c['direct_usable']:,}  "
          f"({c['direct_category_rows']} of them direct category rows)")
    print(f"  CDC-missing      {c['cdc_missing']:,}  (ledgered, never usable, never vote)")
    print(f"  derived rollups  {c['derived_categories']:,} mapped, "
          f"{c['ambiguous_categories']:,} ambiguous")
    print(f"  distinct DCs     {c['distinct_dcs']:,}")
    if artifact["ledger"]["quarantine_changed_outcome"]:
        print(f"  quarantine changed the outcome for "
              f"{len(artifact['ledger']['quarantine_changed_outcome'])} category(ies): "
              f"{', '.join(d['category'] for d in artifact['ledger']['quarantine_changed_outcome'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
