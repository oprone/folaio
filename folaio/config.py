"""Paths, hardware detection and the catalog of Writer models."""
from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

APP_NAME = "Folaio"
ROOT = Path(__file__).resolve().parent.parent


OLD_NAME = "HeraAI"   # the app's name before it became Folaio


def data_dir() -> Path:
    """Where Folaio keeps models and the user's library.

    Portable mode: if a folder called ``Folaio-data`` sits next to the app,
    everything lives there (so the whole brain can be carried on a USB drive).
    Data from before the rename (a "HeraAI" folder) is moved over automatically.
    """
    if env := os.environ.get("FOLAIO_HOME") or os.environ.get("HERAAI_HOME"):
        return _ready(Path(env))
    portable, old_portable = ROOT / f"{APP_NAME}-data", ROOT / f"{OLD_NAME}-data"
    if portable.is_dir() or old_portable.is_dir():
        return _ready(portable, old_portable)
    if sys.platform == "darwin":
        parent = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        parent = Path(os.environ.get("APPDATA", Path.home()))
    else:
        parent = Path.home() / ".local" / "share"
    return _ready(parent / APP_NAME, parent / OLD_NAME)


def _ready(base: Path, old: Path | None = None) -> Path:
    if old is not None and not base.exists() and old.is_dir():
        try:
            old.rename(base)
        except OSError:
            return _ready(old)   # couldn't move it: keep using the old folder
    base.mkdir(parents=True, exist_ok=True)
    return base


DATA = data_dir()
LEGACY_MODELS_DIR = DATA / "models"    # where brains were kept before v0.2


def _default_models_dir() -> Path:
    """Brains live in a "brains" folder inside Folaio's own folder.
    (With FOLAIO_HOME set, inside that folder instead; if Folaio's folder is
    read-only, e.g. an installed app, in the data folder.)"""
    folder = DATA / "brains" if os.environ.get("FOLAIO_HOME") else ROOT / "brains"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return folder
    except OSError:
        return DATA / "brains"


DEFAULT_MODELS_DIR = _default_models_dir()   # the user can choose another folder for brains
MIND_DIR = DATA / "mind"          # what Folaio Core has learned
LIBRARY_DIR = DATA / "library"
FILES_DIR = LIBRARY_DIR / "files"
INBOX_DIR = LIBRARY_DIR / "inbox"  # watch folder: drop PDFs here
DB_PATH = LIBRARY_DIR / "folaio.db"
if not DB_PATH.exists() and (LIBRARY_DIR / "heraai.db").exists():   # named before the rename
    for _suffix in ("", "-wal", "-shm"):
        _old = LIBRARY_DIR / f"heraai.db{_suffix}"
        if _old.exists():
            _old.rename(LIBRARY_DIR / f"folaio.db{_suffix}")
for _d in (DEFAULT_MODELS_DIR, MIND_DIR, FILES_DIR, INBOX_DIR):
    _d.mkdir(parents=True, exist_ok=True)

@dataclass(frozen=True)
class WriterSpec:
    key: str
    label: str
    file: str
    url: str
    size_bytes: int
    min_ram_gb: float
    license: str
    quizzes: bool = True  # tiny brains make unreliable answer keys
    name: str = ""        # friendly name shown to users
    blurb: str = ""

    @property
    def path(self) -> Path:
        return models_dir() / self.file


_HF = "https://huggingface.co/bartowski"
# Only very small brains, so Folaio stays light. Both Apache-2.0 (fine to ship).
# Tiny models write unreliable quiz answer keys, so Folaio Core makes the quizzes.
WRITERS: list[WriterSpec] = [
    WriterSpec("mini", "SmolLM2 360M Instruct", "SmolLM2-360M-Instruct-Q4_K_M.gguf",
               f"{_HF}/SmolLM2-360M-Instruct-GGUF/resolve/main/SmolLM2-360M-Instruct-Q4_K_M.gguf",
               270_590_880, 0, "Apache-2.0", quizzes=False,
               name="Mini", blurb="Smallest and fastest. Works on any computer."),
    WriterSpec("small", "Qwen2.5 0.5B Instruct", "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf",
               f"{_HF}/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf",
               397_808_192, 4, "Apache-2.0", quizzes=False,
               name="Small", blurb="A little smarter. For computers with 4 GB RAM or more."),
]


def writer_by_key(key: str) -> WriterSpec:
    return next(w for w in WRITERS if w.key == key)


def hardware() -> dict:
    ram_gb = psutil.virtual_memory().total / 1024**3
    return {
        "os": {"Darwin": "macOS"}.get(platform.system(), platform.system()),
        "arch": platform.machine(),
        "ram_gb": round(ram_gb, 1),
        "cpu_cores": psutil.cpu_count(logical=False) or os.cpu_count() or 2,
    }


def recommended_writer() -> WriterSpec:
    """Largest model that fits comfortably in this machine's RAM."""
    ram = hardware()["ram_gb"]
    fitting = [w for w in WRITERS if ram >= w.min_ram_gb]
    return fitting[-1]


def models_dir() -> Path:
    """Where Plus brains are kept: the folder the user chose, or the default."""
    custom = load_settings().get("models_dir")
    return Path(custom) if custom else DEFAULT_MODELS_DIR


def move_legacy_brains() -> None:
    """One-time move of brains from the old default folder to the new one."""
    if load_settings().get("models_dir") or not LEGACY_MODELS_DIR.is_dir():
        return
    import shutil
    for f in LEGACY_MODELS_DIR.glob("*.gguf"):
        target = DEFAULT_MODELS_DIR / f.name
        if not target.exists():
            shutil.move(str(f), str(target))
    for f in LEGACY_MODELS_DIR.glob("*.part"):
        f.unlink(missing_ok=True)
    try:
        LEGACY_MODELS_DIR.rmdir()   # only if now empty
    except OSError:
        pass


def downloaded_writers() -> list[WriterSpec]:
    return [w for w in WRITERS if w.path.exists()]


def active_writer() -> WriterSpec | None:
    """The Plus brain in use: the one the user picked, else the biggest downloaded.
    None when Plus is switched off or nothing is downloaded."""
    choice = load_settings().get("brain")
    if choice == "off":
        return None
    have = downloaded_writers()
    return next((w for w in have if w.key == choice), have[-1] if have else None)


def disk_free_gb() -> float:
    import shutil
    folder = models_dir()
    return round(shutil.disk_usage(folder if folder.is_dir() else DATA).free / 1e9, 1)


SETTINGS_PATH = DATA / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(**changes) -> dict:
    settings = load_settings() | changes
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    return settings
