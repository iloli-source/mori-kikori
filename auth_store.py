"""OAuth トークンの永続化層。

mcp SDK の TokenStorage プロトコル実装。tokens.json に
アクセストークン・リフレッシュトークン・動的クライアント登録情報を保存する。
ファイルは chmod 600 で保護し、Git 管理外とする。
"""

import json
import os
import time

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
            # 壊れたファイルは無言で {} にせず、診断用に退避してから空扱いにする
            try:
                os.replace(self.path, f"{self.path}.corrupt")
            except OSError:
                pass
            return {}

    def _save(self, data: dict) -> None:
        tmp_path = f"{self.path}.tmp{os.getpid()}"
        # 作成時点から 0600（chmod 前の一瞬でも他ユーザーに読ませない）
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    async def get_tokens(self) -> OAuthToken | None:
        """保存済みトークンを返す。expires_in は取得時刻からの残り秒数に補正する。

        mcp SDK はトークンの有効期限をプロセス内にしか持たないため、保存時の
        obtained_at を使って残存有効期間を計算し直す。期限切れなら 0 になる。
        """
        data = self._load()
        if "tokens" not in data:
            return None
        tokens = OAuthToken.model_validate(data["tokens"])
        obtained_at = data.get("obtained_at")
        if obtained_at is not None and tokens.expires_in is not None:
            remaining = int(obtained_at + tokens.expires_in - time.time())
            tokens = tokens.model_copy(update={"expires_in": max(0, remaining)})
        return tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        data["obtained_at"] = time.time()
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
