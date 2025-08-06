# from turtle import delay
from artiq.experiment import *
from numpy import int64
import time

class cavitydedrift(EnvExperiment):
    def build(self):
        self.setattr_device("core")
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
            delay(10*us)
            self.output_frequency = self.output_frequency + drift_correction
            self.dedrift_aom.set(frequency=self.output_frequency)
            if count % 10000 == 0.0:
                    self.set_dataset("drift_aom_frequency", self.output_frequency, broadcast=True)
            count = count + 1
      
