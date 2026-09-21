from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
import queue
import threading
from typing import Any


EXCEL_MAX_ROWS = 1_048_576
CSV_DATA_ROWS_PER_FILE = EXCEL_MAX_ROWS - 1
CSV_WRITE_BATCH_SIZE = 1000
CSV_FLUSH_ROWS = 5000


class CsvSessionLogger:
    HEADER = [
        "Record_Number",
        "Session_Elapsed_s",
        "Wall_Time",
        "Source",
        "PDU_Address",
        "Register",
        "Symbol",
        "Name",
        "Type",
        "Value",
        "Unit",
        "Raw_Words",
        "Quality",
    ]

    def __init__(self, root_dir: str | Path, max_rows_per_file: int = CSV_DATA_ROWS_PER_FILE):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.max_rows_per_file = max(1, int(max_rows_per_file))
        self.session_dir: Path | None = None
        self.csv_path: Path | None = None
        self.csv_file_index = 1
        self.started_at: datetime | None = None
        self.dropped_rows = 0
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=50000)
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()
        self.start_new_session()

    def set_root_dir(self, root_dir: str | Path) -> Path:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        return self.start_new_session()

    def start_new_session(self) -> Path:
        started_at = datetime.now()
        session_dir = self._make_session_dir(started_at)
        done = threading.Event()
        result: dict[str, Any] = {}
        self._commands.put(("rotate", (session_dir, started_at, done, result)))
        done.wait(timeout=10)
        if result.get("error"):
            raise RuntimeError(result["error"])
        with self._state_lock:
            self.session_dir = session_dir
            self.csv_path = self._csv_path_for_index(session_dir, 1)
            self.csv_file_index = 1
            self.started_at = started_at
        return session_dir

    def log_value(self, source: str, field: Any, value: Any, raw_words: list[int], quality: str = "OK") -> None:
        payload = {
            "received_at": datetime.now(),
            "source": source,
            "address": int(getattr(field, "address", 0)),
            "register": _display_register(source, int(getattr(field, "address", 0))),
            "symbol": str(getattr(field, "symbol", "")),
            "name": str(getattr(field, "name", "")),
            "type": str(getattr(field, "type", "")),
            "value": value,
            "unit": str(getattr(field, "unit", "")),
            "raw_words": " ".join(f"{int(word) & 0xFFFF:04X}" for word in raw_words),
            "quality": quality,
        }
        try:
            self._commands.put_nowait(("value", payload))
        except queue.Full:
            self.dropped_rows += 1

    def close(self) -> None:
        done = threading.Event()
        self._commands.put(("stop", done))
        done.wait(timeout=10)
        self._thread.join(timeout=2)

    def _make_session_dir(self, started_at: datetime) -> Path:
        base_name = started_at.strftime("%Y%m%d_%H%M%S")
        candidate = self.root_dir / base_name
        index = 1
        while candidate.exists():
            index += 1
            candidate = self.root_dir / f"{base_name}_{index:02d}"
        return candidate

    def _csv_path_for_index(self, session_dir: Path, file_index: int) -> Path:
        if file_index <= 1:
            return session_dir / "mdch_values.csv"
        return session_dir / f"mdch_values_{file_index:03d}.csv"

    def _open_csv_file(self, session_dir: Path, file_index: int):
        path = self._csv_path_for_index(session_dir, file_index)
        csv_file = path.open("w", newline="", encoding="utf-8-sig")
        writer = csv.writer(csv_file, delimiter=",", quotechar='"', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(self.HEADER)
        csv_file.flush()
        with self._state_lock:
            self.csv_path = path
            self.csv_file_index = file_index
        return csv_file, writer

    def _writer_loop(self) -> None:
        csv_file = None
        writer = None
        session_started_at: datetime | None = None
        active_session_dir: Path | None = None
        csv_file_index = 1
        rows_in_current_file = 0
        record_number = 0
        pending_flush = 0
        deferred_command: tuple[str, Any] | None = None

        def collect_batch(first_payload: Any) -> list[Any]:
            nonlocal deferred_command
            payloads = [first_payload]
            while len(payloads) < CSV_WRITE_BATCH_SIZE:
                try:
                    next_command, next_payload = self._commands.get_nowait()
                except queue.Empty:
                    break
                if next_command != "value":
                    deferred_command = (next_command, next_payload)
                    break
                payloads.append(next_payload)
            return payloads

        while True:
            if deferred_command is not None:
                command, payload = deferred_command
                deferred_command = None
            else:
                try:
                    command, payload = self._commands.get(timeout=0.5)
                except queue.Empty:
                    if csv_file and pending_flush:
                        csv_file.flush()
                        pending_flush = 0
                    continue

            if command == "rotate":
                session_dir, started_at, done, result = payload
                try:
                    if csv_file:
                        csv_file.flush()
                        csv_file.close()
                    session_dir.mkdir(parents=True, exist_ok=True)
                    active_session_dir = session_dir
                    csv_file_index = 1
                    rows_in_current_file = 0
                    record_number = 0
                    pending_flush = 0
                    csv_file, writer = self._open_csv_file(active_session_dir, csv_file_index)
                    session_started_at = started_at
                except Exception as error:
                    result["error"] = str(error)
                finally:
                    done.set()
            elif command == "value":
                if not writer or not csv_file or not session_started_at:
                    continue
                rows = []
                for item in collect_batch(payload):
                    if active_session_dir is not None and rows_in_current_file >= self.max_rows_per_file:
                        writer.writerows(rows)
                        rows.clear()
                        csv_file.flush()
                        csv_file.close()
                        csv_file_index += 1
                        rows_in_current_file = 0
                        csv_file, writer = self._open_csv_file(active_session_dir, csv_file_index)
                        pending_flush = 0
                    record_number += 1
                    rows_in_current_file += 1
                    rows.append(_row_from_payload(record_number, session_started_at, item))
                writer.writerows(rows)
                pending_flush += len(rows)
                if pending_flush >= CSV_FLUSH_ROWS:
                    csv_file.flush()
                    pending_flush = 0
            elif command == "stop":
                done = payload
                if csv_file:
                    csv_file.flush()
                    csv_file.close()
                done.set()
                return


def _row_from_payload(record_number: int, session_started_at: datetime, payload: dict[str, Any]) -> list[Any]:
    received_at = payload["received_at"]
    elapsed = max(0.0, (received_at - session_started_at).total_seconds())
    return [
        record_number,
        f"{elapsed:.6f}",
        received_at.isoformat(timespec="milliseconds"),
        payload["source"],
        payload["address"],
        payload["register"],
        payload["symbol"],
        payload["name"],
        payload["type"],
        _format_value(payload["value"]),
        payload["unit"],
        payload["raw_words"],
        payload["quality"],
    ]


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _display_register(source: str, address: int) -> int:
    if source.lower().startswith("input"):
        return 30001 + address
    return 40001 + address
