"""Brush Control: real startup, calibration, and X-axis cleaning motion.
Install: pip install textual pyserial
Run: python brush_control_startup_g28.py
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import threading
import time
import math
import re
import csv
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import serial
from textual import on, work
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, ProgressBar, Static

PORT = "/dev/ttyUSB0"
BAUD = 250000
HX711_REFERENCE_UNIT = 201.36  # Verify against a known weight before trusting N readings.
PRESSURE_SAMPLE_INTERVAL_S = 0.5  # Target cadence; HX711 read latency may cause delays.

AREAS = {"run1": (105, 33), "run2": (105, 68), "run3": (105, 103)}
STANDBY = (-2.0, 155.0, 80.0)
PICKUP = (-2.0, 305.0, 0.0)
CALIBRATION_Z = 30.0
POST_STOP_Y = 310.0  # Must be within the printer firmware Y travel limits.

# Fixed positioning speeds (mm/min). G28 homing speed is set by Marlin firmware.
# Tune these to your printer limits; firmware may cap requested speeds.
TRAVEL_XY_FEED = 4000  # Existing setting from supplied script; positioning and return movements
TRAVEL_Z_FEED = 1500   # Existing setting from supplied script; firmware Z limit may be lower
Z_JOG_FEED = 1200      # 20 mm/s requested for 1-mm calibration jogs
PRESSURE_LOG_DIR = Path(__file__).resolve().parent / "pressure_logs"
SAMPLE_FIELDS = ["timestamp", "run_id", "area", "stroke", "speed_m_min", "pressure_g", "force_n"]
STROKE_FIELDS = ["run_id", "area", "stroke", "planned_strokes", "speed_m_min",
                 "z_mm", "start_x_mm", "end_x_mm", "started_at", "ended_at",
                 "status", "sample_count", "avg_force_n", "peak_force_n",
                 "min_force_n", "avg_pressure_g", "peak_pressure_g"]
LOG_FIELDS = ["record_type"] + list(dict.fromkeys(SAMPLE_FIELDS + STROKE_FIELDS))
POSITION_TOLERANCE = 0.15
Z_MAX, Z_MIN_APPROACH, Z_STEP = 80.0, -3, 1.0


class InspectionDialog(ModalScreen[str]):
    """Operator acknowledgement; this dialog does not verify physical safety."""
    CSS = """
    InspectionDialog { align: center middle; background: #08101c 85%; }
    #inspection-dialog { width: 70; height: auto; padding: 2; background: #17283c; border: thick #f0a55a; }
    #inspection-actions { height: 3; margin-top: 2; }
    #inspection-actions Button { width: 1fr; margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="inspection-dialog"):
            yield Static("POST INSPECTION - MACHINE STOP REQUESTED")
            yield Static(
                "M410 requests a quickstop; it is NOT an emergency stop. "
                "Wait for ALL motion to stop. Inspect the machine, ensure the "
                "brush is clear and the entire homing path is unobstructed "
                "before pressing GO HOME. This will abandon the current run, home all axes, then move ONLY Y to 310 while X and Z remain at their homed positions. Confirm this path is clear and Y310 is within travel limits."
            )
            with Horizontal(id="inspection-actions"):
                yield Button("GO HOME (G28)", id="inspection-confirm", variant="primary")

    @on(Button.Pressed, "#inspection-confirm")
    def confirm(self):
        self.dismiss("home")


class Dashboard(App):
    TITLE = "Brush Control"
    SUB_TITLE = "Artillery Sidewinder | STARTUP HOME + PICKUP"
    CSS = """
    Screen { background: #0a101c; color: #e8f0fa; }
    Header { background: #101b2c; color: #e9f5ff; }
    Footer { background: #101b2c; }
    #main { width: 100%; height: 1fr; padding: 0 2; }
    #content { width: 100%; max-width: 108; height: auto; margin: 1 0; padding: 0 1; }
    #fixed-panel { width: 100%; height: 21; background: #101b2c; padding: 0 2; border-top: solid #345c7c; }
    #status-row { width: 100%; height: 9; min-height: 9; }
    #statecard { width: 3fr; margin-right: 1; height: 100%; }
    #progresscard { width: 2fr; height: 100%; }
    #topbar { height: 3; margin-top: 1; }
    #brand { width: 1fr; height: 3; padding: 0 1; color: #f0f7ff; text-style: bold; background: #14263c; border-left: thick #43d7c3; content-align-vertical: middle; }
    #exit { width: 18; height: 3; margin-left: 1; background: #8c3046; color: #fff3f5; border: round #d35a6e; text-style: bold; }
    .card { background: #142237; border: round #2b425b; padding: 1 2; height: auto; }
    .eyebrow { color: #83b8db; text-style: bold; height: 1; }
    .hint { color: #8ea7bd; margin-top: 1; }
    #calibrationcard { width: 100%; height: auto; margin-top: 1; }
    #calibration-areas, #z-adjust, #calbuttons, #actionbuttons, #modebuttons { height: 3; width: 100%; margin-top: 1; }
    #calibration-areas Button, #z-adjust Button, #calbuttons Button, #modebuttons Button { width: 1fr; margin-right: 1; }
    #calinfo { height: auto; margin-top: 1; color: #bce8db; }
    #topline { height: auto; margin-top: 1; }
    #speedcard { width: 2fr; margin-right: 1; }
    #speed { width: 100%; margin-top: 1; background: #0c192b; border: tall #345c7c; color: #f3f8ff; }
    #actioncard { margin-top: 1; }
    #runbuttons { margin-top: 1; height: 4; }
    #runbuttons Button { width: 1fr; height: 4; margin-right: 1; text-style: bold; background: #1b5d92; color: #f7fcff; border: round #397eb5; }
    #runbuttons Button:disabled { background: #233349; color: #647e95; border: round #2c4055; }
    #finish { width: 2fr; margin-right: 1; background: #244e55; color: #e6fffa; border: round #3c8887; }
    #stop { width: 1fr; background: #8c3046; color: #fff3f5; border: round #d35a6e; text-style: bold; }
    #forcecard { height: 4; margin-bottom: 0; padding: 0 2; }
    #countcard { height: 6; margin-bottom: 0; padding: 0 2; }
    #pressure-table { height: 2; overflow-y: auto; }
    #forcevalue { color: #55f5c4; text-style: bold; height: 1; }
    #counts { color: #d6e8f7; text-style: bold; }
    #status { color: #e4f3ff; height: 3; min-height: 3; overflow-y: auto; }
    #warning { color: #ffab9d; height: auto; max-height: 2; }
    #progress { margin-top: 1; height: 1; color: #39d2ad; }
    #mode { margin-top: 1; color: #80cbbd; text-style: bold; }
    """

    def __init__(self):
        super().__init__()
        self.phase = "homing"
        self.homed = False
        self.serial_link = None
        self.serial_lock = threading.RLock()
        self.stop_requested = threading.Event()
        self.stop_send_failed = False
        self.pre_stop_phase = None
        self.area = None
        self.z = Z_MAX
        self.xyz = [0.0, 310.0, Z_MAX]
        self.status_message = "Starting..."
        self.speed = 1.0  # Default X brushing speed, m/min
        self.speed_confirmed = False
        self.speed_draft = ""
        self.repetitions = 1
        self.counts = {a: 0 for a in AREAS}
        self.calibrations = {area: CALIBRATION_Z for area in AREAS}
        self.pickup_eligible = False
        self.reset_counts_on_next_selection = False
        self.hx = None
        self.sensor_ready = False
        self.sensor_stop = threading.Event()
        self.marlin_xyz = None
        self.pressure_lock = threading.Lock()
        self.active_stroke = None
        self.recent_pressure_rows = []
        self.pressure_log_error = None
        # One new date/time-labelled CSV file for EACH application launch.
        # Random suffix avoids overwriting even if launched twice in one second.
        self.log_session = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S") + "_" + uuid4().hex[:6]
        self.pressure_filename = f"pressure_{self.log_session}.csv"

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main"):
            with Vertical(id="content"):
                with Horizontal(id="topbar"):
                    yield Static("  BRUSH CONTROL / ARTILLERY SIDEWINDER", id="brand")
                    yield Button("EXIT", id="exit")
                with Vertical(id="calibrationcard", classes="card"):
                    yield Static("CALIBRATION MODE — SET BRUSHING PRESSURE", classes="eyebrow")
                    yield Static("Startup: G28, pickup X-2 Y305 Z0, then tare. CAL AREA moves through standby X-2 Y155 Z80 to its XY and Z30.", classes="hint")
                    with Horizontal(id="calibration-areas"):
                        for n in (1, 2, 3):
                            yield Button(f"CAL AREA {n}", id=f"cal_run{n}")
                    with Horizontal(id="z-adjust"):
                        yield Button("RAISE Z 1 mm", id="z-up")
                        yield Button("LOWER Z 1 mm", id="z-down")
                    with Horizontal(id="calbuttons"):
                        yield Button("SAVE HEIGHT → STANDBY", id="cal-confirm")
                        yield Button("CANCEL → STANDBY", id="cal-cancel")
                    yield Static("All areas initially use Z30; adjust before contact brushing. STOP requests M410, then opens inspection confirmation. Hardware E-stop for emergencies.", id="calinfo")
                with Horizontal(id="topline"):
                    with Vertical(id="speedcard", classes="card"):
                        yield Static("BRUSH STROKE SPEED ONLY (X105 TO X205)", classes="eyebrow")
                        yield Input(placeholder="1 - 10 m/min (Enter to save)", id="speed", restrict=r"[0-9.]*")
                        yield Static("Only X brushing strokes use this speed. Default 1 m/min; Enter to save.", id="speed-saved", classes="hint")
                        yield Static("NUMBER OF RUNS (FORWARD / BACKWARD)", classes="eyebrow")
                        yield Input(value="1", id="repetitions", restrict=r"[0-9]*")
                        yield Static("1 = X105 to X205; 2 = X105 to X205 to X105; alternate each stroke.", classes="hint")
                with Vertical(id="actioncard", classes="card"):
                    yield Static("SELECT CLEANING AREA", classes="eyebrow")
                    with Horizontal(id="runbuttons"):
                        for n in (1, 2, 3):
                            yield Button(f"Area {n}", id=f"run{n}")
                    with Horizontal(id="actionbuttons"):
                        yield Button("FINISH / PICKUP", id="finish")
                        yield Button("STOP", id="stop")
        with Vertical(id="fixed-panel"):
            with Vertical(id="forcecard", classes="card"):
                yield Static("LIVE HX711 SENSOR READING", classes="eyebrow")
                yield Static("WAITING FOR STARTUP POSITION BEFORE TARE", id="forcevalue")
            with Vertical(id="countcard", classes="card"):
                yield Static("COMPLETED CYCLES THIS SESSION", classes="eyebrow")
                yield Static("", id="counts")
                # yield Static("PRESSURE LOG (latest strokes; N)", classes="eyebrow")
                # yield Static("No strokes logged yet", id="pressure-table")
            with Horizontal(id="status-row"):
                with Vertical(id="statecard", classes="card"):
                    yield Static("MACHINE STATUS", classes="eyebrow")
                    yield Static("", id="status")
                    # yield Static("REAL STARTUP, CALIBRATION, X BRUSH RUNS + PICKUP; STOP REQUESTS M410; HARDWARE E-STOP FOR EMERGENCIES", id="warning")
                with Vertical(id="progresscard", classes="card"):
                    yield Static("CYCLE PROGRESS", classes="eyebrow")
                    yield ProgressBar(total=100, id="progress", show_eta=False)
                    # yield Static("● REAL HOME + CAL + X STROKES + STANDBY", id="mode")
        yield Footer()

    def on_mount(self):
        try:
            self.initialize_pressure_log_files()
        except OSError as exc:
            self.pressure_log_error = f"Could not create session CSV file: {exc}"
        self.refresh_counts()
        self.status("Connecting to Marlin... Logs: " + str(PRESSURE_LOG_DIR / self.pressure_filename)
                    + (f" | LOG ERROR: {self.pressure_log_error}" if self.pressure_log_error else ""))
        self.controls()
        self.home_on_startup()

    def marlin_command(self, ser, cmd, timeout):
        """Collect a complete acknowledged reply; do not treat `ok` as motion proof."""
        if self.stop_requested.is_set():
            raise InterruptedError("Software stop requested; position untrusted")
        ser.write((cmd + "\n").encode("ascii"))
        ser.flush()
        deadline = time.monotonic() + timeout
        responses = []
        while time.monotonic() < deadline:
            if self.stop_requested.is_set():
                raise InterruptedError("Software stop requested; position untrusted")
            line = ser.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            responses.append(line)
            low = line.lower()
            if low.startswith(("error:", "!!", "echo:printer halted", "echo:kill")):
                raise RuntimeError(f"Marlin rejected {cmd}: {line}")
            if low == "ok" or low.startswith("ok "):
                return responses
        raise TimeoutError(f"No acknowledgment for {cmd}; last replies: {responses[-6:]}")

    @staticmethod
    def parse_m114(lines):
        """Use logical XYZ before Marlin's stepper `Count` fields."""
        number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
        for line in lines:
            line = re.split(r"\bCount\b", line, maxsplit=1, flags=re.I)[0]
            found = re.findall(r"(?<![A-Za-z])([XYZ])\s*:\s*(" + number + r")", line, flags=re.I)
            coords = {axis.upper(): float(value) for axis, value in found}
            if all(axis in coords and math.isfinite(coords[axis]) for axis in "XYZ"):
                return tuple(coords[axis] for axis in "XYZ")
        return None

    def read_marlin_xyz(self, ser):
        """Query Marlin's reported logical position after the preceding M400.

        Some Marlin builds defer M114 while processing a long Z move and emit
        `echo:busy: processing`. Keep waiting within a *fixed* overall limit;
        do not queue another M114 while the first is still outstanding.
        Never use a position reply without its command acknowledgment.
        """
        if self.stop_requested.is_set():
            raise InterruptedError("Software stop requested; position untrusted")
        ser.write(b"M114\n")
        ser.flush()
        lines = []
        acknowledged = False
        coords = None
        # Fixed deadline: busy messages do not extend it indefinitely.
        deadline = time.monotonic() + 120.0
        after_ok_deadline = None

        while time.monotonic() < deadline:
            if self.stop_requested.is_set():
                raise InterruptedError("Software stop requested; position untrusted")
            if after_ok_deadline is not None and time.monotonic() >= after_ok_deadline:
                break
            line = ser.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            lines.append(line)
            low = line.lower()
            if low.startswith(("error:", "!!", "echo:printer halted", "echo:kill")):
                raise RuntimeError(f"Marlin M114 error: {line}")
            # `echo:busy: processing` is a status, not a completed reply.
            if low == "ok" or low.startswith("ok "):
                acknowledged = True
                # A few Marlin versions print the position *after* `ok`.
                after_ok_deadline = time.monotonic() + 3.0
            parsed = self.parse_m114([line])
            if parsed is not None:
                coords = parsed
            if acknowledged and coords is not None:
                self.call_from_thread(self.show_marlin_xyz, coords)
                return coords

        if not acknowledged:
            raise TimeoutError(
                "M114 still not acknowledged after 120 s; "
                f"last responses: {lines[-8:]}. Position unknown; motion locked."
            )
        raise RuntimeError(
            "M114 acknowledged but no complete XYZ was received; "
            f"last responses: {lines[-8:]}. Position unknown; motion locked."
        )

    def show_marlin_xyz(self, coords):
        self.marlin_xyz = coords
        self.status(self.status_message)

    @work(thread=True, group="startup-home", exclusive=True)
    def home_on_startup(self):
        """Home, verify position, travel, verify target, then tare sensor."""
        try:
            with self.serial_lock:
                ser = serial.Serial(PORT, BAUD, timeout=0.25, write_timeout=2, exclusive=True)
                self.serial_link = ser
                time.sleep(3)
                ser.reset_input_buffer()
                self.call_from_thread(self.status, "Homing all axes...")
                self.marlin_command(ser, "G28", 180)
                self.marlin_command(ser, "M400", 240)
                self.call_from_thread(self.status, "Homing complete. Reading position...")
                self.read_marlin_xyz(ser)

                self.call_from_thread(self.status, "Moving to X-2 Y305 Z0...")
                self.marlin_command(ser, "G90", 15)
                self.marlin_command(ser, f"G1 X-2 Y305 Z0 F{TRAVEL_XY_FEED}", 180)
                self.marlin_command(ser, "M400", 240)
                self.call_from_thread(self.status, "Movement finished. Checking Marlin position...")
                actual = self.read_marlin_xyz(ser)
                target = (-2.0, 305.0, 0.0)
                if any(abs(actual[i] - target[i]) > 0.15 for i in range(3)):
                    raise RuntimeError(
                        "Target NOT reached. M114: "
                        f"X{actual[0]:.2f} Y{actual[1]:.2f} Z{actual[2]:.2f}; "
                        "requested X-2 Y305 Z0. Tare skipped. Inspect limits and motion."
                    )
                self.call_from_thread(self.status, "Target verified. Taring HX711; keep sensor unloaded...")
                self.call_from_thread(self.query_one("#forcevalue", Static).update,
                                      "TARING HX711 - KEEP UNLOADED")
                from hx711py.hx711 import HX711
                hx = HX711(6, 5)
                self.hx = hx
                hx.set_reading_format("MSB", "MSB")
                hx.set_reference_unit(HX711_REFERENCE_UNIT)
                hx.reset()
                hx.tare()
                self.sensor_ready = True
                self.call_from_thread(self.query_one("#forcevalue", Static).update,
                                      "HX711 TARED - reading sensor...")
                self.call_from_thread(self.homing_succeeded)
                self.call_from_thread(self.watch_force_sensor)
        except Exception as exc:
            self.sensor_ready = False
            if self.hx is not None:
                try:
                    self.hx.power_down()
                except Exception:
                    pass
            self.call_from_thread(self.query_one("#forcevalue", Static).update,
                                  "TARE NOT COMPLETE / SENSOR UNAVAILABLE")
            self.call_from_thread(self.homing_failed, str(exc))
        finally:
            # Keep the connection available for later CAL AREA positioning.
            # Close only on startup failure; never allow a second serial owner.
            if not self.sensor_ready:
                with self.serial_lock:
                    if self.serial_link is not None:
                        self.serial_link.close()
                        self.serial_link = None

    def verified_move(self, ser, label, command, target):
        """One motion command, M400 planner completion, then M114 report check."""
        self.call_from_thread(self.status, label)
        self.marlin_command(ser, command, 180)
        self.marlin_command(ser, "M400", 240)
        actual = self.read_marlin_xyz(ser)
        if any(abs(a - b) > POSITION_TOLERANCE for a, b in zip(actual, target)):
            raise RuntimeError(
                f"{label}: target NOT reached. Actual X{actual[0]:.2f} "
                f"Y{actual[1]:.2f} Z{actual[2]:.2f}; "
                f"expected X{target[0]:.2f} Y{target[1]:.2f} Z{target[2]:.2f}."
            )
        return actual

    @work(thread=True, group="calibration-position", exclusive=True)
    def position_for_calibration(self, area):
        """Physical calibration entry: Z clearance, standby XY, area XY, then Z30."""
        try:
            with self.serial_lock:
                ser = self.serial_link
                if ser is None or not ser.is_open or not self.sensor_ready:
                    raise RuntimeError("Serial connection or tared sensor unavailable")
                if self.marlin_xyz is None:
                    raise RuntimeError("Current position unknown")
                # Re-check the position before sending any motion.
                current = self.read_marlin_xyz(ser)
                if current[2] > STANDBY[2] + POSITION_TOLERANCE:
                    raise RuntimeError("Current Z above Z80; clearance route unverified")
                self.marlin_command(ser, "G90", 15)
                # Raise to Z80 before travelling in XY, avoiding XY motion at Z0.
                if abs(current[2] - STANDBY[2]) > POSITION_TOLERANCE:
                    current = self.verified_move(
                        ser, "Raising to safe Z80...", f"G1 Z{STANDBY[2]:g} F{TRAVEL_Z_FEED}",
                        (current[0], current[1], STANDBY[2]))
                # Z80 clearance must be verified before coordinated XY travel.
                current = self.verified_move(
                    ser, f"Standby: moving XY together to X{STANDBY[0]:g} Y{STANDBY[1]:g} at Z{STANDBY[2]:g}...",
                    f"G1 X{STANDBY[0]:g} Y{STANDBY[1]:g} F{TRAVEL_XY_FEED}", STANDBY)
                x, y = AREAS[area]
                current = self.verified_move(
                    ser, f"{area.upper()}: moving XY to X{x} Y{y} at Z80...",
                    f"G1 X{x} Y{y} F{TRAVEL_XY_FEED}", (float(x), float(y), STANDBY[2]))
                current = self.verified_move(
                    ser, f"{area.upper()}: lowering to Z30...",
                    f"G1 Z{CALIBRATION_Z:g} F{TRAVEL_Z_FEED}", (float(x), float(y), CALIBRATION_Z))
            self.call_from_thread(self.calibration_position_succeeded, area, current)
        except Exception as exc:
            self.call_from_thread(self.calibration_position_failed, str(exc))

    @work(thread=True, group="z-jog", exclusive=True)
    def jog_z(self, direction):
        """One verified 1-mm physical Z jog; never automatically retry motion."""
        try:
            with self.serial_lock:
                ser = self.serial_link
                if ser is None or not ser.is_open or not self.sensor_ready:
                    raise RuntimeError("Serial connection or sensor unavailable")
                current = self.read_marlin_xyz(ser)
                if not (Z_MIN_APPROACH - POSITION_TOLERANCE <= current[2] <= Z_MAX + POSITION_TOLERANCE):
                    raise RuntimeError(f"Current Z{current[2]:.2f} outside configured jog limits")
                target_z = current[2] + direction * Z_STEP
                # A limit is a normal condition, not a machine fault. Do not send
                # motion past the limit; leave the opposite direction available.
                if target_z < Z_MIN_APPROACH or target_z > Z_MAX:
                    self.call_from_thread(self.z_jog_limit_reached, current)
                    return
                self.marlin_command(ser, "G90", 15)
                actual = self.verified_move(
                    ser, f"Jogging Z to {target_z:.2f} mm...",
                    f"G1 Z{target_z:.3f} F{Z_JOG_FEED}",
                    (current[0], current[1], target_z))
            self.call_from_thread(self.z_jog_succeeded, actual)
        except Exception as exc:
            self.call_from_thread(self.z_jog_failed, str(exc))

    def z_jog_succeeded(self, actual):
        if self.stop_requested.is_set():
            return
        self.z = actual[2]
        self.xyz = list(actual)
        self.phase = "calibration_ready"
        self.status(f"Z jog complete: Z{self.z:.2f} mm (M114 verified).")
        self.controls()

    def z_jog_limit_reached(self, actual):
        if self.stop_requested.is_set():
            return
        self.z = actual[2]
        self.xyz = list(actual)
        self.phase = "calibration_ready"
        self.status(f"Z limit reached ({Z_MIN_APPROACH:g}-{Z_MAX:g} mm). Move in the opposite direction.")
        self.controls()

    def z_jog_failed(self, reason):
        if self.stop_requested.is_set():
            return
        self.phase = "calibration_failed"
        self.homed = False
        self.status(f"Z JOG FAILED: {reason}. Motion locked; inspect machine.")
        self.controls()

    @work(thread=True, group="return-to-standby", exclusive=True)
    def return_to_standby(self, area, save_height):
        """Physically retract Z, then move XY; commit saved height only on success."""
        try:
            with self.serial_lock:
                ser = self.serial_link
                if ser is None or not ser.is_open or not self.sensor_ready:
                    raise RuntimeError("Serial connection or sensor unavailable")
                current = self.read_marlin_xyz(ser)
                if not (Z_MIN_APPROACH - POSITION_TOLERANCE <= current[2] <= Z_MAX + POSITION_TOLERANCE):
                    raise RuntimeError(f"Z{current[2]:.2f} outside safe calibration range")
                area_x, area_y = AREAS[area]
                if abs(current[0] - area_x) > POSITION_TOLERANCE or abs(current[1] - area_y) > POSITION_TOLERANCE:
                    raise RuntimeError("Calibration XY position differs from selected area")
                saved_z = current[2]
                self.marlin_command(ser, "G90", 15)
                if abs(current[2] - STANDBY[2]) > POSITION_TOLERANCE:
                    current = self.verified_move(
                        ser, "Returning to standby: raising Z first...",
                        f"G1 Z{STANDBY[2]:g} F{TRAVEL_Z_FEED}",
                        (current[0], current[1], STANDBY[2]))
                current = self.verified_move(
                    ser, "Returning to standby: moving XY together...",
                    f"G1 X{STANDBY[0]:g} Y{STANDBY[1]:g} F{TRAVEL_XY_FEED}", STANDBY)
            self.call_from_thread(self.standby_return_succeeded, area, save_height, saved_z, current)
        except Exception as exc:
            self.call_from_thread(self.standby_return_failed, str(exc))

    def standby_return_succeeded(self, area, save_height, saved_z, actual):
        if self.stop_requested.is_set():
            return
        if save_height:
            self.calibrations[area] = saved_z
            self.pickup_eligible = True
            result = f"{area.upper()} saved Z{saved_z:.3f} mm (session memory)."
        else:
            result = f"{area.upper()} cancelled; previous saved height unchanged."
        self.area = None
        self.phase = "standby"
        self.z = actual[2]
        self.xyz = list(actual)
        self.query_one("#calinfo", Static).update(result + " Returned to real standby.")
        self.status(result + " Standby position verified.")
        self.controls()

    def standby_return_failed(self, reason):
        if self.stop_requested.is_set():
            return
        self.phase = "calibration_failed"
        self.homed = False
        self.status(f"RETURN TO STANDBY FAILED: {reason}. No height saved; motion locked.")
        self.query_one("#calinfo", Static).update(
            "Return movement failed; saved calibrations unchanged. Inspect machine before restarting.")
        self.controls()

    def calibration_position_succeeded(self, area, actual):
        if self.stop_requested.is_set():
            return
        self.phase = "calibration_ready"
        self.z = actual[2]
        self.xyz = list(actual)
        self.status(f"{area.upper()}: calibration position reached.")
        self.query_one("#calinfo", Static).update(
            f"{area.upper()} at X{actual[0]:.1f} Y{actual[1]:.1f} Z{actual[2]:.1f}. "
            "Z jog and return to standby are REAL."
        )
        self.controls()

    def calibration_position_failed(self, reason):
        if self.stop_requested.is_set():
            return
        self.phase = "calibration_failed"
        self.homed = False
        self.status(f"CALIBRATION MOVE FAILED: {reason} Inspect machine; position not trusted.")
        self.query_one("#calinfo", Static).update(
            "Calibration movement stopped. Other areas locked; restart only after inspection."
        )
        self.controls()

    def homing_succeeded(self):
        self.homed = True
        self.phase = "pickup"
        self.xyz = list(self.marlin_xyz)
        self.status("Ready. Target verified; sensor tared.")
        self.controls()

    def homing_failed(self, reason):
        self.homed = False
        self.phase = "home_failed"
        self.status(f"STARTUP FAILED: {reason}")
        self.controls()

    def status(self, message):
        self.status_message = message
        if self.marlin_xyz is None:
            position = "MARLIN XYZ: not yet available"
        else:
            x, y, z = self.marlin_xyz
            position = f"MARLIN XYZ: X{x:.3f}  Y{y:.3f}  Z{z:.3f} mm"
        self.query_one("#status", Static).update(f"{message}\n{position}")

    def refresh_counts(self):
        total = sum(self.counts.values())
        self.query_one("#counts", Static).update(
            f"RUN1: {self.counts['run1']}    |    RUN2: {self.counts['run2']}    |    RUN3: {self.counts['run3']}    |    TOTAL: {total}"
        )

    def controls(self):
        selecting = self.homed and self.sensor_ready and self.serial_link is not None and self.phase in ("pickup", "standby", "post_stop_ready")
        calibrating = self.homed and self.phase == "calibration_ready"
        for a in AREAS:
            self.query_one(f"#cal_{a}", Button).disabled = not selecting
            self.query_one(f"#{a}", Button).disabled = not (selecting and self.speed is not None and self.repetitions is not None )
        # Disable only the direction that would exceed the travel limit.
        # The opposite direction stays enabled when calibration is ready.
        self.query_one("#z-up", Button).disabled = not calibrating or self.z + Z_STEP > Z_MAX + POSITION_TOLERANCE
        self.query_one("#z-down", Button).disabled = not calibrating or self.z - Z_STEP < Z_MIN_APPROACH - POSITION_TOLERANCE
        self.query_one("#cal-confirm", Button).disabled = not calibrating
        self.query_one("#cal-cancel", Button).disabled = not calibrating
        self.query_one("#exit", Button).disabled = self.phase in ("homing", "calibration_moving", "z_jog_moving", "standby_moving", "cleaning_moving", "pickup_moving", "stopping", "recovering")
        self.query_one("#finish", Button).disabled = not (self.homed and self.phase == "standby" and self.pickup_eligible)
        stop = self.query_one("#stop", Button)
        stop.disabled = self.phase in ("homing", "stopping", "recovering", "home_failed", "pickup_failed", "calibration_failed") or self.serial_link is None
        stop.label = "POST INSPECTION" if self.phase == "inspection" else "SOFT STOP (M410)"

    def reset_counts_if_new_pickup_session(self):
        """Reset displayed stroke totals only on first new selection after pickup."""
        if self.reset_counts_on_next_selection:
            self.counts = {area: 0 for area in AREAS}
            self.refresh_counts()
            self.query_one("#progress", ProgressBar).update(progress=0)
            self.reset_counts_on_next_selection = False
            # Deliberately leave self.calibrations untouched.

    @work(thread=True, group="pickup-motion", exclusive=True)
    def return_to_pickup(self):
        """No G28: verify Z80, travel XY to pickup, then descend to Z0."""
        try:
            with self.serial_lock:
                ser = self.serial_link
                if ser is None or not ser.is_open or not self.sensor_ready:
                    raise RuntimeError("Serial connection or tared sensor unavailable")
                current = self.read_marlin_xyz(ser)
                if current[2] > Z_MAX + POSITION_TOLERANCE:
                    raise RuntimeError("Current Z above verified Z80 clearance")
                self.marlin_command(ser, "G90", 15)
                if abs(current[2] - Z_MAX) > POSITION_TOLERANCE:
                    current = self.verified_move(
                        ser, "Pickup: raising Z80 before XY movement...",
                        f"G1 Z{Z_MAX:g} F{TRAVEL_Z_FEED}", (current[0], current[1], Z_MAX))
                current = self.verified_move(
                    ser, "Pickup: moving XY to X-2 Y305 at Z80...",
                    f"G1 X{PICKUP[0]:g} Y{PICKUP[1]:g} F{TRAVEL_XY_FEED}",
                    (PICKUP[0], PICKUP[1], Z_MAX))
                current = self.verified_move(
                    ser, "Pickup: lowering Z to zero...",
                    f"G1 Z{PICKUP[2]:g} F{TRAVEL_Z_FEED}", PICKUP)
            self.call_from_thread(self.pickup_return_succeeded, current)
        except Exception as exc:
            self.call_from_thread(self.pickup_return_failed, str(exc))

    def pickup_return_succeeded(self, actual):
        if self.stop_requested.is_set():
            return
        self.xyz = list(actual)
        self.z = actual[2]
        self.phase = "pickup"
        self.pickup_eligible = False
        self.reset_counts_on_next_selection = True
        self.status("PICKUP reached X-2 Y305 Z0 (M114 reported, no G28). "
                    "Counts retained until next area selection; saved calibration heights retained.")
        self.controls()

    def pickup_return_failed(self, reason):
        if self.stop_requested.is_set():
            return
        self.phase = "pickup_failed"
        self.homed = False
        self.pickup_eligible = False
        self.status(f"PICKUP FAILED: {reason}. Motion locked; inspect machine.")
        self.controls()

    def initialize_pressure_log_files(self):
        """Create exactly one dated CSV file at launch with a unified schema."""
        PRESSURE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (PRESSURE_LOG_DIR / self.pressure_filename).open("x", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=LOG_FIELDS).writeheader()

    @staticmethod
    def append_pressure_csv(filename, fields, row):
        PRESSURE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = PRESSURE_LOG_DIR / filename
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if handle.tell() == 0:
                writer.writeheader()
            writer.writerow(row)

    def begin_pressure_stroke(self, run_id, area, index, total, speed, z, start_x, end_x):
        with self.pressure_lock:
            self.active_stroke = {
                "run_id": run_id, "area": area, "stroke": index,
                "planned_strokes": total, "speed_m_min": speed,
                "z_mm": z, "start_x_mm": start_x, "end_x_mm": end_x,
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "samples": [],
            }

    def record_pressure_sample(self, grams, newtons):
        with self.pressure_lock:
            stroke = self.active_stroke
            if stroke is None:
                return
            timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
            sample = {"timestamp": timestamp, "run_id": stroke["run_id"],
                      "area": stroke["area"], "stroke": stroke["stroke"],
                      "speed_m_min": stroke["speed_m_min"], "pressure_g": round(grams, 3), "force_n": round(newtons, 5)}
            stroke["samples"].append(sample)
            try:
                self.append_pressure_csv(self.pressure_filename, LOG_FIELDS, {"record_type": "sample", **sample})
            except OSError as exc:
                self.pressure_log_error = str(exc)

    def finish_pressure_stroke(self, status):
        with self.pressure_lock:
            stroke = self.active_stroke
            self.active_stroke = None
            if stroke is None:
                return
            samples = stroke.pop("samples")
            values = [s["force_n"] for s in samples]
            grams = [s["pressure_g"] for s in samples]
            row = {**stroke,
                "ended_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "status": status, "sample_count": len(samples),
                "avg_force_n": round(sum(values) / len(values), 4) if values else "",
                "peak_force_n": round(max(values), 4) if values else "",
                "min_force_n": round(min(values), 4) if values else "",
                "avg_pressure_g": round(sum(grams) / len(grams), 2) if grams else "",
                "peak_pressure_g": round(max(grams), 2) if grams else ""}
            try:
                self.append_pressure_csv(self.pressure_filename, LOG_FIELDS, {"record_type": "stroke_summary", **row})
            except OSError as exc:
                self.pressure_log_error = str(exc)
            # self.call_from_thread(self.show_pressure_row, row)

    def show_pressure_row(self, row):
        self.recent_pressure_rows.append(row)
        self.recent_pressure_rows = self.recent_pressure_rows[-5:]
        lines = ["Area   Stroke   Avg N    Peak N   Samples  Result"]
        for r in self.recent_pressure_rows:
            avg = str(r["avg_force_n"]) if r["avg_force_n"] != "" else "--"
            peak = str(r["peak_force_n"]) if r["peak_force_n"] != "" else "--"
            lines.append(f"{r['area']:<6} {r['stroke']:>3}/{r['planned_strokes']:<3}  {avg:>7}  {peak:>7}   {r['sample_count']:>5}   {r['status']}")
        self.query_one("#pressure-table", Static).update("\n".join(lines))

    @work(thread=True, group="cleaning-motion", exclusive=True)
    def run_cleaning(self, area, strokes, speed_m_min, target_z):
        """One X stroke per run; Z80 first, standby waypoint, then area."""
        completed = 0
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        try:
            with self.serial_lock:
                ser = self.serial_link
                if ser is None or not ser.is_open or not self.sensor_ready:
                    raise RuntimeError("Serial connection or tared sensor unavailable")
                if not (1 <= strokes <= 1000 and 1 <= speed_m_min <= 10):
                    raise RuntimeError("Invalid stroke count or speed")
                if not (Z_MIN_APPROACH <= target_z <= Z_MAX):
                    raise RuntimeError("Brushing Z outside approach limits")
                x0, y0 = AREAS[area]
                if x0 != 105:
                    raise RuntimeError("X starting point must be 105")
                current = self.read_marlin_xyz(ser)
                if current[2] > Z_MAX + POSITION_TOLERANCE:
                    raise RuntimeError("Z exceeds verified clearance height")
                self.marlin_command(ser, "G90", 15)
                if abs(current[2] - Z_MAX) > POSITION_TOLERANCE:
                    current = self.verified_move(ser, "Raising Z before XY travel...",
                        f"G1 Z{Z_MAX:g} F{TRAVEL_Z_FEED}", (current[0], current[1], Z_MAX))
                # Always visit verified standby before travelling to an area.
                current = self.verified_move(ser, "Moving to standby at Z80...",
                    f"G1 X{STANDBY[0]:g} Y{STANDBY[1]:g} F{TRAVEL_XY_FEED}", STANDBY)
                current = self.verified_move(ser, "Moving from standby to brushing start at Z80...",
                    f"G1 X{x0:g} Y{y0:g} F{TRAVEL_XY_FEED}", (float(x0), float(y0), Z_MAX))
                if abs(target_z - Z_MAX) > POSITION_TOLERANCE:
                    current = self.verified_move(ser, "Lowering to selected brushing height...",
                        f"G1 Z{target_z:.3f} F{TRAVEL_Z_FEED}", (float(x0), float(y0), target_z))
                # Marlin feedrates use mm/min. User input is m/min.
                feedrate = speed_m_min * 1000
                for index in range(strokes):
                    target_x = 210.0 if index % 2 == 0 else 105.0
                    self.begin_pressure_stroke(run_id, area, index + 1, strokes,
                                               speed_m_min, target_z, current[0], target_x)
                    try:
                        current = self.verified_move(ser,
                            f"{area.upper()} stroke {index + 1}/{strokes}: X{target_x:g}...",
                            f"G1 X{target_x:g} F{feedrate:.3f}",
                            (target_x, float(y0), target_z))
                    except BaseException:
                        self.finish_pressure_stroke("interrupted" if self.stop_requested.is_set() else "failed")
                        raise
                    self.finish_pressure_stroke("completed")
                    completed += 1
                    self.call_from_thread(self.cleaning_stroke_completed, area, completed, strokes)
                # Mandatory Z-first return to standby.
                if abs(current[2] - Z_MAX) > POSITION_TOLERANCE:
                    current = self.verified_move(ser, "Retracting Z before standby XY...",
                        f"G1 Z{Z_MAX:g} F{TRAVEL_Z_FEED}", (current[0], current[1], Z_MAX))
                current = self.verified_move(ser, "Returning XY to standby...",
                    f"G1 X{STANDBY[0]:g} Y{STANDBY[1]:g} F{TRAVEL_XY_FEED}", STANDBY)
            self.call_from_thread(self.cleaning_succeeded, area, completed, current)
        except Exception as exc:
            self.call_from_thread(self.cleaning_failed, area, completed, str(exc))
        finally:
            # Never attribute travel or the next run's samples to a stopped stroke.
            self.finish_pressure_stroke("interrupted" if self.stop_requested.is_set() else "failed")

    def cleaning_stroke_completed(self, area, completed, total):
        if self.stop_requested.is_set():
            return
        self.query_one("#progress", ProgressBar).update(progress=100 * completed / total)
        self.status(f"{area.upper()}: completed stroke {completed}/{total}; real motion.")

    def cleaning_succeeded(self, area, completed, actual):
        if self.stop_requested.is_set():
            return
        self.counts[area] += completed
        self.refresh_counts()
        self.xyz = list(actual)
        self.z = actual[2]
        self.phase = "standby"
        self.pickup_eligible = True
        suffix = f" PRESSURE LOG ERROR: {self.pressure_log_error}" if self.pressure_log_error else " Pressure CSV saved."
        self.status(f"{area.upper()}: {completed} real X strokes complete. Standby verified." + suffix)
        self.controls()

    def cleaning_failed(self, area, completed, reason):
        if self.stop_requested.is_set():
            return
        self.phase = "calibration_failed"
        self.homed = False
        self.status(f"CLEANING FAILED after {completed} verified strokes: {reason}. Motion locked; inspect machine.")
        self.controls()

    @work(thread=True, group="hx711-monitor", exclusive=True)
    def watch_force_sensor(self):
        """Read only; no motor or Marlin commands."""
        try:
            next_sample_at = time.monotonic() + PRESSURE_SAMPLE_INTERVAL_S
            while True:
                # Schedule on a monotonic clock instead of adding a 0.5 s
                # sleep after every HX711 read (which would slow sampling).
                if self.sensor_stop.wait(max(0.0, next_sample_at - time.monotonic())):
                    return
                if not self.sensor_ready or self.hx is None:
                    return
                grams = float(self.hx.get_weight(3))
                if not math.isfinite(grams):
                    raise ValueError("HX711 returned invalid reading")
                force_n = max(0.0, grams) * 0.00980665
                # Each sample is timestamped and appended to this launch's dated samples CSV
                # while a brushing stroke is active, including its user-selected speed.
                self.record_pressure_sample(grams, force_n)
                self.call_from_thread(self.query_one("#forcevalue", Static).update,
                                      f"{grams:.1f} g / {force_n:.2f} N (tared)")
                next_sample_at += PRESSURE_SAMPLE_INTERVAL_S
                # If the HX711 read took too long, skip missed slots rather
                # than producing fake readings or a burst of catch-up samples.
                now = time.monotonic()
                if next_sample_at <= now:
                    missed = int((now - next_sample_at) // PRESSURE_SAMPLE_INTERVAL_S) + 1
                    next_sample_at += missed * PRESSURE_SAMPLE_INTERVAL_S
        except Exception as exc:
            self.call_from_thread(self.query_one("#forcevalue", Static).update,
                                  f"HX711 READ ERROR: {exc}")
        finally:
            if self.hx is not None:
                try:
                    self.hx.power_down()
                except Exception:
                    pass

    def on_unmount(self):
        self.sensor_stop.set()
        with self.serial_lock:
            if self.serial_link is not None:
                self.serial_link.close()
                self.serial_link = None

    @on(Input.Changed, "#speed")
    def speed_changed(self, event: Input.Changed):
        # Reject out-of-range keystrokes immediately, restoring the last valid
        # draft. Empty, 0, and a trailing decimal point are permitted only as
        # editing prefixes; Enter still requires a complete value from 1 to 10.
        value = event.value
        valid_prefix = bool(re.fullmatch(r"(?:|0|[1-9](?:\.\d*)?|10(?:\.0*)?)", value))
        if not valid_prefix:
            event.input.value = self.speed_draft
            event.input.cursor_position = len(self.speed_draft)
            return
        if value == self.speed_draft:
            return
        self.speed_draft = value
        self.speed_confirmed = False
        self.query_one("#speed-saved", Static).update("Unsaved input. Runs use the last saved speed (default 1 m/min) until Enter.")
        self.controls()

    @on(Input.Submitted, "#speed")
    def speed_submitted(self, event: Input.Submitted):
        try:
            value = Decimal(event.value)
            if not value.is_finite() or not Decimal("1") <= value <= Decimal("10"):
                raise ValueError("Speed must be between 1 and 10 m/min")
        except (InvalidOperation, ValueError):
            self.speed_confirmed = False
            self.query_one("#speed-saved", Static).update("INVALID input. Last saved speed remains active (default 1 m/min).")
            self.status("X brush speed not saved: valid range is 1-10 m/min.")
        else:
            self.speed = float(value)
            self.speed_confirmed = True
            self.query_one("#speed-saved", Static).update(f"SAVED X speed: {self.speed:g} m/min ({self.speed * 1000 / 60:.2f} mm/s).")
            self.status(f"X brush movement speed saved: {self.speed:g} m/min (session only).")
        self.controls()

    @on(Input.Changed, "#repetitions")
    def repetitions_changed(self, event: Input.Changed):
        try:
            value = int(event.value)
            self.repetitions = value if 1 <= value <= 1000 else None
        except ValueError:
            self.repetitions = None
        self.controls()

    def request_soft_stop(self):
        """M410 is a firmware quickstop request, NOT a safety-rated stop.

        The current motion worker holds serial_lock, so do not wait for that lock
        here. Immediately prevent all later commands from the worker instead.
        """
        if self.phase == "inspection":
            if not self.stop_send_failed:
                self.push_screen(InspectionDialog(), self.inspection_dialog_result)
            return
        self.stop_requested.set()
        # Mark the active stroke as interrupted immediately; do not log travel as brushing.
        self.finish_pressure_stroke("interrupted")
        self.homed = False
        self.pickup_eligible = False
        self.area = None
        self.phase = "stopping"
        self.marlin_xyz = None
        self.controls()
        try:
            ser = self.serial_link
            if ser is None or not ser.is_open:
                raise RuntimeError("Serial link unavailable")
            # Marlin's emergency parser must be enabled for M410 to interrupt
            # an executing move. Otherwise it may be queued and NOT stop promptly.
            ser.write(b"M410\n")
            ser.flush()
            self.stop_send_failed = False
            self.phase = "inspection"
            self.status("M410 quickstop REQUESTED. Wait until motion is fully stopped. "
                        "Inspect the entire machine and clear all obstructions before POST INSPECTION. "
                        "Position is untrusted. Hardware E-stop if unsafe.")
        except Exception as exc:
            self.stop_send_failed = True
            self.phase = "inspection"
            self.status(f"STOP REQUEST FAILED: {exc}. Do not use software recovery; "
                        "use hardware safety controls and inspect the machine.")
        self.controls()
        if not self.stop_send_failed:
            self.push_screen(InspectionDialog(), self.inspection_dialog_result)

    def inspection_dialog_result(self, choice: str):
        if self.phase != "inspection" or self.stop_send_failed or choice != "home":
            return
        self.phase = "recovering"
        self.status("Operator confirmed inspection. Performing G28, then Y310 with X and Z held at home; keep clear.")
        self.controls()
        self.recover_after_inspection()

    @work(thread=True, group="inspection-home", exclusive=True)
    def recover_after_inspection(self):
        """Re-home after human inspection; never rely on interrupted XYZ."""
        try:
            # Wait for interrupted worker to release serial ownership.
            with self.serial_lock:
                ser = self.serial_link
                if ser is None or not ser.is_open:
                    raise RuntimeError("Serial link unavailable")
                # Previous worker has now finished or failed. Its planner
                # position cannot be trusted; only a real G28 re-establishes it.
                self.stop_requested.clear()
                ser.reset_input_buffer()
                self.marlin_command(ser, "G28", 180)
                self.marlin_command(ser, "M400", 240)
                actual = self.read_marlin_xyz(ser)
                # Keep X and Z at the G28-reported home coordinates.
                # Operator must confirm that the Y path is clear at home Z.
                self.marlin_command(ser, "G90", 15)
                home_x, _, home_z = actual
                actual = self.verified_move(
                    ser, "Post-stop: moving ONLY Y to 310 at homed Z...",
                    f"G1 Y{POST_STOP_Y:g} F{TRAVEL_XY_FEED}",
                    (home_x, POST_STOP_Y, home_z),
                )
                # After the Y310 move has completed and its reported position
                # has been checked, keep X/Y/Z steppers energized at rest.
                # M84 S0 prevents Marlin's idle stepper timeout.
                self.marlin_command(ser, "M17 X Y Z", 15)
                self.marlin_command(ser, "M84 S0", 15)
            self.call_from_thread(self.inspection_home_succeeded, actual)
        except Exception as exc:
            self.call_from_thread(self.inspection_home_failed, str(exc))

    def inspection_home_succeeded(self, actual):
        if not (0.0 - POSITION_TOLERANCE <= actual[2] <= Z_MAX + POSITION_TOLERANCE):
            self.inspection_home_failed("Reported homed Z is outside the configured safe motion range")
            return
        if not self.sensor_ready or self.serial_link is None or not self.serial_link.is_open:
            self.inspection_home_failed("Sensor or serial connection unavailable after recovery")
            return
        self.xyz = list(actual)
        self.z = actual[2]
        self.homed = True
        self.phase = "post_stop_ready"
        self.pickup_eligible = False
        self.reset_counts_on_next_selection = False
        self.calibrations = {area: CALIBRATION_Z for area in AREAS}
        self.speed = 1.0
        self.speed_confirmed = False
        self.speed_draft = ""
        self.repetitions = 1
        self.counts = {area: 0 for area in AREAS}
        self.area = None
        self.stop_send_failed = False
        self.query_one("#speed", Input).value = ""
        self.query_one("#repetitions", Input).value = "1"
        self.query_one("#speed-saved", Static).update("Only X brushing strokes use this speed. Default 1 m/min; Enter to save.")
        self.query_one("#calinfo", Static).update("All three calibration heights reset to default Z30. Calibrate before contact brushing.")
        self.query_one("#progress", ProgressBar).update(progress=0)
        self.refresh_counts()
        self.status("Post-inspection G28 completed, then ONLY Y moved to 310; X and Z remain at their homed coordinates (M114 logical position checked). X/Y/Z motors enabled with idle timeout disabled. All settings reset. Calibration and cleaning area buttons are available again; verify clearance before moving.")
        self.controls()

    def inspection_home_failed(self, reason):
        self.homed = False
        self.phase = "home_failed"
        self.status(f"POST-INSPECTION HOME FAILED: {reason}. Motion locked. "
                    "Use hardware safety controls and inspect the printer.")
        self.controls()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed):
        key = event.button.id
        if key == "exit":
            if self.phase in ("homing", "calibration_moving", "z_jog_moving", "standby_moving", "cleaning_moving", "pickup_moving"):
                self.status("Physical motion in progress; use hardware E-stop if unsafe.")
                return
            self.sensor_stop.set()
            with self.serial_lock:
                if self.serial_link is not None:
                    self.serial_link.close()
                    self.serial_link = None
            self.exit()
            return
        if key.startswith("cal_run") and self.phase in ("pickup", "standby", "post_stop_ready") and self.sensor_ready:
            self.reset_counts_if_new_pickup_session()
            self.area = key[4:]
            self.phase = "calibration_moving"
            self.pickup_eligible = False
            self.query_one("#calinfo", Static).update(
                f"{self.area.upper()} selected. REAL motion to standby, then XY, then Z30. Other areas locked."
            )
            self.controls()
            self.position_for_calibration(self.area)
        elif key in ("z-up", "z-down") and self.phase == "calibration_ready":
            direction = 1 if key == "z-up" else -1
            target_z = min(Z_MAX, self.z + Z_STEP) if direction > 0 else max(Z_MIN_APPROACH, self.z - Z_STEP)
            if abs(target_z - self.z) < POSITION_TOLERANCE:
                self.status(f"Z limit reached: allowed range {Z_MIN_APPROACH:g} to {Z_MAX:g} mm.")
            else:
                self.phase = "z_jog_moving"
                self.controls()
                self.jog_z(direction)
        elif key in ("cal-confirm", "cal-cancel") and self.phase == "calibration_ready":
            area = self.area
            save_height = key == "cal-confirm"
            self.phase = "standby_moving"
            self.query_one("#calinfo", Static).update(
                f"{area.upper()}: {'saving height after' if save_height else 'cancelling after'} verified return to standby..."
            )
            self.controls()
            self.return_to_standby(area, save_height)
        elif key in AREAS and self.phase in ("pickup", "standby", "post_stop_ready") and self.speed is not None and self.repetitions:
            self.reset_counts_if_new_pickup_session()
            target_z = self.calibrations.get(key, CALIBRATION_Z)
            self.status(f"{key.upper()}: selected. X speed {self.speed:g} m/min; Z{target_z:g} mm "
                        f"({'saved calibration' if key in self.calibrations else 'DEFAULT, uncalibrated'}). "
                        "Other area buttons locked; returning via standby first.")
            self.phase = "cleaning_moving"
            self.pickup_eligible = False
            self.query_one("#progress", ProgressBar).update(progress=0)
            self.controls()
            self.run_cleaning(key, self.repetitions, self.speed, target_z)
        elif key == "finish" and self.phase == "standby" and self.pickup_eligible:
            self.phase = "pickup_moving"
            self.controls()
            self.return_to_pickup()
        elif key == "stop":
            self.request_soft_stop()
        self.controls()


if __name__ == "__main__":
    Dashboard().run()
