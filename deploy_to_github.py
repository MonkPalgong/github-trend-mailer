import base64
import json
import os
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_TO_PUSH = [
    "config.json",
    "send_digest.py",
    "change_schedule.py",
    ".env.example",
    ".github/workflows/weekly_digest.yml",
]


def gh_api(method, path, token, payload=None):
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "GitHubTrendMailerUploader/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def upload_file(owner, repo, rel_path, token):
    full_path = os.path.join(BASE_DIR, rel_path)
    with open(full_path, "rb") as f:
        b64_content = base64.b64encode(f.read()).decode("ascii")

    api_path = f"/repos/{owner}/{repo}/contents/{rel_path}"
    sha = None
    try:
        existing = gh_api("GET", api_path, token)
        sha = existing.get("sha")
    except Exception:
        pass

    payload = {
        "message": f"chore: sync {rel_path}",
        "content": b64_content,
    }
    if sha:
        payload["sha"] = sha

    gh_api("PUT", api_path, token, payload)
    print(f"  [업로드 성공] {rel_path}")


def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        token = input("GitHub Personal Access Token (repo, workflow 권한 포함) 입력: ").strip()
    repo_name = os.environ.get("GITHUB_REPO_NAME", "github-trend-mailer").strip()

    user = gh_api("GET", "/user", token)
    owner = user["login"]
    print(f"[인증 확인] GitHub 계정: {owner}")

    try:
        gh_api("GET", f"/repos/{owner}/{repo_name}", token)
        print(f"[확인] 기존 레포지토리 발견: https://github.com/{owner}/{repo_name}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[생성] 새 레포지토리 생성 중: {owner}/{repo_name} ...")
            gh_api("POST", "/user/repos", token, {
                "name": repo_name,
                "description": "Automated GitHub Trend Email Digest (Star Velocity + Work + Personal/Fun)",
                "private": False,
                "auto_init": True,
            })
        else:
            raise

    for rel_path in FILES_TO_PUSH:
        upload_file(owner, repo_name, rel_path, token)

    print(f"\n[완료!] GitHub 레포지토리 배포 성공: https://github.com/{owner}/{repo_name}")
    print(f"이제 https://github.com/{owner}/{repo_name}/settings/secrets/actions 에서")
    print("GEMINI_API_KEY, SMTP_HOST(smtp.naver.com), SMTP_PORT(587), SMTP_USER(kusari0215@naver.com), SMTP_PASSWORD, RECIPIENT_EMAIL(kusari0215@naver.com) 만 등록하시면 PC가 꺼져 있어도 매주 자동 발송됩니다!")


if __name__ == "__main__":
    main()
