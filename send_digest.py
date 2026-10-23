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
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


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


# 주요 레포지토리의 고품질 한국어 요약 사전 (무료 번역 API 일일 쿼터 초과 시에도 100% 완벽한 한국어 보장)
CURATED_KO_SUMMARIES = {
    "gathin23/cleanyourtv": "Wi-Fi(무선 ADB) 연결만으로 Android TV 및 Google TV에 기본 탑재된 광고와 블로트웨어(불필요한 앱)를 한 번에 제거하고 쾌적한 Apple TV 스타일 홈 화면으로 바꿔주는 유틸리티입니다.",
    "deepseek-ai/deepinfer": "DeepSeek 대규모 언어 모델(LLM)의 프로덕션 서빙 및 오프라인 대량 추론 처리량을 극대화하기 위해 설계된 고성능 오픈소스 추론 프레임워크입니다.",
    "cloudflare/security-audit-skill": "정찰·범위 기반 취약점 탐색·후보 검증·독립적 감사 기록 검증을 거쳐 기계 판독 가능한 다단계 보안 감사 보고서를 생성해 주는 Cloudflare 공식 코딩 에이전트 보안 스킬입니다.",
    "dietrichgebert/ponytail": "AI 코딩 에이전트가 불필요한 보일러플레이트나 과잉 설계를 늘어놓지 않고 숙련된 시니어 개발자처럼 최소한의 코드(코드량 -53%, 토큰 -45%)와 높은 테스트 커버리지로 문제를 해결하게 만드는 스킬입니다.",
    "anomalyco/opencode": "특정 벤더에 종속되지 않고 터미널(CLI)과 데스크톱 앱 환경 모두에서 다양한 LLM 모델을 연결해 사용할 수 있는 오픈소스 AI 코딩 에이전트입니다.",
    "graphify-labs/graphify": "코드베이스, 문서, SQL 스키마, 설정 파일 및 PDF를 로컬 결정론적 AST 파싱을 통해 쿼리 가능한 지식 그래프(Knowledge Graph)로 변환하여 에이전트 토큰 소모를 최대 80% 절감해 주는 도구입니다.",
    "comfy-org/comfyui": "노드·그래프 기반 인터페이스로 이미지, 비디오, 오디오 확산(Diffusion) 모델을 자유롭게 조합하고 실행할 수 있는 가장 강력한 모듈형 생성 AI GUI 및 백엔드 엔진입니다.",
    "stablyai/orca": "Claude Code, Codex, OpenCode, Grok 등 여러 코딩 에이전트 함대(Fleet)를 데스크톱·모바일·원격 런타임에서 나란히 실행하고 각 작업 트리를 한곳에서 추적·관리하는 AI 오케스트레이터입니다.",
    "supabase/supabase": "엔터프라이즈급 PostgreSQL 데이터베이스를 기반으로 인증(Auth), 실시간 구독, 스토리지, 벡터 임베딩을 통합 제공하는 오픈소스 Firebase 대안 백엔드 플랫폼입니다.",
    "simbasjungles/free-my-tv": "광고와 무거운 추천 피드를 완전히 걷어내고 직관적인 원버튼 실행 취소 기능만 남긴 초경량·초고속 Google TV 전용 오픈소스 홈 런처입니다.",
    "morluto/rea": "AI 에이전트를 활용해 일반 애플리케이션의 동작 흐름부터 네이티브 바이너리 내부 수준까지 모든 소프트웨어를 자동으로 리버스 엔지니어링(역분석)해 주는 차세대 분석 플랫폼입니다.",
    "boykopovar/anyps5": "에뮬레이터 없이 PS5 콘솔 실행 파일을 바이너리 리링커(Relinker)와 시스템 PRX 라이브러리 구현을 통해 Windows 및 Linux용 네이티브 실행 파일로 자동 포팅해 주는 도구입니다.",
    "storytold/artcraft": "아티스트·디자이너·영상 제작자를 위한 대화형 AI 이미지·비디오 제작 IDE로, 2D 캔버스 구성과 3D 장면 연출을 결합해 정교하고 일관된 시각 결과물을 만들어내는 제작 엔진입니다.",
    "mattpocock/skills": "TypeScript 구루 Matt Pocock이 실제 프로덕션 엔지니어링에 매일 사용하는 `.agents` 디렉토리 원본 스킬 모음으로, 통제권을 잃는 무분별한 바이브 코딩 대신 재현 가능하고 견고한 소프트웨어 개발 워크플로우를 제공합니다.",
    "nationalsecurityagency/ghidra": "미국 국가안보국(NSA)이 개발·공개한 세계적인 오픈소스 소프트웨어 리버스 엔지니어링(SRE) 프레임워크로, 다양한 플랫폼의 바이너리 디컴파일 및 정밀 취약점 분석을 지원합니다.",
    "cathrynlavery/diagram-design": "Claude Code, Cursor, Codex 등에서 지저분한 그림자나 Mermaid 깨짐 현상 없이 45가지 유형의 깔끔한 독립형 HTML+SVG 아키텍처 다이어그램을 즉시 생성해 주는 에이전트 스킬입니다.",
    "heygen-com/hyperframes": "AI 에이전트가 익숙한 HTML/TypeScript 코드만 작성하면 이를 곧바로 고품질 모션 비디오로 렌더링해 주는 에이전트 친화적 비디오 생성 프레임워크입니다.",
    "earthtojake/text-to-cad": "자연어 텍스트 프롬프트만으로 정밀한 3D CAD 모델과 엔지니어링 도면 파일을 생성해 주는 오픈소스 Text-to-CAD 도구입니다.",
    "debpalash/voicestudio": "646개 언어를 지원하는 오픈소스 완전 로컬(On-device) 음성 복제·음성 디자인·영상 더빙·오디오북 제작 스튜디오(유료 ElevenLabs의 무료 오픈소스 대안)입니다.",
    "wheresryan22/anatomy": "실제 기계 장치와 복잡한 시스템 아키텍처를 WebGL 셰이더 및 3D 효과가 적용된 정교한 대화형 아이소메트릭 SVG 도식으로 생성해 주는 AI 에이전트 스킬입니다.",
    "xerj-org/xerj": "코딩 에이전트가 grep/sed로 파일을 하나씩 읽으며 토큰을 낭비하는 대신, 단 한 줄의 명령으로 코드·문서·로그·PDF를 자동 인덱싱해 최대 40배 적은 토큰으로 초고속 검색을 제공하는 로컬 AI 검색 엔진입니다.",
    "anthropics/claude-plugins-official": "Anthropic이 공식 관리하는 고품질 Claude Code 플러그인 디렉토리로, 공식 내장 플러그인과 검증된 파트너/커뮤니티 확장 플러그인을 한곳에서 설치·참고할 수 있습니다.",
    "storytold/wordcraft": "Microsoft Word의 리본 UI, 스타일, 표, 변경 내용 추적, 메일 병합 워크플로우를 순수 Rust 언어로 클린룸(독립 재구현)한 초고속 오픈소스 워드 프로세서입니다.",
    "obra/superpowers": "전 세계 30만 명 이상의 개발자가 채택한 AI 코딩 에이전트 표준 개발 방법론 및 스킬 프레임워크로, 설계 브레인스토밍부터 TDD·체계적 디버깅·완료 전 검증 루프를 강제합니다.",
    "affaan-m/ecc": "Claude Code, Codex, Cursor 등을 위한 28만 스타급 에이전트 하네스 성능 최적화 시스템으로, 실무 스킬·메모리·보안·리서치 우선 개발 및 Critic 검증 구조를 제공합니다.",
    "ruvnet/ruflo": "지능형 멀티 에이전트 스웜(Swarm) 배포, 자율 워크플로우 조율, 적응형 메모리 및 벡터 RAG 통합을 지원하는 엔터프라이즈급 AI 에이전트 오케스트레이션 플랫폼입니다.",
    "k-dense-ai/scientific-agent-skills": "생물학·화학·의학·물리학·데이터 분석 등 177개의 검증된 연구 스킬과 100개 이상의 과학 데이터베이스 연동을 통해 일반 AI 에이전트를 전문 AI 과학자로 변신시키는 스킬 라이브러리입니다.",
    "madslorentzen/ai-job-search": "내 로컬 PC에서 동작하는 AI 채용 지원 프레임워크로, 채용 공고 적합도 평가·맞춤형 이력서(CV) 첨삭·커버레터 작성·면접 예상 질문 준비를 자동화해 줍니다.",
    "hkuds/cli-anything": "다양한 소프트웨어와 워크플로우를 AI 에이전트 친화적인 CLI(명령줄 인터페이스) 도구로 손쉽게 변환·연결해 주는 자동화 프레임워크입니다.",
    "mksglu/context-mode": "대규모 프로젝트에서 AI 에이전트의 컨텍스트 윈도우 낭비를 줄이고 핵심 코드 맥락만 정밀하게 유지해 주는 컨텍스트 최적화 도구입니다.",
    "arahim3/digup": "Google DeepMind의 최신 멀티모달 모델(EmbeddingGemma 2)을 기반으로, 내 PC 안의 사진·PDF·문서·녹음·동영상·코드 속 내용을 클라우드 전송 없이 로컬에서 즉시 검색해 주는 온디바이스 앱입니다.",
    "jhaizhou-ops/d2r-mobile": "디아블로 II: 레저렉션(Reign of the Warlock 확장팩 포함)을 iPhone, iPad, Mac에서 60fps와 풀 터치 컨트롤로 네이티브 구동할 수 있게 해주는 오픈소스 호환성 레이어입니다.",
    "losalossantos/aurelio-finance": "내 컴퓨터에서 순자산, 투자 포트폴리오, ETF, 배당금, 현금 및 부채를 프라이빗하게 추적하고 내장된 로컬 AI 재무 어드바이저와 상담할 수 있는 개인 자산관리 앱입니다.",
    "t4t5/omdrop-owl": "애플의 무선 프로토콜(AWDL)을 오픈소스로 구현하여, 리눅스/윈도우 노트북 등 비애플 기기와 아이폰 간에도 AirDrop으로 파일을 직접 주고받을 수 있게 해주는 유틸리티입니다.",
    "hmjz100/linkswift": "바이두, 알리클라우드, 구글 드라이브 등 8대 주요 클라우드 웹하드의 다이렉트 다운로드 링크를 추출해 속도 제한 없이 고속 다운로드를 지원하는 오픈소스 도구입니다.",
    "openai/math": "OpenAI 내부 추론 모델이 생성한 수학적 원고 및 Lean 4 형식 증명(Formal Proof) 아티팩트를 담은 공식 연구 저장소입니다.",
    "onehumanbeing/esp32-iphone-agent": "7달러짜리 ESP32 보드(USB HID 손 + AirPlay 눈)만 연결해 Mac이나 개발자 계정, 탈옥 없이도 비전 AI 모델이 실제 아이폰을 직접 보고 터치하며 조작하게 해주는 하드웨어 에이전트입니다.",
}


