[![Donate](https://badgen.net/badge/donate/paypal)](https://paypal.me/HomebridgeJ1mbo)

# homebridge-TERMA-MOA-Blue

A HomeBridge interface for the TERMA MOA Blue range of bluetooth enabled towel rail heating elements.


WARNING:

Water heaters can be dangerous. Please read and understand the warnings in the included web server,
moa-web-server.py, before using this software.

DO NOT REMOVE ANY PROTECTION OR VALIDATION LOGIC.

ALL TOWEL RAILS REQUIRE EXPANSION SPACE TO ACCOMODATE THE EXPANSION OF THEIR CONTENTS WHEN HEATED
SUFFICIENT TO ACCOMODATE THE CONTENTS AT THE MAXIMUM POSSIBLE TEMPERATURE.

SELF-PROTECTION MECHANISMS OF THE TERMA ELEMENTS ARE NOT MADE PUBLIC AND IT IS ABSOLUTELY POSSIBLE
TO CONFIGURE THESE DEVICES TO EXCEED THEIR DESIGN MAXIMUM WATER TEMPERATURE OF 60°C.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT.

THE INTENDED USE OF THE SOFTWARE IS TO PROVIDE THE END-USER WITH EXAMPLE CODE SHOWING METHODS TO
CONNECT THE TERMA "MOA BLUE" RANGE OF BLUETOOTH ENABLED ELECTRIC TOWEL RAIL HEATING ELEMENTS TO
HOMEKIT. THE SOFTWARE HAS NOT BEEN TESTED IN ALL POSSIBLE SCENARIOS AND IS NOT A FINISHED PRODUCT
IN ITSELF. THE END USER IS RESPONSIBLE FOR TESTING THE COMPLETE SYSTEM AND ALL LIABILITY ARISING
FROM ITS USE.

BY USING THIS SOFTWARE, YOU ARE ACCEPTING THESE TERMS OF USE.


Key features:

- Control of one or more heating elements via Homekit, exposing them as Thermostat accessories.


# Plugin Configuration

Installed through HomeBridge plugins UI, the settings are fully configurable in the UI.


# Solution Architecture

The plugin accesses the configured heating element(s) via the included Python web server - moa_web_server.py - which connects to elements by bluetooth and exposes a simple HTTP API to provide control over them.

The web server needs to be deployed somewhere physically close enough to the elements to provide reliable a Bluetooth connection to the target heating element. This web server needs to be installed manually on that device (see INSTALLATION for instructions), and can be deployed on the Homekit server itself if required.

This approach eliminates range issues since using a Pi Zero 2W, for example, the web server can be deployed cheaply wherever needed without needing to maintain multiple instances of Homebridge.

# Issues and Contact

Please raise an issue should you come across one via Github.

