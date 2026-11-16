import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import socket
import struct
import threading
import time
import cv2
import numpy as np
import os
import ctypes
import subprocess
import json
from datetime import datetime, timedelta
import base64
import sys
import shutil
import re


IS_WIN = sys.platform == "win32"
FROZEN = getattr(sys, "frozen", False)

if IS_WIN:
    BUNDLED_DIR = sys._MEIPASS if FROZEN else os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = os.path.dirname(sys.executable) if FROZEN else BUNDLED_DIR
    _bundled_ffmpeg = os.path.join(BUNDLED_DIR, "ffmpeg.exe")
    FFMPEG_PATH = (
        _bundled_ffmpeg if os.path.exists(_bundled_ffmpeg)
        else shutil.which("ffmpeg") or "ffmpeg"
    )
else:
    BUNDLED_DIR = sys._MEIPASS if FROZEN else os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = os.path.join(os.path.expanduser("~"), ".config", "CYD_Player")
    os.makedirs(BASE_DIR, exist_ok=True)
    FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"

# Linux fonts/widgets render a bit larger than Windows, so the side panel is wider.
SIDE_PANEL_W = 270 if IS_WIN else 340
SIDE_OFFSET = 505 if IS_WIN else 575

AUDIO_RATE = 16000      # Hz, mono unsigned 8-bit PCM (what the ESP32 expects)
AUDIO_CHUNK = 1024      # bytes per 'A' packet
AUDIO_PREFILL = 4096    # bytes sent at once so the ESP32 can start immediately
                        # (matches AUDIO_PREBUFFER_SIZE in the firmware)
AUDIO_START_WAIT = 3.0  # max seconds video waits for audio to begin

PORT = 3333
STATUS_PORT = 3335
FPS = 24
MAX_SMALL_RES_FPS = 30   # ceiling for "Auto" FPS
MAX_CUSTOM_FPS = 60      # ceiling for a typed FPS
PRESET_RESOLUTIONS = ("80 x 60", "120 x 90", "160 x 120", "240 x 180", "320 x 240")
MIN_RES = 16             # smallest width/height (JPEG block size)
MAX_RES_W = 320          # the CYD's screen
MAX_RES_H = 240
RECONNECT_INTERVAL = 3

VIDEO_EXTENSIONS = (
    ".mp4", ".avi", ".mkv", ".mov", ".wmv"
)

AUDIO_EXTENSIONS = (
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma"
)

MEDIA_EXTENSIONS = VIDEO_EXTENSIONS + AUDIO_EXTENSIONS

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def keep_awake():
    if IS_WIN:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )


def allow_sleep():
    if IS_WIN:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def extract_audio(filename, start_seconds=0.0):
    """Start ffmpeg and return the process; its stdout is raw u8 mono PCM.

    start_seconds seeks BEFORE decoding so pause/resume/skip stay in sync.
    """
    command = [FFMPEG_PATH, "-nostdin"]

    if start_seconds > 0.05:
        command += ["-ss", f"{start_seconds:.3f}"]

    command += [
        "-i", filename,
        "-vn", "-ac", "1", "-ar", str(AUDIO_RATE),
        "-f", "u8", "-acodec", "pcm_u8",
        "pipe:1",
    ]

    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0,
    )


def parse_resolution(text):
    """'200x150', '200 x 150', '200,150' -> (200, 150); None if invalid."""
    match = re.fullmatch(r"\s*(\d+)\s*[xX\u00d7*,]\s*(\d+)\s*", str(text))

    if not match:
        return None

    width, height = int(match.group(1)), int(match.group(2))

    if not (MIN_RES <= width <= MAX_RES_W and MIN_RES <= height <= MAX_RES_H):
        return None

    return width, height


def parse_fps(text):
    """'Auto' -> 0, '24' or '24 fps' -> 24; None if invalid."""
    text = str(text).strip().lower()

    if text in ("", "auto"):
        return 0

    match = re.fullmatch(r"(\d+)\s*(fps)?", text)

    if not match:
        return None

    value = int(match.group(1))

    return value if 1 <= value <= MAX_CUSTOM_FPS else None


