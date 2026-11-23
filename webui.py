#!/usr/bin/env python3
"""Live web UI for the AS7343 light meter (phone friendly).

	.venv/bin/python webui.py [--port 8080]

One thread owns the sensor: it streams spectral frames and runs queued jobs
(flicker capture, point records).  Browsers get updates by Server-Sent
Events on /events.
"""
import argparse
import csv
import datetime
import io
import json
import math
import os
import queue
import threading
import time
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import lightmeter as lm
from lightmeter import colour

HERE = os.path.dirname(os.path.abspath(__file__))
FLICKER_S = 2.0
RECORD_AVG = 8
CSV_FIELDS = ['time', 'location', 'lux', 'CCT', 'Duv', 'Ra', 'R9', 'Rf', 'Rg',
	'flicker_f0', 'flicker_percent', 'flicker_index', 'ieee1789']


def clean(v):
	"""numpy / NaN -> plain JSON values."""
	if isinstance(v, dict):
		return {k: clean(x) for k, x in v.items()}
	if isinstance(v, (list, tuple, np.ndarray)):
		return [clean(x) for x in v]
	if isinstance(v, (np.floating, np.integer, np.bool_)):
		v = v.item()
	if isinstance(v, float) and not math.isfinite(v):
		return None
	return v


def swatch(XYZ):
	"""Display colour of the light (sRGB hex, brightness normalised)."""
	if not XYZ[1] > 0:
		return '#000000'
	rgb = colour.XYZ_to_sRGB(XYZ / XYZ[1], apply_cctf_encoding=False)
	rgb = np.clip(rgb / max(rgb.max(), 1e-9), 0, 1)
	rgb = colour.cctf_encoding(rgb)
	return '#' + ''.join(f'{int(round(c * 255)):02x}' for c in rgb)


def summary(col, ren):
	d = {
		'lux': col['lux'], 'CCT': col['CCT'], 'Duv': col['Duv'],
		'x': col['xy'][0], 'y': col['xy'][1], 'swatch': swatch(col['XYZ']),
	}
	if ren:
		d.update(Ra=ren['Ra'], R9=ren['Ri'][9], Rf=ren['Rf'], Rg=ren['Rg'],
			Ri=[ren['Ri'][k] for k in range(1, 15)])
	return d


def flicker_summary(flk):
	keys = ('detected', 'f0', 'percent', 'index', 'ieee1789', 'detect_limit_percent', 'fs')
	return {k: flk.get(k) for k in keys}


class Meter:
	def __init__(self, dev, cal, data_path):
		self.dev, self.cal, self.data_path = dev, cal, data_path
		self.jobs = queue.Queue()
		self.cond = threading.Condition()
		self.seq = 0
		self.state = {}
		self.col = None
		self.ren = None
		self.flicker = None			# (summary, unix time)
		self.busy = None

	# ---- sensor thread ----

	def run(self):
		while True:
			try:
				fn, fut, label = self.jobs.get_nowait()
			except queue.Empty:
				fn = None
			try:
				if fn:
					self.busy = label
					self.publish()
					try:
						fut.set_result(fn())
					except Exception as e:  # noqa: BLE001  report to the client
						fut.set_exception(e)
					self.busy = None
					continue
				raw, gain, tint, flags = self.dev.read_continuous()
				self.col = lm.colorimetry(self.cal, lm.basic_counts(self.cal, raw, gain, tint))
				self.publish(gain=gain, flags=flags)
			except (OSError, TimeoutError) as e:
				# typically a loose I2C wire while walking around; the driver
				# restarts its measurement engine on the next call
				self.publish(error=f'传感器读取失败: {e}')
				time.sleep(1)

	def render_loop(self):
		"""CRI/TM-30 take ~0.5 s on a Pi 4: keep them off the sensor thread."""
		last = None
		while True:
			col = self.col
			if col is not None and col is not last:
				last = col
				self.ren = lm.rendering(col['spectrum']) if col['lux'] > 1 else None
			time.sleep(0.5)

	def publish(self, gain=None, flags=(), error=None):
		with self.cond:
			col, ren = self.col, self.ren
			s = {'time': time.time(), 'busy': self.busy, 'error': error}
			if gain is not None:
				s.update(gain=gain, flags=list(flags))
			elif self.state:
				s.update(gain=self.state.get('gain'), flags=self.state.get('flags', []))
			if col is not None:
				s.update(summary(col, ren))
				sp = np.clip(col['spectrum'][::5], 0, None)
				s['spectrum'] = (sp / sp.max()).round(3) if sp.max() > 0 else sp
				flk = self.flicker[0] if self.flicker else None
				s['checks'] = [{'key': k, 'name': n, 'ok': ok}
					for k, n, ok in lm.judge(col, ren, flk)]
			if self.flicker:
				s['flicker'] = dict(self.flicker[0], at=self.flicker[1])
			self.seq += 1
			self.state = clean(s)
			self.cond.notify_all()

	# ---- jobs (run on the sensor thread) ----

	def submit(self, fn, label):
		fut = Future()
		self.jobs.put((fn, fut, label))
		return fut

	def job_flicker(self):
		flk, _ = lm.measure_flicker(self.dev, FLICKER_S)
		self.flicker = (flicker_summary(flk), time.time())
		return clean(self.flicker[0])

	def job_record(self, location):
		meas, col = lm.measure(self.dev, self.cal, RECORD_AVG)
		ren = lm.rendering(col['spectrum'])
		flk, _ = lm.measure_flicker(self.dev, FLICKER_S)
		self.flicker = (flicker_summary(flk), time.time())
		rec = dict(summary(col, ren), time=meas['time'], location=location,
			gain=meas['gain'], flags=meas['flags'],
			flicker=flicker_summary(flk),
			spectrum_380_780_1nm=col['spectrum'])
		rec = clean(rec)
		os.makedirs(os.path.dirname(self.data_path), exist_ok=True)
		with open(self.data_path, 'a') as f:
			f.write(json.dumps(rec, ensure_ascii=False) + '\n')
		return rec

	def records(self):
		if not os.path.exists(self.data_path):
			return []
		with open(self.data_path) as f:
			out = [json.loads(line) for line in f if line.strip()]
		for r in out:
			r.pop('spectrum_380_780_1nm', None)
		return out


