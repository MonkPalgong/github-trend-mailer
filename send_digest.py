import argparse
import base64
import json
import math
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
CACHE_JSON_PATH = os.path.join(BASE_DIR, "latest_curated_repos.json")


def load_dotenv():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)



def fetch_url(url, headers=None, timeout=12):
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GitHubTrendMailer/5.1"
    }
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if gh_token and "api.github.com" in url:
        req_headers["Authorization"] = f"Bearer {gh_token}"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def has_word(text, words):
    """부분 문자열('Rain' 안의 'ai', 'supporting' 안의 'porting') 오매칭을 막기 위해 정확한 단어 경계(\\b)로 검사합니다."""
    if isinstance(words, str):
        words = [words]
    for w in words:
        pattern = r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, text.lower()):
            return True
    return False


def has_cjk_only_without_korean(text):
    """설명이 중국어로만 되어 있고 영어/한국어가 거의 없는 레포인지 판별합니다."""
    if not text:
        return False
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    en_ko_count = len(re.findall(r"[a-zA-Z가-힣]", text))
    return cjk_count > 4 and en_ko_count < cjk_count


def preprocess_en_dev_terms(text):
    """기계 번역기가 개발 용어(Agents->상담원, Mermaid->인어, Claude->끌로드, Metal->금속)를 오역하지 않도록 영문 단계에서 보정합니다."""
    if not text:
        return ""
    replacements = [
        (r"&amp;", "&"),
        (r"&#39;", "'"),
        (r"&quot;", '"'),
        (r"\bAI\s+agents\b", "AI 에이전트"),
        (r"\bagents\b", "AI 에이전트"),
        (r"\bagent\s+skill\b", "AI 에이전트 스킬"),
        (r"\bagent\s+skills\b", "AI 에이전트 스킬"),
        (r"\bagent\b", "AI 에이전트"),
        (r"\bClaude\s+Code\b", "Claude Code"),
        (r"\bClaude\s+skill\b", "Claude 스킬"),
        (r"\bClaude\b", "Claude"),
        (r"\bMermaid\b", "Mermaid 다이어그램"),
        (r"\bMetal\s+driver\b", "Apple Metal 그래픽 드라이버"),
        (r"\bclean-room\b", "클린룸"),
        (r"\bself-hosted\b", "셀프호스팅"),
        (r"\bon-device\b", "온디바이스"),
        (r"\breverse\s+engineer(?:ing)?\b", "리버스 엔지니어링"),
    ]
    out = text
    for pat, rep in replacements:
        out = re.sub(pat, rep, out, flags=re.I)
    return out


def postprocess_ko_dev_terms(text):
    """번역 완료 후 남은 기계 번역투 문장, 중국어 잔재, 잘린 문장을 자연스러운 한국어로 정돈합니다."""
    if not text:
        return ""
    out = text
    out = re.sub(r"[\u4e00-\u9fff《》【】]+[^\.\n]*[\.\s]*", " ", out)
    fixes = {
        "&amp;": "&",
        "&#39;": "'",
        "&quot;": '"',
        "상담원": "AI 에이전트",
        "대리인": "AI 에이전트",
        "끌로드": "Claude",
        "인어 기울기 없음": "Mermaid 다이어그램 왜곡 없음",
        "인어가 기울어지지 않습니다": "Mermaid 다이어그램이 깨지지 않습니다",
        "인어": "Mermaid 다이어그램",
        "금속 드라이버": "Apple Metal 그래픽 드라이버",
        "클린 룸": "클린룸(독립 재구현)",
        "클린룸": "클린룸(독립 재구현)",
        "재링커": "바이너리 리링커(Relinker)",
        "등각 그림": "3D 아이소메트릭 다이어그램",
        "등각 피규어": "3D 아이소메트릭 도식",
        "문서온디바이스": "문서, 온디바이스",
        "AI AI 에이전트": "AI 에이전트",
        "팽창을 제거하고": "블로트웨어(불필요한 기본 앱)를 제거하고",
        "바이러스성 트윗": "화제의 트윗",
        "애플 TV 스타일의 집": "Apple TV 스타일의 홈 화면",
        "Apple TV 스타일의 집": "Apple TV 스타일의 홈 화면",
    }
    for k, v in fixes.items():
        out = out.replace(k, v)
    out = re.sub(r"[\u4e00-\u9fff]+", " ", out)
    out = re.sub(r"\.\.+", ".", out)
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > 90 and not out.endswith((".", "다", "요", "!", "니다")):
        last_dot = max(out.rfind("."), out.rfind("다."), out.rfind("요."))
        if last_dot > 45:
            out = out[: last_dot + 1].strip()
    return out


