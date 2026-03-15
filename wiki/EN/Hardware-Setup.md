# Hardware Setup (Arduino)

NativMix is compatible with standard **[deej](https://github.com/omriharel/deej)** firmware.

## Protocol

The Arduino sends pipe-separated ADC values (0–1023) as a newline-terminated string:

```
512|0|1023|256\n
```

Each value corresponds to one potentiometer / channel.

## Recommended Firmware

For an enhanced experience and up-to-date Arduino code, we recommend **[deejHotkey](https://github.com/knoellix/deejHotkey)**. This repository provides optimized sketches and modern alternatives to the original deej firmware, specifically tailored for advanced setups.

## Auto-Detect & Hot-Plug

NativMix automatically detects `/dev/ttyACM0`, `/dev/ttyUSB0`, or any USB-serial device. If the Arduino is unplugged, NativMix will reconnect automatically as soon as it is plugged back in.

## Permissions

See [Permissions](Permissions.md) if NativMix cannot access the serial port.
