"""Named parser connections; credentials remain server-side."""

import json
import re
from pathlib import Path

from indexer.artifact_store import atomic_json
from indexer.settings import ServerSettings, values

FIELDS = ("parser_provider", "parser_base_url", "parser_api_key", "parser_model", "parser_mode", "parser_options", "parser_sync_timeout")


class ParserProfiles:
    def __init__(self, root: Path):
        self.path = root / "parser-profiles.json"

    def read(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
            raise ValueError("저장된 파서 목록을 확인할 수 없습니다")
        return data

    def list(self, config):
        current = values(config)
        return [{**{k: v for k, v in row.items() if k != "parser_api_key"},
                 "profile_id": profile_id, "api_key_configured": bool(row["parser_api_key"]),
                 "active": all(row.get(k) == current[k] for k in FIELDS)}
                for profile_id, row in self.read().items()]

    def save(self, profile_id, data, config):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", profile_id):
            raise ValueError("파서 ID를 확인해 주세요")
        if set(data) - {*FIELDS, "name"}:
            raise ValueError("알 수 없는 파서 설정입니다")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError("파서 이름을 확인해 주세요")
        profiles = self.read()
        old = profiles.get(profile_id, {})
        merged = {**{k: getattr(config, k) for k in FIELDS}, **old, **data}
        checked = ServerSettings.model_validate(values(config) | {k: merged[k] for k in FIELDS}).model_dump()
        if not checked["parser_provider"]:
            raise ValueError("파서 제공자를 선택해 주세요")
        profiles[profile_id] = {"name": name.strip(), **{k: checked[k] for k in FIELDS}}
        atomic_json(self.path, profiles)
        return next(row for row in self.list(config) if row["profile_id"] == profile_id)

    def delete(self, profile_id):
        profiles = self.read()
        if profile_id not in profiles:
            raise ValueError("파서를 찾을 수 없습니다")
        del profiles[profile_id]
        atomic_json(self.path, profiles)
