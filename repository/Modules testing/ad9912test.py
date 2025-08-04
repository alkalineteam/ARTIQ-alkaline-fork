# from turtle import delay
from artiq.experiment import *
from numpy import int64

class TestAD9912(EnvExperiment):
    def build(self):
        self.setattr_device("core")
        self.ad9912_0=self.get_device("urukul1_ch3") 
    
        self.setattr_argument("Number_of_pulse", NumberValue(default=10))
        self.setattr_argument("Pulse_width", NumberValue(default=1000)) 
        self.setattr_argument("attenuation", NumberValue(default=20 *dB))

        self.output_frequency = self.get_dataset("drift_aom_frequency")

    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()

        self.ad9912_0.cpld.init()
        self.ad9912_0.init()
        count = 0
        self.ad9912_0.sw.on()
        
        self.ad9912_0.set_att(self.attenuation * dB)
        drift_correction = 0.000007  # Set this to the desired drift correction value
        # output_frequency = 40 * MHz
        self.ad9912_0.set(frequency=self.output_frequency)

        with parallel:
            while True:
                delay(10*us)
                self.output_frequency = self.output_frequency + drift_correction
                self.ad9912_0.set(frequency=self.output_frequency)
                if count % 10000 == 0.0:
                    self.set_dataset("drift_aom_frequency", self.output_frequency, broadcast=True)
                count = count + 1
        # for i in range(int64(self.Number_of_pulse)):
        #     self.ad9912_0.set(frequency=30 * MHz)
        #     delay(self.Pulse_width*ms)
        
        #     self.ad9912_0.set(frequency=90 * MHz)
        #     delay(self.Pulse_width*ms)

        # self.ad9912_0.sw.off()

        print("AD9912 test is done")
