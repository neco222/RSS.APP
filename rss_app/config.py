from __future__ import annotations

import json
import os
import typing as t


DEFAULT_TOKEN_ENVS = ("RSS_GITHUB_TOKEN", "GITHUB_TOKEN")


def load_config(path: str = "config.json") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    resolve_github_token(cfg)
    return cfg


def resolve_github_token(cfg: dict) -> None:
    github = cfg.setdefault("github", {})
    token = str(github.get("token") or "").strip()
    token_envs = _token_env_names(github)

    if token.startswith("${") and token.endswith("}"):
        token_envs.insert(0, token[2:-1])
        token = ""

    for env_name in token_envs:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            github["token"] = env_value
            return

    if token:
        github["token"] = token


def _token_env_names(github: dict) -> list[str]:
    names: list[str] = []
    raw = github.get("token_env")
    if isinstance(raw, str):
        names.append(raw)
    elif isinstance(raw, list):
        names.extend(str(item) for item in raw if item)

    for name in DEFAULT_TOKEN_ENVS:
        if name not in names:
            names.append(name)
    return names

