from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

APP_VERSION = "0.1.0-dev"
APP_PORT = 8099

DATA_DIR = Path("/data")
SUPERVISOR_OPTIONS_PATH = DATA_DIR / "options.json"
WEBUI_OPTIONS_PATH = DATA_DIR / "webui_options.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "sync.log"
HASH_INDEX_PATH = DATA_DIR / "hash_index.json"
STATIC_DIR = Path("/app/static")
CONFIG_ROOT = Path("/config")

IGNORE_DIRS = {".storage", ".cloud", "tts", "__pycache__", ".git"}
IGNORE_PATTERNS = (
    "home-assistant.log",
    "home-assistant.log.*",
    "home-assistant_v2.db",
    "home-assistant_v2.db-*",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.tmp",
    "*.swp",
    "*.pyc",
)

DEFAULT_OPTIONS: dict[str, Any] = {
    "github_repository": "",
    "github_branch": "main",
    "github_token": "",
    "sync_interval_minutes": 60,
    "dry_run": True,
}

DEFAULT_STATE: dict[str, Any] = {
    "status": "idle",
    "last_run": None,
    "last_success": None,
    "last_error": None,
    "last_result": None,
}


def _load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(fallback)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(fallback)
    if not isinstance(parsed, dict):
        return dict(fallback)
    return parsed


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _merge_options() -> dict[str, Any]:
    options = dict(DEFAULT_OPTIONS)
    options.update(_load_json(SUPERVISOR_OPTIONS_PATH, {}))
    options.update(_load_json(WEBUI_OPTIONS_PATH, {}))
    return options


def _load_state() -> dict[str, Any]:
    state = dict(DEFAULT_STATE)
    state.update(_load_json(STATE_PATH, {}))
    return state


def _save_state(updates: dict[str, Any]) -> dict[str, Any]:
    state = _load_state()
    state.update(updates)
    _save_json(STATE_PATH, state)
    return state


def _append_log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def _validate_payload(payload: dict[str, Any]) -> tuple[bool, str | None]:
    repository = str(payload.get("github_repository", "")).strip()
    if repository and repository.count("/") != 1:
        return False, "github_repository must be in owner/repo format"

    branch = str(payload.get("github_branch", "")).strip()
    if not branch:
        return False, "github_branch is required"

    interval_raw = payload.get("sync_interval_minutes")
    try:
        interval = int(interval_raw)
    except (TypeError, ValueError):
        return False, "sync_interval_minutes must be an integer"
    if interval < 5 or interval > 1440:
        return False, "sync_interval_minutes must be between 5 and 1440"

    if not isinstance(payload.get("dry_run"), bool):
        return False, "dry_run must be true or false"

    return True, None


def _mask_token(options: dict[str, Any]) -> dict[str, Any]:
    output = dict(options)
    token = output.get("github_token") or ""
    if token:
        output["github_token"] = "********"
    return output


def _github_repo_probe(repository: str, token: str) -> tuple[bool, str]:
    url = f"https://api.github.com/repos/{repository}"
    headers = {
        "User-Agent": "github-config-sync-addon",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            if response.status == 200:
                return True, "Repository probe succeeded"
            return False, f"Unexpected status: {response.status}"
    except urllib.error.HTTPError as err:
        return False, f"GitHub API returned HTTP {err.code}"
    except urllib.error.URLError as err:
        return False, f"GitHub API request failed: {err.reason}"


def _is_ignored(relative_path: str) -> bool:
    if any(part in IGNORE_DIRS for part in Path(relative_path).parts):
        return True
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in IGNORE_PATTERNS)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _build_hash_index(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}

    index: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _is_ignored(relative):
            continue
        index[relative] = _sha256_file(path)
    return index


def _diff_hash_indexes(previous: dict[str, str], current: dict[str, str]) -> dict[str, Any]:
    previous_keys = set(previous)
    current_keys = set(current)

    added = sorted(current_keys - previous_keys)
    removed = sorted(previous_keys - current_keys)
    changed = sorted(
        key for key in (current_keys & previous_keys) if previous.get(key) != current.get(key)
    )

    return {
        "added_count": len(added),
        "changed_count": len(changed),
        "removed_count": len(removed),
        "total_files": len(current),
        "added_files": added[:50],
        "changed_files": changed[:50],
        "removed_files": removed[:50],
    }


