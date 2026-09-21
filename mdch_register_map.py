from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import struct
from typing import Any, Iterable


DEFAULT_MAP_PATH = Path(__file__).resolve().parent / "templates" / "mdch_v2.json"


class RegisterMapError(ValueError):
    pass


@dataclass(frozen=True)
class RegisterField:
    table: str
    address: int
    words: int
    type: str
    symbol: str
    name: str
    unit: str = ""
    description: str = ""
    access: str = "read"
    group: str = ""
    default: Any = None
    allowed: str = ""
    source: str = ""
    mirror_of: str = ""

    @property
    def end_address(self) -> int:
        return self.address + self.words

    @property
    def writable(self) -> bool:
        access = self.access.lower()
        return "write" in access or "fc06" in access or "fc16" in access

    def decode(self, words: list[int]) -> Any:
        if len(words) != self.words:
            raise RegisterMapError(f"{self.symbol}: expected {self.words} words, got {len(words)}")

        words = [int(word) & 0xFFFF for word in words]
        field_type = self.type.upper()
        if field_type == "U16":
            return words[0]
        if field_type == "I16":
            return words[0] - 0x10000 if words[0] & 0x8000 else words[0]
        if field_type == "U32":
            return (words[0] << 16) | words[1]
        if field_type == "I32":
            raw = (words[0] << 16) | words[1]
            return raw - 0x100000000 if raw & 0x80000000 else raw
        if field_type == "F32":
            raw = ((words[1] << 16) | words[0]).to_bytes(4, byteorder="big", signed=False)
            return struct.unpack(">f", raw)[0]
        raise RegisterMapError(f"{self.symbol}: unsupported type {self.type}")

    def encode(self, value: Any) -> list[int]:
        field_type = self.type.upper()
        if field_type == "U16":
            raw = int(value)
            _check_range(self.symbol, raw, 0, 0xFFFF)
            return [raw]
        if field_type == "I16":
            raw = int(value)
            _check_range(self.symbol, raw, -0x8000, 0x7FFF)
            return [raw & 0xFFFF]
        if field_type == "U32":
            raw = int(value)
            _check_range(self.symbol, raw, 0, 0xFFFFFFFF)
            return [(raw >> 16) & 0xFFFF, raw & 0xFFFF]
        if field_type == "I32":
            raw = int(value)
            _check_range(self.symbol, raw, -0x80000000, 0x7FFFFFFF)
            raw &= 0xFFFFFFFF
            return [(raw >> 16) & 0xFFFF, raw & 0xFFFF]
        if field_type == "F32":
            number = float(value)
            if not math.isfinite(number):
                raise RegisterMapError(f"{self.symbol}: F32 must be finite")
            raw = int.from_bytes(struct.pack(">f", number), byteorder="big", signed=False)
            return [raw & 0xFFFF, (raw >> 16) & 0xFFFF]
        raise RegisterMapError(f"{self.symbol}: unsupported type {self.type}")

    def format_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.6g}"
        return str(value)


@dataclass(frozen=True)
class CommandDefinition:
    code: int
    name: str
    key: int
    arg: str
    description: str


@dataclass(frozen=True)
class BitDefinition:
    register: str
    bit: int
    mask_hex: str
    symbol: str
    description: str


