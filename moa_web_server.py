#!/usr/bin/env python3
import sys
import re
import glob
import os
import asyncio
import time
import logging
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, validator
from bleak import BleakClient, BleakScanner
import uvicorn
import pexpect

app = FastAPI()

BANNER = r"""
########################################################################################################
#
# TERMA MOA BLUE WEB INTERFACE, Copyright (C) James Pearce, 2025
# This program creates a web API for one or more Terma MOA Bluetooth Electric Heating Elements.
#
# WARNING:
#
# Setting the heating element progmatically can result in eroneous values being set, particularly
# when switching between modes 5 and 6, which results in target temperatures being doubled. To work
# around this, this utility always first sets (and confirms) mode 0 (off), then sets (and confirms)
# the target mode. If the correct values are not reported back after several retries, the device is
# then set to mode 0 (off). Input values are also clamped within manufacturer provided ranges.
#
# DO NOT REMOVE ANY OF THIS PROTECTION AND VALIDATION LOGIC. ALL TOWEL RAILS REQUIRE EXPANSION SPACE
# TO ACCOMODATE THE EXPANSION OF THEIR CONTENTS WHEN HEATED SUFFICIENT TO ACCOMMODATE THE CONTENTS AT
# THE MAXIMUM POSSIBLE TEMPERATURE.
#
# SELF-PROTECTION MECHANISMS OF THE TERMA ELEMENTS ARE NOT MADE PUBLIC AND IT IS ABSOLUTELY POSSIBLE
# TO CONFIGURE THESE DEVICES TO EXCEED THEIR DESIGN MAXIMUM WATER TEMPERATURE OF 60°C.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
# NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT.
#
# THE INTENDED USE OF THE SOFTWARE IS TO PROVIDE THE END-USER WITH EXAMPLE CODE SHOWING METHODS TO
# INTERFACE WITH THE TERMA "MOA BLUE" RANGE OF BLUETOOTH ENABLED ELECTRIC TOWEL RAIL HEATING ELEMENTS.
# THE SOFTWARE HAS NOT BEEN TESTED IN ALL POSSIBLE SCENARIOS AND IS NOT A FINISHED PRODUCT IN ITSELF.
# THE END USER IS RESPONSIBLE FOR TESTING THE COMPLETE SYSTEM AND ALL LIABILITY ARISING FROM ITS USE.
# BY USING THIS SOFTWARE, YOU ARE ACCEPTING THESE TERMS OF USE.
#
########################################################################################################
"""

HELP = r"""
TERMA MOA BLUE WEB INTERFACE, Copyright (C) James Pearce, 2025
This program creates a web API for one or more Terma MOA Bluetooth Electric Heating Elements.

Before elements can be used, they must be connected and trusted as the OS layer and return as their
device name "MOA Blue TERMA".

Functions provided:

/pair?address=XX:XX:XX:XX:XX:XX&pin=XXXXXX
  Using bluetoothctl:
    1. Removes the device with specified MAC address
    2. Enables scanning until specified MAC address is detected
    3. Disables scanning
    3. Connects, trusts, and pairs with the specified MAC address using specified PIN

  This is slow and error prone and this will retry the whole process three times.
  The device needs to be in pairing mode. For MOA Blue TERMA device, the easiest way
  to do this is to open the TERMA app on your phone, assuming this is already connected
  to it. The PIN is usually 123456.

  Returns "success" or "failure".

/status?address=XX:XX:XX:XX:XX:XX
  Returns device status, for example:
  {
    "device": "CC:22:37:10:43:4B",
    "name": "MOA Blue TERMA",
    "mode": 5,
    "room_current_temp": 20.9,
    "room_target_temp": 20,
    "heater_current_temp": 42.8,
    "heater_target_temp": 20,
    "room_temp_source": "HeatingElement"
  }

/set?address=XX:XX:XX:XX:XX:XX&mode=Y&temp=ZZ.Z
  Attempts to set device operating mode and returns the device status as per /status
  mode can be:
    5 - Room temperature regulation, as measured by the sensor in the element, 15.0-29.9°C
    6 - Radiator water(/surface) temperature regulation, 29.9°C to 59.8°C
    any other value - converted to 0, which is off

/query-device?address=XX:XX:XX:XX:XX:XX
  Provided for troubleshooting, returns all raw data retrieved from device.

/discover?timeout=XX.X
  Lists all devices and their associated addresses visible. If timeout is omitted uses 15s.

... (rest of help text omitted for brevity) ...
"""

