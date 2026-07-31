"""Convert the CDC ICD-10-CM order file into a compact code->description table.

Order-file line (fixed-ish width):
  00002 A000    1 Cholera due to Vibrio cholerae 01, biovar cholerae   Cholera due to...
  order  code   flag short-desc(60)                                    long-desc
We keep code (dotless) + the fuller (long) description.
"""
import re, sys

SRC = r"C:\tmp\icd_order.txt"
OUT = r"C:\tmp\icd10_data.tsv"

# Fixed columns in the CDC order file:
#   [0:5] order  [6:13] code  [14] valid-flag  [16:76] short desc  [77:] long desc
n = 0
with open(SRC, encoding="utf-8") as f, open(OUT, "w", encoding="utf-8") as out:
    for line in f:
        line = line.rstrip("\n")
        if len(line) < 16:
            continue
        code = line[6:13].strip()
        long_desc = line[77:].strip() if len(line) > 77 else ""
        short_desc = line[16:76].strip()
        desc = long_desc or short_desc
        if not code or not desc:
            continue
        out.write(f"{code}\t{desc}\n")
        n += 1

print("wrote", n, "codes to", OUT)
