// HomeBridge Plugin to expose as a thermostat accessory TERMA MOA Blue range of towel rail elements
// and towel rails that can be accessed via http using 'moa_web_server.py'.
//
// Copyright (C) James Pearce, 2025
//
//

'use strict';
const pkg = require('./package.json');
const pluginVersion = pkg.version;

// Uncomment the following line if you're using Node.js < 18:
// const fetch = require('node-fetch');

module.exports = (api) => {
  api.registerAccessory('TERMA-MOA-Blue', ThermostatAccessory);
};

class ThermostatAccessory {
  constructor(log, config, api) {
    this.log = log;
    this.config = config;
    this.api = api;

    // Log module initialising with version number
    this.log(`Homebridge TERMA MOA Blue plugin starting, version ${pluginVersion}`);

    this.Service = this.api.hap.Service;
    this.Characteristic = this.api.hap.Characteristic;

    // Extract config settings
    this.name = config.name || 'TERMA MOA Blue';
    this.baseUrl = config.baseUrl || 'http://127.0.0.1:8080';
    this.deviceAddress = config.deviceAddress; // Bluetooth MAC address
    if (!this.deviceAddress) {
      throw new Error('You must specify a deviceAddress in the configuration');
    }
    // Optional drying temperature (e.g. 30°C). If not provided or below 30, the heater will be turned off when not needed.
    this.drying = config.drying;
    // Default poll interval: 300000 ms (5 minutes)
    this.pollInterval = config.pollInterval || 300000;

    // Initialise working values
    this.currentRoomTemp = 20;               // Default starting value
    this.targetTemperature = 20;             // Default HomeKit target temperature
    this.DisplayUnits = this.Characteristic.TemperatureDisplayUnits.CELSIUS;
    this.roomTempSource = undefined;         // Holds value returned by heater ("HeatingElement" or "DS18B20")
    this.currentRadiatorTarget = undefined; // For feedback control in DS18B20 mode

    // Create an information service...
    this.informationService = new this.Service.AccessoryInformation()
      .setCharacteristic(this.Characteristic.Manufacturer, "TERMA")
      .setCharacteristic(this.Characteristic.Model, "MOA Blue")
      .setCharacteristic(this.Characteristic.SerialNumber, this.deviceAddress)
      .setCharacteristic(this.Characteristic.FirmwareRevision, pluginVersion);

    // Create the Thermostat service.
    this.service = new this.Service.Thermostat(this.name);

    // Handlers for required characteristics
    this.service.getCharacteristic(this.Characteristic.CurrentHeatingCoolingState)
      .onGet(this.handleCurrentHeatingCoolingStateGet.bind(this));

    this.service.getCharacteristic(this.Characteristic.TargetHeatingCoolingState)
      .onGet(this.handleTargetHeatingCoolingStateGet.bind(this))
      .onSet(this.handleTargetHeatingCoolingStateSet.bind(this));

    this.service.getCharacteristic(this.Characteristic.CurrentTemperature)
      .onGet(this.handleCurrentTemperatureGet.bind(this));

    this.service.getCharacteristic(this.Characteristic.TargetTemperature)
      .onGet(this.handleTargetTemperatureGet.bind(this))
      .onSet(this.handleTargetTemperatureSet.bind(this));

    this.service.getCharacteristic(this.Characteristic.TemperatureDisplayUnits)
      .onGet(this.handleTemperatureDisplayUnitsGet.bind(this))
      .onSet(this.handleTemperatureDisplayUnitsSet.bind(this));

    // Start periodic polling.
    this.startPolling();
  }

  // ----------------------------
  // HomeKit Characteristic Handlers
  // ----------------------------

  async handleCurrentTemperatureGet() {
    return this.currentRoomTemp;
  }

  async handleTargetTemperatureGet() {
    return this.targetTemperature;
  }

