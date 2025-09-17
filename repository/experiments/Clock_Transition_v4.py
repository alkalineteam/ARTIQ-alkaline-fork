from artiq.experiment import *
from artiq.coredevice.ttl import TTLOut
from artiq.language.core import delay
from artiq.test.lit.iodelay import sequential
from artiq.coredevice.sampler import Sampler
from artiq.language.units import *
from numpy import int64, int32
import numpy as np

class clock_transition_lookup_v4(EnvExperiment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("ccb")
        self.Camera:TTLOut=self.get_device("ttl10")
        self.Pixelfly:TTLOut=self.get_device("ttl15")
        self.BMOT_TTL:TTLOut=self.get_device("ttl6")
        self.Probe_TTL:TTLOut=self.get_device("ttl8")
        self.Broadband_On:TTLOut=self.get_device("ttl5")
        self.Broadband_Off:TTLOut=self.get_device("ttl7")
        self.Zeeman_Slower_TTL:TTLOut=self.get_device("ttl12")
        self.Repump707:TTLOut=self.get_device("ttl4")
        self.Repump679:TTLOut=self.get_device("ttl9")
        self.BMOT_AOM = self.get_device("urukul1_ch0")
        self.ZeemanSlower=self.get_device("urukul1_ch1")
        self.Single_Freq=self.get_device("urukul1_ch2")
        self.Probe=self.get_device("urukul1_ch3")
        self.Clock=self.get_device("urukul0_ch0")
        self.MOT_Coil_1=self.get_device("zotino0")
        self.MOT_Coil_2=self.get_device("zotino0")
        self.sampler:Sampler = self.get_device("sampler0")
        self.Ref = self.get_device("urukul0_ch3")
        self.ttl:TTLOut=self.get_device("ttl15")

        self.setattr_argument("Loading_Time", NumberValue(default=1500))
        self.setattr_argument("Transfer_Time", NumberValue(default=80))
        self.setattr_argument("Holding_Time", NumberValue(default=80))
        self.setattr_argument("Compression_Time", NumberValue(default=8))
        self.setattr_argument("Single_Freq_Time", NumberValue(default=80))
        self.setattr_argument("State_Preparation_Time", NumberValue(default=30))
        self.setattr_argument("Clock_Interrogation_Time", NumberValue(default=300))

        self.setattr_argument("Center_Frequency", NumberValue(unit="MHz", default=79.95e6, precision=4))
        self.setattr_argument("Scan_Range", NumberValue(unit="kHz", default=100e3, precision=4))
        self.setattr_argument("Step_Size", NumberValue(unit="Hz", default=500, precision=4))
        
        self.setattr_argument("sampling_rate", NumberValue(unit="kHz", default=50000))

        # Initialize class attributes that will be used across methods
        self.voltage_1 = 0.0
        self.voltage_2 = 0.0
        self.voltage_1_Tr = 0.0
        self.voltage_2_Tr = 0.0
        self.amp_com = 0.0

        # Clock params
        self.cycles = int32(self.Scan_Range/self.Step_Size)
        self.start = self.Center_Frequency - self.Scan_Range/2

        # Sampler params
        sample_duration = 0.05  # 50 ms: detection cycle duration ~ 54 ms
        self.sampling_period = 1/self.sampling_rate
        self.num_samples = int32(sample_duration / self.sampling_period)
        
        # Pre-allocate arrays during build phase
        self.samples = [[0.0 for i in range(8)] for i in range(self.num_samples)]
        self.detection_data = [0.0 for i in range(self.num_samples)]
        self.detection_x = [i for i in range(self.num_samples)]
        self.excitation_fraction_list = [0.0 for i in range(self.cycles+1)]
        self.frequencies_MHz = [(self.Center_Frequency - self.Scan_Range/2 + i*self.Step_Size)*1e-6 for i in range(self.cycles+1)]

    @kernel
    def initialise(self):
        # Initialize the modules
        self.Pixelfly.output()
        self.Camera.output()
        self.BMOT_TTL.output()
        self.Probe_TTL.output()
        self.Zeeman_Slower_TTL.output()
        self.Repump707.output()
        self.MOT_Coil_1.init()
        self.MOT_Coil_2.init()
        self.sampler.init()
        self.BMOT_AOM.cpld.init()
        self.BMOT_AOM.init()
        self.ZeemanSlower.cpld.init()
        self.ZeemanSlower.init()
        self.Probe.cpld.init()
        self.Probe.init()
        self.Single_Freq.cpld.init()
        self.Single_Freq.init()
        self.Clock.cpld.init()
        self.Clock.init()

        self.Ref.cpld.init()
        self.Ref.init()

        # Set the RF channels ON
        self.BMOT_AOM.sw.on()
        self.ZeemanSlower.sw.on()
        self.Probe.sw.on()

        # Set the RF attenuation
        self.BMOT_AOM.set_att(0.0)
        self.ZeemanSlower.set_att(0.0)
        self.Probe.set_att(0.0)
        self.Single_Freq.set_att(0.0)
        self.Clock.set_att(0.0)

        self.Ref.set(frequency=80 * MHz)
        self.Ref.set_att(0.0)

    @kernel
    def blue_mot(self, coil_1_voltage, coil_2_voltage):
        # **************************** Blue MOT Loading ****************************
        self.BMOT_AOM.set(frequency=90*MHz, amplitude=0.08)
        self.ZeemanSlower.set(frequency=180*MHz, amplitude=0.35)
        self.Probe.set(frequency=65*MHz, amplitude=0.02)
        self.Single_Freq.set(frequency=80*MHz, amplitude=0.35)

        self.voltage_1 = coil_1_voltage
        self.voltage_2 = coil_2_voltage
        self.MOT_Coil_1.write_dac(0, self.voltage_1)
        self.MOT_Coil_2.write_dac(1, self.voltage_2)

        with parallel:
            self.MOT_Coil_1.load()
            self.MOT_Coil_2.load()
            self.BMOT_TTL.on()
            self.Probe_TTL.off()
            self.Broadband_On.pulse(10*ms)
            self.Single_Freq.sw.off()
            self.Zeeman_Slower_TTL.on()
            self.Repump707.on()
            self.Repump679.on()

        delay(self.Loading_Time*ms)
    
    @kernel
    def transfer(self):
        # **************************** Transfer ****************************
        self.ZeemanSlower.set(frequency=180*MHz, amplitude=0.00)
        self.Zeeman_Slower_TTL.off()
        delay(4.0*ms)

        steps_tr = self.Transfer_Time
        t_tr = self.Transfer_Time/steps_tr

        for i in range(int64(steps_tr)):
            amp_steps = (0.08 - 0.003)/steps_tr
            amp = 0.08 - ((i+1) * amp_steps)
            self.BMOT_AOM.set(frequency=90*MHz, amplitude=amp)
            delay(t_tr*ms)

        delay(200*ms)

        with parallel:
            self.BMOT_TTL.off()
            self.Repump707.off()
            self.Repump679.off()
        delay(4*ms)
        self.BMOT_AOM.set(frequency=90*MHz, amplitude=0.08)
    
    @kernel
    def broadband_red_mot(self):
        # **************************** Broadband Red MOT ****************************
        self.voltage_1_Tr = 4.903
        self.voltage_2_Tr = 4.027
        self.MOT_Coil_1.write_dac(0, self.voltage_1_Tr)
        self.MOT_Coil_2.write_dac(1, self.voltage_2_Tr)
        with parallel:
            self.MOT_Coil_1.load()
            self.MOT_Coil_2.load()

        delay(self.Holding_Time*ms)

    @kernel
    def broadband_red_mot_compression(self):
        # **************************** Br Red MOT Compression ****************************
        with parallel:
            self.Broadband_Off.pulse(10*ms)
            self.Single_Freq.sw.on()

        voltage_1_com = 2.51
        voltage_2_com = 2.23
        red_amp = 0.35
        self.amp_com = 0.03
        red_freq = 80.0
        red_freq_com = 80.3
        steps_com = self.Compression_Time
        t_com = self.Compression_Time/steps_com
        # Fixed: Use self.voltage_1_Tr and self.voltage_2_Tr
        volt_1_steps = (self.voltage_1_Tr - voltage_1_com)/steps_com
        volt_2_steps = (self.voltage_2_Tr - voltage_2_com)/steps_com
        amp_steps = (red_amp-self.amp_com)/steps_com
        freq_steps = (red_freq_com - red_freq)/steps_com

        with parallel:
            for i in range(int64(steps_com)):
                voltage_1 = self.voltage_1_Tr - volt_1_steps
                voltage_2 = self.voltage_2_Tr - volt_2_steps
                self.MOT_Coil_1.write_dac(0, voltage_1)
                self.MOT_Coil_2.write_dac(1, voltage_2)
                with parallel:
                    self.MOT_Coil_1.load()
                    self.MOT_Coil_2.load()
                delay(t_com*ms)

            for i in range(int64(steps_com)):
                amp = red_amp - ((i+1) * amp_steps)
                freq = red_freq + ((i+1) * freq_steps)
                self.Single_Freq.set(frequency=freq*MHz, amplitude=amp)
                delay(t_com*ms)

    @kernel
    def single_frequency_red_mot(self):
        # **************************** Single Frequency Red MOT****************************
        self.Single_Freq.set(frequency=80.3*MHz, amplitude=self.amp_com)
        delay(self.Single_Freq_Time*ms)
        self.Single_Freq.sw.off()

    @kernel
    def state_preparation(self, coil_1_voltage, coil_2_voltage):
        # **************************** State Preparation *****************************
        self.MOT_Coil_1.write_dac(0, coil_1_voltage)
        self.MOT_Coil_2.write_dac(1, coil_2_voltage)
        with parallel:
            self.MOT_Coil_1.load()
            self.MOT_Coil_2.load()

        delay(self.State_Preparation_Time*ms)

    @kernel
    def clock_interrogation(self, j):
        # **************************** Clock Interrogation *****************************
        self.Clock.sw.on()
        self.Clock.set(frequency=self.start)
        print("Clock Frequency:", self.start*1e-6, "MHz, Cycle:", j)
        self.start += self.Step_Size
        delay(self.Clock_Interrogation_Time*ms)
        self.Clock.sw.off()
    
    @kernel
    def detection(self):
        # **************************** Detection *****************************
        # Turn off MOT coils for detection
        self.MOT_Coil_1.write_dac(0, 4.08)
        self.MOT_Coil_2.write_dac(1, 4.11)
        with parallel:
            self.MOT_Coil_1.load()
            self.MOT_Coil_2.load()

        with parallel:
            with sequential:
                # **************************** Ground State ****************************
                self.Probe_TTL.on()
                delay(2.8*ms)

                with parallel:
                    self.Camera.on()
                    self.Probe.set(frequency=65*MHz, amplitude=0.02)

                delay(0.5*ms)

                with parallel:
                    self.Camera.off()
                    self.Probe_TTL.off()
                    self.Probe.set(frequency=65*MHz, amplitude=0.00)
                delay(5*ms)

                # **************************** Repumping ****************************
                with parallel:
                    self.Repump707.pulse(15*ms)
                    self.Repump679.pulse(15*ms)

                self.Probe.set(frequency=65*MHz, amplitude=0.02)
                delay(10*ms)
                self.Probe.set(frequency=65*MHz, amplitude=0.00)

                # **************************** Excited State ****************************
                self.Probe_TTL.on()
                delay(2.8*ms)

                self.Probe.set(frequency=65*MHz, amplitude=0.02)
                
                delay(0.5*ms)
                
                with parallel:
                    self.Probe_TTL.off()
                    self.Probe.set(frequency=65*MHz, amplitude=0.00)
                delay(5*ms)

                self.Probe.set(frequency=65*MHz, amplitude=0.02)
                delay(10*ms)
                self.Probe.set(frequency=65*MHz, amplitude=0.00)

                # **************************** Background ****************************
                self.Probe_TTL.on()
                delay(2.8*ms)

                self.Probe.set(frequency=65*MHz, amplitude=0.02)

                delay(0.5*ms)

                with parallel:
                    self.Probe_TTL.off()
                    self.Probe.set(frequency=65*MHz, amplitude=0.00)

            with sequential:
                for j in range(self.num_samples):
                    self.sampler.sample(self.samples[j])
                    delay(self.sampling_period * s)

        self.Probe.set(frequency=65*MHz, amplitude=0.02)

    @rpc
    def excitation_fraction(self, detection_data, j) -> float:
        ground = np.array(detection_data[0:200])
        excited = np.array(detection_data[1200:1400])
        background = np.array(detection_data[1800:2000])
        baseline = np.array(detection_data[200:1200])

        ground_mean = ground.mean()
        excited_mean = excited.mean()
        background_mean = background.mean()
        baseline_mean = baseline.mean()

        ground_state = ground_mean - baseline_mean
        excited_state = excited_mean - baseline_mean
        background_state = background_mean - baseline_mean

        # Once you have fixed PMT allignment, you don't need baseline mean as you can directly ignore the noise floor and set a threshold
        numerator = excited_state
        denominator = excited_state + ground_state - 2*background_state

        if denominator != 0.0:
            excitation_fraction = numerator / denominator
            if excitation_fraction < 0.0 or excitation_fraction > 1.0:
                excitation_fraction = 0.0
        else:
            excitation_fraction = 0.0
        print(f"Excitation Fraction: {excitation_fraction:.3f}, Cycle: {j}")
        return excitation_fraction

    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()
        self.initialise()

        for j in range(self.cycles+1):
            delay(50*ms)
            self.blue_mot(coil_1_voltage=1.05, coil_2_voltage=0.45)
            self.transfer()
            self.broadband_red_mot()
            self.broadband_red_mot_compression()
            self.single_frequency_red_mot()
            self.state_preparation(coil_1_voltage=6.996, coil_2_voltage=0.4)
            self.clock_interrogation(j) 
            self.detection()
            
            # Detection window
            for i in range(self.num_samples):
                self.detection_data[i] = self.samples[i][0]

            self.set_dataset("excitation.detection", self.detection_data, broadcast=True, archive=True)
            self.set_dataset("excitation.detection_x", self.detection_x, broadcast=True, archive=True)

            # Excitation fraction
            self.excitation_fraction_list[j] = self.excitation_fraction(self.detection_data, j)
            
            self.set_dataset("excitation.excitation_fractions", self.excitation_fraction_list, broadcast=True, archive=True)
            self.set_dataset("excitation.frequencies_MHz", self.frequencies_MHz, broadcast=True, archive=True)
            
            # Plot both
            if j == 0:
                self.ccb.issue(
                            "create_applet", 
                            "Detection Plot", 
                            "${artiq_applet}plot_xy"
                            " excitation.detection"
                            " --x excitation.detection_x"
                            " --title Detection", 
                            group = "excitation"
                          )
                self.ccb.issue(
                            "create_applet", 
                            "Excitation Fraction Plot", 
                            "${artiq_applet}plot_xy"
                            " excitation.excitation_fractions"
                            " --x excitation.frequencies_MHz"
                            " --fit excitation.excitation_fractions"
                            " --title Excitation_Fraction", 
                            group = "excitation"
                          )
        
        print("Test Complete")