from artiq.experiment import *
from artiq.coredevice.ttl import TTLOut
from artiq.language.core import delay
from numpy import int64, int32, max, float64, float32
import numpy as numpy
import numpy as np
from scipy.optimize import curve_fit
from artiq.coredevice import ad9910
import os
import csv
from datetime import datetime

"""
Author: Jordan Wayland
Last Updated: 2025-06-28
Description:
    Drift-insensitive self-comparison (DISC) clock lock loop for Sr-88 using ARTIQ.
    Evaluates the Quadratic Zeeman Shift by interleaving bias field 
    and corrects AOM frequencies based on excitation fractions.
    

    DISC Method Citation: 
      Zhou, C.; Lu, X.; Lu, B.;
    Wang, Y.; Chang, H. Demonstration
    of the Systematic Evaluation of an
    Optical Lattice Clock Using the
    Drift-Insensitive Self-Comparison
    Method. Appl. Sci. 2021, 11, 1206.
    https://doi.org/10.3390/app11031206

"""

class quad_zeeman_shift_disc(EnvExperiment):

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
        
        self.setattr_argument("high_bias_field_mT", NumberValue(default=5),group="Shift Parameters")
        self.setattr_argument("low_bias_field_mT", NumberValue(default=2.3),group="Shift Parameters")
        self.setattr_argument("rabi_pulse_duration_ms_param_1", NumberValue(default= 60 * ms), group="Shift Parameters")
        self.setattr_argument("rabi_pulse_duration_ms_param_2", NumberValue(default= 60 * ms), group="Shift Parameters")
        self.setattr_argument("scan_center_frequency_Hz", NumberValue(default=85000000 * Hz),group="Scan Parameters",)
        self.setattr_argument("scan_range_Hz", NumberValue(default=500000 * Hz), group="Scan Parameters")
        self.setattr_argument("scan_step_size_Hz", NumberValue(default=1000 * Hz), group="Scan Parameters")
        self.setattr_argument("clock_intensity", NumberValue(default=0.05), group="Locking")
        self.setattr_argument("blue_mot_loading_time", NumberValue(default=2000 * ms), group="Sequence Parameters")
        self.setattr_argument("Enable_Lock", BooleanValue(default=False), group="Locking")
        self.setattr_argument("servo_gain_1", NumberValue(default=0.3), group="Locking")
        self.setattr_argument("linewidth_1", NumberValue(default=100 * Hz), group="Locking")  # This is the linewidth of the clock transition, adjust as necessary
        self.setattr_argument("servo_gain_2", NumberValue(default=0.3), group="Locking")  # Added new servo gain parameter
        self.setattr_argument("linewidth_2", NumberValue(default=100 * Hz), group="Locking")  # Added new linewidth parameter

        self.feedback_list = []
        self.atom_lock_list = []
        self.error_log_list = []
        # scan_start = int32(self.scan_center_frequency_Hz - (int32(self.scan_range_Hz )/ 2))
        # scan_end =int32(self.scan_center_frequency_Hz + (int32(self.scan_range_Hz ) / 2))
        # self.scan_frequency_values = [float(x) for x in range(scan_start, scan_end, int32(self.scan_step_size_Hz))]
        # self.cycles = len(self.scan_frequency_values)

        # self.gs_list = [0.0] * self.cycles
        # self.es_list = [0.0] * self.cycles
        # self.excitation_fraction_list = [0.0] * self.cycles

    @kernel
    def initialise_modules(self):
            
        delay(1000*ms)

        # Initialize the modules
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
        self.mot_coil_1.init()
        self.mot_coil_2.init()
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
        self.atom_lock_aom.init()
        self.atom_lock_aom.cpld.init()

        self.atom_lock_aom.set(frequency = 61 * MHz)
        self.atom_lock_aom.set_att(26*dB)

        # Set the RF channels ON
        self.blue_mot_aom.sw.on()
        self.zeeman_slower_aom.sw.on()
        self.atom_lock_aom.sw.on()
        # self.red_mot_aom.sw.on()
        self.probe_aom.sw.off()
        # self.lattice_aom.sw.on()

        # Set the RF attenuation
        self.blue_mot_aom.set_att(0.0)
        self.zeeman_slower_aom.set_att(0.0)
        self.probe_aom.set_att(0.0)
        self.red_mot_aom.set_att(0.0)

        delay(100*ms)

        # scan_start = int32(self.scan_center_frequency_Hz - (int32(self.scan_range_Hz )/ 2))
        # scan_end =int32(self.scan_center_frequency_Hz + (int32(self.scan_range_Hz ) / 2))
        # self.scan_frequency_values = [float(x) for x in range(scan_start, scan_end, int32(self.scan_step_size_Hz))]
        # self.cycles = len(self.scan_frequency_values)

        # self.gs_list = [0.0] * self.cycles
        # self.es_list = [0.0] * self.cycles
        # self.excitation_fraction_list = [0.0] * self.cycles

    @kernel
    def clock_spectroscopy(self,aom_frequency,pulse_time,clock_intensity):                     #Switch to Helmholtz field, wait, then generate Rabi Pulse
       
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

        delay(40*ms)  #wait for coils to switch

        #rabi spectroscopy pulse
        self.stepping_aom.set(frequency = aom_frequency )
        self.stepping_aom.set_att(clock_intensity)
        self.stepping_aom.sw.on()
        delay(pulse_time*ms)
        self.stepping_aom.sw.off()
        self.stepping_aom.set(frequency = 0 * Hz)
        self.stepping_aom.sw.off()
   
    @kernel
    def normalised_detection(self,j,is_param_1,excitation_fraction_list_param_1,excitation_fraction_list_param_2):        #This function should be sampling from the PMT at the same time as the camera being triggered for seperate probe
        self.core.break_realtime()
        sample_period = 1 / 25000   #10kHz sampling rate should give us enough data points
        sampling_duration = 0.06      #30ms sampling time to allow for all the imaging slices to take place

        num_samples = int32(sampling_duration/sample_period)
        samples = [[0.0 for i in range(8)] for i in range(num_samples)]
    
        with parallel:
    
            with sequential:
                ##########################Ground State###############################
                
                with parallel:
                    self.blue_mot_aom.sw.off()
                    self.probe_shutter.on()

                self.mot_coil_1.write_dac(0, 5.0)   #Set 0 field 
                self.mot_coil_2.write_dac(1, 5.0)

                with parallel:
                    self.mot_coil_1.load()
                    self.mot_coil_2.load()

                delay(3.9*ms)     #wait for shutter to open

                with parallel:
                    self.camera_trigger.pulse(1*ms)
                    
                    self.probe_aom.set(frequency=205 * MHz, amplitude=0.18)
                self.probe_aom.sw.on()
                delay(1* ms)      #Ground state probe duration                           
                self.probe_aom.sw.off()
                self.probe_shutter.off()
                
                delay(5*ms)                         #repumping 
               
                with parallel:
                    self.repump_shutter_679.pulse(10*ms)
                    self.repump_shutter_707.pulse(10*ms)

                delay(12*ms)                         #repumping 

                # ###############################Excited State##################################

                self.probe_shutter.on()
                delay(4.1*ms) 

                self.probe_aom.sw.on()
                delay(1*ms)            #Ground state probe duration
                self.probe_aom.sw.off()
                # self.probe_shutter.off()
    
                delay(22*ms)

                # self.probe_shutter.on()
                # delay(4.1*ms)
                #########################Background############################
 
                self.probe_aom.sw.on()
                delay(1*ms)            #Ground state probe duration
                self.probe_aom.sw.off()
                self.probe_shutter.off()

                delay(7*ms)
                
            with sequential:
                self.core.break_realtime()
                for k in range(num_samples):   
                    delay(5*us)
                    self.sampler.sample(samples[k])
                    delay(sample_period*s)
                
                delay(sampling_duration*s)

        samples_ch0 = [float(i[0]) for i in samples]
        

        self.set_dataset("excitation_fraction", samples_ch0, broadcast=True, archive=True)

        # print(self.excitation_fraction(samples_ch0))
                                 
        #     # Split the samples
        baseline = samples_ch0[0:40]
        baseline_mean = 0.0
        gs = samples_ch0[70:130]
        es = samples_ch0[680:740]
        bg = samples_ch0[1100:1160]


        with parallel: 
            baseline_sum = 0.0
            for x in baseline:
                baseline_sum += float(x)
                baseline_mean = baseline_sum / len(baseline)

            gs_counts = 0.0
            es_counts = 0.0
            bg_counts = 0.0

            measurement_time = 600.0 * sample_period     #set to 600 as each slice size is 600 samples at the moment,
                                                         # we should trim this tighter to the peaks to avoid added noise
            for val in gs[1:]:
                gs_counts += val
            for val in es[1:]:
                es_counts += val
            for val in bg[1:]:
                bg_counts += val

        
        #if we want the PMT to determine atom no, we will probably want photon counts,
        # will need expected collection efficiency of the telescope,Quantum efficiency etc, maybe use the camera atom no calculation to get this
        
        with parallel:
            gs_measurement = ((gs_counts-baseline_mean)) * measurement_time         #integrates over the slice time to get the total photon counts
            es_measurement = ((es_counts-baseline_mean))  * measurement_time
            bg_measurement = ((bg_counts-baseline_mean)) * measurement_time

    
                    
            #if we want the PMT to determine atom no, we will probably want photon counts,
            # will need expected collection efficiency of the telescope,Quantum efficiency etc, maybe use the camera atom no calculation to get this


            numerator = es_measurement - bg_measurement
            denominator = (gs_measurement - bg_measurement) + (es_measurement - bg_measurement)
            if denominator != 0.0:
                excitation_fraction = ((numerator / denominator ) )
                if excitation_fraction < 0.0:
                    excitation_fraction = 0.0
            else:
                excitation_fraction = float(0) # or 0.5 or some fallback value depending on experiment
            
            if is_param_1 == True: 
                excitation_fraction_list_param_1[j] = float(excitation_fraction)
            elif is_param_1 == False:
                excitation_fraction_list_param_2[j] = float(excitation_fraction)
            
    

        delay(500*us)
        return excitation_fraction
        delay(25*ms)
        # ef.append(self.excitation_fraction_list)
 
    def fit_lorentzian(self, xdata, ydata):
        """Fit a Lorentzian function to the data and return the fit curve and parameters."""
        def lorentzian(x, a, x0, gamma):
            return a * gamma**2 / ((x - x0)**2 + gamma**2)

        xdata = np.array(xdata)
        ydata = np.array(ydata) 

        # Initial guesses
        a_guess = np.max(ydata)
        x0_guess = xdata[np.argmax(ydata)]
        gamma_guess = 50 # rough width

        try:
            popt, pcov = curve_fit(lorentzian, xdata, ydata, p0=[a_guess, x0_guess, gamma_guess])
            fit_curve = lorentzian(xdata, *popt)
            return fit_curve.astype(np.float64), float(popt[0]), float(popt[1]), float(popt[2]), popt, pcov
        except Exception as e:
            print("Fit failed:", str(e))
            return np.zeros_like(xdata), 0.0, 0.0, 0.0, [], []
        
    @rpc
    def analyse_fit(self, scan_frequency_values, excitation_fraction_list):
        fit_curve, amplitude, center, width, popt, pcov = self.fit_lorentzian(scan_frequency_values, excitation_fraction_list)

        fit_curve = np.array(fit_curve, dtype=np.float64)
        fit_params = np.array([amplitude, center, width], dtype=np.float64)

        self.set_dataset("fit_result", fit_curve, broadcast=True, archive=True)
        self.set_dataset("fit_params", fit_params, broadcast=True, archive=True)
    @rpc
    def correction_log(self,value):
        self.feedback_list.append(61000000 - value)
        self.set_dataset("feedback_list", self.feedback_list, broadcast=True, archive=True)

    @rpc 
    def error_log(self,which_param,value):
        """log of the error in the clock frequency"""
        if which_param == 1: 
            self.error_log_list_1.append(value)
            self.set_dataset("error_log_param_1", self.error_log_list_1, broadcast=True, archive=True)
        elif which_param == 2:
            self.error_log_list_2.append(value)
            self.set_dataset("error_log_param_2", self.error_log_list_2, broadcast=True, archive=True)

    @rpc 
    def param_shift_log(self,value):
        self.param_log_list.append(value)
        """Logging of the shift in the clock frequency from the changing parameters"""
        self.set_dataset("param log", self.param_log_list, unit = Hz ,broadcast=True, archive=True)

    @rpc
    def atom_lock_ex_log(self,which_param,value):
        """all of the excitation fractions from both individual loops and together"""
        if which_param == 1: 
            self.lock_ex_list_1.append(value)
            self.set_dataset("lock_excitation_fraction_param_1", self.lock_ex_list_1, broadcast=True, archive=True)
        elif which_param == 2: 
            self.lock_ex_list_2.append(value)
            self.set_dataset("lock_excitation_fraction_param_2", self.lock_ex_list_2, broadcast=True, archive=True)
        self.lock_ex_list_main.append(value)
        self.set_dataset("lock_excitation_fraction_both", self.lock_ex_list_main, broadcast=True, archive=True)


    @kernel
    def run_sequence(self,j,param,stepping_aom_freq,rabi_pulse_duration,which_param,excitation_fraction_list_param_1,excitation_fraction_list_param_2 ):
        bmot_compression_time = 20 
        blue_mot_cooling_time = 60 
        broadband_red_mot_time = 10
        red_mot_compression_time = 7
        single_frequency_time = 30
        time_of_flight = 0 
        bmot_voltage_1 = 8.0
        bmot_voltage_2 = 7.9
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
        rmot_A_start = 0.05,
        rmot_A_end = 0.0025,

        is_param_1 = False

        if which_param == 1:
            is_param_1 = True
        elif which_param == 2:
            is_param_1 = False

        ################################# Blue MOT #########################################

        delay(500*us)
        self.blue_mot_aom.set(frequency= 90 * MHz, amplitude=0.06)
        self.zeeman_slower_aom.set(frequency= 70 * MHz, amplitude=0.08)

        self.blue_mot_aom.sw.on()
        self.zeeman_slower_aom.sw.on()
    
        self.mot_coil_1.write_dac(0, bmot_voltage_1)
        self.mot_coil_2.write_dac(1, bmot_voltage_2)

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
        delay(self.blue_mot_loading_time* ms)

        ############################ Blue MOT Compression and Cooling #################################

        self.zeeman_slower_aom.set(frequency=70 * MHz, amplitude=0.00)   #Turn off the Zeeman Slower
        self.zeeman_slower_shutter.off()
        self.red_mot_aom.sw.on()
        delay(4.0*ms)                                                 #wait for shutter to close

        steps_com = bmot_compression_time 
        t_com = bmot_compression_time/steps_com
        volt_1_steps = (compressed_blue_mot_coil_1_voltage - bmot_voltage_1)/steps_com
        volt_2_steps = (compressed_blue_mot_coil_2_voltage - bmot_voltage_2 )/steps_com
        amp_steps = (bmot_amp-compress_bmot_amp)/steps_com
    
        for i in range(int64(steps_com)):

            voltage_1 = bmot_voltage_1 + ((i+1) * volt_1_steps)
            voltage_2 = bmot_voltage_2 + ((i+1) * volt_2_steps)
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

        ########################################### BB Red MOT ###############################################
        self.blue_mot_aom.set(frequency=90*MHz,amplitude=0.00)   
        self.blue_mot_aom.sw.off()                                   #Switch off blue beams
        self.repump_shutter_679.off()
        self.repump_shutter_707.off()
        self.blue_mot_shutter.off()
        delay(3.9*ms)

        self.mot_coil_1.write_dac(0,  bb_rmot_coil_1_voltage)
        self.mot_coil_2.write_dac(1, bb_rmot_coil_2_voltage)

        with parallel:
            self.mot_coil_1.load()
            self.mot_coil_2.load()
    
        delay(broadband_red_mot_time*ms)

        self.red_mot_aom.set(frequency = 80.55 *MHz, amplitude = 0.05)

        delay(5*ms)

        ######################################## Red MOT Compression & SF ##########################################
        step_duration = 0.1
        steps_com = int(red_mot_compression_time / step_duration)  

        freq_steps = (rmot_f_start - rmot_f_end)/steps_com

        volt_1_steps = (sf_rmot_coil_1_voltage - bb_rmot_coil_1_voltage)/steps_com
        volt_2_steps = (sf_rmot_coil_2_voltage - bb_rmot_coil_2_voltage)/steps_com


        amp_steps = (rmot_A_start - rmot_A_end)/steps_com
        

        for i in range(int64(steps_com)):
            voltage_1 = bb_rmot_coil_1_voltage + ((i+1) * volt_1_steps)
            voltage_2 = bb_rmot_coil_2_voltage + ((i+1) * volt_2_steps)
            amp = rmot_A_start - ((i+1) * amp_steps)
            freq = rmot_f_start - ((i+1) * freq_steps)

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

        ####################################### Clock Spectroscopy ############################################
        self.clock_spectroscopy(
            aom_frequency = stepping_aom_freq,
            pulse_time = rabi_pulse_duration,
            clock_intensity = self.clock_intensity    
        )

        excitation = self.normalised_detection(j,is_param_1,excitation_fraction_list_param_1,excitation_fraction_list_param_2)           
        delay(40*ms)
        self.set_dataset("excitation_fraction_list", excitation_fraction_list_param_1, broadcast=True, archive=True)
        return excitation 

    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()

        self.initialise_modules()

        scan_start = int32(self.scan_center_frequency_Hz - (int32(self.scan_range_Hz )/ 2))
        scan_end = int32(self.scan_center_frequency_Hz + (int32(self.scan_range_Hz ) / 2))
        scan_frequency_values = [float(x) for x in range(scan_start, scan_end, int32(self.scan_step_size_Hz))]
        cycles = len(scan_frequency_values)

        excitation_fraction_list_param_1 = [0.0] * cycles
        excitation_fraction_list_param_2 = [0.0] * cycles
        

        ############################### Scan Parameter 1: Low Bias Field ##############################
        for j in range(int32(cycles)):        
            self.run_sequence(j,
                self.high_bias_field_mT,   
                scan_frequency_values[j],
                self.rabi_pulse_duration_param_1,
                1,
                excitation_fraction_list_param_1,
                excitation_fraction_list_param_2     
            )  

        ############################### Scan Parameter 2: High Bias Field ###############################
        for j in range(int32(cycles)):        
            self.run_sequence(j,
                self.high_bias_field_mT,                    #parameter 2
                scan_frequency_values[j],  #stepping aom values
                self.rabi_pulse_duration_param_2,
                2,                         #parameter marker
                excitation_fraction_list_param_1,
                excitation_fraction_list_param_2    
            )  

        #process data and do fit from the scan

        self.analyse_fit(scan_frequency_values, excitation_fraction_list_param_1)
        self.analyse_fit(scan_frequency_values, excitation_fraction_list_param_2)

        # from the excitation fraction list we need to manually extract the peak height and center_frequency. 

        max_val_1 = excitation_fraction_list_param_1[0]
        max_idx_1 = 0
        max_val_2 = excitation_fraction_list_param_2[0]
        max_idx_2 = 0

        # Loop through the lists
        for i in range(1, len(excitation_fraction_list_param_1)):
            if excitation_fraction_list_param_1[i] > max_val_1:
                max_val_1 = excitation_fraction_list_param_1[i]
                max_idx_1 = i
            if excitation_fraction_list_param_2[i] > max_val_2:
                max_val_2 = excitation_fraction_list_param_2[i]
                max_idx_2 = i

        # Assign contrast and center frequency
        contrast_1 = max_val_1
        center_frequency_1 = scan_frequency_values[max_idx_1]

        contrast_2 = max_val_2
        center_frequency_2 = scan_frequency_values[max_idx_2]


        param_shift = center_frequency_2 - center_frequency_1
        

        delay(1*ms)

        # if atom lock is enabled as True, then begin new while loop which will run the clock sequence but steps the stepping_aom by half the linewidth
        if self.Enable_Lock == True:

            self.core.break_realtime()                                       # How many seconds there are in a month
            count = 0
            feedback_aom_frequency_1 = 61.0 * MHz
            feedback_aom_frequency_2 = feedback_aom_frequency_1 + param_shift
            
            delay(10*ms)
            
            while True:
                self.core.break_realtime()
                ### Insert entire sequence again
                t1 = self.core.get_rtio_counter_mu()

                #In the DISC method, we interleave between Parameter 1 and Parameter 2 in a P1 P2 P2 P1 order rather than P1 P2 P1 P2, therefore the correction is generated every 4 clock cycles. 
                

                self.atom_lock_aom.set(frequency = feedback_aom_frequency_1)
                p_1_low = self.run_sequence(0,
                    self.low_bias_field_mT,                    #parameter 2
                    center_frequency_1 - self.linewidth_1/2,  #stepping aom values
                    self.rabi_pulse_duration_param_1,
                    1,
                    excitation_fraction_list_param_1,
                    excitation_fraction_list_param_2    
                ) 
                self.atom_lock_aom.set(frequency = feedback_aom_frequency_2)
                p_2_low = self.run_sequence(0,
                   self.high_bias_field_mT,                    #parameter 2
                    center_frequency_1 - self.linewidth_2/2,  #stepping aom values
                    self.rabi_pulse_duration_param_2,
                    2,
                    excitation_fraction_list_param_1,
                    excitation_fraction_list_param_2    
                ) 
                p_2_high = self.run_sequence(0,
                    self.high_bias_field_mT,                  #parameter 2
                    center_frequency_1 - self.linewidth_2/2,  #stepping aom values
                    2,
                    self.rabi_pulse_duration_param_2,             
                    excitation_fraction_list_param_1,
                    excitation_fraction_list_param_2    
                )
                self.atom_lock_aom.set(frequency = feedback_aom_frequency_1)
                p_1_high = self.run_sequence(0,
                    self.low_bias_field_mT,                    #parameter 2
                    center_frequency_1 + self.linewidth_1/2,  #stepping aom values
                    self.rabi_pulse_duration_param_1,
                    1,
                    excitation_fraction_list_param_1,
                    excitation_fraction_list_param_2    
                )

                delta_f1 = (self.servo_gain_1 * (p_1_high - p_1_low) * self.linewidth_1 ) / 2 * contrast_1        #Scaling into Hz
                delta_f2 = (self.servo_gain_2 * (p_2_high - p_2_low) * self.linewidth_2) / 2 * contrast_2

                feedback_aom_frequency_1 = feedback_aom_frequency_1 + delta_f1
                feedback_aom_frequency_2 = feedback_aom_frequency_2 + delta_f2
                param_shift = feedback_aom_frequency_2 - feedback_aom_frequency_1

                self.error_log(1,delta_f1)
                self.error_log(2,delta_f2)

                self.param_shift_log(param_shift)

                self.atom_lock_ex_log(1,[p_1_low,p_1_high])
                self.atom_lock_ex_log(2,[p_2_low,p_2_high])

                    
                delay(5*ms)

                
                count = count + 1
                t2 = self.core.get_rtio_counter_mu()
                self.set_dataset("cycle_times",(t2-t1)*1e-6, broadcast=True,unit = ms, archive=True)

                delay(100*us)


  

