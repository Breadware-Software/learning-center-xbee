'''
Copyright (c) 2023, Breadware LLC

Project: Remote Monitoring
Date Modified: March 2 2021
Version: 0.0.1

Description: Proof-of-concept for a device that leverages
5G cellular technology to monitor and report the temperature,
humidity, and location (lat/lon) analog and digital inputs

Sensor and GPS Code Contributed by: Mouser, Digi, Breadware, Green Shoe Garage

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''

import ujson as json
import time, machine
import os
import socket, network, xbee
import ustruct as struct
from machine import unique_id, Pin
from m1mqtt import MQTTClient, MQTTNetworkError, MQTTBadUsernameOrPassword
from sys import stdin, stdout
from machine import UART
from machine import I2C
from hdc1080 import HDC1080
import micropython
from machine import WDT

# Constants
FW_VERSION = "20210426.0"                       # XB firmware version (sent during connect)
PATH_ROOT = "/flash"                            # root of file system
CREDS_FN = 'devicecreds.json'                   # basic user credentials (generated during registartion)
LAST_ENV_FN = 'lastenv.txt'                     # last env device was connected to (generated when switching env)
PRINT_OVER_UART = True                          # enabled print mode (for debugging purposes), disable when connected to MCU
HEARTBEAT_INTERVAL_SECONDS = 3600               # default 1 hr = 3600
DATA_INTERVAL_SECONDS = 60 * 10                 # 10 minutes
UART_OUT_DELIMITER = '\n'                       # delimiter for uart out
UART_IN_DELIMITER_A = '\n'                      # uart input delimiters
UART_IN_DELIMITER_B = '\r'
RESET_IN_SECONDS = 10                           # delay to reset if exception is caught
INITIAL_BOOT_WAIT_SECONDS = 30                  # Time to wait before booting to allow for keyboard interrupt
WAIT_DNS_RETRY_SECONDS = 10                     # Time to retry if DNS is not ready
WAIT_REGISTRATION_RETRY_SECONDS = 20            # Time to re-registration after MQTT connect error
MAX_WAIT_FOR_DNS = 10
GNGGA_MARK = "$GNGGA"                           # GPS interpretation
GNGSA_MARK = "$GNGSA"
HAS_GPS = True                                  # Set to False if no GPS board

# State machine constants
WAIT_CELLULAR = 0                               # Waiting for radio state
REGISTER = 1                                    # Device register state
WAIT_REGISTRATION = 7                           # Waiting for registration
AUTHENTICATING = 2                              # Authenticating
CONNECTED = 3                                   # Connected
NO_REGISTRATION_FILE = 4                        # no registration file
FAILED_REGISTRATION = 5                         # failed registration
INIT_CELLULAR = 6                               # init cellular
WAIT_FOR_DNS = 8                                # wait for DNS availability
WAIT_RETRY_REGISTRATION = 9                     # Pending Re-register
WAIT_REBOOT = 10                                # Pending Reboot state

# init vars
api_user, api_password = '',''                  # basic api user credentials
event_data = ""                                 # payload to console
heartbeat_time = 0                              # next time HB to be sent init 0
datatransmit_time = 0                           # next time to transmit data
state = INIT_CELLULAR
reg_mqtt = None
stopwatch = 0
count_wait_for_dns = 0

#Define GPIO pins
di0 = Pin("D0", Pin.IN, Pin.PULL_UP) # pin 20, button
di1 = Pin("D6", Pin.IN) # pin 16
do0 = Pin("D7", Pin.OUT) # pin 12
di2 = Pin("D8", Pin.IN) # pin 9

di0_last = None
di1_last = None
do0_last = None
di2_last = None

# map tags to pins, used to process controls from cloud
tag_to_pin = {"di0": di0, "di1": di1, "do0": do0, "di2": di2}

# set watchdog for 2 mins
wdt = WDT(timeout=120000)

def mprint(str):
    '''
    Debug mode print function
    :param str: payload to print
    :return: none
    '''
    if PRINT_OVER_UART:
        print(str)

def friendly_time():
    '''
    Return user friendly time in str.
    :return: str
    '''
    now = time.localtime(time.time())
    return "{}/{}/{} {:02d}:{:02d}".format(now[0],now[1],now[2],now[3],now[4])

def create_event(data):
    '''
    Send JSON payload to cloud
    :param data: json payload w/o event_data wrapper
    :return:
    '''
    mqtt.ping()
    all_data = {"event_data": data}
    data = json.dumps(all_data)
    mprint(friendly_time() + ": sending {}".format(data))
    pub_topic = '0/' + registration["project_id"] + '/' + api_user + '/' + imei_number
    mqtt.pub(pub_topic, data)

def save_last_env(env):
    '''
    Saves the current env to file
    :param env: "dev", "tst" or "prd"
    :return: none
    '''
    mprint("Saving last env to file {}".format(env))
    last_env_file = open(LAST_ENV_FN, 'w')
    last_env_file.write(env)
    last_env_file.close()

def load_registration(env):
    '''
    Load registration file flash.
    :param env: "dev", "tst" or "prd"
    :return: none
    '''
    global state, registration

    if env == "dev":
        regis_fn = "registration_dev.json"
    elif env == "tst":
        regis_fn = "registration_tst.json"
    else:
        regis_fn = "registration_prd.json"

    if regis_fn not in os.listdir():
        mprint("Error, no registration file found in the filesystem: " + regis_fn)
        state = NO_REGISTRATION_FILE
    else:
        registration_file = open(regis_fn, 'r')
        registration_file_content = registration_file.read()
        registration_file.close()
        registration = json.loads(registration_file_content)

        if ("project_id" not in registration.keys()) or ("api_key" not in registration.keys()) or (
            "reg_mqtt_id" not in registration.keys()) or ("reg_password" not in registration.keys()) or (
            "host" not in registration.keys()) or ("port" not in registration.keys()):
            mprint("Error, Registration file missing necessary keys")
            state = NO_REGISTRATION_FILE
        else:
            mprint("Successfully loaded registration file: " + regis_fn)
            mprint(registration)

def register_device():
    '''
    Register device on Console
    :return:
    '''
    global api_user, api_password,imei_number, reg_mqtt, state, stopwatch

    mprint('Logging in as Registration User')
    api_user, api_password = '', ''

    reg_mqtt = MQTTClient(registration["project_id"] + imei_number + registration["reg_mqtt_id"],
                          registration["host"],
                          port=registration["port"],
                          user=registration["project_id"] + '/' + registration["reg_mqtt_id"],
                          password=registration["api_key"] + '/' + registration["reg_password"])
    reg_mqtt.disconnect()

    try:
        reg_mqtt.connect()
        reg_mqtt.cb = callback
        reg_sub_topic = '5/' + registration["project_id"] + '/' + imei_number
        reg_pub_topic = '4/' + registration["project_id"] + '/' + imei_number
        reg_mqtt.sub(reg_sub_topic)
        reg_mqtt.pub(reg_pub_topic, '{}')
        state = WAIT_REGISTRATION

    except MQTTNetworkError as mne:
        mprint('MQTT Connect Failed due to network issue (no DNS)')
        mprint(mne)
        stopwatch = time.time()
        state = WAIT_FOR_DNS

    except Exception as e:
        mprint('Failed registration.  Exit state machine.')
        state = FAILED_REGISTRATION


def validate_json(data):
    '''
    Validate if event_data is json format
    :return: True or False
    '''
    try:
        jdata = json.loads(data)
        if (len(jdata) > 0):
            return True
        else:
            return False
    except:
        return False

def puback(pid):
    pass

def callback(topic, msg):
    '''
    Call back from MQTT subscription.  Used for registration and normal operation (based on topic)
    :param topic:
    :param msg:
    :return:
    '''
    global api_user
    global api_password
    global state

    reg_sub_topic = '5/' + registration["project_id"] + '/' + imei_number
    if topic == bytearray(reg_sub_topic):
        json_obj = json.loads(str(msg, 'utf-8'))
        api_user = json_obj['mqtt_id']
        api_password = json_obj['password']

        # save credentials #
        credsfile = open(CREDS_FN, 'w')
        credsfile.write(json.dumps({"api_user": api_user, "api_password": api_password}))
        credsfile.close()
        mprint('Device Registered')
        reg_mqtt.disconnect()
        state = AUTHENTICATING

    else:
        json_obj = json.loads(str(msg, 'utf-8'))
        mprint("callback received:")
        mprint(json.dumps(json_obj))

        if "p" in json_obj:
            mprint("ping received")
            stdout.buffer.write(json.dumps(json_obj) + UART_OUT_DELIMITER)

        if "xp" in json_obj:
            mprint("xb ping received")
            res = {"ss": x.atcmd('DB')}
            if "mid" in json_obj:
                res["mid"] = json_obj["mid"]
            create_event(res)

        if "e" in json_obj:
            target = json_obj["e"]
            # "0" for dev
            # "1" for tst
            # "2" for prd

            mprint("Disconnecting from MQTT and switching to env {}".format(target))
            mqtt.disconnect()

            # remove current cred file to force re-registration
            os.remove("%s/%s" % (PATH_ROOT, CREDS_FN))
            if target == "0":
                save_last_env("dev")
                load_registration("dev")
            elif target == "1":
                save_last_env("tst")
                load_registration("tst")
            else:
                save_last_env("prd")
                load_registration("prd")

            if state != NO_REGISTRATION_FILE:
                state == REGISTER

        if "t" in json_obj:
            mprint("Received data transmission")

        if "c" in json_obj:
            mprint("Control received")
            for key in json_obj["c"]:
                if key in tag_to_pin:
                    val = json_obj["c"][key]
                    mprint("Setting " + key + " to " + str(val))
                    control_pin = tag_to_pin[key]
                    control_pin.value(val)

def process_data():
    '''
    Process and send incoming UART data
    :return:
    '''
    global event_data

    if state == CONNECTED:
        if event_data == "?":
            stdout.buffer.write('{"r":6}')      # connected status
        else:
            if validate_json(event_data):
                jdata = json.loads(event_data)
                create_event(jdata)
                stdout.buffer.write('{"r":0}')      # 0 means success
            else:
                stdout.buffer.write('{"r":1}')      # 1 means bad json

    elif state == INIT_CELLULAR or state == WAIT_CELLULAR:
        stdout.buffer.write('{"r":2}')          # no cellular connection

    elif state == WAIT_FOR_DNS:
        stdout.buffer.write('{"r":3}')          # establishing cellular connection

    elif state == WAIT_RETRY_REGISTRATION or state == AUTHENTICATING or state == WAIT_REGISTRATION:
        stdout.buffer.write('{"r":4}')          # busy authenticating to console

    elif state == FAILED_REGISTRATION:
        stdout.buffer.write('{"r":5}')          # Unable to register device on console

    event_data = ""

def check_last_env():
    '''
    Get last env from flash
    :return:
    '''
    if LAST_ENV_FN not in os.listdir():
        load_registration("prd")
    else:
        last_env_file = open(LAST_ENV_FN, 'r')
        last_env = last_env_file.read()
        last_env_file.close()
        load_registration(last_env)

### Begin GPS Helpers  ###
def extract_gps(serial_data):
    """
    Extracts the GPS data from the provided text.
    :param serial_data: Text to extract the GPS data from.
    :return: GPS data or and empty string if data could not be found.
    """

    if GNGGA_MARK in serial_data and GNGSA_MARK in serial_data:
        # Find repeating GPS sentence mark "$GNGGA", ignore it
        # and everything before it.
        _, after_gngga = serial_data.split(GNGGA_MARK, 1)
        # Now find mark "$GNGSA" in the result and ignore it
        # and everything after it.
        reading, _ = after_gngga.split(GNGSA_MARK, 1)

        return reading
    else:
        return ""


def extract_latitude(input_string):
    """
    Extracts the latitude from the provided text, value is all in degrees and
    negative if South of Equator.
    :param input_string: Text to extract the latitude from.
    :return: Latitude
    """

    if "N" in input_string:
        find_me = "N"
    elif "S" in input_string:
        find_me = "S"
    else:
        # 9999 is a non-sensical value for Lat or Lon, allowing the user to
        # know that the GPS unit was unable to take an accurate reading.
        return 9999

    index = input_string.index(find_me)
    deg_start = index - 11
    deg_end = index - 9
    deg = input_string[deg_start:deg_end]
    min_start = index - 9
    min_end = index - 1
    deg_decimal = input_string[min_start:min_end]
    latitude = (float(deg)) + ((float(deg_decimal)) / 60)

    if find_me == "S":
        latitude *= -1

    return latitude


def extract_longitude(input_string):
    """
    Extracts the longitude from the provided text, value is all in degrees and
    negative if West of London.
    :param input_string: Text to extract the longitude from.
    :return: Longitude
    """

    if "E" in input_string:
        find_me = "E"
    elif "W" in input_string:
        find_me = "W"
    else:
        # 9999 is a non-sensical value for Lat or Lon, allowing the user to
        # know that the GPS unit was unable to take an accurate reading.
        return 9999

    index = input_string.index(find_me)
    deg_start = index - 12
    deg_end = index - 9
    deg = input_string[deg_start:deg_end]
    min_start = index - 9
    min_end = index - 1
    deg_decimal = input_string[min_start:min_end]
    longitude = (float(deg)) + ((float(deg_decimal)) / 60)

    if find_me == "W":
        longitude *= -1

    return longitude


def read_gps_sample():
    """
    Attempts to read GPS and print the latest GPS values.
    """

    # Configure the UART to the GPS required parameters.
    u.init(9600, bits=8, parity=None, stop=1)

    # Ensures that there will only be a print if the UART
    # receives information from the GPS module.
    while not u.any():
        if u.any():
            break

    # Read data from the GPS.
    gps_data = str(u.read(), 'utf8')
    # Close the UART.
    u.deinit()

    # Get latitude and longitude from the read GPS data.
    lat = extract_latitude(extract_gps(gps_data))
    lon = extract_longitude(extract_gps(gps_data))

    return lat, lon

### End GPS Helper ###

### Initialize ###
mprint("Will resume bootup in {} seconds".format(INITIAL_BOOT_WAIT_SECONDS))
time.sleep(INITIAL_BOOT_WAIT_SECONDS) # allow time for keyboard interrupt during boot for interactive development
micropython.kbd_intr(-1) # disable keyboard interrupt

# determine which env to communicate with
check_last_env()

# Create a HDC1080 Temp/Humidity Sensor instance
sensor = HDC1080(I2C(1))

# Create a UART instance (this will talk to the GPS module).
u = UART(1, 9600)


while True:
    try:
        wdt.feed()       # refresh watchdog
        if state == INIT_CELLULAR:
            mprint(friendly_time() + ': Waiting for cellular network')
            x = xbee.XBee()
            x.atcmd('NR')
            c = network.Cellular()
            c.active(True)
            state = WAIT_CELLULAR

        elif state == WAIT_CELLULAR:
            if c.isconnected():
                mprint(friendly_time() + ': Connected to network')
                imei_number = c.config('imei')
                mprint(friendly_time() + ': IMEI ' + str(imei_number))
                state = AUTHENTICATING

        elif state == WAIT_FOR_DNS:
            if count_wait_for_dns > MAX_WAIT_FOR_DNS:
                mprint(friendly_time() + ": Exceeded max wait time for DNS, restart board")
                machine.reset()

            if (time.time() - stopwatch) > WAIT_DNS_RETRY_SECONDS:
                count_wait_for_dns += 1
                state = AUTHENTICATING

        elif state == WAIT_REBOOT:
            if (time.time() - stopwatch) > RESET_IN_SECONDS:
                machine.reset()

        elif state == WAIT_RETRY_REGISTRATION:
            if (time.time() - stopwatch) > WAIT_REGISTRATION_RETRY_SECONDS:
                register_device()

        elif state == AUTHENTICATING:
            if CREDS_FN not in os.listdir():
                mprint(friendly_time() + ": Credentials file not found: {}".format(CREDS_FN))
                register_device()
            else:
                credsfile = open(CREDS_FN, 'r')
                credsfile_content = credsfile.read()
                credsfile.close()
                mprint(friendly_time() + ": Loaded " + CREDS_FN)
                creds = json.loads(credsfile_content)

                if "api_user" not in creds.keys() or "api_password" not in creds.keys():
                    mprint(friendly_time() + "Corrupt credentials file: {}.  Re-registering ".format(CREDS_FN))
                    register_device()

                else:
                    api_user = creds["api_user"]
                    api_password = creds["api_password"]

                    mqtt = MQTTClient(registration["project_id"] + api_user,
                                      registration["host"],
                                      port=registration["port"],
                                      user=registration["project_id"] + '/' + api_user,
                                      password=registration["api_key"] + '/' + api_password)
                    mqtt.disconnect()

                    try:
                        mprint(friendly_time() + ': Mqtt Connect Attempt')
                        mqtt.connect()
                        state = CONNECTED
                        count_wait_for_dns = 0
                        mprint(friendly_time() + ': MQTT Connected')

                    except MQTTNetworkError as e:
                        mprint(friendly_time() + ': MQTT Connect Failed due to network issue (no DNS).  Will retry in {} secs.'.format(WAIT_DNS_RETRY_SECONDS))
                        mprint(e)
                        stopwatch = time.time()
                        state = WAIT_FOR_DNS
                        continue

                    except MQTTBadUsernameOrPassword as e:
                        mprint(friendly_time() + ': Mqtt Failed Authentication. Will re-register.')
                        mprint(e)
                        register_device()
                        continue

                    except Exception as e:
                        mprint(friendly_time() + ': Mqtt Connect Unknown Failure. Will reboot in {} secs.'.format(RESET_IN_SECONDS))
                        mprint(e)
                        stopwatch = time.time()
                        state = WAIT_REBOOT
                        continue

                    # subscribe and on-connnect event
                    try:
                        mqtt.cb = callback
                        mqtt.puback = puback
                        sub_topic = '1/' + registration[
                            "project_id"] + '/' + api_user + '/' + imei_number + '/event'
                        mqtt.sub(sub_topic)

                        create_event({"connected": True,
                                      "xbfw": FW_VERSION,
                                      "ss": x.atcmd('DB'),
                                      "imei": str(c.config('imei')),
                                      "dt": "0"})

                    except Exception as e:
                        mprint(friendly_time() + ": Unexpected Error. Will reboot in {} secs.".format(RESET_IN_SECONDS))
                        mprint(e)
                        stopwatch = time.time()
                        state = WAIT_REBOOT
                        continue

        elif state == WAIT_REGISTRATION:
            reg_mqtt.check_msg()

        elif state == CONNECTED:
            # check for subscription msg
            ret = mqtt.check_msg()
            if ret:
                mprint(friendly_time() + ': warning: check_msg returned {}'.format(ret))

            #### XB Heartbeat ###
            if time.time() > heartbeat_time:
                create_event({"h": 1, "ss": x.atcmd('DB')})
                heartbeat_time = time.time() + HEARTBEAT_INTERVAL_SECONDS

            #### Read Temp & Hum ####
            try:
                sensor_read = False
                temp_celsius = sensor.read_temperature(True)
                humidity_hr = sensor.read_humidity()
                sensor_read = True
            except Exception as e:
                mprint(friendly_time() + ": Unable to read Temp & Hum")
                mprint(e)

            #### Read GPS ####
            try:
                gps_lock = False
                if HAS_GPS:
                    latitude_dec, longitude_dec = read_gps_sample()
                    if latitude_dec != 9999 and longitude_dec != 9999:
                        gps_lock = True

            except Exception as e:
                continue

            #### Read GPIO Pins ###
            di0_now = di0.value()
            di1_now = di1.value()
            do0_now = do0.value()
            di2_now = di2.value()

            digital_change_detected = False
            if di0_now != di0_last or di1_now != di1_last or do0_now != do0_last or di2_now != di2_last:
                digital_change_detected = True

            di0_last = di0_now
            di1_last = di1_now
            do0_last = do0_now
            di2_last = di2_now

            #### Print payload ####
            payload = {"ss": x.atcmd('DB'), "di0": di0_now, "di1": di1_now, "do0": do0_now,
                       "di2": di2_now}

            if sensor_read:
                payload["temp"] = round(temp_celsius, 2)
                payload["hum"] = round(humidity_hr, 2)

            if gps_lock:
                payload["gps"] = "{} {}".format(latitude_dec, longitude_dec)

            if time.time() > datatransmit_time:
                mprint(friendly_time() + ": {}".format(payload))
                create_event(payload)
                datatransmit_time = time.time() + DATA_INTERVAL_SECONDS
            elif digital_change_detected:
                mprint(friendly_time() + ": {}".format(payload))
                create_event(payload)


    except Exception as e:
        mprint(friendly_time() + ": Exception ... resetting in {} secs".format(RESET_IN_SECONDS))
        mprint(e)
        if e == "Socket disconnected":
            state = INIT_CELLULAR
        else:
            state = WAIT_REBOOT
            stopwatch = time.time()©
