"""!
@file slot.py
@brief The save-slot seam. A save slot is a directory of independently written parts
    ("dm_state", "llm_state", "gui_state"), each owned by one core -- the cores only ever talk
    through events, so there's no shared writer. This module is the one place that knows where a
    slot lives, how a part is stamped with a format version, and how it's written and read back:

    - FileSlotStore: the production adapter, Saves/<slot>/<part>.json, written atomically.
    - MemorySlotStore: the test adapter -- the same interface with no filesystem.

    Persistable is the contract a subsystem implements to contribute keys to a part; DMCore
    composes an ordered list of them (see snapshot_all/restore_all). Neither dm/ nor llm/ imports
    the other -- both import downward from this neutral package, the same reason intents/ and
    resolution/ exist.

    Reading a part never mutates anything: read() returns the parsed dict or raises SaveError, so
    a caller can validate a whole part before touching live state.
"""

import json
import os

from paths import PROJECT_ROOT

# Bump when a part's shape changes incompatibly. Old slots are rejected with
# SaveError("unsupported_version") rather than migrated -- see docs/persistence.md.
FORMAT_VERSION = 3
VERSION_KEY = "format_version"


class SaveError(Exception):
    """!
    @brief A slot part couldn't be read. reason is one of "not_found", "corrupt",
        "unsupported_version" -- the same string game_load_failed publishes.
    """

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def safe_slot_name(slot_name):
    """!
    @brief Sanitizes a player-supplied slot name. os.path.basename strips any path-separator
        components, so a slot can't escape the saves root (a slot literally named "../../etc"
        resolves to "etc").
    """
    return os.path.basename(slot_name.strip()) or "unnamed"


def _check_version(data, check_version):
    if not isinstance(data, dict):
        raise SaveError("corrupt", "top level isn't an object")
    if check_version and data.get(VERSION_KEY) != FORMAT_VERSION:
        raise SaveError("unsupported_version", f"found {data.get(VERSION_KEY)!r}, need {FORMAT_VERSION}")
    return data


class FileSlotStore:
    """!
    @brief Saves/<slot>/<part>.json on disk. root defaults to Saves/ under the project root
        (script-relative, so it's the same directory whatever the process's cwd is).
    """

    def __init__(self, root=None):
        self.root = root or os.path.join(PROJECT_ROOT, "Saves")

    def slot_dir(self, slot_name):
        return os.path.join(self.root, safe_slot_name(slot_name))

    def path(self, slot_name, part):
        return os.path.join(self.slot_dir(slot_name), f"{part}.json")

    def list_slots(self):
        if not os.path.isdir(self.root):
            return []
        return sorted(
            name for name in os.listdir(self.root)
            if os.path.isdir(os.path.join(self.root, name))
        )

    def write(self, slot_name, part, data):
        """!
        @brief Stamps data with FORMAT_VERSION and writes it atomically (temp file, then
            os.replace), so a crash mid-write can never leave a half-written part behind.
        """
        os.makedirs(self.slot_dir(slot_name), exist_ok=True)
        path = self.path(slot_name, part)
        temp_path = path + ".tmp"
        with open(temp_path, "w") as f:
            json.dump({**data, VERSION_KEY: FORMAT_VERSION}, f, indent=2)
        os.replace(temp_path, path)

    def read(self, slot_name, part, check_version=True):
        """!
        @param check_version False for a tolerant peek (LLDM.py's cold-start scenario lookup)
            that only wants a key or two and shouldn't fail on a stale slot.
        @return The parsed part (including its version stamp).
        @raise SaveError "not_found", "corrupt" or "unsupported_version".
        """
        path = self.path(slot_name, part)
        if not os.path.exists(path):
            raise SaveError("not_found", path)
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (OSError, ValueError) as error:
            raise SaveError("corrupt", str(error)) from error
        return _check_version(data, check_version)


class MemorySlotStore:
    """!
    @brief FileSlotStore's interface over a dict -- for tests that want a save/load round trip
        without the filesystem. Round-trips through JSON text so the same non-serializable-value
        bugs a real save would hit show up here too.
    """

    def __init__(self):
        self._parts = {}

    def slot_dir(self, slot_name):
        return f"memory://{safe_slot_name(slot_name)}"

    def path(self, slot_name, part):
        return f"{self.slot_dir(slot_name)}/{part}.json"

    def list_slots(self):
        return sorted({slot for slot, _ in self._parts})

    def write(self, slot_name, part, data):
        self._parts[(safe_slot_name(slot_name), part)] = json.dumps({**data, VERSION_KEY: FORMAT_VERSION})

    def read(self, slot_name, part, check_version=True):
        text = self._parts.get((safe_slot_name(slot_name), part))
        if text is None:
            raise SaveError("not_found", self.path(slot_name, part))
        return _check_version(json.loads(text), check_version)


class Persistable:
    """!
    @brief The contract a subsystem implements to contribute keys to one slot part.
        snapshot() returns that subsystem's own JSON-serializable keys; restore(data) takes the
        whole parsed part and reads only its own keys, tolerating any being absent. Each
        subsystem owns its keys outright -- snapshot_all rejects two participants claiming the
        same one.
    """

    def snapshot(self):
        raise NotImplementedError

    def restore(self, data):
        raise NotImplementedError


def snapshot_all(participants):
    """!
    @brief Merges every participant's snapshot into one part, in list order.
    @raise ValueError if two participants emit the same key.
    """
    merged = {}
    for participant in participants:
        for key, value in participant.snapshot().items():
            if key in merged:
                raise ValueError(f"{type(participant).__name__} re-emits key {key!r}")
            merged[key] = value
    return merged


def restore_all(participants, data):
    """!
    @brief Restores every participant, in list order -- the order is the contract: a
        participant that must run before another (ex: the removed-entity list before scenario
        re-instancing) is simply listed earlier.
    """
    for participant in participants:
        participant.restore(data)
