"""토스증권 Open API 인증 (OAuth 2.0 Client Credentials).

주의: 토큰을 재발급하면 이전 토큰이 즉시 무효화된다.
따라서 매 실행마다 새로 발급하지 않고, 파일에 캐시해 만료 전까지 재사용한다.
"""

import json
import time
from pathlib import Path

import requests

BASE_URL = "https://openapi.tossinvest.com"
TOKEN_CACHE = Path.home() / ".toss_token_cache.json"

# 만료 5분 전부터는 새 토큰을 받는다 (여유 마진)
EXPIRY_MARGIN_SEC = 300


class TossAuthError(Exception):
    pass


def _issue_token(client_id: str, client_secret: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise TossAuthError(
            f"토큰 발급 실패 (HTTP {resp.status_code}): {resp.text[:300]}\n"
            "- client_id/secret이 맞는지\n"
            "- 이 PC의 공인 IP가 WTS '허용 IP 관리'에 등록됐는지 확인하세요."
        )
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "expires_at": time.time() + int(data.get("expires_in", 86400)),
    }


def get_access_token(client_id: str, client_secret: str) -> str:
    """캐시된 토큰이 유효하면 재사용, 아니면 새로 발급."""
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text())
            if cached.get("expires_at", 0) - EXPIRY_MARGIN_SEC > time.time():
                return cached["access_token"]
        except (json.JSONDecodeError, KeyError):
            pass  # 캐시가 깨졌으면 새로 발급

    token = _issue_token(client_id, client_secret)
    TOKEN_CACHE.write_text(json.dumps(token))
    TOKEN_CACHE.chmod(0o600)  # 본인만 읽기 가능
    return token["access_token"]
