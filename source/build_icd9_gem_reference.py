"""Build the local ICD-9-CM bridge from a frozen ICD-9 to ICD-10-CM GEM.

    python build_icd9_gem_reference.py --gem <path to icd9-to-icd10-gem.json> \
                                       --codes <path to icd10_data.tsv> \
                                       --dc-reference <folder holding dc_reference.json> \
                                       --out <folder to write into>

or point at a repository root and let it find the GEM:

    python build_icd9_gem_reference.py --ratemyvso <repo root> --codes ..\\app\\icd10_data.tsv \
                                       --dc-reference ..\\app --out ..\\app

Writes two files into --out:

    icd9_gem_reference.json           the data the extractor reads
    icd9_gem_reference.manifest.json  the detached build record, with the artifact's SHA-256

WHAT THIS IS, AND WHAT IT IS NOT
    A GEM is a General Equivalence Mapping: a frozen conversion table from ICD-9-CM to
    ICD-10-CM. It is NOT an official VA crosswalk, not a rating, not an entitlement decision
    and not advice about what to claim. Bridging an ICD-9 code to ICD-10-CM and then reading
    the Potential-DC reference produces a RESEARCH CROSS-REFERENCE and nothing more.

    Nothing here changes which diagnoses the extractor finds. A bridged ICD-10-CM target is
    never substituted for the ICD-9 code in a diagnosis row, and never enters the paste block.

WHAT BRIDGES, AND WHAT DOES NOT
    Only codes the record itself LABELS as ICD-9: an "ICD-9" or "ICD-9-CM" label in the text,
    or a CDA element whose codeSystem is the ICD-9-CM OID. An unlabeled 250.00 in a record is
    a number, and this file is not permission to treat it as a diagnosis.

DETERMINISM
    The artifact is byte-identical for identical inputs. `--generated` is an input, not a
    clock read, so a rebuild can be reproduced exactly; it defaults to today in UTC.

    The DC reference is an input too. It is read through the extractor's OWN loader and its
    OWN lookup, so the disposition summary recorded here cannot drift from what the program
    will actually print, and a DC artifact the program would refuse stops this build instead
    of being summarised as though it were fine.

THE 60% FLOOR
    One ICD-9 code can bridge to several ICD-10-CM targets. Each target that maps to a
    diagnostic code casts one vote; the dominant DC wins when it holds at least 60% of the
    votes cast, the same bar Chunk 2 uses for a category rollup. Below that the bridge is
    `ambiguous` and the vote is reported rather than a DC. Targets that map to nothing do not
    vote, and a code whose targets all map to nothing is `unmapped`.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import icd_extract as X                                          # noqa: E402

SCHEMA = "icd9-gem-bridge/1"
ARTIFACT_NAME = "icd9_gem_reference.json"
MANIFEST_NAME = "icd9_gem_reference.manifest.json"

# The floor, the disposition vocabulary and the two code shapes all come from the extractor, so
# the builder and the reader cannot disagree about them. A constant defined twice is a constant
# that will eventually be changed once.
FLOOR_PERCENT = X.DC_FLOOR_PERCENT
DISPOSITIONS = X.GEM_DISPOSITIONS
KEY_RE = X.GEM_KEY_RE                       # ICD-9-CM, dotless: 250.00 -> 25000
TARGET_RE = X.CODE_KEY_RE                   # ICD-10-CM, dotless: E11.9  -> E119


def meets_floor(votes, total):
    """Exact integer form of votes/total >= FLOOR_PERCENT%.

    Deliberately not `100.0 * votes / total >= FLOOR_PERCENT`: the reader applies the same rule
    and classifies a bridge by it, so a vote sitting precisely on the bar must not be able to
    land on one side here and the other side there through floating-point rounding.
    """
    return votes * 100 >= FLOOR_PERCENT * total


class BuildError(Exception):
    """A problem that must stop the build rather than produce a quietly wrong artifact."""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def normalize(code):
    """A code reduced to the dotless uppercase form both the GEM and the extractor use."""
    return re.sub(r'[^0-9A-Za-z]', '', str(code)).upper()


def load_code_list(path):
    """The CDC ICD-10-CM code list, as a set of dotless codes.

    Used only to RECORD which GEM targets have since been retired from the current list. It
    does not gate anything: a retired target is still the correct historical bridge, and the
    DC reference is the authority on what maps.
    """
    codes = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            code = normalize(line.rstrip("\n").partition("\t")[0])
            if code:
                codes.add(code)
    if not codes:
        raise BuildError(f"{path}: no codes read")
    return codes


def load_gem(path):
    """The GEM, validated on the way in. Raises rather than guessing.

    Every row must be an ICD-9-CM key mapping to a non-empty list of distinct ICD-10-CM
    targets. Anything else is a source this builder does not understand, and emitting a
    reshaped version of a file it does not understand is how a wrong answer gets a provenance
    block wrapped around it.
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict) or not raw:
        raise BuildError(f"{path}: expected a non-empty JSON object of ICD-9 code -> targets")
    rows = {}
    for key, value in raw.items():
        code = normalize(key)
        if not KEY_RE.match(code):
            raise BuildError(f"{path}: row key {key!r} is not an ICD-9-CM code shape")
        if isinstance(value, str) or not isinstance(value, list):
            raise BuildError(f"{path}: row {key!r} is not a list of ICD-10-CM targets; this "
                             f"builder only understands the code -> [target, ...] shape")
        if not value:
            raise BuildError(f"{path}: row {key!r} has an empty target list")
        targets = []
        for t in value:
            if not isinstance(t, str):
                raise BuildError(f"{path}: row {key!r} has a non-string target {t!r}")
            target = normalize(t)
            if not TARGET_RE.match(target):
                raise BuildError(f"{path}: row {key!r} has target {t!r}, which is not an "
                                 f"ICD-10-CM code shape")
            if target in targets:
                raise BuildError(f"{path}: row {key!r} lists target {target} twice")
            targets.append(target)
        if code in rows and rows[code] != targets:
            raise BuildError(f"{path}: key {key!r} normalizes to {code}, which already exists "
                             f"with different targets")
        rows[code] = targets
    return rows


