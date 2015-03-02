import os.path
# Copyright 2012 BrewPi/Elco Jacobs.
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

import subprocess as sub
import serial
import time
import simplejson as json
import os
import brewpiVersion
import expandLogMessage
import settingRestore
from sys import stderr
import BrewPiUtil as util


def printStdErr(string):
    print >> stderr, string + '\n'


def asbyte(v):
    return chr(v & 0xFF)


class LightYModem:
    """
    Receive_Packet
    - first byte SOH/STX (for 128/1024 byte size packets)
    - EOT (end)
    - CA CA abort
    - ABORT1 or ABORT2 is abort

    Then 2 bytes for seqno (although the sequence number isn't checked)

    Then the packet data

    Then CRC16?

    First packet sent is a filename packet:
    - zero-terminated filename
    - file size (ascii) followed by space?
    """

    packet_len = 1024
    stx = 2
    eot = 4
    ack = 6
    nak = 0x15
    ca =  0x18
    crc16 = 0x43
    abort1 = 0x41
    abort2 = 0x61

    def __init__(self):
        self.seq = None
        self.ymodem = None

    def _read_response(self):
        ch1 = ''
        while not ch1:
            ch1 = self.ymodem.read(1)
        ch1 = ord(ch1)
        if ch1==LightYModem.ack and self.seq==0:    # may send also a crc16
            ch2 = self.ymodem.read(1)
        elif ch1==LightYModem.ca:                   # cancel, always sent in pairs
            ch2 = self.ymodem.read(1)
        return ch1

    def _send_ymodem_packet(self, data):
        # pad string to 1024 chars
        data = data.ljust(LightYModem.packet_len)
        seqchr = asbyte(self.seq & 0xFF)
        seqchr_neg = asbyte((-self.seq-1) & 0xFF)
        crc16 = '\x00\x00'
        packet = asbyte(LightYModem.stx) + seqchr + seqchr_neg + data + crc16
        if len(packet)!=1029:
            raise Exception("packet length is wrong!")

        self.ymodem.write(packet)
        self.ymodem.flush()
        response = self._read_response()
        if response==LightYModem.ack:
            printStdErr("sent packet nr %d " % (self.seq))
            self.seq += 1
        return response

    def _send_close(self):
        self.ymodem.write(asbyte(LightYModem.eot))
        self.ymodem.flush()
        response = self._read_response()
        if response == LightYModem.ack:
            self.send_filename_header("", 0)
            self.ymodem.close()

    def send_packet(self, file, output):
        response = LightYModem.eot
        data = file.read(LightYModem.packet_len)
        if len(data):
            response = self._send_ymodem_packet(data)
        return response

    def send_filename_header(self, name, size):
        self.seq = 0
        packet = name + asbyte(0) + str(size) + ' '
        return self._send_ymodem_packet(packet)

    def transfer(self, file, ymodem, output):
        self.ymodem = ymodem
        """
        file: the file to transfer via ymodem
        ymodem: the ymodem endpoint (a file-like object supporting write)
        output: a stream for output messages
        """
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(0, os.SEEK_SET)
        response = self.send_filename_header("binary", size)
        while response==LightYModem.ack:
            response = self.send_packet(file, output)

        file.close()
        if response==LightYModem.eot:
            self._send_close()

        return response


def fetchBoardSettings(boardsFile, boardType):
    boardSettings = {}
    for line in boardsFile:
        if line.startswith(boardType):
            setting = line.replace(boardType + '.', '', 1).strip()  # strip board name, period and \n
            [key, sign, val] = setting.rpartition('=')
            boardSettings[key] = val
    return boardSettings


def loadBoardsFile(arduinohome):
    return open(arduinohome + 'hardware/arduino/boards.txt', 'rb').readlines()


