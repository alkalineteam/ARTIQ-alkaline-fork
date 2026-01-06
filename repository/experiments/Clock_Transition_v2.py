from artiq.coredevice.core import Core
from artiq.coredevice.ad9910 import AD9910
from artiq.coredevice.ad9912 import AD9912
from artiq.coredevice.urukul import CPLD
from artiq.coredevice.zotino import Zotino
from artiq.coredevice.ttl import TTLOut
from artiq.coredevice.sampler import Sampler
from artiq.experiment import EnvExperiment
from artiq.experiment import kernel
from artiq.experiment import NumberValue
from artiq.experiment import parallel, sequential
from artiq.experiment import rpc
from artiq.language.core import delay
from artiq.language.units import ms, MHz
class clock_transition_lookup_v2(EnvExperiment):
    def build(self):
        self.core: Core = self.get_device("core")
        self.setattr_device("ccb")
        self.cpld: CPLD = self.get_device("urukul0_cpld")
        self.Camera:TTLOut=self.get_device("ttl10")
        self.Pixelfly:TTLOut=self.get_device("ttl15")
        self.BMOT_TTL:TTLOut=self.get_device("ttl6")
        self.Probe_TTL:TTLOut=self.get_device("ttl8")
        self.Broadband_On:TTLOut=self.get_device("ttl5")
        self.Broadband_Off:TTLOut=self.get_device("ttl7")
        self.Zeeman_Slower_TTL:TTLOut=self.get_device("ttl12")
        self.clock_shutter:TTLOut=self.get_device("ttl4")
        self.Repump679:TTLOut=self.get_device("ttl9")
        self.BMOT_AOM:AD9910 = self.get_device("urukul1_ch0")
        self.ZeemanSlower:AD9910 = self.get_device("urukul1_ch1")
        self.Single_Freq:AD9910 = self.get_device("urukul1_ch2")
        self.Probe:AD9910 = self.get_device("urukul1_ch3")
        self.Clock:AD9912 = self.get_device("urukul0_ch0")
        self.Clock_Feedback:AD9912 = self.get_device("urukul0_ch1")
        self.MOT_Coil_1:Zotino = self.get_device("zotino0")
        self.MOT_Coil_2:Zotino = self.get_device("zotino0")
        self.sampler:Sampler = self.get_device("sampler0")
        self.Ref:AD9912 = self.get_device("urukul0_ch3")

        self.setattr_argument("Loading_Time", NumberValue(default=1500))
        self.setattr_argument("Transfer_Time", NumberValue(default=80))
        self.setattr_argument("Holding_Time", NumberValue(default=80))
        self.setattr_argument("Compression_Time", NumberValue(default=8))
        self.setattr_argument("Single_Freq_Time", NumberValue(default=80))
        self.setattr_argument("State_Preparation_Time", NumberValue(default=30))
        self.setattr_argument("Clock_Interrogation_Time", NumberValue(default=300))

        self.setattr_argument("Center_Frequency", NumberValue(default=79.42, precision=4))
        self.setattr_argument("Step_Size", NumberValue(default=500, precision=4))
        self.setattr_argument("Scan_Range", NumberValue(default=100, precision=4))
    
    @kernel
    def probe_init(self, camera: bool):
        self.Probe.set(frequency=65*MHz, amplitude=0.02)
        delay(5*ms)
        self.Probe.set(frequency=65*MHz, amplitude=0.00)
        self.Probe_TTL.on()
        delay(3.0*ms)

        if camera:
            with parallel:
                self.Camera.on()
                self.Probe.set(frequency=65*MHz, amplitude=0.02)
        else:
            self.Probe.set(frequency=65*MHz, amplitude=0.02)

        delay(0.5*ms)

        if camera:
            with parallel:
                self.Camera.off()
                self.Probe.set(frequency=65*MHz, amplitude=0.00)
                self.Probe_TTL.off()
        else:
            with parallel:
                self.Probe.set(frequency=65*MHz, amplitude=0.00)
                self.Probe_TTL.off()


    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()

        # Initialize the modules
        self.Pixelfly.output()
        self.Camera.output()
        self.BMOT_TTL.output()
        self.Probe_TTL.output()
        self.Zeeman_Slower_TTL.output()
        self.clock_shutter.output()
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
        self.Clock_Feedback.cpld.init()
        self.Clock_Feedback.init()

        self.Ref.cpld.init()
        self.Ref.init()

        # Set the RF channels ON
        self.BMOT_AOM.sw.on()
        self.ZeemanSlower.sw.on()
        self.Probe.sw.on()
        self.Clock.sw.on()
        self.Clock_Feedback.sw.on()

        # Set the RF attenuation
        self.BMOT_AOM.set_att(0.0)
        self.ZeemanSlower.set_att(0.0)
        self.Probe.set_att(0.0)
        self.Single_Freq.set_att(0.0)
        self.Clock.set_att(0.0)
        self.Clock_Feedback.set_att(0.0)

        self.Clock_Feedback.set(frequency=66*MHz)
        self.Ref.set(frequency=80*MHz)
        self.Ref.set_att(0.0)

        # Clock parameters
        cycles = int((self.Scan_Range)*1e3/self.Step_Size)
        start = self.Center_Frequency - (cycles/2)*(self.Step_Size/1e6)

        # Sampler params
        sample_duration = 70  #60 ms detection window
        sampling_period = 0.04 #in ms = 25 kHz
        num_samples = int(sample_duration / sampling_period)

        # Pre-allocate arrays
        samples = [[0.0 for i in range(8)] for _ in range(num_samples)]
        excitation_fraction_list = [0.0 for _ in range(cycles+1)]
        frequencies_MHz = [start + i * self.Step_Size / 1e6 for i in range(cycles + 1)]

        for j in range(cycles + 1):
            # **************************** Slice 1: Loading ****************************
            delay(100*ms)
            self.BMOT_AOM.set(frequency=90 * MHz, amplitude=0.08)
            self.ZeemanSlower.set(frequency=180 * MHz, amplitude=0.35)
            self.Probe.set(frequency= 65 * MHz, amplitude=0.02)
            self.Single_Freq.set(frequency= 80 * MHz, amplitude=0.35)
            
            voltage_1 = 1.045
            voltage_2 = 0.547
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
                self.clock_shutter.off()
                self.Repump679.on()

            delay(self.Loading_Time*ms)

            # **************************** Slice 2: Transfer ****************************
            self.ZeemanSlower.set(frequency=180 * MHz, amplitude=0.00)
            self.Zeeman_Slower_TTL.off()
            delay(4.0*ms)

            steps_tr = self.Transfer_Time
            t_tr = self.Transfer_Time/steps_tr

            for i in range(int(steps_tr)):
                amp_steps = (0.08 - 0.003)/steps_tr
                amp = 0.08 - ((i+1) * amp_steps)
                self.BMOT_AOM.set(frequency=90*MHz, amplitude=amp)
                delay(t_tr*ms)

            delay(200*ms)

            with parallel:
                self.BMOT_TTL.off()
                self.Repump679.off()

            delay(4*ms)
            self.BMOT_AOM.set(frequency=90*MHz, amplitude=0.08)

            voltage_1_Tr = 4.012
            voltage_2_Tr = 4.027
            self.MOT_Coil_1.write_dac(0, voltage_1_Tr)
            self.MOT_Coil_2.write_dac(1, voltage_2_Tr)
            with parallel:
                self.MOT_Coil_1.load()
                self.MOT_Coil_2.load()

            # **************************** Slice 3: Holding ****************************
            delay(self.Holding_Time*ms)

            # **************************** Slice 4: Compression ****************************
            with parallel:
                self.Broadband_Off.pulse(10*ms)
                self.Single_Freq.sw.on()

            voltage_1_com = 2.585
            voltage_2_com = 2.286
            red_amp = 0.35
            amp_com = 0.03
            red_freq = 75.0
            red_freq_com = 75.3
            steps_com = self.Compression_Time
            t_com = self.Compression_Time/steps_com
            volt_1_steps = (voltage_1_Tr - voltage_1_com)/steps_com
            volt_2_steps = (voltage_2_Tr - voltage_2_com)/steps_com
            amp_steps = (red_amp-amp_com)/steps_com
            freq_steps = (red_freq_com - red_freq)/steps_com

            with parallel:
                for i in range(int(steps_com)):
                    voltage_1 = voltage_1 - volt_1_steps
                    voltage_2 = voltage_2 - volt_2_steps
                    self.MOT_Coil_1.write_dac(0, voltage_1)
                    self.MOT_Coil_2.write_dac(1, voltage_2)
                    with parallel:
                        self.MOT_Coil_1.load()
                        self.MOT_Coil_2.load()
                    delay(t_com*ms)

                for i in range(int(steps_com)):
                    amp = red_amp - ((i+1) * amp_steps)
                    freq = red_freq + ((i+1) * freq_steps)
                    self.Single_Freq.set(frequency=freq*MHz, amplitude=amp)
                    delay(t_com*ms)

            # **************************** Slice 5: Single Frequency ****************************
            self.Single_Freq.set(frequency=75.3*MHz, amplitude=amp_com)
            delay(self.Single_Freq_Time*ms)
            self.Single_Freq.sw.off()

            # **************************** Slice 5: State Preparation *****************************
            self.MOT_Coil_1.write_dac(0, 7.035)# 4.7/3.32 = 0.8; 4.898/3.14 = 1; 5.07/2.93 = 1.2; 5.64/2.265 = 1.85; 7.169/0.435 = 3.5;
            self.MOT_Coil_2.write_dac(1, 0.566)
            with parallel:
                self.MOT_Coil_1.load()
                self.MOT_Coil_2.load()

            delay(self.State_Preparation_Time*ms)

            # **************************** Slice 5: Clock Interrogation *****************************
            self.clock_shutter.on()
            delay(4*ms)

            self.Clock.set(frequency=start*MHz)
            print("Clock Frequency:", start, "MHz, Cycle:", j)
            start+=(self.Step_Size/1e6)

            delay(self.Clock_Interrogation_Time*ms)

            self.clock_shutter.off()
            delay(4*ms)

            # **************************** Detection **************************
            self.MOT_Coil_1.write_dac(0, 4.08)
            self.MOT_Coil_2.write_dac(1, 4.11)
            with parallel:
                self.MOT_Coil_1.load()
                self.MOT_Coil_2.load()            

            self.BMOT_AOM.set(frequency=10*MHz, amplitude=0.08)

            with parallel:
                with sequential:
                    # **************************** Ground State **************************
                    self.probe_init(camera=True)                      
                    delay(5*ms)

                    # ***************************** Repumping ****************************
                    self.Repump679.pulse(30*ms)

                    # *************************** Excited State **************************
                    self.probe_init(camera=False)
                    delay(20*ms)

                    # ************************* Background State *************************
                    self.probe_init(camera=False)
                    delay(5*ms)

                with sequential:
                    for k in range(num_samples):
                        self.sampler.sample(samples[k])
                        delay(sampling_period * ms)
                
            # **************************** Slice 4 ****************************
            self.Probe.set(frequency=65*MHz, amplitude=0.02)
            self.BMOT_AOM.set(frequency=90*MHz, amplitude=0.08)
            self.Single_Freq.set(frequency=80*MHz, amplitude=0.35)
            self.Broadband_On.pulse(10*ms)
            delay(100*ms)

            detection = [i[0] for i in samples]

            self.set_dataset("excitation.detection", detection, broadcast=True, archive=True)
            self.ccb.issue("create_applet", 
                        "PMT Detection", 
                        "${artiq_applet}plot_xy"
                        " excitation.detection"
                        " --title PMT_detection", 
                        group = "excitation"
                    )
            
            # shutter 3.0ms delay
            # probe 0.5ms delay
            ground_state = detection[164:175]
            excited_state = detection[1053:1064]
            background = detection[1634:1645]

            # # shutter 3.0ms delay
            # # probe 1.0ms delay
            # ground_state = detection[164:185]
            # excited_state = detection[1063:1084]
            # background = detection[1655:1676]

            gs_sum = 0.0
            for _ in ground_state:
                gs_sum+=_
            
            es_sum = 0.0
            for _ in excited_state:
                es_sum+=_

            bg_sum = 0.0
            for _ in background:
                bg_sum+=_

            gs_avg = gs_sum/len(ground_state)
            es_avg = es_sum/len(excited_state)
            bg_avg = bg_sum/len(background)
            print("GS avg:", gs_avg, ", ES avg:", es_avg, ", BG avg:", bg_avg)

            numerator = es_avg - bg_avg
            denominator = es_avg + gs_avg - 2*bg_avg

            # excitation_fraction = min(max(numerator / denominator if denominator != 0.0 else 0.0, 0.0), 1.0)
            excitation_fraction = numerator / denominator
            print("Excitation Fraction:", excitation_fraction, ", Cycle:", j)

            excitation_fraction_list[j] = excitation_fraction

            self.set_dataset("excitation.excitation_fraction_list", excitation_fraction_list, broadcast=True, archive=True)
            self.set_dataset("excitation.frequencies_MHz", frequencies_MHz, broadcast=True, archive=True)

            self.ccb.issue("create_applet", 
                        "Excitation Fraction Plot", 
                        "${artiq_applet}plot_xy"
                        " excitation.excitation_fraction_list"
                        # " --x excitation.frequencies_MHz"
                        " --title Excitation_Fraction", 
                        group = "excitation"
                    )

        print("clock transition scan completed!!")