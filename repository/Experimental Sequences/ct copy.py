from artiq.experiment import *
from artiq.coredevice.ttl import TTLOut
from numpy import int64, int32, max, float64, float32
import numpy as np
from artiq.coredevice import ad9910
import pandas as pd
import os
import csv
from datetime import datetime

default_cfr1 = (
    (1 << 1)    # configures the serial data I/O pin (SDIO) as an input only pin; 3-wire serial programming mode
)
default_cfr2 = (
    (1 << 5)    # forces the SYNC_SMP_ERR pin to a Logic 0; this pin indicates (active high) detection of a synchronization pulse sampling error
    | (1 << 16) # a serial I/O port read operation of the frequency tuning word register reports the actual 32-bit word appearing at the input to the DDS phase accumulator (i.e. not the contents of the frequency tuning word register)
    | (1 << 24) # the amplitude is scaled by the ASF from the active profile (without this, the DDS outputs max. possible amplitude -> cracked AOM crystals)
)

class ct_scan(EnvExperiment):
    """CT 1"""

    def build(self):
        self.setattr_device("core")
        
        self.sampler:Sampler = self.get_device("sampler0")
        #Assign all channels
              #TTLs
        self.blue_mot_shutter:TTLOut=self.get_device("ttl4")
        self.repump_shutter_707:TTLOut=self.get_device("ttl5")
        self.zeeman_slower_shutter:TTLOut=self.get_device("ttl6")
        self.probe_shutter:TTLOut=self.get_device("ttl7")
        self.camera_trigger:TTLOut=self.get_device("ttl8")
        self.clock_shutter:TTLOut=self.get_device("ttl9")
        self.repump_shutter_679:TTLOut=self.get_device("ttl10")

        # self.pmt_shutter:TTLOut=self.get_device("ttl10")
        # self.camera_trigger:TTLOut=self.get_device("ttl11")
        # self.camera_shutter:TTLOut=self.get_device("ttl12")        
        #AD9910
        self.red_mot_aom = self.get_device("urukul0_ch0")
        self.blue_mot_aom = self.get_device("urukul0_ch1")
        self.zeeman_slower_aom = self.get_device("urukul0_ch2")
        self.probe_aom = self.get_device("urukul0_ch3")
        #AD9912
        self.lattice_aom=self.get_device("urukul1_ch0")
        self.stepping_aom=self.get_device("urukul1_ch1")
        self.atom_lock_aom=self.get_device("urukul1_ch2")
               
        
        #Zotino
        self.mot_coil_1=self.get_device("zotino0")
        self.mot_coil_2=self.get_device("zotino0")
        
        self.setattr_argument("scan_center_frequency_Hz", NumberValue(default=85000000 * Hz))
        self.setattr_argument("scan_range_Hz", NumberValue(default=500000 * Hz))
        self.setattr_argument("scan_step_size_Hz", NumberValue(default=1000 * Hz))
        self.setattr_argument("rabi_pulse_duration_ms", NumberValue(default= 60 * ms))
        self.setattr_argument("clock_intensity", NumberValue(default=0.05))
        self.setattr_argument("bias_field_mT", NumberValue(default=3.0))
        self.setattr_argument("blue_mot_loading_time", NumberValue(default=2000 * ms))

        
    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()

        scan_start = self.scan_center_frequency_Hz - (self.scan_range_Hz / 2)
        scan_end =self.scan_center_frequency_Hz + (self.scan_range_Hz / 2)
        scan_frequency_values = [float(x) for x in range(int32(scan_start), int32(scan_end), int32(self.scan_step_size_Hz))]
        # scan_frequency_values = np.arange(scan_start, scan_end, self.scan_step_size_Hz)
        
        cycles = len(scan_frequency_values)

        gs_list = [0.0] * cycles
        es_list = [0.0] * cycles
        excitation_fraction_list = [0.0] * cycles

        # print(len(self.gs_list))

        delay(1000*ms)
        # Initialize the modules
        self.mot_coil_1.init()
        self.mot_coil_2.init()
        #  self.camera_shutter.output()
        self.camera_trigger.output()
        self.blue_mot_shutter.output()
        #  self.red_mot_shutter.output()
        self.zeeman_slower_shutter.output()
        self.repump_shutter_707.output()
        self.repump_shutter_679.output()
        self.probe_shutter.output()
        self.clock_shutter.output()
        #   self.pmt_shutter.output()
        
        self.blue_mot_aom.cpld.init()
        self.blue_mot_aom.init()
        self.zeeman_slower_aom.cpld.init()
        self.zeeman_slower_aom.init()
        self.probe_aom.cpld.init()
        self.probe_aom.init()
        self.red_mot_aom.cpld.init()
        self.red_mot_aom.init()
        self.lattice_aom.cpld.init()
        self.lattice_aom.init()

        # Set the RF channels ON
        self.blue_mot_aom.sw.on()
        self.zeeman_slower_aom.sw.on()
        # self.red_mot_aom.sw.on()
        self.probe_aom.sw.off()
        # self.lattice_aom.sw.on()

        # Set the RF attenuation
        self.blue_mot_aom.set_att(0.0)
        self.zeeman_slower_aom.set_att(0.0)
        self.probe_aom.set_att(0.0)
        self.red_mot_aom.set_att(0.0)

        delay(100*ms)

        print(scan_start, scan_end)

        #Sequence Parameters - Update these with optimised values
        bmot_compression_time = 20 
        blue_mot_cooling_time = 70 
        broadband_red_mot_time = 10
        red_mot_compression_time = 12
        single_frequency_time = 30
        time_of_flight = 0 
        blue_mot_coil_1_voltage = 8.0
        blue_mot_coil_2_voltage = 7.9
        compressed_blue_mot_coil_1_voltage = 8.62
        compressed_blue_mot_coil_2_voltage = 8.39
        bmot_amp = 0.06
        compress_bmot_amp = 0.0035
        bb_rmot_coil_1_voltage = 5.24
        bb_rmot_coil_2_voltage = 5.22
        sf_rmot_coil_1_voltage = 5.72
        sf_rmot_coil_2_voltage = 5.64
        rmot_f_start = 80.6,
        rmot_f_end = 81,
        rmot_A_start = 0.03,
        rmot_A_end = 0.005,


        for j in range(int64(cycles)):        

            ####################################################### Blue MOT loading #############################################################

            self.blue_mot_aom.set(frequency= 90 * MHz, amplitude=0.06)
            self.zeeman_slower_aom.set(frequency= 70 * MHz, amplitude=0.08)

            self.blue_mot_aom.sw.on()
            self.zeeman_slower_aom.sw.on()
        
            self.mot_coil_1.write_dac(0, blue_mot_coil_1_voltage)
            self.mot_coil_2.write_dac(1, blue_mot_coil_2_voltage)

            with parallel:
                self.mot_coil_1.load()
                self.mot_coil_2.load()
                self.blue_mot_shutter.on()
                self.probe_shutter.off()
                self.zeeman_slower_shutter.on()
                self.repump_shutter_707.on()
                self.repump_shutter_679.on()

            
                self.red_mot_aom.set(frequency = 80.45 * MHz, amplitude = 0.08)
                self.red_mot_aom.sw.on()



            delay(self.blue_mot_loading_time * ms)

            ####################################################### Blue MOT compression & cooling ########################################################

            self.zeeman_slower_aom.set(frequency=70 * MHz, amplitude=0.00)   #Turn off the Zeeman Slower
            self.zeeman_slower_shutter.off()
            self.red_mot_aom.sw.on()
            delay(4.0*ms)                                                 #wait for shutter to close

            steps_com = bmot_compression_time 
            t_com = bmot_compression_time/steps_com
            volt_1_steps = (compressed_blue_mot_coil_1_voltage - blue_mot_coil_1_voltage)/steps_com
            volt_2_steps = (compressed_blue_mot_coil_2_voltage - blue_mot_coil_2_voltage )/steps_com
            amp_steps = (bmot_amp - compress_bmot_amp)/steps_com
        
            for i in range(int64(steps_com)):

                voltage_1 = blue_mot_coil_1_voltage + ((i+1) * volt_1_steps)
                voltage_2 = blue_mot_coil_2_voltage + ((i+1) * volt_2_steps)
                amp = bmot_amp - ((i+1) * amp_steps)

                self.mot_coil_1.write_dac(0, voltage_1)
                self.mot_coil_2.write_dac(1, voltage_2)

                with parallel:
                    self.mot_coil_1.load()
                    self.mot_coil_2.load()
                    self.blue_mot_aom.set(frequency=90*MHz, amplitude=amp)
                
                delay(t_com*ms)

            delay(bmot_compression_time*ms)    #Blue MOT compression time


            delay(blue_mot_cooling_time*ms)   #Allowing further cooling of the cloud by just holding the atoms here

            ########################################################### BB red MOT #################################################################

            self.blue_mot_aom.set(frequency=90*MHz,amplitude=0.00)   
            self.blue_mot_aom.sw.off()                                   #Switch off blue beams
            self.repump_shutter_679.off()
            self.repump_shutter_707.off()
            self.blue_mot_shutter.off()
            delay(3.9*ms)

            self.mot_coil_1.write_dac(0, bb_rmot_coil_1_voltage)
            self.mot_coil_2.write_dac(1, bb_rmot_coil_2_voltage)

            with parallel:
                self.mot_coil_1.load()
                self.mot_coil_2.load()

            delay(broadband_red_mot_time*ms)

            self.red_mot_aom.set(frequency = 80.55 *MHz, amplitude = 0.06)

            delay(5*ms)



            ########################################################### red MOT compression & Single Frequency ####################################################################


            start_freq = rmot_f_start
            end_freq = rmot_f_end


            bb_rmot_amp = rmot_A_start
            compress_rmot_amp= rmot_A_end

            
            step_duration = 0.1
            steps_com = int(red_mot_compression_time / step_duration)  

            freq_steps = (start_freq - end_freq)/steps_com

            volt_1_steps = (sf_rmot_coil_1_voltage - bb_rmot_coil_1_voltage)/steps_com
            volt_2_steps = (sf_rmot_coil_2_voltage - bb_rmot_coil_2_voltage)/steps_com


            amp_steps = (bb_rmot_amp-compress_rmot_amp)/steps_com
            

            for i in range(int64(steps_com)):
                voltage_1 = bb_rmot_coil_1_voltage + ((i+1) * volt_1_steps)
                voltage_2 = bb_rmot_coil_2_voltage + ((i+1) * volt_2_steps)
                amp = bb_rmot_amp - ((i+1) * amp_steps)
                freq = start_freq - ((i+1) * freq_steps)

                self.mot_coil_1.write_dac(0, voltage_1)
                self.mot_coil_2.write_dac(1, voltage_2)

                with parallel:
                    self.mot_coil_1.load()
                    self.mot_coil_2.load()
                    self.red_mot_aom.set(frequency = freq * MHz, amplitude = amp)
                    
                
                delay(step_duration*ms)

            delay(red_mot_compression_time*ms)

            delay(single_frequency_time*ms)

            self.red_mot_aom.sw.off()

            # self.seperate_probe(
            #     tof = 50,
            #     probe_duration = 1* ms ,
            #     probe_frequency= 205 * MHz
            # )

 

            ################################################################### Clock Spectroscopy ##################################################################################

            delay(40*ms)

            self.red_mot_aom.sw.off()
            self.stepping_aom.sw.off()

            comp_field = 1.35 * 0.14    # comp current * scaling factor from measurement
            bias_at_coil = (self.bias_field_mT - comp_field)/ 0.914   #bias field dips in center of coils due to geometry, scaling factor provided by modelling field
            current_per_coil = ((bias_at_coil) / 2.0086) / 2   
            coil_1_voltage = current_per_coil + 5.0
            coil_2_voltage = 5.0 - (current_per_coil / 0.94 )           #Scaled against coil 1
        
        
            #Switch to Helmholtz
            self.mot_coil_1.write_dac(0, coil_1_voltage)  
            self.mot_coil_2.write_dac(1, coil_2_voltage)
            
            with parallel:
                self.mot_coil_1.load()
                self.mot_coil_2.load()

            # self.pmt_shutter.on()
            # self.camera_shutter.on()
            self.clock_shutter.on()    

            delay(20*ms)  #wait for coils to switch

            #rabi spectroscopy pulse
            self.stepping_aom.set(frequency = scan_frequency_values[j] * Hz)
            self.stepping_aom.sw.on()
            delay(self.rabi_pulse_duration_ms*ms)
            self.stepping_aom.sw.off()
            self.stepping_aom.set(frequency = 0 * Hz)
            self.stepping_aom.sw.off()
            print("AOM freq:", scan_frequency_values[j])



            ################### Detection ####################

            # sample_period = 1 / 40000     #10kHz sampling rate should give us enough data points
            # sampling_duration = 0.06      #30ms sampling time to allow for all the imaging slices to take place

            # num_samples = int32(sampling_duration/sample_period)    #2400
            # samples = [[0.0 for i in range(8)] for i in range(num_samples)]
        
            # with parallel:
            #     with sequential:
                    ##########################Ground State###############################
                    
            with parallel:
                self.blue_mot_aom.sw.off()
                self.probe_shutter.on()

            self.mot_coil_1.write_dac(0, 5.0)   #Set 0 field 
            self.mot_coil_2.write_dac(1, 5.0)

            with parallel:
                self.mot_coil_1.load()
                self.mot_coil_2.load()

            delay(4.1*ms)     #wait for shutter to open

            with parallel:
                self.camera_trigger.pulse(1*ms)
                self.probe_aom.set(frequency=205 * MHz, amplitude=0.18)
                self.probe_aom.sw.on()

            delay(0.8* ms)      #Ground state probe duration            
            
            with parallel:
                self.probe_shutter.off()
                self.probe_aom.sw.off()

            delay(10*ms)     

                    # delay(5*ms)                         #repumping 
                
                    # with parallel:
                    #     self.repump_shutter_679.pulse(10*ms)
                    #     self.repump_shutter_707.pulse(10*ms)

                    # delay(10*ms)                         #repumping 

                    # # ###############################Excited State##################################

                    # self.probe_shutter.on()
                    # delay(4.1*ms) 

                    # self.probe_aom.sw.on()
                    # delay(0.8*ms)            #Ground state probe duration
                    # self.probe_aom.sw.off()
                    # # self.probe_shutter.off()
                    # delay(20*ms)
                    # # self.probe_shutter.on()
                    # # delay(4.1*ms)


                    # #  ########################Background############################
    
                    # self.probe_aom.sw.on()
                    # delay(0.2*ms)            #Ground state probe duration
                    # self.probe_aom.sw.off()
                    # self.probe_shutter.off()
                    # delay(7*ms)
                    
                # with sequential:
                #     print("1") 
                #     for k in range(num_samples):
                #         self.sampler.sample(samples[k])
                #         delay(sample_period*s)
                #     print("2") 
                
            # delay(sampling_duration*s)

            # samples_ch0 = [i[0] for i in samples]
            

            # self.set_dataset("excitation_fraction", samples_ch0, broadcast=True, archive=True)


            # gs = samples_ch0[0:600]
            # es = samples_ch0[600:1300]
            # bg = samples_ch0[1300:2000]

            # gs_max = gs[0]
            # es_max = es[0]
            # bg_max = bg[0]

            # Loop through the rest of the list

            # with parallel:
            #     for num in gs[1:]:
            #         if num > gs_max:
            #             gs_max = num

            #     for num in es[1:]:
            #         if num > es_max:
            #             es_max = num

            #     for num in bg[1:]:
            #         if num > bg_max:
            #             bg_max = num

            #     if es_max < bg_max:
            #         es_max = bg_max

            # numerator = es_max - bg_max
            # denominator = (gs_max - bg_max) + (es_max - bg_max)

            # if denominator != 0:
            #     excitation_fraction = numerator / denominator
            # else:
            #     excitation_fraction = float(0) # or 0.5 or some fallback value depending on experiment
            
            # gs_list[j] = float(gs_max)
            # es_list[j] = float(es_max)
            # excitation_fraction_list[j] = float(excitation_fraction)
            # print(excitation_fraction)

            # delay(10*ms)
            
            # delay(50*ms)

            # print(excitation_fraction_list)

            # self.set_dataset("excitation_fraction_list", excitation_fraction_list, broadcast=True, archive=True)
            # print(excitation_fraction_list[0:cycles])

            # dataset = [scan_frequency_values, excitation_fraction_list, gs_list, es_list]


            # self.set_dataset("Clock",dataset, broadcast=True, archive = True)


        print("Experiment complete!")

    
        
    # # @kernel 
    # # def seperate_probe(self,tof,probe_duration,probe_frequency):
    # #         with parallel:
    # #             self.red_mot_aom.sw.off()
    # #             self.blue_mot_aom.sw.off()
    # #             self.repump_shutter_679.off()
    # #             self.repump_shutter_707.off()
    # #             self.probe_shutter.on()

    # #         self.mot_coil_1.write_dac(0, 5.0)  
    # #         self.mot_coil_2.write_dac(1, 5.0)
           
    # #         with parallel:
    # #             self.mot_coil_1.load()
    # #             self.mot_coil_2.load()

    # #         delay(((tof +3.9)*ms))

    # #         with parallel:
    # #                 self.camera_trigger.pulse(2*ms)
    # #                 self.probe_aom.set(frequency=205 *MHz, amplitude=0.18)
    # #                 self.probe_aom.sw.on()
                    
    # #         delay(probe_duration)
                    
    # #         with parallel:
    # #             self.probe_shutter.off()
    # #               #Camera shutter takes 26ms to open so we will open it here
    # #             self.probe_aom.set(frequency=probe_frequency, amplitude=0.00)
    # #             self.probe_aom.sw.off()

    # #         delay(10*ms)

          

    

        