# Serve banner and help information when the root URL is requested.
@app.get("/", response_class=PlainTextResponse)
async def root():
    return BANNER + "\n\n" + HELP


# Terma specific UUIDs and temperature encode/decode
ROOM_TEMP_UUID      = "d97352b1-d19e-11e2-9e96-0800200c9a66"  # For room temperature mode (mode 5)
HEATER_TEMP_UUID    = "d97352b2-d19e-11e2-9e96-0800200c9a66"  # For heater temperature mode (mode 6)
OPERATING_MODE_UUID = "d97352b3-d19e-11e2-9e96-0800200c9a66"  # Operating mode (0, 5, or 6)

def decode_temperature(data: bytes) -> (float, float):
    if len(data) < 4:
        raise ValueError("Temperature data too short")
    current = ((data[0] * 255) + data[1]) / 10.0
    target  = ((data[2] * 255) + data[3]) / 10.0
    return current, target

def encode_temperature(target: float) -> bytes:
    """
    Encode a target temperature (°C) into a 4-byte payload.
    The protocol expects the first two bytes to be zero and the last two bytes
    represent target*10.
    """
    value = int(round(target * 10))
    high = value // 255
    low = value % 255
    return bytes([0, 0, high, low])

# Utility to validate a BLE address (e.g., "CC:22:37:10:43:4B")
def validate_address(addr: str) -> str:
    parts = addr.split(":")
    if len(parts) != 6 or not all(len(p) == 2 for p in parts):
        raise HTTPException(status_code=400, detail="Invalid address format.")
    return addr.upper()

#########################################################################
# PERSISTENT BLUETOOTH CONNECTIONS
#
# We now maintain a persistent connection per device using a custom class.
#
PERSISTENT_CLIENTS = {}

class PersistentBleClient:
    def __init__(self, address: str):
        self.address = validate_address(address)
        self.client = None
        self.lock = asyncio.Lock()

    async def connect(self):
        async with self.lock:
            if self.client is not None and self.client.is_connected:
                return self.client
            # Create a new BleakClient instance for this address.
            self.client = BleakClient(self.address, timeout=10.0)
            await self._attempt_connect()
            return self.client

    async def _attempt_connect(self):
        attempts = 0
        while attempts < 3:
            try:
                print(f"[PersistentBleClient] Attempt {attempts+1} connecting to {self.address}...")
                await self.client.connect()
                if self.client.is_connected:
                    print(f"[PersistentBleClient] Connected to {self.address} on attempt {attempts+1}.")
                    return
            except Exception as e:
                print(f"[PersistentBleClient] Connection attempt {attempts+1} failed: {e}")
            attempts += 1
            await asyncio.sleep(3)
        # After three normal attempts, run bluetoothctl commands.
        print(f"[PersistentBleClient] Normal connection attempts failed for {self.address}. Running bluetoothctl power on commands.")
        await self._bluetoothctl_power_on()
        attempts = 0
        while attempts < 3:
            try:
                print(f"[PersistentBleClient] Post-bluetoothctl attempt {attempts+1} connecting to {self.address}...")
                await self.client.connect()
                if self.client.is_connected:
                    print(f"[PersistentBleClient] Connected to {self.address} after bluetoothctl on attempt {attempts+1}.")
                    return
            except Exception as e:
                print(f"[PersistentBleClient] Post-bluetoothctl connection attempt {attempts+1} failed: {e}")
            attempts += 1
            await asyncio.sleep(3)
        raise Exception(f"Unable to connect to {self.address} after persistent reconnection attempts.")

    async def _bluetoothctl_power_on(self):
        print("[PersistentBleClient] Running bluetoothctl power on commands.")
        child = pexpect.spawn("bluetoothctl", encoding="utf-8", timeout=5)
        child.sendline("power on")
        time.sleep(2)
        child.sendline("power on")
        time.sleep(2)
        child.sendline("exit")
        child.close()
        print("[PersistentBleClient] bluetoothctl power on commands executed.")

    async def disconnect(self):
        async with self.lock:
            if self.client and self.client.is_connected:
                await self.client.disconnect()
                print(f"[PersistentBleClient] Disconnected from {self.address}")

