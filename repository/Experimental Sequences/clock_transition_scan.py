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

default_cfr1 = (
    (1 << 1)    # configures the serial data I/O pin (SDIO) as an input only pin; 3-wire serial programming mode
)
default_cfr2 = (
    (1 << 5)    # forces the SYNC_SMP_ERR pin to a Logic 0; this pin indicates (active high) detection of a synchronization pulse sampling error
    | (1 << 16) # a serial I/O port read operation of the frequency tuning word register reports the actual 32-bit word appearing at the input to the DDS phase accumulator (i.e. not the contents of the frequency tuning word register)
    | (1 << 24) # the amplitude is scaled by the ASF from the active profile (without this, the DDS outputs max. possible amplitude -> cracked AOM crystals)
)

class clock_transition_scan(EnvExperiment):

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
        self.red_mot_shutter:TTLOut=self.get_device("ttl12")


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
        
        self.setattr_argument("scan_center_frequency_Hz", NumberValue(default=85000000 * Hz),group="Scan Parameters",)
        self.setattr_argument("scan_range_Hz", NumberValue(default=500000 * Hz), group="Scan Parameters")
        self.setattr_argument("scan_step_size_Hz", NumberValue(default=1000 * Hz), group="Scan Parameters")
        self.setattr_argument("rabi_pulse_duration_ms", NumberValue(default= 60 * ms), group="Scan Parameters")
        self.setattr_argument("clock_intensity", NumberValue(default=0.05), group="Locking")
        self.setattr_argument("bias_field_mT", NumberValue(default=3.0),group="Locking")
        self.setattr_argument("blue_mot_loading_time", NumberValue(default=2000 * ms), group="Sequence Parameters")
        self.setattr_argument("Enable_Lock", BooleanValue(default=False), group="Locking")
        self.setattr_argument("servo_gain", NumberValue(default=0.3), group="Locking")
        self.setattr_argument("linewidth", NumberValue(default=100 * Hz), group="Locking")  # This is the linewidth of the clock transition, adjust as necessary
        self.setattr_argument("drift_rate", NumberValue(default=0.2*Hz),group="Locking")

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
        self.red_mot_shutter.output()
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

        self.atom_lock_aom.set(frequency = 125 * MHz)
        self.atom_lock_aom.set_att(14*dB)

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
    def blue_mot_loading(self,bmot_voltage_1,bmot_voltage_2):                  
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
    
    @kernel
    def blue_mot_compression(self,bmot_voltage_1,bmot_voltage_2,compress_bmot_volt_1,compress_bmot_volt_2,bmot_amp,compress_bmot_amp,compression_time):

        self.zeeman_slower_aom.set(frequency=70 * MHz, amplitude=0.00)   #Turn off the Zeeman Slower
        self.zeeman_slower_shutter.off()
        self.red_mot_aom.sw.on()
        delay(4.0*ms)                                                 #wait for shutter to close

        steps_com = compression_time 
        t_com = compression_time/steps_com
        volt_1_steps = (compress_bmot_volt_1 - bmot_voltage_1)/steps_com
        volt_2_steps = (compress_bmot_volt_2 - bmot_voltage_2 )/steps_com
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

    @kernel
    def broadband_red_mot(self,rmot_voltage_1,rmot_voltage_2):      
             
        self.blue_mot_aom.set(frequency=90*MHz,amplitude=0.00)   
        self.blue_mot_aom.sw.off()                                   #Switch off blue beams
        self.repump_shutter_679.off()
        self.repump_shutter_707.off()
        self.blue_mot_shutter.off()
        delay(3.9*ms)

        self.mot_coil_1.write_dac(0, rmot_voltage_1)
        self.mot_coil_2.write_dac(1, rmot_voltage_2)

        with parallel:
            self.mot_coil_1.load()
            self.mot_coil_2.load()
    
    @kernel
    def red_mot_compression(self,bb_rmot_volt_1,bb_rmot_volt_2,sf_rmot_volt_1,sf_rmot_volt_2,f_start,f_end,A_start,A_end,comp_time):

        start_freq = f_start
        end_freq = f_end


        bb_rmot_amp = A_start
        compress_rmot_amp= A_end

        
        step_duration = 0.1
        steps_com = int(comp_time / step_duration)  

        freq_steps = (start_freq - end_freq)/steps_com

        volt_1_steps = (sf_rmot_volt_1 - bb_rmot_volt_1)/steps_com
        volt_2_steps = (sf_rmot_volt_2 - bb_rmot_volt_2)/steps_com


        amp_steps = (bb_rmot_amp-compress_rmot_amp)/steps_com
        

        for i in range(int64(steps_com)):
            voltage_1 = bb_rmot_volt_1 + ((i+1) * volt_1_steps)
            voltage_2 = bb_rmot_volt_2 + ((i+1) * volt_2_steps)
            amp = bb_rmot_amp - ((i+1) * amp_steps)
            freq = start_freq - ((i+1) * freq_steps)

            self.mot_coil_1.write_dac(0, voltage_1)
            self.mot_coil_2.write_dac(1, voltage_2)

            with parallel:
                self.mot_coil_1.load()
                self.mot_coil_2.load()
                self.red_mot_aom.set(frequency = freq * MHz, amplitude = amp)
                
            
            delay(step_duration*ms)
        
    @kernel 
    def seperate_probe(self,tof,probe_duration,probe_frequency):
            with parallel:
                self.red_mot_aom.sw.off()
                self.blue_mot_aom.sw.off()
                self.repump_shutter_679.off()
                self.repump_shutter_707.off()
                self.probe_shutter.on()

            self.mot_coil_1.write_dac(0, 5.0)  
            self.mot_coil_2.write_dac(1, 5.0)
           
            with parallel:
                self.mot_coil_1.load()
                self.mot_coil_2.load()

            delay(((tof +3.9)*ms))

            with parallel:
                    self.camera_trigger.pulse(2*ms)
                    self.probe_aom.set(frequency=205 *MHz, amplitude=0.18)
                    self.probe_aom.sw.on()
                    
            delay(probe_duration)
                    
            with parallel:
                self.probe_shutter.off()
                  #Camera shutter takes 26ms to open so we will open it here
                self.probe_aom.set(frequency=probe_frequency, amplitude=0.00)
                self.probe_aom.sw.off()

            delay(10*ms)
    
    @kernel
    def clock_spectroscopy(self,aom_frequency,pulse_time):                     #Switch to Helmholtz field, wait, then generate Rabi Pulse
       
        self.red_mot_shutter.off()
        self.red_mot_aom.sw.off()
        self.stepping_aom.sw.off()

        comp_field = 1.35 * 0.14    # comp current * scaling factor from measurement
        bias_at_coil = (self.bias_field_mT - comp_field)/ 0.914   #bias field dips in center of coils due to geometry, scaling factor provided by modelling field
        current_per_coil = ((bias_at_coil) / 2.0086) / 2   
        coil_1_voltage = current_per_coil + 5.0
        coil_2_voltage = 5.0 - (current_per_coil / 0.94 )           #Scaled against coil 1


         #Switch to Helmholtz
        self.mot_coil_1.write_dac(1, coil_1_voltage)  
        self.mot_coil_2.write_dac(0, coil_2_voltage)
        
        with parallel:
            self.mot_coil_1.load()
            self.mot_coil_2.load()

        # self.pmt_shutter.on()
        # self.camera_shutter.on()
          

        delay(50*ms)  #wait for coils to switch

        self.clock_shutter.on()  
        delay(4*ms)
        #rabi spectroscopy pulse
        self.stepping_aom.set(frequency = aom_frequency )
        self.stepping_aom.set_att(16*dB)
        self.stepping_aom.sw.on()
        delay(pulse_time*ms)
        self.stepping_aom.sw.off()
        self.stepping_aom.set(frequency = 0 * Hz)
        self.stepping_aom.sw.off()
        self.clock_shutter.off()
   

    @kernel
    def normalised_detection(self,j,gs_list,es_list,excitation_fraction_list):        #This function should be sampling from the PMT at the same time as the camera being triggered for seperate probe
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
                    self.probe_aom.set(frequency=205 * MHz, amplitude=0.5)

                self.probe_aom.sw.on()
                delay(1* ms)      #Ground state probe duration                          
                self.probe_aom.sw.off()
                self.probe_shutter.off()


                delay(5*ms)                         #repumping

                with parallel:
                    self.repump_shutter_679.pulse(14*ms)
                    self.repump_shutter_707.pulse(14*ms)

                delay(20*ms)                         #repumping 

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


                #  ########################Background############################
 
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

        
        baseline_mean = 0.0
        gs = samples_ch0[90:110]
        es = samples_ch0[908:928]
        bg = samples_ch0[1334:1354]
       

        baseline = samples_ch0[0:40]
        baseline_sum = 0.0
        for x in baseline:
            baseline_sum += float(x)
            baseline_mean = baseline_sum / len(baseline)

        gs_counts = 0.0
        es_counts = 0.0
        bg_counts = 0.0

        measurement_time = 60 * sample_period     #set to 600 as each slice size is 600 samples at the moment,
                                                        # we should trim this tighter to the peaks to avoid added noise

        for val in gs[1:]:
            gs_counts += val
        for val in es[1:]:
            es_counts += val
        for val in bg[1:]:
            bg_counts += val


        gs_mean = gs_counts / len(gs)
        es_mean = es_counts / len(es)
        bg_mean = bg_counts / len(bg)


        
        #if we want the PMT to determine atom no, we will probably want photon counts,
        # will need expected collection efficiency of the telescope,Quantum efficiency etc, maybe use the camera atom no calculation to get this
        
        with parallel:
            gs_measurement = ((gs_mean-baseline_mean)) * measurement_time         #integrates over the slice time to get the total photon counts
            es_measurement = ((es_mean-baseline_mean))  * measurement_time
            bg_measurement = ((bg_mean-baseline_mean)) * measurement_time

    
                    
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
            gs_list[j] = float(gs_measurement)
            es_list[j] = float(es_measurement)
            excitation_fraction_list[j] = float(excitation_fraction)
            
    

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
        self.feedback_list.append(125000000 - value)
        self.set_dataset("feedback_list", self.feedback_list, broadcast=True, archive=True)

    @rpc 
    def error_log(self,value):
        self.error_log_list.append(value)
        """This function is used to log the error in the clock frequency"""
        self.set_dataset("error_log", self.error_log_list, broadcast=True, archive=True)

      

    @rpc
    def atom_lock_ex(self,value):
        """This function is used to lock the atom frequency to the center of the clock transition"""
        self.atom_lock_list.append(value)
        self.set_dataset("atom_lock_list", self.atom_lock_list, broadcast=True, archive=True)
      
    
   
    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()

        self.initialise_modules()

        #Sequence Parameters - Update these with optimised values
        bmot_compression_time = 20 
        blue_mot_cooling_time = 60 
        broadband_red_mot_time = 10
        red_mot_compression_time = 7
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
        rmot_A_start = 0.05,
        rmot_A_end = 0.0025,

        scan_start = int32(self.scan_center_frequency_Hz - (int32(self.scan_range_Hz )/ 2))
        scan_end =int32(self.scan_center_frequency_Hz + (int32(self.scan_range_Hz ) / 2))
        scan_frequency_values = [float(x) for x in range(scan_start, scan_end, int32(self.scan_step_size_Hz))]
        cycles = len(scan_frequency_values)

        gs_list = [0.0] * cycles
        es_list = [0.0] * cycles
        excitation_fraction_list = [0.0] * cycles

        

        
        for j in range(int32(cycles)):        
            t1 = self.core.get_rtio_counter_mu()
            ####################################################### Blue MOT loading #############################################################

            delay(500*us)

            self.blue_mot_loading(
                 bmot_voltage_1 = blue_mot_coil_1_voltage,
                 bmot_voltage_2 = blue_mot_coil_2_voltage
            )

            self.red_mot_shutter.on()
            self.red_mot_aom.set(frequency = 80.45 * MHz, amplitude = 0.08)
            self.red_mot_aom.sw.on()


            delay(self.blue_mot_loading_time* ms)

            ####################################################### Blue MOT compression & cooling ########################################################

            self.blue_mot_compression(                           #Here we are ramping up the blue MOT field and ramping down the blue power
                bmot_voltage_1 = blue_mot_coil_1_voltage,
                bmot_voltage_2 = blue_mot_coil_2_voltage,
                compress_bmot_volt_1 = compressed_blue_mot_coil_1_voltage,
                compress_bmot_volt_2 = compressed_blue_mot_coil_2_voltage,
                bmot_amp = bmot_amp,
                compress_bmot_amp = compress_bmot_amp,
                compression_time = bmot_compression_time
            )

            delay(bmot_compression_time*ms)    #Blue MOT compression time


            delay(blue_mot_cooling_time*ms)   #Allowing further cooling of the cloud by just holding the atoms here

            ########################################################### BB red MOT #################################################################

            self.broadband_red_mot(                                  #Switch to low field gradient for Red MOT, switches off the blue beams
                rmot_voltage_1= bb_rmot_coil_1_voltage,
                rmot_voltage_2 = bb_rmot_coil_2_voltage
            )

            delay(broadband_red_mot_time*ms)

            self.red_mot_aom.set(frequency = 80.55 *MHz, amplitude = 0.05)

            delay(5*ms)



            ########################################################### red MOT compression & Single Frequency ####################################################################


            self.red_mot_compression(                         #Compressing the red MOT by ramping down power, field ramping currently not active
                bb_rmot_volt_1 = bb_rmot_coil_1_voltage,
                bb_rmot_volt_2 = bb_rmot_coil_2_voltage,
                sf_rmot_volt_1 = sf_rmot_coil_1_voltage,
                sf_rmot_volt_2 = sf_rmot_coil_2_voltage,
                f_start = rmot_f_start,
                f_end = rmot_f_end,
                A_start = rmot_A_start,
                A_end = rmot_A_end,
                comp_time = red_mot_compression_time
            )

            delay(red_mot_compression_time*ms)

            delay(single_frequency_time*ms)

            self.red_mot_aom.sw.off()

            # self.seperate_probe(
            #     tof = 50,
            #     probe_duration = 1* ms ,
            #     probe_frequency= 205 * MHz
            # )

 

            #################################################################### Clock Spectroscopy ##################################################################################

            # delay(40*ms)
            self.clock_spectroscopy(
                aom_frequency = scan_frequency_values[j],
                pulse_time = self.rabi_pulse_duration_ms,
            )

            self.normalised_detection(j,gs_list,es_list,excitation_fraction_list)
            
            delay(2*ms)

            self.set_dataset("excitation_fraction_list", excitation_fraction_list, broadcast=True, archive=True)
            t2 = self.core.get_rtio_counter_mu()

        #process data and do fit from the scan

        
        self.analyse_fit(scan_frequency_values, excitation_fraction_list)

        # from the excitation fraction list we need to manually extract the peak height and center_frequency. 
        # Inline calculation of max value and index
        max_val = excitation_fraction_list[0]
        max_idx = 0
        i = 0
        while i < len(excitation_fraction_list):
            if excitation_fraction_list[i] > max_val:
                max_val = excitation_fraction_list[i]
                max_idx = i
            i += 1
        # max_val is the maximum value, max_idx is its index
        contrast = 0.6
        center_frequency = scan_frequency_values[max_idx]
        
        recenter_peak = self.drift_rate *(cycles - (max_idx+1)) * 1.5
        print(recenter_peak)


        # print(contrast)
        # print(center_frequency)
        
        # print((t2-t1)*1e-9, "s")



        delay(1*ms)

        # if atom lock is enabled as True, then begin new while loop which will run the clock sequence but steps the stepping_aom by half the linewidth
        if self.Enable_Lock == True:

            self.core.break_realtime()
            n = 2628288                                           # How many seconds there are in a month
            high_side =0.0
            low_side = 0.0
            count = 0
            lock_loop = 0
            thue_morse = [0]
            while len(thue_morse) <= n:
                thue_morse = thue_morse + [1 - bit for bit in thue_morse] 
            feedback_aom_frequency = (125.000) * MHz  
            print(feedback_aom_frequency)
           


            delay(100*ms)
            
            for i in range(int32(n)):
                count = i
                self.core.break_realtime()
                delay(10*ms)
                ### Insert entire sequence again
                # t1 = self.core.get_rtio_counter_mu()
                delay(2*ms)

                self.blue_mot_loading(
                    bmot_voltage_1 = blue_mot_coil_1_voltage,
                    bmot_voltage_2 = blue_mot_coil_2_voltage
                )
            
                self.red_mot_shutter.on()
                self.red_mot_aom.set(frequency = 80.45 * MHz, amplitude = 0.08)
                self.red_mot_aom.sw.on()

                delay(self.blue_mot_loading_time* ms)

                ####################################################### Blue MOT compression & cooling ########################################################

                self.blue_mot_compression(                           #Here we are ramping up the blue MOT field and ramping down the blue power
                    bmot_voltage_1 = blue_mot_coil_1_voltage,
                    bmot_voltage_2 = blue_mot_coil_2_voltage,
                    compress_bmot_volt_1 = compressed_blue_mot_coil_1_voltage,
                    compress_bmot_volt_2 = compressed_blue_mot_coil_2_voltage,
                    bmot_amp = bmot_amp,
                    compress_bmot_amp = compress_bmot_amp,
                    compression_time = bmot_compression_time
                )
                delay(bmot_compression_time*ms)    #Blue MOT compression time

                delay(blue_mot_cooling_time*ms)   #Allowing further cooling of the cloud by just holding the atoms here

                ########################################################### BB red MOT #################################################################

                self.broadband_red_mot(                                  #Switch to low field gradient for Red MOT, switches off the blue beams
                    rmot_voltage_1= bb_rmot_coil_1_voltage,
                    rmot_voltage_2 = bb_rmot_coil_2_voltage
                )

                delay(broadband_red_mot_time*ms)

                self.red_mot_aom.set(frequency = 80.55 *MHz, amplitude = 0.06)

                delay(5*ms)

                ########################################################### red MOT compression & Single Frequency ####################################################################


                self.red_mot_compression(                         #Compressing the red MOT by ramping down power, field ramping currently not active
                    bb_rmot_volt_1 = bb_rmot_coil_1_voltage,
                    bb_rmot_volt_2 = bb_rmot_coil_2_voltage,
                    sf_rmot_volt_1 = sf_rmot_coil_1_voltage,
                    sf_rmot_volt_2 = sf_rmot_coil_2_voltage,
                    f_start = rmot_f_start,
                    f_end = rmot_f_end,
                    A_start = rmot_A_start,
                    A_end = rmot_A_end,
                    comp_time = red_mot_compression_time
                )

                delay(red_mot_compression_time*ms)

                delay(single_frequency_time*ms)

                self.red_mot_aom.sw.off()

                #################################################################### Clock Spectroscopy ##################################################################################
                if thue_morse[count] == 0:
                    self.core.break_realtime()
                    self.clock_spectroscopy(
                        aom_frequency = (center_frequency - (self.linewidth/2))*Hz,
                        pulse_time = self.rabi_pulse_duration_ms,
                    )
                
                    low_side = self.normalised_detection(0,[0.0],[0.0],[0.0])

                    self.atom_lock_ex(low_side)
                    # print("low_side")
                    delay(2*ms)
                   
                    #return most recent excitation_fraction list value

                            
                elif thue_morse[count] == 1:
                    self.core.break_realtime()
                    self.clock_spectroscopy(
                        aom_frequency = center_frequency + self.linewidth/2,
                        pulse_time = self.rabi_pulse_duration_ms,
                    )
                    high_side = self.normalised_detection(0,[0.0],[0.0],[0.0])
                    # print("high_side")
                    self.atom_lock_ex(high_side)
                    delay(2*ms)
                if count == 0: 
                    continue
                else:   
                    if count % 2 == 0:              # Every other cycle generate correction
                        #Calculate error signal and then make correction
                        self.core.break_realtime()
                       
                        if high_side > 1.0 or low_side > 1.0:        #prevents bad excitation fraction from destabilising the lock
                            error_signal = 0.0
                        else:
                            error_signal = high_side - low_side

                    # denominator = 0.8 * 0.65 * (self.rabi_pulse_duration_ms * 1e-3)

                    # if denominator == 0.0:
                    #     print("Warning: Division by zero prevented. Check contrast and rabi_pulse_duration_ms.")
                    #     frequency_correction = 0.0  # or skip this iteration with `continue`
                    # else:
                    #     frequency_correction = (self.servo_gain / denominator) * error_signal
                    # # This is the first servo loop

                        if error_signal == 0.0:
                            frequency_correction = 0.0
                            print("No correction made")
                        elif high_side+low_side <= 0.05:
                            frequency_correction = 0.0
                            print("No correction made - too low")
                        elif high_side+low_side >= 1.0:
                            frequency_correction = 0.0
                            print("No correction made - too high")
                        else:
                            frequency_correction = - (self.servo_gain * error_signal * self.linewidth) / (2 * contrast)


                        delay(500*us)
                        feedback_aom_frequency = feedback_aom_frequency + frequency_correction
                        print(feedback_aom_frequency)
                        delay(10*ms)

                        self.atom_lock_aom.set(frequency = feedback_aom_frequency)
                        delay(10*ms)


                        self.error_log(error_signal)
                        self.correction_log(feedback_aom_frequency)
                        
                        delay(5*ms)

                        
                        
                    
                        #send the list of frequency corrections to the database, this will be done on the host side
                        lock_loop = lock_loop + 1
                        
                        
                        #write to text file
                
                # count = count + 1
                # t2 = self.core.get_rtio_counter_mu()
                # print((t2-t1)*1e-6, "ms")

                if count % 51 == 0:
                    print("Breaking RTIO timeline at iteration", count)
                    self.core.break_realtime()



                delay(200*us)


                # Add logic to adjust the aom frequency based on the contrast and linewidth
                # This is a placeholder for the actual locking logic
                # Adjust the scan_frequency_values[j] based on the feedback from the lock
  
  


        print("Scan complete")

