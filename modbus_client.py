from __future__ import annotations

from dataclasses import dataclass
import math
import socket
import threading
import time
from typing import Any

from mdch_register_map import MdchRegisterMap


FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_SINGLE = 0x06
FC_WRITE_MULTIPLE = 0x10


class ModbusError(RuntimeError):
    pass


class ModbusException(ModbusError):
    def __init__(self, function_code: int, exception_code: int):
        self.function_code = function_code
        self.exception_code = exception_code
        super().__init__(f"Modbus exception {exception_code:02X} for function {function_code:02X}")


@dataclass
class ModbusConnection:
    mode: str = "demo"
    slave_id: int = 1
    timeout: float = 0.5
    port: str = "COM1"
    baudrate: int = 19200
    parity: str = "E"
    stopbits: int = 1
    host: str = "127.0.0.1"
    tcp_port: int = 502


class ModbusClient:
    is_demo = False

    def connect(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def read_input_registers(self, address: int, count: int) -> list[int]:
        raise NotImplementedError

    def read_holding_registers(self, address: int, count: int) -> list[int]:
        raise NotImplementedError

    def write_single_register(self, address: int, value: int) -> None:
        raise NotImplementedError

    def write_multiple_registers(self, address: int, values: list[int]) -> None:
        raise NotImplementedError


class StandardModbusClient(ModbusClient):
    def __init__(self, transport: "ModbusTransport", slave_id: int):
        self.transport = transport
        self.slave_id = int(slave_id)

    def connect(self) -> None:
        self.transport.connect()

    def close(self) -> None:
        self.transport.close()

    def read_input_registers(self, address: int, count: int) -> list[int]:
        return self._read_registers(FC_READ_INPUT, address, count)

    def read_holding_registers(self, address: int, count: int) -> list[int]:
        return self._read_registers(FC_READ_HOLDING, address, count)

    def write_single_register(self, address: int, value: int) -> None:
        pdu = bytes([FC_WRITE_SINGLE]) + int(address).to_bytes(2, "big") + int(value & 0xFFFF).to_bytes(2, "big")
        response = self._request(pdu)
        if response != pdu:
            raise ModbusError("Unexpected FC06 response")

    def write_multiple_registers(self, address: int, values: list[int]) -> None:
        values = [int(value) & 0xFFFF for value in values]
        payload = b"".join(value.to_bytes(2, "big") for value in values)
        pdu = (
            bytes([FC_WRITE_MULTIPLE])
            + int(address).to_bytes(2, "big")
            + len(values).to_bytes(2, "big")
            + bytes([len(payload)])
            + payload
        )
        response = self._request(pdu)
        expected = bytes([FC_WRITE_MULTIPLE]) + int(address).to_bytes(2, "big") + len(values).to_bytes(2, "big")
        if response != expected:
            raise ModbusError("Unexpected FC16 response")

    def _read_registers(self, function_code: int, address: int, count: int) -> list[int]:
        pdu = bytes([function_code]) + int(address).to_bytes(2, "big") + int(count).to_bytes(2, "big")
        response = self._request(pdu)
        if not response or response[0] != function_code:
            raise ModbusError("Unexpected read response")
        byte_count = response[1]
        if byte_count != count * 2 or len(response) != byte_count + 2:
            raise ModbusError("Malformed read response")
        return [int.from_bytes(response[2 + index : 4 + index], "big") for index in range(0, byte_count, 2)]

    def _request(self, pdu: bytes) -> bytes:
        response = self.transport.transact(self.slave_id, pdu)
        if not response:
            raise ModbusError("Empty response")
        if response[0] == (pdu[0] | 0x80):
            code = response[1] if len(response) > 1 else 0
            raise ModbusException(pdu[0], code)
        return response


class ModbusTransport:
    def connect(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def transact(self, slave_id: int, pdu: bytes) -> bytes:
        raise NotImplementedError


class TcpTransport(ModbusTransport):
    def __init__(self, host: str, port: int = 502, timeout: float = 0.5):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._socket: socket.socket | None = None
        self._transaction_id = 0

    def connect(self) -> None:
        self.close()
        self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._socket.settimeout(self.timeout)

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def transact(self, slave_id: int, pdu: bytes) -> bytes:
        if self._socket is None:
            raise ModbusError("TCP socket is not connected")
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        header = (
            self._transaction_id.to_bytes(2, "big")
            + b"\x00\x00"
            + (len(pdu) + 1).to_bytes(2, "big")
            + bytes([slave_id & 0xFF])
        )
        self._socket.sendall(header + pdu)
        response_header = _recv_exact(self._socket, 7)
        length = int.from_bytes(response_header[4:6], "big")
        if length < 1:
            raise ModbusError("Malformed MBAP length")
        return _recv_exact(self._socket, length - 1)


class RtuTransport(ModbusTransport):
    def __init__(self, port: str, baudrate: int, parity: str = "E", stopbits: int = 1, timeout: float = 0.5):
        self.port = port
        self.baudrate = int(baudrate)
        self.parity = parity.upper()
        self.stopbits = int(stopbits)
        self.timeout = float(timeout)
        self._serial: Any | None = None

    def connect(self) -> None:
        self.close()
        try:
            import serial
        except ImportError as error:
            raise ModbusError("RTU mode requires pyserial. Run: py -m pip install -r requirements.txt") from error
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity=self.parity,
            stopbits=self.stopbits,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def transact(self, slave_id: int, pdu: bytes) -> bytes:
        if self._serial is None:
            raise ModbusError("Serial port is not connected")
        frame = bytes([slave_id & 0xFF]) + pdu
        frame += crc16(frame).to_bytes(2, "little")
        self._serial.reset_input_buffer()
        self._serial.write(frame)
        self._serial.flush()

        function = pdu[0]
        header = self._serial.read(3)
        if len(header) < 3:
            raise ModbusError("RTU timeout")
        if header[0] != (slave_id & 0xFF):
            raise ModbusError("Unexpected RTU slave id")
        if header[1] == (function | 0x80):
            tail = self._serial.read(2)
            _validate_crc(header + tail)
            return header[1:3]
        if function in (FC_READ_INPUT, FC_READ_HOLDING):
            response = header + self._serial.read(header[2] + 2)
        elif function in (FC_WRITE_SINGLE, FC_WRITE_MULTIPLE):
            response = header + self._serial.read(5)
        else:
            response = header + self._serial.read(256)
        _validate_crc(response)
        return response[1:-2]


class DemoMdchClient(ModbusClient):
    is_demo = True

    def __init__(self, register_map: MdchRegisterMap):
        self.map = register_map
        self.connected = False
        self._lock = threading.Lock()
        self._holding: dict[int, int] = {}
        self._input: dict[int, int] = {}
        self._started = time.monotonic()
        self._active_rev = 1
        self._saved_rev = 1
        self._pulse_count = 0
        self._init_registers()

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def read_input_registers(self, address: int, count: int) -> list[int]:
        with self._lock:
            self._update_inputs()
            return [self._input.get(address + offset, 0) for offset in range(count)]

    def read_holding_registers(self, address: int, count: int) -> list[int]:
        with self._lock:
            return [self._holding.get(address + offset, 0) for offset in range(count)]

    def write_single_register(self, address: int, value: int) -> None:
        self.write_multiple_registers(address, [value])

    def write_multiple_registers(self, address: int, values: list[int]) -> None:
        with self._lock:
            if address == 0 and len(values) == 4:
                self._execute_command(values)
                return
            for offset, value in enumerate(values):
                current = address + offset
                if current >= 1000:
                    raise ModbusException(FC_WRITE_MULTIPLE, 0x02)
                self._holding[current] = int(value) & 0xFFFF
            self._set_input_symbol("CFG_FLAGS", 0x0001)

    def _init_registers(self) -> None:
        for field in self.map.draft_fields:
            value = field.default if field.default is not None else 0
            self._write_field_words(self._holding, field.address, field.encode(value))
        self._copy_draft_to_active()
        self._set_input_symbol("MAP_VERSION", 0x0200)
        self._set_input_symbol("MODEL_CODE", 1)
        self._set_input_symbol("FW_MAJOR", 0)
        self._set_input_symbol("FW_MINOR", 2)
        self._set_input_symbol("FW_BUILD", 1)
        self._set_input_symbol("HW_REVISION", 7)
        self._set_input_symbol("SERIAL_NUMBER", 260921001)
        self._set_input_symbol("CAPABILITIES", 0x000F)

    def _update_inputs(self) -> None:
        elapsed = time.monotonic() - self._started
        speed = max(0.0, 900.0 + 450.0 * math.sin(elapsed / 7.0) + 75.0 * math.sin(elapsed / 1.7))
        teeth = max(1, int(self._decode_holding_value("TEETH_N") or 1))
        ratio = float(self._decode_holding_value("SPEED_RATIO") or 1.0)
        frequency = speed * teeth / max(0.001, 60.0 * ratio)
        fast_speed = speed + 25.0 * math.sin(elapsed * 2.0)
        fast_freq = fast_speed * teeth / max(0.001, 60.0 * ratio)
        self._pulse_count += 1

        hh = float(self._decode_holding_value("HH_RPM") or 1800.0)
        h = float(self._decode_holding_value("H_RPM") or 1650.0)
        event_active = 0x0001 if speed >= hh else 0x0002 if speed >= h else 0

        self._set_input_symbol("FREQ_HZ", frequency)
        self._set_input_symbol("SPEED_RPM", speed)
        self._set_input_symbol("MEAS_FLAGS", 0x0001)
        self._set_input_symbol("EVENT_ACTIVE", event_active)
        self._set_input_symbol("EVENT_LATCHED", event_active)
        self._set_input_symbol("EVENT_UNACK", event_active)
        self._set_input_symbol("IO_STATUS", 0x10C0)
        self._set_input_symbol("MODULE_STATE", 3 if event_active & ~0x0006 else 2 if event_active else 1)
        self._set_input_symbol("FAST_FREQ_HZ", fast_freq)
        self._set_input_symbol("FAST_SPEED_RPM", fast_speed)
        self._set_input_symbol("PULSE_AGE_MS", int(20 + 10 * abs(math.sin(elapsed))))
        self._set_input_symbol("PULSE_COUNT", self._pulse_count)
        self._set_input_symbol("AO_COMMAND_MA", 4.0 + min(16.0, speed / 125.0))
        self._set_input_symbol("AO_DAC_CODE", int(1000 + min(3000, speed * 2)))
        self._set_input_symbol("PRECHECK_RESULT", 0)
        self._set_input_symbol("PRECHECK_AGE_MS", int(elapsed * 1000) % 60000)
        self._set_input_symbol("CFG_ACTIVE_REV", self._active_rev)
        self._set_input_symbol("CFG_SAVED_REV", self._saved_rev)
        self._set_input_symbol("UPTIME_S", int(elapsed))
        self._set_input_symbol("RUN_STATE", 1)
        self._set_input_symbol("SAMPLE_SEQ", int(elapsed * 10) & 0xFFFFFFFF)
        self._set_input_symbol("SAMPLE_AGE_MS", 0)
        self._set_input_symbol("MB_ADDRESS_ACTIVE", 1)
        self._set_input_symbol("MB_FORMAT_ACTIVE", 0)
        self._set_input_symbol("MB_BAUD_ACTIVE", 19200)

    def _execute_command(self, values: list[int]) -> None:
        cmd, arg, key, seq = [int(value) & 0xFFFF for value in values]
        result = 2
        detail = 0
        if cmd in (11, 12, 14, 20, 22) and key != 0xA55A:
            result = 5
            detail = 2
        elif cmd == 11:
            self._copy_draft_to_active()
            self._active_rev += 1
        elif cmd == 12:
            self._saved_rev = self._active_rev
        elif cmd == 13:
            self._copy_active_to_draft()
        elif cmd == 14:
            self._init_registers()
        elif cmd in (1, 2, 3):
            active = self._read_input_symbol("EVENT_ACTIVE") or 0
            latched = self._read_input_symbol("EVENT_LATCHED") or 0
            unack = self._read_input_symbol("EVENT_UNACK") or 0
            if cmd in (1, 3):
                self._set_input_symbol("EVENT_UNACK", unack & ~arg)
            if cmd in (2, 3):
                remaining = active & latched & arg
                self._set_input_symbol("EVENT_LATCHED", latched & ~(arg & ~active))
                result = 7 if remaining else 2
                detail = remaining
        self._set_input_symbol("LAST_CMD_SEQ", seq)
        self._set_input_symbol("LAST_CMD_CODE", cmd)
        self._set_input_symbol("CMD_RESULT", result)
        self._set_input_symbol("CMD_DETAIL", detail)

    def _copy_draft_to_active(self) -> None:
        for field in self.map.active_fields:
            source = self.map.holding_by_symbol.get(field.mirror_of)
            if source is None:
                continue
            words = [self._holding.get(source.address + offset, 0) for offset in range(source.words)]
            self._write_field_words(self._holding, field.address, words)
        self._set_input_symbol("CFG_FLAGS", 0)

    def _copy_active_to_draft(self) -> None:
        for field in self.map.active_fields:
            source = self.map.holding_by_symbol.get(field.mirror_of)
            if source is None:
                continue
            words = [self._holding.get(field.address + offset, 0) for offset in range(field.words)]
            self._write_field_words(self._holding, source.address, words)
        self._set_input_symbol("CFG_FLAGS", 0)

    def _decode_holding_value(self, symbol: str) -> Any:
        field = self.map.holding_by_symbol.get(symbol)
        if field is None:
            return None
        words = [self._holding.get(field.address + offset, 0) for offset in range(field.words)]
        return field.decode(words)

    def _set_input_symbol(self, symbol: str, value: Any) -> None:
        field = self.map.input_by_symbol.get(symbol)
        if field is None:
            return
        self._write_field_words(self._input, field.address, field.encode(value))

    def _read_input_symbol(self, symbol: str) -> Any:
        field = self.map.input_by_symbol.get(symbol)
        if field is None:
            return None
        words = [self._input.get(field.address + offset, 0) for offset in range(field.words)]
        return field.decode(words)

    @staticmethod
    def _write_field_words(target: dict[int, int], address: int, words: list[int]) -> None:
        for offset, word in enumerate(words):
            target[address + offset] = int(word) & 0xFFFF


def create_client(connection: ModbusConnection, register_map: MdchRegisterMap) -> ModbusClient:
    mode = connection.mode.lower()
    if mode == "demo":
        return DemoMdchClient(register_map)
    if mode == "tcp":
        return StandardModbusClient(TcpTransport(connection.host, connection.tcp_port, connection.timeout), connection.slave_id)
    if mode == "rtu":
        return StandardModbusClient(
            RtuTransport(connection.port, connection.baudrate, connection.parity, connection.stopbits, connection.timeout),
            connection.slave_id,
        )
    raise ModbusError(f"Unknown connection mode: {connection.mode}")


def crc16(payload: bytes) -> int:
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def _validate_crc(frame: bytes) -> None:
    if len(frame) < 4:
        raise ModbusError("Short RTU frame")
    expected = int.from_bytes(frame[-2:], "little")
    actual = crc16(frame[:-2])
    if expected != actual:
        raise ModbusError("Bad RTU CRC")


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ModbusError("Socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
