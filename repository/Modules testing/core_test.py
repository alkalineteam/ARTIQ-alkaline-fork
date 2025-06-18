from artiq.experiment import *
from numpy import int64, int64

class TestCore(EnvExperiment):
	def build(self):
		self.setattr_device("core")

		self.setattr_argument("scan_center_frequency_Hz", NumberValue(default=85000000, unit="Hz"))
		self.setattr_argument("scan_range_Hz", NumberValue(default=500000, unit="Hz"))
		self.setattr_argument("scan_step_size_Hz", NumberValue(default=1000,  unit="Hz"))
		self.setattr_argument("rabi_pulse_duration_ms", NumberValue(default= 60,  unit="ms"))
		self.setattr_argument("clock_intensity", NumberValue(default=0.05))
		self.setattr_argument("bias_field_mT", NumberValue(default=3.0))
		self.setattr_argument("blue_mot_loading_time", NumberValue(default=2000,  unit="ms"))

		

	@rpc
	def run(self):
		print("Hello testbed setup")
		print(self.scan_center_frequency_Hz)
		print(type(self.scan_center_frequency_Hz))

		self.scan_start = self.scan_center_frequency_Hz - (self.scan_range_Hz / 2)
		self.scan_end =self.scan_center_frequency_Hz + (self.scan_range_Hz / 2)
		self.scan_frequency_values = [float(x) for x in range(int64(self.scan_start), int64(self.scan_end), int64(self.scan_step_size_Hz))]
		self.cycles = len(self.scan_frequency_values)

		self.gs_list = [0.0] * self.cycles
		self.es_list = [0.0] * self.cycles
		self.excitation_fraction_list = [0.0] * self.cycles
		