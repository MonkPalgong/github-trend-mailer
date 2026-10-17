import base64
import json
import os
import re
import smtplib
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
PREVIEW_PATH = os.path.join(BASE_DIR, "sample_newsletter.html")


def load_dotenv():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_url(url, headers=None, timeout=12):
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GitHubTrendMailer/3.0"
    }
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if gh_token and "api.github.com" in url:
        req_headers["Authorization"] = f"Bearer {gh_token}"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def translate_en_to_ko(text):
    """MyMemory 무료 번역 API를 통해 영문 요약문을 자연스러운 한국어로 번역합니다."""
    if not text or not text.strip():
        return ""
    if len(re.findall(r"[가-힣]", text)) > 10:
        return text.strip()
    try:
        snippet = text.strip()[:420]
        q = urllib.parse.quote(snippet)
        url = f"https://api.mymemory.translated.net/get?q={q}&langpair=en|ko"
        raw = fetch_url(url, timeout=8)
        data = json.loads(raw)
        ko = data.get("responseData", {}).get("translatedText", "")
        if ko and "MYMEMORY WARNING" not in ko.upper():
            return html_unescape_clean(ko)
    except Exception:
        pass
    return text.strip()


def html_unescape_clean(text):
    text = text.replace("&#39;", "'").replace("&quot;", '"').replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def clean_markdown_to_summary(md_text):
    """README 마크다운에서 배지, 이미지, URL, 다국어 링크 바를 걷어내고 핵심 기능 설명 문장만 추출합니다."""
    if not md_text:
        return ""
    text = re.sub(r"<!--.*?-->", " ", md_text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^\)]*\)", " ", text)
    text = re.sub(r"\[!\[[^\]]*\]\([^\)]*\)\]\([^\)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)

    paragraphs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("|") or line.startswith("---") or line.startswith("==="):
            continue
        if "简体中文" in line or "繁體中文" in line or line.count("·") >= 3:
            continue
        if re.match(r"^(watch|demo|star|follow|sponsor|license|install|see a feature)\b", line, re.I) and len(line) < 75:
            continue
        line = re.sub(r"[*_`~>]+", "", line).strip()
        if len(line) >= 35:
            paragraphs.append(line)
        if len(" ".join(paragraphs)) > 260:
            break

    combined = " ".join(paragraphs)
    combined = re.sub(r"\s+", " ", combined).strip()
    return combined[:280]


def summarize_with_gemini_batch(repos):
    """GEMINI_API_KEY가 설정되어 있으면 Gemini 모델을 호출해 15개 레포의 (2. 내용 요약 / 3. 나에게 주는 도움)을 한 번에 생성합니다."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return False

    items_payload = []
    for r in repos:
        items_payload.append({
            "full_name": r["full_name"],
            "language": r.get("language", "Multi"),
            "description": r.get("description", ""),
            "readme_excerpt": r.get("readme_excerpt", ""),
            "topics": r.get("topics", [])
        })

    prompt = (
        "너는 한국인 개발자를 위한 전담 GitHub 오픈소스 큐레이터야. "
        "다음 JSON 목록에 있는 각 GitHub 레포지토리에 대해 반드시 한국어로 다음 2가지 필드를 작성해서 JSON 배열로만 응답해줘:\n"
        "1) full_name: 원래 레포 이름 그대로\n"
        "2) summary_ko: 이 아이템이 구체적으로 무엇이고 어떤 기능을 하는 도구인지 처음 보는 사람도 단번에 이해할 수 있게 2~3문장으로 명쾌하게 요약\n"
        "3) benefit_ko: 이 아이템이 나에게(실무 코딩/자동화/아키텍처 참고 또는 집 PC 설치/개인 생산성/취미) 구체적으로 어떤 도움이 되고 어떻게 써먹을 수 있는지 1~2문장으로 설명\n\n"
        f"대상 목록:\n{json.dumps(items_payload, ensure_ascii=False)}"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
        text_out = res_json["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text_out)
        lookup = {item["full_name"]: item for item in parsed if isinstance(item, dict) and "full_name" in item}
        for r in repos:
            if r["full_name"] in lookup:
                r["summary_ko"] = lookup[r["full_name"]].get("summary_ko", r.get("summary_ko", ""))
                r["benefit_ko"] = lookup[r["full_name"]].get("benefit_ko", r.get("benefit_ko", ""))
        print("[SUCCESS] Gemini AI API를 통해 15개 아이템의 심층 한국어 요약 및 도움 포인트를 생성했습니다!")
        return True
    except Exception as e:
        print(f"[INFO] Gemini API 호출 실패({e}) -> 내장 README 한국어 번역 분석기로 자동 전환합니다.")
        return False


def generate_personal_benefit(repo, summary_ko):
    blob = f"{repo['full_name']} {repo.get('description', '')} { repo.get('readme_excerpt', '') } {summary_ko} {' '.join(repo.get('topics', []))}".lower()
    lang = repo.get("language", "Multi")
    name = repo["full_name"]

    if "rea" in name.lower() or "reverse engineer" in blob:
        return "상용 앱이나 바이너리가 내부적으로 어떻게 동작하는지 AI 에이전트로 역분석할 때, 또는 복잡한 레거시 코드를 빠르게 파악하는 용도로 큰 도움이 됩니다."
    if "ps5" in blob or "emulator" in blob or "porting" in blob:
        return "콘솔 실행 파일을 윈도우/리눅스용으로 변환하는 리링커(Relinker) 및 시스템 아키텍처 구조를 구경하거나 주말 게임/취미 프로젝트로 돌려보기에 흥미롭습니다."
    if "artcraft" in blob or ("2d" in blob and "3d" in blob and "video" in blob):
        return "프롬프트만으로 다루기 힘들었던 AI 이미지·영상 제작을 2D/3D 시각적 캔버스 위에서 정교하게 연출하고 싶을 때 개인 창작 및 프레젠테이션 도구로 유용합니다."
    if "wordcraft" in blob or ("writing" in blob and "agent" in blob):
        return "기술 블로그 포스팅, 기획서, 사내 문서를 작성할 때 AI와 함께 문단 구조를 다듬고 글쓰기 생산성을 높이는 나만의 에디터로 활용할 수 있습니다."
    if "anatomy" in blob or "isometric" in blob or "svg" in blob:
        return "복잡한 시스템 아키텍처나 기계 구조를 입체적인 아이소메트릭(3D풍) SVG 다이어그램으로 자동 생성해 주어, 발표 자료나 기술 문서 퀄리티를 단숨에 높여줍니다."
    if "math" in name.lower() or "lean" in lang.lower():
        return "OpenAI 등 빅테크가 최신 수학적 추론 및 형식 검증(Formal Verification)을 어떻게 연구하는지 파악하여 AI 엔지니어링 인사이트를 넓히는 데 도움이 됩니다."
    if any(k in blob for k in ["agent", "mcp", "claude", "codex", "skill", "prompt", "llm", "ai", "gpt"]):
        return "현재 쓰시는 AI 에이전트(Antigravity/Claude/Gemini) 환경에 스킬이나 자동화 파이프라인으로 결합하여 업무 및 개인 코딩 속도를 크게 높일 수 있습니다."
    if any(k in blob for k in ["cli", "terminal", "shell", "tui", "command"]):
        return "번거로운 마우스 클릭 대신 터미널 명령어 한 줄로 반복 작업을 끝내는 개인용 단축 유틸리티로 집 PC에 바로 깔아 쓰기 좋습니다."
    if any(k in blob for k in ["self-hosted", "selfhosted", "docker", "home", "nas", "media", "photo", "note", "memo", "rss"]):
        return "유료 클라우드 구독료를 아끼고 집 PC나 개인 서버(Docker)에 직접 띄워 나만의 프라이빗 생산성/미디어 앱으로 평생 무료 활용할 수 있습니다."

    return f"최근 급부상 중인 {lang} 기반 오픈소스로, 핵심 모듈 구조나 아이디어를 내 개인 토이 프로젝트나 실무 코드에 벤치마킹하기 좋습니다."


def enrich_repo_details(repo):
    full_name = repo["full_name"]
    desc = repo.get("description", "").strip()
    topics = repo.get("topics", [])

    if not desc or not repo.get("total_stars"):
        try:
            meta_raw = fetch_url(
                f"https://api.github.com/repos/{full_name}",
                headers={"Accept": "application/vnd.github+json"},
                timeout=8
            )
            meta = json.loads(meta_raw)
            if meta.get("description"):
                desc = meta["description"].strip()
            if meta.get("topics"):
                topics = meta["topics"]
            if not repo.get("total_stars") and meta.get("stargazers_count"):
                repo["total_stars"] = meta["stargazers_count"]
        except Exception:
            pass

    readme_excerpt = ""
    try:
        readme_raw = fetch_url(
            f"https://api.github.com/repos/{full_name}/readme",
            headers={"Accept": "application/vnd.github+json"},
            timeout=8
        )
        readme_json = json.loads(readme_raw)
        if readme_json.get("content"):
            decoded = base64.b64decode(readme_json["content"]).decode("utf-8", errors="replace")
            readme_excerpt = clean_markdown_to_summary(decoded)
    except Exception:
        pass

    repo["description"] = desc
    repo["readme_excerpt"] = readme_excerpt
    repo["topics"] = topics[:5]

    combined_en = f"{desc}. {readme_excerpt}".strip(". ") if (desc and readme_excerpt and readme_excerpt[:25] not in desc) else (desc or readme_excerpt)
    if combined_en:
        summary_ko = translate_en_to_ko(combined_en)
    else:
        summary_ko = f"{full_name} ({repo.get('language', 'Multi')}) 기반의 오픈소스 도구로, 최근 개발자 커뮤니티에서 빠르게 별점이 급증하고 있는 프로젝트입니다."

    repo["summary_ko"] = summary_ko
    repo["benefit_ko"] = generate_personal_benefit(repo, summary_ko)
    return repo


def fetch_github_trending(since="weekly"):
    url = f"https://github.com/trending?since={since}"
    html = fetch_url(url)
    articles = re.findall(r'<article class="Box-row">(.*?)</article>', html, re.S)
    repos = []

    for art in articles:
        m_name = re.search(r'<h2[^>]*>.*?<a[^>]+href="/([^"/]+/[^"/]+)"', art, re.S)
        if not m_name:
            continue
        full_name = m_name.group(1).strip()

        m_desc = re.search(r'<p class="col-9[^"]*">(.*?)</p>', art, re.S)
        desc = re.sub(r"<[^>]+>", "", m_desc.group(1)).strip() if m_desc else ""
        desc = re.sub(r"\s+", " ", desc)

        m_lang = re.search(r'itemprop="programmingLanguage">(.*?)</span>', art, re.S)
        lang = m_lang.group(1).strip() if m_lang else "Multi"

        m_total = re.search(r'href="/' + re.escape(full_name) + r'/stargazers"[^>]*>.*?</svg>\s*([0-9,]+)', art, re.S)
        total_stars = int(m_total.group(1).replace(",", "")) if m_total else 0

        m_gained = re.search(r'([0-9,]+)\s+stars\s+(?:this\s+week|today|this\s+month)', art, re.I)
        gained_stars = int(m_gained.group(1).replace(",", "")) if m_gained else 0

        repos.append({
            "full_name": full_name,
            "url": f"https://github.com/{full_name}",
            "description": desc,
            "language": lang,
            "total_stars": total_stars,
            "gained_stars": gained_stars,
            "topics": [],
            "source": f"GitHub Trending ({since})"
        })
    return repos


def fetch_newly_surging_repos(days=14):
    created_since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    query = urllib.parse.quote(f"created:>{created_since} stars:>150")
    url = f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page=15"
    try:
        raw = fetch_url(url, headers={"Accept": "application/vnd.github+json"})
        data = json.loads(raw)
        items = []
        for item in data.get("items", []):
            items.append({
                "full_name": item["full_name"],
                "url": item["html_url"],
                "description": item.get("description") or "",
                "language": item.get("language") or "Multi",
                "total_stars": item.get("stargazers_count", 0),
                "gained_stars": item.get("stargazers_count", 0),
                "topics": item.get("topics", []),
                "source": f"신생 급등 (최근 {days}일 내 생성)"
            })
        return items
    except Exception:
        return []


def fetch_topic_repos(topic_query, label):
    pushed_since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    q = urllib.parse.quote(f"{topic_query} pushed:>{pushed_since} stars:>500")
    url = f"https://api.github.com/search/repositories?q={q}&sort=updated&order=desc&per_page=10"
    try:
        raw = fetch_url(url, headers={"Accept": "application/vnd.github+json"})
        data = json.loads(raw)
        items = []
        for item in data.get("items", []):
            items.append({
                "full_name": item["full_name"],
                "url": item["html_url"],
                "description": item.get("description") or "",
                "language": item.get("language") or "Multi",
                "total_stars": item.get("stargazers_count", 0),
                "gained_stars": 0,
                "topics": item.get("topics", []),
                "source": label
            })
        return items
    except Exception:
        return []


def categorize_repos(trending_repos, surging_repos, config):
    count = config["curation"].get("items_per_section", 5)
    work_kw = [k.lower() for k in config["curation"].get("work_keywords", [])]
    fun_kw = [k.lower() for k in config["curation"].get("fun_keywords", [])]

    combined_surge = {r["full_name"]: r for r in (trending_repos + surging_repos)}.values()
    surge_list = sorted(combined_surge, key=lambda x: (x["gained_stars"], x["total_stars"]), reverse=True)[:count]
    used_names = {r["full_name"] for r in surge_list}

    work_candidates = []
    fun_candidates = []

    pool = [r for r in (trending_repos + surging_repos) if r["full_name"] not in used_names]
    for r in pool:
        text = f"{r['full_name']} {r['description']} {r['language']}".lower()
        fun_score = sum(1 for k in fun_kw if k in text)
        work_score = sum(1 for k in work_kw if k in text)
        if fun_score > work_score:
            fun_candidates.append(r)
        else:
            work_candidates.append(r)

    work_list = sorted(work_candidates, key=lambda x: (x["gained_stars"], x["total_stars"]), reverse=True)[:count]
    used_names.update(r["full_name"] for r in work_list)

    if len(fun_candidates) < count:
        extra_fun = fetch_topic_repos("topic:self-hosted OR topic:cli OR topic:productivity", "개인/취미 추천")
        for ef in extra_fun:
            if ef["full_name"] not in used_names:
                fun_candidates.append(ef)

    fun_list = sorted(fun_candidates, key=lambda x: (x["gained_stars"], x["total_stars"]), reverse=True)[:count]

    all_selected = surge_list + work_list + fun_list
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(enrich_repo_details, all_selected))

    # GEMINI_API_KEY가 있으면 Gemini AI로 한 번 더 고품질 요약 덮어쓰기
    summarize_with_gemini_batch(all_selected)

    return surge_list, work_list, fun_list


def render_section_html(title, subtitle, accent_color, bg_soft, repos, category):
    cards_html = ""
    for idx, r in enumerate(repos, 1):
        gained = r["gained_stars"]
        total = r["total_stars"]
        lang = r.get("language", "Multi")
        if category == "surge":
            badge = f"🔥 기간 내 +{gained:,}⭐ 급등 (총 {total:,}⭐)" if gained > 0 else f"🔥 총 {total:,}⭐"
        else:
            badge = f"⭐ 누적 {total:,}" + (f" (최근 +{gained:,}⭐)" if gained > 0 else "")

        topics_html = ""
        if r.get("topics"):
            tags = "".join(
                f'<span style="display:inline-block; background:#0f172a; color:#94a3b8; border:1px solid #334155; padding:1px 8px; border-radius:999px; font-size:11px; margin-right:5px;">#{escape(t)}</span>'
                for t in r["topics"]
            )
            topics_html = f'<div style="margin-top:6px;">{tags}</div>'

        cards_html += f"""
        <div style="background:#1e293b; border:1px solid #334155; border-left:5px solid {accent_color}; border-radius:12px; padding:18px 20px; margin-bottom:16px;">
          <!-- 1. 아이템 -->
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:10px; border-bottom:1px solid #334155; padding-bottom:10px;">
            <div>
              <span style="background:{bg_soft}; color:{accent_color}; padding:2px 8px; border-radius:6px; font-size:11.5px; font-weight:800; margin-right:6px;">
                1. 아이템
              </span>
              <a href="{escape(r['url'])}" target="_blank" style="color:#38bdf8; font-size:17px; font-weight:800; text-decoration:none;">
                {idx}. {escape(r['full_name'])}
              </a>
              <span style="color:#94a3b8; font-size:12px; margin-left:6px;">[{escape(lang)}]</span>
            </div>
            <span style="background:{bg_soft}; color:{accent_color}; padding:4px 10px; border-radius:999px; font-size:12px; font-weight:700;">
              {escape(badge)}
            </span>
          </div>

          <!-- 2. 이 아이템의 내용 요약 -->
          <div style="background:#0f172a; border:1px solid #1e293b; border-radius:8px; padding:12px 14px; margin-bottom:10px;">
            <div style="color:#fbbf24; font-size:12px; font-weight:800; margin-bottom:4px;">
              📌 2. 이 아이템의 내용 요약 (무엇인가요?)
            </div>
            <p style="color:#f8fafc; font-size:13.8px; margin:0; line-height:1.6;">
              {escape(r.get('summary_ko', ''))}
            </p>
            {topics_html}
          </div>

          <!-- 3. 이 아이템이 나한테 어떤 도움이 될 것 같은지 -->
          <div style="background:rgba(56,189,248,0.08); border-left:3px solid #38bdf8; border-radius:6px; padding:11px 14px;">
            <div style="color:#38bdf8; font-size:12px; font-weight:800; margin-bottom:3px;">
              💡 3. 이 아이템이 나한테 어떤 도움이 될까요? (활용 포인트)
            </div>
            <p style="color:#e0f2fe; font-size:13.3px; margin:0; line-height:1.55;">
              {escape(r.get('benefit_ko', ''))}
            </p>
          </div>
        </div>
        """

    return f"""
    <div style="margin-bottom:32px;">
      <h2 style="color:{accent_color}; font-size:19px; font-weight:800; margin-bottom:4px;">{escape(title)}</h2>
      <p style="color:#94a3b8; font-size:13px; margin-bottom:14px;">{escape(subtitle)}</p>
      {cards_html}
    </div>
    """


def generate_newsletter_html(surge_list, work_list, fun_list, config):
    now_str = datetime.now().strftime("%Y-%m-%d (%a) %H:%M KST")
    sched = config["schedule"]
    sched_desc = f"매주 {sched['day_of_week']} {sched['hour_kst']:02d}:{sched['minute_kst']:02d} KST"

    sec1 = render_section_html(
        "🔥 섹션 1. 최근 별점 급등 (Star Velocity TOP 5)",
        "이번 주 가장 폭발적으로 별점이 치솟았거나 최근 14일 내 등장해 떡상 중인 핫 이슈 레포지토리",
        "#fbbf24", "rgba(251,191,36,0.15)", surge_list, "surge"
    )
    sec2 = render_section_html(
        "💼 섹션 2. 업무 & 개발 생산성 추천 (Work & DevTools TOP 5)",
        "실무 아키텍처, AI 에이전트, 자동화 및 개발 효율을 높여주는 엔지니어링 오픈소스",
        "#38bdf8", "rgba(56,189,248,0.15)", work_list, "work"
    )
    sec3 = render_section_html(
        "🎮 섹션 3. 개인 활용 & 재미 추천 (Personal & Fun TOP 5)",
        "집 PC에 바로 깔아보기 좋은 셀프호스팅 앱, 터미널 유틸리티, 주말 장난감 프로젝트",
        "#f472b6", "rgba(244,114,182,0.15)", fun_list, "fun"
    )

    all_links = "".join(
        f'<li style="margin-bottom:6px;"><a href="{escape(r["url"])}" target="_blank" style="color:#38bdf8; text-decoration:none; font-weight:700;">{escape(r["full_name"])}</a> (⭐ {r["total_stars"]:,}) — <span style="color:#94a3b8;">{escape(r["url"])}</span></li>'
        for r in sorted(surge_list + work_list + fun_list, key=lambda x: x["total_stars"], reverse=True)
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <title>GitHub 트렌드 올인원 이메일 매거진</title>
</head>
<body style="margin:0; padding:28px 16px; background-color:#0f172a; color:#f8fafc; font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:780px; margin:0 auto; background:#0f172a;">
    <div style="background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%); border:1px solid #334155; border-radius:14px; padding:24px; margin-bottom:24px;">
      <div style="display:inline-block; background:rgba(56,189,248,0.15); color:#38bdf8; padding:4px 10px; border-radius:999px; font-size:12px; font-weight:700; margin-bottom:8px;">
        📬 Automated GitHub Trend Digest ({escape(sched_desc)})
      </div>
      <h1 style="margin:0 0 6px 0; font-size:22px; color:#f8fafc;">이번 주 GitHub 트렌드 올인원 매거진 (급등 · 업무 · 개인)</h1>
      <p style="margin:0; color:#94a3b8; font-size:13px;">발행 시각: {escape(now_str)} | 수신: kusari0215@naver.com | 모든 아이템은 [1. 아이템 → 2. 내용 요약(한국어) → 3. 나에게 주는 도움]으로 구성됩니다.</p>
    </div>

    {sec1}
    {sec2}
    {sec3}

    <div style="background:#1e293b; border:1px solid #334155; border-radius:12px; padding:18px 20px; margin-top:24px;">
      <h3 style="margin:0 0 10px 0; font-size:15px; color:#f8fafc;">🔗 이번 호에 수록된 전체 GitHub 주소 (⭐ 누적 별점 내림차순)</h3>
      <ul style="margin:0; padding-left:18px; font-size:13px; color:#cbd5e1; line-height:1.6;">
        {all_links}
      </ul>
    </div>
  </div>
</body>
</html>"""


def send_email_if_configured(html_body, config):
    recipient = os.environ.get("RECIPIENT_EMAIL", "kusari0215@naver.com").strip()
    subject_prefix = config["email"].get("subject_prefix", "📬 [GitHub 트렌드 매거진]")
    subject = f"{subject_prefix} {datetime.now().strftime('%Y-%m-%d')} 별점 급등 · 업무 · 개인 추천 리포트"

    # 방법 1: 네이버 설정 변경 없이 Resend API 키 하나로 즉시 발송 (RESEND_API_KEY 설정 시)
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    if resend_key:
        payload = json.dumps({
            "from": "GitHub Trend Digest <onboarding@resend.dev>",
            "to": [recipient],
            "subject": subject,
            "html": html_body
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
                "User-Agent": "GitHubTrendMailer/3.0"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[SUCCESS] Resend API를 통해 이메일 발송 완료! -> {recipient} ({resp.status})")
            return True

    # 방법 2: 내 네이버/Gmail 계정(SMTP)으로 직접 발송
    smtp_host = os.environ.get("SMTP_HOST", "smtp.naver.com").strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "587").strip())
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASSWORD", "").strip()

    if not smtp_user or not smtp_pass:
        print(f"[INFO] 수신처({recipient})가 지정되었으나 발송 인증키(SMTP_PASSWORD 또는 RESEND_API_KEY)가 아직 입력되지 않아 HTML 미리보기만 갱신했습니다.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [recipient], msg.as_string())

    print(f"[SUCCESS] SMTP를 통해 이메일 발송 완료! -> {recipient}")
    return True


def main():
    load_dotenv()
    config = load_config()
    window = config["curation"].get("trending_window", "weekly")
    days = config["curation"].get("surge_created_within_days", 14)

    print(f"[1/4] GitHub Trending ({window}) 및 최근 {days}일 내 별점 급등 레포 수집 중...")
    trending = fetch_github_trending(since=window)
    surging = fetch_newly_surging_repos(days=days)

    print(f"[2/4] 3대 섹션 분류 및 각 레포지토리 README 심층 분석 + 한국어 번역 중 (총 15개)...")
    surge_list, work_list, fun_list = categorize_repos(trending, surging, config)

    print("[3/4] 3단 구조(1.아이템 / 2.내용요약 / 3.나에게 주는 도움) HTML 뉴스레터 생성 중...")
    html_body = generate_newsletter_html(surge_list, work_list, fun_list, config)

    with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
        f.write(html_body)
    print(f"[4/4] 샘플 뉴스레터 HTML 저장 완료: {PREVIEW_PATH}")

    send_email_if_configured(html_body, config)


if __name__ == "__main__":
    main()