def load_dc_reference(folder):
    """The Chunk 2 DC reference, through the extractor's own loader.

    That loader returns None for anything it will not trust at run time. Refusing to build on
    a reference the program itself would reject is the point: a disposition summary computed
    from data the extractor will not read is worse than no summary at all.
    """
    ref = X.load_dc_reference(folder)
    if ref is None:
        raise BuildError(f"{folder}: no usable {X.DC_REF_NAME}. The extractor's own loader "
                         f"refused it, so it is missing, damaged, wrong-schema, or not "
                         f"vouched for by {X.DC_MANIFEST_NAME}.")
    return ref


def disposition(dc_ref, targets):
    """(state, dc, votes, total) for one ICD-9 row's targets, by the runtime rule.

    Uses the extractor's own dc_for_code so the audit recorded in the artifact is the answer
    the program will give, not a second implementation of the same idea.
    """
    votes = []
    for target in targets:
        state, dc, _name, _basis = X.dc_for_code(dc_ref, target)
        if state == "mapped":
            votes.append(dc)
    if not votes:
        return "unmapped", "", 0, 0
    tally = {}
    for dc in votes:
        tally[dc] = tally.get(dc, 0) + 1
    # Ties break to the lower DC number so the artifact is deterministic; a tie cannot clear
    # the floor with more than two candidates anyway.
    dc, count = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    if meets_floor(count, len(votes)):
        return "mapped", dc, count, len(votes)
    return "ambiguous", dc, count, len(votes)


def build(gem_path, codes_path, dc_folder, generated):
    gem = load_gem(gem_path)
    code_list = load_code_list(codes_path)
    dc_ref = load_dc_reference(dc_folder)

    counts = {d: 0 for d in DISPOSITIONS}
    for targets in gem.values():
        counts[disposition(dc_ref, targets)[0]] += 1
    if sum(counts.values()) != len(gem):
        raise BuildError("disposition imbalance: the summary does not cover every source row")

    all_targets = sorted({t for targets in gem.values() for t in targets})
    retired = sorted(t for t in all_targets if t not in code_list)
    no_current = sorted(k for k, targets in gem.items()
                        if all(t not in code_list for t in targets))

    dc_digest = sha256_file(os.path.join(dc_folder, X.DC_REF_NAME))
    artifact = {
        "schema": SCHEMA,
        "generated": generated,
        "policy": {
            "floor_percent": FLOOR_PERCENT,
            "dispositions": list(DISPOSITIONS),
            "vote_basis": "one vote per bridge target that the Potential-DC reference maps to "
                          "a diagnostic code; targets it does not map cast no vote",
            "detection_basis": "codes the record itself labels ICD-9 or ICD-9-CM, or a CDA "
                               "element whose codeSystem is the ICD-9-CM OID; never an "
                               "unlabeled number",
            "note": "Research cross-reference only. A GEM is a frozen conversion table, not an "
                    "official VA crosswalk, not a rating, not an entitlement or filing "
                    "decision.",
        },
        "source": {
            "name": os.path.basename(gem_path),
            "sha256": sha256_file(gem_path),
            "bytes": os.path.getsize(gem_path),
            "rows": len(gem),
        },
        "code_list": {
            "name": os.path.basename(codes_path),
            "sha256": sha256_file(codes_path),
            "rows": len(code_list),
        },
        "counts": {
            "source_rows": len(gem),
            "bridge_rows": len(gem),
            "multi_target_rows": sum(1 for t in gem.values() if len(t) > 1),
            "target_slots": sum(len(t) for t in gem.values()),
            "distinct_targets": len(all_targets),
            "targets_outside_code_list": len(retired),
            "rows_with_no_current_target": len(no_current),
        },
        "bridge": {code: targets for code, targets in sorted(gem.items())},
        "dc_audit": {
            "reference": {
                "name": X.DC_REF_NAME,
                "schema": dc_ref["schema"],
                "sha256": dc_digest,
                "generated": dc_ref["generated"],
            },
            "counts": counts,
            "explanation": "How the bridge rows disposed against the DC reference named here, "
                           "at build time. This is a RECEIPT, not an answer: the extractor "
                           "votes live against whichever DC reference is installed beside it, "
                           "so a rebuilt DC reference changes the answers without this file "
                           "having to be rebuilt.",
        },
        "ledger": {
            "explanation": "Every source row bridges. There is no quarantine: unlike the DC "
                           "crosswalk, a GEM target is never emitted as a diagnosis, so a "
                           "target the current CDC list no longer carries is still the "
                           "correct historical bridge and is preserved. The two lists below "
                           "record that frozen-vintage fact for audit and gate nothing.",
            "targets_outside_code_list": retired,
            "rows_with_no_current_target": no_current,
        },
    }

    if len(artifact["bridge"]) != len(gem):
        raise BuildError(f"ledger imbalance: {len(artifact['bridge'])} bridged vs {len(gem)} "
                         f"source rows")
    return artifact