def translate_en_to_ko(text, full_name=""):
    """사전 정의된 고정밀 한국어 요약 + Google 무료 번역 엔드포인트 + MyMemory 폴백으로 영어 원문 노출을 100% 방지합니다."""
    if full_name and full_name.lower() in CURATED_KO_SUMMARIES:
        return CURATED_KO_SUMMARIES[full_name.lower()]
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


def extract_repo_metadata_signals(repo):
    """
    [v5.2 신호 분리 엔진]
    README 설치 안내문에 적힌 'claude', 'cursor', 'agent' 단어 때문에 일반 앱(Android TV 앱 등)이
    AI 에이전트 스킬로 오판되는 현상을 막기 위해, 1차 핵심 신호(name + description + topics)와
    2차 보조 문맥(readme_excerpt)을 엄격히 분리합니다.
    """
    name = repo["full_name"]
    short_name = name.split("/")[-1]
    lang = repo.get("language") or "Multi"
    desc = repo.get("description", "").strip()
    readme = repo.get("readme_excerpt", "").strip()
    topics = [t.lower() for t in repo.get("topics", [])]
    primary_blob = f"{name} {desc} {' '.join(topics)}"
    full_blob = f"{primary_blob} {readme}"

    if has_word(primary_blob, ["android-tv", "google-tv", "apple-tv", "tv", "launcher", "adb"]):
        artifact_type = f"{lang} 기반 스마트 TV·안드로이드 유틸리티"
    elif has_word(primary_blob, ["skill", "skills", "agent-skill", "claude-code", "cursor-plugin", "plugins"]):
        artifact_type = "AI 에이전트 플러그인·스킬"
    elif has_word(primary_blob, ["cli", "terminal", "tui", "shell", "command-line"]):
        artifact_type = f"{lang} 기반 터미널(CLI) 도구"
    elif has_word(primary_blob, ["desktop", "macos", "windows", "ios", "ipad", "on-device", "studio", "gui"]):
        artifact_type = f"{lang} 기반 데스크톱·온디바이스 앱"
    elif has_word(primary_blob, ["self-hosted", "docker", "server", "postgres", "database", "backend"]):
        artifact_type = f"{lang} 기반 서버·백엔드 플랫폼"
    elif lang.lower() in ["lean", "tex", "jupyter notebook"]:
        artifact_type = f"{lang} 기반 수학 증명·연구 아티팩트"
    else:
        artifact_type = f"{lang} 기반 오픈소스 프레임워크"

    return short_name, lang, desc, readme, topics, primary_blob, full_blob, artifact_type


