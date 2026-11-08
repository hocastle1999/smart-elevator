"""관리 기능(고장 주입, 수리, 점검, 복귀)의 접근 제어.

공유 데모라 아무나 카를 고장 내면 안 된다. ADMIN_TOKEN을 설정했을 때만 관리 API가 열리고,
요청 헤더 X-Admin-Token이 같아야 한다. 설정하지 않으면 관리 API는 항상 403이다(기본은 닫힘).
"""

import secrets

from fastapi import HTTPException, Request, status
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    admin_token: SecretStr | None = None

    @property
    def enabled(self) -> bool:
        return self.admin_token is not None and bool(self.admin_token.get_secret_value())


def require_admin(request: Request) -> None:
    settings: AdminSettings = request.app.state.admin_settings
    if not settings.enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="관리 기능이 꺼져 있습니다.")
    assert settings.admin_token is not None
    given = request.headers.get("X-Admin-Token", "")
    # compare_digest: 문자열을 앞에서부터 비교하다 다른 곳에서 바로 멈추면, 응답 시간 차이로
    # 토큰을 한 글자씩 알아낼 수 있다(타이밍 공격). 항상 끝까지 비교한다.
    if not secrets.compare_digest(given.encode(), settings.admin_token.get_secret_value().encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="관리자 토큰이 맞지 않습니다.")
