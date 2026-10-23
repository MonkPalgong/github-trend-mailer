import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
WORKFLOW_DIR = os.path.join(BASE_DIR, ".github", "workflows")
WORKFLOW_PATH = os.path.join(WORKFLOW_DIR, "weekly_digest.yml")

DAY_MAP = {
    "SUN": "0", "MON": "1", "TUE": "2", "WED": "3",
    "THU": "4", "FRI": "5", "SAT": "6", "*": "*"
}

PRESETS = {
    "1": ("매주 토요일 오전 9시 (주말 모닝 매거진)", "SAT", 9, 0, "weekly"),
    "2": ("매주 월요일 오전 8시 (출근 전 주간 브리핑)", "MON", 8, 0, "weekly"),
    "3": ("매일 아침 8시 (일간 핫이슈 브리핑)", "*", 8, 0, "daily"),
    "4": ("매주 수요일/토요일 오전 9시 (주 2회 브리핑)", "WED,SAT", 9, 0, "daily"),
}


def kst_to_utc_cron(day_str, hour_kst, minute_kst):
    """한국 시간(KST, UTC+9)을 GitHub Actions용 UTC Cron 표현식으로 자동 변환합니다."""
    utc_hour = hour_kst - 9
    day_shift = 0
    if utc_hour < 0:
        utc_hour += 24
        day_shift = -1

    if day_str.strip() == "*":
        utc_days = "*"
    else:
        parts = [p.strip().upper() for p in day_str.split(",")]
        converted = []
        for p in parts:
            num = int(DAY_MAP.get(p, "6"))
            num = (num + day_shift) % 7
            converted.append(str(num))
        utc_days = ",".join(converted)

    return f"{minute_kst} {utc_hour} * * {utc_days}"


def sync_workflow(config):
    os.makedirs(WORKFLOW_DIR, exist_ok=True)
    sched = config["schedule"]
    day_str = sched["day_of_week"]
    hour_kst = int(sched["hour_kst"])
    minute_kst = int(sched.get("minute_kst", 0))
    cron_expr = kst_to_utc_cron(day_str, hour_kst, minute_kst)

    yml = f"""name: GitHub Trend Email Digest

on:
  schedule:
    # 자동 생성됨: 한국 시간(KST) 기준 [{day_str} {hour_kst:02d}:{minute_kst:02d}] 실행
    - cron: '{cron_expr}'
  workflow_dispatch:
    inputs:
      trending_window:
        description: '수집 기간 선택 (daily / weekly / monthly)'
        required: false
        default: '{config["curation"].get("trending_window", "weekly")}'

permissions:
  contents: write  # lets the job commit sent_history.json

jobs:
  send-digest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Generate and Send GitHub Trend Digest
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
          RESEND_API_KEY: ${{{{ secrets.RESEND_API_KEY }}}}
          GEMINI_API_KEY: ${{{{ secrets.GEMINI_API_KEY }}}}
          SMTP_HOST: ${{{{ secrets.SMTP_HOST }}}}
          SMTP_PORT: ${{{{ secrets.SMTP_PORT }}}}
          SMTP_USER: ${{{{ secrets.SMTP_USER }}}}
          SMTP_PASSWORD: ${{{{ secrets.SMTP_PASSWORD }}}}
          RECIPIENT_EMAIL: ${{{{ secrets.RECIPIENT_EMAIL || 'kusari0215@naver.com' }}}}
        run: python send_digest.py

      - name: Commit sent history
        run: |
          [ -f sent_history.json ] || exit 0
          git config user.name "github-actions[bot]"
          git config user.email "41898283+github-actions[bot]@users.noreply.github.com"
          git add sent_history.json
          git diff --cached --quiet || (git commit -m "chore: update sent history" && git push)
"""
    with open(WORKFLOW_PATH, "w", encoding="utf-8") as f:
        f.write(yml)
    return cron_expr


def apply_schedule(day_str, hour_kst, minute_kst=0, window=None):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    config["schedule"]["day_of_week"] = day_str
    config["schedule"]["hour_kst"] = int(hour_kst)
    config["schedule"]["minute_kst"] = int(minute_kst)
    if window:
        config["curation"]["trending_window"] = window

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    cron_expr = sync_workflow(config)
    print(f"[완료] 발송 주기가 변경되었습니다: 요일={day_str}, 시간={int(hour_kst):02d}:{int(minute_kst):02d} KST (UTC Cron: '{cron_expr}')")


if __name__ == "__main__":
    # CLI 인자로 바로 실행하거나(예: python change_schedule.py MON 8), 인자 없이 실행하면 기본 config.json을 워크플로우에 동기화
    if len(sys.argv) >= 3:
        day = sys.argv[1].upper()
        hr = int(sys.argv[2])
        win = sys.argv[3] if len(sys.argv) >= 4 else None
        apply_schedule(day, hr, 0, win)
    else:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cron = sync_workflow(cfg)
        print(f"[동기화 완료] 현재 설정: {cfg['schedule']['day_of_week']} {cfg['schedule']['hour_kst']:02d}:{cfg['schedule']['minute_kst']:02d} KST -> GitHub Actions Cron: '{cron}'")
        print("주기를 바꾸려면: python change_schedule.py <요일:SAT|MON|*|WED,SAT> <시간(0~23)> [daily|weekly]")