class MdchRegisterMap:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.version = str(payload.get("version", ""))
        self.source = str(payload.get("source", ""))
        self.input_fields = tuple(_field_from_payload("input", item) for item in payload.get("input_registers", []))
        self.holding_fields = tuple(_field_from_payload("holding", item) for item in payload.get("holding_registers", []))
        self.parameters = tuple(dict(item) for item in payload.get("parameters", []))
        self.commands = tuple(_command_from_payload(item) for item in payload.get("commands", []))
        self.bits = tuple(_bit_from_payload(item) for item in payload.get("bits", []))

        self.input_by_symbol = _by_symbol(self.input_fields)
        self.holding_by_symbol = _by_symbol(self.holding_fields)
        self.draft_fields = tuple(field for field in self.holding_fields if field.group and not field.mirror_of)
        self.active_fields = tuple(field for field in self.holding_fields if field.mirror_of)
        self.command_fields = tuple(field for field in self.holding_fields if field.address < 10)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_MAP_PATH) -> "MdchRegisterMap":
        map_path = Path(path)
        with map_path.open("r", encoding="utf-8") as file:
            return cls(json.load(file))

    def command_by_name(self, name: str) -> CommandDefinition:
        normalized = name.strip().upper()
        for command in self.commands:
            if command.name.upper() == normalized:
                return command
        raise KeyError(name)

    def decode_fields(self, fields: Iterable[RegisterField], start_address: int, words: list[int]) -> dict[str, Any]:
        decoded: dict[str, Any] = {}
        for field in fields:
            offset = field.address - start_address
            if offset < 0 or offset + field.words > len(words):
                continue
            decoded[field.symbol] = field.decode(words[offset : offset + field.words])
        return decoded

    def decode_input_block(self, start_address: int, words: list[int]) -> dict[str, Any]:
        return self.decode_fields(self.input_fields, start_address, words)

    def decode_holding_block(self, start_address: int, words: list[int]) -> dict[str, Any]:
        return self.decode_fields(self.holding_fields, start_address, words)

    def input_spans(self) -> list[tuple[int, int, tuple[RegisterField, ...]]]:
        return contiguous_spans(self.input_fields)

    def holding_spans(self) -> list[tuple[int, int, tuple[RegisterField, ...]]]:
        fields = [field for field in self.holding_fields if field.address >= 100]
        return contiguous_spans(fields)


def contiguous_spans(fields: Iterable[RegisterField]) -> list[tuple[int, int, tuple[RegisterField, ...]]]:
    sorted_fields = sorted(fields, key=lambda field: field.address)
    spans: list[tuple[int, int, list[RegisterField]]] = []
    for field in sorted_fields:
        if not spans or field.address > spans[-1][1]:
            spans.append((field.address, field.end_address, [field]))
        else:
            start, end, span_fields = spans[-1]
            span_fields.append(field)
            spans[-1] = (start, max(end, field.end_address), span_fields)
    return [(start, end - start, tuple(span_fields)) for start, end, span_fields in spans]


def _check_range(symbol: str, value: int, minimum: int, maximum: int) -> None:
    if value < minimum or value > maximum:
        raise RegisterMapError(f"{symbol}: {value} outside {minimum}..{maximum}")


def _field_from_payload(table: str, item: dict[str, Any]) -> RegisterField:
    return RegisterField(
        table=table,
        address=int(item["address"]),
        words=int(item["words"]),
        type=str(item["type"]),
        symbol=str(item["symbol"]),
        name=str(item.get("name") or item.get("symbol") or ""),
        unit=str(item.get("unit") or ""),
        description=str(item.get("description") or ""),
        access=str(item.get("access") or "read"),
        group=str(item.get("group") or ""),
        default=item.get("default"),
        allowed=str(item.get("allowed") or ""),
        source=str(item.get("source") or ""),
        mirror_of=str(item.get("mirror_of") or ""),
    )


def _command_from_payload(item: dict[str, Any]) -> CommandDefinition:
    key = item.get("key", 0)
    if isinstance(key, str) and key.lower().endswith("h"):
        key_value = int(key[:-1], 16)
    else:
        key_value = int(key)
    return CommandDefinition(
        code=int(item["code"]),
        name=str(item["name"]),
        key=key_value,
        arg=str(item.get("arg") or ""),
        description=str(item.get("description") or ""),
    )


def _bit_from_payload(item: dict[str, Any]) -> BitDefinition:
    return BitDefinition(
        register=str(item["register"]),
        bit=int(item["bit"]),
        mask_hex=str(item.get("mask_hex") or ""),
        symbol=str(item["symbol"]),
        description=str(item.get("description") or ""),
    )


def _by_symbol(fields: Iterable[RegisterField]) -> dict[str, RegisterField]:
    result: dict[str, RegisterField] = {}
    for field in fields:
        result[field.symbol] = field
    return result
