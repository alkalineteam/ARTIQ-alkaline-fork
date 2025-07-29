from artiq.experiment import *
import numpy as np
import random


class Datasets(EnvExperiment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("ccb")
        
    def prepare(self):
        # Perform numpy operations here (outside the kernel)
        self.x = np.linspace(0, 2, 2000)
        self.y = np.sin(2 * np.pi * 5 * self.x)
        
        self.a = {random.randint(3, 9)}
        self.b = {random.randint(3, 9)}
        print("Hello UoB!")
    
    def analyse(self):
        pass

    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()        

        self.set_dataset("UoB2.dat_x", self.x, archive=True, broadcast=True)
        self.set_dataset("UoB2.dat_y", self.y, archive=True, broadcast=True)
        
        
        self.analyse()

        self.ccb.issue("create_applet", 
                       "plotting", 
                       "${artiq_applet}plot_xy "
                       "dat_y "
                       "--x dat_x "
                       "--title Sineeee", 
                       group = "UoB2"
        )