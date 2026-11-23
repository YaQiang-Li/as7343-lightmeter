"""Offline tests (no sensor needed). Run on the Pi venv:

	.venv/bin/python -m unittest discover -s tests

Tests marked "needs cal" are skipped until cal/ams_golden.json has been
generated with tools/extract_ams_cal.py (see README).
"""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import lightmeter as lm  # noqa: E402

HAVE_CAL = os.path.exists(lm.CAL_AMS)
# demo input and results of the ams template, stored by extract_ams_cal.py
DEMO = json.load(open(lm.CAL_AMS))['demo'] if HAVE_CAL else None
needs_cal = unittest.skipUnless(HAVE_CAL, 'needs cal/ams_golden.json')


@needs_cal
class TestAmsPipeline(unittest.TestCase):
	"""Reproduce 'Demonstration Calculations' of the ams template."""

	def setUp(self):
		self.cal = lm.load_cal()
		self.cal['lux_factor'] = 1.0
		bc = lm.basic_counts(self.cal, DEMO['raw'], DEMO['gain'], DEMO['tint_ms'],
			np.array(DEMO['balance']))
		self.col = lm.colorimetry(self.cal, bc)

	def test_spectral_path(self):
		# our CIE 1931 CMF table differs slightly from the template's one
		np.testing.assert_allclose(self.col['XYZ'], DEMO['XYZ'], rtol=5e-3)
		self.assertAlmostEqual(self.col['lux'], DEMO['lux'], delta=DEMO['lux'] * 5e-3)

	def test_direct_path(self):
		self.assertAlmostEqual(self.col['lux_direct'], DEMO['lux_direct'], delta=0.01)

	def test_cct(self):
		# template uses McCamy's estimate; Ohno 2013 should agree closely
		self.assertAlmostEqual(self.col['CCT'], DEMO['cct_mccamy'], delta=30)


class TestFlicker(unittest.TestCase):
	fs = 1.0 / (256 * 2.78e-6)
	t = np.arange(int(2 * fs)) / fs
	rng = np.random.default_rng(1)

	def wave(self, w):
		return np.round(w + self.rng.normal(0, 0.5, len(self.t)))

	def test_sine(self):
		r = lm.analyze_flicker(self.wave(150 * (1 + 0.2 * np.sin(2 * np.pi * 100 * self.t))), self.fs)
		self.assertTrue(r['detected'])
		self.assertAlmostEqual(r['f0'], 100, delta=0.2)
		self.assertAlmostEqual(r['percent'], 20, delta=0.5)
		self.assertAlmostEqual(r['index'], 0.2 / np.pi, delta=0.003)

	def test_pwm(self):
		w = np.where((self.t * 250) % 1 < 0.3, 200.0, 0.0)
		r = lm.analyze_flicker(self.wave(w), self.fs)
		self.assertAlmostEqual(r['f0'], 250, delta=0.3)
		self.assertGreater(r['percent'], 95)
		self.assertAlmostEqual(r['index'], 0.7, delta=0.05)

	def test_steady(self):
		r = lm.analyze_flicker(self.wave(np.full(len(self.t), 150.0)), self.fs)
		self.assertFalse(r['detected'])

	def test_ieee1789(self):
		self.assertEqual(lm.ieee1789(100, 20), '高风险')
		self.assertEqual(lm.ieee1789(100, 5), '低风险')
		self.assertEqual(lm.ieee1789(100, 2), '无影响')
		self.assertEqual(lm.ieee1789(2000, 80), '低风险')


class TestRendering(unittest.TestCase):
	def test_fl2(self):
		import colour
		sd = colour.SDS_ILLUMINANTS['FL2'].copy().align(colour.SpectralShape(380, 780, 1))
		r = lm.rendering(sd.values)
		self.assertAlmostEqual(r['Ra'], 64, delta=1)
		self.assertLess(r['Ri'][9], 0)


if __name__ == '__main__':
	unittest.main()