def get_persistent_client(address: str) -> PersistentBleClient:
    address = validate_address(address)
    if address not in PERSISTENT_CLIENTS:
        PERSISTENT_CLIENTS[address] = PersistentBleClient(address)
    return PERSISTENT_CLIENTS[address]

#########################################################################
# ORIGINAL HELPER FUNCTIONS, REVISED TO USE THE PERSISTENT CONNECTION
#

async def query_device(address: str):
    """
    Scans for the device with the given MAC address (if needed)
    and returns a dictionary with the device name and discovered services.
    This version uses the persistent connection.
    """
    address = validate_address(address)
    print(f"[query_device] Querying device with address: {address}")
    persistent_client = get_persistent_client(address)
    client = await persistent_client.connect()
    # Optionally retrieve the device name via scanning.
    device_obj = await BleakScanner.find_device_by_address(address, timeout=3.0)
    device_name = device_obj.name if device_obj and device_obj.name else "Unknown"
    try:
        # Force service discovery.
        services = client.services
        service_info = {}
        for service in services:
            char_info = {}
            for char in service.characteristics:
                data = {"properties": list(char.properties)}
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char.uuid)
                        data["value"] = value.hex()
                    except Exception as read_exc:
                        data["value_error"] = str(read_exc)
                else:
                    data["value"] = None
                char_info[char.uuid] = data
            service_info[service.uuid] = char_info
        return {
            "device": address,
            "name": device_name,
            "services": service_info
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error connecting to device: {str(e)}")

def get_char_value(services: dict, char_uuid: str):
    """
    Searches the services dictionary for the given characteristic UUID.
    Returns the value as bytes (if available) or None.
    """
    for svc, chars in services.items():
        if char_uuid in chars:
            val_hex = chars[char_uuid].get("value")
            if val_hex is not None:
                try:
                    return bytes.fromhex(val_hex)
                except Exception:
                    return None
    return None

def read_ds18b20_temp():
    """
    Reads the temperature from the first available DS18B20 sensor.
    Returns the temperature in degrees Celsius.
    """
    base_dir = '/sys/bus/w1/devices'
    sensor_folders = glob.glob(os.path.join(base_dir, '28-*'))
    if not sensor_folders:
        raise Exception("INFO: No DS18B20 sensor found")
    sensor_folder = sensor_folders[0]
    sensor_file = os.path.join(sensor_folder, 'w1_slave')
    
    try:
        with open(sensor_file, 'r') as f:
            lines = f.readlines()
    except Exception as e:
        raise Exception(f"Error reading DS18B20 sensor file: {str(e)}")
    
    if lines[0].strip()[-3:] != "YES":
        raise Exception("DS18B20 sensor not ready")
    
    equals_pos = lines[1].find('t=')
    if equals_pos == -1:
        raise Exception("Temperature reading not found in sensor output")
    
    temp_string = lines[1][equals_pos+2:]
    try:
        temp_c = float(temp_string) / 1000.0
    except ValueError:
        raise Exception("Invalid temperature format from DS18B20")
    
    return temp_c

async def read_status(address: str):
    """
    Uses query_device to retrieve the device dump,
    then extracts measurement values from the known UUIDs.
    If a DS18B20 sensor is available, its reading is used.
    """
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            query = await query_device(address)
            break  # Success – exit loop.
        except Exception as e:
            if attempt < max_attempts - 1:
                print(f"[read_status] Attempt {attempt + 1} failed: {e}. Retrying...")
                await asyncio.sleep(3)
            else:
                raise Exception(f"All {max_attempts} attempts failed: {e}")

    services = query.get("services", {})
    room_data = get_char_value(services, ROOM_TEMP_UUID)
    heater_data = get_char_value(services, HEATER_TEMP_UUID)
    mode_data   = get_char_value(services, OPERATING_MODE_UUID)
    
    if room_data is None or heater_data is None or mode_data is None:
        raise Exception("Missing measurement data in device services")
    
    try:
        room_current = read_ds18b20_temp()
        room_temp_source = "DS18B20"
    except Exception:
        try:
            room_current, _ = decode_temperature(room_data)
        except Exception as e:
            raise Exception(f"Error decoding current room temperature: {str(e)}")
        room_temp_source = "HeatingElement"
    
    try:
        _, room_target = decode_temperature(room_data)
        heater_current, heater_target = decode_temperature(heater_data)
    except Exception as exc:
        raise Exception(f"Error decoding room target temperature: {str(exc)}")
    
    mode = mode_data[0] if len(mode_data) >= 1 else None
    return {
        "device": address,
        "name": query.get("name"),
        "mode": mode,
        "room_current_temp": room_current,
        "room_target_temp": room_target,
        "heater_current_temp": heater_current,
        "heater_target_temp": heater_target,
        "room_temp_source": room_temp_source
    }

class SetRequest(BaseModel):
    mode: int         # Allowed values: 0 (off), 5 (room mode), or 6 (heater mode)
    target_temp: float  # Desired target temperature in ºC

    @validator("mode")
    def validate_mode(cls, v):
        if v not in [0, 5, 6]:
            raise ValueError("Invalid mode; must be 0, 5, or 6")
        return v

    @validator("target_temp", pre=True)
    def clamp_target_temp(cls, v, values):
        mode = values.get("mode")
        try:
            val = float(v)
        except Exception:
            raise ValueError("target_temp must be a number")
        if mode == 5:
            if val < 15.0:
                print(f"Clamping room temperature {val}°C to 15.0°C")
                return 15.0
            elif val > 29.9:
                print(f"Clamping room temperature {val}°C to 29.9°C")
                return 29.9
        elif mode == 6:
            if val < 29.9:
                print(f"Clamping heater temperature {val}°C to 29.9°C")
                return 29.9
            elif val > 59.8:
                print(f"Clamping heater temperature {val}°C to 59.8°C")
                return 59.8
        return val

async def retry_read_status(address: str, retries: int = 5, delay: float = 5.0, timeout: float = 5.0):
    last_error = None
    for i in range(retries):
        try:
            print(f"[retry_read_status] Attempting to read status for {address} (try {i+1} of {retries})...")
            result = await asyncio.wait_for(read_status(address), timeout=timeout)
            print("[retry_read_status] Status read successfully.")
            return result
        except Exception as e:
            last_error = e
            print(f"[retry_read_status] Retry {i+1}/{retries} failed: {repr(e)}")
            await asyncio.sleep(delay)
    raise last_error

#########################################################################
# ENDPOINTS
#

@app.get("/status")
async def get_status(address: str = Query(..., description="BLE address, e.g., CC:22:37:10:43:4B")):
    print(f"[API] Received /status command for device: {address}")
    try:
        return await read_status(address)
    except Exception as e:
        print(f"[API] /status command failed: {repr(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/query-device")
async def query_device_endpoint(address: str = Query(..., description="BLE address, e.g., CC:22:37:10:43:4B")):
    print(f"[API] Received /query-device command for address: {address}")
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            query = await query_device(address)
            break
        except Exception as e:
            if attempt < max_attempts - 1:
                print(f"[API] Attempt {attempt + 1} failed: {e}. Retrying...")
                await asyncio.sleep(3)
            else:
                raise Exception(f"All {max_attempts} attempts failed: {e}")
    return query

async def set_thermostat(address: str, req: SetRequest):
    expected_mode = req.mode
    expected_target = req.target_temp
    max_attempts = 3
    attempt = 0
    while attempt < max_attempts:
        print(f"[set_thermostat] Attempt {attempt+1}: mode={req.mode}, target_temp={req.target_temp}")
        address = validate_address(address)
        persistent_client = get_persistent_client(address)
        client = await persistent_client.connect()
        try:
            await client.get_services()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error retrieving services: {str(e)}")
        
        if req.mode == 5:
            temp_uuid = ROOM_TEMP_UUID
        elif req.mode == 6:
            temp_uuid = HEATER_TEMP_UUID
        else:
            temp_uuid = None

        if temp_uuid is None:
            print("[set_thermostat] Clearing current config (setting device to mode 0=off)")
            try:
                await client.write_gatt_char(OPERATING_MODE_UUID, bytes([0]), response=False)
                print("[set_thermostat] Device set to mode 0 (off).")
            except Exception as e:
                print(f"[set_thermostat] Could not set device to off: {e}")
        else:
            payload = encode_temperature(req.target_temp)
            print(f"[set_thermostat] Writing temperature payload {payload.hex()} to {temp_uuid}")
            try:
                await client.write_gatt_char(temp_uuid, payload, response=False)
            except Exception as e:
                raise Exception(f"Failed to write target temperature: {e}")
            mode_payload = bytes([req.mode])
            print(f"[set_thermostat] Writing operating mode {mode_payload.hex()} to {OPERATING_MODE_UUID}")
            try:
                await client.write_gatt_char(OPERATING_MODE_UUID, mode_payload, response=False)
            except Exception as e:
                print(f"[set_thermostat] Failed to write operating mode: {e}")

        print("[set_thermostat] Write commands completed, waiting for device to re-advertise...")
        await asyncio.sleep(3)
        print("[set_thermostat] Reading status after set command...")
        try:
            status = await read_status(address)
        except Exception as e:
            print(f"[set_thermostat] Failed to read status: {e}")
            status = None

        if status is not None:
            if req.mode == 5:
                actual = status.get("room_target_temp", 0)
                if abs(actual - expected_target) < 0.5:
                    print(f"[set_thermostat] Status verified for mode 5: {actual}°C (expected {expected_target}°C)")
                    return status
                else:
                    print(f"[set_thermostat] room_target_temp {actual}°C != expected {expected_target}°C")
            elif req.mode == 6:
                actual = status.get("heater_target_temp", 0)
                if abs(actual - expected_target) < 0.5:
                    print(f"[set_thermostat] Status verified for mode 6: {actual}°C (expected {expected_target}°C)")
                    return status
                else:
                    print(f"[set_thermostat] heater_target_temp {actual}°C != expected {expected_target}°C")
            else:
                if status.get("mode") == 0:
                    print("[set_thermostat] Status verified as mode 0 (off)")
                    return status
                else:
                    print(f"[set_thermostat] Reported mode ({status.get('mode')}) != expected (0)")
        print(f"[set_thermostat] Retrying set command (attempt {attempt+1}/{max_attempts})...")
        attempt += 1

    print("[set_thermostat] Failed to set expected values after 3 attempts. Setting device to mode 0 (off).")
    try:
        await client.write_gatt_char(OPERATING_MODE_UUID, bytes([0]), response=False)
        print("[set_thermostat] Device set to mode 0 (off).")
    except Exception as e:
        print(f"[set_thermostat] Failed to set device to off: {e}")
    return {"mode": 0, "error": "Failed to set expected values after 3 attempts; device turned off."}

@app.get("/set")
async def set_thermostat_get(address: str = Query(..., description="BLE address, e.g., CC:22:37:10:43:4B"),
                             mode: int = Query(0, description="Mode: 0, 5, or 6"),
                             temp: float = Query(20.0, description="Target temperature in ºC")):
    print(f"[API] Received GET /set command for device: {address} with mode={mode}, temp={temp}")
    # First clear device config (set to off)
    try:
        req = SetRequest(mode=0, target_temp=20.0)
        result = await set_thermostat(address, req)
        print("[API] Initial clear command completed.")
    except Exception as e:
        print(f"[API] /set command failed during clear: {repr(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    # If mode 5 or 6 is requested, set accordingly.
    if mode in [5, 6]:
        try:
            req = SetRequest(mode=mode, target_temp=temp)
            result = await set_thermostat(address, req)
            print("[API] /set command completed.")
            return result
        except Exception as e:
            print(f"[API] /set command failed: {repr(e)}")
            raise HTTPException(status_code=500, detail=str(e))
    else:
        return result

@app.get("/discover")
async def find_all_devices(timeout: float = 15.0):
    """
    Returns a list of all visible Bluetooth devices.
    """
    print(f"[API] Received /discover command with timeout={timeout}")
    devices = await BleakScanner.discover(timeout=timeout)
    return [{"address": device.address, "name": device.name} for device in devices]

#########################################################################
# BLUETOOTH PAIRING FUNCTION (using bluetoothctl via pexpect)
#
logging.basicConfig(level=logging.WARNING, format='%(asctime)s [%(levelname)s] %(message)s')

def send_command(child, command, expected_pattern, timeout=2, retries=3):
    for attempt in range(1, retries + 1):
        logging.info(f"Sending command: {command} (attempt {attempt})")
        child.sendline(command)
        try:
            child.expect(expected_pattern, timeout=timeout)
            logging.info(f"Expected output received for command: {command}")
            return True
        except pexpect.TIMEOUT:
            logging.warning(f"Timeout waiting for '{expected_pattern}' after command: {command}")
            if attempt < retries:
                logging.info("Retrying command...")
            else:
                logging.error(f"Failed after {retries} attempts: {command}")
    return False

@app.get("/pair")
async def pair_device(address: str = Query(..., description="BLE address, e.g., CC:22:37:10:43:4B"),
                      pin: str = Query("123456", description="Device connect PIN")):
    print(f"[API] Received /pair command for device: {address} with pin={pin}")
    address = validate_address(address)
    max_overall_retries = 3
    for overall_attempt in range(1, max_overall_retries + 1):
        logging.info(f"Overall pairing attempt {overall_attempt} for device {address}")
        child = pexpect.spawn("bluetoothctl", encoding="utf-8", timeout=5)
        child.logfile = sys.stdout

        try:
            if not send_command(child, "", re.compile(r'\[bluetooth\]'), timeout=5):
                raise Exception("Initial prompt not received")
            
            child.sendline(f"remove {address}")
            if not send_command(child, "", re.compile(r'\[bluetooth\]'), timeout=5):
                raise Exception("After remove command")
            
            if not send_command(child, "agent KeyboardOnly", re.compile(r'\[bluetooth\]'), timeout=5):
                raise Exception("Agent command failed")
            if not send_command(child, "default-agent", re.compile(r"Default agent request successful"), timeout=2):
                raise Exception("Default agent command failed")
            
            if not send_command(child, "power on", re.compile(r'\[bluetooth\]'), timeout=5):
                raise Exception("Power on command failed")
            
            logging.info("Starting scan...")
            child.sendline("scan on")
            try:
                child.expect(f"{address}", timeout=30)
                logging.info(f"Device {address} detected during scan.")
            except pexpect.TIMEOUT:
                raise Exception(f"Timeout waiting for {address} to appear in list.")
            
            time.sleep(2)
            logging.info(f"Connecting to device {address}...")
            max_connect_retries = 3
            connection_successful = False
            for attempt in range(1, max_connect_retries + 1):
                child.sendline(f"connect {address}")
                try:
                    child.expect("Connection successful", timeout=5)
                    logging.info("Connection successful received.")
                    child.expect(f"{address} ServicesResolved: yes", timeout=15)
                    logging.info("Services confirmed.")
                    connection_successful = True
                    break
                except pexpect.TIMEOUT:
                    logging.warning("No response after connect command. Retrying connect...")
            if not connection_successful:
                raise Exception("Failed to connect after multiple attempts.")
            
            time.sleep(1)
            if not send_command(child, f"trust {address}", re.compile(r"trust succeeded"), timeout=2):
                raise Exception("Trust command failed")
            
            time.sleep(1)
            logging.info(f"Pairing with device {address}...")
            child.sendline(f"pair {address}")
            try:
                index = child.expect([
                    re.compile(r"Enter passkey"),
                    re.compile(r"Request passkey"),
                    re.compile(r"Pairing successful"),
                    pexpect.TIMEOUT
                ], timeout=5)
                if index in [0, 1]:
                    logging.info(f"Passkey prompt detected. Sending PIN: {pin}")
                    child.sendline(pin)
                    child.expect("Pairing successful", timeout=5)
                    logging.info("Pairing successful!")
                elif index == 2:
                    logging.info("Pairing successful (no passkey prompt).")
                else:
                    raise Exception("Pairing failed: unexpected response or timeout.")
            except pexpect.TIMEOUT:
                raise Exception("Pairing failed: operation timed out.")
            
            logging.info("Device paired successfully!")
            if not send_command(child, "power off", re.compile(r'\[bluetooth\]'), timeout=5):
                raise Exception("Power off command failed")
            if not send_command(child, "power on", re.compile(r'\[bluetooth\]'), timeout=5):
                raise Exception("Power on command failed")
            time.sleep(1)
            logging.info("Exiting bluetoothctl...")
            child.sendline("exit")
            child.close()
            return {"pairing": "success"}
        except Exception as e:
            logging.error(f"Error encountered: {e}. Retrying entire pairing process after 3 seconds.")
            child.close()
            time.sleep(3)
    logging.error("All overall pairing attempts failed.")
    raise HTTPException(status_code=500, detail={"pairing": "failed"})

#########################################################################
# PROGRAM START POINT:
if __name__ == "__main__":
    print(BANNER)
    if any(arg in ("--help", "-h", "?") for arg in sys.argv[1:]):
        print(HELP)
        sys.exit(0)
    uvicorn.run("moa_web_server:app", host="0.0.0.0", port=8080)
