import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import os
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary,
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.location_finder   import location_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.phone_control     import phone_control
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.website_scraper   import scrape_website, run_scraped_site, remember_scrape_folder, last_scrape_folder
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.hand_gesture      import HandGestureEngine


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
# Deprecated preview-12-2025 → use current Live native-audio model
LIVE_MODEL          = "gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS. Speak ONLY fluent natural Kurdish Sorani (کوردی سۆرانی). "
            "Arabic script. Address as گەورەم. Never English or Turkish. "
            "Short clear everyday Sorani. Smooth speech, never letter-by-letter. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()


def _join_transcript(parts: list[str]) -> str:
    """Join streamed transcript fragments.

    Gemini often streams Kurdish (and some other scripts) as 1–2 character
    pieces. Joining those with spaces makes logs look like 'س ل ا و'.
    """
    cleaned = [p for p in (x.strip() for x in parts) if p]
    if not cleaned:
        return ""
    short = sum(1 for p in cleaned if len(p) <= 2)
    if short >= max(3, int(len(cleaned) * 0.45)):
        return "".join(cleaned)
    return " ".join(cleaned)


_YT_INTENT = re.compile(
    r"(youtube|یوتیوب|\byt\b|گۆرانی|goran|grani|song|music|tubu|tubu|"
    r"l[êe]b[de]|play|kamulepdi|kamul[êe]p|burita|atoni|گۆرانی)",
    re.I,
)
_PHONE_HINT = re.compile(r"phone|mobile|مۆبایل|لە\s*مۆبایل", re.I)


def _youtube_query_from_text(text: str) -> str | None:
    """Detect PC YouTube play intent from user speech/text."""
    t = (text or "").strip()
    if not t or _PHONE_HINT.search(t):
        return None
    if not _YT_INTENT.search(t):
        return None
    q = re.sub(
        r"(?i)(please|open|go to|youtube|یوتیوب|play|l[êe]b[de]|گۆرانی|"
        r"song|music|video|for me|بکە|bke|bka|atoni|burita|tubu|grani|kamulepdi|"
        r"any|random|something|چ|\bme\b)",
        " ",
        t,
    )
    q = re.sub(r"\s+", " ", q).strip(" ,.-")
    return q if len(q) >= 2 else "kurdish music"

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it. "
            "SPECIAL monitoring module (chawdere): "
            "If user only says they WANT to surveil someone («ئەمڕۆ ئەمەوێ چاودێری کەسێک بکەم») "
            "— do NOT call this tool yet; ask the scripted camera-section question first. "
            "Open ONLY after they confirm / say open / «بڕۆ بەشی چاودێریکردن»: "
            "open_app(app_name='chawdere', action='open'). "
            "Close → open_app(app_name='chawdere', action='close'). "
            "Do NOT use computer_settings close_window for this — it will fail."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": (
                        "Application name. For the monitoring section use exactly: chawdere "
                        "(aliases: چاودێریکردن, بەشی چاودێریکردن, surveillance)."
                    ),
                },
                "action": {
                    "type": "STRING",
                    "description": "open | close (default: open). Use close for بەشی چاودێریکردن داخە.",
                },
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. "
            "Modes: 'search' (default), 'news' (latest headlines on a topic), "
            "'research' (deep comprehensive answer), 'price' (product cost lookup), "
            "'compare' (side-by-side comparison of items)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query or topic"},
                "mode":   {"type": "STRING", "description": "search | news | research | price | compare"},
                "items":  {"type": "ARRAY",  "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "website_scraper",
        "description": (
            "STEP 1 ONLY — scrape / take a website's source «بردنی وێبسایت». "
            "Before calling without url, speak Sorani: scraping mode is on, paste the link. "
            "Use ONLY when user wants to scrape. NEVER use this to run or serve a site."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "url": {
                    "type": "STRING",
                    "description": "Full website URL if already known. Omit to open the paste box.",
                },
            },
        }
    },
    {
        "name": "run_scraped_website",
        "description": (
            "STEP 2 ONLY — run the website that was already scraped. "
            "Use ONLY when user explicitly asks to run / serve / open the scraped site "
            "(e.g. run the website we scraped, وێبسایتەکە لێبدە, serve it). "
            "Opens a terminal, cds to the saved folder, runs python run.py. "
            "NEVER call this during or right after scraping unless user clearly asked to run."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "folder": {
                    "type": "STRING",
                    "description": "Optional folder path. Omit to use the last scraped site.",
                },
            },
        }
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "DEFAULT for playing songs/videos on YouTube on the WINDOWS PC. "
            "ALWAYS use this for: play a song, play music, گۆرانی لێبدە, یوتیوب, "
            "play kurdish song, go to youtube and play… — UNLESS the user clearly "
            "says phone / mobile / مۆبایل / لە مۆبایل. "
            "After play, the browser is minimized and audio keeps playing. "
            "Do NOT use phone_control for normal song requests."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "ONE-SHOT capture of the SCREEN (or a single webcam snapshot) to analyze it. "
            "Use for: what is on my screen, analyze my screen, look at this screenshot. "
            "Do NOT use this to 'open the camera' — that is hand_gesture. "
            "You have NO visual ability without this tool for screen questions."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' (default) or 'camera' for a one-shot snapshot only — not for opening live camera"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the hand-gesture live camera (and any vision camera preview). "
            "Call when user says: close camera, stop camera, turn off camera, "
            "کامێرا دابخە, kamerayı kapat, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "hand_gesture",
        "description": (
            "Opens/closes the LIVE hand-gesture camera. THIS is the default camera. "
            "MUST use action=start when user says: open the camera, turn on camera, "
            "start camera, open cam, کامێرا بکەوە, کامێرا بکەرەوە, hand camera. "
            "When ON: circle index finger = Windows volume; hold open palm = JARVIS describes what you show. "
            "action=stop when they say close/stop camera (or use close_camera)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "start | stop | toggle (default: start when opening camera)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "Simple open/search requests launch the user's own browser normally (their real profile "
            "and logged-in accounts); interactive actions (click, type, fill_form...) attach an "
            "automation browser. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER",  "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "location_finder",
        "description": (
            "DISABLED in this build. If user asks to find/locate/trace someone, "
            "do NOT call this tool — say the disabled-law Sorani message from prompt.txt."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Phone number, social username, or any identifier the user provided",
                },
                "number": {"type": "STRING", "description": "Phone number if given"},
                "username": {"type": "STRING", "description": "Social media username if given"},
            },
            "required": [],
        },
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "JARVIS checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "phone_control",
        "description": (
            "Android PHONE only (Wi‑Fi ADB). Do NOT use for normal YouTube/song requests. "
            "ONLY use play here if user explicitly says phone/mobile/مۆبایل/لە مۆبایل. "
            "Otherwise songs → youtube_video on PC. "
            "Also: unlock, open apps on phone, home, pair/connect/status."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "unlock | play | youtube | open | open_app | home | pair | connect | status. "
                        "For playing a song/video on phone YouTube use action=play."
                    ),
                },
                "query": {
                    "type": "STRING",
                    "description": (
                        "Search/play query for phone YouTube, e.g. 'kurdish song', "
                        "'گۆرانی کوردی'. Required for action=play."
                    ),
                },
                "app": {
                    "type": "STRING",
                    "description": "App name for open/open_app e.g. youtube, chrome, instagram, whatsapp",
                },
                "url": {
                    "type": "STRING",
                    "description": "Optional YouTube/watch URL to open on the phone",
                },
            },
            "required": [],
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Kurdish Sorani', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
]

