"""Offline tests of the web UI with a fake sensor (no hardware)."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import lightmeter as lm  # noqa: E402
import webui  # noqa: E402
from test_offline import DEMO, needs_cal  # noqa: E402


class FakeDev:
	"""Replays the ams demo counts; flicker is a 100 Hz 20 % sine."""
	tint_ms = 100.0

	def __init__(self):
		# same basic counts as the ams demo, at 100 ms / 64x
		k = self.tint_ms * 64 / (DEMO['tint_ms'] * DEMO['gain'])
		self.raw = {ch: v * k for ch, v in DEMO['raw'].items()}

	def read_continuous(self):
		time.sleep(0.05)
		return self.raw, 64, self.tint_ms, []

	def measure_spectral(self, n_avg=4):
		return self.raw, 64, self.tint_ms, []

	def capture_flicker(self, seconds=2.0):
		fs = 1.0 / (256 * 2.78e-6)
		t = np.arange(int(seconds * fs)) / fs
		s = np.round(150 * (1 + 0.2 * np.sin(2 * np.pi * 100 * t)))
		return list(s.astype(int)), fs, fs, 255, 64.0


@needs_cal
class TestWebUI(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.tmp = tempfile.TemporaryDirectory()
		cal = lm.load_cal()
		cal['lux_factor'] = 1.0
		path = os.path.join(cls.tmp.name, 'records.jsonl')
		cls.srv, cls.meter = webui.serve(FakeDev(), cal, 0, path)
		threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
		cls.base = f'http://127.0.0.1:{cls.srv.server_address[1]}'

	@classmethod
	def tearDownClass(cls):
		cls.srv.shutdown()
		cls.tmp.cleanup()

	def get(self, path):
		with urllib.request.urlopen(self.base + path, timeout=10) as r:
			return r.status, r.headers.get('Content-Type'), r.read()

	def post(self, path, body):
		req = urllib.request.Request(self.base + path, json.dumps(body).encode(),
			{'Content-Type': 'application/json'})
		with urllib.request.urlopen(req, timeout=60) as r:
			return json.loads(r.read())

	def first_event(self, pred, timeout=10):
		with urllib.request.urlopen(self.base + '/events', timeout=timeout) as r:
			t0 = time.time()
			while time.time() - t0 < timeout:
				line = r.readline().decode()
				if line.startswith('data: '):
					s = json.loads(line[6:])
					if pred(s):
						return s
		self.fail('no matching event')

	def test_index(self):
		code, ctype, body = self.get('/')
		self.assertEqual(code, 200)
		self.assertIn('text/html', ctype)
		self.assertIn('EventSource', body.decode())

	def test_live_event(self):
		s = self.first_event(lambda s: s.get('Ra') is not None)
		# web must show exactly what the CLI pipeline computes
		dev = FakeDev()
		col = lm.colorimetry(self.meter.cal, lm.basic_counts(self.meter.cal, dev.raw, 64, dev.tint_ms))
		self.assertAlmostEqual(s['lux'], col['lux'], places=6)
		self.assertAlmostEqual(s['CCT'], col['CCT'], places=3)
		self.assertEqual(len(s['spectrum']), 81)
		self.assertRegex(s['swatch'], r'^#[0-9a-f]{6}$')
		self.assertIn('ra', {c['key'] for c in s['checks']})

	def test_flicker_and_record(self):
		f = self.post('/api/flicker', {})
		self.assertTrue(f['detected'])
		self.assertAlmostEqual(f['f0'], 100, delta=0.3)
		self.assertAlmostEqual(f['percent'], 20, delta=0.5)
		self.assertEqual(f['ieee1789'], '高风险')

		rec = self.post('/api/record', {'location': '书桌<b>'})
		self.assertEqual(rec['location'], '书桌<b>')
		self.assertEqual(len(rec['spectrum_380_780_1nm']), 401)
		code, _, body = self.get('/api/records')
		recs = json.loads(body)
		self.assertEqual(recs[-1]['location'], '书桌<b>')
		self.assertNotIn('spectrum_380_780_1nm', recs[-1])
		code, ctype, body = self.get('/api/records.csv')
		self.assertIn('text/csv', ctype)
		lines = body.decode('utf-8-sig').strip().splitlines()
		self.assertEqual(lines[0].split(',')[:3], ['time', 'location', 'lux'])
		self.assertIn('书桌<b>', lines[-1])

		# after the jobs the live stream must resume
		s = self.first_event(lambda s: not s.get('busy') and s.get('flicker'))
		self.assertTrue(s['flicker']['detected'])


if __name__ == '__main__':
	unittest.main()
