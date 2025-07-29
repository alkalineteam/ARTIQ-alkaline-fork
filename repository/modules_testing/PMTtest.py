from artiq.experiment import *
from artiq.coredevice.sampler import *
import numpy as np
from numpy import int32, int64
import random


class PMTtest(EnvExperiment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("ccb")
        self.setattr_device("sampler")

        self.setattr_argument("sample_number", NumberValue(precision=3, default=0))
        self.setattr_argument("sampling_rate", NumberValue(precision=3, default=0))
        

    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()    
        self.sampler.init()

        num_samples = int32(self.sample_number)
        samples = [[0.0 for i in range(8)] for i in range(num_samples)]
        sampling_period = 1/self.sampling_rate

        for i in range(num_samples):
            self.sampler.sample(samples[i])
            delay(sampling_period * s)

        sample2 = [i[0] for i in samples]
        self.set_dataset("samples", sample2, broadcast=True, archive=True)
        
        self.ccb.issue("create_applet", 
                       "plotting", 
                       "${artiq_applet}plot_x "
                    #    "dat_y "
                       "--x dat_x "
                       "--title PMTtest", 
                    #    group = "test"
        )