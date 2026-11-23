#!/usr/bin/env python3
"""Indoor light source quality meter based on the ams AS7343.

  lightmeter.py report   [--out FILE.json] [--target-lux N] [--no-flicker]
  lightmeter.py monitor  [--interval S]
  lightmeter.py calibrate --ref-lux N
"""
import argparse
import datetime
import json
import os
import sys
import time
import warnings

import numpy as np

warnings.filterwarnings('ignore')		# colour: scipy/matplotlib missing
import colour  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CAL_AMS = os.path.join(HERE, 'cal', 'ams_golden.json')
CAL_USER = os.path.join(HERE, 'cal', 'user.json')
WL = np.arange(380, 781)				# nm, 1 nm, visible range used by ams
K_M = 683.0							# lm/W

# ---------------------------------------------------------------- spectrum


def load_cal():
	if not os.path.exists(CAL_AMS):
		sys.exit(f'{CAL_AMS} 不存在：请先用 tools/extract_ams_cal.py 从 ams 评估软件包生成，见 README')
	with open(CAL_AMS) as f:
		ams = json.load(f)
	lux_factor = 1.0
	if os.path.exists(CAL_USER):
		with open(CAL_USER) as f:
			lux_factor = json.load(f)['lux_factor']
	start = ams['spectral_wl_start']
	spec = np.array(ams['spectral_matrix'])[WL[0] - start:WL[-1] - start + 1]
	return {
		'channels': ams['channels'],
		'xyz': np.array(ams['xyz_matrix']),
		'spec': spec,
		'gain': {float(k): np.array(v) for k, v in ams['gain_correction'].items()},
		'lux_factor': lux_factor,
	}


def basic_counts(cal, raw, gain, tint_ms, balance=None):
	"""ams 'Basic Counts' with gain correction: raw / (gain * tint_ms)."""
	r = np.array([raw[ch] for ch in cal['channels']], dtype=float)
	bc = r / (gain * tint_ms) * cal['gain'][float(gain)]
	return bc if balance is None else bc * balance


_CMF = None


def cmf():
	global _CMF
	if _CMF is None:
		cm = colour.MSDS_CMFS['CIE 1931 2 Degree Standard Observer']
		_CMF = cm.copy().align(colour.SpectralShape(380, 780, 1)).values
	return _CMF


def colorimetry(cal, bc):
	"""Spectral reconstruction (ams golden matrix) + photometry."""
	s = cal['spec'] @ bc					# relative W/m2/nm
	XYZ = s @ cmf()
	xyz_direct = cal['xyz'] @ bc			# ams small-matrix path, cross check
	r = {
		'spectrum': s,
		'XYZ': XYZ,
		'lux': K_M * XYZ[1] * cal['lux_factor'],
		'lux_direct': K_M * xyz_direct[1] * cal['lux_factor'],
		'xy': colour.XYZ_to_xy(XYZ),
	}
	r['uv_prime'] = colour.xy_to_Luv_uv(r['xy'])
	r['CCT'], r['Duv'] = colour.temperature.uv_to_CCT_Ohno2013(
		colour.xy_to_UCS_uv(r['xy']))
	r['CCT_direct'] = colour.temperature.uv_to_CCT_Ohno2013(
		colour.xy_to_UCS_uv(colour.XYZ_to_xy(xyz_direct)))[0]
	return r


def rendering(spectrum):
	"""CRI (CIE 13.3) and TM-30-18 on the reconstructed spectrum."""
	s = np.clip(spectrum, 0, None)
	sd = colour.SpectralDistribution(s / s.max(), WL)
	cri = colour.colour_rendering_index(sd, additional_data=True)
	tm = colour.colour_fidelity_index(
		sd, additional_data=True, method='ANSI/IES TM-30-18')
	return {
		'Ra': float(cri.Q_a),
		'Ri': {k: float(v.Q_a) for k, v in cri.Q_as.items()},
		'Rf': float(tm.R_f),
		'Rg': float(tm.R_g),
	}