def openSerial(port, altport, baud, timeoutVal):
    # open serial port
    try:
        ser = serial.Serial(port, baud, timeout=timeoutVal)
        return [ser, port]
    except serial.SerialException as e:
        if altport:
            try:
                ser = serial.Serial(altport, baud, timeout=timeoutVal)
                return [ser, altport]
            except serial.SerialException as e:
                pass
        return [None, None]



def programArduino(config, boardType, hexFile, restoreWhat):
    programmer = SerialProgrammer.create(config, boardType)
    return programmer.program(hexFile, restoreWhat)


def json_decode_response(line):
    try:
        return json.loads(line[2:])
    except json.decoder.JSONDecodeError, e:
        printStdErr("JSON decode error: " + str(e))
        printStdErr("Line received was: " + line)

msg_map = { "a" : "Arduino" }

class SerialProgrammer:

    @staticmethod
    def create(config, boardType):
        if boardType=='spark-core':
            msg_map["a"] = "Spark Core"
            programmer = SparkProgrammer(config, boardType)
        else:
            msg_map["a"] = "Arduino"
            programmer = ArduinoProgrammer(config, boardType)
        return programmer

    def __init__(self, config):
        self.config = config
        self.restoreSettings = False
        self.restoreDevices = False
        self.ser = None
        self.port = None
        self.avrVersionNew = None
        self.avrVersionOld = None
        self.oldSettings = {}

    def program(self, hexFile, restoreWhat):
        printStdErr("****    %(a)s Program script started    ****" % msg_map)

        self.parse_restore_settings(restoreWhat)
        if not self.open_serial(self.config, 57600, 0.2):
            return 0

        self.delay_serial_open()

        # request all settings from board before programming
        printStdErr("Checking old version before programming.")
        if self.fetch_current_version():
            self.save_settings()

        if not self.flash_file(hexFile):
            return 0

        printStdErr("Waiting for device to reset.")

        # wait for serial to close
        retries = 30
        while retries and self.ser:
            self.ser.close()
            self.ser = None
            time.sleep(1)
            self.ser, self.port = openSerial(self.config['port'], self.config.get('altport'), 57600, 0.2)
            retries -= 1

        retries = 30
        while retries and not self.ser:
            time.sleep(1)
            self.ser, self.port = openSerial(self.config['port'], self.config.get('altport'), 57600, 0.2)
            retries -= 1

        if not self.ser:
            printStdErr("Error opening serial port after programming. Program script will exit. Settings are not restored.")
            return False

        time.sleep(1)
        self.fetch_new_version()
        self.reset_settings(self.ser)

        printStdErr("Now checking which settings and devices can be restored...")
        if self.avrVersionNew is None:
            printStdErr(("Warning: Cannot receive version number from %(a)s after programming. " +
                         "Something must have gone wrong. Restoring settings/devices settings failed.\n" % msg_map))
            return 0
        if self.avrVersionOld is None:
            printStdErr("Could not receive version number from old board, " +
                        "No settings/devices are restored.")
            return 0
        self.restore_settings()
        self.restore_devices()
        printStdErr("****    Program script done!    ****")
        printStdErr("If you started the program script from the web interface, BrewPi will restart automatically")
        self.ser.close()
        return 1

    def parse_restore_settings(self, restoreWhat):
        restoreSettings = False
        restoreDevices = False
        if 'settings' in restoreWhat:
            if restoreWhat['settings']:
                restoreSettings = True
        if 'devices' in restoreWhat:
            if restoreWhat['devices']:
                restoreDevices = True
        # Even when restoreSettings and restoreDevices are set to True here,
        # they might be set to false due to version incompatibility later

        printStdErr("Settings will " + ("" if restoreSettings else "not ") + "be restored" +
                    (" if possible" if restoreSettings else ""))
        printStdErr("Devices will " + ("" if restoreDevices else "not ") + "be restored" +
                    (" if possible" if restoreSettings else ""))
        self.restoreSettings = restoreSettings
        self.restoreDevices = restoreDevices

    def open_serial(self, config, baud, timeout):
        self.ser, self.port = openSerial(config['port'], config.get('altport'), baud, timeout)
        if self.ser is None:
            printStdErr("Could not open serial port. Programming aborted.")
            return False
        return True

    def delay_serial_open(self):
        pass

    def fetch_version(self, msg):
        version = brewpiVersion.getVersionFromSerial(self.ser)
        if version is None:
            printStdErr(("Warning: Cannot receive version number from %(a)s. " +
                         "Your %(a)s is either not programmed yet or running a very old version of BrewPi. "
                         "%(a)s will be reset to defaults." % msg_map))
        else:
            printStdErr(msg+"Found " + version.toExtendedString() +
                        " on port " + self.port + "\n")
        return version

    def fetch_current_version(self):
        self.avrVersionOld = self.fetch_version("Checking current version: ")
        return self.avrVersionOld

    def fetch_new_version(self):
        self.avrVersionNew = self.fetch_version("Checking new version: ")
        return self.avrVersionNew

    def save_settings(self):
        ser, oldSettings = self.ser, self.oldSettings
        oldSettings.clear()
        printStdErr("Requesting old settings from %(a)s..." % msg_map)
        expected_responses = 2
        if self.avrVersionOld.minor > 1:  # older versions did not have a device manager
            expected_responses += 1
            ser.write("d{}")  # installed devices
            time.sleep(1)
        ser.write("c")  # control constants
        ser.write("s")  # control settings
        time.sleep(2)

        while expected_responses:
            line = ser.readline()
            if line:
                if line[0] == 'C':
                    expected_responses -= 1
                    oldSettings['controlConstants'] = json_decode_response(line)
                elif line[0] == 'S':
                    expected_responses -= 1
                    oldSettings['controlSettings'] = json_decode_response(line)
                elif line[0] == 'd':
                    expected_responses -= 1
                    oldSettings['installedDevices'] = json_decode_response(line)

        oldSettingsFileName = 'oldAvrSettings-' + time.strftime("%b-%d-%Y-%H-%M-%S") + '.json'

        scriptDir = util.scriptPath()  # <-- absolute dir the script is in
        if not os.path.exists(scriptDir + '/settings/avr-backup/'):
            os.makedirs(scriptDir + '/settings/avr-backup/')

        oldSettingsFile = open(scriptDir + '/settings/avr-backup/' + oldSettingsFileName, 'wb')
        oldSettingsFile.write(json.dumps(oldSettings))
        oldSettingsFile.truncate()
        oldSettingsFile.close()
        printStdErr("Saved old settings to file " + oldSettingsFileName)

    def delay(self, countDown):
        while countDown > 0:
            time.sleep(1)
            countDown -= 1
            printStdErr("Back up in " + str(countDown) + "...")

    def flash_file(self, hexFile):
        raise Exception("not implemented")

    def reset_settings(self, ser):
        printStdErr("Resetting EEPROM to default settings")
        ser.write('E')
        time.sleep(5)  # resetting EEPROM takes a while, wait 5 seconds
        line = ser.readline()
        if line:  # line available?
            if line[0] == 'D':
                # debug message received
                try:
                    expandedMessage = expandLogMessage.expandLogMessage(line[2:])
                    printStdErr(("%(a)s debug message: " % msg_map) + expandedMessage)
                except Exception, e:  # catch all exceptions, because out of date file could cause errors
                    printStdErr("Error while expanding log message: " + str(e))
                    printStdErr(("%(a)s debug message was: " % msg_map) + line[2:])

    def print_debug_log(self, line):
        try:  # debug message received
            expandedMessage = expandLogMessage.expandLogMessage(line[2:])
            printStdErr(expandedMessage)
        except Exception, e:  # catch all exceptions, because out of date file could cause errors
            printStdErr("Error while expanding log message: " + str(e))
            printStdErr(("%(a)s debug message: " % msg_map) + line[2:])

    def restore_settings(self):
        ser, avrVersionOld, avrVersionNew, oldSettings = self.ser, self.avrVersionOld, self.avrVersionNew, self.oldSettings
        if self.restoreSettings:
            printStdErr("Trying to restore compatible settings from " +
                        avrVersionOld.toString() + " to " + avrVersionNew.toString())
            settingsRestoreLookupDict = {}
            if avrVersionNew.toString() == avrVersionOld.toString():
                printStdErr("New version is equal to old version, restoring all settings")
                settingsRestoreLookupDict = "all"
            elif avrVersionNew.major == 0 and avrVersionNew.minor == 2:
                if avrVersionOld.major == 0:
                    if avrVersionOld.minor == 0:
                        printStdErr("Could not receive version number from old board, " +
                                    "resetting to defaults without restoring settings.")
                        self.restoreDevices = False
                        self.restoreSettings = False
                    elif avrVersionOld.minor == 1:
                        # version 0.1.x, try to restore most of the settings
                        settingsRestoreLookupDict = settingRestore.keys_0_1_x_to_0_2_x
                        printStdErr("Settings can only be partially restored when going from 0.1.x to 0.2.x")
                        self.restoreDevices = False
                    elif avrVersionOld.minor == 2:
                        # restore settings and devices
                        if avrVersionNew.revision == 0:
                            settingsRestoreLookupDict = settingRestore.keys_0_2_x_to_0_2_0
                        elif avrVersionNew.revision == 1:
                            settingsRestoreLookupDict = settingRestore.keys_0_2_x_to_0_2_1
                        elif avrVersionNew.revision == 2:
                            settingsRestoreLookupDict = settingRestore.keys_0_2_x_to_0_2_2
                        elif avrVersionNew.revision == 3:
                            settingsRestoreLookupDict = settingRestore.keys_0_2_x_to_0_2_3
                        elif avrVersionNew.revision == 4:
                            if avrVersionOld.revision >= 3:
                                settingsRestoreLookupDict = settingRestore.keys_0_2_3_to_0_2_4
                            else:
                                settingsRestoreLookupDict = settingRestore.keys_0_2_x_to_0_2_4
                        printStdErr("Will try to restore compatible settings")
            else:
                printStdErr("Sorry, settings can only be restored when updating to BrewPi 0.2.0 or higher")

            self.restore_settings_dict(ser, oldSettings, settingsRestoreLookupDict)
            printStdErr("restoring settings done!")
        else:
            printStdErr("No settings to restore!")

    def send_restored_settings(self, restoredSettings, ser):
        printStdErr("Restoring these settings: " + json.dumps(restoredSettings))
        for key in settingRestore.restoreOrder:
            if key in restoredSettings.keys():
                # send one by one or the arduino cannot keep up
                if restoredSettings[key] is not None:
                    command = "j{" + str(key) + ":" + str(restoredSettings[key]) + "}\n"
                    ser.write(command)
                    time.sleep(0.5)
                # read all replies
                while 1:
                    line = ser.readline()
                    if line:  # line available?
                        if line[0] == 'D':
                            self.print_debug_log(line)
                    else:
                        break

    def retrieve_settings(self, ser):
        ccNew = {}
        csNew = {}
        tries = 0
        outstanding = set()
        while (ccNew == {} or csNew == {}) or len(outstanding):
            if ccNew == {} and not 'c' in outstanding:
                ser.write('c')
                outstanding.add('c')
                tries += 1
            if csNew == {} and not 's' in outstanding:
                ser.write('s')
                outstanding.add('s')
                tries += 1

            line = ser.readline()
            while line:
                if line[0] == 'C':
                    outstanding.remove('c')
                    ccNew = json_decode_response(line)
                elif line[0] == 'S':
                    outstanding.remove('s')
                    csNew = json_decode_response(line)
                elif line[0] == 'D':
                    self.print_debug_log(line)
                line = ser.readline() if outstanding else None

            if tries>10:
                printStdErr("Could not receive all keys for settings to restore from %(a)s" % msg_map)
                break

        return ccNew, csNew

    def restore_settings_dict(self, ser, oldSettings, settingsRestoreLookupDict):
        restoredSettings = {}
        ccOld = oldSettings['controlConstants']
        csOld = oldSettings['controlSettings']

        ccNew, csNew = self.retrieve_settings(ser)

        printStdErr("Trying to restore old control constants and settings")
        # find control constants to restore
        for key in ccNew.keys():  # for all new keys
            if settingsRestoreLookupDict == "all":
                restoredSettings[key] = ccOld[key]
            else:
                for alias in settingRestore.getAliases(settingsRestoreLookupDict, key):  # get valid aliases in old keys
                    if alias in ccOld.keys():  # if they are in the old settings
                        restoredSettings[key] = ccOld[alias]  # add the old setting to the restoredSettings

        # find control settings to restore
        for key in csNew.keys():  # for all new keys
            if settingsRestoreLookupDict == "all":
                restoredSettings[key] = csOld[key]
            else:
                for alias in settingRestore.getAliases(settingsRestoreLookupDict, key):  # get valid aliases in old keys
                    if alias in csOld.keys():  # if they are in the old settings
                        restoredSettings[key] = csOld[alias]  # add the old setting to the restoredSettings

        self.send_restored_settings(restoredSettings, ser)

    def restore_devices(self):
        ser = self.ser
        if self.restoreDevices:
            printStdErr("Now trying to restore previously installed devices: " + str(self.oldSettings['installedDevices']))
            detectedDevices = None
            for device in self.oldSettings['installedDevices']:
                printStdErr("Restoring device: " + json.dumps(device))
                if "a" in device.keys(): # check for sensors configured as first on bus
                    if int(device['a'], 16) == 0:
                        printStdErr("OneWire sensor was configured to autodetect the first sensor on the bus, " +
                                    "but this is no longer supported. " +
                                    "We'll attempt to automatically find the address and add the sensor based on its address")
                        if detectedDevices is None:
                            ser.write("h{}")  # installed devices
                            time.sleep(1)
                            # get list of detected devices
                            for line in ser:
                                if line[0] == 'h':
                                    detectedDevices = json_decode_response(line)

                        for detectedDevice in detectedDevices:
                            if device['p'] == detectedDevice['p']:
                                device['a'] = detectedDevice['a'] # get address from sensor that was first on bus

                ser.write("U" + json.dumps(device))

                time.sleep(3)  # give the Arduino time to respond

                # read log messages from arduino
                while 1:  # read all lines on serial interface
                    line = ser.readline()
                    if line:  # line available?
                        if line[0] == 'D':
                            self.print_debug_log(line)
                        elif line[0] == 'U':
                            printStdErr(("%(a)s reports: device updated to: " % msg_map) + line[2:])
                    else:
                        break
            printStdErr("Restoring installed devices done!")
        else:
            printStdErr("No devices to restore!")


