# from turtle import delay
from artiq.experiment import *
from numpy import int64
import time

class cavitydedrift(EnvExperiment):
    def build(self):
        self.setattr_device("core")
      
        #AD9910
        self.red_mot_aom = self.get_device("urukul0_ch0")
        self.blue_mot_aom = self.get_device("urukul0_ch1")
        self.zeeman_slower_aom = self.get_device("urukul0_ch2")
        self.probe_aom = self.get_device("urukul0_ch3")
        #AD9912
        self.lattice_aom=self.get_device("urukul1_ch0")
        self.stepping_aom=self.get_device("urukul1_ch1")
        self.atom_lock_aom=self.get_device("urukul1_ch2")
        self.dedrift_aom=self.get_device("urukul1_ch3") 
    
        self.setattr_argument("Hz_s_correction", NumberValue(default=0.7))
        self.setattr_argument("attenuation", NumberValue(default=20 *dB))

        self.output_frequency = self.get_dataset("drift_aom_frequency")

    @kernel
    def run(self):
        self.core.reset()

        self.core.break_realtime()

        self.dedrift_aom.cpld.init()
        self.dedrift_aom.init()
      
        count = 0
        self.dedrift_aom.sw.on()

        self.dedrift_aom.set_att(self.attenuation * dB)
        drift_correction = self.Hz_s_correction * 1e-5# for 10us delay, 1e-6 for 1us delay
      
        self.dedrift_aom.set(frequency=self.output_frequency)

        while True:
            delay(14.3*ms)
            self.output_frequency = self.output_frequency + (0.01 *Hz)
            self.dedrift_aom.set(frequency=self.output_frequency)
            if count % 10000 == 0.0:
                    self.set_dataset("drift_aom_frequency", self.output_frequency, broadcast=True)
            count = count + 1
      
