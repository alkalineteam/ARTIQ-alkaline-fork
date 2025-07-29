from artiq.experiment import *
from artiq.coredevice.sampler import *
import numpy as np
import random


class PMTtest(EnvExperiment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("ccb")
        self.setattr_device("sampler")

        self.setattr_argument("sample_number", NumberValue(precision=3, default=0))
        self.setattr_argument("sample_interval", NumberValue(precision=3, default=0))

        self.n = int(self.sample_number) #Total data point to sample
        self.dt = self.sample_interval # time interval between samples, in ms unit
        self.sample_array = [0.0] * self.n
        self.sample_time = [0.0] * self.n
        self.sample_present = [0.0] * 8
        self.snrerr = [0.0] * self.n

        
    # def prepare(self):
    #     # Perform numpy operations here (outside the kernel)
    #     self.x = np.linspace(0, 2, 2000)
    #     self.y = np.sin(2 * np.pi * 5 * self.x)
        
    #     self.a = {random.randint(3, 9)}
    #     self.b = {random.randint(3, 9)}
    #     print("Hello UoB!")
    

    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()    
        self.sampler.init()

        self.set_dataset("test.dat_x", self.sample_time, archive=True, broadcast=True)
        self.set_dataset("test.dat_y", self.sample_array, archive=True, broadcast=True)
        
        self.ccb.issue("create_applet", 
                       "plotting", 
                       "${artiq_applet}plot_xy "
                       "dat_y "
                       "--x dat_x "
                       "--title PMTtest", 
                       group = "test"
        )