class SparkProgrammer(SerialProgrammer):
    def __init__(self, config, boardType):
        SerialProgrammer.__init__(self, config)

    def flash_file(self, hexFile):
        self.ser.write('F')
        line = self.ser.readline()
        printStdErr(line)
        time.sleep(0.2)

        file = open(hexFile, 'rb')
        result = LightYModem().transfer(file, self.ser, stderr)
        file.close()
        success = result==LightYModem.eot
        printStdErr("File flashed successfully" if success else "Problem flashing file: "+str(result))
        return success


class ArduinoProgrammer(SerialProgrammer):
    def __init__(self, config, boardType):
        SerialProgrammer.__init__(self, config)
        self.boardType = boardType

    def delay_serial_open(self):
        time.sleep(5)  # give the arduino some time to reboot in case of an Arduino UNO

    def flash_file(self, hexFile):
        config, boardType = self.config, self.boardType
        printStdErr("Loading programming settings from board.txt")
        arduinohome = config.get('arduinoHome', '/usr/share/arduino/')  # location of Arduino sdk
        avrdudehome = config.get('avrdudeHome', arduinohome + 'hardware/tools/')  # location of avr tools
        avrsizehome = config.get('avrsizeHome', '')  # default to empty string because avrsize is on path
        avrconf = config.get('avrConf', avrdudehome + 'avrdude.conf')  # location of global avr conf

        boardsFile = loadBoardsFile(arduinohome)
        boardSettings = fetchBoardSettings(boardsFile, boardType)

        # parse the Arduino board file to get the right program settings
        for line in boardsFile:
            if line.startswith(boardType):
                # strip board name, period and \n
                setting = line.replace(boardType + '.', '', 1).strip()
                [key, sign, val] = setting.rpartition('=')
                boardSettings[key] = val

        printStdErr("Checking hex file size with avr-size...")

        # start programming the Arduino
        avrsizeCommand = avrsizehome + 'avr-size ' + "\"" + hexFile + "\""

        # check program size against maximum size
        p = sub.Popen(avrsizeCommand, stdout=sub.PIPE, stderr=sub.PIPE, shell=True)
        output, errors = p.communicate()
        if errors != "":
            printStdErr('avr-size error: ' + errors)
            return False

        programSize = output.split()[7]
        printStdErr(('Program size: ' + programSize +
                     ' bytes out of max ' + boardSettings['upload.maximum_size']))

        # Another check just to be sure!
        if int(programSize) > int(boardSettings['upload.maximum_size']):
            printStdErr("ERROR: program size is bigger than maximum size for your Arduino " + boardType)
            return False

        hexFileDir = os.path.dirname(hexFile)
        hexFileLocal = os.path.basename(hexFile)

        programCommand = (avrdudehome + 'avrdude' +
                          ' -F ' +  # override device signature check
                          ' -e ' +  # erase flash and eeprom before programming. This prevents issues with corrupted EEPROM
                          ' -p ' + boardSettings['build.mcu'] +
                          ' -c ' + boardSettings['upload.protocol'] +
                          ' -b ' + boardSettings['upload.speed'] +
                          ' -P ' + self.port +
                          ' -U ' + 'flash:w:' + "\"" + hexFileLocal + "\"" +
                          ' -C ' + avrconf)

        printStdErr("Programming Arduino with avrdude: " + programCommand)

        # open and close serial port at 1200 baud. This resets the Arduino Leonardo
        # the Arduino Uno resets every time the serial port is opened automatically
        self.ser.close()
        del self.ser  # Arduino won't reset when serial port is not completely removed
        if boardType == 'leonardo':
            if not self.open_serial(self.config, 1200, 0.2):
                printStdErr("Could not open serial port at 1200 baud to reset Arduino Leonardo")
                return False

            self.ser.close()
            time.sleep(1)  # give the bootloader time to start up

        p = sub.Popen(programCommand, stdout=sub.PIPE, stderr=sub.PIPE, shell=True, cwd=hexFileDir)
        output, errors = p.communicate()

        # avrdude only uses stderr, append its output to the returnString
        printStdErr("result of invoking avrdude:\n" + errors)

        printStdErr("avrdude done!")

        printStdErr("Giving the Arduino a few seconds to power up...")
        self.delay(6)
        return True

def test_program_spark_core():
    file = "R:\\dev\\brewpi\\firmware\\platform\\spark\\target\\brewpi.bin"
    config = { "port" : "COM22" }
    result = programArduino(config, "spark-core", file, { "settings":True, "devices":True})
    printStdErr("Result is "+str(result))

if __name__ == '__main__':
    test_program_spark_core()
