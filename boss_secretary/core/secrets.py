"""密钥存取（macOS Keychain 优先，明文回退）。

敏感字段（feishu.app_secret / llm.*.api_key）存 Keychain（服务名 boss-secretary），
settings.yaml 只留非敏感配置。Keychain 不可用时回退读 yaml 明文字段（保持可用）。

CLI:
  python3 -m boss_secretary.secrets set llm.cloud.api_key     # 交互输入(getpass)
  python3 -m boss_secretary.secrets get  feishu.app_secret
  python3 -m boss_secretary.secrets list
  python3 -m boss_secretary.secrets migrate config/settings.yaml  # yaml明文→Keychain并清空
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path
from typing import Mapping

import yaml

SERVICE = "boss-secretary"

SECRET_PATHS = ("feishu.app_secret", "llm.cloud.api_key", "llm.local.api_key",
                "channels.telegram.bot_token")


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def get_secret(name: str) -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, name)
    except Exception:
        return None


def set_secret(name: str, value: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    kr.set_password(SERVICE, name, value)
    return True


def delete_secret(name: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:
            pass


def _dig(d: Mapping, dotted: str):
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _pierce(d: dict, dotted: str) -> Any:
    cur = d
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    return cur, parts[-1]


def merge_secret_overrides(settings: dict) -> dict:
    """Keychain 里有值的字段覆盖 settings（无则保留 yaml 值, 兼容明文回退）。"""
    for path in SECRET_PATHS:
        v = get_secret(path)
        if v:
            parent, key = _pierce(settings, path)
            parent[key] = v
    return settings


def migrate(settings_path: str = "config/settings.yaml") -> list[str]:
    """yaml 中的明文密钥 → Keychain，文件内清空为空串（幂等）。"""
    p = Path(settings_path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    moved = []
    for path in SECRET_PATHS:
        v = _dig(data, path)
        if v:
            if set_secret(path, str(v)):
                parent, key = _pierce(data, path)
                parent[key] = ""
                moved.append(path)
    if moved:
        p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")
    return moved


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, *rest = argv
    if cmd == "set":
        name = rest[0]
        value = getpass.getpass(f"{name} (输入不回显): ")
        ok = set_secret(name, value)
        print("已存入 Keychain" if ok else "keyring 不可用: pip install keyring")
        return 0 if ok else 1
    if cmd == "get":
        v = get_secret(rest[0])
        print(v if v else "(未设置)")
        return 0
    if cmd == "list":
        for path in SECRET_PATHS:
            v = get_secret(path)
            print(f"{path:<22} {'已存(Keychain)' if v else '-'}")
        return 0
    if cmd == "migrate":
        moved = migrate(rest[0] if rest else "config/settings.yaml")
        print("已迁移并清空: " + (", ".join(moved) if moved else "（无明文密钥）"))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