def translate_en_to_ko(text):
    """Google 무료 번역 엔드포인트 + MyMemory 폴백으로 영어 원문을 한국어로 옮깁니다."""
    if not text or not text.strip():
        return ""
    if len(re.findall(r"[가-힣]", text)) > 15:
        return postprocess_ko_dev_terms(text.strip())

    prepped = preprocess_en_dev_terms(text.strip()[:380])

    # 1차: Google Translate 무료 gtx 엔드포인트
    try:
        q = urllib.parse.quote(prepped)
        g_url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ko&dt=t&q={q}"
        raw = fetch_url(g_url, timeout=8)
        arr = json.loads(raw)
        if isinstance(arr, list) and arr and isinstance(arr[0], list):
            ko_parts = [seg[0] for seg in arr[0] if isinstance(seg, list) and seg and seg[0]]
            ko = "".join(ko_parts).strip()
            if len(re.findall(r"[가-힣]", ko)) >= 8:
                return postprocess_ko_dev_terms(html_unescape_clean(ko))
    except Exception:
        pass

    # 2차: MyMemory API 폴백
    try:
        q = urllib.parse.quote(prepped)
        url = f"https://api.mymemory.translated.net/get?q={q}&langpair=en|ko"
        raw = fetch_url(url, timeout=8)
        data = json.loads(raw)
        ko = data.get("responseData", {}).get("translatedText", "")
        if ko and "MYMEMORY WARNING" not in ko.upper() and len(re.findall(r"[가-힣]", ko)) >= 8:
            return postprocess_ko_dev_terms(html_unescape_clean(ko))
    except Exception:
        pass

    return postprocess_ko_dev_terms(prepped)


def html_unescape_clean(text):
    text = text.replace("&#39;", "'").replace("&quot;", '"').replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def clean_markdown_to_summary(md_text):
    """README 마크다운에서 배지, 이미지, 설치 경고, 다국어 링크 바를 제거하고 핵심 기능 설명 문장만 추출합니다."""
    if not md_text:
        return ""
    text = re.sub(r"<!--.*?-->", " ", md_text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^\)]*\)", " ", text)
    text = re.sub(r"\[!\[[^\]]*\]\([^\)]*\)\]\([^\)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\u4e00-\u9fff]+.*", " ", text)

    paragraphs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("#", "|", "---", "===", ">")):
            continue
        if line.count("·") >= 2 or "npx skill add" in line.lower():
            continue
        if re.search(r"(follow the install|similar repo names|copied readmes|powershell says scripts cannot)", line, re.I):
            continue
        if re.match(r"^(watch|demo|star|follow|sponsor|license|install|see a feature|warning|note)\b", line, re.I) and len(line) < 85:
            continue
        line = re.sub(r"[*_`~>]+", "", line).strip()
        if len(line) >= 30:
            paragraphs.append(line)
        if len(" ".join(paragraphs)) > 240:
            break

    combined = " ".join(paragraphs)
    combined = re.sub(r"\s+", " ", combined).strip()
    return combined[:260]


def enrich_repo_details(repo):
    full_name = repo["full_name"]
    desc = html_unescape_clean(repo.get("description", "").strip())
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
                desc = html_unescape_clean(meta["description"].strip())
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

    return repo


