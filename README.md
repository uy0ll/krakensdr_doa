<h2>Kraken SDR DoA DSP</h2>
This software is intended to demonstrate the direction of arrival (DoA) estimation capabilities of the KrakenSDR and other RTL-SDR based coherent receiver systems which use the compatible data acquisition system - HeIMDALL DAQ Firmware.
<br>
<br>
The complete application is broken down into two main modules in terms of implementation, into the DAQ Subsystem and to the DSP Subsystem. These two modules can operate together either remotely through Ethernet connection or locally on the same host using shared-memory.

Running these two subsystems on separate processing units can grant higher throughput and stability, while running on the same processing unit makes the entire system more compact.

<h4>Install pre-reqs</h4>

``` bash
sudo apt update
sudo apt install php-cli
```

<h4>Install Heimdall DAQ</h4>

First, follow the instructions at https://github.com/krakenrf/heimdall_daq_fw/tree/development to install the Heimdall DAQ Firmware.

<h4>Set up Miniconda environment</h4>

Please run the installs in this order as we need to ensure a specific version of dash is installed.

``` bash
conda activate kraken

conda install quart
conda install pandas
conda install orjson
conda install matplotlib

pip3 install dash_bootstrap_components
pip3 install quart_compress
pip3 install dash_devices
pip3 install pyargus

conda install dash==1.20.0
```

<h4>Install the krakensdr_doa software</h4>

```bash
cd ~/krakensdr
git clone https://github.com/krakenrf/krakensdr_doa
cd krakensdr_doa
git checkout clientside_graphs
```

Copy the the *krakensdr_doa/util/kraken_doa_start.sh* and the *krakensdr_doa/util/kraken_doa_stop.sh* scripts into the krakensdr root folder of the project.
```bash
cd ~/krakensdr
cp krakensdr_doa/util/kraken_doa_start.sh .
cp krakensdr_doa/util/kraken_doa_stop.sh .
```

<h3>Running</h3>

<h4>Local operation (Recommended)</h4>

You can run the complete application on a single host either by using Ethernet interface between the two subsystems or by using a shared-memory interface. Using shared-memory is the recommended in this situation. 

```bash
./kraken_doa_start.sh
```

Please be patient on the first run, at it can take 1-2 minutes for the JIT numba compiler to compile the optimized functions. On subsqeuent runs this will be faster and read from cache.

<h4>Remote operation</h4>

1. Start the DAQ Subsystem either remotely. (Make sure that the *daq_chain_config.ini* contains the proper configuration) 
    (See:https://github.com/krakenrf/heimdall_daq_fw/Documentation)
2. Set the IP address of the DAQ Subsystem in the settings.json, *default_ip* field.
3. Start the DoA DSP software by typing:
`./gui_run.sh`
4. To stop the server and the DSP processing chain run the following script:
`./kill.sh`

<p1> After starting the script a web based server opens at port number 8051, which then can be accessed by typing "KRAKEN_IP:8050/" in the address bar of any web browser. You can find the IP address of the KrakenSDR Pi4 wither via your routers WiFi management page, or by typing "ip addr" into the terminal. You can also use the hostname of the Pi4 in place of the IP address, but this only works on local networks, and not the internet, or mobile hotspot networks. </p1>

  ![image info](./doc/kraken_doadsp_main.png)


This software was 95% developed by Tamas Peto, and makes use of his pyAPRIL and pyARGUS libraries. See his website at www.tamaspeto.com
