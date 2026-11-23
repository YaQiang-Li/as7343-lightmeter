"""AS7343 driver (I2C, smbus2).

Register references: ams-OSRAM AS7343 datasheet DS001046 v6-00.
"""
import math
import time

from smbus2 import SMBus, i2c_msg

ADDR = 0x39
PART_ID = 0x81

# registers (bank 0 unless noted)
REG_ID = 0x5A			# bank 1
REG_ENABLE = 0x80
REG_ATIME = 0x81
REG_STATUS2 = 0x90
REG_ASTATUS = 0x94		# reading it latches DATA_0..17 (0x95..0xB8)
REG_STATUS4 = 0xBC
REG_CFG0 = 0xBF			# bit4 REG_BANK
REG_CFG1 = 0xC6			# AGAIN[4:0]
REG_ASTEP = 0xD4		# 16 bit, LSB first
REG_CFG20 = 0xD6		# bit7 FD_FIFO_8b, bits6:5 auto_smux
REG_AZ_CONFIG = 0xDE
REG_FD_CFG0 = 0xDF		# bit7 FIFO_WRITE_FD, bits6:0 keep 0b0100001
REG_FD_TIME_1 = 0xE0
REG_FD_TIME_2 = 0xE2	# FD_GAIN[7:3], FD_TIME[10:8]
REG_CONTROL = 0xFA		# bit3 SW_RESET, bit1 FIFO_CLR
REG_FIFO_MAP = 0xFC
REG_FIFO_LVL = 0xFD
REG_FDATA = 0xFE

PON, SP_EN, FDEN = 0x01, 0x02, 0x40
AVALID = 0x40
FIFO_OV = 0x80
T_STEP = 2.78e-6		# s

# DATA_n index of each channel in automatic 18-channel mode (CFG20)
#   cycle 1: FZ FY FXL NIR VIS FD, cycle 2: F2 F3 F4 F6 VIS FD,
#   cycle 3: F1 F7 F8 F5 VIS FD
DATA_INDEX = {
	'FZ': 0, 'FY': 1, 'FXL': 2, 'NIR': 3,
	'F2': 6, 'F3': 7, 'F4': 8, 'F6': 9,
	'F1': 12, 'F7': 13, 'F8': 14, 'F5': 15,
	'VIS': 4, 'FD': 5,
}
SPECTRAL = ['F1', 'F2', 'FZ', 'F3', 'F4', 'FY', 'F5', 'FXL', 'F6', 'F7', 'F8', 'NIR']

# 100 ms is a whole number of 100 Hz (50 Hz mains) and 120 Hz ripple periods
TINT_STEPS = 35971		# ASTEP + 1, ATIME = 0  ->  99.999 ms, full scale 35971
GAIN_MIN, GAIN_MAX = 0, 10	# AGAIN code: gain = 0.5 * 2**code; >512x has no
							# ams gain correction data, so it is not used
FD_GAIN_MAX = 10			# >=1024x read back as 0 on the test unit


def gain_of(code):
	return 0.5 * 2 ** code


def next_gain(code, peak, lo, hi):
	"""AGAIN code that moves the channel peak (fraction of full scale) back
	into [lo, hi], aiming at 0.6."""
	if peak > 0.9:
		new = code - 3					# clipped: real level unknown
	elif lo <= peak <= hi:
		return code
	else:
		new = code + int(math.floor(math.log2(0.6 / max(peak, 1e-4))))
	return max(GAIN_MIN, min(GAIN_MAX, new))