def records_csv(recs):
	def r(v, nd):
		return '' if v is None else round(v, nd)
	buf = io.StringIO()
	w = csv.writer(buf)
	w.writerow(CSV_FIELDS)
	for x in recs:
		f = x.get('flicker') or {}
		w.writerow([x.get('time'), x.get('location'), r(x.get('lux'), 1), r(x.get('CCT'), 0),
			r(x.get('Duv'), 4), r(x.get('Ra'), 1), r(x.get('R9'), 1), r(x.get('Rf'), 1), r(x.get('Rg'), 1),
			r(f.get('f0'), 1), r(f.get('percent'), 2), r(f.get('index'), 4), f.get('ieee1789') or ''])
	return '\ufeff' + buf.getvalue()			# BOM: Excel opens UTF-8


def make_handler(meter):
	class Handler(BaseHTTPRequestHandler):
		def log_message(self, *a):
			pass

		def reply(self, code, body, ctype='application/json; charset=utf-8', extra=()):
			if not isinstance(body, bytes):
				body = (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode()
			self.send_response(code)
			self.send_header('Content-Type', ctype)
			self.send_header('Content-Length', str(len(body)))
			self.send_header('Cache-Control', 'no-store')
			for k, v in extra:
				self.send_header(k, v)
			self.end_headers()
			self.wfile.write(body)

		def do_GET(self):
			if self.path in ('/', '/index.html'):
				with open(os.path.join(HERE, 'web', 'index.html'), 'rb') as f:
					self.reply(200, f.read(), 'text/html; charset=utf-8')
			elif self.path == '/events':
				self.events()
			elif self.path == '/api/records':
				self.reply(200, meter.records())
			elif self.path == '/api/records.csv':
				name = datetime.date.today().strftime('light-%Y%m%d.csv')
				self.reply(200, records_csv(meter.records()), 'text/csv; charset=utf-8',
					[('Content-Disposition', f'attachment; filename="{name}"')])
			else:
				self.reply(404, {'error': 'not found'})

		def do_POST(self):
			n = int(self.headers.get('Content-Length') or 0)
			body = json.loads(self.rfile.read(n) or b'{}')
			if self.path == '/api/flicker':
				fut = meter.submit(meter.job_flicker, '正在测频闪…')
			elif self.path == '/api/record':
				loc = str(body.get('location') or '未命名').strip()[:40]
				fut = meter.submit(lambda: meter.job_record(loc), f'正在记录「{loc}」…')
			else:
				return self.reply(404, {'error': 'not found'})
			try:
				self.reply(200, fut.result(timeout=60))
			except Exception as e:  # noqa: BLE001
				self.reply(500, {'error': str(e)})

		def events(self):
			self.send_response(200)
			self.send_header('Content-Type', 'text/event-stream')
			self.send_header('Cache-Control', 'no-store')
			self.end_headers()
			seen = -1
			try:
				while True:
					with meter.cond:
						meter.cond.wait_for(lambda: meter.seq != seen, timeout=15)
						seq, state = meter.seq, meter.state
					if seq == seen:
						self.wfile.write(b': ping\n\n')		# keep proxies/phones awake
					else:
						seen = seq
						self.wfile.write(b'data: ' + json.dumps(state).encode() + b'\n\n')
					self.wfile.flush()
			except (BrokenPipeError, ConnectionResetError):
				pass

	return Handler


def serve(dev, cal, port, data_path):
	meter = Meter(dev, cal, data_path)
	for fn in (meter.run, meter.render_loop):
		threading.Thread(target=fn, daemon=True).start()
	srv = ThreadingHTTPServer(('0.0.0.0', port), make_handler(meter))
	srv.daemon_threads = True
	return srv, meter


def main():
	ap = argparse.ArgumentParser(description='AS7343 光源质量 Web 实时界面')
	ap.add_argument('--port', type=int, default=8080)
	ap.add_argument('--data', default=os.path.join(HERE, 'data', 'records.jsonl'))
	args = ap.parse_args()
	cal = lm.load_cal()
	from as7343 import AS7343
	srv, _ = serve(AS7343(), cal, args.port, args.data)
	print(f'listening on http://0.0.0.0:{args.port}', flush=True)
	srv.serve_forever()


if __name__ == '__main__':
	main()
