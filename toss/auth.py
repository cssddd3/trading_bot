"""토스증권 Open API 인증 (OAuth 2.0 Client Credentials).

주의: 토큰을 재발급하면 이전 토큰이 즉시 무효화된다.
따라서 매 실행마다 새로 발급하지 않고, 파일에 캐시해 만료 전까지 재사용한다.
"""

import json
import time
from pathlib import Path

import requests

BASE_URL = "https://openapi.tossinvest.com"
TOKEN_CACHE = Path.home() / ".toss_token_cache.json"   # (구) 단일 캐시 — 호환용 경로 상수

# 만료 5분 전부터는 새 토큰을 받는다 (여유 마진)
EXPIRY_MARGIN_SEC = 300


def cache_path(client_id: str) -> Path:
    """client_id별 토큰 캐시 파일. 9-27: 한 컴퓨터(같은 홈 디렉토리)에서 봇 두 개(가족 계좌)를
    돌리면 단일 캐시 파일을 서로 덮어써 상대 토큰을 읽고 401 → 재발급 → 상대 토큰 무효화
    루프. 키의 해시 8자리로 파일을 분리해 인스턴스끼리 완전히 독립시킨다."""
    import hashlib
    tag = hashlib.sha256(client_id.encode()).hexdigest()[:8]
    return Path.home() / f".toss_token_cache_{tag}.json"


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
    """캐시된 토큰이 유효하면 재사용, 아니면 새로 발급. 캐시는 client_id별 파일."""
    path = cache_path(client_id)
    if path.exists():
        try:
            cached = json.loads(path.read_text())
            if cached.get("expires_at", 0) - EXPIRY_MARGIN_SEC > time.time():
                return cached["access_token"]
        except (json.JSONDecodeError, KeyError):
            pass  # 캐시가 깨졌으면 새로 발급

    token = _issue_token(client_id, client_secret)
    path.write_text(json.dumps(token))
    path.chmod(0o600)  # 본인만 읽기 가능
    return token["access_token"]
