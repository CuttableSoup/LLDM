"""!
@file Ollama_Launcher.py
@brief Best-effort local Ollama server bootstrap, called once from LLDM.py's own main() before
    anything talks to the LLM. Matches the rest of the app's "the local LLM is best-effort,
    never blocks core gameplay" posture (see NPC_Generation.py/AdHoc_Generation.py's own module
    notes): if Ollama is already running, this is a fast no-op; if a real install exists on
    PATH but isn't running, this starts it; and if Ollama isn't installed anywhere at all, this
    downloads the official portable Windows build straight from Ollama's own GitHub releases
    into vendor/ollama/ (gitignored -- see CLAUDE.md's own note on why this is never committed)
    and runs it from there, so a fresh clone can go from nothing to a working local LLM with no
    manual install step. Either way, once a server is reachable, this also makes sure the actual
    model LLM_Core.py/LLM_Client.py are going to ask for is pulled -- a chat completion against
    an unpulled model name 404s outright rather than falling back to whatever happens to be
    loaded (see CLAUDE.md's "LLM integration"), so "Ollama is running" alone doesn't mean
    narration will actually work. Any failure along the way (no network, a failed checksum, an
    unwritable vendor/ directory, the server never coming up, a failed pull) just logs and lets
    the app continue exactly as if the user were expected to set this up by hand -- narration
    already degrades gracefully to "Could not connect to the local LLM"
    (LLM_Core.py's own _fetch_and_publish) or a 404 when nothing/the wrong thing answers.

    Windows-only by design (ollama.exe, the win_amd64 release asset, CREATE_NO_WINDOW) --
    matches this project's own current platform (see CLAUDE.md's "Platform: win32"). A second
    platform would need its own asset name/executable name, not a switch bolted onto this one.
"""

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
import zipfile

from paths import PROJECT_ROOT

OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_EXECUTABLE_NAME = "ollama.exe"
# Mirrors LLM_Client.py's own DEFAULT_MODEL / LLM_Core.py's own self.model -- three independent
# "gemma4" defaults, deliberately duplicated rather than shared (the same intentional non-
# sharing _save_slot_dir's own module note documents elsewhere in this codebase), so keep this
# in sync by hand if either of those ever changes.
DEFAULT_MODEL = "gemma4"
# How long to wait for a server (freshly spawned, or already running) to actually answer before
# giving up on checking/pulling a model -- a fresh `ollama serve` process typically binds its
# port within a couple seconds, so this is a generous ceiling, not an expected wait.
DEFAULT_READY_TIMEOUT = 15

# vendor/ never ships in the repo (see .gitignore) -- exactly like Saves/, it's local, per-
# machine state, just for a downloaded binary instead of a save file. Resolved relative to the
# project root (not cwd), the same reasoning DM_Persistence.py's _save_slot_dir already follows
# for Saves/.
VENDOR_DIR = os.path.join(PROJECT_ROOT, "vendor", "ollama")

_RELEASE_BASE = "https://github.com/ollama/ollama/releases/latest/download"
_ASSET_NAME = "ollama-windows-amd64.zip"
_CHECKSUMS_NAME = "sha256sum.txt"
_USER_AGENT = "LLDM-Ollama-Launcher"

# Spawning a console window for a background server would be visible/surprising behind LLDM's
# own Tkinter GUI -- this suppresses it.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _default_is_reachable(host):
    try:
        urllib.request.urlopen(f"{host}/v1/models", timeout=1)
        return True
    except Exception:
        return False


def _default_list_models(host):
    """!
    @brief Default list_models seam -- GET host/api/tags, Ollama's own native (non-OpenAI-
        compat) model listing. Each entry's own "name" already carries its tag (ex:
        "gemma4:latest"), which is what _model_already_pulled compares against.
    @return A list of pulled model name strings.
    """
    with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
    return [entry.get("name", "") for entry in data.get("models", [])]


