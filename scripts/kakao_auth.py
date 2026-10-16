"""카카오 '나에게 보내기'용 refresh token 최초 1회 발급.

사전 준비 (developers.kakao.com):
  1. 내 애플리케이션 → 앱 생성 → [앱 키]에서 REST API 키 복사
  2. [카카오 로그인] 활성화 ON, Redirect URI에 https://localhost:3000 등록
  3. [카카오 로그인 > 동의항목]에서 '카카오톡 메시지 전송(talk_message)' 선택 동의 설정

실행: venv\\Scripts\\python.exe scripts\\kakao_auth.py
  → 출력된 URL을 브라우저로 열어 로그인·동의
  → 이동된 주소(https://localhost:3000/?code=...)를 통째로 붙여넣기
  → refresh token이 DB에 저장되고 화면에도 출력됨 (GitHub Secret 등록용)
"""
import os
import sys
import urllib.parse

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db                    # noqa: E402  (.env 로드 + 회사 프록시 인증서 처리)
from kakao_brief import save_refresh_token  # noqa: E402

REDIRECT_URI = "https://localhost:3000"


def main():
    rest_key = os.environ.get("KAKAO_REST_API_KEY", "").strip() or input("REST API 키: ").strip()
    secret   = os.environ.get("KAKAO_CLIENT_SECRET", "").strip()

    auth_url = "https://kauth.kakao.com/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": rest_key, "redirect_uri": REDIRECT_URI,
        "response_type": "code", "scope": "talk_message",
    })
    print("\n1) 아래 주소를 브라우저에서 열어 로그인·동의하세요:\n\n" + auth_url)
    print("\n2) '사이트에 연결할 수 없음' 화면이 떠도 정상입니다. 주소창 URL 전체를 복사하세요.")
    redirected = input("\n이동된 URL 붙여넣기: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(redirected).query).get("code", [redirected])[0]

    data = {"grant_type": "authorization_code", "client_id": rest_key,
            "redirect_uri": REDIRECT_URI, "code": code}
    if secret:
        data["client_secret"] = secret
    resp = requests.post("https://kauth.kakao.com/oauth/token", data=data, timeout=15)
    if resp.status_code != 200:
        sys.exit(f"토큰 발급 실패 {resp.status_code}: {resp.text}")
    refresh = resp.json()["refresh_token"]

    conn = get_db()
    try:
        save_refresh_token(conn, refresh)
    finally:
        conn.close()
    print("\n✅ refresh token을 DB에 저장했습니다.")
    print("GitHub Secret KAKAO_REFRESH_TOKEN에도 등록하세요 (DB 초기화 시 백업용):\n\n" + refresh)


if __name__ == "__main__":
    main()
