#!/usr/bin/env python3
"""
Jellyfin Tizen WGT auto updater for Samsung TV.

Workflow:
1. Query GitHub latest release with a GitHub token.
2. Check whether the TV's SDB port is reachable.
3. Download the target .wgt asset only when it changed.
4. Connect with sdb and install with Tizen CLI.
5. Retry important steps and send Telegram notifications.

Tested design target: Windows + Tizen Studio installed at C:\\tizen-studio.
It also works on Linux/macOS if Tizen Studio paths are adjusted.
"""

from __future__ import annotations

import fnmatch
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import requests

T = TypeVar("T")


@dataclass(frozen=True)
class Config:
    # GitHub
    github_token: str
    repo: str = "jeppevinkel/jellyfin-tizen-builds"
    # Prefer exact asset name first; if not found, use ASSET_PATTERN.
    asset_name: str = "Jellyfin-OSA.wgt"
    asset_pattern: str = "Jellyfin-OSA*.wgt"

    # TV / Tizen Studio
    tv_host: str = "192.168.1.88"
    tv_port: int = 26101
    # Leave empty to install by serial/IP:port. Set to UA65RU7700JXXZ if you prefer -t target.
    tizen_target: str = ""
    tizen_studio: str = r"C:\tizen-studio"

    # Paths
    download_dir: Path = Path(r"D:\Download")
    state_file: Path = Path(r"D:\Download\jellyfin_tizen_update_state.json")

    # Retry / timeout
    retry_attempts: int = 3
    retry_delay_sec: float = 8.0
    retry_backoff: float = 1.8
    tv_connect_timeout_sec: float = 5.0
    command_timeout_sec: int = 300
    download_timeout_sec: int = 300

    # Telegram, optional but recommended
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_no_update: bool = False

    @property
    def tv_serial(self) -> str:
        return f"{self.tv_host}:{self.tv_port}"

    @property
    def tools_dir(self) -> Path:
        return Path(self.tizen_studio) / "tools"

    @property
    def sdb_path(self) -> Path:
        exe = "sdb.exe" if platform.system().lower() == "windows" else "sdb"
        return self.tools_dir / exe

    @property
    def tizen_cli_path(self) -> Path:
        name = "tizen.bat" if platform.system().lower() == "windows" else "tizen"
        return Path(self.tizen_studio) / "tools" / "ide" / "bin" / name