class AS7343:
	def __init__(self, bus=1):
		self.bus = SMBus(bus)
		self._w(REG_CONTROL, 0x08)		# SW_RESET
		time.sleep(0.01)
		self._w(REG_CFG0, 0x10)
		pid = self._r(REG_ID, 1)[0]
		self._w(REG_CFG0, 0x00)
		if pid != PART_ID:
			raise RuntimeError(f'AS7343 not found (ID=0x{pid:02x})')
		self._w(REG_ENABLE, PON)
		self.tint_ms = TINT_STEPS * T_STEP * 1e3
		self.full_scale = TINT_STEPS
		self._live, self._live_code = False, 7

	def _w(self, reg, val):
		self.bus.write_byte_data(ADDR, reg, val)

	def _r(self, reg, n):
		wr, rd = i2c_msg.write(ADDR, [reg]), i2c_msg.read(ADDR, n)
		self.bus.i2c_rdwr(wr, rd)
		return bytes(rd)

	# ---- spectral ----

	def _start_spectral(self, gain_code):
		self._w(REG_ENABLE, PON)
		self._w(REG_ATIME, 0)
		self._w(REG_ASTEP, (TINT_STEPS - 1) & 0xFF)
		self._w(REG_ASTEP + 1, (TINT_STEPS - 1) >> 8)
		self._w(REG_CFG1, gain_code)
		self._w(REG_CFG20, 0x60)		# automatic 18 channel
		self._w(REG_ENABLE, PON | SP_EN)

	def _read_frame(self, timeout=3.0):
		t0 = time.monotonic()
		while not self._r(REG_STATUS2, 1)[0] & AVALID:
			if time.monotonic() - t0 > timeout:
				raise TimeoutError('spectral measurement timeout')
			time.sleep(0.01)
		d = self._r(REG_ASTATUS, 37)
		raw = [d[1 + 2 * i] | d[2 + 2 * i] << 8 for i in range(18)]
		return {ch: raw[i] for ch, i in DATA_INDEX.items()}, d[0]

	def measure_spectral(self, n_avg=4):
		"""Auto-ranged, averaged 12-channel raw counts.

		Returns (raw dict, gain value, tint_ms, flags list).
		"""
		code = 7
		flags = []
		self._live = False
		for _ in range(8):
			self._start_spectral(code)
			self._read_frame()				# first frame after a change: drop
			raw, _ = self._read_frame()
			peak = max(raw[ch] for ch in SPECTRAL) / self.full_scale
			new = next_gain(code, peak, 0.3, 0.6)
			if new == code:
				break
			code = new
		acc = {ch: 0.0 for ch in DATA_INDEX}
		for _ in range(n_avg):
			raw, _ = self._read_frame()
			if any(raw[ch] >= self.full_scale * 0.98 for ch in SPECTRAL):
				flags.append('saturated')
			for ch in acc:
				acc[ch] += raw[ch] / n_avg
		self._w(REG_ENABLE, PON)
		if max(acc[ch] for ch in SPECTRAL) < 0.02 * self.full_scale:
			flags.append('low_signal')
		return acc, gain_of(code), self.tint_ms, sorted(set(flags))

	def read_continuous(self):
		"""One frame of free-running measurement, for live display.

		The engine keeps running between calls; gain is only changed when
		the peak leaves 10..90 % of full scale.  Returns (raw, gain,
		tint_ms, flags) like measure_spectral, with n_avg = 1.  After an
		I2C error the next call restarts the engine.
		"""
		try:
			if not self._live:
				self._live = True
				self._start_spectral(self._live_code)
				self._read_frame()
			for _ in range(8):
				raw, _ = self._read_frame()
				peak = max(raw[ch] for ch in SPECTRAL) / self.full_scale
				new = next_gain(self._live_code, peak, 0.1, 0.9)
				if new == self._live_code:
					break
				self._live_code = new
				self._start_spectral(new)
				self._read_frame()			# first frame after a change: drop
		except (OSError, TimeoutError):
			self._live = False
			raise
		flags = []
		if any(raw[ch] >= self.full_scale * 0.98 for ch in SPECTRAL):
			flags.append('saturated')
		if peak < 0.02:
			flags.append('low_signal')
		return raw, gain_of(self._live_code), self.tint_ms, flags

	# ---- flicker (FD photodiode, raw samples through the FIFO) ----

	def _capture_fd(self, fd_time, gain_code, seconds):
		self._live = False
		self._w(REG_ENABLE, PON)
		self._w(REG_AZ_CONFIG, 255)		# auto zero only before first cycle
		self._w(REG_FD_TIME_1, fd_time & 0xFF)
		self._w(REG_FD_TIME_2, gain_code << 3 | (fd_time >> 8) & 0x07)
		self._w(REG_CFG20, 0x80)			# 8 bit FD samples
		self._w(REG_FD_CFG0, 0x80 | 0x21)
		self._w(REG_FIFO_MAP, 0x00)
		self._w(REG_CONTROL, 0x02)			# FIFO_CLR
		self._w(REG_ENABLE, PON | FDEN)
		time.sleep(0.02)
		self._w(REG_CONTROL, 0x02)			# drop start-up samples
		buf = bytearray()
		overflow = False
		t0 = time.monotonic()
		while True:
			lvl = self._r(REG_FIFO_LVL, 1)[0]
			if lvl:
				buf += self._r(REG_FDATA, lvl * 2)
			if self._r(REG_STATUS4, 1)[0] & FIFO_OV:
				overflow = True
			t = time.monotonic() - t0
			if t >= seconds:
				break
			time.sleep(0.01)
		self._w(REG_ENABLE, PON)
		return bytes(buf), len(buf) / t, overflow

	def capture_flicker(self, fd_time=255, seconds=2.0):
		"""Auto-ranged FD waveform.

		Returns (samples list, measured sample rate, nominal sample rate,
		full scale, gain value).
		"""
		# A saturated FD sample reads 0 on this part, and a PWM light may
		# legitimately read 0 in its off phase.  Climb from the lowest gain
		# and step back once the share of zero samples grows.
		full = min(255, fd_time + 1)
		code, zeros = 0, None
		while True:
			s, _, _ = self._capture_fd(fd_time, code, 0.15)
			z = s.count(0) / max(1, len(s))
			if zeros is not None and z > zeros + 0.01:
				code -= 1
				break
			if max(s) > 0.4 * full or code == FD_GAIN_MAX:
				break
			zeros = z
			code += 1
		s, fs, ov = self._capture_fd(fd_time, code, seconds)
		if ov:
			raise RuntimeError('FD FIFO overflow, I2C too slow')
		fs_nom = 1.0 / ((fd_time + 1) * T_STEP)
		return list(s), fs, fs_nom, full, gain_of(code)