def dumps(obj):
    """Stable JSON: sorted keys, no incidental whitespace, one trailing newline."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def main():
    ap = argparse.ArgumentParser(
        description="Build the local labeled-ICD-9-CM bridge artifact.")
    ap.add_argument("--gem", help="path to icd9-to-icd10-gem.json")
    ap.add_argument("--ratemyvso", help="RateMyVSO repository root; the GEM is read from "
                                        "src/data/icd9-to-icd10-gem.json under it")
    ap.add_argument("--codes", required=True, help="path to icd10_data.tsv, the CDC code list")
    ap.add_argument("--dc-reference", required=True, dest="dc_reference",
                    help=f"folder holding {X.DC_REF_NAME} and {X.DC_MANIFEST_NAME}")
    ap.add_argument("--out", required=True, help="folder to write the artifact and manifest into")
    ap.add_argument("--generated", default=datetime.datetime.now(datetime.timezone.utc)
                    .strftime("%Y-%m-%d"),
                    help="generation date recorded in the artifact (default: today, UTC). It is "
                         "an input so a build can be reproduced byte for byte.")
    args = ap.parse_args()

    if bool(args.gem) == bool(args.ratemyvso):
        ap.error("give exactly one of --gem or --ratemyvso")
    gem_path = args.gem or os.path.join(args.ratemyvso, "src", "data", "icd9-to-icd10-gem.json")

    for path in (gem_path, args.codes):
        if not os.path.isfile(path):
            print(f"ERROR: not found: {path}", file=sys.stderr)
            return 2
    if not os.path.isdir(args.dc_reference):
        print(f"ERROR: not a folder: {args.dc_reference}", file=sys.stderr)
        return 2
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', args.generated):
        print("ERROR: --generated must be YYYY-MM-DD", file=sys.stderr)
        return 2

    try:
        artifact = build(gem_path, args.codes, args.dc_reference, args.generated)
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
        "dc_audit": {k: v for k, v in artifact["dc_audit"].items() if k != "explanation"},
        "ledger_summary": {
            "targets_outside_code_list": artifact["counts"]["targets_outside_code_list"],
            "rows_with_no_current_target": artifact["counts"]["rows_with_no_current_target"],
        },
    }
    with open(os.path.join(args.out, MANIFEST_NAME), "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(manifest))

    c, a = artifact["counts"], artifact["dc_audit"]["counts"]
    print(f"{ARTIFACT_NAME}: {manifest['artifact']['bytes']:,} bytes  "
          f"sha256 {manifest['artifact']['sha256'][:16]}...")
    print(f"  source rows      {c['source_rows']:,}  (all bridged, none quarantined)")
    print(f"  multi-target     {c['multi_target_rows']:,}  "
          f"({c['target_slots']:,} target slots, {c['distinct_targets']:,} distinct)")
    print(f"  against {artifact['dc_audit']['reference']['name']} "
          f"{artifact['dc_audit']['reference']['sha256'][:16]}...")
    print(f"    mapped         {a['mapped']:,}")
    print(f"    ambiguous      {a['ambiguous']:,}")
    print(f"    unmapped       {a['unmapped']:,}")
    print(f"  frozen vintage   {c['targets_outside_code_list']} target(s) no longer in "
          f"{os.path.basename(args.codes)}, {c['rows_with_no_current_target']} row(s) with "
          f"no current target")
    return 0


if __name__ == "__main__":
    sys.exit(main())
