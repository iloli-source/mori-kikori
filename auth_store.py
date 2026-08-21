"""OAuth トークンの永続化層。

mcp SDK の TokenStorage プロトコル実装。tokens.json に
アクセストークン・リフレッシュトークン・動的クライアント登録情報を保存する。
ファイルは chmod 600 で保護し、Git 管理外とする。
"""

import json
import os

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

TOKENS_FILE = os.path.join(os.path.dirname(__file__), "tokens.json")


class FileTokenStorage:
    """tokens.json に OAuth トークンとクライアント情報を保存する TokenStorage 実装。"""

    def __init__(self, path: str = TOKENS_FILE) -> None:
        self.path = path

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self.path)

    async def get_tokens(self) -> OAuthToken | None:
        data = self._load()
        if "tokens" not in data:
            return None
        return OAuthToken.model_validate(data["tokens"])

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._save(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._load()
        if "client_info" not in data:
            return None
        return OAuthClientInformationFull.model_validate(data["client_info"])

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save(data)
