from artiq.experiment import *
from artiq.coredevice.sampler import *
from artiq.coredevice.ttl import TTLOut
from numpy import int64, int32

class clock_transition_lookup_v3(EnvExperiment):
    def build(self):
        self.setattr_device("core")
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
        self.Sampler=self.get_device("sampler0")
        self.Ref = self.get_device("urukul0_ch3")


        self.setattr_argument("Loading_Time", NumberValue(default=2000))
        self.setattr_argument("Transfer_Time", NumberValue(default=40))
        self.setattr_argument("Holding_Time", NumberValue(default=40))
        self.setattr_argument("Compression_Time", NumberValue(default=8))
        self.setattr_argument("Single_Freq_Time", NumberValue(default=40))
        self.setattr_argument("State_Preparation_Time", NumberValue(default=40))
        self.setattr_argument("Clock_Interrogation_Time", NumberValue(default=50))

        self.setattr_argument("Center_Frequency", NumberValue(default=80.068, precision=4))
        self.setattr_argument("Step_Size", NumberValue(default=500, precision=4))
        self.setattr_argument("Scan_Range", NumberValue(default=100, precision=4)) 


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
        self.Sampler.init()

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
        self.Ref.set_att(10.0)

        # Clock parameters
        step_size = self.Step_Size
        center_freq = self.Center_Frequency
        scan_range = self.Scan_Range
        cycles = int64((scan_range)*1e3/step_size)
        start = center_freq - (cycles/2)*(step_size/1e6)

        # Sampler initialization
        sample_period = 1 / 2500  #10kHz sampling rate should give us enough data points
        sampling_duration = 0.06      #30ms sampling time to allow for all the imaging slices to take place

        num_samples = int32(sampling_duration/sample_period)

        for j in range(cycles + 1):
            # **************************** Slice 1: Loading ****************************
            delay(0.5*ms)
            # blue_amp = 0.08
            self.BMOT_AOM.set(frequency=90 * MHz, amplitude=0.08)
            # self.Broadband_On.pulse(10*ms)
            self.ZeemanSlower.set(frequency=180 * MHz, amplitude=0.35)
            self.Probe.set(frequency= 65 * MHz, amplitude=0.02)
            self.Single_Freq.set(frequency= 80 * MHz, amplitude=0.35)
            
            voltage_1 = 1.02
            voltage_2 = 0.42
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

            # **************************** Slice 2: Transfer ****************************
            self.ZeemanSlower.set(frequency=180 * MHz, amplitude=0.00)
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

            voltage_1_Tr = 4.012
            voltage_2_Tr = 4.027
            self.MOT_Coil_1.write_dac(0, voltage_1_Tr)
            self.MOT_Coil_2.write_dac(1, voltage_2_Tr)
            self.MOT_Coil_1.load()
            self.MOT_Coil_2.load()

            # **************************** Slice 3: Holding ****************************
            delay(self.Holding_Time*ms)

            # **************************** Slice 4: Compression ****************************
            with parallel:
                self.Broadband_Off.pulse(10*ms)
                self.Single_Freq.sw.on()

            voltage_1_com = 2.54
            voltage_2_com = 2.28
            red_amp = 0.35
            amp_com = 0.02
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
                    voltage_1 = voltage_1 - volt_1_steps
                    voltage_2 = voltage_2 - volt_2_steps
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

            # **************************** Slice 5: Single Frequency ****************************
            self.Single_Freq.set(frequency=80.3*MHz, amplitude=amp_com)
            delay(self.Single_Freq_Time*ms)
            self.Single_Freq.sw.off()

            # **************************** Slice 5: State Preparation *****************************
            self.MOT_Coil_1.write_dac(0, 6.9)# 5.56/2.28 = 1.85; 6.9/0.54 = 3.5; 4.9/3.1 = 1;
            self.MOT_Coil_2.write_dac(1, 0.54)
            with parallel:
                self.MOT_Coil_1.load()
                self.MOT_Coil_2.load()

            delay(self.State_Preparation_Time*ms)

            # **************************** Slice 5: Clock Interrogation *****************************
            self.Clock.sw.on()

            self.Clock.set(frequency=start*MHz)
            print("Clock Frequency:", start, "MHz, Cycle:", j)
            start += (step_size/1e6)

            delay(self.Clock_Interrogation_Time*ms)
            self.Clock.sw.off()

            # **************************** Slice 5: Detection : Ground State**************************
            with parallel:
                with sequential:
                    self.MOT_Coil_1.write_dac(0, 4.08)
                    self.MOT_Coil_2.write_dac(1, 4.11)
                                
                    self.Probe_TTL.on()
                    self.BMOT_AOM.set(frequency=10*MHz, amplitude=0.08)
                    delay(2.8 *ms)

                    with parallel:
                        self.Camera.on()
                        self.Pixelfly.on()
                        self.Probe.set(frequency= 65*MHz, amplitude=0.02)
                        self.Ref.sw.on()
                    
                    delay(0.5 *ms)
                    
                    with parallel:
                        self.Pixelfly.off()
                        self.Camera.off()
                        self.Ref.sw.off()
                        self.Probe_TTL.off()
                        self.Probe.set(frequency= 65 * MHz, amplitude=0.00)
                    delay(5*ms)

                    # **************************** Slice 6: Repumping **************************
                    with parallel:
                        self.Repump707.pulse(15*ms)
                        self.Repump679.pulse(15*ms)

                    self.Probe.set(frequency= 65*MHz, amplitude=0.02)
                    delay(10*ms)
                    self.Probe.set(frequency= 65 * MHz, amplitude=0.00)
                    
                    # **************************** Slice 7: Excited State **************************
                    self.Probe_TTL.on()
                    delay(2.8*ms)

                    with parallel:
                        self.Ref.sw.on()
                        self.Probe.set(frequency= 65*MHz, amplitude=0.02)
                    
                    delay(0.5*ms)
                    
                    with parallel:
                        self.Ref.sw.off()
                        self.Probe_TTL.off()
                        self.Probe.set(frequency= 65 * MHz, amplitude=0.00)
                    delay(5*ms)

                    self.Probe.set(frequency= 65*MHz, amplitude=0.02)
                    delay(10*ms)
                    self.Probe.set(frequency= 65 * MHz, amplitude=0.00)

                    # **************************** Slice 7: Background State **************************
                    self.Probe_TTL.on()
                    delay(2.8 *ms)

                    with parallel:
                        self.Ref.sw.on()
                        self.Probe.set(frequency= 65*MHz, amplitude=0.02)

                    delay(0.5 *ms)
                    
                    with parallel:
                        self.Ref.sw.off()
                        self.Probe_TTL.off()
                        self.Probe.set(frequency= 65 * MHz, amplitude=0.00)

                    print("ending Detection Slice")
                with sequential:
                    samples = [[0.0 for i in range(8)] for i in range(num_samples)]
                    for k in range(int32(num_samples)):   
                        # delay(5*us)
                        self.Sampler.sample(samples[k])
                        delay(sample_period*s)
                    delay(sampling_duration*s)

            
            samples_ch0 = [float(s[0]) for s in samples]
        
            print("done sampling")
            self.set_dataset("excitation_fraction", samples_ch0, broadcast=True, archive=True)
            self.set_dataset("samples", [x for x in range(num_samples)], broadcast=True, archive=True)

            self.ccb.issue("create_applet", 
                        "plotting", 
                        "${artiq_applet}plot_xy "
                        "excitation_fraction"
                        "--x samples"
                        "--title Excitation Fraction",
            )
                                    
            # # Split the samples
            # baseline = samples_ch0[0:40]
            # baseline_mean = 0.0
            # gs = samples_ch0[70:130]
            # es = samples_ch0[680:740]
            # bg = samples_ch0[1100:1160]


            # with parallel: 
            #     baseline_sum = 0.0
            #     for x in baseline:
            #         baseline_sum += float(x)
            #         baseline_mean = baseline_sum / len(baseline)

            #     gs_counts = 0.0
            #     es_counts = 0.0
            #     bg_counts = 0.0

            #     measurement_time = 600.0 * sample_period     #set to 600 as each slice size is 600 samples at the moment,
            #                                                 # we should trim this tighter to the peaks to avoid added noise
            #     for val in gs[1:]:
            #         gs_counts += val
            #     for val in es[1:]:
            #         es_counts += val
            #     for val in bg[1:]:
            #         bg_counts += val

            
            # #if we want the PMT to determine atom no, we will probably want photon counts,
            # # will need expected collection efficiency of the telescope,Quantum efficiency etc, maybe use the camera atom no calculation to get this
            
            # with parallel:
            #     gs_measurement = ((gs_counts-baseline_mean)) * measurement_time         #integrates over the slice time to get the total photon counts
            #     es_measurement = ((es_counts-baseline_mean))  * measurement_time
            #     bg_measurement = ((bg_counts-baseline_mean)) * measurement_time

        
                        
            #     #if we want the PMT to determine atom no, we will probably want photon counts,
            #     # will need expected collection efficiency of the telescope,Quantum efficiency etc, maybe use the camera atom no calculation to get this


            #     numerator = es_measurement - bg_measurement
            #     denominator = (gs_measurement - bg_measurement) + (es_measurement - bg_measurement)
            #     if denominator != 0.0:
            #         excitation_fraction = ((numerator / denominator ) )
            #         if excitation_fraction < 0.0:
            #             excitation_fraction = 0.0
            #     else:
            #         excitation_fraction = float(0) # or 0.5 or some fallback value depending on experiment
                
            #     if is_param_1 == True: 
            #         excitation_fraction_list_param_1[j] = float(excitation_fraction)
            #     elif is_param_1 == False:
            #         excitation_fraction_list_param_2[j] = float(excitation_fraction)
                
        

            # delay(500*us)
            # print(excitation_fraction)
            
            # **************************** Slice 4 ****************************
            # delay(4.0*ms)
            self.Probe.set(frequency= 65*MHz, amplitude=0.02)
            self.BMOT_AOM.set(frequency=90*MHz, amplitude=0.08)
            self.Broadband_On.pulse(10*ms)
            delay(100*ms)
        
        print("clock transition scan completed!!")