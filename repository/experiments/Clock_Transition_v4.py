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
        self.setattr_argument("Transfer_Time", NumberValue(default=40))
        self.setattr_argument("Holding_Time", NumberValue(default=40))
        self.setattr_argument("Compression_Time", NumberValue(default=8))
        self.setattr_argument("Single_Freq_Time", NumberValue(default=40))
        self.setattr_argument("State_Preparation_Time", NumberValue(default=30))
        self.setattr_argument("Clock_Interrogation_Time", NumberValue(default=300))

        self.setattr_argument("Center_Frequency", NumberValue(unit="MHz", default=79.95e6, precision=4))
        self.setattr_argument("Scan_Range", NumberValue(unit="kHz", default=100e3, precision=4))
        self.setattr_argument("Step_Size", NumberValue(unit="Hz", default=500, precision=4))
        
        self.setattr_argument("sampling_rate", NumberValue(unit="kHz", default=50000))

        

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

        # Initialize the modules
        self.Pixelfly.output()
        self.Camera.output()
        self.BMOT_TTL.output()
        self.Probe_TTL.output()
        self.Zeeman_Slower_TTL.output()
        self.Repump707.output()
        self.MOT_Coil_1.init()
        self.MOT_Coil_2.init()
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
    def blue_mot(self):
        # **************************** Blue MOT Loading ****************************
        self.BMOT_AOM.set(frequency=90*MHz, amplitude=0.08)
        self.ZeemanSlower.set(frequency=180*MHz, amplitude=0.35)
        self.Probe.set(frequency=65*MHz, amplitude=0.02)
        self.Single_Freq.set(frequency=80*MHz, amplitude=0.35)

        global voltage_1, voltage_2
        voltage_1 = 1.02
        voltage_2 = 0.45
        self.MOT_Coil_1.write_dac(0, voltage_1)
        self.MOT_Coil_2.write_dac(1, voltage_2)

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
        global voltage_1_Tr, voltage_2_Tr
        voltage_1_Tr = 4.903
        voltage_2_Tr = 4.027
        self.MOT_Coil_1.write_dac(0, voltage_1_Tr)
        self.MOT_Coil_2.write_dac(1, voltage_2_Tr)
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
        global amp_com
        amp_com = 0.03
        red_freq = 80.0
        red_freq_com = 80.3
        steps_com = self.Compression_Time
        t_com = self.Compression_Time/steps_com
        volt_1_steps = (voltage_1_Tr - voltage_1_com)/steps_com
        volt_2_steps = (voltage_2_Tr - voltage_2_com)/steps_com
        amp_steps = (red_amp-amp_com)/steps_com
        freq_steps = (red_freq_com - red_freq)/steps_com

        with parallel:
            for i in range(int64(steps_com)):
                voltage_1 = voltage_1_Tr - volt_1_steps
                voltage_2 = voltage_2_Tr - volt_2_steps
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
        self.Single_Freq.set(frequency=80.3*MHz, amplitude=amp_com)
        delay(self.Single_Freq_Time*ms)
        self.Single_Freq.sw.off()

    @kernel
    def state_preparation(self):
        # **************************** State Preparation *****************************
        self.MOT_Coil_1.write_dac(0, 7.06) # 5.62/2.24 = 1.80; 7.03/0.45 = 3.5; 4.903/3.1 = 1;
        self.MOT_Coil_2.write_dac(1, 0.45)
        with parallel:
            self.MOT_Coil_1.load()
            self.MOT_Coil_2.load()

        delay(self.State_Preparation_Time*ms)

    @kernel
    def clock_interrogation(self):
        # **************************** Clock Interrogation *****************************
        self.Clock.sw.on()
        self.Clock.set(frequency=start)
        print("Clock Frequency:", start*1e-6, "MHz")
        start += self.Step_Size
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
                        self.Ref.sw.on()

                    delay(0.5*ms)

                    with parallel:
                        self.Camera.off()
                        self.Ref.sw.off()
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

                    with parallel:
                        self.Ref.sw.on()
                        self.Probe.set(frequency=65*MHz, amplitude=0.02)
                    
                    delay(0.5*ms)
                    
                    with parallel:
                        self.Ref.sw.off()
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
                    self.Probe.set(frequency=65*MHz, amplitude=0.00)

                with sequential:
                    for j in range(num_samples):
                        self.sampler.sample(samples[j])
                        delay(sampling_period * s)
                        
            self.Probe_TTL.off()

    @kernel
    def detection_plot(self):
        global detection
        detection = [x[0] for x in samples]
        self.set_dataset("excitation.samples", detection, broadcast=True, archive=True)
        self.set_dataset("excitation.samples_x", [x for x in range(len(detection))], broadcast=True, archive=True)

        self.ccb.issue("create_applet", 
                        "Excitation Plot", 
                        "${artiq_applet}plot_xy"
                        " excitation.samples"
                        " --x excitation.samples_x"
                        " --title Excitation", 
                        group = "excitation"
                    )
        
    @rpc
    def excitation_fraction_list(self):
        ground = np.array(detection[0:250])
        excited = np.array(detection[1200:1400])
        background = np.array(detection[1700:2000])
        ground_avg = ground[ground > 0.8].mean()
        excited_avg = excited[excited > 0.8].mean()
        background_avg = background[background > 0.8].mean()

        print(ground_avg, excited_avg, background_avg)

        return (ground_avg, excited_avg, background_avg)

    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()
        self.initialise()

        # Clock params
        global cycles, start
        cycles = int32(self.Scan_Range/self.Step_Size)
        start = self.Center_Frequency - self.Scan_Range/2

        # Sampler params
        global num_samples, sampling_period, samples
        sample_duration = 0.05  # 50 ms: detection cycle duration ~ 54 ms
        sampling_period = 1/self.sampling_rate
        num_samples = int32(sample_duration / sampling_period)
        samples = [[0.0 for i in range(8)] for i in range(num_samples)]
        excitation_fraction_list = [0.0 for i in range(cycles+1)]

        for j in range(cycles+1):
            delay(500*ms)
            self.blue_mot()
            self.transfer()
            self.broadband_red_mot()
            self.broadband_red_mot_compression()
            self.single_frequency_red_mot()
            self.state_preparation()
            self.clock_interrogation() 
            self.detection()
            self.detection_plot()
            self.excitation_fraction_list()           

        print("Test Complete")