  // When HomeKit sets a new target temperature, update our internal state and schedule a heater update.
  async handleTargetTemperatureSet(value) {
    this.log(`HomeKit set target temperature to: ${value}°C`);
    this.targetTemperature = value;
    // Schedule heater update asynchronously.
    setImmediate(() => {
      this.updateHeaterSetting();
    });
    return;
  }

  async handleCurrentHeatingCoolingStateGet() {
    this.log.debug('Triggered GET CurrentHeatingCoolingState');
    return (this.targetTemperature > this.currentRoomTemp) ?
      this.Characteristic.CurrentHeatingCoolingState.HEAT :
      this.Characteristic.CurrentHeatingCoolingState.OFF;
  }

  async handleTargetHeatingCoolingStateGet() {
    this.log.debug('Triggered GET TargetHeatingCoolingState');
    return (this.targetTemperature > this.currentRoomTemp) ?
      this.Characteristic.TargetHeatingCoolingState.HEAT :
      this.Characteristic.TargetHeatingCoolingState.OFF;
  }

  // When HomeKit sets the target heating/cooling state, turn the heater off (or use drying) or resume heating.
  async handleTargetHeatingCoolingStateSet(value) {
    this.log.debug(`Triggered SET TargetHeatingCoolingState: ${value}`);
    if (value === this.Characteristic.TargetHeatingCoolingState.OFF) {
      // If turning off, either use drying temperature (if enabled) or turn heater off.
      if (this.drying !== undefined && this.drying >= 30) {
        this.sendHeaterUpdate(6, this.drying);
      } else {
        this.sendHeaterUpdate(0, 0);
      }
    } else if (value === this.Characteristic.TargetHeatingCoolingState.HEAT) {
      // If turning on, check the temperature source:
      if (this.roomTempSource === "HeatingElement") {
        // Use absolute HomeKit target (Mode 5)
        this.sendHeaterUpdate(5, this.targetTemperature);
      } else if (this.roomTempSource === "DS18B20") {
        // Use the feedback mechanism (Mode 6)
        this.updateHeaterSetting();
      } else {
        // If unknown, default to DS18B20 feedback logic.
        this.updateHeaterSetting();
      }
    }
    return;
  }

  async handleTemperatureDisplayUnitsGet() {
    this.log.debug('Triggered GET TemperatureDisplayUnits');
    return this.DisplayUnits;
  }

  async handleTemperatureDisplayUnitsSet(value) {
    this.log.debug(`Triggered SET TemperatureDisplayUnits: ${value}`);
    this.DisplayUnits = value;
    return;
  }

  // ----------------------------
  // Polling and Heater Control
  // ----------------------------

  // Start polling the heater’s status.
  startPolling() {
    this.log(`Starting polling every ${this.pollInterval / 60000} minute(s).`);
    // Initial update.
    this.updateStatus();
    // Schedule repeated polling.
    this.pollTimer = setInterval(() => {
      this.updateStatus();
    }, this.pollInterval);
  }

  // Utility: Reset the polling interval.
  resetPollingInterval(newInterval) {
    clearInterval(this.pollTimer);
    this.pollInterval = newInterval;
    this.log(`Resetting polling interval to ${this.pollInterval / 1000} seconds.`);
    this.pollTimer = setInterval(() => {
      this.updateStatus();
    }, this.pollInterval);
  }

