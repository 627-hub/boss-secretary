"""LLM 客户端（OpenAI 兼容：OpenRouter / vLLM / GLM 同一接口）。

settings.yaml 驱动；注入优先（调用方可直接传 base_url/api_key 覆盖）。
JSON 输出用鲁棒解析（剥 ``` 围栏 + 首个平衡对象 raw_decode），免费模型不稳定时也能接住。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests
import yaml

DEFAULT_SETTINGS = "config/settings.yaml"


class LLMError(Exception):
    pass


def load_settings(path: str | Path | None = None) -> dict:
    p = Path(path or DEFAULT_SETTINGS)
    if not p.exists():
        raise LLMError(f"配置不存在: {p}（复制 config/settings.example.yaml 改名）")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def resolve_llm(settings: Mapping | None = None, provider: str | None = None) -> dict:
    s = settings or load_settings()
    llm = s.get("llm") or {}
    provider = provider or llm.get("provider", "cloud")
    if provider == "local" and (llm.get("local") or {}).get("base_url"):
        cfg = dict(llm["local"])
    else:
        cfg = dict(llm["cloud"])
    return {"base_url": (cfg.get("base_url") or "").rstrip("/"),
            "api_key": cfg.get("api_key") or "",
            "model": cfg.get("model") or cfg.get("model_extract"),
            "timeout": int(cfg.get("timeout", 90))}


def chat(messages: Sequence[Mapping[str, Any]], *, base_url: str, api_key: str,
         model: str, temperature: float = 0.1, max_tokens: int = 2000,
         timeout: int = 90, extra_headers: Mapping | None = None) -> str:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json",
               "X-Title": "boss-secretary"}
    if extra_headers:
        headers.update(extra_headers)
    try:
        r = requests.post(url, headers=headers, timeout=timeout, json={
            "model": model, "messages": list(messages),
            "temperature": temperature, "max_tokens": max_tokens})
    except requests.RequestException as e:
        raise LLMError(f"LLM 请求失败: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError(f"LLM 响应结构异常: {r.text[:300]}") from e


def extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(t[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    raise LLMError(f"响应中无 JSON 对象: {text[:200]}")


def chat_json(messages: Sequence[Mapping[str, Any]], **kw: Any) -> dict:
    raw = chat(messages, **kw)
    try:
        return extract_json(raw)
    except LLMError:
        retry = list(messages) + [{"role": "user", "content":
                                   "上一条回复不是合法 JSON。请只输出一个 JSON 对象，不要任何其他文字。"}]
        return extract_json(chat(retry, **kw))


def from_settings(messages: Sequence[Mapping[str, Any]], settings: Mapping | None = None,
                  model_key: str = "model", provider: str | None = None,
                  **kw: Any) -> str:
    resolved = resolve_llm(settings, provider)
    resolved["model"] = kw.pop("model", None) or resolved["model"]
    return chat(messages, base_url=resolved["base_url"], api_key=resolved["api_key"],
                model=resolved["model"], timeout=resolved["timeout"], **kw)


def from_settings_json(messages: Sequence[Mapping[str, Any]],
                       settings: Mapping | None = None, model_key: str = "model",
                       provider: str | None = None, **kw: Any) -> dict:
    resolved = resolve_llm(settings, provider)
    resolved["model"] = kw.pop("model", None) or resolved["model"]
    return chat_json(messages, base_url=resolved["base_url"],
                     api_key=resolved["api_key"], model=resolved["model"],
                     timeout=resolved["timeout"], **kw)
