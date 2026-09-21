from __future__ import annotations

from collections import deque
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from mdch_register_map import DEFAULT_MAP_PATH, MdchRegisterMap, RegisterField
from mdch_session_logger import CsvSessionLogger
from modbus_client import ModbusConnection, ModbusError, create_client


APP_DIR = Path(__file__).resolve().parent
DEFAULT_SESSION_ROOT = APP_DIR / "sessions"
BAUD_RATES = ("9600", "14400", "19200", "28800", "38400", "57600", "115200")
BIT_MASK_SYMBOLS = {
    "EVENT_ACTIVE",
    "EVENT_LATCHED",
    "EVENT_UNACK",
    "MEAS_FLAGS",
    "IO_STATUS",
    "CFG_FLAGS",
    "CAPABILITIES",
}


class PollWorker(threading.Thread):
    def __init__(
        self,
        client: Any,
        register_map: MdchRegisterMap,
        output_queue: queue.Queue[dict[str, Any]],
        event_queue: queue.Queue[tuple[str, str]],
        poll_ms: int,
    ):
        super().__init__(daemon=True)
        self.client = client
        self.map = register_map
        self.output_queue = output_queue
        self.event_queue = event_queue
        self.poll_ms = max(100, int(poll_ms))
        self.stop_event = threading.Event()

    def run(self) -> None:
        try:
            self.client.connect()
            self.event_queue.put(("connected", "Подключено"))
            holding_counter = 0
            while not self.stop_event.is_set():
                snapshot: dict[str, Any] = {"input": {}, "holding": {}, "raw": {}, "ts": time.time()}
                self._poll_input(snapshot)
                holding_counter += 1
                if holding_counter >= 5:
                    holding_counter = 0
                    self._poll_holding(snapshot)
                self.output_queue.put(snapshot)
                self.stop_event.wait(self.poll_ms / 1000.0)
        except Exception as error:
            self.event_queue.put(("error", str(error)))
        finally:
            try:
                self.client.close()
            finally:
                self.event_queue.put(("disconnected", "Отключено"))

    def stop(self) -> None:
        self.stop_event.set()

    def _poll_input(self, snapshot: dict[str, Any]) -> None:
        for address, count, fields in self.map.input_spans():
            words = self.client.read_input_registers(address, count)
            snapshot["input"].update(self.map.decode_fields(fields, address, words))
            for field in fields:
                offset = field.address - address
                snapshot["raw"][("input", field.symbol)] = words[offset : offset + field.words]

    def _poll_holding(self, snapshot: dict[str, Any]) -> None:
        for address, count, fields in self.map.holding_spans():
            words = self.client.read_holding_registers(address, count)
            snapshot["holding"].update(self.map.decode_fields(fields, address, words))
            for field in fields:
                offset = field.address - address
                snapshot["raw"][("holding", field.symbol)] = words[offset : offset + field.words]


class SignalChart(ttk.Frame):
    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.values: deque[tuple[float, float]] = deque(maxlen=1200)
        self.canvas = tk.Canvas(self, height=210, background="#ffffff", highlightthickness=1, highlightbackground="#cbd5e1")
        self.canvas.pack(fill=tk.BOTH, expand=True)

    def append(self, timestamp: float, value: float) -> None:
        self.values.append((timestamp, value))

    def clear(self) -> None:
        self.values.clear()
        self.draw()

    def draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(10, canvas.winfo_width())
        height = max(10, canvas.winfo_height())
        left, top, right, bottom = 54, 16, width - 16, height - 32
        canvas.create_rectangle(left, top, right, bottom, outline="#e2e8f0")
        canvas.create_text(10, 8, anchor="nw", text="SPEED_RPM", fill="#334155", font=("Segoe UI", 9, "bold"))
        if len(self.values) < 2:
            canvas.create_text(width / 2, height / 2, text="Нет данных", fill="#94a3b8", font=("Segoe UI", 10))
            return
        now = self.values[-1][0]
        points = [(ts, value) for ts, value in self.values if now - ts <= 120]
        if len(points) < 2:
            return
        values = [value for _, value in points]
        minimum = min(values)
        maximum = max(values)
        if abs(maximum - minimum) < 1e-6:
            minimum -= 1
            maximum += 1
        start = points[0][0]
        span = max(1e-6, points[-1][0] - start)
        coords = []
        for ts, value in points:
            x = left + (ts - start) / span * (right - left)
            y = bottom - (value - minimum) / (maximum - minimum) * (bottom - top)
            coords.extend([x, y])
        canvas.create_line(*coords, fill="#2563eb", width=2)
        canvas.create_text(left, bottom + 8, anchor="nw", text=time.strftime("%H:%M:%S", time.localtime(start)), fill="#64748b", font=("Segoe UI", 8))
        canvas.create_text(right, bottom + 8, anchor="ne", text=time.strftime("%H:%M:%S", time.localtime(points[-1][0])), fill="#64748b", font=("Segoe UI", 8))
        canvas.create_text(6, top, anchor="nw", text=f"{maximum:.1f}", fill="#64748b", font=("Segoe UI", 8))
        canvas.create_text(6, bottom - 12, anchor="nw", text=f"{minimum:.1f}", fill="#64748b", font=("Segoe UI", 8))


class MdchServiceApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("FSM Service - MDCH Modbus")
        self.root.geometry("1280x780")
        self.map_path = tk.StringVar(value=str(DEFAULT_MAP_PATH))
        self.mode_var = tk.StringVar(value="Demo")
        self.port_var = tk.StringVar(value="COM1")
        self.baud_var = tk.StringVar(value="115200")
        self.parity_var = tk.StringVar(value="E")
        self.stopbits_var = tk.StringVar(value="1")
        self.host_var = tk.StringVar(value="127.0.0.1")
        self.tcp_port_var = tk.StringVar(value="502")
        self.slave_var = tk.StringVar(value="1")
        self.timeout_var = tk.StringVar(value="0.5")
        self.poll_var = tk.StringVar(value="500")
        self.status_var = tk.StringVar(value="Готово")
        self.session_var = tk.StringVar(value="")

        self.register_map = MdchRegisterMap.load(DEFAULT_MAP_PATH)
        self.logger = CsvSessionLogger(DEFAULT_SESSION_ROOT)
        self.client = None
        self.worker: PollWorker | None = None
        self.message_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
        self.event_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.latest_input: dict[str, Any] = {}
        self.latest_holding: dict[str, Any] = {}
        self.previous_masks: dict[str, int] = {}
        self.bit_definitions_by_symbol: dict[str, list[Any]] = {}
        self.event_row_count = 0
        self.command_seq = 0

        self._build_ui()
        self._load_map_into_tables()
        self._update_session_label()
        self.root.after(50, self._poll_queues)
        self.root.after(250, self._redraw_chart)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        top = ttk.Frame(self.root, padding=8)
        top.grid(row=0, column=0, sticky="ew")
        for index in range(18):
            top.columnconfigure(index, weight=0)
        top.columnconfigure(17, weight=1)

        ttk.Label(top, text="Режим").grid(row=0, column=0, padx=3)
        ttk.Combobox(top, textvariable=self.mode_var, values=("Demo", "RTU", "TCP"), width=7, state="readonly").grid(row=0, column=1, padx=3)
        ttk.Label(top, text="COM").grid(row=0, column=2, padx=3)
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, values=_list_serial_ports(), width=10)
        self.port_combo.grid(row=0, column=3, padx=3)
        ttk.Button(top, text="Обн.", command=self.refresh_ports, width=5).grid(row=0, column=4, padx=3)
        ttk.Label(top, text="Baud").grid(row=0, column=5, padx=3)
        ttk.Combobox(top, textvariable=self.baud_var, values=BAUD_RATES, width=8, state="readonly").grid(row=0, column=6, padx=3)
        ttk.Label(top, text="Fmt").grid(row=0, column=7, padx=3)
        ttk.Combobox(top, textvariable=self.parity_var, values=("E", "O", "N"), width=3, state="readonly").grid(row=0, column=8, padx=3)
        ttk.Entry(top, textvariable=self.stopbits_var, width=3).grid(row=0, column=9, padx=3)
        ttk.Label(top, text="TCP").grid(row=0, column=10, padx=3)
        ttk.Entry(top, textvariable=self.host_var, width=13).grid(row=0, column=11, padx=3)
        ttk.Entry(top, textvariable=self.tcp_port_var, width=6).grid(row=0, column=12, padx=3)
        ttk.Label(top, text="Slave").grid(row=0, column=13, padx=3)
        ttk.Entry(top, textvariable=self.slave_var, width=4).grid(row=0, column=14, padx=3)
        ttk.Label(top, text="Опрос, мс").grid(row=0, column=15, padx=3)
        ttk.Entry(top, textvariable=self.poll_var, width=6).grid(row=0, column=16, padx=3, sticky="w")

        actions = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        actions.grid(row=1, column=0, sticky="ew")
        self.connect_button = ttk.Button(actions, text="Подключить", command=self.connect)
        self.connect_button.pack(side=tk.LEFT, padx=3)
        self.disconnect_button = ttk.Button(actions, text="Отключить", command=self.disconnect, state=tk.DISABLED)
        self.disconnect_button.pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text="Новая сессия", command=self.new_session).pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text="Карта...", command=self.choose_map).pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text="Очистить график", command=self.clear_chart).pack(side=tk.LEFT, padx=3)
        ttk.Label(actions, textvariable=self.session_var).pack(side=tk.LEFT, padx=12)
        ttk.Label(actions, textvariable=self.status_var).pack(side=tk.RIGHT, padx=3)

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.measure_tab = ttk.Frame(notebook)
        self.param_tab = ttk.Frame(notebook)
        self.event_tab = ttk.Frame(notebook)
        self.command_tab = ttk.Frame(notebook)
        self.chart_tab = ttk.Frame(notebook)
        notebook.add(self.measure_tab, text="Измерения")
        notebook.add(self.param_tab, text="Параметры")
        notebook.add(self.event_tab, text="События")
        notebook.add(self.command_tab, text="Команды")
        notebook.add(self.chart_tab, text="График")
        self._build_measure_tab()
        self._build_param_tab()
        self._build_event_tab()
        self._build_command_tab()
        self.chart = SignalChart(self.chart_tab)
        self.chart.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    def _build_measure_tab(self) -> None:
        columns = ("register", "symbol", "value", "unit", "name")
        self.measure_tree = ttk.Treeview(self.measure_tab, columns=columns, show="headings")
        for column, title, width in (
            ("register", "PDU", 70),
            ("symbol", "Обозначение", 160),
            ("value", "Значение", 120),
            ("unit", "Ед.", 80),
            ("name", "Параметр", 520),
        ):
            self.measure_tree.heading(column, text=title)
            self.measure_tree.column(column, width=width, anchor=tk.W)
        self.measure_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    def _build_param_tab(self) -> None:
        toolbar = ttk.Frame(self.param_tab)
        toolbar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(toolbar, text="Записать черновик", command=self.write_selected_parameter).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="Обновить Holding", command=self.refresh_holding_once).pack(side=tk.LEFT, padx=3)

        columns = ("group", "draft", "active", "symbol", "type", "unit", "allowed", "name")
        self.param_tree = ttk.Treeview(self.param_tab, columns=columns, show="headings")
        headings = {
            "group": ("Группа", 130),
            "draft": ("Черновик", 90),
            "active": ("Применено", 90),
            "symbol": ("Обозначение", 150),
            "type": ("Тип", 60),
            "unit": ("Ед.", 70),
            "allowed": ("Допустимо", 180),
            "name": ("Параметр", 360),
        }
        for column, (title, width) in headings.items():
            self.param_tree.heading(column, text=title)
            self.param_tree.column(column, width=width, anchor=tk.W)
        self.param_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

    def _build_event_tab(self) -> None:
        toolbar = ttk.Frame(self.event_tab)
        toolbar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(toolbar, text="Очистить журнал", command=self.clear_event_log).pack(side=tk.LEFT, padx=3)
        columns = ("time", "register", "bit", "state", "symbol", "description")
        self.event_tree = ttk.Treeview(self.event_tab, columns=columns, show="headings")
        headings = {
            "time": ("Время", 90),
            "register": ("Регистр", 120),
            "bit": ("Бит", 50),
            "state": ("Состояние", 90),
            "symbol": ("Обозначение", 150),
            "description": ("Описание", 720),
        }
        for column, (title, width) in headings.items():
            self.event_tree.heading(column, text=title)
            self.event_tree.column(column, width=width, anchor=tk.W)
        self.event_tree.tag_configure("set", foreground="#b91c1c")
        self.event_tree.tag_configure("clear", foreground="#166534")
        self.event_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

    def _build_command_tab(self) -> None:
        left = ttk.Frame(self.command_tab, padding=8)
        left.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(left, text="Команда").pack(anchor=tk.W)
        self.command_var = tk.StringVar(value="VALIDATE")
        names = [command.name for command in self.register_map.commands]
        ttk.Combobox(left, textvariable=self.command_var, values=names, width=24, state="readonly").pack(anchor=tk.W, pady=3)
        ttk.Label(left, text="ARG").pack(anchor=tk.W)
        self.command_arg_var = tk.StringVar(value="0")
        ttk.Entry(left, textvariable=self.command_arg_var, width=24).pack(anchor=tk.W, pady=3)
        ttk.Button(left, text="Выполнить", command=self.execute_selected_command).pack(anchor=tk.W, pady=8)
        self.command_text = tk.Text(self.command_tab, height=20, wrap=tk.WORD)
        self.command_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)

    def _load_map_into_tables(self) -> None:
        self._rebuild_bit_index()
        self.measure_tree.delete(*self.measure_tree.get_children())
        for field in self.register_map.input_fields:
            self.measure_tree.insert("", tk.END, iid=f"input:{field.symbol}", values=(field.address, field.symbol, "", field.unit, field.name))

        self.param_tree.delete(*self.param_tree.get_children())
        for field in self.register_map.draft_fields:
            active_symbol = f"{field.symbol}_ACTIVE"
            active = self.register_map.holding_by_symbol.get(active_symbol)
            active_text = "" if active is None else active.address
            self.param_tree.insert(
                "",
                tk.END,
                iid=f"param:{field.symbol}",
                values=(field.group, field.address, active_text, field.symbol, field.type, field.unit, field.allowed, field.name),
            )

    def connect(self) -> None:
        if self.worker is not None:
            return
        try:
            self.previous_masks.clear()
            connection = self._connection_from_ui()
            self.client = create_client(connection, self.register_map)
            self.worker = PollWorker(self.client, self.register_map, self.message_queue, self.event_queue, int(self.poll_var.get()))
            self.worker.start()
            self.connect_button.configure(state=tk.DISABLED)
            self.disconnect_button.configure(state=tk.NORMAL)
            self.status_var.set("Подключение...")
        except Exception as error:
            messagebox.showerror("Подключение", str(error))

    def disconnect(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        if self.client is not None:
            self.client.close()
            self.client = None
        self.connect_button.configure(state=tk.NORMAL)
        self.disconnect_button.configure(state=tk.DISABLED)
        self.status_var.set("Отключено")

    def refresh_ports(self) -> None:
        ports = _list_serial_ports()
        self.port_combo.configure(values=ports)
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def new_session(self) -> None:
        try:
            self.logger.start_new_session()
            self._update_session_label()
        except Exception as error:
            messagebox.showerror("Сессия", str(error))

    def choose_map(self) -> None:
        path = filedialog.askopenfilename(title="Открыть карту регистров", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.register_map = MdchRegisterMap.load(path)
            self.map_path.set(path)
            self.previous_masks.clear()
            self._load_map_into_tables()
            self.status_var.set(f"Карта: {Path(path).name}")
        except Exception as error:
            messagebox.showerror("Карта", str(error))

    def refresh_holding_once(self) -> None:
        if self.client is None:
            messagebox.showinfo("Holding", "Сначала подключитесь")
            return
        try:
            snapshot = {"input": {}, "holding": {}, "raw": {}, "ts": time.time()}
            PollWorker(self.client, self.register_map, self.message_queue, self.event_queue, int(self.poll_var.get()))._poll_holding(snapshot)
            self._handle_snapshot(snapshot)
        except Exception as error:
            messagebox.showerror("Holding", str(error))

    def write_selected_parameter(self) -> None:
        item = self.param_tree.focus()
        if not item or not item.startswith("param:"):
            messagebox.showinfo("Параметр", "Выберите параметр")
            return
        if self.client is None:
            messagebox.showinfo("Параметр", "Сначала подключитесь")
            return
        symbol = item.split(":", 1)[1]
        field = self.register_map.holding_by_symbol[symbol]
        if field.address < 100 or field.mirror_of:
            messagebox.showerror("Параметр", "Запись разрешена только в черновик")
            return
        current = self.latest_holding.get(symbol, field.default if field.default is not None else "")
        value_text = simpledialog.askstring("Запись параметра", f"{field.symbol} ({field.type})\n{field.name}", initialvalue=str(current))
        if value_text is None:
            return
        try:
            value = _parse_value(value_text, field)
            words = field.encode(value)
            if field.type.upper() == "U16" and len(words) == 1:
                self.client.write_single_register(field.address, words[0])
            else:
                self.client.write_multiple_registers(field.address, words)
            self.status_var.set(f"Записан черновик {field.symbol}")
            self.refresh_holding_once()
        except Exception as error:
            messagebox.showerror("Запись параметра", str(error))

    def execute_selected_command(self) -> None:
        if self.client is None:
            messagebox.showinfo("Команда", "Сначала подключитесь")
            return
        try:
            command = self.register_map.command_by_name(self.command_var.get())
            arg = int(self.command_arg_var.get(), 0)
            self.command_seq = 1 if self.command_seq >= 65535 else self.command_seq + 1
            self.client.write_multiple_registers(0, [command.code, arg, command.key, self.command_seq])
            self._append_command_log(f"SEQ={self.command_seq} {command.name} ARG=0x{arg:04X}")
        except Exception as error:
            messagebox.showerror("Команда", str(error))

    def clear_chart(self) -> None:
        self.chart.clear()

    def clear_event_log(self) -> None:
        self.event_tree.delete(*self.event_tree.get_children())
        self.event_row_count = 0

    def _connection_from_ui(self) -> ModbusConnection:
        return ModbusConnection(
            mode=self.mode_var.get().lower(),
            slave_id=int(self.slave_var.get()),
            timeout=float(self.timeout_var.get()),
            port=self.port_var.get().strip(),
            baudrate=int(self.baud_var.get()),
            parity=self.parity_var.get().strip().upper(),
            stopbits=int(self.stopbits_var.get()),
            host=self.host_var.get().strip(),
            tcp_port=int(self.tcp_port_var.get()),
        )

    def _poll_queues(self) -> None:
        handled = 0
        while handled < 100:
            try:
                snapshot = self.message_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_snapshot(snapshot)
            handled += 1
        while True:
            try:
                kind, message = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(kind, message)
        self.root.after(30, self._poll_queues)

    def _handle_snapshot(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("input"):
            self._remember_bit_events(snapshot["input"])
            self.latest_input.update(snapshot["input"])
            self._update_measurements(snapshot)
            speed = self.latest_input.get("SPEED_RPM")
            if isinstance(speed, (int, float)):
                self.chart.append(snapshot.get("ts", time.time()), float(speed))
        if snapshot.get("holding"):
            self.latest_holding.update(snapshot["holding"])
            self._update_parameters()

    def _update_measurements(self, snapshot: dict[str, Any]) -> None:
        for field in self.register_map.input_fields:
            if field.symbol not in self.latest_input:
                continue
            value = self.latest_input[field.symbol]
            self.measure_tree.set(f"input:{field.symbol}", "value", field.format_value(value))
            raw = snapshot.get("raw", {}).get(("input", field.symbol), [])
            self.logger.log_value("input", field, value, raw)

    def _update_parameters(self) -> None:
        for field in self.register_map.draft_fields:
            item_id = f"param:{field.symbol}"
            if item_id not in self.param_tree.get_children(""):
                continue
            draft = self.latest_holding.get(field.symbol, "")
            active = self.latest_holding.get(f"{field.symbol}_ACTIVE", "")
            self.param_tree.set(item_id, "draft", field.format_value(draft))
            self.param_tree.set(item_id, "active", field.format_value(active))

    def _handle_event(self, kind: str, message: str) -> None:
        if kind == "connected":
            self.status_var.set(message)
            self.connect_button.configure(state=tk.DISABLED)
            self.disconnect_button.configure(state=tk.NORMAL)
        elif kind == "disconnected":
            if self.worker is not None and self.worker.stop_event.is_set():
                pass
            self.status_var.set(message)
            self.connect_button.configure(state=tk.NORMAL)
            self.disconnect_button.configure(state=tk.DISABLED)
            self.worker = None
        elif kind == "error":
            self.status_var.set(f"Ошибка: {message}")
            self._append_command_log(f"Ошибка: {message}")
            self.connect_button.configure(state=tk.NORMAL)
            self.disconnect_button.configure(state=tk.DISABLED)
            self.worker = None
        else:
            self.status_var.set(message)

    def _rebuild_bit_index(self) -> None:
        result: dict[str, list[Any]] = {}
        for bit in self.register_map.bits:
            register_text = bit.register.upper()
            if register_text.startswith("EVENT"):
                symbols = ("EVENT_ACTIVE", "EVENT_LATCHED", "EVENT_UNACK")
            else:
                symbols = [symbol for symbol in BIT_MASK_SYMBOLS if symbol in register_text]
            for symbol in symbols:
                result.setdefault(symbol, []).append(bit)
        self.bit_definitions_by_symbol = result

    def _remember_bit_events(self, values: dict[str, Any]) -> None:
        for symbol, bits in self.bit_definitions_by_symbol.items():
            if symbol not in values:
                continue
            try:
                current = int(values[symbol])
            except (TypeError, ValueError):
                continue
            previous = self.previous_masks.get(symbol)
            self.previous_masks[symbol] = current
            if previous is None:
                continue
            changed = previous ^ current
            if changed == 0:
                continue
            for bit in bits:
                mask = 1 << bit.bit
                if not changed & mask:
                    continue
                is_set = bool(current & mask)
                self._append_event_row(symbol, bit.bit, bit.symbol, bit.description, is_set)

    def _append_event_row(self, register: str, bit: int, symbol: str, description: str, is_set: bool) -> None:
        self.event_row_count += 1
        state = "Взведён" if is_set else "Снят"
        tag = "set" if is_set else "clear"
        item_id = f"event:{self.event_row_count}"
        self.event_tree.insert(
            "",
            0,
            iid=item_id,
            values=(time.strftime("%H:%M:%S"), register, bit, state, symbol, description),
            tags=(tag,),
        )

    def _append_command_log(self, line: str) -> None:
        self.command_text.insert(tk.END, f"{time.strftime('%H:%M:%S')} {line}\n")
        self.command_text.see(tk.END)

    def _redraw_chart(self) -> None:
        self.chart.draw()
        self.root.after(250, self._redraw_chart)

    def _update_session_label(self) -> None:
        if self.logger.session_dir is None:
            self.session_var.set("")
            return
        csv_name = self.logger.csv_path.name if self.logger.csv_path else "mdch_values.csv"
        self.session_var.set(f"Сессия: {self.logger.session_dir} | CSV: {csv_name}")

    def close(self) -> None:
        self.disconnect()
        self.logger.close()
        self.root.destroy()


def _parse_value(text: str, field: RegisterField) -> Any:
    stripped = text.strip().replace(",", ".")
    if field.type.upper() == "F32":
        return float(stripped)
    return int(stripped, 0)


def _list_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports

        ports = [port.device for port in list_ports.comports()]
    except Exception:
        ports = []
    if ports:
        return ports
    return [f"COM{index}" for index in range(1, 17)]


def main() -> None:
    root = tk.Tk()
    try:
        root.call("source", "azure.tcl")
        root.call("set_theme", "light")
    except tk.TclError:
        pass
    app = MdchServiceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