  // Poll the heater’s /status endpoint and update HomeKit.
  updateStatus() {
    const url = `${this.baseUrl}/status?address=${this.deviceAddress}`;
    this.log(`Polling heater status: ${url}`);
    fetch(url)
      .then(res => res.json())
      .then(json => {
        this.log(`Polled status: ${JSON.stringify(json)}`);
        if (json.room_current_temp !== undefined) {
          this.currentRoomTemp = json.room_current_temp;
          this.service.getCharacteristic(this.Characteristic.CurrentTemperature)
              .updateValue(this.currentRoomTemp);
        }
        // Capture the temperature source.
        if (json.room_temp_source) {
          this.roomTempSource = json.room_temp_source;
        }
        // Control logic based on the temperature source:
        if (this.roomTempSource === "HeatingElement") {
          // Use Mode 5 with the absolute HomeKit target.
          this.sendHeaterUpdate(5, this.targetTemperature);
        } else if (this.roomTempSource === "DS18B20") {
          // Use our feedback control mechanism.
          this.updateHeaterSetting();
        }
        // On successful polling, reset poll interval to 5 minutes if it was shortened.
        if (this.pollInterval !== 300000) {
          this.log("Polling succeeded, resetting poll interval to 5 minutes.");
          this.resetPollingInterval(300000);
        }
      })
      .catch(err => {
        this.log(`Error polling heater status: ${err}`);
        // On polling failure, shorten poll interval to 90 seconds.
        if (this.pollInterval !== 90000) {
          this.log("Polling failed, shortening poll interval to 90 seconds.");
          this.resetPollingInterval(90000);
        }
      });
  }

  // Recalculate and set the radiator temperature.
  // For DS18B20 control, we use a feedback mechanism to gradually adjust the surface temperature.
  updateHeaterSetting() {
    this.log.debug('updateHeaterSetting called');
    const error = this.targetTemperature - this.currentRoomTemp;
    let mode, desiredTarget, newRadiatorTarget;

    if (error > 0) {
      // When the room is too cool, compute a desired radiator target based on a proportional scale.
      desiredTarget = 30 + (Math.min(error, 10) / 10) * (59 - 30);
      mode = 6;
    } else {
      // When no heating is required, use drying (if valid) or turn off.
      if (this.drying !== undefined && this.drying >= 30) {
        desiredTarget = this.drying;
        mode = 6;
      } else {
        desiredTarget = 0;
        mode = 0;
      }
    }

    // Apply feedback only when using DS18B20.
    if (this.roomTempSource === "DS18B20") {
      const alpha = 0.2; // Tuning parameter for gradual adjustment.
      if (this.currentRadiatorTarget === undefined) {
        this.currentRadiatorTarget = desiredTarget;
      } else {
        this.currentRadiatorTarget = this.currentRadiatorTarget + alpha * (desiredTarget - this.currentRadiatorTarget);
      }
      newRadiatorTarget = this.currentRadiatorTarget;
    } else {
      // For HeatingElement, simply use the desired target.
      newRadiatorTarget = desiredTarget;
    }

    // Ensure that if the new radiator target is zero, we force mode 0.
    if (newRadiatorTarget <= 0) {
      mode = 0;
    }

    this.log(`Calculated update: error = ${error.toFixed(1)}°C, desired target = ${desiredTarget.toFixed(1)}°C, new radiator target = ${newRadiatorTarget.toFixed(1)}°C, mode = ${mode}`);
    this.sendHeaterUpdate(mode, newRadiatorTarget);
  }

  // Helper to send a heater update command.
  sendHeaterUpdate(mode, temperature) {
    const url = `${this.baseUrl}/set?address=${this.deviceAddress}&mode=${mode}&temp=${temperature.toFixed(1)}`;
    this.log(`Sending heater update: ${url}`);
    fetch(url)
      .then(res => res.json())
      .then(json => {
        this.log(`Heater API response: ${JSON.stringify(json)}`);
        if (json.room_current_temp !== undefined) {
          this.currentRoomTemp = json.room_current_temp;
          this.service.getCharacteristic(this.Characteristic.CurrentTemperature)
              .updateValue(this.currentRoomTemp);
        }
      })
      .catch(err => {
        this.log(`Error updating heater setting: ${err}`);
      });
  }

  // ----------------------------
  // Homebridge Services
  // ----------------------------

  // Homebridge calls this to get all services provided by the accessory.
  getServices() {
    this.log.debug('getServices called');
    return [
      this.informationService,
      this.service,
    ];
  }
}