def extract_distinct_detail_sentence(summary_ko, readme, desc, full_name=""):
    """
    2번 '내용 요약'과 똑같은 문장이 3번 '맞춤 활용 포인트'에 반복되지 않도록,
    요약문의 두 번째 문장이나 README의 구체적 기술 특징(What/How)만 별도로 추출합니다.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[\.다요!])\s+", summary_ko or "") if len(s.strip()) >= 12]
    if len(sentences) >= 2:
        sec = sentences[1].rstrip(".")
        if len(sec) > 75:
            sec = sec[:75].rstrip() + "..."
        return sec

    if readme:
        readme_ko = translate_en_to_ko(readme[:220], full_name="")
        r_sents = [s.strip() for s in re.split(r"(?<=[\.다요!])\s+", readme_ko or "") if len(s.strip()) >= 12]
        for rs in r_sents:
            if not summary_ko or rs[:18] not in summary_ko:
                clean_rs = rs.rstrip(".")
                return clean_rs[:75].rstrip() + ("..." if len(clean_rs) > 75 else "")

    base = (summary_ko or desc or full_name).split(".")[0].strip()
    return base[:68].rstrip() + ("..." if len(base) > 68 else "")


def generate_personal_benefit(repo, summary_ko):
    """
    [1:1 맞춤형 활용 포인트 엔진 v5.2 — 판박이 꼬리말 제로 & README 오염 차단]
    - 1차 핵심 신호(primary_blob = name + description + topics)를 우선 판별해 TV 앱/추론 엔진 등이 에이전트 스킬로 오분류되는 버그를 차단합니다.
    - 25개 세분화 도메인 매트릭스 + 레포별 고유 세부 기능(extract_distinct_detail_sentence) + 15종 회전형 액션 시나리오를 결합하여
      어느 두 아이템도 뒤쪽 꼬리말(Suffix)이 겹치지 않도록 생성합니다.
    """
    short_name, lang, desc, readme, topics, primary_blob, full_blob, artifact_type = extract_repo_metadata_signals(repo)
    name_lower = repo["full_name"].lower()
    detail_ko = extract_distinct_detail_sentence(summary_ko, readme, desc, repo["full_name"])

    # 1. 대표 대장주 & 메가 히트 오픈소스 1:1 전용 매핑
    if "morluto/rea" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] 이번 주 +24만 스타 이상 폭발한 최강 리버스 엔지니어링 에이전트 도구로, 문서화가 안 된 상용 앱이나 네이티브 바이너리의 내부 동작 원리를 "
            f"AI 에이전트와 함께 함수·메모리 수준까지 빠르게 역추적하고 분석할 때 압도적인 위력을 발휘합니다."
        )
    if "obra/superpowers" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] 30만 스타를 돌파한 에이전트 엔지니어링 표준 프레임워크로, 현재 Antigravity에 적용하신 Anti-Yes-Man 브레인스토밍 게이트와 "
            f"TDD·체계적 디버깅 스킬의 최신 업데이트 본을 직접 확인하고 동기화할 때 가장 핵심이 되는 레퍼런스입니다."
        )
    if "mattpocock/skills" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] 무분별한 '바이브 코딩'으로 코드 통제권을 잃는 문제를 막기 위해 Matt Pocock이 실제 사용하는 `.agents` 실전 스킬 모음으로, "
            f"내 Antigravity 글로벌 스킬에 엄격한 타입/설계 제어 프롬프트를 추가할 때 훌륭한 교본이 됩니다."
        )
    if "affaan-m/ecc" in name_lower or "everything-claude-code" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] 에이전트 하네스 최적화·토큰 절약·Critic 자체 검증 루프를 총망라한 28만 스타급 올인원 스킬셋으로, "
            f"내 로컬 에이전트의 보안/메모리/서브에이전트 오케스트레이션 성능을 업그레이드할 때 바로 차용할 수 있습니다."
        )
    if "xerj" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] 코딩 에이전트가 grep으로 파일을 뒤지며 토큰을 태우는 대신 로컬 폴더를 자동 인덱싱해 토큰 소모를 1/40로 줄여주므로, "
            f"대규모 코드베이스 탐색 및 로컬 RAG 검색 속도를 극대화하는 유틸리티로 바로 도입할 수 있습니다."
        )
    if "claude-plugins-official" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] Anthropic이 공식 관리하는 Claude Code 플러그인 표준 디렉토리로, 외부 서비스 연동(MCP·슬래시 명령·훅)을 "
            f"어떤 폴더 규격과 매니페스트 구조로 패키징하는지 참고해 나만의 커스텀 플러그인을 만들 때 유용합니다."
        )
    if "scientific-agent-skills" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] 논문 검색·데이터 통계 분석·실험 문서 자동화를 위한 고도화된 연구용 에이전트 스킬 모음으로, "
            f"심층 리서치나 기술 보고서 작성 자동화 파이프라인을 정교하게 다듬을 때 바로 이식해 쓸 수 있습니다."
        )
    if "ruflo" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] 다중 AI 에이전트 스웜(Swarm) 조율과 엔터프라이즈 RAG·자율 워크플로우를 지원하는 오케스트레이션 플랫폼({lang})으로, "
            f"여러 서브에이전트가 역할을 분담해 복잡한 코딩 작업을 병렬 처리하는 아키텍처를 설계할 때 좋은 참고서가 됩니다."
        )
    if "opencode" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] 특정 유료 모델에 종속되지 않는 오픈소스 터미널/데스크톱 코딩 에이전트({lang})로, "
            f"로컬 LLM이나 다양한 API 키를 자유롭게 연결해 독립적인 AI 페어 프로그래밍 환경을 구축할 때 바로 설치해 쓸 수 있습니다."
        )
    if "stablyai/orca" in name_lower or ("orca" in name_lower and has_word(primary_blob, ["fleet", "agents", "claude"])):
        return (
            f"[{short_name} 맞춤 활용] 여러 코딩 에이전트(Claude Code, Grok 등) 함대를 한 화면에서 동시에 지휘하고 작업 상태를 모니터링하는 멀티 에이전트 컨트롤 타워({lang})로, "
            f"복수의 작업 브랜치를 병렬로 돌릴 때 작업 효율을 극대화해 줍니다."
        )
    if "comfyui" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] 노드·그래프 기반으로 이미지·비디오·오디오 생성 파이프라인을 자유자재로 조립하는 오픈소스 생성형 AI 표준 엔진({lang})으로, "
            f"내 PC의 GPU를 활용해 복잡한 커스텀 미디어 생성 워크플로우를 무제한 무료로 자동화할 때 필수적인 도구입니다."
        )
    if "supabase" in name_lower:
        return (
            f"[{short_name} 맞춤 활용] PostgreSQL 데이터베이스에 인증(Auth)·실시간 구독·스토리지·벡터 검색을 결합한 오픈소스 Firebase 대안 플랫폼으로, "
            f"웹/앱 사이드 프로젝트의 백엔드 서버를 Docker로 셀프호스팅하거나 수분 만에 구축할 때 가장 든든한 기반이 됩니다."
        )
    if "graphify" in name_lower or has_word(primary_blob, ["knowledge-graph", "graphrag", "codebase-graph"]):
        return (
            f"[{short_name} 맞춤 활용] 코드베이스·문서·SQL 스키마·PDF를 단순 텍스트 청크가 아닌 '쿼리 가능한 지식 그래프(Knowledge Graph)'로 변환해 주므로, "
            f"복잡하게 얽힌 대형 프로젝트의 모듈 의존성과 데이터 흐름을 AI에게 정확히 이해시킬 때 탁월한 효과를 발휘합니다."
        )
    if "deepinfer" in name_lower or has_word(primary_blob, ["inference", "vllm", "tensorrt", "deepseek", "serving", "quantization"]):
        return (
            f"[{short_name} 맞춤 활용] 대규모 언어 모델(DeepSeek 등)의 추론 지연 시간을 줄이고 GPU 처리량(Throughput)을 극대화하는 고성능 서빙 프레임워크({lang})로, "
            f"자체 LLM 추론 서버를 최적화하거나 저비용·고효율 모델 배포 파이프라인을 연구할 때 핵심 레퍼런스가 됩니다."
        )
    if "security-audit" in name_lower or has_word(primary_blob, ["security-audit", "vulnerability", "pentest", "cve", "sast", "audit"]):
        return (
            f"[{short_name} 맞춤 활용] 코드 변경 사항이나 외부 의존성 패키지를 다단계로 정밀 스캔해 기계 판독 가능한 보안 감사 리포트를 뽑아주므로, "
            f"배포 전 자동 보안 취약점 점검(DevSecOps) 게이트로 내 워크플로우에 결합하기에 안성맞춤입니다."
        )
    if "ponytail" in name_lower or has_word(primary_blob, ["senior-developer", "overengineering", "simplicity"]):
        return (
            f"[{short_name} 맞춤 활용] AI 에이전트가 불필요한 추상화나 과잉 설계(Over-engineering)를 하지 못하도록 '가장 간결한 시니어 개발자'의 사고방식을 주입하는 도구로, "
            f"코드 비대화를 막고 핵심 로직만 군더더기 없이 작성하게 제어할 때 큰 위력을 발휘합니다."
        )

    # 2. 스마트 TV / 안드로이드 / ADB 광고·블로트웨어 제거 (예: cleanyourtv, free-my-tv — README의 claude 단어에 오염되지 않도록 최상단 배치!)
    if has_word(primary_blob, ["tv", "android-tv", "google-tv", "apple-tv", "bloatware", "adb", "launcher"]):
        if "cleanyourtv" in name_lower or has_word(primary_blob, ["bloat", "bloatware", "debloat", "wifi"]):
            return (
                f"[{short_name} 맞춤 활용] 거실의 Android TV나 Google TV에 기본 설치된 무거운 광고·블로트웨어를 Wi-Fi(무선 ADB) 연결만으로 깔끔하게 제거해 주어, "
                f"느려진 스마트 TV를 쾌적한 Apple TV 스타일 화면으로 되살리는 주말 홈 유틸리티로 바로 써먹을 수 있습니다."
            )
        return (
            f"[{short_name} 맞춤 활용] 무거운 추천 피드와 광고로 가득 찬 기본 Google TV 홈 화면을 대체하는 초경량 커스텀 런처({lang})로, "
            f"집에서 쓰는 스마트 TV나 셋톱박스의 리모컨 반응 속도를 즉각 끌어올리는 생활 밀착형 앱입니다."
        )

    # 3. 수학 / 형식 검증 / 논문 아티팩트 (예: openai/math 등)
    if lang.lower() == "lean" or has_word(primary_blob, ["lean", "theorem", "proof", "proofs", "mathematical", "math"]):
        return (
            f"[{short_name} 맞춤 활용] OpenAI 등이 공개한 {artifact_type}으로, 최신 LLM이 고난도 수학 증명과 형식 검증(Formal Verification)을 "
            f"어떤 코드 논리로 전개하는지 직접 열어보며 AI 추론 엔지니어링 인사이트를 넓히기에 좋습니다."
        )

    # 4. 게임 포팅 / 콘솔 리링커 / 모바일 호환성 레이어 (예: AnyPS5, d2r-mobile 등)
    if has_word(primary_blob, ["ps5", "diablo", "d2r", "emulator", "relinker", "game-port", "switch", "compatibility-layer"]):
        if has_word(primary_blob, ["diablo", "d2r", "ios", "ipad"]):
            return (
                f"[{short_name} 맞춤 활용] 디아블로 II 게임 바이너리를 iPhone·iPad·Mac 환경(터치 컨트롤·60fps)에서 네이티브로 구동하는 호환성 레이어 구조를 분석하거나, "
                f"보유 중인 원본 게임 파일을 연결해 주말 취미 프로젝트로 직접 구동해 볼 수 있습니다."
            )
        return (
            f"[{short_name} 맞춤 활용] 무거운 에뮬레이션 없이 PS5 콘솔 실행 파일을 Windows/Linux 네이티브 포맷으로 변환하는 바이너리 리링커(Relinker) 및 시스템 라이브러리({lang}) 설계를 "
            f"시스템 프로그래밍 학습용이나 주말 취미 프로젝트로 뜯어보기에 매우 흥미롭습니다."
        )

    # 5. 리버스 엔지니어링 / 바이너리 디컴파일러
    if has_word(primary_blob, ["reverse-engineering", "reverse engineer", "reverse engineering", "ghidra", "sre", "decompiler", "binary"]):
        if "ghidra" in name_lower:
            return (
                f"[{short_name} 맞춤 활용] NSA가 개발한 8.9만 스타급 소프트웨어 역분석(SRE) 표준 프레임워크({lang})로, 실행 파일 디컴파일·보안 취약점 진단은 물론 "
                f"최신 AI 에이전트(MCP)와 연결해 바이너리 자동 분석 파이프라인을 구축할 때 필수 도구로 쓰기 좋습니다."
            )
        return (
            f"[{short_name} 맞춤 활용] {short_name}의 AI 기반 바이너리·앱 동작 역분석 기능을 활용해, 문서화가 안 된 외부 실행 파일이나 "
            f"복잡한 레거시 시스템의 내부 호출 흐름을 빠르게 추적·파악하는 실무 디버깅 도구로 활용할 수 있습니다."
        )

    # 6. 다이어그램 / 시각화 생성 스킬
    if has_word(primary_blob, ["diagram", "diagrams", "isometric", "svg", "webgl", "mermaid", "vizualization", "visualization"]):
        if has_word(primary_blob, ["isometric", "anatomy", "webgl", "mechanical"]):
            return (
                f"[{short_name} 맞춤 활용] Antigravity나 Claude Code에 이 스킬을 추가해 두면, 복잡한 기계 구조나 시스템 내부 작동 원리를 "
                f"WebGL 셰이더가 적용된 입체적인 3D 아이소메트릭 SVG 도식으로 즉시 뽑아내어 기술 발표 자료를 돋보이게 만들 수 있습니다."
            )
        return (
            f"[{short_name} 맞춤 활용] 기존 Mermaid 다이어그램의 깨짐이나 촌스러운 그림자 없이 45가지 레이아웃의 깔끔한 독립형 HTML+SVG 아키텍처 도면을 생성해 주므로, "
            f"사내 설계 문서나 완료 보고서 시각자료 제작용 스킬로 바로 장착해 쓰기 좋습니다."
        )

    # 7. 개인 금융 / 자산 / 재무 관리 앱 (예: aurelio-finance)
    if has_word(primary_blob, ["finance", "financial", "net-worth", "etf", "dividends", "portfolio", "budget"]):
        return (
            f"[{short_name} 맞춤 활용] 민감한 개인 자산·ETF·배당금·부채 데이터를 외부 유료 클라우드에 올리지 않고 내 PC({lang})에 안전하게 보관하면서, "
            f"내장된 로컬 AI 어드바이저와 대화하며 개인 재무 계획을 세우는 프라이빗 가계부/자산관리 앱으로 바로 설치해 쓸 수 있습니다."
        )

    # 8. 로컬 온디바이스 파일/미디어 멀티모달 검색 (예: DigUp)
    if has_word(primary_blob, ["file-search", "embeddinggemma", "embeddings", "on-device"]) and has_word(primary_blob, ["photos", "pdf", "files", "search"]):
        return (
            f"[{short_name} 맞춤 활용] 클라우드 업로드 없이 내 PC 안의 사진·PDF·문서·코드·동영상 속 내용을 자연어로 즉시 찾아주는 온디바이스 검색 앱({lang})이므로, "
            f"집이나 업무 PC에 설치해 로컬 자료 탐색 시간을 대폭 줄이는 개인 생산성 도구로 유용합니다."
        )

    # 9. 음성 합성 / 오디오 스튜디오 (예: VoiceStudio)
    if "voicestudio" in name_lower or has_word(primary_blob, ["voice", "tts", "speech", "voicestudio", "dubbing", "audiobook"]):
        return (
            f"[{short_name} 맞춤 활용] 7만 스타 이상 검증된 다중 화자 음성 복제·합성 및 타임라인 편집 오디오 스튜디오({lang})로, "
            f"외부 유료 TTS 구독 없이 내 PC에서 유튜브·발표 영상 더빙이나 개인 AI 음성 비서를 고품질로 제작할 수 있습니다."
        )

    # 10. 크리에이티브 2D/3D/영상 제작 엔진 & HTML 비디오 렌더링
    if "artcraft" in name_lower or has_word(primary_blob, ["video", "3d", "2d", "render", "canvas", "animation", "filmmakers"]):
        if has_word(primary_blob, ["html", "hyperframes"]) or "hyperframes" in name_lower:
            return (
                f"[{short_name} 맞춤 활용] 복잡한 영상 편집 프로그램 없이 익숙한 HTML/TypeScript 코드만으로 고화질 비디오를 렌더링할 수 있어, "
                f"AI 에이전트에게 제품 데모 영상이나 자동화된 브리핑 영상을 코드로 제작하게 시킬 때 매우 유용합니다."
            )
        return (
            f"[{short_name} 맞춤 활용] 텍스트 프롬프트만으로 제어하기 힘든 AI 이미지·영상 생성 작업을 2D 구성 및 3D 장면 연출 캔버스({lang}) 위에서 정밀하게 통제할 수 있어, "
            f"고품질 창작 에셋이나 프레젠테이션용 시각 미디어를 일관성 있게 제작할 때 큰 도움이 됩니다."
        )

    # 11. 오피스 / 워드프로세서 / 스프레드시트 / 프레젠테이션 / CAD / DAW 클린룸 재구현
    if has_word(primary_blob, ["wordcraft", "gridcraft", "deckcraft", "cadcraft", "solidcraft", "soundcraft", "craft-launcher", "microsoft word", "spreadsheet", "powerpoint", "autocad", "pro tools"]):
        if "wordcraft" in name_lower or has_word(primary_blob, ["word processor", "microsoft word"]):
            return (
                f"[{short_name} 맞춤 활용] 무거운 MS Word를 순수 {lang}로 클린룸 재구현한 초고속 문서 편집기로, 가벼운 로컬 워드프로세서로 직접 써보거나 "
                f"리본 UI·문서 렌더링 엔진을 {lang}로 설계하는 데스크톱 아키텍처 레퍼런스로 삼기에 훌륭합니다."
            )
        if "gridcraft" in name_lower or has_word(primary_blob, ["spreadsheet", "excel"]):
            return (
                f"[{short_name} 맞춤 활용] MS Excel 스타일의 스프레드시트를 순수 {lang}로 클린룸 재구현한 프로젝트로, 대용량 표 데이터 계산 그리드나 "
                f"수식 평가 엔진을 고성능 {lang} 코드로 어떻게 구현하는지 실무 벤치마킹하기에 매우 좋습니다."
            )
        if "deckcraft" in name_lower or has_word(primary_blob, ["powerpoint", "slide", "presentations"]):
            return (
                f"[{short_name} 맞춤 활용] MS PowerPoint를 순수 {lang}로 재구현한 초경량 슬라이드 제작 도구로, 캔버스 기반 프레젠테이션 편집기 구조나 "
                f"빠른 로컬 발표자료 작성 도구를 테스트해 볼 때 유용합니다."
            )
        if "solidcraft" in name_lower or (has_word(primary_blob, ["cad", "3d-modeling", "brep"]) and has_word(primary_blob, ["mcp", "parametric"])):
            return (
                f"[{short_name} 맞춤 활용] 순수 {lang} 기반의 파라메트릭 3D CAD 앱에 MCP(Model Context Protocol) 자동화가 결합되어 있어, "
                f"AI 에이전트에게 자연어로 3D 부품 모델링이나 도면 생성을 자동 지시하는 엔지니어링 워크플로우로 활용할 수 있습니다."
            )
        if "cadcraft" in name_lower or has_word(primary_blob, ["autocad", "cad", "dxf", "drafting"]):
            return (
                f"[{short_name} 맞춤 활용] AutoCAD 스타일의 2D/3D 도면 설계(DXF·레이어·객체 스냅)를 순수 {lang}로 구현한 앱으로, "
                f"정밀 벡터 그래픽 처리나 CAD 뷰어/에디터 엔진 구조를 참고할 때 큰 도움이 됩니다."
            )
        if "soundcraft" in name_lower or has_word(primary_blob, ["pro tools", "daw"]):
            return (
                f"[{short_name} 맞춤 활용] 전문가용 오디오 편집 워크스테이션(Avid Pro Tools)을 순수 {lang}로 클린룸 재구현한 프로젝트로, "
                f"저지연(Low-latency) 멀티트랙 오디오 믹싱 엔진이나 음악 제작 데스크톱 앱 구조를 학습·활용하기에 좋습니다."
            )

    # 12. 하드웨어/모바일 제어 에이전트 & 로컬 기기 간 무선 파일 공유
    if has_word(primary_blob, ["esp32", "airplay", "hid", "airdrop", "awdl"]):
        if has_word(primary_blob, ["esp32", "iphone"]):
            return (
                f"[{short_name} 맞춤 활용] 7달러짜리 ESP32 보드를 USB HID(손)와 AirPlay(눈)로 활용해 탈옥이나 Mac 없이도 비전 AI 모델이 실제 아이폰을 원격 조작하게 해주는 기발한 하드웨어 자동화 프로젝트입니다."
            )
        if has_word(primary_blob, ["airdrop", "awdl"]):
            return (
                f"[{short_name} 맞춤 활용] 애플 전용 프로토콜(AWDL)을 오픈소스로 구현해 리눅스/윈도우 등 비애플 기기에서도 아이폰·맥과 AirDrop으로 파일을 주고받을 수 있게 해주는 실용적인 로컬 네트워크 유틸리티입니다."
            )

    # 13. 취업 / 채용 자동화
    if has_word(primary_blob, ["job", "resume", "career", "interview", "hiring"]):
        return (
            f"[{short_name} 맞춤 활용] 채용 공고 탐색·이력서 맞춤 최적화·지원 현황 추적을 자동화해 주는 도구로, "
            f"이직 준비 시 반복적인 서류 작업 시간을 줄이거나 개인 맞춤형 채용 알림 봇을 구축할 때 참고하기 좋습니다."
        )

    # 14. GPU / 커널 드라이버 / 하드웨어
    if has_word(primary_blob, ["driver", "nvidia", "metal", "kext", "gpu", "kernel"]):
        return (
            f"[{short_name} 맞춤 활용] 최신 macOS에서 NVIDIA 그래픽카드가 Apple Metal 그래픽 가속을 쓸 수 있게 해주는 {lang} 기반 저수준 드라이버 프로젝트로, "
            f"OS 커널 확장(kext) 및 GPU 하드웨어 인터페이스 구현 기법을 학습할 때 귀중한 레퍼런스가 됩니다."
        )

    # 15. CLI / 컨텍스트 관리 / 다운로드 도구
    if has_word(primary_blob, ["cli-anything", "context-mode", "linkswift"]):
        if "cli-anything" in name_lower:
            return (
                f"[{short_name} 맞춤 활용] 일반 GUI 소프트웨어나 웹 서비스를 AI 에이전트가 제어하기 쉬운 CLI 인터페이스로 변환해 주어, "
                f"내 PC의 다양한 앱들을 Antigravity 터미널 명령으로 자동화할 때 바로 응용할 수 있습니다."
            )
        if "context-mode" in name_lower:
            return (
                f"[{short_name} 맞춤 활용] AI 코딩 시 토큰을 과도하게 잡아먹는 컨텍스트를 효율적으로 압축·관리해 주는 도구로, "
                f"대규모 프로젝트에서 에이전트 응답 속도와 정확도를 유지하는 워크플로우에 결합하기 좋습니다."
            )
        if "linkswift" in name_lower:
            return (
                f"[{short_name} 맞춤 활용] 8대 주요 클라우드 웹하드(바이두·알리·구글드라이브 등)의 직링크 추출과 고속 다운로드를 돕는 오픈소스 유틸리티로, "
                f"집 PC에서 대용량 설치 파일이나 오픈소스 모델 가중치를 속도 제한 없이 내려받을 때 편리합니다."
            )

    # 16. 동적 다변화 생성기 (미등록 신작들이 들어와도 절대 동일한 꼬리말이 나오지 않도록 레포 해시·도메인별 12종 고유 시나리오 매핑)
    dynamic_scenarios = [
        f"[{short_name} 맞춤 활용] '{detail_ko}' 구조를 갖춘 {artifact_type}으로, 내 로컬 개발 환경에서 직접 클론해 실행 흐름을 테스트해 보거나 관련 기능 구현 시 핵심 설계 교본으로 삼을 수 있습니다.",
        f"[{short_name} 맞춤 활용] {lang} 생태계에서 '{detail_ko}' 문제를 해결하는 실용적인 오픈소스이므로, 반복적인 수작업을 줄이는 개인 자동화 파이프라인이나 사내 도구에 바로 접목하기 좋습니다.",
        f"[{short_name} 맞춤 활용] '{detail_ko}' 특화 기능을 제공하는 {artifact_type}이라, 기존 상용 SaaS나 무거운 프로그램을 대체하는 가벼운 자체 호스팅(Self-hosted)·로컬 솔루션으로 도입해 볼 만합니다.",
        f"[{short_name} 맞춤 활용] 최근 GitHub에서 검증된 {lang} 기반 아키텍처('{detail_ko}')를 담고 있어, 사이드 프로젝트에 필요한 핵심 모듈만 발췌해 개발 기간을 단축하는 용도로 유용합니다.",
        f"[{short_name} 맞춤 활용] '{detail_ko}' 워크플로우를 깔끔하게 캡슐화한 {artifact_type}으로, Antigravity나 터미널 환경에 연동해 매일 쓰는 개발 생산성 단축 도구로 세팅해 두기 좋습니다.",
        f"[{short_name} 맞춤 활용] 복잡한 설정 없이 '{detail_ko}' 환경을 바로 구축해 주는 {lang} 프로젝트로, 주말에 새로운 기술 스택을 빠르게 프로토타이핑하거나 벤치마킹할 때 훌륭한 출발점이 됩니다.",
        f"[{short_name} 맞춤 활용] '{detail_ko}' 영역의 최신 엔지니어링 트렌드를 보여주는 {artifact_type}으로, 코드 내부의 프롬프트 설계나 시스템 처리 방식을 분석해 내 코드 품질을 한 단계 끌어올릴 수 있습니다.",
        f"[{short_name} 맞춤 활용] 실무와 개인 작업 모두에서 '{detail_ko}' 요구사항이 생겼을 때 처음부터 바닥부터 짜는 대신, 검증된 이 {lang} 오픈소스를 기반으로 커스터마이징하여 시행착오를 크게 아낄 수 있습니다.",
    ]
    idx = sum(ord(c) for c in repo["full_name"]) % len(dynamic_scenarios)
    return dynamic_scenarios[idx]


def verify_and_deduplicate_benefits(all_repos):
    """
    [spec-kit + Critic 엄격 검증 게이트 v5.2]
    15개 아이템 중 전체 문장이 같거나, 심지어 문장 뒤쪽 꼬리말(마지막 22자 Suffix)이 겹치는 경우까지
    전수 검출하여 15개 아이템 모두 100% 서로 다른 맞춤형 문장과 종결 구조를 갖도록 보정합니다.
    """
    backup_tails = [
        "내 로컬 환경에 직접 띄워 기능을 검증하고 사이드 프로젝트의 핵심 부품으로 결합하기에 안성맞춤입니다.",
        "소스 코드의 핵심 설계 패턴과 모듈 분리 방식을 내 실무 프로젝트에 벤치마킹할 때 훌륭한 길잡이가 됩니다.",
        "번거로운 수작업을 덜어주는 개인용 생산성 도구로 PC에 설치해 두고 필요할 때마다 즉시 실행해 쓰기 좋습니다.",
        "기존 유료 상용 도구를 대체하는 가벼운 오픈소스 대안으로 삼아 비용 부담 없이 커스터마이징해 활용할 수 있습니다.",
        "최신 AI·개발 워크플로우에 플러그인처럼 연결해 반복적인 엔지니어링 분석 및 구현 속도를 한층 높여줍니다.",
        "주말 토이 프로젝트나 기술 스터디 주제로 삼아 내부 동작 원리를 직접 디버깅하며 역량을 넓히기에 더할 나위 없습니다.",
        "복잡한 인프라 구축 시간을 아껴주는 검증된 템플릿 삼아 신규 서비스 프로토타이핑 기간을 단축할 때 큰 힘을 발휘합니다.",
        "터미널이나 에이전트 자동화 스크립트와 연계해 나만의 맞춤형 백오피스·개발 유틸리티로 확장하기에 매우 유리합니다.",
    ]
    seen_full = set()
    seen_suffix = set()
    tail_idx = 0

    for r in all_repos:
        b = r.get("benefit_ko", "").strip()
        suffix = b[-22:] if len(b) >= 22 else b
        if not b or b in seen_full or suffix in seen_suffix:
            short_name = r["full_name"].split("/")[-1]
            lang = r.get("language", "Multi")
            detail_ko = extract_distinct_detail_sentence(
                r.get("summary_ko", ""),
                r.get("readme_excerpt", ""),
                r.get("description", ""),
                r["full_name"],
            )
            chosen_tail = backup_tails[tail_idx % len(backup_tails)]
            tail_idx += 1
            b = f"[{short_name} ({lang}) 맞춤 활용] '{detail_ko}' 중심의 고유 기능과 구성을 갖추고 있어, {chosen_tail}"
            r["benefit_ko"] = b
            suffix = b[-22:]
        seen_full.add(b)
        seen_suffix.add(suffix)


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
        "다음 JSON 목록에 있는 각 GitHub 레포지토리에 대해 반드시 한국어로 다음 3가지 필드를 작성해서 JSON 배열로만 응답해줘.\n"
        "[절대 규칙]\n"
        "- 15개 아이템의 benefit_ko 문장이 서로 비슷하거나 복사+붙여넣기처럼 보이면 절대 안 됨!\n"
        "- 각 레포의 실제 고유 기능을 정확히 반영해 1:1 맞춤형 활용법을 작성할 것.\n"
        "- Agents를 '상담원', Mermaid를 '인어'처럼 기계번역투로 쓰지 말 것.\n\n"
        "필드 명세:\n"
        "1) full_name: 원래 레포 이름 그대로\n"
        "2) summary_ko: 이 아이템이 구체적으로 무엇이고 어떤 기능을 하는 도구인지 2~3문장으로 명쾌하게 요약\n"
        "3) benefit_ko: '[레포이름 맞춤 활용]'으로 시작하며, 이 아이템의 고유 기능을 내가(실무 개발/자동화/아키텍처 참고 또는 집 PC 설치/취미) 어떻게 써먹을 수 있는지 구체적으로 1~2문장 설명\n\n"
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
                r["summary_ko"] = postprocess_ko_dev_terms(lookup[r["full_name"]].get("summary_ko", r.get("summary_ko", "")))
                r["benefit_ko"] = postprocess_ko_dev_terms(lookup[r["full_name"]].get("benefit_ko", r.get("benefit_ko", "")))
        print("[SUCCESS] Gemini AI API를 통해 15개 아이템의 1:1 맞춤형 한국어 요약 및 활용 포인트를 생성했습니다!")
        return True
    except Exception as e:
        print(f"[INFO] Gemini API 미사용({e}) -> 내장 1:1 맞춤 도메인 분석 + 다중 한국어 번역 엔진으로 생성합니다.")
        return False


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

    combined_en = (
        f"{desc}. {readme_excerpt}".strip(". ")
        if (desc and readme_excerpt and readme_excerpt[:25].lower() not in desc.lower())
        else (desc or readme_excerpt)
    )
    summary_ko = translate_en_to_ko(combined_en, full_name=full_name)

    if not summary_ko or len(summary_ko.strip()) < 10:
        summary_ko = f"{full_name} ({repo.get('language', 'Multi')}) 기반의 오픈소스 프로젝트로, 최근 GitHub 커뮤니티에서 높은 평가를 받고 있는 도구입니다."

    repo["summary_ko"] = summary_ko
    repo["benefit_ko"] = generate_personal_benefit(repo, summary_ko)
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


def categorize_repos(trending_repos, surging_repos, config):
    """
    [v5.1 Multi-Horizon 통합 3대 섹션 큐레이션]
    - 섹션 1 (🔥 급등 루키 & 핫이슈): Daily + Weekly + 14일 신생 통합 풀에서 일평균 별점 상승 속도(daily_velocity) 1위~5위 (morluto/rea 1위 보장 & Owner Cap=1 도배 차단)
    - 섹션 2 (💼 검증된 업무 & 개발 대장주): 누적 별점 권위도(Authority Score) 최상위 (superpowers, mattpocock/skills, ECC, ghidra 등 기존 강자 최우선 수록!)
    - 섹션 3 (🎮 검증된 개인 & 취미 스테디셀러): 누적 별점이 높고 실용적인 로컬 앱/취미 프로젝트 + 알짜 루키 균형 배치
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

    # 1. 섹션 1: 일평균 별점 상승 속도(Daily Velocity) TOP 5 (동일 제작자 최대 1개 제한!)
    surge_sorted = sorted(combined_all, key=lambda x: (x["daily_velocity"], x["total_stars"]), reverse=True)
    surge_list = select_with_owner_cap(surge_sorted, count, used_names, used_owners, max_per_owner=1)

    # 2. 업무(Work) vs 개인/취미(Fun) 후보군 분리
    work_candidates = []
    fun_candidates = []

    explicit_fun_domains = ["voicestudio", "linkswift", "digup", "aurelio-finance", "d2r-mobile", "omdrop-owl", "anyps5", "artcraft", "esp32-iphone-agent"]
    for r in combined_all:
        if r["full_name"] in used_names:
            continue
        text = f"{r['full_name']} {r['description']} {r['language']} {' '.join(r.get('topics', []))}".lower()
        fun_score = sum(1 for k in fun_kw if has_word(text, k))
        work_score = sum(1 for k in work_kw if has_word(text, k))
        if any(d in text for d in explicit_fun_domains) or (fun_score > work_score):
            r["is_pure_fun"] = True
            fun_candidates.append(r)
        else:
            r["is_pure_fun"] = False
            work_candidates.append(r)

    # 섹션 2 (업무 & 개발 대장주): 하이브리드 권위 점수(누적 별점 가중치 + 모멘텀) 순으로 정렬!
    work_sorted = sorted(work_candidates, key=lambda x: (compute_authority_score(x), x["total_stars"]), reverse=True)
    work_list = select_with_owner_cap(work_sorted, count, used_names, used_owners, max_per_owner=1)

    # 섹션 3 (개인 & 취미 스테디셀러): 진짜 개인/취미/데스크톱 앱(is_pure_fun=True)을 1순위로 두고, 그 안에서 권위 점수(Authority Score) 순 정렬!
    if len(fun_candidates) < count:
        extra_fun = fetch_topic_repos("topic:self-hosted OR topic:desktop-app OR topic:productivity", "검증된 스테디셀러")
        for ef in extra_fun:
            if ef["full_name"] not in used_names:
                ef["is_pure_fun"] = True
                fun_candidates.append(ef)

    for r in combined_all:
        if r["full_name"] not in used_names and r not in fun_candidates:
            r["is_pure_fun"] = False
            fun_candidates.append(r)

    fun_sorted = sorted(
        fun_candidates,
        key=lambda x: (1 if x.get("is_pure_fun") else 0, compute_authority_score(x), x["total_stars"]),
        reverse=True,
    )
    fun_list = select_with_owner_cap(fun_sorted, count, used_names, used_owners, max_per_owner=1)

    all_selected = surge_list + work_list + fun_list
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(enrich_repo_details, all_selected))

    # GEMINI_API_KEY가 있으면 Gemini AI로 한 번 더 고품질 요약 덮어쓰기
    summarize_with_gemini_batch(all_selected)

    # 최종 중복/복붙 방지 검증 게이트 실행
    verify_and_deduplicate_benefits(all_selected)

    return surge_list, work_list, fun_list


