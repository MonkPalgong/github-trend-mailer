import json
import os
import subprocess
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def test_send_mail():
    print("\n=== [1단계] kusari0215@naver.com 즉시 테스트 메일 발송 ===")
    smtp_user = input("1) 발신용 이메일 주소 (엔터 치면 kusari0215@naver.com 사용): ").strip() or "kusari0215@naver.com"
    smtp_pass = input("2) 네이버(또는 Gmail) 2단계 인증 '애플리케이션 비밀번호' 입력: ").strip()

    if "gmail.com" in smtp_user:
        smtp_host = "smtp.gmail.com"
    else:
        smtp_host = "smtp.naver.com"

    env_content = (
        f"SMTP_HOST={smtp_host}\n"
        f"SMTP_PORT=587\n"
        f"SMTP_USER={smtp_user}\n"
        f"SMTP_PASSWORD={smtp_pass}\n"
        f"RECIPIENT_EMAIL=kusari0215@naver.com\n"
    )
    with open(os.path.join(BASE_DIR, ".env"), "w", encoding="utf-8") as f:
        f.write(env_content)

    print("\n-> .env 저장 완료! 지금 바로 kusari0215@naver.com 으로 첫 뉴스레터를 발송합니다...")
    subprocess.run(["python", os.path.join(BASE_DIR, "send_digest.py")], cwd=BASE_DIR)


if __name__ == "__main__":
    test_send_mail()