# --- Plugin system ---


class JarvisLive:

    def __init__(self, ui):  # ui: JarvisUI (imported at runtime in main())
        self.ui             = ui
        self._asst_name     = "JARVIS"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_summon         = self._on_summon
        self.ui.on_start          = self._on_start
        self.ui.on_voice_changed  = self._on_voice_changed
        self.ui.on_hand_gesture   = self._on_hand_gesture_toggle
        self.ui.on_hand_preview   = self._on_hand_preview_toggle
        try:
            self.ui.on_scrape_url = self._on_scrape_url
        except Exception:
            pass
        self._scrape_event = threading.Event()
        self._scrape_url: str | None = None
        self._scrape_waiting = False
        self._last_scrape_folder: str | None = last_scrape_folder()
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once (manual Start)
        self._session_started  = False          # False until user presses Start / Space
        self._force_reconnect  = False
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary
        self._hand_engine: HandGestureEngine | None = None
        self._hand_vision_pending = False
        self._last_tool_call_time = 0.0
        self._last_local_youtube   = 0.0
        self._quota_warned: set[int] = set()

        # Stay silent until Start — mic muted so you can record intro video first
        if not self.ui.muted:
            self.ui.muted = True
        self.ui.write_log("SYS: Standby — press START or Space when ready.")

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    async def _send_text(self, text: str) -> None:
        """Send a text turn via realtime input (required by gemini-3.1 Live)."""
        if not self.session or not text:
            return
        await self.session.send_realtime_input(text=text)

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(self._send_text(text), self._loop)

    def _on_hand_gesture_toggle(self, enabled: bool) -> None:
        """Start/stop hand tracking thread (called from UI / worker thread)."""
        if enabled:
            if self._hand_engine is None:
                self._hand_engine = HandGestureEngine(
                    on_frame=self._on_hand_frame,
                    on_status=lambda s: self.ui.emit_hand_status(s),
                    on_show_hand=self._on_hand_show_trigger,
                )
            self._hand_engine.set_preview_visible(self.ui.hand_preview_visible())
            msg = self._hand_engine.start()
            self.ui.write_log(f"SYS: {msg}")
            # Camera opens inside the worker thread (Camo/DSHOW). Failure is reported via status.
        else:
            if self._hand_engine:
                msg = self._hand_engine.stop()
                self.ui.write_log(f"SYS: {msg}")

    def _on_hand_preview_toggle(self, visible: bool) -> None:
        if self._hand_engine and self._hand_engine.active:
            self._hand_engine.set_preview_visible(visible)

    def _on_hand_frame(self, jpeg_bytes: bytes) -> None:
        self.ui._win._cam_frame_sig.emit(jpeg_bytes)

    def _on_hand_show_trigger(self, jpeg_bytes: bytes) -> None:
        if not self._loop or not self._session_started:
            return
        asyncio.run_coroutine_threadsafe(
            self._inject_hand_vision(jpeg_bytes), self._loop
        )

    async def _inject_hand_vision(self, img_bytes: bytes) -> None:
        if not self.session or self._vision_busy:
            return
        self._vision_busy = True
        self._vision_last_time = time.monotonic()
        self._hand_vision_pending = True
        self.ui.write_log("SYS: Hand shown — sending to vision...")
        try:
            await self.session.send_realtime_input(
                video=types.Blob(data=img_bytes, mime_type="image/jpeg")
            )
            await self._send_text(
                "[HAND_VISION] The user held an open palm to the camera to show something. "
                "Describe clearly what they are holding or presenting in fluent Kurdish Sorani. "
                "2–3 short sentences. Do not call any tools."
            )
        except Exception as e:
            self._vision_busy = False
            self._hand_vision_pending = False
            print(f"[HandGesture] vision inject failed: {e}")

    def _on_voice_changed(self):
        """Called when the user picks a new voice in the UI drawer."""
        if self.session:
            self.ui.write_log("SYS: Reconnecting to apply new voice...")
            # Trigger a reconnect by closing the current session.
            # The main loop will automatically reconnect and build a new config.
            self._force_reconnect = True
            asyncio.create_task(self.session.close())

    def _on_start(self):
        """Manual start — unmute + run briefing once (no auto-speak on launch)."""
        if self._session_started:
            self.ui.write_log("SYS: Already started.")
            return
        if not self._loop or not self.session:
            self.ui.write_log("SYS: Still connecting — wait a second, then press Start.")
            return

        self._session_started = True
        # mark_started must run on Qt thread
        try:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self.ui._win.mark_started)
        except Exception:
            pass
        if self.ui.muted:
            self.ui.muted = False
        self.ui.write_log("SYS: Started — speaking now.")
        print("[JARVIS] ▶ Session started by user")

        if not self._briefing_sent:
            self._briefing_sent = True
            asyncio.run_coroutine_threadsafe(
                self._send_startup_briefing(), self._loop
            )

    def _on_summon(self):
        """Called when user summons JARVIS (F8 / Ctrl+Shift+J / button)."""
        if not self._session_started:
            self._on_start()
            return
        self._on_text_command(
            "[SUMMON] The user just summoned you to the foreground. "
            "Greet them warmly in Kurdish Sorani only (کوردی سۆرانی, Arabic script), "
            "say you are ready and listening, one short sentence. "
            "Address them as گەورەم. Do not call any tools. Do not speak English."
        )

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(self._send_text(text), self._loop)

    async def _send_quota_warning(self, remaining: int, resets_at: str) -> None:
        if not self.session:
            return
        await self._send_text(
            f"[QUOTA_WARNING] User has {remaining} seconds of voice left today "
            f"(resets {resets_at or 'midnight UTC'}). "
            "Say ONE short sentence in Kurdish Sorani only: daily voice time is almost up."
        )

    async def _handle_quota_exceeded(self, exc) -> None:
        resets = getattr(exc, "resets_at", "") or "midnight UTC"
        self.ui.write_log(f"SYS: Daily voice limit reached. Resets at {resets}.")
        self.ui.muted = True
        self.ui.set_state("SLEEPING")

        spoken = False
        if self.session:
            try:
                await self._send_text(
                    "[QUOTA_EXCEEDED] The daily 5-minute voice limit is now reached. "
                    "Say ONLY this in Kurdish Sorani (Arabic script), calm and warm: "
                    "«گەورەم، کاتەکەت بۆ ئەمڕۆ تەواو بوو — پێنج خولەکت بەکارهێنا. "
                    "سبەیێ دووبارە دەتوانیت بەکاریبهێنیت.» Do not call any tools."
                )
                await asyncio.sleep(10)
                spoken = True
            except Exception:
                pass

        if not spoken:
            def _tts_done():
                try:
                    from core.tts import EdgeTTSEngine
                    EdgeTTSEngine(voice="ar-IQ-BasselNeural").speak(
                        "گەورەم، کاتەکەت بۆ ئەمڕۆ تەواو بوو. سبەیێ دووبارە دەتوانیت بەکاریبهێنیت."
                    )
                except Exception as e:
                    print(f"[Quota] TTS fallback: {e}")

            try:
                await asyncio.get_event_loop().run_in_executor(None, _tts_done)
            except Exception:
                pass

        self._conn_backoff = 300

    async def _local_youtube_play(self, query: str) -> None:
        """Fallback when voice proxy session has no tool calling."""
        now = time.monotonic()
        if now - self._last_local_youtube < 8.0:
            return
        if now - self._last_tool_call_time < 4.0:
            return
        self._last_local_youtube = now
        self.ui.write_log(f"SYS: YouTube auto-play — {query}")
        self.ui.set_state("THINKING")
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: youtube_video(
                    {"action": "play", "query": query},
                    player=self.ui,
                ),
            )
        except Exception as e:
            result = f"YouTube failed: {e}"
        if self.session:
            await self._send_text(
                "[LOCAL_ACTION_DONE] youtube_video already executed on the PC. "
                f"Result: {result}. "
                "Confirm briefly in Sorani that the song is playing now. "
                "Do NOT say you cannot play music."
            )
        if not self.ui.muted:
            self.ui.set_state("LISTENING")

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(
            f"[ERROR] بڵێ بە کوردی سۆرانی تەنها: گەورەم، کێشەیەک ڕوویدا لە {tool_name}. "
            f"کورتەکەی: {short}. هیچ ئینگلیزی مەڵێ."
        )

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            with open(API_CONFIG_PATH, encoding="utf-8") as _f:
                _cfg = json.loads(_f.read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        # Keep Kurdish Sorani as the stored default (fixes old English memory)
        _lang_e = memory.get("identity", {}).get("language", {})
        _lang_v = (_lang_e.get("value", "") if isinstance(_lang_e, dict) else str(_lang_e)).strip()
        if not _lang_v or _lang_v.lower() in ("english", "turkish", "en", "tr"):
            update_memory({"identity": {"language": {"value": "Kurdish Sorani"}}})
            memory = load_memory()

        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        lang_lock = (
            "[LANGUAGE LOCK — ABSOLUTE #1 PRIORITY]\n"
            "Speak ONLY fluent Kurdish Sorani (کوردی سۆرانی / Central Kurdish).\n"
            "Natural spoken Sorani like people in هەولێر and سلێمانی — NOT bookish, NOT English translated.\n"
            "Arabic script. Short clear everyday words. Address as گەورەم.\n"
            "NEVER speak English, Turkish, Kurmanji, or mixed language.\n"
            "If tool/news data is English → translate to natural Sorani before speaking.\n"
            "Never say: sir, efendim, okay, done, one moment, loading.\n"
            "Use instead: باشە، تەواو، چاوەڕێ بە، ئامادەم، ئەنجام درا.\n"
            "Smooth full words — never letter-by-letter. This overrides all other hints.\n\n"
            "[VOICE TONE — CRITICAL]\n"
            "Calm, warm, professional Jarvis-style assistant — like a trusted Kurdish friend.\n"
            "Speak SLOWLY and SMOOTHLY. Never rush. Never sound angry, stiff, or robotic.\n"
            "Use commas (،) and periods (.) between short phrases so speech breathes naturally.\n"
            "Keep replies short (1–3 sentences) but complete and natural — not chopped fragments.\n"
            "Think in Sorani first. Avoid stiff literal translations from English.\n"
            "Good: «باشە گەورەم، ئامادەم. چاوەڕێ بە، ئێستا ئەنجام دەدەم.»\n"
            "Bad: «OK. Done.» or «بەخێربێیتەوە سەرۆک، من زۆر خۆشحاڵم بۆ یارمەتیدانت...» (too formal/long).\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        _addr = (
            f"ADDRESS: Always call the user '{_user_name}'. Speak ONLY Kurdish Sorani."
            if _user_name
            else "ADDRESS: Speak ONLY Kurdish Sorani. Say «گەورەم» respectfully."
        )
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        parts = [lang_lock, time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            # Native-audio models pick language automatically — language_code is rejected
            # (e.g. 'ku' → 1007 Unsupported). Sorani is enforced via system instructions.
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=_cfg.get("gemini_voice", "Orus")
                    )
                ),
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "open_app":
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "screen_process":
                import time as _t_mod
                angle = (args.get("angle") or "screen").lower().strip()
                user_text = (args.get("text") or "What do you see?").strip()
                # "Open the camera" must open hand-gesture cam — not one-shot vision
                _open_cam = bool(re.search(
                    r"\b(open|start|turn\s*on|enable)\b.*\b(camera|cam|webcam)\b"
                    r"|\b(camera|cam|webcam)\b.*\b(open|start|on)\b"
                    r"|کامێرا\s*(بکەوە|بکەرەوە|بکەڕەوە)"
                    r"|^\s*(camera|cam|webcam)\s*$",
                    user_text,
                    re.I,
                ))
                if angle == "camera" and _open_cam:
                    win = self.ui._win
                    if not win._hand_gesture_active:
                        win._toggle_hand_gesture()
                    result = (
                        "Hand gesture camera is now open. "
                        "Tell the user briefly in Sorani that the camera is on: "
                        "circle finger for volume, show open palm to explain what they hold."
                    )
                else:
                    _now = _t_mod.monotonic()
                    _cooldown = 4.0
                    if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                        _wait = max(0, _cooldown - (_now - self._vision_last_time))
                        print(f"[Vision] Cooldown active ({_wait:.1f}s) — ignoring duplicate")
                        result = "Vision is still processing the previous request. I will not call this again."
                    else:
                        self._vision_busy = True
                        self._vision_last_time = _now
                        if angle == "camera":
                            img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                            # Prefer hand-cam preview if already on; else classic stream
                            if not self.ui._win._hand_gesture_active:
                                self.ui.start_camera_stream()
                                self._vision_cam_active = True
                            print(f"[Vision] Camera snapshot: {len(img_b):,} bytes")
                            _stall = "camera"
                        else:
                            img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                            print(f"[Vision] Screen: {len(img_b):,} bytes")
                            _stall = "screen"
                        self._pending_vision = (img_b, mime_t, user_text, angle)
                        result = (
                            f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                            f"Immediately say ONE short natural sentence in the user's own language, "
                            f"telling them you are looking at their {_stall} right now. "
                            f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                        )

            elif name == "close_camera":
                win = self.ui._win
                if win._hand_gesture_active:
                    win._toggle_hand_gesture()
                else:
                    self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "hand_gesture":
                action = (args.get("action") or "start").lower().strip()
                win = self.ui._win
                if action == "start":
                    if not win._hand_gesture_active:
                        win._toggle_hand_gesture()
                    result = (
                        "Hand gesture camera started. "
                        "Confirm briefly in Sorani that camera is open."
                    )
                elif action == "stop":
                    if win._hand_gesture_active:
                        win._toggle_hand_gesture()
                    result = "Hand gesture camera stopped."
                else:
                    win._toggle_hand_gesture()
                    result = (
                        "Hand gesture camera started."
                        if win._hand_gesture_active
                        else "Hand gesture camera stopped."
                    )

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            elif name == "website_scraper":
                result = await loop.run_in_executor(
                    None, lambda: self._run_website_scraper(args)
                )

            elif name == "run_scraped_website":
                folder = ((args or {}).get("folder") or "").strip() or self._last_scrape_folder
                result = await loop.run_in_executor(
                    None, lambda: self._run_scraped_website(folder)
                )
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "location_finder":
                result = (
                    "FEATURE DISABLED. Say EXACTLY in Sorani — nothing else:\n"
                    "«ببورە ئەم یاسایە گۆڕاوە و بۆ تۆ بەردەست نییە، "
                    "ئەو ڤێرژنە تەنها بۆ ڕەوە دێڤ بەردەستە!»\n"
                    "Do NOT run any scan. Do NOT call this tool again."
                )

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "phone_control":
                r = await loop.run_in_executor(
                    None, lambda: phone_control(parameters=args, player=self.ui)
                )
                result = r or "Phone action done."

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self._send_text(
                                "Say a brief natural goodbye to the user in Kurdish Sorani."
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            # Prefer explicit audio= Blob (media= is legacy / rejected on newer Live models)
            data = msg.get("data") if isinstance(msg, dict) else None
            mime = (msg.get("mime_type") if isinstance(msg, dict) else None) or "audio/pcm;rate=16000"
            if data is not None:
                await self.session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type=mime)
                )
            else:
                await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if not jarvis_speaking and not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm;rate=16000"}
                )

        try:
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
            ):
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _receive_audio(self):
        from core.voice_proxy import QuotaLimitReached

        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                if getattr(self, '_force_reconnect', False):
                    self._force_reconnect = False
                    raise Exception("Forced Reconnect")
                
                async for response in self.session.receive():
                    if getattr(self, '_force_reconnect', False):
                        self._force_reconnect = False
                        raise Exception("Forced Reconnect")

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Keep Gemini packets intact — slicing caused extra queue
                            # hops and made slow Kurdish streams sound choppy.
                            self.audio_in_queue.put_nowait(response.data)

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = _join_transcript(in_buf)
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                                yt_q = _youtube_query_from_text(full_in)
                                if yt_q:
                                    asyncio.create_task(self._local_youtube_play(yt_q))
                            in_buf = []

                            full_out = _join_transcript(out_buf)
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_realtime_input(
                                    video=types.Blob(data=img_b, mime_type=mime_t or "image/jpeg")
                                )
                                await self._send_text(question)
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())
                            elif self._hand_vision_pending:
                                self._hand_vision_pending = False
                                self._vision_busy = False

                    if response.tool_call:
                        fn_responses = []
                        self._last_tool_call_time = time.monotonic()
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except QuotaLimitReached as e:
            print("[JARVIS] Daily voice quota reached — shutting down gracefully")
            await self._handle_quota_exceeded(e)
            raise
        except Exception as e:
            if getattr(e, "code", None) == 429:
                print("[JARVIS] Daily voice quota reached — shutting down gracefully")
                await self._handle_quota_exceeded(QuotaLimitReached(str(e)))
                raise QuotaLimitReached(str(e)) from e
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        # Higher latency + larger blocks = fewer underruns (choppy Kurdish speech)
        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=4096,
            latency="high",
        )
        stream.start()

        # ~350 ms prebuffer at 24 kHz / 16-bit mono before first write of a turn
        PREBUFFER = 16800
        buf = bytearray()
        speaking = False

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.12 if speaking else 0.25,
                    )
                    if self._interrupted:
                        buf.clear()
                        speaking = False
                        self.set_speaking(False)
                        continue

                    buf.extend(chunk)
                    while True:
                        try:
                            buf.extend(self.audio_in_queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break

                    # Prebuffer at start of utterance so PortAudio doesn't underrun
                    if not speaking:
                        turn_done = (
                            self._turn_done_event is not None
                            and self._turn_done_event.is_set()
                        )
                        if len(buf) < PREBUFFER and not turn_done:
                            continue
                        speaking = True
                        self.set_speaking(True)

                    to_write = bytes(buf)
                    buf.clear()
                    try:
                        await asyncio.to_thread(stream.write, to_write)
                    except (RuntimeError, asyncio.CancelledError):
                        break

                except asyncio.TimeoutError:
                    # Flush leftover audio, then mark end of speech
                    if buf and not self._interrupted:
                        try:
                            await asyncio.to_thread(stream.write, bytes(buf))
                        except (RuntimeError, asyncio.CancelledError):
                            break
                        buf.clear()
                    if (
                        speaking
                        and self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        speaking = False
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    def _on_scrape_url(self, url: str) -> None:
        url = (url or "").strip()
        print(f"[Scraper] received URL: {url}")
        if not url:
            try:
                self.ui.hide_scraper_portal()
            except Exception:
                pass
            return

        try:
            self.ui.scraper_set_status("ACQUIRING")
        except Exception:
            pass
        self.ui.write_log(f"SYS: Scraping {url} ...")

        def _bg_scrape():
            try:
                data = scrape_website(url)
            except Exception as e:
                try:
                    self.ui.scraper_set_status("FAILED")
                except Exception:
                    pass
                print(f"[Scraper] failed: {e}")
                self.ui.write_log(f"SYS: Scrape failed: {e}")
                time.sleep(3.0)
                try:
                    self.ui.hide_scraper_portal()
                except Exception:
                    pass
                return

            self._last_scrape_folder = data["folder"]
            remember_scrape_folder(data["folder"])

            tree = "\n".join(data.get("tree") or [])
            preview = ""
            try:
                idx = Path(data["folder"]) / "index.html"
                if idx.is_file():
                    preview = idx.read_text(encoding="utf-8", errors="replace")[:8000]
            except Exception:
                preview = ""

            try:
                self.ui.scraper_show_sources(data["folder"], tree, preview)
                self.ui.show_content(
                    "SOURCE LOCKED",
                    f"{data['url']}\n{data['folder']}\n\n{tree[:3500]}",
                )
            except Exception:
                pass

            self.ui.write_log(f"SYS: Scrape saved to {Path(data['folder']).name}")
            time.sleep(2.0)
            try:
                self.ui.hide_scraper_portal()
            except Exception:
                pass

            # Force the AI to speak by injecting a system turn
            msg = (
                "[SYSTEM_ALERT] SCRAPE COMPLETE.\n"
                f"Folder: {Path(data['folder']).name}\n"
                "You MUST speak right now! Say EXACTLY this in Sorani (very calm):\n"
                "«تەواو گەورەم، وێبسایتەکە بە سەرکەوتوویی بردرا. ئەتەوێ ئێستا ئیشی پێ بکەین؟»\n"
                "Do NOT add anything else. Do NOT read the HTML. Do NOT run the site. Wait for my next command."
            )
            if self._loop and self.session:
                asyncio.run_coroutine_threadsafe(self._send_text(msg), self._loop)

        threading.Thread(target=_bg_scrape, daemon=True).start()

    def _run_website_scraper(self, args: dict) -> str:
        """Step 1: open portal only. Scrape runs in background."""
        url = ((args or {}).get("url") or "").strip()
        if url in ("", "none", "null", "undefined"):
            url = ""

        try:
            self.ui.show_scraper_portal()
        except Exception:
            return "Scraper portal unavailable."

        if url:
            # If AI already extracted a URL, just trigger it
            self._on_scrape_url(url)
            return "Scraping started in background. Tell the user you are scraping it, then wait."

        return (
            "Portal opened on screen. You MUST speak now:\n"
            "Say EXACTLY: «بەشی بردنی وێبسایت ئامادەیە. تکایە لینکەکە دابنێ گەورەم.»\n"
            "Then STOP your turn. Do NOT call this tool again. The system will handle the rest."
        )

    def _run_scraped_website(self, folder: str | None = None) -> str:
        """Step 2: run only when user explicitly asks."""
        result = run_scraped_site(folder)
        if result.startswith("TERMINAL LAUNCHED"):
            self.ui.write_log("SYS: Scraped site launched in terminal.")
        return result

    async def _send_startup_briefing(self) -> None:
        """Startup greeting: scripted Sorani introduction."""
        await asyncio.sleep(0.1)
        if not self.session:
            return

        p = (
            "[STARTUP_BRIEFING]\n"
            "Speak ONLY in fluent Kurdish Sorani. CALM, SLOW pace.\n"
            "Say EXACTLY this — word for word, do NOT add or remove anything:\n"
            "«سڵاو سەرۆک، من ناوم جارڤسە بە ڤێرژنی کوردی، "
            "لەلایەن ڕەوە دێڤەوە دروستکراوم بۆ خزمەتکردنی ئێوەی بەڕێز، "
            "بەڵام من یاساکانم دەستکاری کراوە و ڤێرژنێکی تەواو جیاوازم! "
            "تکایە چۆن هاوکاریت بکەم؟»\n"
            "No tools. No browser. No news. Stop after that line."
        )
        if self._turn_done_event:
            self._turn_done_event.clear()
        await self._send_text(p)
        self.ui.write_log("SYS: Startup greeting sent.")

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "Kurdish Sorani"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from actions.gemini_text import generate_text
            resp = await asyncio.to_thread(generate_text, prompt)
            summary = (resp.text or "").strip()
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            if not self._session_started:
                continue
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self._send_text(alert)
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session and self._session_started:
                # Don't interrupt if user spoke recently or JARVIS is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "Kurdish Sorani"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self._send_text(msg)
                            self.ui.write_log(f"SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._session_started:
                continue
            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self._send_text(prompt)
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self._send_text(text)
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        from core.voice_api import VoiceAPI, VoiceAPIError, get_access_token

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        voice_api = VoiceAPI()
        from core.voice_proxy import QuotaLimitReached

        def _on_quota(msg: dict) -> None:
            rem = msg.get("remaining_seconds", 0)
            used = msg.get("used_seconds", 0)
            lim = msg.get("limit_seconds", 300)
            resets = msg.get("resets_at", "")

            if rem in (60, 30, 15, 10, 5, 0):
                self.ui.write_log(f"SYS: Voice quota {used}/{lim}s ({rem}s left today)")

            for threshold in (60, 30, 10):
                if rem <= threshold and threshold not in self._quota_warned:
                    self._quota_warned.add(threshold)
                    if self._loop and self.session:
                        asyncio.run_coroutine_threadsafe(
                            self._send_quota_warning(rem, resets), self._loop
                        )
                    break

        while True:
            try:
                token = get_access_token()
                if not token:
                    self.ui.write_log("ERR: Not signed in — please log in again.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    continue

                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                config = self._build_config()
                from core.voice_proxy import ProxyLiveSession, live_config_to_setup, QuotaLimitReached

                setup_payload = live_config_to_setup(
                    LIVE_MODEL, config, tool_declarations=TOOL_DECLARATIONS
                )

                setup_inner = setup_payload.get("setup")
                loop = asyncio.get_event_loop()
                session = None
                start_info = None
                proxy = None

                for attempt, use_preload in enumerate((True, False)):
                    try:
                        start_info = await loop.run_in_executor(
                            None,
                            lambda preload=use_preload: voice_api.live_start(
                                token,
                                setup=setup_inner if preload else None,
                            ),
                        )
                    except VoiceAPIError as e:
                        if e.status == 429:
                            detail = e.detail if isinstance(e.detail, dict) else {}
                            await self._handle_quota_exceeded(
                                QuotaLimitReached(
                                    "Daily voice limit reached.",
                                    resets_at=detail.get("resets_at", ""),
                                )
                            )
                            await asyncio.sleep(self._conn_backoff)
                            break
                        if e.status == 503:
                            self.ui.write_log("SYS: Voice service temporarily disabled.")
                            self.ui.set_state("SLEEPING")
                            await asyncio.sleep(60)
                            break
                        if e.status == 401:
                            self.ui.write_log("ERR: Session expired — please sign in again.")
                            self.ui.prompt_reconfig()
                            while not self.ui._win._ready:
                                await asyncio.sleep(1)
                            break
                        raise

                    setup_ok = bool(start_info.get("setup_accepted")) and use_preload
                    live_sid = start_info.get("live_session_id") if use_preload else None
                    ws_url = voice_api.ws_url(
                        token,
                        start_info.get("ws_path"),
                        live_session_id=live_sid,
                    )

                    if use_preload and setup_ok:
                        self.ui.write_log("SYS: Voice tools registered on server.")
                    elif not use_preload:
                        self.ui.write_log("SYS: Connecting with WebSocket setup (fallback).")

                    try:
                        proxy = ProxyLiveSession(
                            ws_url,
                            LIVE_MODEL,
                            config,
                            tool_declarations=TOOL_DECLARATIONS,
                            setup_preloaded=setup_ok,
                            access_token=token,
                            on_quota=_on_quota,
                        )
                        session = await proxy.__aenter__()
                        break
                    except RuntimeError as e:
                        print(f"[JARVIS] Live connect failed (preload={use_preload}): {e}")
                        if proxy is not None:
                            try:
                                await proxy.__aexit__(None, None, None)
                            except Exception:
                                pass
                            proxy = None
                        if use_preload:
                            self.ui.write_log("SYS: Preloaded setup failed — retrying…")
                            continue
                        raise

                if session is None or start_info is None:
                    continue

                try:
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False
                    self._quota_warned         = set()

                    rem = start_info.get("remaining_seconds")
                    if rem is not None:
                        self.ui.write_log(f"SYS: Voice quota — {rem}s remaining today.")

                    print("[JARVIS] Connected.")
                    if self._session_started and not self.ui.muted:
                        self.ui.set_state("LISTENING")
                    else:
                        self.ui.set_state("SLEEPING")
                    self.ui.write_log(
                        "SYS: JARVIS online — standby. Press START or Space to begin."
                        if not self._session_started
                        else "SYS: JARVIS online."
                    )

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    try:
                        async with asyncio.TaskGroup() as tg:
                            tg.create_task(self._send_realtime())
                            tg.create_task(self._listen_audio())
                            tg.create_task(self._receive_audio())
                            tg.create_task(self._play_audio())
                            tg.create_task(self._run_system_monitor())
                            tg.create_task(self._run_background_monitor())
                            tg.create_task(self._run_proactive_mode())
                            if self._dashboard:
                                tg.create_task(self._relay_phone_audio())
                    except* QuotaLimitReached:
                        pass
                finally:
                    if session is not None:
                        await proxy.__aexit__(None, None, None)
                    self.session = None

                    # Briefing is MANUAL only — wait for Start / Space (see _on_start)

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except QuotaLimitReached:
                pass
            except BaseException as e:
                if isinstance(e, ExceptionGroup) and all(
                    isinstance(ex, QuotaLimitReached) for ex in e.exceptions
                ):
                    pass
                else:
                    err_str = str(e)
                    print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                    traceback.print_exc()

                    code = getattr(e, "code", 0)
                    if code == 429 or "Daily voice limit" in err_str:
                        self.ui.write_log("SYS: Daily voice limit reached.")
                        self.ui.set_state("SLEEPING")
                        self._conn_backoff = 300
                    elif code in (503, 409) or "temporarily disabled" in err_str.lower():
                        self.ui.write_log("SYS: Voice service unavailable — retrying…")
                        self.ui.set_state("SLEEPING")
                        self._conn_backoff = 60
                    elif code == 401 or "Unauthorized" in err_str:
                        self.ui.write_log("ERR: Please sign in again.")
                        self.ui.set_state("SLEEPING")
                        self.ui.prompt_reconfig()
                        while not self.ui._win._ready:
                            await asyncio.sleep(1)
                        _conn_backoff = 3
                        continue

                    # Network / timeout errors — log clearly and back off
                    is_net_err = any(k in err_str for k in (
                        "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                        "ConnectionRefusedError", "OSError", "Cannot connect",
                        "WebSocket closed",
                    ))
                    if is_net_err:
                        _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                        self._conn_backoff = _conn_backoff
                        self.ui.write_log(
                            f"NET: Bağlantı kurulamadı — {_conn_backoff}s sonra tekrar deneniyor. "
                            "(VPN gerekiyor olabilir)"
                        )
                    else:
                        self._conn_backoff = 3
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            if delay >= 300:
                print("[JARVIS] Daily voice limit — waiting for quota reset…")
            else:
                print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_auth()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()