from __future__ import annotations

import json
import os
import typing as t


DEFAULT_TOKEN_ENVS = ("RSS_GITHUB_TOKEN", "GITHUB_TOKEN")
DEFAULT_REPO_ENVS = ("GITHUB_REPOSITORY",)


def load_config(path: str = "config.json") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    resolve_github_token(cfg)
    resolve_github_repo(cfg)
    return cfg


def resolve_github_token(cfg: dict) -> None:
    github = cfg.setdefault("github", {})
    token = str(github.get("token") or "").strip()
    token_envs = _token_env_names(github)

    if token.startswith("${") and token.endswith("}"):
        token_envs.insert(0, token[2:-1])
        token = ""
        github.pop("token", None)

    for env_name in token_envs:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            github["token"] = env_value
            return

    if token:
        github["token"] = token


def resolve_github_repo(cfg: dict) -> None:
    github = cfg.setdefault("github", {})
    repo = str(github.get("repo") or "").strip()
    repo_envs = _github_env_names(github, "repo_env", DEFAULT_REPO_ENVS)

    if repo.startswith("${") and repo.endswith("}"):
        repo_envs.insert(0, repo[2:-1])
        repo = ""
        github.pop("repo", None)

    for env_name in repo_envs:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            github["repo"] = env_value
            return

    if repo:
        github["repo"] = repo


def _token_env_names(github: dict) -> list[str]:
    return _github_env_names(github, "token_env", DEFAULT_TOKEN_ENVS)


def _github_env_names(github: dict, key: str, defaults: tuple[str, ...]) -> list[str]:
    names: list[str] = []
    raw = github.get(key)
    if isinstance(raw, str):
        names.append(raw)
    elif isinstance(raw, list):
        names.extend(str(item) for item in raw if item)

    for name in defaults:
        if name not in names:
            names.append(name)
    return names
