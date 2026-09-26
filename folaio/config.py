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
    """Where Folaio keeps everything: a "data" folder inside Folaio's own folder,
    so the whole app, with its library and brains, can be copied or carried on a
    USB drive. Data from older locations is moved in automatically.

    FOLAIO_HOME overrides it (used for tests). If Folaio's folder can't be written
    to (e.g. an installed app), the computer's usual app-data folder is used.
    """
    if env := os.environ.get("FOLAIO_HOME") or os.environ.get("HERAAI_HOME"):
        return _ready(Path(env))
    if sys.platform == "darwin":
        system = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        system = Path(os.environ.get("APPDATA", Path.home()))
    else:
        system = Path.home() / ".local" / "share"
    local = ROOT / "data"
    try:
        local.mkdir(exist_ok=True)
        probe = local / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError:
        return _ready(system / APP_NAME, system / OLD_NAME)
    older = [ROOT / f"{APP_NAME}-data", ROOT / f"{OLD_NAME}-data", system / APP_NAME, system / OLD_NAME]
    return _ready(local, *older)


def _ready(base: Path, *older: Path) -> Path:
    """Use `base`; if it's still empty, first move in the newest older data folder found."""
    empty = not base.exists() or not any(p.name != ".DS_Store" for p in base.iterdir())
    old = next((o for o in older if o.is_dir() and any(o.iterdir())), None)
    if empty and old is not None:
        import shutil
        try:
            base.mkdir(parents=True, exist_ok=True)
            for item in list(old.iterdir()):
                shutil.move(str(item), str(base / item.name))   # works across drives too
            old.rmdir()
        except OSError:
            pass   # anything not moved stays where it was; nothing is deleted
    base.mkdir(parents=True, exist_ok=True)
    return base


DATA = data_dir()
# Where brains were kept by earlier versions: moved into data/brains on start.
LEGACY_MODELS_DIRS = (DATA / "models", ROOT / "brains")


def _default_models_dir() -> Path:
    """Brains live in data/brains (the user can choose another folder)."""
    folder = DATA / "brains"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


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
    """One-time move of brains from the folders earlier versions used."""
    if load_settings().get("models_dir"):
        return   # the user chose their own folder: leave it alone
    import shutil
    for old in LEGACY_MODELS_DIRS:
        if not old.is_dir() or old.resolve() == DEFAULT_MODELS_DIR.resolve():
            continue
        for f in old.glob("*.gguf"):
            target = DEFAULT_MODELS_DIR / f.name
            if not target.exists():
                shutil.move(str(f), str(target))
        for f in old.glob("*.part"):
            f.unlink(missing_ok=True)
        (old / ".DS_Store").unlink(missing_ok=True)
        try:
            old.rmdir()   # only if now empty
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
