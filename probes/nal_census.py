#!/usr/bin/env python3
"""Annex-B NAL census for a raw H.264/H.265 elementary stream.

Prints one line:
  codec=<h264|h265> vcl=<n> idr=<n> sei=<n> sps=<n>

vcl counts first-slice VCL NALs (== frames on single-slice encoders),
idr counts random-access points, sei counts SEI NALs.
"""
import sys

path, codec = sys.argv[1], sys.argv[2]
data = open(path, "rb").read()
vcl = idr = sei = sps = 0
idr_marks = []
i = 0
n = len(data)
while True:
    i = data.find(b"\x00\x00\x01", i)
    if i < 0 or i + 4 > n:
        break
    b0 = data[i + 3]
    if codec == "h265":
        t = (b0 >> 1) & 0x3F
        if t in (19, 20, 21):
            idr += 1
            idr_marks.append(vcl)
            vcl += 1
        elif t <= 31:
            vcl += 1
        elif t in (39, 40):
            sei += 1
        elif t == 33:
            sps += 1
    else:
        t = b0 & 0x1F
        if t == 5:
            idr += 1
            idr_marks.append(vcl)
            vcl += 1
        elif 1 <= t <= 4:
            vcl += 1
        elif t == 6:
            sei += 1
        elif t == 7:
            sps += 1
    i += 3
ints = [b - a for a, b in zip(idr_marks, idr_marks[1:])]
extra = ""
if ints:
    from collections import Counter
    modal = Counter(ints).most_common(1)[0][0]
    dev = max(abs(x - modal) for x in ints)
    extra = f" gopint={modal} gopdev={dev} gopn={len(ints)}"
print(f"codec={codec} vcl={vcl} idr={idr} sei={sei} sps={sps}{extra}")
