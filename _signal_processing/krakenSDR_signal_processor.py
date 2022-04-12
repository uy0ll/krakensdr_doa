# KrakenSDR Signal Processor
#
# Copyright (C) 2018-2021  Carl Laufer, Tamás Pető
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
#
# - coding: utf-8 -*-

# Import built-in modules
import sys
import os
#os.environ['OPENBLAS_NUM_THREADS'] = '4'
#os.environ['NUMBA_CPU_NAME'] = 'cortex-a72'

import time
import logging
import threading
import queue
import math
import xml.etree.ElementTree as ET
import requests

# Import optimization modules
import numba as nb
from numba import jit, njit
from functools import lru_cache

# Math support
import numpy as np
import numpy.linalg as lin

# Signal processing support
import scipy
from scipy import fft
from scipy import signal
from pyargus import directionEstimation as de

# Make gpsd an optional component
try:
    import gpsd
    hasgps = True
    print("gpsd Available")
except ModuleNotFoundError:
    hasgps = False
    print("Can't find gpsd - ok if no external gps used")

class SignalProcessor(threading.Thread):
    def __init__(self, data_que, module_receiver, logging_level=10):

        """
            Parameters:
            -----------
            :param: data_que: Que to communicate with the UI (web iface/Qt GUI)
            :param: module_receiver: Kraken SDR DoA DSP receiver modules
        """
        super(SignalProcessor, self).__init__()
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging_level)

        root_path      = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        doa_res_file_path = os.path.join(os.path.join(root_path,"_android_web","DOA_value.html"))
        self.DOA_res_fd = open(doa_res_file_path,"w+")

        self.module_receiver = module_receiver
        self.data_que = data_que
        self.en_spectrum = False
        self.en_record = False
        self.en_DOA_estimation = True
        self.first_frame = 1 # Used to configure local variables from the header fields
        self.processed_signal = np.empty(0)

        # Squelch feature
        self.data_ready = False
        self.dsp_decimation = 1

        # DOA processing options
        self.en_DOA_Bartlett = False
        self.en_DOA_Capon    = False
        self.en_DOA_MEM      = False
        self.en_DOA_MUSIC    = False
        self.en_DOA_FB_avg   = False
        self.DOA_offset      = 0
        self.DOA_inter_elem_space = 0.5
        self.DOA_ant_alignment    = "ULA"
        self.DOA_theta =  np.linspace(0,359,360)
        self.custom_array_x = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
        self.custom_array_y = np.array([0.1, 0.2, 0.3, 0.4, 0.5])

        # Processing parameters
        self.spectrum_window_size = fft.next_fast_len(4096)
        self.spectrum_plot_size = 1024
        self.spectrum_window = "hann"
        self.run_processing = False
        self.is_running = False
        self.channel_number = 4  # Update from header
        self.spectrum_fig_type = 'Single' #0 Single, 1 Full

        # Result vectors
        self.DOA_Bartlett_res = np.ones(181)
        self.DOA_Capon_res = np.ones(181)
        self.DOA_MEM_res = np.ones(181)
        self.DOA_MUSIC_res = np.ones(181)
        #self.DOA_theta = np.arange(0,181,1)

        # VFO settings
        self.max_vfos = 16
        self.vfo_bw = [12500] * self.max_vfos
        self.vfo_freq = [self.module_receiver.daq_center_freq] * self.max_vfos
        self.vfo_squelch = [-120] * self.max_vfos

        self.active_vfos = 1
        self.output_vfo = 0
        self.vfo_mode = 'Standard'

        #self.DOA_theta =  np.linspace(0,359,360)
        self.spectrum = None #np.ones((self.channel_number+2,N), dtype=np.float32)

        self.latency = 100

        # Output Data format. XML for Kerberos, CSV for Kracken, JSON future
        self.DOA_data_format = "Kraken App"  # XML, CSV, or JSON

        # Location parameters
        self.station_id = "NOCALL"
        self.latitude = 0.0
        self.longitude = 0.0
        self.fixed_heading = False
        self.heading = 0.0
        self.altitude = 0.0
        self.speed = 0.0
        self.hasgps = hasgps
        self.usegps = False
        self.gps_connected = False

        # Kraken Pro Remote API Key
        self.krakenpro_key = "0"

    def run(self):
        """
            Main processing thread
        """
        #scipy.fft.set_workers(4)
        while True:
            self.is_running = False
            time.sleep(1)
            while self.run_processing:
                self.is_running = True
                que_data_packet = []

                #-----> ACQUIRE NEW DATA FRAME <-----
                self.module_receiver.get_iq_online()
                start_time = time.time()

                # Check frame type for processing
                en_proc = (self.module_receiver.iq_header.frame_type == self.module_receiver.iq_header.FRAME_TYPE_DATA)# or \
                          #(self.module_receiver.iq_header.frame_type == self.module_receiver.iq_header.FRAME_TYPE_CAL)# For debug purposes
                """
                    You can enable here to process other frame types (such as call type frames)
                """

                que_data_packet.append(['iq_header',self.module_receiver.iq_header])
                self.logger.debug("IQ header has been put into the data que entity")

                # Configure processing parameteres based on the settings of the DAQ chain
                if self.first_frame:
                    self.channel_number = self.module_receiver.iq_header.active_ant_chs
                    self.spectrum = np.ones((self.channel_number+4, self.spectrum_window_size), dtype=np.float32)
                    self.first_frame = 0

                self.data_ready = False

                if en_proc:
                    self.processed_signal = np.ascontiguousarray(self.module_receiver.iq_samples) #self.module_receiver.iq_samples.copy()
                    sampling_freq = self.module_receiver.iq_header.sampling_freq

                    global_decimation_factor = max(int(self.dsp_decimation), 1) #max(int(self.phasetest[0]), 1) #ps_len // 65536 #int(self.phasetest[0]) + 1
 
                    if global_decimation_factor > 1:
                        #fir_filter = filter_only(global_decimation_factor, 5, 1.0)
                        #self.processed_signal = signal.decimate(self.processed_signal, global_decimation_factor, ftype=fir_filter)
                        self.processed_signal = signal.decimate(self.processed_signal, global_decimation_factor, n = global_decimation_factor * 5, ftype='fir')
                        sampling_freq = sampling_freq//global_decimation_factor

                    self.data_ready = True
                    #max_amplitude = -100

                    if self.spectrum_fig_type == 'Single':
                        m = 0
                        N = self.spectrum_window_size
                        self.spectrum = np.ones((self.channel_number+(self.active_vfos*2+1), N), dtype=np.float32) # Only 0.1 ms, not performance bottleneck

                        single_ch = self.processed_signal[1,:]
                        f, Pxx_den = signal.welch(single_ch, sampling_freq,
                                     nperseg= N,
                                     nfft=N,
                                     noverlap=0.25, #int(N_perseg*0.0),
                                     detrend=False,
                                     return_onesided=False,
                                     window= 'blackman', #('tukey', 0.25), #tukey window gives better time resolution for squelching
                                     scaling='spectrum')
                        self.spectrum[1+m,:] = fft.fftshift(10*np.log10(Pxx_den))
                        self.spectrum[0,:] = fft.fftshift(f)
                    else:
                        N = 32768
                        self.spectrum = np.ones((self.channel_number+(self.active_vfos*2+1), N), dtype=np.float32)
                        for m in range(self.channel_number): #range(1): #range(self.channel_number):
                            f, Pxx_den = signal.periodogram(self.processed_signal[m,:], sampling_freq,
                                         nfft=N,
                                         detrend=False,
                                         return_onesided=False,
                                         window= 'blackman',
                                         scaling='spectrum')
                            self.spectrum[1+m,:] = fft.fftshift(10*np.log10(Pxx_den))
                        self.spectrum[0,:] = fft.fftshift(f)

                    max_amplitude = np.max(self.spectrum[1, :]) #Max amplitude out of all 5 channels
                    que_data_packet.append(['max_amplitude',max_amplitude])

                    #-----> DoA PROCESSING <-----
                    if self.data_ready:
                        spectrum_window_size = len(self.spectrum[0,:])
                        active_vfos = self.active_vfos if self.vfo_mode == 'Standard' else 1
                        write_freq = 0
                        for i in range(active_vfos):
                            # If chanenl freq is out of bounds for the current tuned bandwidth, reset to the middle freq
                            if abs(self.vfo_freq[i] - self.module_receiver.daq_center_freq) > sampling_freq/2 :
                                self.vfo_freq[i] = self.module_receiver.daq_center_freq

                            freq = self.vfo_freq[i] - self.module_receiver.daq_center_freq #ch_freq is relative to -sample_freq/2 : sample_freq/2, so correct for that and get the actual freq

                            if self.vfo_mode == 'Auto': # Mode 1 is Auto Max Mode
                                max_index = self.spectrum[1,:].argmax()
                                freq = self.spectrum[0, max_index]

                            decimation_factor = max((sampling_freq // self.vfo_bw[i]), 1) # How much decimation is required to get to the requested bandwidth

                            # Get max amplitude of the channel from the FFT for squelching
                            # From channel frequency determine array index of channel
                            vfo_width_idx = int((spectrum_window_size * self.vfo_bw[i]) / (sampling_freq)) # Width of channel in array indexes based on FFT size
                            vfo_width_idx = max(vfo_width_idx, 2)

                            freqMin = -sampling_freq/2

                            vfo_center_idx = int((((freq - freqMin) * spectrum_window_size) / sampling_freq))

                            vfo_upper_bound = vfo_center_idx + vfo_width_idx//2
                            vfo_lower_bound = vfo_center_idx - vfo_width_idx//2

                            if self.spectrum_fig_type == 'Single': # Do CH1 only (or make channel selectable)
                                spectrum_channel = self.spectrum[1, max(vfo_lower_bound, 0) : min(vfo_upper_bound, spectrum_window_size)]
                                max_amplitude = np.max(spectrum_channel)
                            else:
                                spectrum_channel = self.spectrum[:, max(vfo_lower_bound, 0) : min(vfo_upper_bound, spectrum_window_size)]
                                max_amplitude = np.max(spectrum_channel[1:self.module_receiver.iq_header.active_ant_chs+1, :])

                            # *** HERE WE NEED TO PERFORM THE SPECTRUM UPDATE TOO ***
                            if self.en_spectrum:
                                # Selected Channel Window
                                signal_window = np.zeros(spectrum_window_size) - 120
                                signal_window[max(vfo_lower_bound, 4) : min(vfo_upper_bound, spectrum_window_size-4)] = 0 #max_amplitude
                                self.spectrum[self.channel_number+(2*i+1), :] = signal_window #np.ones(len(spectrum[1,:])) * self.module_receiver.daq_squelch_th_dB # Plot threshold line

                                # Squelch Window
                                signal_window[max(vfo_lower_bound, 4) : min(vfo_upper_bound, spectrum_window_size-4)] = self.vfo_squelch[i]
                                self.spectrum[self.channel_number+(2*i+2), :] = signal_window #np.ones(len(spectrum[1,:])) * self.module_receiver.daq_squelch_th_dB # Plot threshold line

                            #-----> DoA ESIMATION <-----
                            conf_val = 0
                            theta_0 = 0
                            if self.en_DOA_estimation and self.channel_number > 1 and max_amplitude > self.vfo_squelch[i] and (i == self.output_vfo or self.output_vfo < 0):
                                write_freq = int(self.vfo_freq[i])
                                # Do channelization
                                vfo_channel = channelize(self.processed_signal, freq, decimation_factor, sampling_freq)

                                ########################## Method to check IQ diffs when noise source forced ON
                                #iq_diffs = calc_sync(self.processed_signal)
                                #print("IQ DIFFS: " + str(iq_diffs))
                                #print("IQ DIFFS ANGLE: " + str(np.rad2deg(np.angle(iq_diffs))))
                                ##########################
                                self.estimate_DOA(vfo_channel)

                                que_data_packet.append(['doa_thetas', self.DOA_theta])
                                if self.en_DOA_Bartlett:
                                    doa_result_log = DOA_plot_util(self.DOA_Bartlett_res)
                                    theta_0 = self.DOA_theta[np.argmax(doa_result_log)]
                                    conf_val = calculate_doa_papr(self.DOA_Bartlett_res)
                                    que_data_packet.append(['DoA Bartlett', doa_result_log])
                                    que_data_packet.append(['DoA Bartlett Max', theta_0])
                                    que_data_packet.append(['DoA Bartlett confidence', conf_val])
                                if self.en_DOA_Capon:
                                    doa_result_log = DOA_plot_util(self.DOA_Capon_res)
                                    theta_0 = self.DOA_theta[np.argmax(doa_result_log)]
                                    conf_val = calculate_doa_papr(self.DOA_Capon_res)
                                    que_data_packet.append(['DoA Capon', doa_result_log])
                                    que_data_packet.append(['DoA Capon Max', theta_0])
                                    que_data_packet.append(['DoA Capon confidence', conf_val])
                                if self.en_DOA_MEM:
                                    doa_result_log = DOA_plot_util(self.DOA_MEM_res)
                                    theta_0 = self.DOA_theta[np.argmax(doa_result_log)]
                                    conf_val = calculate_doa_papr(self.DOA_MEM_res)
                                    que_data_packet.append(['DoA MEM', doa_result_log])
                                    que_data_packet.append(['DoA MEM Max', theta_0])
                                    que_data_packet.append(['DoA MEM confidence', conf_val])
                                if self.en_DOA_MUSIC:
                                    doa_result_log = DOA_plot_util(self.DOA_MUSIC_res)
                                    theta_0 = self.DOA_theta[np.argmax(doa_result_log)]
                                    conf_val = calculate_doa_papr(self.DOA_MUSIC_res)
                                    que_data_packet.append(['DoA MUSIC', doa_result_log])
                                    que_data_packet.append(['DoA MUSIC Max', theta_0])
                                    que_data_packet.append(['DoA MUSIC confidence', conf_val])

                                DOA_str = str(int(theta_0))
                                confidence_str = "{:.2f}".format(np.max(conf_val))
                                max_power_level_str = "{:.1f}".format((np.maximum(-100, max_amplitude)))

                        if self.hasgps:
                            self.update_location()

                        if self.DOA_data_format == "DF Aggregator":
                            self.wr_xml(self.station_id,
                                        DOA_str,
                                        confidence_str,
                                        max_power_level_str,
                                        write_freq,
                                        self.latitude,
                                        self.longitude,
                                        self.heading)
                        elif self.DOA_data_format == "Kraken App":
                            self.wr_csv(self.station_id,
                                        DOA_str,
                                        confidence_str,
                                        max_power_level_str,
                                        write_freq,
                                        doa_result_log,
                                        self.latitude,
                                        self.longitude,
                                        self.heading,
                                        "Kraken")
                        elif self.DOA_data_format == "Kerberos App":
                            self.wr_csv(self.station_id,
                                        DOA_str,
                                        confidence_str,
                                        max_power_level_str,
                                        write_freq,
                                        doa_result_log,
                                        self.latitude,
                                        self.longitude,
                                        self.heading,
                                        "Kerberos")
                        elif self.DOA_data_format == "Kraken Pro Local":
                            self.wr_json(self.station_id,
                                        DOA_str,
                                        confidence_str,
                                        max_power_level_str,
                                        write_freq,
                                        doa_result_log,
                                        self.latitude,
                                        self.longitude,
                                        self.heading)
                        elif self.DOA_data_format == "Kraken Pro Remote":
                            self.wr_json(self.station_id,
                                        DOA_str,
                                        confidence_str,
                                        max_power_level_str,
                                        write_freq,
                                        doa_result_log,
                                        self.latitude,
                                        self.longitude,
                                        self.heading)


                        else:
                            self.logger.error(f"Invalid DOA Result data format: {self.DOA_data_format}")

                    #-----> SPECTRUM PROCESSING <-----
                    if self.en_spectrum and self.data_ready:
                        spectrum_plot_data = reduce_spectrum(self.spectrum, self.spectrum_plot_size, self.channel_number)
                        que_data_packet.append(['spectrum', spectrum_plot_data])

                    # Record IQ samples
                    if self.en_record:
                        # TODO: Implement IQ frame recording
                        self.logger.error("Saving IQ samples to npy is obsolete, IQ Frame saving is currently not implemented")

                stop_time = time.time()
                que_data_packet.append(['update_rate', stop_time-start_time])
                que_data_packet.append(['latency', int(stop_time*10**3)-self.module_receiver.iq_header.time_stamp])

                # Put data into buffer, but if there is no data because its a cal/trig wait frame etc, then only write if the buffer is empty
                # Otherwise just discard the data so that we don't overwrite good DATA frames.
                try:
                    self.data_que.put(que_data_packet, False) # Must be non-blocking so DOA can update when dash browser window is closed
                except:
                    # Discard data, UI couldn't consume fast enough
                    pass

    def estimate_DOA(self, processed_signal):
        """
            Estimates the direction of arrival of the received RF signal
        """

        # Calculating spatial correlation matrix
        R = corr_matrix(processed_signal) #de.corr_matrix_estimate(self.processed_signal.T, imp="fast")

        if self.en_DOA_FB_avg:
            R=de.forward_backward_avg(R)

        M = self.channel_number
        scanning_vectors = []
        if self.DOA_ant_alignment == "UCA" or self.DOA_ant_alignment == "ULA":
            scanning_vectors = gen_scanning_vectors(M, self.DOA_inter_elem_space, self.DOA_ant_alignment)
        elif self.DOA_ant_alignment == "Custom":
            scanning_vectors = gen_scanning_vectors_custom(M, self.custom_array_x, self.custom_array_y)

        # DOA estimation
        if self.en_DOA_Bartlett:
            DOA_Bartlett_res = de.DOA_Bartlett(R, scanning_vectors)
            self.DOA_Bartlett_res = DOA_Bartlett_res
        if self.en_DOA_Capon:
            DOA_Capon_res = de.DOA_Capon(R, scanning_vectors)
            self.DOA_Capon_res = DOA_Capon_res
        if self.en_DOA_MEM:
            DOA_MEM_res = de.DOA_MEM(R, scanning_vectors,  column_select = 0)
            self.DOA_MEM_res = DOA_MEM_res
        if self.en_DOA_MUSIC:
            DOA_MUSIC_res = DOA_MUSIC(R, scanning_vectors, signal_dimension = 1) #de.DOA_MUSIC(R, scanning_vectors, signal_dimension = 1)
            self.DOA_MUSIC_res = DOA_MUSIC_res

    # Enable GPS
    def enable_gps(self):
        if self.hasgps:
            if gpsd.state == {}:
                gpsd.connect()
                self.logger.info("Connecting to GPS")
                self.gps_connected = True
        else:
            self.logger.error("You're trying to use GPS, but gpsd-py3 isn't installed")

        return self.gps_connected

    # Get GPS Data
    def update_location(self):
        if self.gps_connected:
            try:
                packet = gpsd.get_current()
                self.latitude, self.longitude = packet.position()
                if not self.fixed_heading:
                    self.heading = round(packet.movement().get('track'), 1)
            except (gpsd.NoFixError, UserWarning):
                self.latitude = self.longitude = 0.0
                self.heading if self.fixed_heading else 0.0
        else:
            self.logger.error("Trying to use GPS, but can't connect to gpsd")

    def wr_xml(self, station_id, doa, conf, pwr, freq,
               latitude, longitude, heading):
        epoch_time = int(1000 * round(time.time(), 3))
        # create the file structure
        data = ET.Element('DATA')
        xml_st_id = ET.SubElement(data, 'STATION_ID')
        xml_time = ET.SubElement(data, 'TIME')
        xml_freq = ET.SubElement(data, 'FREQUENCY')
        xml_location = ET.SubElement(data, 'LOCATION')
        xml_latitide = ET.SubElement(xml_location, 'LATITUDE')
        xml_longitude = ET.SubElement(xml_location, 'LONGITUDE')
        xml_heading = ET.SubElement(xml_location, 'HEADING')
        xml_doa = ET.SubElement(data, 'DOA')
        xml_pwr = ET.SubElement(data, 'PWR')
        xml_conf = ET.SubElement(data, 'CONF')

        xml_st_id.text = str(station_id)
        xml_time.text = str(epoch_time)
        xml_freq.text = str(freq / 1000000)
        xml_latitide.text = str(latitude)
        xml_longitude.text = str(longitude)
        xml_heading.text = str(heading)
        xml_doa.text = doa
        xml_pwr.text = pwr
        xml_conf.text = conf

        # create a new XML file with the results
        html_str = ET.tostring(data, encoding="unicode")
        self.DOA_res_fd.seek(0)
        self.DOA_res_fd.write(html_str)
        self.DOA_res_fd.truncate()
        # print("Wrote XML")

    def wr_csv(self, station_id, DOA_str, confidence_str, max_power_level_str,
               freq, doa_result_log, latitude, longitude, heading, app_type):

        if app_type == "Kraken":
            epoch_time = int(time.time() * 1000)

            # KrakenSDR Android App Output
            message = f"{epoch_time}, {DOA_str}, {confidence_str}, {max_power_level_str}, "
            message += f"{freq}, {self.DOA_ant_alignment}, {self.latency}, {station_id}, "
            message += f"{latitude}, {longitude}, {heading}, "
            message += "R, R, R, R, R, R"  # Reserve 6 entries for other things

            doa_result_log = doa_result_log + np.abs(np.min(doa_result_log))
            for i in range(len(doa_result_log)):
                message += ", " + "{:.2f}".format(doa_result_log[i])

            self.DOA_res_fd.seek(0)
            self.DOA_res_fd.write(message)
            self.DOA_res_fd.truncate()
            self.logger.debug("DoA results writen: {:s}".format(message))
        else: # Legacy Kerberos app support
            confidence_str = "{}".format(np.max(int(float(confidence_str)*100)))
            max_power_level_str = "{:.1f}".format((np.maximum(-100, float(max_power_level_str)+100)))

            message = str(int(time.time() * 1000)) + ", " + DOA_str + ", " + confidence_str + ", " + max_power_level_str
            html_str = "<DATA>\n<DOA>"+DOA_str+"</DOA>\n<CONF>"+confidence_str+"</CONF>\n<PWR>"+max_power_level_str+"</PWR>\n</DATA>"
            self.DOA_res_fd.seek(0)
            self.DOA_res_fd.write(html_str)
            self.DOA_res_fd.truncate()
            self.logger.debug("DoA results writen: {:s}".format(html_str))

    def wr_json(self, station_id, DOA_str, confidence_str, max_power_level_str,
               freq, doa_result_log, latitude, longitude, heading):
        #KrakenSDR Flutter app out
        doaString = str('')
        for i in range(len(doa_result_log)):
            doaString += "{:.2f}".format(doa_result_log[i] + np.abs(np.min(doa_result_log)))+',' #TODO: After confirmed to work, optimize

        #doaString = str('')
        #doa_result_log = doa_result_log + np.abs(np.min(doa_result_log))
        #for i in range(len(doa_result_log)):
        #    doaString += ", " + "{:.2f}".format(doa_result_log[i])

        jsonDict = {}
        jsonDict["tStamp"] = int(time.time() * 1000)
        jsonDict["latitude"] = 0
        jsonDict["longitude"] = 0
        jsonDict["gpsBearing"] = 0
        jsonDict["radioBearing"] = DOA_str
        jsonDict["conf"] = confidence_str
        jsonDict["power"] = max_power_level_str
        jsonDict["freq"] = freq #self.module_receiver.daq_center_freq
        jsonDict["antType"] = self.DOA_ant_alignment
        jsonDict["latency"] = 100
        jsonDict["doaArray"] = doaString

        try:
            r = requests.post('http://127.0.0.1:8042/doapost', json=jsonDict)
        except requests.exceptions.RequestException as e:
            self.logger.error("Error while posting to local websocket server")


def calc_sync(iq_samples):
    iq_diffs   = np.ones(4, dtype=np.complex64)
    dyn_ranges = []

    N_proc = len(iq_samples[0,:])

    # Calculate Spatial correlation matrix to determine amplitude-phase missmatches
    Rxx = iq_samples.dot(np.conj(iq_samples.T))
    # Perform eigen-decomposition
    eigenvalues, eigenvectors = lin.eig(Rxx)
    # Get dominant eigenvector
    max_eig_index = np.argmax(np.abs(eigenvalues))
    vmax  = eigenvectors[:, max_eig_index]
    iq_diffs = 1 / vmax
    iq_diffs /= iq_diffs[0]

    return iq_diffs

# Reduce spectrum size for plotting purposes by taking the MAX val every few values
# Significantly faster with numba once we added nb.prange
@njit(fastmath=True, cache=True, parallel=True)
def reduce_spectrum(spectrum, spectrum_size, channel_number):
    spectrum_elements = len(spectrum[:,0])

    spectrum_plot_data = np.zeros((spectrum_elements, spectrum_size), dtype=np.float32)
    group = len(spectrum[0,:]) // spectrum_size
    for m in nb.prange(spectrum_elements):
        for i in nb.prange(spectrum_size):
            spectrum_plot_data[m, i] = np.max(spectrum[m, i*group:group*(i+1)])
    return spectrum_plot_data

# Get the FIR filter
@lru_cache(maxsize=32)
def get_fir(n, q, padd):
    return signal.dlti(signal.firwin(n+1, 1. / (q * padd), window='hann'), 1.)

# Get the frequency rotation exponential
@lru_cache(maxsize=32)
def get_exponential(freq, sample_freq, sig_len):
    # Auto shift peak frequency center of spectrum, this frequency will be decimated:
    # https://pysdr.org/content/filters.html
    f0 = -freq #+10
    Ts = 1.0/sample_freq
    t = np.arange(0.0, Ts*sig_len, Ts)
    exponential = np.exp(2j*np.pi*f0*t) # this is essentially a complex sine wave

    return np.ascontiguousarray(exponential)

@njit(fastmath=True, cache=True, parallel=True)
def numba_mult(a,b):
    return a*b

# Memoize the total shift filter
@lru_cache(maxsize=32)
def shift_filter(decimation_factor, freq, sampling_freq, padd):
    system = get_fir(decimation_factor*2, decimation_factor, padd)
    b = system.num
    a = system.den
    exponential = get_exponential(-freq, sampling_freq, len(b))
    b = numba_mult(b, exponential)
    return signal.dlti(b,a)


@lru_cache(maxsize=32)
def filter_only(decimation_factor, fir_multiplier, padd):
    system = get_fir(decimation_factor*fir_multiplier, decimation_factor, padd)
    b = system.num
    a = system.den
    #exponential = get_exponential(-freq, sampling_freq, len(b))
    #b = numba_mult(b, exponential)
    return signal.dlti(b,a)


# This function takes the full data, and efficiently returns only a filtered and decimated requested channel
# Efficient method: Create BANDPASS Filter for frequency of interest, decimate with that bandpass filter, then do the final shift
#@jit(fastmath=True, cache=True, parallel=True)
def channelize(processed_signal, freq, decimation_factor, sampling_freq):
    system = shift_filter(decimation_factor, freq, sampling_freq, 1.1) # Decimate with a BANDPASS filter
    decimated = signal.decimate(processed_signal, decimation_factor, ftype=system)
    exponential = get_exponential(freq, sampling_freq/decimation_factor, len(decimated[0,:]))     # Shift the signal AFTER to get back to normal decimate behaviour
    return numba_mult(decimated, exponential)

    # Old Method
    # Auto shift peak frequency center of spectrum, this frequency will be decimated:
    # https://pysdr.org/content/filters.html
    #f0 = -freq #+10
    #Ts = 1.0/sample_freq
    #t = np.arange(0.0, Ts*len(processed_signal[0, :]), Ts)
    #exponential = np.exp(2j*np.pi*f0*t) # this is essentially a complex sine wave

    # Decimate down to BW
    #decimation_factor = max((sample_freq // bw), 1)
    #decimated_signal = signal.decimate(processed_signal, decimation_factor, n = decimation_factor * 2, ftype='fir')

    #return decimated_signal

# NUMBA optimized MUSIC function. About 100x faster on the Pi 4
#@njit(fastmath=True, cache=True, parallel=True)
@njit(fastmath=True, cache=True)
def DOA_MUSIC(R, scanning_vectors, signal_dimension, angle_resolution=1):
    # --> Input check
    if R[:,0].size != R[0,:].size:
        print("ERROR: Correlation matrix is not quadratic")
        return np.ones(1, dtype=np.complex64)*-1 #[(-1, -1j)]

    if R[:,0].size != scanning_vectors[:,0].size:
        print("ERROR: Correlation matrix dimension does not match with the antenna array dimension")
        return np.ones(1, dtype=np.complex64)*-2

    ADORT = np.zeros(scanning_vectors[0,:].size, dtype=np.complex64)
    M = R[:,0].size #np.size(R, 0)

    # --- Calculation ---
    # Determine eigenvectors and eigenvalues
    sigmai, vi = lin.eig(R)
    sigmai = np.abs(sigmai)

    idx = sigmai.argsort()[::1] # Sort eigenvectors by eigenvalues, smallest to largest
    vi = vi[:,idx]

    # Generate noise subspace matrix
    noise_dimension = M - signal_dimension

    E = np.zeros((M, noise_dimension),dtype=np.complex64)
    for i in range(noise_dimension):
        E[:,i] = vi[:,i]

    E_ct = E @ E.conj().T
    theta_index=0
    for i in range(scanning_vectors[0,:].size):
        S_theta_ = scanning_vectors[:, i]
        S_theta_  = S_theta_.T
        ADORT[theta_index] = 1/np.abs(S_theta_.conj().T @ E_ct @ S_theta_)
        theta_index += 1

    return ADORT

# Numba optimized version of pyArgus corr_matrix_estimate with "fast". About 2x faster on Pi4
@njit(fastmath=True, cache=True)
def corr_matrix(X):
    N = X[0,:].size
    R = np.dot(X, X.conj().T)
    R = np.divide(R, N)
    return R

# LRU cache memoize about 1000x faster.
@lru_cache(maxsize=32)
def gen_scanning_vectors(M, DOA_inter_elem_space, type):
    thetas =  np.linspace(0,359,360) # Remember to change self.DOA_thetas too, we didn't include that in this function due to memoization cannot work with arrays
    if type == "UCA":
        x = DOA_inter_elem_space * np.cos(2*np.pi/M * np.arange(M))
        y = -DOA_inter_elem_space * np.sin(2*np.pi/M * np.arange(M)) # For this specific array only
    elif "ULA":
        x = np.zeros(M)
        y = -np.arange(M) * DOA_inter_elem_space

    scanning_vectors = np.zeros((M, thetas.size), dtype=np.complex64)
    for i in range(thetas.size):
        scanning_vectors[:,i] = np.exp(1j*2*np.pi* (x*np.cos(np.deg2rad(thetas[i])) + y*np.sin(np.deg2rad(thetas[i]))))

    return np.ascontiguousarray(scanning_vectors)

#@lru_cache(maxsize=32)
@njit(fastmath=True, cache=True)
def gen_scanning_vectors_custom(M, custom_x, custom_y):
    thetas =  np.linspace(0,359,360) # Remember to change self.DOA_thetas too, we didn't include that in this function due to memoization cannot work with arrays
#    x = np.array(custom_x)
#    y = np.array(custom_y)
    x = custom_x
    y = custom_y
    scanning_vectors = np.zeros((M, thetas.size), dtype=np.complex64)
    complex_pi = 1j*2*np.pi
    for i in range(thetas.size):
        scanning_vectors[:,i] = np.exp(complex_pi * (x*np.cos(np.deg2rad(thetas[i])) + y*np.sin(np.deg2rad(thetas[i]))))

    return np.ascontiguousarray(scanning_vectors)

@njit(fastmath=True, cache=True)
def DOA_plot_util(DOA_data, log_scale_min=-100):
    """
        This function prepares the calulcated DoA estimation results for plotting.

        - Noramlize DoA estimation results
        - Changes to log scale
    """

    DOA_data = np.divide(np.abs(DOA_data), np.max(np.abs(DOA_data))) # Normalization
    DOA_data = 10*np.log10(DOA_data) # Change to logscale

    for i in range(len(DOA_data)): # Remove extremely low values
        if DOA_data[i] < log_scale_min:
            DOA_data[i] = log_scale_min

    return DOA_data

@njit(fastmath=True, cache=True)
def calculate_doa_papr(DOA_data):
    return 10*np.log10(np.max(np.abs(DOA_data))/np.mean(np.abs(DOA_data)))

# Old time-domain squelch algorithm (Unused as freq domain FFT with overlaps gives significantly better sensitivity with acceptable time resolution expense
"""
    K = 10
    self.filtered_signal = self.raw_signal_amplitude #convolve(np.abs(self.raw_signal_amplitude),np.ones(K), mode = 'same')/K

    # Burst is always started at the begining of the processed block, ensured by the squelch module in the DAQ FW
    burst_stop_index  = len(self.filtered_signal) # CARL FIX: Initialize this to the length of the signal, incase the signal is active the entire time
    self.logger.info("Original burst stop index: {:d}".format(burst_stop_index))

    min_burst_size = K                    
    burst_stop_amp_val = 0
    for n in np.arange(K, len(self.filtered_signal), 1):                        
        if self.filtered_signal[n] < self.squelch_threshold:
            burst_stop_amp_val = self.filtered_signal[n]
            burst_stop_index = n
            burst_stop_index-=K # Correction with the length of filter
            break

        #burst_stop_index-=K # Correction with the length of filter


    self.logger.info("Burst stop index: {:d}".format(burst_stop_index))
    self.logger.info("Burst stop ampl val: {:f}".format(burst_stop_amp_val))
    self.logger.info("Processed signal length: {:d}".format(len(self.processed_signal[0,:])))

    # If sign
    if burst_stop_index < min_burst_size:
        self.logger.debug("The length of the captured burst size is under the minimum: {:d}".format(burst_stop_index))
        burst_stop_index = 0

    if burst_stop_index !=0:                        
        self.logger.info("INSIDE burst_stop_index != 0")

       self.logger.debug("Burst stop index: {:d}".format(burst_stop_index))
       self.logger.debug("Burst stop ampl val: {:f}".format(burst_stop_amp_val))
       self.squelch_mask = np.zeros(len(self.filtered_signal))                        
       self.squelch_mask[0 : burst_stop_index] = np.ones(burst_stop_index)*self.squelch_threshold
       # Next line removes the end parts of the samples after where the signal ended, truncating the array
       self.processed_signal = self.module_receiver.iq_samples[: burst_stop_index, self.squelch_mask == self.squelch_threshold]
       self.logger.info("Raw signal length when burst_stop_index!=0: {:d}".format(len(self.module_receiver.iq_samples[0,:])))
       self.logger.info("Processed signal length when burst_stop_index!=0: {:d}".format(len(self.processed_signal[0,:])))

       #self.logger.info(' '.join(map(str, self.processed_signal)))

       self.data_ready=True
   else:
       self.logger.info("Signal burst is not found, try to adjust the threshold levels")
       #self.data_ready=True                            
       self.squelch_mask = np.ones(len(self.filtered_signal))*self.squelch_threshold
       self.processed_signal = np.zeros([self.channel_number, len(self.filtered_signal)])
"""