def _model_already_pulled(model, pulled_names):
    """!
    @brief model may be given bare ("gemma4") or already tagged ("gemma4:latest") -- Ollama's
        own /api/tags listing always includes a tag, defaulting to ":latest" when none was
        specified at pull time, so a bare request name has to also match its own implicit
        ":latest" form, not just an exact string match.
    """
    if model in pulled_names:
        return True
    if ":" not in model:
        return f"{model}:latest" in pulled_names
    return False


def _default_pull_model(host, model, log):
    """!
    @brief Default pull_model seam -- POST host/api/pull with stream=True, relaying Ollama's
        own NDJSON status lines through log. Ollama reports fine-grained byte-level progress
        (a new "completed"/"total" pair essentially every chunk) -- logged at most every 10%
        per distinct "status" phase (ex: "pulling <digest>", "verifying sha256 digest") so this
        reads as a progress bar, not a flood.
    @raises Exception on any network failure, or RuntimeError if Ollama itself reports an
        "error" (ex: an unknown model name) -- the caller (_ensure_model_pulled) catches
        broadly, matching every other best-effort failure mode in this module.
    """
    request = urllib.request.Request(
        f"{host}/api/pull",
        data=json.dumps({"model": model, "stream": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    last_status = None
    last_percent = -10
    with urllib.request.urlopen(request, timeout=60) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue

            if payload.get("error"):
                raise RuntimeError(payload["error"])

            status = payload.get("status", "")
            if status != last_status:
                log(status)
                last_status = status
                last_percent = -10

            total = payload.get("total")
            completed = payload.get("completed")
            # Some status lines (ex: the initial "pulling <digest>" line for a new layer) carry
            # "total" before "completed" has appeared at all -- guard both, not just total, or
            # `None * 100` blows up before the very first real progress update for a layer.
            if total and completed is not None:
                percent = completed * 100 // total
                if percent >= last_percent + 10:
                    log(f"{status}: {percent}%")
                    last_percent = percent


def _wait_until_reachable(host, is_reachable, ready_timeout, poll_interval=0.5):
    """!
    @brief Polls is_reachable(host) until it's True or ready_timeout elapses -- checks once
        before ever sleeping, so ready_timeout=0 still performs exactly one check (what tests
        that don't care about model-pulling pass, to skip this instantly rather than actually
        sleeping).
    @return True once reachable, False if ready_timeout elapsed first.
    """
    deadline = time.monotonic() + ready_timeout
    while True:
        if is_reachable(host):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def _ensure_model_pulled(host, model, log, is_reachable, list_models, pull_model, ready_timeout):
    """!
    @brief Makes sure model is actually pulled into this Ollama instance -- see this module's
        own docstring for why "Ollama is running" alone doesn't guarantee that. Waits up to
        ready_timeout for the server to answer at all first (needed whether it was already
        running or was just spawned moments ago by this same call -- a freshly-spawned process
        needs a moment to bind its own port before /api/tags means anything), then checks
        list_models and, only if model isn't listed, calls pull_model. Best-effort throughout:
        any failure (server never comes up, network error mid-pull, an unknown model name) just
        logs and gives up -- never raises.
    """
    if not _wait_until_reachable(host, is_reachable, ready_timeout):
        log(f"Ollama never became reachable; skipping the check for model \"{model}\".")
        return

    try:
        pulled = list_models(host)
    except Exception as e:
        log(f"Could not check which models are already pulled: {e}")
        return

    if _model_already_pulled(model, pulled):
        log(f"Model \"{model}\" already pulled.")
        return

    log(f"Model \"{model}\" not found locally; pulling it now (this may take a while)...")
    try:
        pull_model(host, model, log)
    except Exception as e:
        log(f"Failed to pull model \"{model}\": {e}")
        return

    log(f"Model \"{model}\" pulled successfully.")


def _real_fetch_text(url):
    """!
    @brief Default fetch_text seam -- a small (~1KB) plain-text GET, used only for
        sha256sum.txt. Kept separate from _real_download (which streams to disk with progress
        logging) since this result is read into memory and parsed, not saved.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8", errors="replace")


def _real_download(url, dest_path, log):
    """!
    @brief Default download seam -- streams url to dest_path in 1MB chunks, logging progress
        roughly every 10% (the release zip is well over a gigabyte -- see this module's own
        docstring -- so a silent multi-minute wait here would look identical to a hung process).
    @raises Exception on any network/write failure -- the caller (_install_vendored_ollama)
        catches broadly and treats this the same as every other best-effort failure mode.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        total = int(response.headers.get("Content-Length") or 0)
        written = 0
        last_reported = -10
        with open(dest_path, "wb") as f:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if total:
                    percent = written * 100 // total
                    if percent >= last_reported + 10:
                        log(
                            f"Downloading Ollama... {percent}% "
                            f"({written // (1024 * 1024)}MB / {total // (1024 * 1024)}MB)"
                        )
                        last_reported = percent


def _sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_checksum(checksums_text, asset_name):
    """!
    @brief Finds asset_name's own hash in sha256sum.txt's own `<hash>  ./<filename>` format
        (two spaces, a "./" prefix on the filename -- confirmed against the real file at
        github.com/ollama/ollama/releases/latest/download/sha256sum.txt).
    @return The expected hex digest, or None if asset_name isn't listed.
    """
    for line in checksums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and os.path.basename(parts[1]) == asset_name:
            return parts[0]
    return None


def _find_executable(root_dir, name=OLLAMA_EXECUTABLE_NAME):
    """!
    @brief Searches root_dir for name -- not assumed to sit at any particular depth, since
        Ollama's own zip layout (flat vs. a nested folder) isn't part of any stability
        guarantee this module can rely on.
    @return The full path if found, else None.
    """
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        if name in filenames:
            return os.path.join(dirpath, name)
    return None


def _install_vendored_ollama(vendor_dir, download, fetch_text, log):
    """!
    @brief One-time download + verify + extract of Ollama's own official portable Windows
        build into vendor_dir. Blocking (unlike ensure_ollama_running's own otherwise
        fire-and-forget posture) -- this only ever runs once per machine (every later call
        finds the already-extracted executable via _find_executable first and never reaches
        here again), so a first-run delay of however long the download takes is an accepted,
        one-time cost rather than a per-launch one.
    @param vendor_dir Where to download/extract into (created if missing).
    @param download/fetch_text The injected download/fetch_text seams (see
        ensure_ollama_running's own docstring for why these are seams at all).
    @param log Status callable.
    @return The installed executable's full path, or None on any failure -- network error,
        checksum mismatch, corrupt/unexpected zip contents. Never raises.
    """
    log("Ollama not found locally; downloading the official Windows build (one-time, ~1.5GB)...")
    os.makedirs(vendor_dir, exist_ok=True)
    zip_path = os.path.join(vendor_dir, _ASSET_NAME)

    try:
        download(f"{_RELEASE_BASE}/{_ASSET_NAME}", zip_path, log)
    except Exception as e:
        log(f"Failed to download Ollama: {e}")
        return None

    expected = None
    try:
        expected = _expected_checksum(fetch_text(f"{_RELEASE_BASE}/{_CHECKSUMS_NAME}"), _ASSET_NAME)
    except Exception as e:
        log(f"Could not fetch Ollama's own checksums to verify the download: {e}")

    if expected is None:
        log("No checksum available; proceeding without verifying the download.")
    elif _sha256_of(zip_path) != expected:
        log("Downloaded Ollama archive failed checksum verification; discarding it.")
        os.remove(zip_path)
        return None
    else:
        log("Checksum verified.")

    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(vendor_dir)
    except Exception as e:
        log(f"Failed to extract Ollama archive: {e}")
        return None
    finally:
        os.remove(zip_path)

    executable = _find_executable(vendor_dir)
    if not executable:
        log("Extracted Ollama archive did not contain ollama.exe.")
        return None

    log(f"Ollama installed to {executable}.")
    return executable


def _resolve_ollama_executable(which, vendor_dir, download, fetch_text, log):
    """!
    @brief Picks which ollama.exe to run, preferring a real system install over a vendored one
        so a user who later installs Ollama for real transparently takes over from a vendored
        copy this module downloaded earlier -- both are checked fresh on every call, never
        cached past one process's own lifetime.
    @return A full executable path, or None if nothing exists and installing one failed too.
    """
    system_path = which("ollama")
    if system_path:
        return system_path

    vendored = _find_executable(vendor_dir)
    if vendored:
        return vendored

    return _install_vendored_ollama(vendor_dir, download, fetch_text, log)


def ensure_ollama_running(
    host=OLLAMA_HOST, model=DEFAULT_MODEL, log=None, is_reachable=None, which=shutil.which,
    popen=subprocess.Popen, download=None, fetch_text=None, vendor_dir=VENDOR_DIR,
    list_models=None, pull_model=None, ready_timeout=DEFAULT_READY_TIMEOUT,
):
    """!
    @brief Starts a local Ollama server if nothing's already listening at host -- installing one
        first (see _install_vendored_ollama) if neither a system install nor an earlier vendored
        one can be found -- then makes sure model is actually pulled into it (see
        _ensure_model_pulled). The server spawn itself is fire-and-forget about becoming ready
        (NLPCore's own sentence-transformers model load, LLDM.py's very next boot step, ~15-20s,
        already gives a freshly-spawned Ollama plenty of time to come up on its own); the
        model check right after it, by contrast, does wait (up to ready_timeout) for the server
        to actually answer, since there's no way to know what's pulled -- let alone pull
        something missing -- without talking to it. The one-time *install* step also blocks
        (see _install_vendored_ollama's own docstring) -- there's no equivalent "just try again
        later" fallback for a binary that doesn't exist on disk yet.
    @param host Ollama's own base URL (its OpenAI-compat API, not the native one).
    @param model The model tag to make sure is pulled (default DEFAULT_MODEL, "gemma4" --
        matching LLM_Client.py/LLM_Core.py's own defaults, see this module's own note on why
        that's duplicated rather than imported).
    @param log Optional callable(message) for status updates -- defaults to a no-op so this
        stays callable/testable with no EventBus/print side effects.
    @param is_reachable/which/popen/download/fetch_text/list_models/pull_model Injectable seams
        (mirroring call_chat_completion's own dependency-injection pattern elsewhere in this
        codebase) so this is directly testable with no real network call, PATH lookup,
        subprocess spawn, multi-gigabyte download, or model pull. Each resolves to its own real
        implementation *at call time*, not def time, for the same `unittest.mock.patch`-friendly
        reason NPC_Generation.py's own call_chat_completion default does.
    @param vendor_dir Where a downloaded copy lives (see this module's own VENDOR_DIR/.gitignore
        notes) -- overridable so tests never touch the real one.
    @param ready_timeout Forwarded to _ensure_model_pulled -- overridable so tests that don't
        care about model-pulling can skip its wait instantly (ready_timeout=0) instead of
        actually sleeping.
    @return The subprocess.Popen handle if this call started a new process -- the caller now
        owns its lifetime (see LLDM.py's own atexit-based cleanup, which must never terminate an
        Ollama instance this call didn't itself start) -- or None if a server was already
        reachable, or no executable could be found or installed.
    """
    log = log or (lambda message: None)
    is_reachable = is_reachable or _default_is_reachable
    download = download or _real_download
    fetch_text = fetch_text or _real_fetch_text
    list_models = list_models or _default_list_models
    pull_model = pull_model or _default_pull_model

    if is_reachable(host):
        log("Ollama already running.")
        _ensure_model_pulled(host, model, log, is_reachable, list_models, pull_model, ready_timeout)
        return None

    executable = _resolve_ollama_executable(which, vendor_dir, download, fetch_text, log)
    if not executable:
        log("Ollama could not be found or installed; local LLM narration will be unavailable.")
        return None

    try:
        process = popen(
            [executable, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except OSError as e:
        log(f"Failed to start Ollama: {e}")
        return None

    log(f"Starting Ollama (pid={process.pid}).")
    _ensure_model_pulled(host, model, log, is_reachable, list_models, pull_model, ready_timeout)
    return process