def fetch_github_trending(since="daily"):
    url = f"https://github.com/trending?since={since}"
    html = fetch_url(url)
    articles = re.findall(r'<article class="Box-row">(.*?)</article>', html, re.S)
    repos = []

    divisor = 1 if since == "daily" else (7 if since == "weekly" else 30)

    for art in articles:
        m_name = re.search(r'<h2[^>]*>.*?<a[^>]+href="/([^"/]+/[^"/]+)"', art, re.S)
        if not m_name:
            continue
        full_name = m_name.group(1).strip()

        m_desc = re.search(r'<p class="col-9[^"]*">(.*?)</p>', art, re.S)
        desc = re.sub(r"<[^>]+>", "", m_desc.group(1)).strip() if m_desc else ""
        desc = html_unescape_clean(re.sub(r"\s+", " ", desc))

        m_lang = re.search(r'itemprop="programmingLanguage">(.*?)</span>', art, re.S)
        lang = m_lang.group(1).strip() if m_lang else "Multi"

        m_total = re.search(r'href="/' + re.escape(full_name) + r'/stargazers"[^>]*>.*?</svg>\s*([0-9,]+)', art, re.S)
        total_stars = int(m_total.group(1).replace(",", "")) if m_total else 0

        m_gained = re.search(r'([0-9,]+)\s+stars\s+(?:this\s+week|today|this\s+month)', art, re.I)
        gained_stars = int(m_gained.group(1).replace(",", "")) if m_gained else 0
        daily_velocity = max(1, int(round(gained_stars / divisor))) if gained_stars > 0 else 10

        repos.append({
            "full_name": full_name,
            "owner": full_name.split("/")[0].lower(),
            "url": f"https://github.com/{full_name}",
            "description": desc,
            "language": lang,
            "total_stars": total_stars,
            "gained_stars": gained_stars,
            "weekly_gained": gained_stars if since == "weekly" else 0,
            "daily_velocity": daily_velocity,
            "days_alive": 365,
            "is_new_rookie": False,
            "topics": [],
            "source": f"GitHub Trending ({since})"
        })
    return repos


def fetch_multi_horizon_trending():
    """
    [v5.1 핵심 업그레이드]
    단일 윈도우('daily'만 조회)로 인해 주간 최대 핫이슈(morluto/rea +248,962⭐, AnyPS5 등)가
    GitHub 일간 회전으로 누락되는 사각지대(Blind Spot)를 없애기 위해,
    Daily Trending과 Weekly Trending을 동시에 병렬 수집하여 합집합(Union)을 반환합니다.
    """
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_daily = ex.submit(fetch_github_trending, "daily")
        fut_weekly = ex.submit(fetch_github_trending, "weekly")
        daily_repos = fut_daily.result()
        weekly_repos = fut_weekly.result()
    return daily_repos + weekly_repos


def fetch_newly_surging_repos(days=14):
    now_utc = datetime.now(timezone.utc)
    created_since = (now_utc - timedelta(days=days)).strftime("%Y-%m-%d")
    query = urllib.parse.quote(f"created:>{created_since} stars:>300")
    url = f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page=20"
    try:
        raw = fetch_url(url, headers={"Accept": "application/vnd.github+json"})
        data = json.loads(raw)
        items = []
        for item in data.get("items", []):
            desc = html_unescape_clean(item.get("description") or "")
            if has_cjk_only_without_korean(desc):
                continue
            full_name = item["full_name"]
            total_stars = item.get("stargazers_count", 0)
            created_str = item.get("created_at", "")
            days_alive = 7
            if created_str:
                try:
                    dt_created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    days_alive = max(1, (now_utc - dt_created).days)
                except Exception:
                    days_alive = 7
            daily_velocity = max(1, int(round(total_stars / days_alive)))
            items.append({
                "full_name": full_name,
                "owner": full_name.split("/")[0].lower(),
                "url": item["html_url"],
                "description": desc,
                "language": item.get("language") or "Multi",
                "total_stars": total_stars,
                "gained_stars": daily_velocity,
                "weekly_gained": 0,
                "daily_velocity": daily_velocity,
                "days_alive": days_alive,
                "is_new_rookie": True,
                "topics": item.get("topics", []),
                "source": f"신생 루키 (생성 {days_alive}일차, 일평균 +{daily_velocity:,}⭐)"
            })
        return items
    except Exception:
        return []


