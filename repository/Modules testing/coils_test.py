from artiq.experiment import *

import numpy as np

class Coils_test(EnvExperiment):
    def build(self):
        self.setattr_device("core")
        self.zotino=self.get_device("zotino0")

        self.setattr_argument("coil_1", NumberValue(default=0))
        self.setattr_argument("coil_2", NumberValue(default=0))



    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()

        self.zotino.init()

       

 

       

        self.zotino.write_dac(1, self.coil_1)  
        self.zotino.write_dac(0, self.coil_2)

        with parallel:
            self.zotino.load()
            self.zotino.load()


        print("Zotino tested successfully!") 