app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "version": APP_VERSION})


@app.get("/api/options")
def get_options():
    return jsonify(_mask_token(_merge_options()))


@app.post("/api/options")
def set_options():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Invalid JSON body"}), 400

    candidate = {
        "github_repository": str(payload.get("github_repository", "")).strip(),
        "github_branch": str(payload.get("github_branch", "main")).strip() or "main",
        "github_token": str(payload.get("github_token", "")).strip(),
        "sync_interval_minutes": payload.get("sync_interval_minutes", 60),
        "dry_run": payload.get("dry_run", True),
    }

    valid, message = _validate_payload(candidate)
    if not valid:
        return jsonify({"ok": False, "error": message}), 400

    candidate["sync_interval_minutes"] = int(candidate["sync_interval_minutes"])
    _save_json(WEBUI_OPTIONS_PATH, candidate)
    _append_log("Settings updated via web UI")
    return jsonify({"ok": True, "options": _mask_token(_merge_options())})


@app.get("/api/status")
def get_status():
    state = _load_state()
    logs = ""
    if LOG_PATH.exists():
        logs = LOG_PATH.read_text(encoding="utf-8")[-4000:]
    return jsonify({"ok": True, "state": state, "log_tail": logs})


@app.post("/api/sync")
def trigger_sync():
    options = _merge_options()
    repository = str(options.get("github_repository", "")).strip()
    token = str(options.get("github_token", "")).strip()
    dry_run = bool(options.get("dry_run", True))

    if not repository:
        state = _save_state(
            {
                "status": "error",
                "last_error": "github_repository is required",
                "last_run": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )
        return jsonify({"ok": False, "error": "github_repository is required", "state": state}), 400

    started = dt.datetime.now(dt.timezone.utc).isoformat()
    _save_state({"status": "running", "last_run": started, "last_error": None})
    _append_log(f"Sync started for {repository} (dry_run={dry_run})")

    previous_index = _load_json(HASH_INDEX_PATH, {})
    current_index = _build_hash_index(CONFIG_ROOT)
    change_summary = _diff_hash_indexes(previous_index, current_index)
    _append_log(
        "Scan summary: "
        f"+{change_summary['added_count']} "
        f"~{change_summary['changed_count']} "
        f"-{change_summary['removed_count']} "
        f"files={change_summary['total_files']}"
    )

    if dry_run:
        _save_json(HASH_INDEX_PATH, current_index)
        result = (
            "Dry run completed (no changes pushed). "
            f"Added: {change_summary['added_count']}, "
            f"Changed: {change_summary['changed_count']}, "
            f"Removed: {change_summary['removed_count']}."
        )
        state = _save_state(
            {
                "status": "ok",
                "last_success": dt.datetime.now(dt.timezone.utc).isoformat(),
                "last_result": result,
                "last_scan": change_summary,
                "last_error": None,
            }
        )
        _append_log(result)
        return jsonify({"ok": True, "result": result, "state": state})

    success, message = _github_repo_probe(repository, token)
    if not success:
        state = _save_state(
            {
                "status": "error",
                "last_error": message,
                "last_scan": change_summary,
                "last_result": None,
            }
        )
        _append_log(f"Sync failed: {message}")
        return jsonify({"ok": False, "error": message, "state": state}), 502

    _save_json(HASH_INDEX_PATH, current_index)
    result = (
        "Repository connectivity verified. "
        f"Pending changes -> Added: {change_summary['added_count']}, "
        f"Changed: {change_summary['changed_count']}, "
        f"Removed: {change_summary['removed_count']}."
    )
    state = _save_state(
        {
            "status": "ok",
            "last_success": dt.datetime.now(dt.timezone.utc).isoformat(),
            "last_scan": change_summary,
            "last_result": result,
            "last_error": None,
        }
    )
    _append_log(result)
    return jsonify({"ok": True, "result": result, "state": state})


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=APP_PORT, debug=False)