def fetch_topic_repos(topic_query, label):
    pushed_since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    q = urllib.parse.quote(f"{topic_query} pushed:>{pushed_since} stars:>2000")
    url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page=12"
    try:
        raw = fetch_url(url, headers={"Accept": "application/vnd.github+json"})
        data = json.loads(raw)
        items = []
        for item in data.get("items", []):
            desc = html_unescape_clean(item.get("description") or "")
            if has_cjk_only_without_korean(desc):
                continue
            full_name = item["full_name"]
            items.append({
                "full_name": full_name,
                "owner": full_name.split("/")[0].lower(),
                "url": item["html_url"],
                "description": desc,
                "language": item.get("language") or "Multi",
                "total_stars": item.get("stargazers_count", 0),
                "gained_stars": 50,
                "weekly_gained": 0,
                "daily_velocity": 50,
                "days_alive": 365,
                "is_new_rookie": False,
                "topics": item.get("topics", []),
                "source": label
            })
        return items
    except Exception:
        return []


def compute_authority_score(repo):
    """
    [v5.1 하이브리드 권위 점수]
    단기 급등(Velocity)에만 매몰되어 5만~30만 스타급 기존 상위권 명작(obra/superpowers, mattpocock/skills, affaan-m/ECC 등)이
    탈락하는 현상을 막기 위해, 누적 별점 로그 가중치(70%) + 일일 모멘텀(30%) + 기존 트렌딩 대장주 보너스를 결합합니다.
    """
    total = max(repo.get("total_stars", 0), 10)
    vel = max(repo.get("daily_velocity", 1), 1)
    established_bonus = 2.0 if (not repo.get("is_new_rookie") and total >= 15000) else 0.0
    return (math.log10(total) * 3.0) + (math.log10(vel) * 1.2) + established_bonus


def select_with_owner_cap(candidates, count, used_names, used_owners, max_per_owner=1):
    """동일 제작자(Owner)의 시리즈물이 뉴스레터를 도배하지 못하도록 계정당 최대 1개(max_per_owner)만 선발합니다."""
    selected = []
    for r in candidates:
        name = r["full_name"]
        owner = r.get("owner") or name.split("/")[0].lower()
        if name in used_names:
            continue
        if used_owners.get(owner, 0) >= max_per_owner:
            continue
        selected.append(r)
        used_names.add(name)
        used_owners[owner] = used_owners.get(owner, 0) + 1
        if len(selected) >= count:
            break
    return selected


# Aliases Google keeps pointing at current models, tried in order, so a retired model id cannot break the run.
GEMINI_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest"]
CANDIDATE_POOL_SIZE = 30
EXTRA_FUN_POOL_SIZE = 8

GEMINI_RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "full_name": {"type": "STRING"},
            "section": {"type": "STRING", "enum": ["work", "fun", "skip"]},
            "summary_ko": {"type": "STRING"},
            "benefit_ko": {"type": "STRING"},
        },
        "required": ["full_name", "section", "summary_ko", "benefit_ko"],
    },
}