def getenv_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> Config:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing env GITHUB_TOKEN")

    return Config(
        github_token=token,
        repo=os.getenv("GITHUB_REPO", "jeppevinkel/jellyfin-tizen-builds").strip(),
        asset_name=os.getenv("ASSET_NAME", "Jellyfin-OSA.wgt").strip(),
        asset_pattern=os.getenv("ASSET_PATTERN", "Jellyfin-OSA*.wgt").strip(),
        tv_host=os.getenv("TV_HOST", "192.168.1.88").strip(),
        tv_port=int(os.getenv("TV_PORT", "26101")),
        tizen_target=os.getenv("TIZEN_TARGET", "").strip(),
        tizen_studio=os.getenv("TIZEN_STUDIO", r"C:\tizen-studio").strip(),
        download_dir=Path(os.getenv("DOWNLOAD_DIR", r"D:\Download")),
        state_file=Path(os.getenv("STATE_FILE", r"D:\Download\jellyfin_tizen_update_state.json")),
        retry_attempts=int(os.getenv("RETRY_ATTEMPTS", "3")),
        retry_delay_sec=float(os.getenv("RETRY_DELAY_SEC", "8")),
        retry_backoff=float(os.getenv("RETRY_BACKOFF", "1.8")),
        tv_connect_timeout_sec=float(os.getenv("TV_CONNECT_TIMEOUT_SEC", "5")),
        command_timeout_sec=int(os.getenv("COMMAND_TIMEOUT_SEC", "300")),
        download_timeout_sec=int(os.getenv("DOWNLOAD_TIMEOUT_SEC", "300")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        notify_no_update=getenv_bool("NOTIFY_NO_UPDATE", False),
    )


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def notify(cfg: Config, text: str) -> None:
    log(f"[telegram] {text}")
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return

    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": cfg.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=20).raise_for_status()
    except Exception as exc:  # Telegram failure should not break updating.
        log(f"Telegram notify failed: {exc}")


def retry(name: str, cfg: Config, func: Callable[[], T]) -> T:
    delay = cfg.retry_delay_sec
    last_exc: Optional[BaseException] = None

    for attempt in range(1, cfg.retry_attempts + 1):
        try:
            if attempt > 1:
                log(f"Retrying {name}: attempt {attempt}/{cfg.retry_attempts}")
            return func()
        except BaseException as exc:
            last_exc = exc
            log(f"{name} failed on attempt {attempt}/{cfg.retry_attempts}: {exc}")
            if attempt < cfg.retry_attempts:
                time.sleep(delay)
                delay *= cfg.retry_backoff

    assert last_exc is not None
    raise last_exc


def github_headers(cfg: Config, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {cfg.github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jellyfin-tizen-auto-updater/1.0",
    }


def get_latest_release(cfg: Config) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{cfg.repo}/releases/latest"
    resp = requests.get(url, headers=github_headers(cfg), timeout=30)
    resp.raise_for_status()
    return resp.json()


def select_asset(cfg: Config, release: dict[str, Any]) -> dict[str, Any]:
    assets: list[dict[str, Any]] = release.get("assets", [])
    if not assets:
        raise RuntimeError(f"Release {release.get('tag_name')} has no assets")

    exact = [a for a in assets if a.get("name") == cfg.asset_name]
    if exact:
        return exact[0]

    matched = [a for a in assets if fnmatch.fnmatch(a.get("name", ""), cfg.asset_pattern)]
    if matched:
        return sorted(matched, key=lambda a: a.get("name", ""))[0]

    names = ", ".join(a.get("name", "?") for a in assets)
    raise RuntimeError(
        f"No asset matched ASSET_NAME={cfg.asset_name!r} or "
        f"ASSET_PATTERN={cfg.asset_pattern!r}. Available: {names}"
    )


def asset_fingerprint(release: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
    # asset id is enough most of the time, but updated_at/size/name makes state more readable.
    return {
        "release_id": release.get("id"),
        "release_tag": release.get("tag_name"),
        "release_name": release.get("name"),
        "release_published_at": release.get("published_at"),
        "asset_id": asset.get("id"),
        "asset_name": asset.get("name"),
        "asset_size": asset.get("size"),
        "asset_updated_at": asset.get("updated_at"),
    }


def load_state(cfg: Config) -> dict[str, Any]:
    if not cfg.state_file.exists():
        return {}
    try:
        return json.loads(cfg.state_file.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"State file unreadable, ignoring: {exc}")
        return {}


def save_state(cfg: Config, state: dict[str, Any]) -> None:
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_file.with_suffix(cfg.state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(cfg.state_file)


def is_same_installed(state: dict[str, Any], fp: dict[str, Any]) -> bool:
    installed = state.get("installed", {})
    keys = ["release_id", "asset_id", "asset_name", "asset_size", "asset_updated_at"]
    return all(installed.get(k) == fp.get(k) for k in keys)


def check_tv_online(cfg: Config) -> bool:
    try:
        with socket.create_connection((cfg.tv_host, cfg.tv_port), timeout=cfg.tv_connect_timeout_sec):
            return True
    except OSError:
        return False


def download_asset(cfg: Config, asset: dict[str, Any]) -> Path:
    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    asset_name = asset["name"]
    dest = cfg.download_dir / asset_name
    tmp = dest.with_suffix(dest.suffix + ".download")

    # Use the release asset API so the token is actually used for the asset download too.
    asset_url = asset.get("url")
    if not asset_url:
        raise RuntimeError("Asset missing API url")

    headers = github_headers(cfg, accept="application/octet-stream")
    with requests.get(asset_url, headers=headers, stream=True, timeout=cfg.download_timeout_sec) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    expected_size = asset.get("size")
    actual_size = tmp.stat().st_size
    if expected_size and actual_size != expected_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded size mismatch: expected {expected_size}, got {actual_size}")

    tmp.replace(dest)
    return dest


def run_cmd(argv: list[str], cfg: Config) -> str:
    log("Running: " + " ".join(f'"{x}"' if " " in x else x for x in argv))

    # On Windows, running .bat through cmd.exe avoids CreateProcess edge cases.
    if platform.system().lower() == "windows" and argv[0].lower().endswith((".bat", ".cmd")):
        argv = [os.environ.get("ComSpec", "cmd.exe"), "/c", *argv]

    proc = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=cfg.command_timeout_sec,
        encoding="utf-8",
        errors="replace",
    )
    output = proc.stdout.strip()
    if output:
        log(output)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {proc.returncode}: {output}")
    return output


def ensure_tools_exist(cfg: Config) -> None:
    if not cfg.sdb_path.exists():
        raise RuntimeError(f"sdb not found: {cfg.sdb_path}")
    if not cfg.tizen_cli_path.exists():
        raise RuntimeError(f"tizen CLI not found: {cfg.tizen_cli_path}")


def sdb_connect(cfg: Config) -> None:
    run_cmd([str(cfg.sdb_path), "connect", cfg.tv_serial], cfg)
    run_cmd([str(cfg.sdb_path), "devices"], cfg)


def install_wgt(cfg: Config, wgt_path: Path) -> None:
    if not wgt_path.exists():
        raise RuntimeError(f"WGT not found: {wgt_path}")

    # Tizen CLI install expects: -n file-name -- package-directory
    if cfg.tizen_target:
        cmd = [
            str(cfg.tizen_cli_path),
            "install",
            "-t",
            cfg.tizen_target,
            "-n",
            wgt_path.name,
            "--",
            str(wgt_path.parent),
        ]
    else:
        cmd = [
            str(cfg.tizen_cli_path),
            "install",
            "-s",
            cfg.tv_serial,
            "-n",
            wgt_path.name,
            "--",
            str(wgt_path.parent),
        ]

    run_cmd(cmd, cfg)


def main() -> int:
    cfg = load_config()
    ensure_tools_exist(cfg)

    try:
        release = retry("GitHub latest release query", cfg, lambda: get_latest_release(cfg))
        asset = select_asset(cfg, release)
        fp = asset_fingerprint(release, asset)
        state = load_state(cfg)

        log(f"Latest release: {fp['release_tag']} / asset: {fp['asset_name']}")
        if is_same_installed(state, fp):
            msg = f"Jellyfin Tizen: no update. Installed {fp['release_tag']} / {fp['asset_name']}"
            log(msg)
            if cfg.notify_no_update:
                notify(cfg, msg)
            return 0

        if not retry("TV online check", cfg, lambda: check_tv_online(cfg) or (_ for _ in ()).throw(RuntimeError("TV SDB port is not reachable"))):
            raise RuntimeError("TV is offline")

        notify(cfg, f"Jellyfin Tizen: update found {fp['release_tag']} / {fp['asset_name']}. Downloading and installing...")

        wgt_path = retry("WGT download", cfg, lambda: download_asset(cfg, asset))
        retry("SDB connect", cfg, lambda: sdb_connect(cfg))
        retry("Tizen install", cfg, lambda: install_wgt(cfg, wgt_path))

        new_state = {
            "installed": fp,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tv_serial": cfg.tv_serial,
            "repo": cfg.repo,
        }
        save_state(cfg, new_state)
        notify(cfg, f"Jellyfin Tizen: installed {fp['release_tag']} / {fp['asset_name']} successfully on {cfg.tv_serial}")
        return 0

    except Exception as exc:
        notify(cfg, f"Jellyfin Tizen: update failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

