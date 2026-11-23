#!/usr/bin/env python3
"""Generate cal/ams_golden.json from the ams AS7343 calibration template.

The template ships with the ams-OSRAM "AS7343_EvalSW_ALS" evaluation
software (this project was built against v2-4-3):

  Documents/application notes/Calibration/
  AS7343_AS7352 Template Spectral to Spectral to XYZ sensor calibration
  ALS woCM v2_2.xlsm

ams material may not be redistributed, so the generated file stays local
(it is git-ignored).  Besides the golden-device matrices it stores the
template's own demo input and results, which the offline tests replay.

Usage: extract_ams_cal.py <xlsm> [out.json]
"""
import json
import os
import re
import sys
import zipfile

CHANNELS = ['F1', 'F2', 'FZ', 'F3', 'F4', 'FY', 'F5', 'FXL', 'F6', 'F7', 'F8', 'NIR']
COLS = 'BCDEFGHIJKLM'
GAINS = [0.5, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048]
GAIN_COLS = ['Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z', 'AA', 'AB', 'AC']
# sheet<N>.xml of the template
INIT, BASIC, DEMO = 2, 3, 4		# I_INIT-File, I_Basic_Counts, Demonstration Calc.
# I_Basic_Counts row 5: F1..F8 in B..L, VIS in M, NIR in N
RAW_COLS = dict(zip(CHANNELS[:11], 'BCDEFGHIJKL'), NIR='N')
DEFAULT_OUT = os.path.join(os.path.dirname(__file__), '..', 'cal', 'ams_golden.json')


def load_sheet(z, sheet):
	x = z.read(f'xl/worksheets/sheet{sheet}.xml').decode()
	cells = {}
	for m in re.finditer(r'<c r="([A-Z]+\d+)"([^>/]*)>(.*?)</c>', x, re.S):
		v = re.search(r'<v>([^<]*)</v>', m.group(3))
		# numeric cells carry no t= attribute (t="s"/"str"/"e" are text/errors)
		if v and ' t="' not in m.group(2):
			cells[m.group(1)] = float(v.group(1))
	return cells


def main():
	z = zipfile.ZipFile(sys.argv[1])
	c = load_sheet(z, INIT)
	xyz = [[c[f'{col}{row}'] for col in COLS] for row in (7, 8, 9)]
	# rows 16..816 hold 300..1100 nm in 1 nm steps
	spec = [[c[f'{col}{row}'] for col in COLS] for row in range(16, 817)]
	assert c['A16'] == 300 and c['A816'] == 1100
	gain = {str(g): [c[f'{gc}{row}'] for row in range(9, 21)]
		for g, gc in zip(GAINS, GAIN_COLS)}
	b, d = load_sheet(z, BASIC), load_sheet(z, DEMO)
	demo = {
		'raw': {ch: b[f'{col}5'] for ch, col in RAW_COLS.items()},
		'tint_ms': b['D9'],
		'gain': b['D10'],
		'balance': [c[f'{gc}37'] for gc in GAIN_COLS[:12]],
		'XYZ': [d['W10'], d['W11'], d['W12']],		# spectral path
		'lux': d['W17'],
		'lux_direct': d['J17'],						# XYZ matrix path
		'cct_mccamy': d['W22'],
	}
	out = {
		'source': os.path.basename(sys.argv[1]),
		'channels': CHANNELS,
		'xyz_matrix': xyz,
		'spectral_wl_start': 300,
		'spectral_matrix': spec,
		'gain_correction': gain,
		'demo': demo,
	}
	path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
	with open(path, 'w') as f:
		json.dump(out, f)
	print(f'wrote {os.path.normpath(path)}')


if __name__ == '__main__':
	main()
