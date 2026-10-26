"""Report self-ingestion and the RateMyVSO export (2026-09-05).

Synthetic fixtures only. Nothing here reads a real record, and nothing here runs OCR or an
extraction: the export is built from a hand-made run dict, and process_file is replaced with a
tripwire so a second extraction would fail the test.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "source"))
import icd_extract  # noqa: E402


def write(path, text, encoding="utf-8", newline="\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=encoding, newline=newline) as f:
        f.write(text)


def synthetic_run(tmp):
    f1 = {"path": os.path.join(tmp, "Example record.pdf"), "error": None, "grain": "page",
          "pages": {"p2": {"docpage": "1 of 3", "reviewer": "Example reviewer", "reviewed": "1/2/2026"}},
          "default": {},
          "rows": [
              {"loc": "p2", "code": "G47.33", "sys": "icd10", "csys": "", "desc": "Obstructive sleep apnea",
               "date": "9/1/2026", "ocr": False},
              {"loc": "p2", "code": "-", "sys": "", "csys": "", "desc": "Foot pain, right", "date": "",
               "ocr": True},
              {"loc": "p3", "code": "Z79.899", "sys": "icd10", "csys": "",
               "desc": "Long term drug therapy  (x3)", "date": "", "ocr": False},
          ]}
    f2 = {"path": os.path.join(tmp, "notes.txt"), "error": None, "grain": "file", "pages": {}, "default": {},
          "rows": [{"loc": "line 7", "code": "-", "sys": "", "csys": "",
                    "desc": "HGB A1C (Tosoh G8) (83036) Ordered", "date": "", "ocr": False}]}
    f3 = {"path": os.path.join(tmp, "sub", "notes.txt"), "error": "locked", "grain": "file", "pages": {},
          "default": {}, "rows": []}
    return {"target": tmp, "output": os.path.join(tmp, "icd10_report.txt"), "when": "2026-09-05 10:00",
            "stopped": False, "unique": True, "files": [f1, f2, f3], "ref": None, "gem": None, "snomed": None}


class OwnOutputExclusion(unittest.TestCase):
    def test_folder_scan_keeps_only_genuine_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            banner = icd_extract.REPORT_BANNER
            write(os.path.join(tmp, "icd10_report.txt"), banner + "\nScanned: x\n")
            # Renamed, BOM, CRLF: still the report by its first line.
            write(os.path.join(tmp, "renamed copy.txt"), "﻿" + banner + "\r\nScanned: x\r\n", newline="")
            write(os.path.join(tmp, "old_claim_handoff.txt"), "CLAIM FILE HANDOFF\r\n====\r\n", newline="")
            write(os.path.join(tmp, "whatever.txt"), "﻿\n\nCLAIM FILE HANDOFF\n")
            export = icd_extract.extractor_export_json(synthetic_run(tmp))
            write(os.path.join(tmp, "icd10_report_ratemyvso.json"), export)
            write(os.path.join(tmp, "renamed.json"), "﻿  " + export)
            # Genuine clinical text files, one of them NAMED like a report and one that says
            # "handoff" inside its body: both must still be scanned.
            write(os.path.join(tmp, "clinic report.txt"), "Patient seen 1/2/2026.\nAssessment: G47.33 OSA\n")
            write(os.path.join(tmp, "report_notes.txt"), "Discussed the claim file handoff with the veteran.\nDx: M54.5\n")
            found = sorted(os.path.basename(p) for p in icd_extract.gather(tmp))
            self.assertEqual(found, ["clinic report.txt", "report_notes.txt"])
            for name in ("icd10_report.txt", "renamed copy.txt", "old_claim_handoff.txt", "whatever.txt",
                         "icd10_report_ratemyvso.json", "renamed.json"):
                self.assertTrue(icd_extract.is_own_report(os.path.join(tmp, name)), name)
            self.assertEqual(icd_extract.own_output_signature(b"\xef\xbb\xbf" + banner.encode()), "report")
            self.assertEqual(icd_extract.own_output_signature("CLAIM FILE HANDOFF\r\n"), "handoff")
            self.assertEqual(icd_extract.own_output_signature(export), "export")
            self.assertIsNone(icd_extract.own_output_signature('{"format": "something else"}'))
            self.assertIsNone(icd_extract.own_output_signature("Dx: G47.33 report handoff"))

    def test_output_path_still_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "custom_name.txt")
            write(out, "Dx: G47.33\n")
            write(os.path.join(tmp, "real.txt"), "Dx: M54.5\n")
            found = sorted(os.path.basename(p) for p in icd_extract.gather(tmp, out))
            self.assertEqual(found, ["real.txt"])


class Export(unittest.TestCase):
    def test_export_reuses_entries_and_never_rescans(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = synthetic_run(tmp)
            original = icd_extract.process_file

            def tripwire(*a, **k):
                raise AssertionError("export must not extract again")
            icd_extract.process_file = tripwire
            try:
                data = icd_extract.extractor_export(run)
                text = icd_extract.extractor_export_json(run)
            finally:
                icd_extract.process_file = original
            self.assertEqual(json.loads(text)["entry_count"], data["entry_count"])
            self.assertEqual(data["format"], "ratemyvso.extractor-handoff")
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["exporter_version"], icd_extract.VERSION)
            self.assertRegex(data["exported_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertEqual(data["source_count"], 3)
            self.assertEqual(len(data["sources"]), 3)
            self.assertEqual(data["entry_count"], len(icd_extract._handoff_entries(run)))
            self.assertEqual(data["entry_count"], 4)
            self.assertTrue(data["run"]["duplicates_collapsed"])
            # Basenames only, never the folder.
            self.assertNotIn(tmp.replace("\\", "\\\\"), text)
            self.assertNotIn(tmp, text)
            s1, s2, s3 = data["sources"]
            self.assertEqual(s1, {"id": "s1", "alias": "F1", "display_name": "Example record.pdf",
                                  "locator_type": "page", "warnings": []})
            self.assertEqual(s2["locator_type"], "line")
            self.assertEqual(s2["warnings"], ["name_collision"])
            self.assertEqual(s3["warnings"], ["could_not_read", "name_collision"])
            self.assertEqual(s3["locator_type"], "file")
            by_wording = {e["wording"]: e for e in data["entries"]}
            osa = by_wording["Obstructive sleep apnea"]
            self.assertEqual(osa["source_id"], "s1")
            self.assertEqual(osa["locator"], {"extracted_page": 2, "printed_page": "1", "line": None})
            self.assertEqual(osa["code_system"], "ICD-10-CM")
            self.assertEqual(osa["code_raw"], "G47.33")
            self.assertEqual(osa["entry_kind"], "diagnosis")
            self.assertEqual(osa["date"], {"text": "9/1/2026", "approximate": True})
            self.assertEqual(osa["provider_context"], {"name": "Example reviewer", "role": "reviewer", "reviewed_at": "1/2/2026"})
            self.assertEqual(osa["review_flags"], ["source_not_checked"])
            self.assertEqual(osa["research_candidates"], [])
            self.assertEqual(osa["research_status"], "reference data unavailable")
            foot = by_wording["Foot pain, right"]
            self.assertIsNone(foot["code_raw"])
            self.assertEqual(foot["code_system"], "other")
            self.assertEqual(foot["entry_kind"], "unsure")
            self.assertEqual(foot["entry_kind_label"], "Clinical entry")
            self.assertEqual(foot["review_flags"], ["source_not_checked", "ocr", "untyped"])
            self.assertEqual(foot["date"], {"text": None, "approximate": False})
            self.assertEqual(foot["provider_context"]["name"], "Example reviewer")
            drug = by_wording["Long term drug therapy"]
            self.assertEqual(drug["occurrence_count"], 3)
            self.assertEqual(drug["entry_kind"], "status")
            self.assertIn("status_code", drug["review_flags"])
            self.assertEqual(drug["locator"], {"extracted_page": 3, "printed_page": None, "line": None})
            self.assertIsNone(drug["provider_context"], "a page header never crosses into the next page")
            order = by_wording["HGB A1C (Tosoh G8) (83036) Ordered"]
            self.assertEqual(order["source_id"], "s2")
            self.assertEqual(order["locator"], {"extracted_page": None, "printed_page": None, "line": 7})
            self.assertEqual(order["entry_kind"], "test_order")
            self.assertIn("test_order", order["review_flags"])
            ids = [e["id"] for e in data["entries"]]
            self.assertEqual(len(ids), len(set(ids)))
            for e in data["entries"]:
                self.assertEqual(set(e.keys()), {"id", "source_id", "locator", "code_system", "code_system_label",
                                                 "code_raw", "wording", "entry_kind", "entry_kind_label", "date",
                                                 "occurrence_count", "ocr", "provider_context", "review_flags",
                                                 "research_candidates", "research_status"})

    def test_export_refuses_without_a_run(self):
        saved = icd_extract.LAST_RUN
        icd_extract.LAST_RUN = None
        try:
            with self.assertRaises(ValueError):
                icd_extract.extractor_export()
        finally:
            icd_extract.LAST_RUN = saved


if __name__ == "__main__":
    unittest.main()