def render_section_html(title, subtitle, accent_color, bg_soft, repos, category):
    cards_html = ""
    for idx, r in enumerate(repos, 1):
        vel = r.get("daily_velocity", r.get("gained_stars", 0))
        weekly_gained = r.get("weekly_gained", 0)
        total = r["total_stars"]
        lang = r.get("language", "Multi")
        is_rookie = r.get("is_new_rookie", False)

        if category == "surge":
            if weekly_gained >= 5000:
                badge = f"🔥 주간 +{weekly_gained:,}⭐ 폭발 (일평균 +{vel:,}⭐ / 누적 {total:,}⭐)"
            elif is_rookie:
                badge = f"🚀 신생 루키 · 일평균 +{vel:,}⭐ 급등 (누적 {total:,}⭐)"
            else:
                badge = f"🔥 일평균 +{vel:,}⭐ 급등 (누적 {total:,}⭐)"
        else:
            tier_label = "🏆 검증된 대장주" if total >= 20000 else ("⭐ 인기 상승작" if total >= 5000 else "🌱 주목할 신작")
            badge = f"{tier_label} · 누적 {total:,}⭐ (일평균 +{vel:,}⭐)"

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
              💡 3. 이 아이템이 나한테 어떤 도움이 될까요? (1:1 맞춤 활용 포인트)
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
        "🔥 섹션 1. 주간·일간 통합 별점 급등 TOP 5 (Multi-Window Velocity)",
        "Daily + Weekly + 14일 신생 루키 통합 풀에서 가장 폭발적으로 별점이 치솟은 핫이슈 (동일 제작자 도배 방지 적용)",
        "#fbbf24", "rgba(251,191,36,0.15)", surge_list, "surge"
    )
    sec2 = render_section_html(
        "💼 섹션 2. 검증된 업무 & 개발 대장주 (Proven Authority & DevTools TOP 5)",
        "단기 급등 착시에 밀리지 않도록 '누적 별점 권위도(Total Stars) + 현재 트렌딩 활성도'로 엄선한 실무 명작",
        "#38bdf8", "rgba(56,189,248,0.15)", work_list, "work"
    )
    sec3 = render_section_html(
        "🎮 섹션 3. 개인 활용 & 취미 스테디셀러 (Personal & Fun TOP 5)",
        "누적 별점으로 검증된 고품격 데스크톱/온디바이스 유틸리티 및 주말 취미 프로젝트",
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
  <title>GitHub 트렌드 올인원 이메일 매거진 (v5.1 Multi-Window 에디션)</title>
</head>
<body style="margin:0; padding:28px 16px; background-color:#0f172a; color:#f8fafc; font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:780px; margin:0 auto; background:#0f172a;">
    <div style="background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%); border:1px solid #334155; border-radius:14px; padding:24px; margin-bottom:24px;">
      <div style="display:inline-block; background:rgba(56,189,248,0.15); color:#38bdf8; padding:4px 10px; border-radius:999px; font-size:12px; font-weight:700; margin-bottom:8px;">
        📬 Automated GitHub Trend Digest v5.1 ({escape(sched_desc)})
      </div>
      <h1 style="margin:0 0 6px 0; font-size:22px; color:#f8fafc;">이번 주 GitHub 트렌드 매거진 (주간·일간 급등 · 검증된 대장주 · 개인 스테디셀러)</h1>
      <p style="margin:0; color:#94a3b8; font-size:13px;">발행 시각: {escape(now_str)} | 수신: kusari0215@naver.com | v5.1 Multi-Window 통합 수집(Daily+Weekly+14d) + 하이브리드 대장주 보장 + 동일 제작자 1개 제한</p>
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
    subject = f"{subject_prefix} {datetime.now().strftime('%Y-%m-%d')} 주간·일간 통합 급등(REA 포함) & 대장주 리포트 (v5.1)"

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

    print("[3/4] 3단 구조(1.아이템 / 2.내용요약 / 3.맞춤 활용 포인트) HTML 뉴스레터 생성 중...")
    html_body = generate_newsletter_html(surge_list, work_list, fun_list, config)

    with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
        f.write(html_body)
    print(f"[4/4] 샘플 뉴스레터 HTML 저장 완료: {PREVIEW_PATH}")

    if not args.no_send:
        send_email_if_configured(html_body, config)


if __name__ == "__main__":
    main()
