# FSM Service

Desktop service utility for MDCH-1 over Modbus.

The application is intentionally a local Python/Tkinter tool: no browser, no web
server, and no cloud dependency. It follows the same working pattern as the CAN
Monitor project:

- choose a session folder;
- load the MDCH register map;
- connect to a device or demo simulator;
- poll measurements and parameters in a background thread;
- show current values, parameters, events and a live speed chart;
- write a CSV session log with Excel-safe file rotation.

## Quick Start

```powershell
py -m pip install -r requirements.txt
py mdch_service_desktop.py
```

The default mode is `Demo`, so the interface can be checked without a device.
The default RTU speed in the UI is `115200`.

## Real Connections

Supported connection modes:

- `Demo`: local simulator of the MDCH v2 register map.
- `RTU`: Modbus RTU through a COM port. Requires `pyserial`.
- `TCP`: Modbus TCP through a host and port.

The v2 map uses zero-based PDU addresses. The application sends PDU addresses,
not 30001/40001 display addresses.

## Register Map

The default map is `templates/mdch_v2.json`, generated from:

- `МДЧ-1 Перечень регистров Modbus v2.xlsx`
- `МДЧ-1 Перечень конфигурируемых параметров v2.xlsx`

Important protocol assumptions in this software:

- `FC04` reads Input registers.
- `FC03` reads Holding registers.
- `FC06` writes single U16 draft parameters.
- `FC16` writes U32/F32 draft parameters and the command packet.
- Float values use Modbus word order CD AB.
- Active Holding mirrors are read-only and located at draft address +1000.
- Commands are written only as `FC16 start=0 count=4`: `CMD, ARG, KEY, SEQ`.

## Files

- `mdch_service_desktop.py` - GUI and polling loop.
- `modbus_client.py` - Modbus RTU/TCP clients and demo device.
- `mdch_register_map.py` - register map loading, decoding and encoding.
- `mdch_session_logger.py` - asynchronous CSV session logger.
- `templates/mdch_v2.json` - MDCH-1 v2 register map.

## Notes

The register map is a project specification, not proof that a connected device
already implements every field. Use demo mode for UI checks, then validate real
devices with read-only polling before writing parameters.
