# LDMX Power Supply Control
The power supplies are controlled by a few python scripts.

## MPOD Control
The ECal and Tracker LV and HV are connected to MPODs.
This is a simple class and script to control MPOD modules through SNMP commands

### Dependencies
Used and tested with [net-snmp](https://www.net-snmp.org/) v5.9.4.

Need [`WIENER-CRATE-MIB.txt`](WIENER-CRATE-MIB.txt) to specify how MPOD talks via SNMP.
This file should be placed in the installation location of net-snmp
in the `install/share/snmp/mibs` subdirectory along side the other configuration files.

### Usage
Besides access to the executables installed by `net-snmp`, we
avoid using any other Python libraries.
```
python mpod_control.py [--dry-run] {enable|disable|print} {config.json}
```

Two environment variables are checked
- `MPOD_CRATE_IP` defines the IP address of the MPOD Crate
- `NET_SNMP_INSTALL` defines where net-snmp is installed

## HCal PS Control
The HCal power supplies are serially connected to the DAQ server over USB.
The HV and LV power supplies are B&K Precision 1698.

> [!NOTE]
> The LV line has an initial pulse of current that is relatively far above the
> normal operating current (~1A), so the power supply current limit should
> be set to 3A to allow this pulse through when the power is initially connected.

In order to provide access to these power supplies via the USB connection to
the server, your friendly neighborhood administrator needs to `sudo chmod 666 /dev/ttyUSBii`
for the specific USB number enumerated after the DAQ server is powered on.

### Usage
Use the `USBmappingJSON.sh` script to list out the different devices connected via USB.
```
./USBmappingJSON.sh
```
From this list, you can use the `Serial` column as the `--serial` argument and the VID:PID
column as the `--vid_pid` argument. The file used for the 2026 cosmics test stand is
[kept in this repository](.hcal-power-supply-names.json) for reference. It only needs to
be changed if the hardware itself changes.

And then you `--read` the current settings.
```
python hcal-ps-control.py --name hhv --read
```
and turn on/off with the `--output` argument.
```
python hcal-ps-control.py --name lv --output on
```

## TS PS Control
One of the TS power supplies is a B&K Precision that is also serially connected to the DAQ server over USB like the HCal poewr supplies.
The other is a Rigol connected to the DAQ server over Ethernet.

### Dependencies
Same comment about permissions changes for USB connections after the DAQ server
is re-powered and the USB devices re-enumerated.

The `vxi11` python package is used for interacting with the Rigol. I don't know which one it is.

### Usage
The `USBmappingJSON.sh` can be used to deduce which USB device is the TS B&K Precision.
Similar network-scanning utilities can check the LAN for the Rigol.
