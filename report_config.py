# ==============================================================================
# VPIN REPORTS - CONFIGURATION
# ==============================================================================
# Settings for build_report.py (and history_report.py). This repo is standalone;
# it only reads the PinUP Popper database, VPinPerfMon logs, and (optionally)
# the Vpin-AI-Recommender's ai_recommendations.json.
# ==============================================================================

import os

# PinUp Popper database to read (CustomGameLog for history, CustomPerfStats for perf).
# Relative paths resolve against this folder.
DB_PATH = r"C:\vPinball\PinUPSystem\PUPDatabase.db"

# Emulator IDs included in BOTH tabs (comma-separated)
TARGET_EMU_IDS = "4,6,10,1001,1002,1006,1010"

# Chart dot color per emulator ID (keys are the IDs above). Emulators without an
# entry fall back to the report's default palette.
EMU_COLORS = {
    4: "#2769a0",     # blue
    6: "#2f9e5b",     # green
    10: "#4fd1c5",    # cyan
    1001: "#ecc94b",  # yellow
    1002: "#f37712",  # orange
    1006: "#9f7aea",  # purple
    1010: "#d71919",  # red
}

# Outline color for tables matching the free-form search box on the Performance tab
SEARCH_HIGHLIGHT_COLOR = "#aeaeae"

# Play History window, in days
HISTORY_DAYS = 365

# AI recommendations JSON (from Vpin_Recommender.py in the Vpin-AI-Recommender repo).
# The "AI Recommendations" tab is added only if this file exists. Default assumes
# both repos are checked out side by side (e.g. C:\projects\Vpin-Reports and
# C:\projects\Vpin-AI-Recommender).
RECS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Vpin-AI-Recommender", "ai_recommendations.json")

# Wheel images on the AI Recommendations tab are found via Games.EMUID -> Emulators.DirMedia\Wheel
# and embedded (downscaled) so the HTML stays portable.
WHEEL_WIDTH = 150  # stored size; displayed at 75px so it stays sharp on high-DPI screens

# VPinPerfMon logs folders (PresentMon_*.csv / .txt) for the Performance tab's run drill-down.
# Accepts a single path or a list: the first is the primary folder checked at build time;
# additional folders are shown with Copy path buttons for use on other computers.
# None or [] = use the folder recorded in the database (CustomPerfStats.FileRawDataPath).
# Example:
# PERF_LOGS_DIR = [r"C:\vPinball\mytools\VPinPerfMon\logs", r"\\cabinet\VPinPerfMon\logs"]
PERF_LOGS_DIR = None

# Optional: use a pre-extracted CustomPerfStats CSV for the Performance tab
# instead of querying DB_PATH (EMU filter is not applied to CSVs). None = use DB.
PERF_CSV = None