# ----------------------------------------------------------------- flicker


def _period_metrics(seg, fs, f0):
	"""Fold one segment at f0 (equivalent-time sampling) -> (percent, index).

	Folding is not limited by Nyquist: as long as f0 and fs are not
	commensurate the samples land on many phases of the period.
	"""
	bins = max(8, min(64, len(seg) // 8))
	ph = (np.arange(len(seg)) / fs * f0) % 1.0
	k = (ph * bins).astype(int)
	cnt = np.bincount(k, minlength=bins)
	w = np.bincount(k, weights=seg, minlength=bins)[cnt > 0] / cnt[cnt > 0]
	pct = (w.max() - w.min()) / (w.max() + w.min()) * 100
	idx = np.clip(w - w.mean(), 0, None).sum() / w.sum()
	return pct, idx


def analyze_flicker(samples, fs, f_min=20.0):
	"""Fundamental, percent flicker, flicker index of the FD waveform."""
	x = np.asarray(samples, dtype=float)
	m = x.mean()
	n = len(x)
	win = np.hanning(n)
	X = np.abs(np.fft.rfft((x - m) * win))
	f = np.fft.rfftfreq(n, 1 / fs)
	band = (f >= f_min) & (f <= 0.95 * fs / 2)
	amp = 2 * X / win.sum() / m				# modulation amplitude per bin
	floor = float(np.median(amp[band]))
	i = int(np.argmax(np.where(band, X, 0)))
	a, b, c = np.log(X[i - 1:i + 2] + 1e-12)
	delta = 0.5 * (a - c) / (a - 2 * b + c)
	f0 = float(f[i] + delta * fs / n)
	raw_pct = float((np.percentile(x, 99.9) - np.percentile(x, 0.1))
		/ (np.percentile(x, 99.9) + np.percentile(x, 0.1)) * 100)
	res = {
		'fs': fs, 'n': n, 'mean_counts': float(m),
		'raw_percent': raw_pct,
		'detect_limit_percent': float(8 * floor * 100),
	}
	if amp[i] < 8 * floor or amp[i] < 0.005:
		res['detected'] = False
		return res
	seg_len = int(max(0.25, 10 / f0) * fs)
	out = [_period_metrics(x[s:s + seg_len], fs, f0)
		for s in range(0, n - seg_len + 1, seg_len)]
	pct, idx = np.median(np.array(out), axis=0)
	res.update(detected=True, f0=f0, percent=float(pct), index=float(idx))
	return res


def ieee1789(f0, pct):
	"""IEEE Std 1789-2015 recommended-practice zones (Mod% vs frequency)."""
	if f0 > 3000:
		return '无影响'
	noe = 0.01 * f0 if f0 < 90 else 0.0333 * f0
	low = 0.025 * f0 if f0 < 90 else 0.08 * f0
	if pct <= noe:
		return '无影响'
	if f0 > 1250 or pct <= low:
		return '低风险'
	return '高风险'

# ------------------------------------------------------------------ output


def bar_chart(spectrum, step=10, width=46):
	s = np.clip(spectrum, 0, None)
	s = s / s.max()
	lines = []
	for wl in range(380, 781, step):
		v = s[wl - 380:wl - 380 + step].mean()
		lines.append(f'  {wl:3d} nm |{"#" * int(round(v * width)):<{width}}| {v:4.2f}')
	return '\n'.join(lines)


def check(ok):
	return '合格' if ok else '不合格'


def judge(col, ren=None, flk=None, target_lux=None):
	"""Reference pass/fail checks -> list of (key, name, ok)."""
	items = []
	if target_lux:
		items.append(('lux', f'照度 ≥ {target_lux:g} lx', col['lux'] >= target_lux))
	if ren:
		items += [
			('ra', 'CRI Ra ≥ 80', ren['Ra'] >= 80),
			('r9', 'R9 > 0', ren['Ri'][9] > 0),
		]
	items += [
		('duv', '|Duv| ≤ 0.006', abs(col['Duv']) <= 0.006),
		('cct', 'CCT ≤ 4000 K（长时间停留场所）', col['CCT'] <= 4000),
	]
	if flk:
		items.append(('flicker', '频闪 IEEE 1789 非高风险',
			not flk['detected'] or flk['ieee1789'] != '高风险'))
	return items


def print_report(meas, col, ren, flk, target_lux):
	p = print
	p('=' * 64)
	p(f'  室内光源质量测试报告   {meas["time"]}')
	p('=' * 64)
	p(f'传感器: AS7343  增益 {meas["gain"]:g}x  积分 {meas["tint_ms"]:.1f} ms'
		f'  平均 {meas["n_avg"]} 帧  标定: ams Golden Device'
		+ (f' × 照度系数 {meas["lux_factor"]:.3f}' if meas['lux_factor'] != 1 else ''))
	if meas['flags']:
		p(f'!! 警告: {", ".join(meas["flags"])}')
	p('\n[光度/色度]')
	p(f'  照度 E          {col["lux"]:9.1f} lx   (XYZ 直接法 {col["lux_direct"]:.1f} lx)')
	p(f'  相关色温 CCT    {col["CCT"]:9.0f} K    (XYZ 直接法 {col["CCT_direct"]:.0f} K)')
	p(f'  Duv             {col["Duv"]:+9.4f}')
	p(f'  CIE 1931 x,y    {col["xy"][0]:.4f}, {col["xy"][1]:.4f}')
	p(f"  CIE 1976 u',v'  {col['uv_prime'][0]:.4f}, {col['uv_prime'][1]:.4f}")
	p('\n[显色性]（由 12 通道重建光谱计算，属估算值）')
	ri = ren['Ri']
	p(f'  CRI Ra          {ren["Ra"]:9.1f}')
	p('  R1-R8           ' + ' '.join(f'{ri[k]:.0f}' for k in range(1, 9)))
	p(f'  R9 (饱和红)     {ri[9]:9.1f}')
	p('  R10-R14         ' + ' '.join(f'{ri[k]:.0f}' for k in range(10, 15)))
	p(f'  TM-30 Rf / Rg   {ren["Rf"]:9.1f} / {ren["Rg"]:.1f}')
	if flk:
		p(f'\n[频闪]  FD 通道 {flk["fs"]:.0f} Hz 采样 {flk["n"] / flk["fs"]:.1f} s,'
			f' 可分析到 {flk["fs"] / 2:.0f} Hz')
		if flk['detected']:
			p(f'  基频            {flk["f0"]:9.1f} Hz')
			p(f'  波动深度 Mod%   {flk["percent"]:9.2f} %   (原始波形 {flk["raw_percent"]:.2f} %)')
			p(f'  频闪指数 FI     {flk["index"]:9.4f}')
			p(f'  IEEE 1789       {flk["ieee1789"]}')
		else:
			p(f'  未检出周期性频闪（检出下限约 {flk["detect_limit_percent"]:.2f} %，'
				f'原始波形峰峰 {flk["raw_percent"]:.2f} %，含量化噪声）')
	p('\n[判定]（参考阈值，见 README）')
	for _, name, ok in judge(col, ren, flk, target_lux):
		p(f'  {check(ok)}  {name}')
	p('\n[重建光谱] 相对辐照度')
	p(bar_chart(col['spectrum']))
	p('=' * 64)


def to_json(meas, col, ren, flk, samples):
	def plain(v):
		if isinstance(v, np.ndarray):
			return v.tolist()
		if isinstance(v, (np.floating, np.integer)):
			return v.item()
		return v
	c = {k: plain(v) for k, v in col.items() if k != 'spectrum'}
	c['spectrum_380_780_1nm'] = plain(col['spectrum'])
	d = {'measurement': meas, 'colorimetry': c, 'rendering': ren,
		'flicker': flk, 'flicker_samples': samples}
	return json.dumps(d, ensure_ascii=False, indent=1)

# ---------------------------------------------------------------- commands


def measure(dev, cal, n_avg):
	raw, gain, tint, flags = dev.measure_spectral(n_avg=n_avg)
	meas = {
		'time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
		'raw': raw, 'gain': gain, 'tint_ms': tint, 'n_avg': n_avg,
		'flags': flags, 'lux_factor': cal['lux_factor'],
	}
	return meas, colorimetry(cal, basic_counts(cal, raw, gain, tint))


def measure_flicker(dev, seconds):
	samples, fs, fs_nom, full, fd_gain = dev.capture_flicker(seconds=seconds)
	flk = analyze_flicker(samples, fs_nom)
	flk.update(fs_measured=fs, fd_gain=fd_gain, full_scale=full)
	if flk['detected']:
		flk['ieee1789'] = ieee1789(flk['f0'], flk['percent'])
	return flk, samples


def cmd_report(dev, cal, args):
	meas, col = measure(dev, cal, args.avg)
	ren = rendering(col['spectrum'])
	flk, samples = None, None
	if not args.no_flicker:
		flk, samples = measure_flicker(dev, args.flicker_s)
	print_report(meas, col, ren, flk, args.target_lux)
	if args.out:
		with open(args.out, 'w') as f:
			f.write(to_json(meas, col, ren, flk, samples))
		print(f'原始数据已保存: {args.out}')


def cmd_monitor(dev, cal, args):
	print('时间       照度(lx)   CCT(K)    Duv     增益')
	try:
		while True:
			meas, col = measure(dev, cal, 1)
			print(f'{meas["time"][11:]}  {col["lux"]:8.1f}  {col["CCT"]:7.0f}  '
				f'{col["Duv"]:+.4f}  {meas["gain"]:g}x {" ".join(meas["flags"])}',
				flush=True)
			time.sleep(args.interval)
	except KeyboardInterrupt:
		pass


def cmd_calibrate(dev, cal, args):
	cal['lux_factor'] = 1.0
	_, col = measure(dev, cal, 8)
	factor = args.ref_lux / col['lux']
	with open(CAL_USER, 'w') as f:
		json.dump({'lux_factor': factor, 'ref_lux': args.ref_lux,
			'sensor_lux': col['lux'], 'CCT': col['CCT'],
			'time': datetime.datetime.now().isoformat(timespec='seconds')}, f, indent=1)
	print(f'传感器 {col["lux"]:.1f} lx, 参考 {args.ref_lux:g} lx -> 照度系数 {factor:.4f}')
	print(f'已写入 {CAL_USER}')


def main():
	ap = argparse.ArgumentParser(description='AS7343 室内光源质量测试')
	sub = ap.add_subparsers(dest='cmd', required=True)
	r = sub.add_parser('report', help='完整测试报告')
	r.add_argument('--out', help='保存原始数据和结果 (JSON)')
	r.add_argument('--target-lux', type=float, help='照度目标值，用于判定')
	r.add_argument('--avg', type=int, default=4, help='光谱平均帧数')
	r.add_argument('--flicker-s', type=float, default=2.0, help='频闪采样时长 s')
	r.add_argument('--no-flicker', action='store_true')
	m = sub.add_parser('monitor', help='连续显示照度/色温')
	m.add_argument('--interval', type=float, default=0.5)
	c = sub.add_parser('calibrate', help='用照度计读数标定照度系数')
	c.add_argument('--ref-lux', type=float, required=True)
	args = ap.parse_args()

	cal = load_cal()
	from as7343 import AS7343
	dev = AS7343()
	{'report': cmd_report, 'monitor': cmd_monitor,
		'calibrate': cmd_calibrate}[args.cmd](dev, cal, args)


if __name__ == '__main__':
	sys.exit(main())