def curate_with_gemini(repos):
    """Ask Gemini to classify each repo (work/fun/skip) and write its Korean summary and use case.

    Returns {full_name: item} on success, or None when the key is missing or the call fails.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None

    items_payload = [
        {
            "full_name": r["full_name"],
            "language": r.get("language", "Multi"),
            "description": r.get("description", ""),
            "readme_excerpt": r.get("readme_excerpt", ""),
            "topics": r.get("topics", []),
        }
        for r in repos
    ]

    prompt = (
        "너는 한국인 개발자에게 GitHub 오픈소스를 골라 소개하는 큐레이터야. "
        "아래 JSON 목록의 레포마다 다음 필드를 채워 JSON 배열로 답해.\n\n"
        "- full_name: 입력 그대로\n"
        "- section: 이 레포를 어디에 실을지\n"
        "  - work: 개발·업무에 쓰는 도구, 라이브러리, 프레임워크, AI 에이전트·코딩 도구, 인프라\n"
        "  - fun: 개인 PC·집에서 쓰는 앱, 셀프호스팅, 게임, 미디어, 취미·DIY 하드웨어, 개인 생산성\n"
        "  - skip: 소개할 가치가 낮음 (목록·링크 모음, 과제 저장소, 설명이 거의 없음, 스팸)\n"
        "- summary_ko: 이 레포가 무엇이고 무엇을 하는지 2문장 이내 한국어. 과장·홍보 문구 없이 사실만.\n"
        "- benefit_ko: 개발자인 내가 이걸 어떻게 써볼 수 있는지 구체적인 상황 1문장. "
        "레포 이름으로 시작하지 말고, 다른 레포와 같은 문장 구조를 반복하지 마.\n\n"
        "기술 용어는 원어를 살려 (Agent를 '상담원', Mermaid를 '인어'로 옮기지 마).\n\n"
        f"목록:\n{json.dumps(items_payload, ensure_ascii=False)}"
    )

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_RESPONSE_SCHEMA,
        },
    }).encode("utf-8")

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
            text_out = res_json["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text_out)
            result = {item["full_name"]: item for item in parsed if isinstance(item, dict) and item.get("full_name")}
            print(f"[INFO] Gemini({model})가 {len(result)}/{len(repos)}개 레포를 분류·요약했습니다.")
            return result
        except Exception as e:
            print(f"[WARN] Gemini({model}) 호출 실패: {e}")

    print("[WARN] 모든 Gemini 모델 호출 실패 -> 키워드 분류 + 설명 번역으로 대체합니다.")
    return None


def apply_fallback_summaries(repos):
    """Without Gemini: translate the description as the summary and leave the use case empty."""
    for r in repos:
        desc = r.get("description", "")
        readme = r.get("readme_excerpt", "")
        combined_en = (
            f"{desc}. {readme}".strip(". ")
            if (desc and readme and readme[:25].lower() not in desc.lower())
            else (desc or readme)
        )
        r["summary_ko"] = translate_en_to_ko(combined_en) or desc
        r["benefit_ko"] = ""


def keyword_section(r, work_kw, fun_kw):
    text = f"{r['full_name']} {r['description']} {r['language']} {' '.join(r.get('topics', []))}".lower()
    fun_score = sum(1 for k in fun_kw if has_word(text, k))
    work_score = sum(1 for k in work_kw if has_word(text, k))
    return "fun" if fun_score > work_score else "work"


def categorize_repos(trending_repos, surging_repos, config):
    """
    섹션 1 (급등): 일평균 별 증가 속도 순.
    섹션 2·3 (업무·취미): 권위 점수 상위 후보를 Gemini가 work/fun/skip으로 분류하고 요약.
    Gemini를 못 쓰면 config 키워드로 분류하고 설명을 번역해 요약으로 씁니다.
    """
    count = config["curation"].get("items_per_section", 5)
    work_kw = [k.lower() for k in config["curation"].get("work_keywords", [])]
    fun_kw = [k.lower() for k in config["curation"].get("fun_keywords", [])]

    merged_map = {}
    for r in surging_repos + trending_repos:
        if has_cjk_only_without_korean(r.get("description", "")):
            continue
        fname = r["full_name"]
        if fname in merged_map:
            merged_map[fname]["total_stars"] = max(merged_map[fname]["total_stars"], r["total_stars"])
            merged_map[fname]["daily_velocity"] = max(merged_map[fname]["daily_velocity"], r["daily_velocity"])
            merged_map[fname]["weekly_gained"] = max(merged_map[fname].get("weekly_gained", 0), r.get("weekly_gained", 0))
            if r.get("topics"):
                merged_map[fname]["topics"] = r["topics"]
            if not r.get("is_new_rookie"):
                merged_map[fname]["is_new_rookie"] = False
        else:
            merged_map[fname] = r

    combined_all = list(merged_map.values())
    used_names = set()
    used_owners = {}

    # 1. 섹션 1: 일평균 별점 상승 속도 TOP N (동일 제작자 최대 1개)
    surge_sorted = sorted(combined_all, key=lambda x: (x["daily_velocity"], x["total_stars"]), reverse=True)
    surge_list = select_with_owner_cap(surge_sorted, count, used_names, used_owners, max_per_owner=1)

    # 2. 업무·취미 후보 풀: 권위 점수 상위 + 셀프호스팅·데스크톱 앱 보강분
    rest = [r for r in combined_all if r["full_name"] not in used_names]
    pool = sorted(rest, key=lambda x: (compute_authority_score(x), x["total_stars"]), reverse=True)[:CANDIDATE_POOL_SIZE]
    pool_names = {r["full_name"] for r in pool} | used_names
    # GitHub search rejects OR between qualifiers, so query each topic separately.
    extra_fun = []
    for topic in ("self-hosted", "desktop-app", "productivity"):
        for r in fetch_topic_repos(f"topic:{topic}", "셀프호스팅·데스크톱 앱"):
            if r["full_name"] not in pool_names:
                r["from_fun_topic"] = True
                extra_fun.append(r)
                pool_names.add(r["full_name"])
    extra_fun.sort(key=lambda x: x["total_stars"], reverse=True)
    pool += extra_fun[:EXTRA_FUN_POOL_SIZE]

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(enrich_repo_details, surge_list + pool))

    curated = curate_with_gemini(surge_list + pool)
    for r in surge_list + pool:
        item = (curated or {}).get(r["full_name"])
        if item:
            r["section"] = item["section"]
            r["summary_ko"] = postprocess_ko_dev_terms(item["summary_ko"])
            r["benefit_ko"] = postprocess_ko_dev_terms(item["benefit_ko"])
        else:
            r["section"] = "fun" if r.get("from_fun_topic") else keyword_section(r, work_kw, fun_kw)

    pool_sorted = sorted(pool, key=lambda x: (compute_authority_score(x), x["total_stars"]), reverse=True)
    work_list = select_with_owner_cap([r for r in pool_sorted if r["section"] == "work"], count, used_names, used_owners)
    fun_list = select_with_owner_cap([r for r in pool_sorted if r["section"] == "fun"], count, used_names, used_owners)
    # Keep sections full when one category runs short.
    leftovers = [r for r in pool_sorted if r["section"] != "skip"]
    work_list += select_with_owner_cap(leftovers, count - len(work_list), used_names, used_owners) if len(work_list) < count else []
    fun_list += select_with_owner_cap(leftovers, count - len(fun_list), used_names, used_owners) if len(fun_list) < count else []

    apply_fallback_summaries([r for r in surge_list + work_list + fun_list if "summary_ko" not in r])

    return surge_list, work_list, fun_list


KST = timezone(timedelta(hours=9))
KO_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
CRON_DAY_KO = {"MON": "월", "TUE": "화", "WED": "수", "THU": "목", "FRI": "금", "SAT": "토", "SUN": "일"}

# Mail palette: one ground, one ink, one accent; surfaces separated by tone, not lines.
GROUND = "#eef0f2"
SURFACE = "#ffffff"
INK = "#1b1d1f"
INK_SOFT = "#4a4f55"
ACCENT = "#0a6b4f"


def now_kst():
    return datetime.now(KST)


def format_schedule_ko(sched):
    days = sched.get("day_of_week", "*")
    day_label = "매일" if days.strip() == "*" else "·".join(CRON_DAY_KO.get(d.strip().upper(), d.strip()) for d in days.split(","))
    return f"{day_label} {sched['hour_kst']:02d}:{sched['minute_kst']:02d}"


def repo_meta_line(r, category):
    vel = r.get("daily_velocity", r.get("gained_stars", 0))
    weekly_gained = r.get("weekly_gained", 0)
    total = r["total_stars"]
    lang = r.get("language") or "Multi"
    if category == "surge":
        growth = f"이번 주 별 +{weekly_gained:,}" if weekly_gained >= 5000 else f"하루 별 +{vel:,}"
        parts = [lang, growth, f"누적 {total:,}"]
        if r.get("is_new_rookie"):
            parts.insert(1, "새로 나온 프로젝트")
    else:
        parts = [lang, f"누적 별 {total:,}", f"하루 +{vel:,}"]
    return " · ".join(parts)


def repo_text_html(r, category, name_size):
    # Drop the legacy "[name 맞춤 활용]" prefix; the line is labelled below.
    benefit = re.sub(r"^\s*\[[^\]]*\]\s*", "", r.get("benefit_ko", ""))
    benefit_html = (
        f'<p style="margin:10px 0 0 0; font-size:16px; line-height:1.6; color:{INK};"><b style="color:{ACCENT};">이렇게 써보세요</b> {escape(benefit)}</p>'
        if benefit else ""
    )
    return f"""
      <h3 style="margin:0; font-size:{name_size}px; line-height:1.3; font-weight:700; color:{INK};"><a href="{escape(r['url'])}" target="_blank" style="color:{INK}; text-decoration:none;">{escape(r['full_name'])}</a></h3>
      <p style="margin:6px 0 0 0; font-size:14px; line-height:1.5; color:{INK_SOFT};">{escape(repo_meta_line(r, category))}</p>
      <p style="margin:12px 0 0 0; font-size:16px; line-height:1.6; color:{INK};">{escape(r.get('summary_ko', ''))}</p>
      {benefit_html}
      <p style="margin:12px 0 0 0; font-size:16px;"><a href="{escape(r['url'])}" target="_blank" style="color:{ACCENT}; font-weight:700; text-decoration:underline;">GitHub에서 보기</a></p>
    """


def render_section_html(title, subtitle, repos, category):
    rows = ""
    for r in repos:
        if category == "surge":
            # Lead section: each pick gets its own surface and the repo's GitHub social preview image.
            owner_repo = escape(r["full_name"])
            rows += f"""
        <tr><td style="padding:0 0 20px 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{SURFACE};">
            <tr><td><a href="{escape(r['url'])}" target="_blank"><img src="https://opengraph.githubassets.com/1/{owner_repo}" width="600" alt="{owner_repo} 미리보기" style="display:block; width:100%; max-width:600px; height:auto; border:0;" /></a></td></tr>
            <tr><td style="padding:20px 22px 24px 22px;">{repo_text_html(r, category, 22)}</td></tr>
          </table>
        </td></tr>
            """
        else:
            # List sections: the owner's GitHub avatar as a small square thumbnail beside the text.
            owner = escape(r["full_name"].split("/")[0])
            rows += f"""
        <tr><td style="padding:0 22px 32px 22px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td width="48" valign="top" style="width:48px; padding:2px 14px 0 0;"><a href="{escape(r['url'])}" target="_blank"><img src="https://avatars.githubusercontent.com/{owner}?s=96" width="48" height="48" alt="{owner} 프로필 이미지" style="display:block; width:48px; height:48px; border:0;" /></a></td>
              <td valign="top">{repo_text_html(r, category, 19)}</td>
            </tr>
          </table>
        </td></tr>
            """

    # Work picks sit on one white sheet; fun picks sit on the accent-tinted field.
    sheet_bg = SURFACE if category == "work" else "#e3efe9"
    if category == "surge":
        body = rows
    else:
        body = f"""
        <tr><td style="background:{sheet_bg}; padding:28px 0 0 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>
        </td></tr>
        """

    return f"""
    <tr><td style="padding:44px 0 18px 0;">
      <h2 style="margin:0; font-size:26px; line-height:1.25; font-weight:800; color:{INK};">{escape(title)}</h2>
      <p style="margin:8px 0 0 0; font-size:15px; line-height:1.5; color:{INK_SOFT};">{escape(subtitle)}</p>
    </td></tr>
    {body}
    """


def generate_newsletter_html(surge_list, work_list, fun_list, config):
    now = now_kst()
    date_str = f"{now.month}월 {now.day}일 {KO_WEEKDAYS[now.weekday()]}요일"
    sched_desc = format_schedule_ko(config["schedule"])

    sec1 = render_section_html(
        "지금 뜨는 레포", "최근 하루·한 주 사이 별이 가장 빠르게 늘어난 프로젝트예요.", surge_list, "surge"
    )
    sec2 = render_section_html(
        "업무·개발 도구", "누적 인기와 최근 활동이 함께 높은 개발 도구예요.", work_list, "work"
    )
    sec3 = render_section_html(
        "개인·취미 프로젝트", "집 PC에 깔거나 주말에 만져보기 좋은 프로젝트예요.", fun_list, "fun"
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>GitHub 트렌드 매거진</title>
</head>
<body style="margin:0; padding:0; background:{GROUND}; font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{GROUND};">
    <tr><td align="center" style="padding:32px 16px 40px 16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;">
        <tr><td style="padding:8px 0 0 0;">
          <h1 style="margin:0; font-size:36px; line-height:1.15; font-weight:800; letter-spacing:-0.02em; color:{INK};">오늘의 GitHub 트렌드 {len(surge_list) + len(work_list) + len(fun_list)}선</h1>
          <p style="margin:14px 0 0 0; font-size:17px; line-height:1.6; color:{INK};">{escape(date_str)}, 요즘 뜨는 레포와 일에 쓸 도구, 취미로 만질 오픈소스 프로젝트를 골랐어요.</p>
        </td></tr>
        {sec1}
        {sec2}
        {sec3}
        <tr><td style="padding:40px 0 0 0;">
          <p style="margin:0; font-size:20px; font-weight:800; color:{INK};">GitHub 트렌드 매거진</p>
          <p style="margin:6px 0 0 0; font-size:14px; line-height:1.6; color:{INK_SOFT};">{escape(sched_desc)}에 GitHub Trending과 GitHub Search API에서 자동으로 골라 보내요.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_email_if_configured(html_body, config):
    recipient = os.environ.get("RECIPIENT_EMAIL", "kusari0215@naver.com").strip()
    subject_prefix = config["email"].get("subject_prefix", "📬 [GitHub 트렌드 매거진]")
    now = now_kst()
    subject = f"{subject_prefix} {now:%m/%d}({KO_WEEKDAYS[now.weekday()]}) 뜨는 레포 · 업무 도구 · 취미 프로젝트"

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
                "User-Agent": "GitHubTrendMailer/5.1"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[SUCCESS] Resend API를 통해 v5.1 이메일 발송 완료! -> {recipient} ({resp.status})")
            return True

    smtp_host = os.environ.get("SMTP_HOST", "smtp.naver.com").strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "587").strip())
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASSWORD", "").strip()

    if not smtp_user or not smtp_pass:
        print(f"[INFO] 수신처({recipient})가 지정되었으나 발송 인증키가 입력되지 않아 HTML 미리보기만 갱신했습니다.")
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
    parser = argparse.ArgumentParser(description="GitHub Trend Digest Generator & Mailer v5.1")
    parser.add_argument("--no-send", action="store_true", help="HTML 미리보기 및 JSON 캐시만 생성하고 메일은 발송하지 않음")
    parser.add_argument("--from-cache", action="store_true", help="저장된 latest_curated_repos.json을 읽어 HTML 렌더링 및 메일 발송 수행")
    args = parser.parse_args()

    load_dotenv()
    config = load_config()
    days = config["curation"].get("surge_created_within_days", 14)

    if args.from_cache and os.path.exists(CACHE_JSON_PATH):
        print(f"[1/3] 캐시된 큐레이션 데이터 로드 중: {CACHE_JSON_PATH}")
        with open(CACHE_JSON_PATH, "r", encoding="utf-8") as f:
            cached = json.load(f)
        surge_list = cached["surge_list"]
        work_list = cached["work_list"]
        fun_list = cached["fun_list"]
    else:
        print(f"[1/4] GitHub Trending (Daily + Weekly 통합) 및 최근 {days}일 내 신생 루키 레포 수집 중...")
        trending = fetch_multi_horizon_trending()
        surging = fetch_newly_surging_repos(days=days)

        print(f"[2/4] v5.1 Multi-Window 하이브리드 랭킹(REA 등 주간 메가히트 + 대장주 권위도 + Owner Cap=1) 분류 및 1:1 분석 중...")
        surge_list, work_list, fun_list = categorize_repos(trending, surging, config)

        with open(CACHE_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump({"surge_list": surge_list, "work_list": work_list, "fun_list": fun_list}, f, ensure_ascii=False, indent=2)

    print("[3/4] HTML 뉴스레터 생성 중...")
    html_body = generate_newsletter_html(surge_list, work_list, fun_list, config)

    with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
        f.write(html_body)
    print(f"[4/4] 샘플 뉴스레터 HTML 저장 완료: {PREVIEW_PATH}")

    if not args.no_send:
        send_email_if_configured(html_body, config)


if __name__ == "__main__":
    main()