class CYDVideoPlayer:
    def __init__(self, root):
        self.root = root
        self.root.title("ESP32 CYD Video Player")
        try:
            self.root.state("zoomed")
        except tk.TclError:
            try:
                self.root.attributes("-zoomed", True)
            except tk.TclError:
                self.root.geometry(
                    f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0"
                )
        self.root.resizable(True, True)
        self.mini_mode = False
        self.last_window_state = self.root.state()

        self.sock = None
        self.connected = False

        # ESP32 STATUS RECEIVING CONNECTION
        self.status_sock = None
        self.status_connected = False
        self.status_lock = threading.Lock()
        self.closing = False
        self.reconnecting = False
        self.worker_running = False

        self.filename = ""

        self.folder_files = []
        self.folder_index = -1
        self.folder_active = False

        # ONE FILE FOR ALL PLAYER DATA
        self.data_file = os.path.join(
            BASE_DIR,
            "player_data.json"
        )

        # COPY DEFAULT SETTINGS FROM EXE ON FIRST LAUNCH
        default_data = os.path.join(
            BUNDLED_DIR,
            "player_data.json"
        )

        if not os.path.exists(self.data_file) and os.path.exists(default_data):
            shutil.copy2(default_data, self.data_file)

        # DEFAULT DIAGNOSTIC SELECTIONS
        self.add_diagnostic_choices = {
            "sender": {
                "Target FPS", "Actual FPS", "Frames Sent",
                "Capture Time", "Resize Time", "Send Time",
                "Frame Size", "Video Rate"
            },
            "receiver": {
                "Wi-Fi", "RSSI", "Receiver FPS", "Frames Received",
                "Receive Time", "Decode Time", "Display Time",
                "Free Heap"
            }
        }

        self.player_data = {
            "recent_media": [],
            "recent_folders": [],
            "session": None,
            "diagnostics": {
                "sender": sorted(
                    self.add_diagnostic_choices["sender"]
                ),
                "receiver": sorted(
                    self.add_diagnostic_choices["receiver"]
                )
            }
        }

        self.recent_media = []
        self.recent_folders = []

        self.load_player_data()
                # ESP32 DISPLAY POSITION
        self.esp32_x = int(self.player_data.get("esp32_x", 0))
        self.esp32_y = int(self.player_data.get("esp32_y", 0))

        self.esp32_brightness = int(
            self.player_data.get("esp32_brightness", 100)
        )

        self.esp32_brightness = max(
            0,
            min(100, self.esp32_brightness)
        )
        self.target_ip = self.player_data.get(
            "esp32_ip",
            "192.168.1.100"
        )

        self.manual_disconnect = False

        self.output_width = 160
        self.output_height = 120
        self.playing = False
        self.stop_requested = False
        self.video_position = 0
        self.audio_position = 0.0   # seconds; only used for audio-only files

        self.audio_process = None
        self.audio_thread = None
        self.audio_stop = threading.Event()
        self.audio_ready = threading.Event()   # set once audio data is flowing
        self.media_is_audio = False

        self.lock = threading.RLock()
        self.send_lock = threading.Lock()

        # SENDER STATISTICS
        self.stats_lock = threading.Lock()

        self.sender_frames_total = 0
        self.sender_bytes_total = 0

        self.sender_capture_ms = 0
        self.sender_resize_ms = 0
        self.sender_send_ms = 0
        self.sender_frame_size = 0
        self.sender_target_fps = 0

        self.stats_previous_frames = 0
        self.stats_previous_bytes = 0
        self.stats_previous_time = time.monotonic()

        self.sender_actual_fps = 0
        self.sender_kbps = 0

        # ADVANCED SERIAL VIEWS
        self.advanced_panel = None
        self.advanced_stats = {}
        self.advanced_mode = None
        self.advanced_update_running = False

        # ADD DIAGNOSTICS WINDOW
        self.add_window = None

        # PLAYBACK MODIFIER STATE
        self.schedule_after_id = None
        self.resume_after_id = None

        self.stop_after_current = False

        self.reverse_playing = False
        self.reverse_speed = 1.0

        self.seek_request = None
        self.current_video_fps = FPS
        self.current_frame_count = 0
        self.playback_fps_limit = 0
        self.last_nonzero_volume = 100

        # SAVED MODIFIER SETTINGS
        self.modifier_settings = {
            "schedule": "",
            "schedule_delay": "",
            "auto_resume": "",
            "temporary_stop": "",
            "skip_interval": "10",
            "reverse_speed": "1x",
            "loop_mode": "No Loop",
            "volume": 100,
            "muted": False
        }

        saved_modifiers = self.player_data.get("modifiers", {})

        if isinstance(saved_modifiers, dict):
            self.modifier_settings.update(saved_modifiers)

        self.volume_level = int(
            self.modifier_settings.get("volume", 100)
        )

        self.muted = bool(
            self.modifier_settings.get("muted", False)
        )

        self.last_nonzero_volume = (
            self.volume_level if self.volume_level > 0 else 100
        )

        self.build_ui()
        self.root.bind(
            "<Configure>",
            self.on_root_configure,
            add="+"
        )

        self.restore_custom_resolution()
        self.load_modifier_controls()
        keep_awake()

        threading.Thread(
            target=self.status_connection_worker,
            daemon=True
        ).start()

        self.root.after(800, self.connect)

    # PLAYER DATA

    def load_player_data(self):
        try:
            with open(
                self.data_file,
                "r",
                encoding="utf-8"
            ) as file:
                saved_data = json.load(file)

            if isinstance(saved_data, dict):
                self.player_data.update(saved_data)

        except (OSError, json.JSONDecodeError):
            # Import the old separate files if no combined file exists.
            old_folder = os.path.dirname(
                os.path.abspath(__file__)
            )

            old_media_file = os.path.join(
                old_folder,
                "recent_media.json"
            )

            old_folders_file = os.path.join(
                old_folder,
                "recent_folders.json"
            )

            old_session_file = os.path.join(
                old_folder,
                "remember_session.txt"
            )

            try:
                with open(
                    old_media_file,
                    "r",
                    encoding="utf-8"
                ) as file:
                    old_media = json.load(file)

                if isinstance(old_media, list):
                    self.player_data["recent_media"] = old_media

            except (OSError, json.JSONDecodeError):
                pass

            try:
                with open(
                    old_folders_file,
                    "r",
                    encoding="utf-8"
                ) as file:
                    old_folders = json.load(file)

                if isinstance(old_folders, list):
                    self.player_data["recent_folders"] = old_folders

            except (OSError, json.JSONDecodeError):
                pass

            try:
                with open(
                    old_session_file,
                    "r",
                    encoding="utf-8"
                ) as file:
                    saved_lines = file.read().splitlines()

                if saved_lines and saved_lines[0].strip():
                    self.player_data["session"] = {
                        "filename": saved_lines[0].strip(),
                        "resolution": (
                            saved_lines[1].strip()
                            if len(saved_lines) > 1
                            else "160 x 120"
                        )
                    }

            except OSError:
                pass

        # VALIDATE RECENT MEDIA.
        saved_media = self.player_data.get(
            "recent_media",
            []
        )

        if isinstance(saved_media, list):
            self.recent_media = [
                entry for entry in saved_media
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("filename"), str)
                    and isinstance(entry.get("resolution"), str)
                )
            ][:10]

        # VALIDATE RECENT FOLDERS.
        saved_folders = self.player_data.get(
            "recent_folders",
            []
        )

        if isinstance(saved_folders, list):
            self.recent_folders = [
                folder for folder in saved_folders
                if (
                    isinstance(folder, str)
                    and os.path.isdir(folder)
                )
            ][:10]

        self.player_data["recent_media"] = self.recent_media
        self.player_data["recent_folders"] = self.recent_folders

        # LOAD SAVED DIAGNOSTIC SELECTIONS.
        # If the diagnostics section does not exist, use the defaults.
        saved_diagnostics = self.player_data.get(
            "diagnostics",
            {}
        )

        if isinstance(saved_diagnostics, dict):
            for mode in ("sender", "receiver"):
                saved_choices = saved_diagnostics.get(mode)

                if isinstance(saved_choices, list):
                    self.add_diagnostic_choices[mode] = {
                        name
                        for name in saved_choices
                        if isinstance(name, str)
                    }

        # MAKE SURE THE DIAGNOSTICS SECTION EXISTS IN THE JSON.
        self.player_data["diagnostics"] = {
            "sender": sorted(
                self.add_diagnostic_choices["sender"]
            ),
            "receiver": sorted(
                self.add_diagnostic_choices["receiver"]
            )
        }

        # SAVE THE MIGRATED OR CLEANED DATA.
        self.save_player_data()

    def save_player_data(self):
        self.player_data["recent_media"] = self.recent_media
        self.player_data["recent_folders"] = self.recent_folders

        # SAVE DIAGNOSTIC SELECTIONS
        self.player_data["diagnostics"] = {
            "sender": sorted(
                self.add_diagnostic_choices["sender"]
            ),
            "receiver": sorted(
                self.add_diagnostic_choices["receiver"]
            )
        }

        # SAVE PLAYBACK MODIFIERS
        if hasattr(self, "modifier_settings"):
            self.player_data["modifiers"] = self.modifier_settings

        try:
            with open(
                self.data_file,
                "w",
                encoding="utf-8"
            ) as file:
                json.dump(
                    self.player_data,
                    file,
                    indent=4
                )

        except OSError as error:
            print("PLAYER DATA SAVE ERROR:", error)


    # BOTTOM PLAYBACK BAR

    def format_playback_time(self, seconds):
        seconds = max(0, int(seconds))

        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        if hours:
            return f"{hours:02}:{minutes:02}:{secs:02}"

        return f"{minutes:02}:{secs:02}"

    def on_timeline_changed(self, event=None):
        if self.current_frame_count <= 0:
            return

        position = self.timeline_scale.get()

        frame = int(
            round(
                position / 1000
                * (self.current_frame_count - 1)
            )
        )

        with self.lock:
            self.seek_request = frame
            self.video_position = frame

        # Start playback if necessary so the pending seek is processed.
        if self.playing:
            self.restart_audio()
        else:
            self.play()

    def on_resolution_changed(self, event=None):
        size = parse_resolution(self.resolution_var.get())

        if size is None:
            # Not valid: put the last good value back and say what is allowed.
            self.resolution_var.set(
                f"{self.output_width} x {self.output_height}"
            )

            self.progress_label.config(
                text=(
                    f"Resolution must be WIDTH x HEIGHT, from "
                    f"{MIN_RES} x {MIN_RES} to {MAX_RES_W} x {MAX_RES_H}"
                )
            )
            return

        width, height = size
        self.resolution_var.set(f"{width} x {height}")

        with self.lock:
            self.output_width = width
            self.output_height = height

        self.remember_custom_resolution(width, height)
        self.report_rate_limits()

    def remember_custom_resolution(self, width, height):
        """A custom size is saved in player_data.json (and added to the RES
        dropdown) until a regular preset size is chosen, which erases it."""
        text = f"{width} x {height}"
        custom = None if text in PRESET_RESOLUTIONS else text

        self.resolution_box.config(
            values=PRESET_RESOLUTIONS + ((custom,) if custom else ())
        )

        if custom == self.player_data.get("custom_resolution"):
            return  # nothing changed, don't rewrite the file

        if custom:
            self.player_data["custom_resolution"] = custom
        else:
            self.player_data.pop("custom_resolution", None)

        self.save_player_data()

    def restore_custom_resolution(self):
        """At startup: bring back the saved custom size, if there is one."""
        saved = self.player_data.get("custom_resolution")

        if saved is None:
            return

        size = parse_resolution(saved)
        text = f"{size[0]} x {size[1]}" if size else None

        if size is None or text in PRESET_RESOLUTIONS:
            # Damaged or no longer custom: forget it.
            self.player_data.pop("custom_resolution", None)
            self.save_player_data()
            return

        self.resolution_var.set(text)

        with self.lock:
            self.output_width, self.output_height = size

        self.resolution_box.config(values=PRESET_RESOLUTIONS + (text,))

    def on_fps_changed(self, event=None):
        new_fps = parse_fps(self.fps_box.get())

        print(f"FPS SELECTOR EVENT: {self.fps_box.get()} -> {new_fps}")

        if new_fps is None:
            with self.lock:
                current = self.playback_fps_limit

            self.fps_box.set("Auto" if current == 0 else str(current))

            self.progress_label.config(
                text=f"FPS must be Auto or a whole number from 1 to {MAX_CUSTOM_FPS}"
            )
            return

        self.fps_box.set("Auto" if new_fps == 0 else str(new_fps))

        with self.lock:
            self.playback_fps_limit = new_fps

        self.report_rate_limits()

    def report_rate_limits(self):
        """Status line: resolution + FPS, and whether FPS is being limited."""
        with self.lock:
            width = self.output_width
            height = self.output_height
            limit = self.playback_fps_limit

        # Same estimate the sender uses: bigger frames mean fewer frames/second.
        ceiling = FPS * (MAX_RES_W * MAX_RES_H) / (width * height)

        text = f"Resolution: {width} x {height}  |  FPS: "

        source = self.current_video_fps if not self.media_is_audio else 0

        if limit == 0:
            text += "Auto"
        elif limit > ceiling:
            text += f"{limit} (limited to {ceiling:.0f} at this size)"
        elif source and limit > source:
            text += f"{limit} (this video is only {source:.0f} fps)"
        else:
            text += str(limit)

        self.progress_label.config(text=text)

    def show_input_hint(self, text):
        self.progress_label.config(text=text)

    # USER INTERFACE

    def build_ui(self):


        # RESOLUTION SETTING
        self.resolution_var = tk.StringVar(
            master=self.root,
            value="160 x 120"
        )

        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(
            menubar,
            tearoff=0
        )

        file_menu.add_command(
            label="Select Media...",
            command=self.select_video
        )

        file_menu.add_command(
            label="Add Playlist..."
        )

        file_menu.add_command(
            label="Select Folder...",
            command=self.select_folder
        )

        self.recent_media_menu = tk.Menu(
            file_menu,
            tearoff=0,
            postcommand=self.update_recent_media_menu
        )

        file_menu.add_cascade(
            label="Recent Media",
            menu=self.recent_media_menu
        )

        self.recent_folders_menu = tk.Menu(
            file_menu,
            tearoff=0,
            postcommand=self.update_recent_folders_menu
        )

        file_menu.add_cascade(
            label="Recent Folders",
            menu=self.recent_folders_menu
        )

        file_menu.add_command(
            label="Remember Session",
            command=self.remember_session
        )

        file_menu.add_command(
            label="Quit",
            command=self.close
        )

        menubar.add_cascade(
            label="File",
            menu=file_menu
        )

        serial_menu = tk.Menu(
            menubar,
            tearoff=0
        )

        serial_menu.add_command(
            label="Normal View",
            command=self.show_normal_view
        )

        serial_menu.add_separator()

        serial_menu.add_command(
            label="Sender Adv.",
            command=lambda: self.show_advanced_view("sender")
        )

        serial_menu.add_command(
            label="Receiver Adv.",
            command=lambda: self.show_advanced_view("receiver")
        )

        serial_menu.add_separator()

        serial_menu.add_command(
            label="Add...",
            command=self.show_add_options
        )

        menubar.add_cascade(
            label="Serial",
            menu=serial_menu
        )

        controls_menu = tk.Menu(
            menubar,
            tearoff=0
        )

        controls_menu.add_command(
            label="PLAY",
            command=self.play
        )

        controls_menu.add_command(
            label="PAUSE",
            command=self.pause
        )

        controls_menu.add_command(
            label="STOP",
            command=self.stop
        )

        controls_menu.add_separator()

        controls_menu.add_command(
            label="REWIND",
            command=self.skip_backward
        )

        controls_menu.add_command(
            label="FORWARD",
            command=self.skip_forward
        )

        controls_menu.add_command(
            label="REVERSE",
            command=self.toggle_reverse
        )

        controls_menu.add_separator()

        controls_menu.add_command(
            label="MUTE",
            command=self.toggle_mute
        )

        menubar.add_cascade(
            label="Controls",
            menu=controls_menu
        )


        playlist_menu = tk.Menu(
            menubar,
            tearoff=0
        )

        menubar.add_cascade(
            label="Playlist",
            menu=playlist_menu
        )


        upload_menu = tk.Menu(
            menubar,
            tearoff=0
        )

        menubar.add_cascade(
            label="Upload",
            menu=upload_menu
        )

        self.main_menu = menubar
        self.root.config(menu=self.main_menu)


        # RIGHT-SIDE PLAYBACK CONTROLS PANEL
        self.controls_panel = tk.Frame(
            self.root,
            bd=1,
            relief=tk.SOLID,
            padx=5,
            pady=4,
            width=SIDE_PANEL_W
        )
        self.controls_panel.pack(
            side=tk.RIGHT,
            fill=tk.BOTH,
            expand=False,
            padx=0,
            pady=0
        )
        self.controls_panel.pack_propagate(False)

        tk.Label(
            self.controls_panel,
            text="PLAYBACK CONTROLS",
            font=("Arial", 11, "bold"),
            anchor="w"
        ).pack(fill=tk.X, anchor="nw")

        # BOXED PLAYBACK CONTROL SECTIONS
        def control_section(parent, title, side=tk.TOP):
            section = tk.Frame(
                parent,
                bd=1,
                relief=tk.SOLID,
                padx=4,
                pady=3
            )
            section.pack(
                side=side,
                fill=tk.X,
                pady=3
            )

            tk.Label(
                section,
                text=title,
                font=("Arial", 9, "bold"),
                anchor="center"
            ).pack(fill=tk.X, pady=(0, 4))

            content = tk.Frame(section)
            content.pack(fill=tk.X)

            return content

        # CENTER VIDEO THUMBNAIL AREA
        self.thumbnail_frame = None
        self.thumbnail_image = None

        self.thumbnail_area = tk.Frame(
            self.root,
            bg="white"
        )

        self.thumbnail_area.place(
            x=225,
            y=70,
            relwidth=1,
            width=-SIDE_OFFSET,
            relheight=1,
            height=-220
        )

        self.thumbnail_canvas = tk.Canvas(
            self.thumbnail_area,
            bg="white",
            highlightthickness=0
        )

        self.thumbnail_canvas.pack(
            fill=tk.BOTH,
            expand=True
        )

        self.thumbnail_canvas.bind(
            "<Configure>",
            self.draw_thumbnail
        )

        self.draw_thumbnail()


        # PLAY
        play_section = control_section(self.controls_panel, "PLAY")

        tk.Button(
            play_section,
            text="PLAY",
            bg="#4CAF50",
            fg="white",
            width=10,
            command=self.play
        ).grid(row=0, column=0, padx=2, pady=2, sticky="ew")

        tk.Label(
            play_section,
            text="Delay (min):"
        ).grid(row=1, column=0, sticky="e")
        self.play_schedule_entry = tk.Entry(play_section, width=9)
        self.play_schedule_entry.grid(row=0, column=2, padx=2)

        tk.Label(play_section, text="Delay (sec):").grid(row=1, column=0, sticky="e")
        self.play_delay_entry = tk.Entry(play_section, width=8)
        self.play_delay_entry.grid(row=1, column=1, padx=2)

        tk.Button(
            play_section,
            text="SCHEDULE",
            width=9,
            command=self.schedule_playback
        ).grid(row=1, column=2, padx=2)

                # PAUSE
        pause_section = control_section(self.controls_panel, "PAUSE")

        tk.Button(
            pause_section,
            text="PAUSE",
            width=10,
            command=self.pause
        ).grid(row=0, column=0, padx=2, pady=2)

        tk.Label(
            pause_section,
            text="Auto-resume (min):"
        ).grid(row=0, column=1, sticky="e")

        self.pause_duration_entry = tk.Entry(
            pause_section,
            width=8
        )
        self.pause_duration_entry.grid(row=0, column=2, padx=2)


        # STOP
        stop_section = control_section(self.controls_panel, "STOP")

        tk.Button(
            stop_section,
            text="STOP",
            bg="#D9534F",
            fg="white",
            width=10,
            command=self.stop
        ).grid(row=0, column=0, padx=2, pady=2)

        tk.Label(
            stop_section,
            text="Temporary (min):"
        ).grid(row=0, column=1, sticky="e")
        self.temp_stop_entry = tk.Entry(stop_section, width=7)
        self.temp_stop_entry.grid(row=0, column=2, padx=2)

        tk.Button(
            stop_section,
            text="TEMP STOP",
            width=10,
            command=self.temporary_stop
        ).grid(row=1, column=0, padx=2, pady=2)

        tk.Button(
            stop_section,
            text="STOP AFTER CURRENT",
            width=20,
            command=self.stop_after_current_media
        ).grid(row=1, column=1, columnspan=2, padx=2)

        # SKIP
        skip_section = control_section(self.controls_panel, "SKIP")

        tk.Button(
            skip_section,
            text="<< REWIND",
            width=11,
            command=self.skip_backward
        ).grid(row=0, column=0, padx=2, pady=2)

        tk.Button(
            skip_section,
            text="FORWARD >>",
            width=11,
            command=self.skip_forward
        ).grid(row=0, column=1, padx=2, pady=2)

        tk.Label(
            skip_section,
            text="Skip interval (min):"
        ).grid(row=1, column=0, sticky="e")

        self.skip_interval_entry = tk.Entry(
            skip_section,
            width=8
        )
        self.skip_interval_entry.insert(0, "10")
        self.skip_interval_entry.grid(row=1, column=1)

        # REVERSE
        reverse_section = control_section(self.controls_panel, "REVERSE VIDEO")

        tk.Button(
            reverse_section,
            text="REVERSE",
            width=10,
            command=self.toggle_reverse
        ).grid(row=0, column=0, padx=2, pady=2)

        self.reverse_speed_box = ttk.Combobox(
            reverse_section,
            values=("0.25x", "0.5x", "1x", "2x", "4x"),
            state="readonly",
            width=9
        )
        self.reverse_speed_box.set("1x")
        self.reverse_speed_box.grid(row=0, column=1, padx=3)

        # LOOP
        loop_section = control_section(self.controls_panel, "LOOP MODE")

        self.loop_mode_box = ttk.Combobox(
            loop_section,
            values=(
                "No Loop",
                "Loop Current Media",
                "Loop Playlist"
            ),
            state="readonly"
        )
        self.loop_mode_box.set("No Loop")
        self.loop_mode_box.pack(fill=tk.X, padx=2, pady=2)

        # SELECT MEDIA
        tk.Button(
            self.controls_panel,
            text="SELECT MEDIA",
            command=self.select_video
        ).pack(
            fill=tk.X,
            pady=3
        )

        # SELECT PLAYLIST
        tk.Button(
            self.controls_panel,
            text="SELECT PLAYLIST",
            command=lambda: None
        ).pack(
            fill=tk.X,
            pady=3
        )

        # VOLUME - ALWAYS AT BOTTOM
        volume_section = control_section(
            self.controls_panel,
            "VOLUME",
            side=tk.BOTTOM
        )

        self.volume_slider = tk.Scale(
            volume_section,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            showvalue=True,
            length=210,
            command=self.on_volume_changed
        )
        self.volume_slider.set(self.volume_level)
        self.volume_slider.pack(
            fill=tk.X,
            padx=2,
            pady=(8, 2)
        )

        self.mute_button = tk.Button(
            volume_section,
            text="MUTE",
            width=7,
            command=self.toggle_mute
        )
        self.mute_button.pack(
            fill=tk.X,
            padx=2,
            pady=2
        )

        # TOP CONNECTION PANEL
        self.connection_panel = tk.Frame(
            self.root,
            bd=1,
            relief=tk.SOLID,
            padx=10,
            pady=7
        )

        self.connection_panel.place(
            x=220,
            y=0,
            relwidth=1,
            width=-(SIDE_OFFSET - 15),
            height=65
        )

        tk.Label(
            self.connection_panel,
            text="ESP32 IP:",
            font=("Arial", 10, "bold")
        ).pack(
            side=tk.LEFT,
            padx=(5, 5)
        )

        self.ip_entry = tk.Entry(
            self.connection_panel,
            width=20,
            font=("Arial", 10)
        )

        self.ip_entry.insert(
            0,
            self.target_ip
        )

        self.ip_entry.pack(
            side=tk.LEFT,
            padx=5
        )

        self.connect_button = tk.Button(
            self.connection_panel,
            text="CONNECT",
            width=12,
            command=self.connect
        )

        self.connect_button.pack(
            side=tk.LEFT,
            padx=5
        )

        # DISCONNECT BUTTON
        self.disconnect_button = tk.Button(
            self.connection_panel,
            text="DISCONNECT",
            width=12,
            command=lambda: self.disconnect()
        )

        self.disconnect_button.pack(
            side=tk.LEFT,
            padx=5
        )

        self.status = tk.Label(
            self.connection_panel,
            text="DISCONNECTED",
            fg="red",
            font=("Arial", 10, "bold")
        )

        self.status.pack(
            side=tk.RIGHT,
            padx=10
        )

        self.progress_label = tk.Label(
            self.connection_panel,
            text="Ready",
            fg="gray"
        )

        self.progress_label.pack(
            side=tk.RIGHT,
            padx=10
        )

        # KEEP FILE LABEL AVAILABLE FOR EXISTING CODE
        self.file_label = tk.Label(
            self.root,
            text="No media selected"
        )

        # SERIAL MONITOR BOX

        self.serial_monitor_panel = tk.Frame(
            self.root,
            bd=1,
            relief=tk.SOLID
        )

        self.serial_monitor_panel.place(
            x=0,
            y=0,
            width=220,
            height=45
        )

        tk.Label(
            self.serial_monitor_panel,
            text="SERIAL MONITOR",
            font=("Arial", 11, "bold"),
            anchor="center"
        ).pack(fill=tk.BOTH, expand=True)

        # RECEIVER BOX - TOP HALF

        self.receiver_panel = tk.Frame(
            self.root,
            bd=1,
            relief=tk.SOLID,
            padx=10,
            pady=8
        )

        self.receiver_panel.place(
            x=0,
            rely=0.5,
            y=22,
            width=220,
            relheight=0.5,
            height=-22
        )

        tk.Label(
            self.receiver_panel,
            text="RECEIVER",
            font=("Arial", 11, "bold"),
            anchor="center"
        ).pack(fill=tk.X, pady=(0, 10))

        self.receiver_stats = {}
        self.receiver_stat_rows = {}

        receiver_items = [
            ("Wi-Fi", "Wi-Fi", "--"),
            ("RSSI", "RSSI", "--"),
            ("FPS", "FPS", "--"),
            ("Frames Received", "Frames Received", "--"),
            ("Receive Time", "Receive Time", "--"),
            ("Decode Time", "Decode Time", "--"),
            ("Display Time", "Display Time", "--"),
            ("Free Heap", "Free Heap", "--"),
            ("Dropped Frames", "Dropped Frames", "--"),
            ("Wi-Fi IP", "Wi-Fi IP", "Not reported"),
            ("Largest Free Block", "Largest Free Block", "Not reported"),
            ("Packets Received", "Packets Received", "Not reported"),
            ("Packets Lost", "Packets Lost", "Not reported"),
            ("Uptime", "Uptime", "Not reported"),
            ("TCP Status", "TCP Status", "DISCONNECTED")
        ]

        for stat_name, display_name, initial_value in receiver_items:
            line = tk.Frame(self.receiver_panel)
            line.pack(fill=tk.X, pady=5)

            tk.Label(
                line,
                text=display_name + ":",
                anchor="w"
            ).pack(side=tk.LEFT)

            value_label = tk.Label(
                line,
                text=initial_value,
                font=("Arial", 9, "bold"),
                anchor="e"
            )

            value_label.pack(side=tk.RIGHT)

            self.receiver_stats[stat_name] = value_label
            self.receiver_stat_rows[stat_name] = line

        self.receiver_connection_label = tk.Label(
            self.receiver_panel,
            text="WAITING FOR ESP32",
            fg="gray",
            font=("Arial", 8, "bold")
        )

        self.receiver_connection_label.pack(
            side=tk.BOTTOM,
            pady=5
        )

        # SENDER BOX - BOTTOM HALF

        self.sender_panel = tk.Frame(
            self.root,
            bd=1,
            relief=tk.SOLID,
            padx=10,
            pady=8
        )

        self.sender_panel.place(
            x=0,
            y=45,
            width=220,
            relheight=0.5,
            height=-23
        )

        tk.Label(
            self.sender_panel,
            text="SENDER",
            font=("Arial", 11, "bold"),
            anchor="center"
        ).pack(fill=tk.X, pady=(0, 10))

        self.sender_stats = {}
        self.sender_stat_rows = {}

        sender_items = [
            ("Target FPS", "0"),
            ("Actual FPS", "0"),
            ("Frames Sent", "0"),
            ("Capture Time", "0 ms"),
            ("Resize Time", "0 ms"),
            ("Send Time", "0 ms"),
            ("Frame Size", "0 KB"),
            ("Video Rate", "0 KB/s"),
            ("Connection", "DISCONNECTED"),
            ("Destination IP", "--"),
            ("Resolution", "160 x 120"),
            ("Playback", "STOPPED"),
            ("Media Type", "--"),
            ("Audio", "STOPPED"),
            ("Video Worker", "IDLE")
        ]

        for label_text, initial_value in sender_items:
            line = tk.Frame(self.sender_panel)
            line.pack(fill=tk.X, pady=5)

            tk.Label(
                line,
                text=label_text + ":",
                anchor="w"
            ).pack(side=tk.LEFT)

            value_label = tk.Label(
                line,
                text=initial_value,
                font=("Arial", 9, "bold"),
                anchor="e"
            )

            value_label.pack(side=tk.RIGHT)

            self.sender_stats[label_text] = value_label
            self.sender_stat_rows[label_text] = line

        self.footer_label = tk.Label(
            self.root,
            text="Wireless ESP32 video and audio streaming",
            fg="gray"
        )
        self.footer_label.pack(
            side=tk.BOTTOM,
            pady=12
        )

        self.apply_normal_diagnostics()

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.close
        )

        # ==========================================
        # BOTTOM PLAYBACK BAR
        # ==========================================

        self.bottom_bar = tk.Frame(
            self.root,
            bd=1,
            relief=tk.SOLID,
            padx=8,
            pady=5,
            bg="#E8E8E8"
        )

        self.bottom_bar.place(
            x=220,
            rely=1.0,
            y=-150,
            relwidth=1.0,
            width=-(SIDE_OFFSET - 15),
            height=150
        )

        # VIDEO FILENAME
        self.bottom_filename = tk.Label(
            self.bottom_bar,
            text="No media selected",
            font=("Arial", 10, "bold"),
            anchor="w",
            bg="#E8E8E8"
        )
        self.bottom_filename.pack(fill=tk.X)

        # TIMELINE ROW
        timeline_row = tk.Frame(
            self.bottom_bar,
            bg="#E8E8E8"
        )
        timeline_row.pack(fill=tk.X, pady=(2, 0))

        self.timeline_time_label = tk.Label(
            timeline_row,
            text="00:00 / 00:00",
            width=15,
            anchor="w",
            bg="#E8E8E8"
        )
        self.timeline_time_label.pack(side=tk.LEFT, padx=(2, 5))

        self.timeline_scale = tk.Scale(
            timeline_row,
            from_=0,
            to=1000,
            orient=tk.HORIZONTAL,
            showvalue=False,
            resolution=1,
            bg="#E8E8E8",
            highlightthickness=0
        )
        self.timeline_scale.pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True
        )

        self.timeline_scale.bind(
            "<ButtonRelease-1>",
            self.on_timeline_changed
        )

        self.frame_counter_label = tk.Label(
            timeline_row,
            text="Frame 0 / 0",
            width=16,
            anchor="e",
            bg="#E8E8E8"
        )
        self.frame_counter_label.pack(
            side=tk.RIGHT,
            padx=5
        )

        # PLAYBACK BUTTON ROW
        playback_row = tk.Frame(
            self.bottom_bar,
            bg="#E8E8E8"
        )
        playback_row.pack(fill=tk.X, pady=3)

        self.bottom_play_button = tk.Button(
            playback_row,
            text="PLAY",
            width=8,
            bg="#4CAF50",
            fg="white",
            command=self.play
        )
        self.bottom_play_button.pack(
            side=tk.LEFT,
            padx=2
        )

        tk.Button(
            playback_row,
            text="PAUSE",
            width=8,
            command=self.pause
        ).pack(side=tk.LEFT, padx=2)

        tk.Button(
            playback_row,
            text="STOP",
            width=8,
            bg="#D9534F",
            fg="white",
            command=self.stop
        ).pack(side=tk.LEFT, padx=2)

        tk.Button(
            playback_row,
            text="<< REWIND",
            width=10,
            command=self.skip_backward
        ).pack(side=tk.LEFT, padx=2)

        tk.Button(
            playback_row,
            text="FORWARD >>",
            width=10,
            command=self.skip_forward
        ).pack(side=tk.LEFT, padx=2)

        tk.Button(
            playback_row,
            text="REVERSE",
            width=8,
            command=self.toggle_reverse
        ).pack(side=tk.LEFT, padx=2)

                # ESP32 DISPLAY POSITION
        tk.Label(
            playback_row,
            text="POSITION:",
            bg="#E8E8E8"
        ).pack(side=tk.LEFT, padx=(10, 2))

        self.position_entry = tk.Entry(
            playback_row,
            width=7,
            justify=tk.CENTER
        )
        self.position_entry.insert(
            0,
            f"{self.esp32_x}, {self.esp32_y}"
        )
        self.position_entry.pack(side=tk.LEFT, padx=2)

        self.position_entry.bind(
            "<Return>",
            self.apply_esp32_position
        )

        self.position_entry.bind(
            "<FocusIn>",
            lambda event: self.position_entry.select_range(0, tk.END)
        )

        # FPS SELECTOR
        tk.Label(
            playback_row,
            text="FPS:",
            bg="#E8E8E8"
        ).pack(side=tk.LEFT, padx=(10, 2))

        self.fps_box = ttk.Combobox(
            playback_row,
            values=("Auto", "10", "15", "20", "24", "30", "45", "60"),
            state="normal",   # editable: type any value
            width=6
        )
        self.fps_box.set("Auto")
        self.fps_box.pack(side=tk.LEFT, padx=2)
        for sequence in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
            self.fps_box.bind(sequence, self.on_fps_changed)

        self.fps_box.bind(
            "<FocusIn>",
            lambda e: self.show_input_hint(
                f"Type Auto or a number from 1 to {MAX_CUSTOM_FPS}, then press Enter"
            )
        )

        # RESOLUTION SELECTOR
        tk.Label(
            playback_row,
            text="RES:",
            bg="#E8E8E8"
        ).pack(side=tk.LEFT, padx=(8, 2))

        self.resolution_box = ttk.Combobox(
            playback_row,
            textvariable=self.resolution_var,
            values=PRESET_RESOLUTIONS,
            state="normal",   # editable: type any WIDTH x HEIGHT
            width=10
        )
        self.resolution_box.pack(side=tk.LEFT, padx=2)

        for sequence in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
            self.resolution_box.bind(sequence, self.on_resolution_changed)

        self.resolution_box.bind(
            "<FocusIn>",
            lambda e: self.show_input_hint(
                f"Type any size such as 200 x 150 "
                f"(max {MAX_RES_W} x {MAX_RES_H}), then press Enter"
            )
        )


        # BOTTOM BAR VOLUME
        volume_frame = tk.Frame(
            playback_row,
            bg="#E8E8E8"
        )
        volume_frame.pack(
            side=tk.LEFT,
            padx=(10, 2)
        )

        tk.Label(
            volume_frame,
            text="VOLUME:",
            bg="#E8E8E8"
        ).pack(
            anchor="center"
        )

        self.bottom_volume_slider = tk.Scale(
            volume_frame,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            showvalue=True,
            length=110,
            command=self.on_volume_changed,
            bg="#E8E8E8",
            highlightthickness=0
        )
        self.bottom_volume_slider.set(self.volume_level)
        self.bottom_volume_slider.pack()

        self.bottom_mute_button = tk.Button(
            playback_row,
            text="MUTE",
            width=7,
            command=self.toggle_mute
        )
        self.bottom_mute_button.pack(
            side=tk.LEFT,
            padx=2
        )

        # INITIALIZE BOTH VOLUME CONTROL AREAS
        self.volume_slider.set(self.volume_level)
        self.bottom_volume_slider.set(self.volume_level)

        self.update_volume_controls()

        # INITIAL FILENAME
        self.bottom_filename.config(
            text=os.path.basename(self.filename)
            if self.filename else "No media selected"
        )


        self.update_stats_display()

        self.file_label = self.bottom_filename


                # ESP32 TFT BRIGHTNESS
        tk.Label(
            playback_row,
            text="BRIGHTNESS:",
            bg="#E8E8E8"
        ).pack(side=tk.LEFT, padx=(10, 2))

        self.brightness_scale = tk.Scale(
            playback_row,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            length=110,
            resolution=1,
            showvalue=True,
            highlightthickness=0,
            bg="#E8E8E8",
            command=self.apply_esp32_brightness
        )

        self.brightness_scale.set(
            self.esp32_brightness
        )

        self.brightness_scale.pack(
            side=tk.LEFT,
            padx=2
        )


        # MINI PLAYER BUTTONS
        self.mini_controls_frame = tk.Frame(
            self.bottom_bar,
            bg="#E8E8E8"
        )

        self.mini_play_button = tk.Button(
            self.mini_controls_frame,
            text="PLAY",
            width=12,
            bg="#4CAF50",
            fg="white",
            command=self.play
        )
        self.mini_play_button.pack(
            fill=tk.X,
            pady=(0, 4)
        )

        self.mini_select_media_button = tk.Button(
            self.mini_controls_frame,
            text="SELECT MEDIA",
            width=12,
            command=self.select_video
        )
        self.mini_select_media_button.pack(
            fill=tk.X
        )

        self.playback_row = playback_row
        self.timeline_row = self.bottom_bar.winfo_children()[1]

        self.setup_wrapping_playback_row()

    # ==========================================
    # WRAPPING PLAYBACK ROW
    # ==========================================
    # Controls flow left to right. When one would cross the right edge of the
    # bottom bar it starts a new line under the PLAY button, and the bar grows
    # taller to fit. A label (FPS:, RES:) always stays with the widget after it.

    PLAYBACK_GAP = 4         # between controls
    PLAYBACK_GROUP_GAP = 10  # before a label/frame group (FPS, RES, VOLUME)
    PLAYBACK_ROW_GAP = 4     # between wrapped lines
    BOTTOM_BAR_MIN_HEIGHT = 150

    def setup_wrapping_playback_row(self):
        children = self.playback_row.winfo_children()

        self.playback_groups = []
        i = 0

        while i < len(children):
            if isinstance(children[i], tk.Label) and i + 1 < len(children):
                self.playback_groups.append([children[i], children[i + 1]])
                i += 2
            else:
                self.playback_groups.append([children[i]])
                i += 1

        for widget in children:
            widget.pack_forget()

        self._reflow_signature = None
        self._reflow_pending = False

        self.playback_row.bind(
            "<Configure>",
            lambda event: self.schedule_playback_reflow()
        )

        self.schedule_playback_reflow()

    def schedule_playback_reflow(self):
        if self._reflow_pending or self.closing:
            return

        self._reflow_pending = True
        self.root.after_idle(self.reflow_playback_row)

    def compute_playback_layout(self, available_width):
        """Return rows of (group, x). Pure layout math, no widget changes."""
        rows = [[]]
        x = 0

        for group in self.playback_groups:
            width = (
                sum(w.winfo_reqwidth() for w in group)
                + 2 * (len(group) - 1)
            )

            if x == 0:
                lead = 0
            elif isinstance(group[0], (tk.Label, tk.Frame)):
                lead = self.PLAYBACK_GROUP_GAP
            else:
                lead = self.PLAYBACK_GAP

            if x > 0 and x + lead + width > available_width:
                rows.append([])
                x = 0
                lead = 0

            rows[-1].append((group, x + lead))
            x += lead + width

        return rows

    def reflow_playback_row(self, force=False):
        self._reflow_pending = False

        if self.closing:
            return

        if force:
            self._reflow_signature = None

        available = self.playback_row.winfo_width()

        if available < 100:
            return  # not on screen yet; a <Configure> will call us again

        rows = self.compute_playback_layout(available)

        signature = (available, tuple(len(r) for r in rows))

        if signature == self._reflow_signature:
            return

        self._reflow_signature = signature

        y = 0

        for row in rows:
            row_height = max(
                w.winfo_reqheight() for group, _ in row for w in group
            )

            for group, x in row:
                for widget in group:
                    widget.place(
                        x=x,
                        y=y + (row_height - widget.winfo_reqheight()) // 2
                    )

                    x += widget.winfo_reqwidth() + 2

            y += row_height + self.PLAYBACK_ROW_GAP

        self.playback_row.config(height=y - self.PLAYBACK_ROW_GAP)

        # Grow (or shrink back) the bottom bar to fit the number of lines.
        self.root.update_idletasks()

        bar_height = max(
            self.BOTTOM_BAR_MIN_HEIGHT,
            self.bottom_bar.winfo_reqheight()
        )

        self.bottom_bar.place_configure(y=-bar_height, height=bar_height)
        self.thumbnail_area.place_configure(height=-(70 + bar_height))

    # SENDER STATISTICS

    # ==========================================
    # MINI PLAYER MODE
    # ==========================================

    def on_root_configure(self, event=None):
        if event is not None and event.widget is not self.root:
            return

        current_state = self.root.state()

        if current_state not in ("normal", "zoomed"):
            return

        if current_state == self.last_window_state:
            return

        self.last_window_state = current_state

        if current_state == "normal" and not self.mini_mode:
            self.root.after_idle(self.enter_mini_mode)

        elif current_state == "zoomed" and self.mini_mode:
            self.root.after_idle(self.leave_mini_mode)


    def enter_mini_mode(self):
        if self.mini_mode or self.closing:
            return

        self.mini_mode = True

        # HIDE RIGHT-SIDE CONTROLS
        self.controls_panel.pack_forget()

        # HIDE LEFT-SIDE PANELS
        self.serial_monitor_panel.place_forget()
        self.receiver_panel.place_forget()
        self.sender_panel.place_forget()

    def leave_mini_mode(self):
        if not self.mini_mode or self.closing:
            return

        self.mini_mode = False

        # HIDE MINI BUTTONS
        self.mini_controls_frame.place_forget()

        # HIDE MINI LAYOUT
        self.bottom_filename.place_forget()
        self.timeline_row.place_forget()
        self.playback_row.place_forget()

        # RESTORE ORIGINAL PLAYBACK LAYOUT
        self.bottom_filename.pack(fill=tk.X)

        self.timeline_row.pack(
            fill=tk.X,
            pady=(2, 0)
        )

        self.playback_row.pack(
            fill=tk.X,
            pady=3
        )

        # RESTORE PLAYBACK ROW SPACING
        self.playback_row.pack_configure(padx=0)

        # RESTORE MENU
        self.root.config(menu=self.main_menu)

        # RESTORE RIGHT CONTROLS
        self.controls_panel.pack(
            side=tk.RIGHT,
            fill=tk.BOTH,
            expand=False,
            padx=0,
            pady=0
        )

        # RESTORE CENTER THUMBNAIL
        self.thumbnail_area.place(
            x=225,
            y=70,
            relwidth=1,
            width=-505,
            relheight=1,
            height=-220
        )

        # RESTORE CONNECTION PANEL
        self.connection_panel.place(
            x=220,
            y=0,
            relwidth=1,
            width=-(SIDE_OFFSET - 15),
            height=65
        )

        # RESTORE LEFT PANELS
        self.serial_monitor_panel.place(
            x=0,
            y=0,
            width=220,
            height=45
        )

        self.receiver_panel.place(
            x=0,
            rely=0.5,
            y=22,
            width=220,
            relheight=0.5,
            height=-22
        )

        self.sender_panel.place(
            x=0,
            y=45,
            width=220,
            relheight=0.5,
            height=-23
        )

        # RESTORE DIAGNOSTICS
        if self.advanced_mode is not None:
            self.show_advanced_view(self.advanced_mode)
        else:
            self.show_normal_view()

        # RESTORE FOOTER
        self.footer_label.pack(
            side=tk.BOTTOM,
            pady=12
        )

        # RESTORE BOTTOM BAR
        self.bottom_bar.place(
            x=220,
            rely=1.0,
            y=-150,
            relwidth=1.0,
            width=-(SIDE_OFFSET - 15),
            height=150
        )

        self.root.minsize(0, 0)

        # RE-FIT THE (WRAPPING) PLAYBACK ROW
        self.root.after_idle(lambda: self.reflow_playback_row(force=True))

        # MAXIMIZE PLAYER
        self.root.state("zoomed")


    def update_stats_display(self):
        if self.closing:
            return

        now = time.monotonic()

        with self.stats_lock:
            elapsed = now - self.stats_previous_time

            if elapsed >= 0.25:
                frames_difference = (
                    self.sender_frames_total
                    - self.stats_previous_frames
                )

                bytes_difference = (
                    self.sender_bytes_total
                    - self.stats_previous_bytes
                )

                self.sender_actual_fps = (
                    frames_difference / elapsed
                )

                self.sender_kbps = (
                    bytes_difference / elapsed / 1024
                )

                self.stats_previous_frames = (
                    self.sender_frames_total
                )

                self.stats_previous_bytes = (
                    self.sender_bytes_total
                )

                self.stats_previous_time = now

            values = {
                "Target FPS": f"{self.sender_target_fps:.1f}",
                "Actual FPS": f"{self.sender_actual_fps:.1f}",
                "Frames Sent": str(self.sender_frames_total),
                "Capture Time": f"{self.sender_capture_ms:.1f} ms",
                "Resize Time": f"{self.sender_resize_ms:.1f} ms",
                "Send Time": f"{self.sender_send_ms:.1f} ms",
                "Frame Size": f"{self.sender_frame_size / 1024:.1f} KB",
                "Video Rate": f"{self.sender_kbps:.1f} KB/s"
            }

        for name, value in values.items():
            self.sender_stats[name].config(text=value)

        self.root.after(
            250,
            self.update_stats_display
        )


    def gui(self, callback):
        if not self.closing:
            try:
                self.root.after(0, callback)
            except tk.TclError:
                pass

    # SERIAL ADVANCED VIEWS

    def create_advanced_panel(self):
        if self.advanced_panel is None:
            self.advanced_panel = tk.Frame(
                self.root,
                bd=1,
                relief=tk.SOLID,
                padx=10,
                pady=8,
                bg=self.root.cget("bg")
            )

        self.advanced_panel.place(
            x=0,
            y=45,
            width=220,
            relheight=1,
            height=-45
        )

    def show_normal_view(self):
        self.advanced_mode = None
        self.advanced_stats = {}

        if self.advanced_panel:
            self.advanced_panel.place_forget()

        self.sender_panel.place(
            x=0,
            y=45,
            width=220,
            relheight=0.5,
            height=-23
        )

        self.receiver_panel.place(
            x=0,
            rely=0.5,
            y=22,
            width=220,
            relheight=0.5,
            height=-22
        )

        self.apply_normal_diagnostics()

    def apply_normal_diagnostics(self):
        # HIDE ALL SENDER ROWS
        for row in self.sender_stat_rows.values():
            row.pack_forget()

        # HIDE ALL RECEIVER ROWS
        for row in self.receiver_stat_rows.values():
            row.pack_forget()

        # SENDER DISPLAY ORDER
        sender_order = [
            "Connection",
            "Destination IP",
            "Target FPS",
            "Actual FPS",
            "Frames Sent",
            "Capture Time",
            "Resize Time",
            "Send Time",
            "Frame Size",
            "Video Rate",
            "Resolution",
            "Playback",
            "Media Type",
            "Audio",
            "Video Worker"
        ]

        # RECEIVER DISPLAY ORDER
        receiver_order = [
            ("Wi-Fi", "Wi-Fi"),
            ("RSSI", "RSSI"),
            ("Receiver FPS", "FPS"),
            ("Frames Received", "Frames Received"),
            ("Receive Time", "Receive Time"),
            ("Decode Time", "Decode Time"),
            ("Display Time", "Display Time"),
            ("Free Heap", "Free Heap"),
            ("Dropped Frames", "Dropped Frames"),
            ("Wi-Fi IP", "Wi-Fi IP"),
            ("Largest Free Block", "Largest Free Block"),
            ("Packets Received", "Packets Received"),
            ("Packets Lost", "Packets Lost"),
            ("Uptime", "Uptime"),
            ("TCP Status", "TCP Status")
        ]

        # NORMAL VIEW: MAXIMUM OF 8 SENDER DIAGNOSTICS
        shown_sender = 0

        for name in sender_order:
            if (
                name in self.add_diagnostic_choices["sender"]
                and shown_sender < 8
            ):
                self.sender_stat_rows[name].pack(
                    fill=tk.X,
                    pady=5
                )

                shown_sender += 1

        # NORMAL VIEW: MAXIMUM OF 8 RECEIVER DIAGNOSTICS
        shown_receiver = 0

        for selection_name, row_name in receiver_order:
            if (
                selection_name in self.add_diagnostic_choices["receiver"]
                and shown_receiver < 8
            ):
                self.receiver_stat_rows[row_name].pack(
                    fill=tk.X,
                    pady=5
                )

                shown_receiver += 1

    def show_advanced_view(self, mode):
        self.advanced_mode = mode
        self.advanced_stats = {}

        self.sender_panel.place_forget()
        self.receiver_panel.place_forget()

        self.create_advanced_panel()

        for widget in self.advanced_panel.winfo_children():
            widget.destroy()

        title = (
            "SENDER ADV. DIAG."
            if mode == "sender"
            else "RECEIVER ADV. DIAG."
        )

        tk.Label(
            self.advanced_panel,
            text=title,
            font=("Arial", 11, "bold"),
            anchor="center"
        ).pack(fill=tk.X, pady=(0, 10))

        if mode == "sender":
            items = [
                ("Connection", "connection"),
                ("Destination IP", "ip"),
                ("Target FPS", "target_fps"),
                ("Actual FPS", "actual_fps"),
                ("Frames Sent", "frames"),
                ("Capture Time", "capture"),
                ("Resize Time", "resize"),
                ("Send Time", "send"),
                ("Frame Size", "frame_size"),
                ("Video Rate", "video_rate"),
                ("Resolution", "resolution"),
                ("Playback", "playback"),
                ("Media Type", "media"),
                ("Audio", "audio"),
                ("Video Worker", "worker")
            ]

        else:
            items = [
                ("Wi-Fi", "wifi"),
                ("RSSI", "rssi"),
                ("Receiver FPS", "fps"),
                ("Frames Received", "frames"),
                ("Receive Time", "receive"),
                ("Decode Time", "decode"),
                ("Display Time", "display"),
                ("Free Heap", "heap"),
                ("Dropped Frames", "dropped"),
                ("Wi-Fi IP", "wifi_ip"),
                ("Largest Free Block", "largest_block"),
                ("Packets Received", "packets"),
                ("Packets Lost", "lost"),
                ("Uptime", "uptime"),
                ("TCP Status", "tcp")
            ]

        for label_text, key in items:
            if label_text not in self.add_diagnostic_choices[mode]:
                continue

            line = tk.Frame(
                self.advanced_panel,
                bg=self.root.cget("bg")
            )
            line.pack(fill=tk.X, pady=5)

            tk.Label(
                line,
                text=label_text + ":",
                anchor="w"
            ).pack(side=tk.LEFT)

            value = tk.Label(
                line,
                text="--",
                anchor="e",
                font=("Arial", 9, "bold")
            )

            value.pack(side=tk.RIGHT)

            self.advanced_stats[key] = value

        self.update_advanced_view()

        if not self.advanced_update_running:
            self.advanced_update_running = True
            self.root.after(500, self.refresh_advanced_view)

    def refresh_advanced_view(self):
        self.advanced_update_running = False

        if self.closing:
            return

        if self.advanced_mode in ("sender", "receiver"):
            self.update_advanced_view()

            self.advanced_update_running = True

            self.root.after(
                500,
                self.refresh_advanced_view
            )

    def update_advanced_view(self):
        if self.advanced_mode == "sender":
            values = {
                "connection": (
                    "CONNECTED" if self.connected
                    else "DISCONNECTED"
                ),

                "ip": self.ip_entry.get(),

                "target_fps": self.sender_stats[
                    "Target FPS"
                ].cget("text"),

                "actual_fps": self.sender_stats[
                    "Actual FPS"
                ].cget("text"),

                "frames": self.sender_stats[
                    "Frames Sent"
                ].cget("text"),

                "capture": self.sender_stats[
                    "Capture Time"
                ].cget("text"),

                "resize": self.sender_stats[
                    "Resize Time"
                ].cget("text"),

                "send": self.sender_stats[
                    "Send Time"
                ].cget("text"),

                "frame_size": self.sender_stats[
                    "Frame Size"
                ].cget("text"),

                "video_rate": self.sender_stats[
                    "Video Rate"
                ].cget("text"),

                "resolution": (
                    f"{self.output_width} x "
                    f"{self.output_height}"
                ),

                "playback": (
                    "PLAYING" if self.playing
                    else "STOPPED / PAUSED"
                ),

                "media": (
                    "AUDIO" if self.media_is_audio
                    else "VIDEO"
                ),

                "audio": (
                    "RUNNING"
                    if (
                        self.audio_thread
                        and self.audio_thread.is_alive()
                    )
                    else "STOPPED"
                ),

                "worker": (
                    "RUNNING" if self.worker_running
                    else "IDLE"
                )
            }

        elif self.advanced_mode == "receiver":
            values = {
                "wifi": self.receiver_stats["Wi-Fi"].cget("text"),
                "rssi": self.receiver_stats["RSSI"].cget("text"),
                "fps": self.receiver_stats["FPS"].cget("text"),
                "frames": self.receiver_stats["Frames Received"].cget("text"),
                "receive": self.receiver_stats["Receive Time"].cget("text"),
                "decode": self.receiver_stats["Decode Time"].cget("text"),
                "display": self.receiver_stats["Display Time"].cget("text"),
                "heap": self.receiver_stats["Free Heap"].cget("text"),
                "dropped": self.receiver_stats["Dropped Frames"].cget("text"),

                "wifi_ip": "Not reported",
                "largest_block": "Not reported",
                "packets": "Not reported",
                "lost": "Not reported",
                "uptime": "Not reported",
                "tcp": (
                    "CONNECTED" if self.connected
                    else "DISCONNECTED"
                )
            }

        else:
            return

        for key, value in values.items():
            if key in self.advanced_stats:
                self.advanced_stats[key].config(
                    text=value
                )

    # ADD DIAGNOSTICS WINDOW

    def show_add_options(self):
        if self.add_window is not None:
            if self.add_window.winfo_exists():
                self.add_window.lift()
                self.add_window.focus_force()
                return

        win = tk.Toplevel(self.root)
        self.add_window = win

        win.title("Add Diagnostics")
        win.geometry("720x600")
        win.minsize(600, 450)
        win.configure(bg="white")
        win.transient(self.root)

        # BLACK AND WHITE STYLE
        black = "#000000"
        white = "#FFFFFF"

        win.option_add("*Background", white)
        win.option_add("*Foreground", black)
        win.option_add("*Font", ("Arial", 10))
        win.option_add("*Button.Background", white)
        win.option_add("*Button.Foreground", black)
        win.option_add("*Checkbutton.Background", white)
        win.option_add("*Checkbutton.Foreground", black)

        # DIAGNOSTIC OPTIONS
        sender_items = [
            "Target FPS",
            "Actual FPS",
            "Frames Sent",
            "Capture Time",
            "Resize Time",
            "Send Time",
            "Frame Size",
            "Video Rate",
            "Connection",
            "Destination IP",
            "Resolution",
            "Playback",
            "Media Type",
            "Audio",
            "Video Worker"
        ]

        receiver_items = [
            "Wi-Fi",
            "RSSI",
            "Receiver FPS",
            "Frames Received",
            "Receive Time",
            "Decode Time",
            "Display Time",
            "Free Heap",
            "Dropped Frames",
            "Wi-Fi IP",
            "Largest Free Block",
            "Packets Received",
            "Packets Lost",
            "Uptime",
            "TCP Status"
        ]

        # WORKING COPY: CANCEL WILL NOT CHANGE SAVED CHOICES
        choices = {
            "sender": set(self.add_diagnostic_choices["sender"]),
            "receiver": set(self.add_diagnostic_choices["receiver"])
        }

        variables = {
            "sender": {},
            "receiver": {}
        }

        # TOP TAB BAR
        tab_bar = tk.Frame(
            win,
            bg=white,
            highlightbackground=black,
            highlightthickness=1
        )

        tab_bar.pack(
            fill=tk.X,
            padx=12,
            pady=(12, 0)
        )

        content = tk.Frame(win, bg=white)

        content.pack(
            fill=tk.BOTH,
            expand=True,
            padx=12,
            pady=12
        )

        tabs = {}

        def make_tab(name):
            frame = tk.Frame(content, bg=white)
            tabs[name] = frame
            return frame

        sender_tab = make_tab("Sender")
        receiver_tab = make_tab("Receiver")
        advanced_tab = make_tab("Advanced")

        def show_tab(name):
            for frame in tabs.values():
                frame.pack_forget()

            for button in tab_buttons.values():
                button.configure(
                    relief=tk.RAISED,
                    bg=white,
                    fg=black
                )

            tabs[name].pack(
                fill=tk.BOTH,
                expand=True
            )

            tab_buttons[name].configure(
                relief=tk.SUNKEN,
                bg=black,
                fg=white
            )

        tab_buttons = {}

        for name in ("Sender", "Receiver", "Advanced"):
            button = tk.Button(
                tab_bar,
                text=name,
                relief=tk.RAISED,
                bd=1,
                bg=white,
                fg=black,
                activebackground=black,
                activeforeground=white,
                command=lambda n=name: show_tab(n)
            )

            button.pack(
                side=tk.LEFT,
                fill=tk.X,
                expand=True,
                ipady=9
            )

            tab_buttons[name] = button

        # SCROLLABLE CHECKBOX LIST
        def make_scrollable(parent):
            outer = tk.Frame(parent, bg=white)
            outer.pack(
                fill=tk.BOTH,
                expand=True
            )

            canvas = tk.Canvas(
                outer,
                bg=white,
                highlightthickness=1,
                highlightbackground=black
            )

            scrollbar = tk.Scrollbar(
                outer,
                orient=tk.VERTICAL,
                command=canvas.yview
            )

            inner = tk.Frame(canvas, bg=white)

            window_id = canvas.create_window(
                (0, 0),
                window=inner,
                anchor="nw"
            )

            def update_scroll(event=None):
                canvas.configure(
                    scrollregion=canvas.bbox("all")
                )

            def resize_inner(event):
                canvas.itemconfigure(
                    window_id,
                    width=event.width
                )

            inner.bind("<Configure>", update_scroll)
            canvas.bind("<Configure>", resize_inner)

            canvas.configure(
                yscrollcommand=scrollbar.set
            )

            canvas.pack(
                side=tk.LEFT,
                fill=tk.BOTH,
                expand=True
            )

            scrollbar.pack(
                side=tk.RIGHT,
                fill=tk.Y
            )

            return inner

        # CHECKBOX CREATION
        MAX_DIAGNOSTICS = 8

        def enforce_diagnostic_limit(mode, label, var):
            selected_count = sum(
                1
                for selected_var in variables[mode].values()
                if selected_var.get()
            )

            if selected_count > MAX_DIAGNOSTICS:
                var.set(False)

                messagebox.showwarning(
                    "Diagnostic Limit",
                    "You can only select a maximum of 8 "
                    + mode.capitalize()
                    + " diagnostics for Normal View.",
                    parent=win
                )

        def add_checkbox(parent, mode, label, normal_limit=False):
            if label in variables[mode]:
                var = variables[mode][label]

            else:
                var = tk.BooleanVar(
                    master=win,
                    value=label in choices[mode]
                )

                variables[mode][label] = var

            command = None

            if normal_limit:
                command = lambda m=mode, l=label, v=var: (
                    enforce_diagnostic_limit(m, l, v)
                )

            checkbox = tk.Checkbutton(
                parent,
                text=label,
                variable=var,
                anchor="w",
                bg=white,
                fg=black,
                activebackground=white,
                activeforeground=black,
                selectcolor=white,
                relief=tk.FLAT,
                bd=0,
                command=command
            )

            checkbox.pack(
                fill=tk.X,
                anchor="w",
                padx=12,
                pady=3
            )

        # SENDER TAB
        sender_inner = make_scrollable(sender_tab)

        tk.Label(
            sender_inner,
            text="SENDER DIAGNOSTICS",
            font=("Arial", 12, "bold"),
            bg=white,
            fg=black
        ).pack(
            anchor="w",
            padx=12,
            pady=12
        )

        for item in sender_items:
            add_checkbox(
                sender_inner,
                "sender",
                item,
                True
            )

        # RECEIVER TAB
        receiver_inner = make_scrollable(receiver_tab)

        tk.Label(
            receiver_inner,
            text="RECEIVER DIAGNOSTICS",
            font=("Arial", 12, "bold"),
            bg=white,
            fg=black
        ).pack(
            anchor="w",
            padx=12,
            pady=12
        )

        for item in receiver_items:
            add_checkbox(
                receiver_inner,
                "receiver",
                item,
                True
            )

        # ADVANCED TAB
        advanced_inner = make_scrollable(advanced_tab)

        tk.Label(
            advanced_inner,
            text="SENDER ADVANCED",
            font=("Arial", 12, "bold"),
            bg=white,
            fg=black
        ).pack(
            anchor="w",
            padx=12,
            pady=(12, 5)
        )

        for item in sender_items:
            add_checkbox(
                advanced_inner,
                "sender",
                item
            )

        tk.Frame(
            advanced_inner,
            height=1,
            bg=black
        ).pack(
            fill=tk.X,
            padx=12,
            pady=12
        )

        tk.Label(
            advanced_inner,
            text="RECEIVER ADVANCED",
            font=("Arial", 12, "bold"),
            bg=white,
            fg=black
        ).pack(
            anchor="w",
            padx=12,
            pady=5
        )

        for item in receiver_items:
            add_checkbox(
                advanced_inner,
                "receiver",
                item
            )

        def cancel_window():
            self.add_window = None
            win.destroy()

        # BOTTOM BUTTON BAR
        bottom = tk.Frame(
            win,
            bg=white,
            highlightbackground=black,
            highlightthickness=1
        )

        bottom.pack(
            fill=tk.X,
            padx=12,
            pady=(0, 12)
        )

        # SAVE SELECTIONS TO PLAYER_DATA.JSON
        def save_choices():
            self.add_diagnostic_choices = {
                "sender": {
                    name
                    for name, var in variables["sender"].items()
                    if var.get()
                },
                "receiver": {
                    name
                    for name, var in variables["receiver"].items()
                    if var.get()
                }
            }

            # THIS WRITES THE DIAGNOSTICS TO player_data.json.
            self.save_player_data()

            # REFRESH WHICHEVER VIEW IS CURRENTLY OPEN.
            current_advanced_mode = self.advanced_mode

            if current_advanced_mode is None:
                self.apply_normal_diagnostics()

            else:
                self.show_advanced_view(
                    current_advanced_mode
                )

            self.add_window = None
            win.destroy()

        tk.Button(
            bottom,
            text="CANCEL",
            command=cancel_window,
            bg=white,
            fg=black,
            relief=tk.RAISED,
            bd=1,
            width=14
        ).pack(
            side=tk.RIGHT,
            padx=8,
            pady=8
        )

        tk.Button(
            bottom,
            text="SAVE",
            command=save_choices,
            bg=black,
            fg=white,
            activebackground=black,
            activeforeground=white,
            relief=tk.RAISED,
            bd=1,
            width=14
        ).pack(
            side=tk.RIGHT,
            padx=8,
            pady=8
        )

        win.protocol(
            "WM_DELETE_WINDOW",
            cancel_window
        )

        show_tab("Sender")


    # ESP32 STATUS RECEIVER

    def status_connection_worker(self):
        while not self.closing:

            # Do not connect the status socket when disconnected.
            if self.manual_disconnect or not self.connected:
                time.sleep(0.25)
                continue

            active_sock = None

            try:
                ip = self.target_ip

                active_sock = socket.socket(
                    socket.AF_INET,
                    socket.SOCK_STREAM
                )

                active_sock.settimeout(5)
                active_sock.connect((ip, STATUS_PORT))
                active_sock.settimeout(1)

                with self.status_lock:
                    if self.manual_disconnect or self.closing:
                        active_sock.close()
                        continue

                    self.status_sock = active_sock
                    self.status_connected = True

                self.gui(
                    lambda: self.receiver_connection_label.config(
                        text="RECEIVING FROM ESP32",
                        fg="green"
                    )
                )

                buffer = b""

                while (
                    not self.closing
                    and not self.manual_disconnect
                    and self.connected
                ):
                    try:
                        data = active_sock.recv(2048)

                        if not data:
                            break

                        buffer += data

                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)

                            line = line.decode(
                                "utf-8",
                                errors="ignore"
                            ).strip()

                            if line:
                                self.gui(
                                    lambda message=line:
                                        self.process_status_line(message)
                                )

                    except socket.timeout:
                        continue

            except OSError:
                pass

            finally:
                with self.status_lock:
                    if self.status_sock is active_sock:
                        self.status_sock = None

                    self.status_connected = False

                if active_sock:
                    try:
                        active_sock.close()
                    except OSError:
                        pass

                if not self.closing and not self.manual_disconnect:
                    self.gui(
                        lambda: self.receiver_connection_label.config(
                            text="WAITING FOR ESP32",
                            fg="orange"
                        )
                    )

                    time.sleep(RECONNECT_INTERVAL)

    def process_status_line(self, line):
        if "=" not in line:
            return

        key, value = line.split("=", 1)

        key = key.strip().upper()
        value = value.strip()

        display_names = {
            "WIFI": "Wi-Fi",
            "RSSI": "RSSI",
            "FPS": "FPS",
            "FRAMES_RECEIVED": "Frames Received",
            "RECEIVE_MS": "Receive Time",
            "DECODE_MS": "Decode Time",
            "DISPLAY_MS": "Display Time",
            "FREE_HEAP": "Free Heap",
            "DROPPED_FRAMES": "Dropped Frames"
        }

        if key not in display_names:
            return

        label_name = display_names[key]

        if key == "RSSI":
            display_value = value + " dBm"

        elif key in (
            "RECEIVE_MS",
            "DECODE_MS",
            "DISPLAY_MS"
        ):
            display_value = value + " ms"

        elif key == "FREE_HEAP":
            try:
                display_value = f"{int(value) / 1024:.1f} KB"
            except ValueError:
                display_value = value

        else:
            display_value = value

        if label_name in self.receiver_stats:
            self.receiver_stats[label_name].config(
                text=display_value
            )

    # CONNECTION

    def connect(self):
        with self.lock:
            if self.connected or self.reconnecting or self.closing:
                return

            self.manual_disconnect = False

        ip = self.ip_entry.get().strip()

        if not ip:
            messagebox.showwarning(
                "Missing IP",
                "Enter the ESP32 IP address first."
            )
            return

        self.target_ip = ip
        self.player_data["esp32_ip"] = ip
        self.save_player_data()

        self.connect_button.config(
            text="CONNECTING",
            state=tk.DISABLED
        )

        self.status.config(
            text="CONNECTING...",
            fg="orange"
        )

        threading.Thread(
            target=self.connection_worker,
            daemon=True
        ).start()

    def disconnect(self):
        # Prevent automatic reconnection after a manual disconnect.
        with self.lock:
            self.manual_disconnect = True
            self.connected = False
            self.playing = False
            self.stop_requested = True
            self.reset_position()

            active_sock = self.sock
            self.sock = None

        self.stop_audio()

        # Close the status connection too.
        with self.status_lock:
            active_status_sock = self.status_sock
            self.status_sock = None
            self.status_connected = False

        for active in (active_sock, active_status_sock):
            if active:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

                try:
                    active.close()
                except OSError:
                    pass

        self.status.config(
            text="DISCONNECTED",
            fg="red"
        )

        self.connect_button.config(
            text="CONNECT",
            state=tk.NORMAL
        )

        self.receiver_connection_label.config(
            text="DISCONNECTED",
            fg="red"
        )

        self.progress_label.config(
            text="Disconnected from ESP32"
        )

        if "Connection" in self.sender_stats:
            self.sender_stats["Connection"].config(
                text="DISCONNECTED"
            )

        if "TCP Status" in self.receiver_stats:
            self.receiver_stats["TCP Status"].config(
                text="DISCONNECTED"
            )

    def connection_worker(self):
        with self.lock:
            if (
                self.closing
                or self.reconnecting
                or self.manual_disconnect
            ):
                return

            self.reconnecting = True

        attempts = 0

        try:
            while not self.closing and not self.manual_disconnect:
                with self.lock:
                    if self.connected:
                        break

                attempts += 1

                self.gui(
                    lambda n=attempts: self.status.config(
                        text=f"CONNECTING... ATTEMPT {n}",
                        fg="orange"
                    )
                )

                new_sock = None

                try:
                    ip = self.target_ip

                    new_sock = socket.socket(
                        socket.AF_INET,
                        socket.SOCK_STREAM
                    )

                    new_sock.settimeout(5)
                    new_sock.connect((ip, PORT))
                    new_sock.settimeout(30)

                    new_sock.setsockopt(
                        socket.IPPROTO_TCP,
                        socket.TCP_NODELAY,
                        1
                    )

                    with self.lock:
                        if (
                            self.closing
                            or self.manual_disconnect
                        ):
                            new_sock.close()
                            break

                        self.sock = new_sock
                        self.connected = True

                    # SEND SAVED TFT BRIGHTNESS ON CONNECT
                    brightness_packet = (
                        b"B" + bytes([self.esp32_brightness])
                    )

                    with self.send_lock:
                        new_sock.sendall(brightness_packet)

                    print(
                        f"ESP32 BRIGHTNESS SENT: "
                        f"{self.esp32_brightness}%"
                    )


            # SEND SAVED ESP32 DISPLAY POSITION ON CONNECT
                    position_packet = (
                        b"O" +
                        struct.pack(
                            "!hh",
                            self.esp32_x,
                            self.esp32_y
                        )
                    )

                    with self.send_lock:
                        new_sock.sendall(position_packet)

                    print(
                        f"ESP32 POSITION SENT: "
                        f"X={self.esp32_x}, Y={self.esp32_y}"
                    )


                    self.gui(
                        lambda: self.status.config(
                            text="CONNECTED TO ESP32",
                            fg="green"
                        )
                    )

                    self.gui(
                        lambda: self.connect_button.config(
                            text="CONNECTED",
                            state=tk.DISABLED
                        )
                    )

                    self.gui(
                        lambda: self.sender_stats["Connection"].config(
                            text="CONNECTED"
                        )
                    )

                    self.gui(
                        lambda: self.sender_stats["Destination IP"].config(
                            text=ip
                        )
                    )

                    self.gui(
                        lambda: self.receiver_stats["TCP Status"].config(
                            text="CONNECTED"
                        )
                    )

                    threading.Thread(
                        target=self.receive_commands,
                        args=(new_sock,),
                        daemon=True
                    ).start()

                    with self.lock:
                        should_start = (
                            self.playing
                            and bool(self.filename)
                        )

                        is_audio_only = self.media_is_audio

                    if should_start:
                        old_audio_thread = self.audio_thread

                        if old_audio_thread and old_audio_thread.is_alive():
                            old_audio_thread.join(timeout=2.0)

                        if not (
                            old_audio_thread
                            and old_audio_thread.is_alive()
                        ):
                            self.start_audio()

                        if not is_audio_only:
                            self.start_streaming()

                    break

                except OSError:
                    if new_sock:
                        try:
                            new_sock.close()
                        except OSError:
                            pass

                    if self.manual_disconnect or self.closing:
                        break

                    self.gui(
                        lambda: self.status.config(
                            text="CONNECTION LOST - RETRYING",
                            fg="orange"
                        )
                    )

                    time.sleep(RECONNECT_INTERVAL)

        finally:
            with self.lock:
                self.reconnecting = False

            if not self.connected and not self.closing:
                if self.manual_disconnect:
                    self.gui(
                        lambda: self.connect_button.config(
                            text="CONNECT",
                            state=tk.NORMAL
                        )
                    )
                else:
                    self.gui(
                        lambda: self.connect_button.config(
                            text="RETRYING",
                            state=tk.DISABLED
                        )
                    )

    def schedule_reconnect(self):
        with self.lock:
            if (
                self.closing
                or self.reconnecting
                or self.manual_disconnect
            ):
                return

        self.gui(
            lambda: self.status.config(
                text="RECONNECTING...",
                fg="orange"
            )
        )

        threading.Thread(
            target=self.connection_worker,
            daemon=True
        ).start()

    def connection_lost(self, failed_sock):
        with self.lock:
            if self.sock is not failed_sock:
                return

            self.connected = False
            self.sock = None

            try:
                failed_sock.close()
            except OSError:
                pass

            should_reconnect = (
                not self.manual_disconnect
                and not self.closing
            )

        self.stop_audio()

        self.gui(
            lambda: self.status.config(
                text="CONNECTION LOST",
                fg="red"
            )
        )

        self.gui(
            lambda: self.connect_button.config(
                text="RECONNECTING",
                state=tk.DISABLED
            )
        )

        self.gui(
            lambda: self.sender_stats["Connection"].config(
                text="DISCONNECTED"
            )
        )

        self.gui(
            lambda: self.receiver_stats["TCP Status"].config(
                text="DISCONNECTED"
            )
        )

        if should_reconnect:
            self.schedule_reconnect()

    # CYD BUTTONS

    def receive_commands(self, active_sock):
        while not self.closing:
            with self.lock:
                if self.sock is not active_sock:
                    return

            try:
                command = active_sock.recv(1)

                if not command:
                    break

                if command == b"P":
                    with self.lock:
                        self.playing = True

                    self.gui(
                        lambda: self.progress_label.config(
                            text="Playing (CYD button)"
                        )
                    )

                elif command == b"p":
                    with self.lock:
                        self.playing = False

                    self.audio_stop.set()

                    self.gui(
                        lambda: self.progress_label.config(
                            text="Paused (CYD button)"
                        )
                    )

                elif command == b"S":
                    with self.lock:
                        self.playing = False
                        self.stop_requested = True
                        self.reset_position()

                    self.audio_stop.set()

                    self.gui(
                        lambda: self.progress_label.config(
                            text="Stopped (CYD button)"
                        )
                    )

            except socket.timeout:
                continue

            except OSError:
                break

        self.connection_lost(active_sock)


    def update_thumbnail(self, filename):
        # Audio files do not have video thumbnails.
        if filename.lower().endswith(AUDIO_EXTENSIONS):
            self.thumbnail_frame = None
            self.draw_thumbnail()
            return

        capture = cv2.VideoCapture(filename)

        if not capture.isOpened():
            self.thumbnail_frame = None
            capture.release()
            self.draw_thumbnail()
            return

        success, frame = capture.read()
        capture.release()

        if success:
            self.thumbnail_frame = frame
        else:
            self.thumbnail_frame = None

        self.draw_thumbnail()

    def draw_thumbnail(self, event=None):
        if not hasattr(self, "thumbnail_canvas"):
            return

        canvas = self.thumbnail_canvas
        width = canvas.winfo_width()
        height = canvas.winfo_height()

        if width < 2 or height < 2:
            return

        canvas.delete("all")
        canvas.configure(bg="#202020")

        if self.thumbnail_frame is None:
            return

        frame = self.thumbnail_frame
        frame_height, frame_width = frame.shape[:2]

        # Use most of the available center area.
        max_width = int(width * 0.92)
        max_height = int(height * 0.88)

        scale = min(
            max_width / frame_width,
            max_height / frame_height
        )

        new_width = max(1, int(frame_width * scale))
        new_height = max(1, int(frame_height * scale))

        resized = cv2.resize(
            frame,
            (new_width, new_height),
            interpolation=cv2.INTER_AREA
        )

        resized = cv2.cvtColor(
            resized,
            cv2.COLOR_BGR2RGB
        )

        success, encoded = cv2.imencode(".png", resized)

        if success:
            self.thumbnail_image = tk.PhotoImage(
                data=base64.b64encode(
                    encoded.tobytes()
                ).decode("ascii")
            )

            canvas.create_image(
                width // 2,
                height // 2,
                image=self.thumbnail_image,
                anchor=tk.CENTER
            )



    # MEDIA SELECTION

    def select_video(self):
        filename = filedialog.askopenfilename(
            title="Select media",
            filetypes=[
                (
                    "Media files",
                    "*.mp4 *.avi *.mkv *.mov *.wmv "
                    "*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.wma"
                ),
                (
                    "Video files",
                    "*.mp4 *.avi *.mkv *.mov *.wmv"
                ),
                (
                    "Audio files",
                    "*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.wma"
                ),
                ("All files", "*.*")
            ]
        )

        if filename:
            self.player_data["session"] = None
            self.save_player_data()

            self.stop_audio()

            with self.lock:
                self.playing = False
                self.filename = filename
                self.reset_position()
                self.stop_requested = True

                self.folder_files = []
                self.folder_index = -1
                self.folder_active = False

                self.media_is_audio = filename.lower().endswith(
                    AUDIO_EXTENSIONS
                )

            self.file_label.config(
                text=os.path.basename(filename)
            )

            self.update_thumbnail(filename)
        self.update_bottom_filename()

        self.progress_label.config(
                text=(
                    "Audio selected"
                    if self.media_is_audio
                    else "Video selected"
                )
            )

    def select_folder(self, folder=None, autoplay=False):
        if folder is None:
            folder = filedialog.askdirectory(
                title="Select Media Folder"
            )

        if not folder:
            return

        if not os.path.isdir(folder):
            messagebox.showerror(
                "Folder Error",
                "The selected folder does not exist."
            )
            return

        media_files = []

        for root_dir, directories, filenames in os.walk(folder):
            directories.sort(key=str.lower)

            for filename in sorted(filenames, key=str.lower):
                if filename.lower().endswith(MEDIA_EXTENSIONS):
                    media_files.append(
                        os.path.join(root_dir, filename)
                    )

        if not media_files:
            messagebox.showwarning(
                "No Media Found",
                "No supported media files were found."
            )
            return

        self.stop_audio()

        with self.lock:
            self.playing = False
            self.stop_requested = True

            self.folder_files = media_files
            self.folder_index = 0
            self.folder_active = True

            self.filename = self.folder_files[0]
            self.reset_position()

            self.media_is_audio = (
                self.filename.lower().endswith(AUDIO_EXTENSIONS)
            )

        self.file_label.config(
            text=os.path.basename(self.filename)
        )
        self.update_bottom_filename()

        self.progress_label.config(
            text=f"Folder loaded: {len(media_files)} files"
        )

        self.add_recent_folder(folder)

        if autoplay:
            with self.lock:
                self.playing = True
                self.stop_requested = False

            if self.connected:
                self.play()
            else:
                self.connect()

    # RECENT FOLDERS

    def load_recent_folders(self):
        self.recent_folders = [
            folder for folder in self.recent_folders
            if os.path.isdir(folder)
        ]

    def save_recent_folders(self):
        self.save_player_data()

    def add_recent_folder(self, folder):
        folder = os.path.abspath(folder)

        if folder in self.recent_folders:
            self.recent_folders.remove(folder)

        self.recent_folders.insert(0, folder)
        self.recent_folders = self.recent_folders[:10]

        self.save_recent_folders()
        self.update_recent_folders_menu()

    def update_recent_folders_menu(self):
        self.recent_folders_menu.delete(0, tk.END)

        self.recent_folders = [
            folder for folder in self.recent_folders
            if os.path.isdir(folder)
        ]

        if not self.recent_folders:
            self.recent_folders_menu.add_command(
                label="No Recent Folders",
                state=tk.DISABLED
            )
            return

        for folder in self.recent_folders:
            self.recent_folders_menu.add_command(
                label=folder,
                command=lambda path=folder: self.select_folder(
                    path,
                    autoplay=True
                )
            )

    def finish_current_media(self):
        if self.stop_after_current:
            self.stop_after_current = False

            with self.lock:
                self.playing = False

            self.stop_audio()

            self.progress_label.config(
                text="Stopped after current media"
            )
            return

        loop_mode = self.loop_mode_box.get()

        if self.folder_active:
            self.advance_folder()
            return

        if loop_mode == "Loop Current Media":
            with self.lock:
                self.reset_position()
                self.playing = True

            self.stop_audio()

            self.root.after(
                100,
                self.play
            )
            return

        with self.lock:
            self.playing = False
            self.reset_position()

        self.stop_audio()

        self.progress_label.config(
            text="Playback finished"
        )



    # FOLDER PLAYBACK

    def advance_folder(self):
        if not self.folder_active:
            return

        self.stop_audio()

        if self.stop_after_current:
            self.stop_after_current = False
            self.folder_active = False
            self.folder_files = []
            self.folder_index = -1

            with self.lock:
                self.playing = False

            self.progress_label.config(
                text="Stopped after current media"
            )
            return

        self.folder_index += 1

        if self.folder_index >= len(self.folder_files):
            if self.loop_mode_box.get() == "Loop Playlist":
                self.folder_index = 0

            else:
                self.folder_active = False
                self.folder_files = []
                self.folder_index = -1

                with self.lock:
                    self.playing = False

                self.progress_label.config(
                    text="Folder playback finished"
                )
                return

        self.filename = self.folder_files[self.folder_index]
        self.reset_position()
        self.reverse_playing = False
        self.seek_request = None

        self.media_is_audio = (
            self.filename.lower().endswith(AUDIO_EXTENSIONS)
        )

        self.file_label.config(
            text=os.path.basename(self.filename)
        )
        self.update_bottom_filename()

        self.progress_label.config(
            text=(
                f"Folder: {self.folder_index + 1}"
                f" / {len(self.folder_files)}"
            )
        )

        self.root.after(
            100,
            self.start_next_folder_item
        )

    def start_next_folder_item(self):
        if not self.folder_active or not self.playing:
            return

        audio_alive = (
            self.audio_thread is not None
            and self.audio_thread.is_alive()
        )

        if self.worker_running or audio_alive:
            self.root.after(
                100,
                self.start_next_folder_item
            )
            return

        self.play()

    # REMEMBER SESSION

    def remember_session(self):
        saved_session = self.player_data.get("session")

        if not isinstance(saved_session, dict):
            messagebox.showinfo(
                "Remember Session",
                "No saved media session."
            )
            return

        filename = saved_session.get(
            "filename",
            ""
        )

        saved_resolution = saved_session.get(
            "resolution",
            "160 x 120"
        )

        if not filename:
            messagebox.showinfo(
                "Remember Session",
                "No saved media session."
            )
            return

        if not os.path.isfile(filename):
            messagebox.showerror(
                "Remember Session",
                "The saved media file no longer exists."
            )
            return

        size = parse_resolution(saved_resolution)

        if size:
            width, height = size
            self.resolution_var.set(f"{width} x {height}")

            with self.lock:
                self.output_width = width
                self.output_height = height

        self.stop_audio()

        with self.lock:
            self.playing = True
            self.filename = filename
            self.reset_position()
            self.stop_requested = False

            self.folder_files = []
            self.folder_index = -1
            self.folder_active = False

            self.media_is_audio = filename.lower().endswith(
                AUDIO_EXTENSIONS
            )

        self.file_label.config(
            text=os.path.basename(filename)
        )
        self.update_bottom_filename()

        self.progress_label.config(
            text=(
                "Audio selected"
                if self.media_is_audio
                else "Video selected"
            )
        )

        # CLEAR THE REMEMBERED SESSION AFTER LOADING IT.
        self.player_data["session"] = None
        self.save_player_data()

        if self.connected:
            self.play()
        else:
            self.connect()

    # RECENT MEDIA

    def save_recent_media(self):
        self.save_player_data()

    def add_recent_media(self, filename=None):
        if filename is None:
            filename = self.filename

        if not filename:
            return

        entry = {
            "filename": filename,
            "resolution": self.resolution_var.get()
        }

        self.recent_media = [
            item for item in self.recent_media
            if item["filename"] != filename
        ]

        self.recent_media.insert(
            0,
            entry
        )

        self.recent_media = self.recent_media[:10]

        self.save_recent_media()

    def update_recent_media_menu(self):
        self.recent_media_menu.delete(0, tk.END)

        if not self.recent_media:
            self.recent_media_menu.add_command(
                label="No recent media",
                state=tk.DISABLED
            )
            return

        for entry in self.recent_media:
            filename = entry["filename"]
            resolution = entry["resolution"]

            label = (
                f"{os.path.basename(filename)}"
                f" - {os.path.dirname(filename)}"
            )

            self.recent_media_menu.add_command(
                label=label,
                command=lambda path=filename, res=resolution:
                    self.play_recent_media(path, res)
            )

    def play_recent_media(self, filename, resolution):
        if not os.path.isfile(filename):
            messagebox.showerror(
                "Recent Media",
                "This media file no longer exists."
            )
            return

        size = parse_resolution(resolution)

        if size:
            width, height = size
            self.resolution_var.set(f"{width} x {height}")

            with self.lock:
                self.output_width = width
                self.output_height = height

        self.stop_audio()

        with self.lock:
            self.playing = True
            self.filename = filename
            self.reset_position()
            self.stop_requested = False

            self.folder_files = []
            self.folder_index = -1
            self.folder_active = False

            self.media_is_audio = filename.lower().endswith(
                AUDIO_EXTENSIONS
            )

        self.file_label.config(
            text=os.path.basename(filename)
        )
        self.update_bottom_filename()

        self.progress_label.config(
            text=(
                "Audio selected"
                if self.media_is_audio
                else "Video selected"
            )
        )

        self.add_recent_media(filename)

        if self.connected:
            self.play()
        else:
            self.connect()

    # AUDIO STREAMING

    def reset_position(self):
        """Back to the start of the current media (video and audio)."""
        self.video_position = 0
        self.audio_position = 0.0

    def audio_start_seconds(self):
        """Where audio should (re)start so it matches what is on screen."""
        if self.media_is_audio:
            return self.audio_position

        fps = max(0.1, float(self.current_video_fps))

        return self.video_position / fps

    def stream_audio(self):
        with self.lock:
            filename = self.filename
            active_sock = self.sock
            audio_only = self.media_is_audio
            start_seconds = self.audio_start_seconds()

        if not active_sock or not filename:
            return

        audio = None
        audio_finished = False
        sent_bytes = 0

        try:
            audio = extract_audio(filename, start_seconds)
            self.audio_process = audio

            if audio.stdout is None:
                raise RuntimeError(
                    "FFmpeg did not provide an audio output pipe."
                )

            next_packet_time = time.monotonic()

            while (
                self.playing
                and self.connected
                and not self.closing
                and not self.audio_stop.is_set()
            ):
                chunk = audio.stdout.read(AUDIO_CHUNK)

                if not chunk:
                    # An empty read after stop/pause just means we killed ffmpeg.
                    audio_finished = not self.audio_stop.is_set()
                    break

                # SOFTWARE VOLUME
                volume = 0 if self.muted else self.volume_level

                if volume < 100:
                    samples = np.frombuffer(
                        chunk,
                        dtype=np.uint8
                    ).astype(np.int16)

                    samples = 128 + (samples - 128) * volume / 100

                    chunk = np.clip(
                        samples,
                        0,
                        255
                    ).astype(np.uint8).tobytes()

                packet = b"A" + struct.pack("!H", len(chunk)) + chunk

                with self.send_lock:
                    with self.lock:
                        if (
                            not self.connected
                            or self.sock is not active_sock
                            or self.audio_stop.is_set()
                        ):
                            break

                    active_sock.sendall(packet)

                sent_bytes += len(chunk)
                self.audio_ready.set()

                if audio_only:
                    self.audio_position = (
                        start_seconds + sent_bytes / AUDIO_RATE
                    )

                # PACE AUDIO AT REAL TIME
                next_packet_time += len(chunk) / AUDIO_RATE

                # The first AUDIO_PREFILL bytes go out unpaced, which fills the
                # ESP32's prebuffer instantly; afterwards we stay that far ahead.
                if sent_bytes > AUDIO_PREFILL:
                    wait_time = next_packet_time - time.monotonic()

                    if wait_time > 0:
                        self.audio_stop.wait(wait_time)
                    else:
                        next_packet_time = time.monotonic()

        except OSError:
            self.connection_lost(active_sock)

        except Exception as e:
            self.gui(
                lambda err=str(e): self.progress_label.config(
                    text=f"Audio error: {err}"
                )
            )

        finally:
            self.audio_ready.set()

            if audio is not None:
                try:
                    audio.terminate()
                    audio.wait(timeout=1)
                except Exception:
                    try:
                        audio.kill()
                        audio.wait(timeout=1)
                    except Exception:
                        pass

            if self.audio_process is audio:
                self.audio_process = None

            if audio_finished and audio_only and self.playing:
                self.gui(self.finish_current_media)

    def start_audio(self):
        if (
            not self.playing
            or not self.connected
            or not self.filename
        ):
            return

        old = self.audio_thread

        if old and old.is_alive():
            if not self.audio_stop.is_set():
                return  # already streaming

            # A stop is still finishing; wait for it so we don't skip this start.
            old.join(timeout=2.0)

            if old.is_alive():
                return

        self.audio_stop.clear()
        self.audio_ready.clear()

        self.audio_thread = threading.Thread(
            target=self.stream_audio,
            daemon=True
        )

        self.audio_thread.start()

    def stop_audio(self):
        self.audio_stop.set()

        process = self.audio_process

        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass

    def restart_audio(self):
        """Re-sync audio to the current video position (after a seek)."""
        self.stop_audio()
        self.start_audio()

    # PLAYBACK

    def play(self):
        if self.resume_after_id is not None:
            self.root.after_cancel(self.resume_after_id)
            self.resume_after_id = None

        with self.lock:
            if not self.connected:
                messagebox.showwarning(
                    "Not connected",
                    "Connect to ESP32 first."
                )
                return

            if not self.filename:
                messagebox.showwarning(
                    "No media",
                    "Select a file first."
                )
                return

            # If the previous video worker is stopping,
            # wait for it to finish before starting another.
            if self.worker_running and self.stop_requested:
                self.progress_label.config(
                    text="Preparing playback..."
                )

                self.root.after(100, self.play)
                return

            if self.stop_requested:
                self.reset_position()

            self.stop_requested = False
            self.playing = True

            is_audio = self.media_is_audio
            worker_active = self.worker_running
            folder_active = self.folder_active

        self.add_recent_media()
        self.start_audio()

        if not is_audio and not worker_active:
            if folder_active:
                self.root.after(150, self.start_streaming)
            else:
                self.start_streaming()

        self.progress_label.config(
            text="Playing"
        )


    def stop(self):
        with self.lock:
            self.playing = False
            self.stop_requested = True
            self.reset_position()

        self.stop_audio()

        self.progress_label.config(
            text="Stopped"
        )

    # PLAYBACK MODIFIERS

    def load_modifier_controls(self):
        settings = self.modifier_settings

        self.play_schedule_entry.delete(0, tk.END)
        self.play_schedule_entry.insert(0, str(settings.get("schedule", "")))

        self.play_delay_entry.delete(0, tk.END)
        self.play_delay_entry.insert(0, str(settings.get("schedule_delay", "")))

        self.pause_duration_entry.delete(0, tk.END)
        self.pause_duration_entry.insert(0, str(settings.get("auto_resume", "")))

        self.temp_stop_entry.delete(0, tk.END)
        self.temp_stop_entry.insert(0, str(settings.get("temporary_stop", "")))

        self.skip_interval_entry.delete(0, tk.END)
        self.skip_interval_entry.insert(0, str(settings.get("skip_interval", "10")))

        speed = settings.get("reverse_speed", "1x")
        if speed in self.reverse_speed_box["values"]:
            self.reverse_speed_box.set(speed)

        loop = settings.get("loop_mode", "No Loop")
        if loop in self.loop_mode_box["values"]:
            self.loop_mode_box.set(loop)

        self.volume_level = max(
            0,
            min(100, int(settings.get("volume", 100)))
        )

        self.muted = bool(settings.get("muted", False))

        self.last_nonzero_volume = max(
            1,
            min(
                100,
                int(settings.get(
                    "last_nonzero_volume",
                    self.volume_level if self.volume_level > 0 else 100
                ))
            )
        )

        # A zero volume must always mean muted.

        # Restore the previous volume if the saved state is muted.
        if self.muted and self.volume_level == 0:
            self.volume_level = self.last_nonzero_volume
            self.muted = False

        self.volume_slider.set(
            0 if self.muted else self.volume_level
        )

        self.mute_button.config(
            text="UNMUTE" if self.muted else "MUTE"
        )

    def save_modifier_settings(self):
        if self.volume_level > 0:
            self.last_nonzero_volume = self.volume_level

        self.modifier_settings = {
            "schedule": self.play_schedule_entry.get().strip(),
            "schedule_delay": self.play_delay_entry.get().strip(),
            "auto_resume": self.pause_duration_entry.get().strip(),
            "temporary_stop": self.temp_stop_entry.get().strip(),
            "skip_interval": self.skip_interval_entry.get().strip(),
            "reverse_speed": self.reverse_speed_box.get(),
            "loop_mode": self.loop_mode_box.get(),
            "volume": self.volume_level,
            "muted": self.muted,
            "last_nonzero_volume": self.last_nonzero_volume
        }

        self.player_data["modifiers"] = self.modifier_settings
        self.save_player_data()

        self.update_volume_controls()


    def get_minutes(self, entry, setting_name):
        value = entry.get().strip()

        if not value:
            return None

        try:
            minutes = float(value)

            if minutes <= 0:
                raise ValueError

            return minutes

        except ValueError:
            messagebox.showerror(
                "Invalid Setting",
                setting_name + " must be a positive number of minutes."
            )
            return None

    # SCHEDULE PLAYBACK

    def schedule_playback(self):
        time_text = self.play_schedule_entry.get().strip()

        try:
            scheduled_time = datetime.strptime(
                time_text,
                "%H:%M"
            ).time()

        except ValueError:
            messagebox.showerror(
                "Invalid Schedule",
                "Enter a valid 24-hour time, such as 18:30."
            )
            return

        delay_minutes = 0
        delay_text = self.play_delay_entry.get().strip()

        if delay_text:
            try:
                delay_minutes = float(delay_text)

                if delay_minutes < 0:
                    raise ValueError

            except ValueError:
                messagebox.showerror(
                    "Invalid Delay",
                    "Delay must be zero or a positive number of minutes."
                )
                return

        now = datetime.now()

        target = datetime.combine(
            now.date(),
            scheduled_time
        )

        if target <= now:
            target += timedelta(days=1)

        target += timedelta(minutes=delay_minutes)

        wait_ms = max(
            1,
            int((target - now).total_seconds() * 1000)
        )

        if self.schedule_after_id is not None:
            self.root.after_cancel(self.schedule_after_id)

        self.save_modifier_settings()

        self.schedule_after_id = self.root.after(
            wait_ms,
            self.scheduled_play
        )

        self.progress_label.config(
            text="Scheduled for " + target.strftime("%H:%M")
        )

    def scheduled_play(self):
        self.schedule_after_id = None
        self.play()

    # AUTOMATIC PAUSE RESUME

    def pause(self):
        with self.lock:
            self.playing = False

        self.stop_audio()

        if self.resume_after_id is not None:
            self.root.after_cancel(self.resume_after_id)
            self.resume_after_id = None

        minutes = self.get_minutes(
            self.pause_duration_entry,
            "Auto-resume"
        )

        if minutes is not None:
            self.save_modifier_settings()

            self.resume_after_id = self.root.after(
                int(minutes * 60000),
                self.resume_playback
            )

            self.progress_label.config(
                text=f"Paused - resuming in {minutes:g} minutes"
            )

        else:
            self.progress_label.config(text="Paused")

    def resume_playback(self):
        self.resume_after_id = None
        self.play()

    # TEMPORARY STOP

    def temporary_stop(self):
        minutes = self.get_minutes(
            self.temp_stop_entry,
            "Temporary Stop"
        )

        if minutes is None:
            return

        if not self.filename:
            messagebox.showwarning(
                "No Media",
                "Select media first."
            )
            return

        if self.resume_after_id is not None:
            self.root.after_cancel(self.resume_after_id)

        with self.lock:
            self.playing = False

        self.stop_audio()
        self.save_modifier_settings()

        self.resume_after_id = self.root.after(
            int(minutes * 60000),
            self.resume_playback
        )

        self.progress_label.config(
            text=f"Temporarily stopped - resuming in {minutes:g} minutes"
        )

    # STOP AFTER CURRENT MEDIA

    def stop_after_current_media(self):
        if not self.playing:
            messagebox.showinfo(
                "Stop After Current",
                "Start playback first."
            )
            return

        self.stop_after_current = True

        self.progress_label.config(
            text="Will stop after current media finishes"
        )

    # VIDEO SKIPPING

    def skip_by_minutes(self, direction):
        if self.media_is_audio:
            messagebox.showinfo(
                "Video Seeking",
                "Video seeking is not available for audio-only files yet."
            )
            return

        minutes = self.get_minutes(
            self.skip_interval_entry,
            "Skip Interval"
        )

        if minutes is None:
            return

        if not self.filename:
            return

        frames = int(
            minutes * 60 * self.current_video_fps
        )

        with self.lock:
            target = self.video_position + direction * frames

            target = max(
                0,
                min(target, max(0, self.current_frame_count - 1))
            )

            self.seek_request = target
            self.video_position = target
            self.reverse_playing = False

        self.save_modifier_settings()

        if self.playing:
            self.restart_audio()
        else:
            self.play()

    def skip_backward(self):
        self.skip_by_minutes(-1)

    def skip_forward(self):
        self.skip_by_minutes(1)

            # ESP32 DISPLAY POSITION

    def apply_esp32_position(self, event=None):
        text = self.position_entry.get().strip()

        match = re.fullmatch(
            r"\s*(-?\d+)\s*,\s*(-?\d+)\s*",
            text
        )

        if not match:
            self.progress_label.config(
                text="Position format must be X, Y"
            )
            return "break"

        x = int(match.group(1))
        y = int(match.group(2))

        if not (-32768 <= x <= 32767 and -32768 <= y <= 32767):
            self.progress_label.config(
                text="Position must be between -32768 and 32767"
            )
            return "break"

        self.esp32_x = x
        self.esp32_y = y

        self.player_data["esp32_x"] = x
        self.player_data["esp32_y"] = y
        self.save_player_data()

        # SEND POSITION TO ESP32
        with self.lock:
            active_sock = self.sock
            connected = self.connected

        if connected and active_sock:
            try:
                packet = b"O" + struct.pack("!hh", x, y)

                with self.send_lock:
                    with self.lock:
                        if (
                            not self.connected
                            or self.sock is not active_sock
                        ):
                            return "break"

                    active_sock.sendall(packet)

                self.progress_label.config(
                    text=f"ESP32 position sent: X={x}, Y={y}"
                )

            except OSError:
                self.connection_lost(active_sock)

        else:
            self.progress_label.config(
                text=f"Position saved: X={x}, Y={y} (ESP32 disconnected)"
            )

        self.position_entry.delete(0, tk.END)
        self.position_entry.insert(0, f"{x}, {y}")

        return "break"



    # ============================================================
    # ESP32 TFT BRIGHTNESS
    # ============================================================

    def apply_esp32_brightness(self, value):

        brightness = max(
            0,
            min(100, int(float(value)))
        )

        self.esp32_brightness = brightness

        self.player_data["esp32_brightness"] = brightness

        self.save_player_data()

        with self.lock:
            active_sock = self.sock
            connected = self.connected

        if not connected or not active_sock:
            return

        try:

            packet = b"B" + bytes([brightness])

            with self.send_lock:

                with self.lock:
                    if (
                        not self.connected
                        or self.sock is not active_sock
                    ):
                        return

                active_sock.sendall(packet)

        except OSError:
            self.connection_lost(active_sock)


    # REVERSE VIDEO

    def toggle_reverse(self):
        if self.media_is_audio:
            messagebox.showinfo(
                "Reverse",
                "Reverse playback is available for video files."
            )
            return

        if not self.filename:
            return

        speed_text = self.reverse_speed_box.get()

        try:
            speed = float(speed_text.replace("x", ""))
        except ValueError:
            speed = 1.0

        with self.lock:
            self.reverse_playing = not self.reverse_playing
            self.reverse_speed = speed
            reversing = self.reverse_playing

        self.save_modifier_settings()

        if reversing:
            self.stop_audio()

            self.progress_label.config(
                text=f"Reverse playback - {speed:g}x"
            )

        else:
            self.start_audio()

            self.progress_label.config(
                text="Forward playback"
            )

        if not self.playing:
            self.play()


    def update_volume_controls(self):
        level = max(0, min(100, int(self.volume_level)))

        # Synchronize right-side slider
        if hasattr(self, "volume_slider"):
            self.volume_slider.set(level)

        # Synchronize bottom slider
        if hasattr(self, "bottom_volume_slider"):
            self.bottom_volume_slider.set(level)

        # Synchronize right-side mute button
        button_text = "UNMUTE" if self.muted else "MUTE"

        if hasattr(self, "mute_button"):
            self.mute_button.config(text=button_text)

        # Synchronize bottom mute button
        if hasattr(self, "bottom_mute_button"):
            self.bottom_mute_button.config(text=button_text)


    def on_volume_changed(self, value):
        level = max(0, min(100, int(float(value))))

        self.volume_level = level
        self.muted = (level == 0)

        if level > 0:
            self.last_nonzero_volume = level

        self.update_volume_controls()

        self.modifier_settings["volume"] = self.volume_level
        self.modifier_settings["muted"] = self.muted

        self.save_modifier_settings()


    def toggle_mute(self):
        if self.muted:
            self.muted = False

            self.volume_level = max(
                1,
                getattr(self, "last_nonzero_volume", 100)
            )

        else:
            if self.volume_level > 0:
                self.last_nonzero_volume = self.volume_level

            self.muted = True
            self.volume_level = 0

        self.update_volume_controls()

        self.modifier_settings["volume"] = self.volume_level
        self.modifier_settings["muted"] = self.muted

        self.save_modifier_settings()

    # ==========================================
    # BOTTOM PLAYBACK BAR METHODS
    # ==========================================

    def update_bottom_filename(self):
        if hasattr(self, "bottom_filename"):
            self.bottom_filename.config(
                text=os.path.basename(self.filename)
                if self.filename else "No media selected"
            )

    def update_bottom_progress(self, frame, total):
        if total <= 0:
            return

        fps = max(0.1, float(self.current_video_fps))

        current_time = max(0, frame - 1) / fps
        total_time = total / fps

        self.timeline_time_label.config(
            text=(
                f"{self.format_playback_time(current_time)}"
                f" / "
                f"{self.format_playback_time(total_time)}"
            )
        )

        self.frame_counter_label.config(
            text=f"Frame {frame} / {total}"
        )

        position = int(
            max(0, min(1000, frame / max(1, total) * 1000))
        )

        self.timeline_scale.set(position)

    # VIDEO STREAMING

    def start_streaming(self):
        with self.lock:
            if (
                self.worker_running
                or not self.connected
                or not self.filename
                or self.closing
                or self.media_is_audio
            ):
                return

            self.worker_running = True

        threading.Thread(
            target=self.stream_video,
            daemon=True
        ).start()

    def send_frame(self, frame):
        with self.lock:
            if not self.connected or not self.sock:
                return False

            active_sock = self.sock

        send_start = time.monotonic()

        try:
            height, width = frame.shape[:2]

            # ENCODE EVERY FRAME AS JPEG
            success, encoded = cv2.imencode(
                ".jpg",
                frame,
                [
                    cv2.IMWRITE_JPEG_QUALITY,
                    75,
                    cv2.IMWRITE_JPEG_OPTIMIZE,
                    1
                ]
            )

            if not success:
                return False

            data = encoded.tobytes()

            # JPEG PACKET HEADER
            packet = (
                b"J"
                + struct.pack(
                    "!HHI",
                    width,
                    height,
                    len(data)
                )
                + data
            )

            with self.send_lock:
                with self.lock:
                    if (
                        not self.connected
                        or self.sock is not active_sock
                    ):
                        return False

                active_sock.sendall(packet)

            send_elapsed = (
                time.monotonic() - send_start
            ) * 1000

            with self.stats_lock:
                self.sender_frames_total += 1
                self.sender_bytes_total += len(packet)
                self.sender_send_ms = send_elapsed
                self.sender_frame_size = len(packet)

            return True

        except OSError:
            self.connection_lost(active_sock)
            return False

    def stream_video(self):
        cap = None
        media_finished = False

        try:
            with self.lock:
                filename = self.filename
                position = self.video_position

            cap = cv2.VideoCapture(filename)

            if not cap.isOpened():
                self.gui(
                    lambda: messagebox.showerror(
                        "Video Error",
                        "Cannot open this video."
                    )
                )
                return

            fps = cap.get(cv2.CAP_PROP_FPS)

            if fps <= 0 or fps > 60:
                fps = FPS

            print(f"VIDEO SOURCE FPS: {fps}")
            print(f"MANUAL FPS LIMIT: {self.playback_fps_limit}")
            print(f"RESOLUTION: {self.output_width} x {self.output_height}")

            frame_count = int(
                cap.get(cv2.CAP_PROP_FRAME_COUNT)
            )

            with self.lock:
                self.current_video_fps = fps
                self.current_frame_count = frame_count
            source_fps = fps

            frame_number = position

            if position > 0:
                cap.set(
                    cv2.CAP_PROP_POS_FRAMES,
                    position
                )

            audio_wait_until = None
            skip_credit = 0.0   # fractional source frames carried between sends

            while not self.closing:

                # HOLD VIDEO UNTIL AUDIO HAS ACTUALLY STARTED
                # (ffmpeg needs a moment to open the file). Done before reading
                # the seek request so a pending seek isn't lost.
                audio_thread = self.audio_thread

                if (
                    not self.playing
                    or self.reverse_playing
                    or self.audio_ready.is_set()
                    or audio_thread is None
                    or not audio_thread.is_alive()
                ):
                    audio_wait_until = None
                else:
                    if audio_wait_until is None:
                        audio_wait_until = (
                            time.monotonic() + AUDIO_START_WAIT
                        )

                    if time.monotonic() < audio_wait_until:
                        time.sleep(0.01)
                        continue

                with self.lock:
                    connected = self.connected
                    playing = self.playing
                    stopped = self.stop_requested
                    reverse = self.reverse_playing
                    speed = self.reverse_speed
                    seek = self.seek_request

                    if seek is not None:
                        self.seek_request = None

                if stopped:
                    break

                if not connected:
                    time.sleep(0.2)
                    continue

                if not playing:
                    time.sleep(0.05)
                    continue

                # HANDLE FORWARD / BACKWARD SEEK REQUESTS
                if seek is not None:
                    frame_number = seek
                    skip_credit = 0.0

                    cap.set(
                        cv2.CAP_PROP_POS_FRAMES,
                        frame_number
                    )

                start = time.monotonic()

                # REVERSE PLAYBACK
                if reverse:
                    frame_number = max(
                        0,
                        frame_number - 1
                    )

                    cap.set(
                        cv2.CAP_PROP_POS_FRAMES,
                        frame_number
                    )

                with self.lock:
                    output_width = self.output_width
                    output_height = self.output_height
                    fps_limit = self.playback_fps_limit

                # CALCULATE TARGET FPS (frames SENT per second)
                # Bigger frames take longer to send, so allow fewer per second.
                resolution_fps = (
                    FPS * (MAX_RES_W * MAX_RES_H)
                    / (output_width * output_height)
                )

                if fps_limit > 0:
                    target_fps = min(fps_limit, resolution_fps)
                else:
                    target_fps = min(
                        source_fps,
                        MAX_SMALL_RES_FPS,
                        resolution_fps
                    )

                if reverse:
                    target_fps *= speed
                else:
                    if target_fps < source_fps:
                        skip_credit += source_fps / max(1.0, target_fps)
                        step = max(1, int(skip_credit))
                        skip_credit -= step

                        for _ in range(step - 1):
                            if not cap.grab():
                                break

                            frame_number += 1

                frame_delay = 1.0 / max(1, target_fps)

                capture_start = time.monotonic()

                ret, frame = cap.read()

                capture_ms = (
                    time.monotonic() - capture_start
                ) * 1000

                with self.stats_lock:
                    self.sender_capture_ms = capture_ms

                if not ret:
                    media_finished = True
                    break

                resize_start = time.monotonic()

                frame = cv2.resize(
                    frame,
                    (output_width, output_height),
                    interpolation=cv2.INTER_AREA
                )

                resize_ms = (
                    time.monotonic() - resize_start
                ) * 1000

                with self.stats_lock:
                    self.sender_resize_ms = resize_ms
                    self.sender_target_fps = target_fps
                    print(f"ACTUAL TARGET FPS: {target_fps} | SELECTED: {fps_limit} | SOURCE: {source_fps}")

                if not self.send_frame(frame):
                    time.sleep(0.2)
                    continue

                if reverse:
                    with self.lock:
                        self.video_position = frame_number

                    if frame_number <= 0:
                        media_finished = True
                        break

                else:
                    frame_number += 1

                    with self.lock:
                        self.video_position = frame_number

                self.gui(
                    lambda n=frame_number, total=frame_count: (
                        self.progress_label.config(
                            text=f"Playing: {n} / {total} frames"
                        ),
                        self.update_bottom_progress(n, total)
                    )
                )

                elapsed = time.monotonic() - start

                time.sleep(
                    max(0, frame_delay - elapsed)
                )

        except Exception as e:
            self.gui(
                lambda err=str(e): self.progress_label.config(
                    text=f"Video error: {err}"
                )
            )

        finally:
            if cap:
                cap.release()

            with self.lock:
                self.worker_running = False

            if media_finished and self.playing and not self.stop_requested:
                self.gui(self.finish_current_media)

    # CLOSE

    def close(self):
        if self.filename:
            self.player_data["session"] = {
                "filename": self.filename,
                "resolution": self.resolution_var.get()
            }

            self.save_player_data()
        if hasattr(self, "play_schedule_entry"):
            self.save_modifier_settings()
        with self.lock:
            self.closing = True
            self.connected = False
            self.playing = False

            active_sock = self.sock
            self.sock = None

        self.stop_audio()

        # CLOSE ESP32 STATUS CONNECTION
        with self.status_lock:
            active_status_sock = self.status_sock
            self.status_sock = None
            self.status_connected = False

        if active_status_sock:
            try:
                active_status_sock.shutdown(
                    socket.SHUT_RDWR
                )
            except OSError:
                pass

            try:
                active_status_sock.close()
            except OSError:
                pass

        if active_sock:
            try:
                active_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

            try:
                active_sock.close()
            except OSError:
                pass

        allow_sleep()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = CYDVideoPlayer(root)
    root.mainloop()
