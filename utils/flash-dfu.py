# Copyright 2015 BrewPi
# This file is part of BrewPi.

# BrewPi is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# BrewPi is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with BrewPi.  If not, see <http://www.gnu.org/licenses/>.

import sys
# Check needed software dependencies to nudge users to fix their setup
if sys.version_info < (2, 7):
    print "Sorry, requires Python 2.7."
    sys.exit(1)

# standard libraries
import time
import getopt
import subprocess
import serial
import os

# choose an implementation, depending on os (copied from serial.tools.list_ports)
if os.name == 'nt': #sys.platform == 'win32':
    import serial.tools.list_ports_windows as serial_tools
elif os.name == 'posix':
    import serial.tools.list_ports_posix as serial_tools
else:
    raise ImportError("Sorry: no implementation for your platform ('%s') available" % (os.name,))


# Read in command line arguments
try:
    opts, args = getopt.getopt(sys.argv[1:], "hf:m",
                               ['help', 'file=', 'multi'])
except getopt.GetoptError:
    print "Unknown parameter, available Options: --file, --multi"

    sys.exit()

multi = False
binFile = None

for o, a in opts:
    # print help message for command line options
    if o in ('-h', '--help'):
        print "\n Available command line options: "
        print "--help: print this help message"
        print "--file: path to .bin file to flash"
        print "--multi: keep the script alive to flash multiple devices"
        exit()
    # supply a config file
    if o in ('-f', '--config'):
        binFile = os.path.abspath(a)
        if not os.path.exists(binFile):
            sys.exit('ERROR: Binary file "%s" was not found!' % binFile)
    # send quit instruction to all running instances of BrewPi
    if o in ('-m', '--multi'):
        multi = True
        print "Started in multi flash mode"

# check whether dfu-util can be found
if sys.platform.startswith('win'):
    p = subprocess.Popen("where dfu-util", stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
else:
    p = subprocess.Popen("which dfu-util", stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
p.wait()
output, errors = p.communicate()
if not output:
    print "dfu-util cannot be found, please add its location to your PATH variable"
    exit(1)

def all_ports():
    result = []
    for port in serial_tools.comports():
        result.append(port[0])
    return result

def new_ports(old_ports):
    result = []
    for port in serial_tools.comports():
        if not port[0] in old_ports:
            result.append(port[0])
    return result

while(True):
    # list DFU devices to see whether a device is connected
    p = subprocess.Popen("dfu-util -l", stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    p.wait()
    output, errors = p.communicate()
    if errors:
        print errors
    if "Found" in output:
        # found a DFU device, flash the binary file to it
        if binFile:
            print "found DFU device, now flashing %s \n\n" % binFile
            existing_ports = all_ports() # get existing ports while in DFU mode, because it will exclude the port we are looking for
            p = subprocess.Popen("dfu-util -d 1d50:607f -a 0 -s 0x08005000:leave -D %s" % binFile, shell=True)
            p.wait()
            print "Waiting for new serial ports"
            ser = None
            for i in range(0,10):
                newPorts = new_ports(existing_ports)
                if newPorts:
                    port = newPorts[0] # assume device is first newly found port
                    try:
                        print("Opening serial port {0}".format(port))
                        ser = serial.Serial(port, 57600, timeout=1)
                    except (OSError, serial.SerialException) as e:
                            print("Error opening serial port {0}. {1}".format(port, str(e)))

                    if not ser:
                        continue

                    # wait until valid version string received. Device can be waiting in touch screen calibration
                    print "Waiting until I receive the version info from the device"
                    while 1:
                        ser.write('n')
                        answer = ser.readline()
                        if answer and answer[0] == 'N':
                            print "Version: {0}".format(answer)
                            break

                    while ser.readline():
                        pass # discard leftover lines

                    print "Resetting EEPROM"
                    ser.write('E\n')
                    answer = ser.readline()
                    if "\"logID\":15" in answer:
                        print "Successfully initialized EEPROM"
                    else:
                        print "Received unexpected reply: {0}".format(answer)
                    break
                else:
                    time.sleep(1)
            else:
                # i reached 10, timeout
                print "could not open serial port of newly programmed device, EEPROM not reset"
        else:
            print "found DFU device, but no binary specified for flashing"
        if not multi:
            break
        else:
            print "Waiting for next device...\n"

    time.sleep(1)
