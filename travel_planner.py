import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai


RESULTS_DIR = Path("results")


def parse_args():
    parser = argparse.ArgumentParser(
        description="LLM API와 Kakao Local API를 활용한 국내 여행 추천 CLI 프로그램"
    )

    parser.add_argument(
        "-date",
        "--date",
        required=True,
        help='여행 날짜를 YYYY-MM-DD 형식으로 입력하세요. 예: "2026-03-15"'
    )

    return parser.parse_args()


def validate_date(date_text):
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def check_api_keys():
    gemini_key = os.getenv("GEMINI_API_KEY")
    kakao_key = os.getenv("KAKAO_REST_API_KEY")

    missing_keys = []

    if not gemini_key:
        missing_keys.append("GEMINI_API_KEY")

    if not kakao_key:
        missing_keys.append("KAKAO_REST_API_KEY")

    if missing_keys:
        print("[오류] API 키가 설정되지 않았습니다.")
        print("누락된 키:", ", ".join(missing_keys))
        print()
        print("프로젝트 폴더에 .env 파일을 만들고 아래처럼 입력하세요.")
        print("GEMINI_API_KEY=your_gemini_api_key_here")
        print("KAKAO_REST_API_KEY=your_kakao_rest_api_key_here")
        sys.exit(1)

    return gemini_key, kakao_key


def extract_json_from_text(text):
    if not text:
        raise ValueError("LLM 응답이 비어 있습니다.")

    cleaned = text.strip()

    cleaned = re.sub(r"^```json", "", cleaned)
    cleaned = re.sub(r"^```", "", cleaned)
    cleaned = re.sub(r"```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        return json.loads(match.group(0))

    raise ValueError("LLM 응답에서 JSON 객체를 찾을 수 없습니다.")


def validate_recommendation_schema(data):
    required_keys = {
        "recommended_city": str,
        "weather": str,
        "events": list,
        "reason": str
    }

    for key, expected_type in required_keys.items():
        if key not in data:
            raise ValueError(f"추천 JSON에 필수 키가 없습니다: {key}")
        if not isinstance(data[key], expected_type):
            raise ValueError(f"{key}의 타입이 올바르지 않습니다.")

    return True


def get_travel_recommendation(date_text, gemini_key, errors):
    client = genai.Client(api_key=gemini_key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    prompt = f"""
너는 국내 여행 추천 전문가야.

사용자가 입력한 여행 날짜를 기준으로 국내 여행지 1곳을 추천해줘.
실제 날씨와 행사 정보의 정확도보다, JSON 구조를 정확히 지키는 것이 더 중요해.

[여행 날짜]
{date_text}

아래 JSON 형식으로만 답변해.
마크다운, 설명 문장, 코드블록 없이 JSON만 출력해.

{{
  "recommended_city": "도시명",
  "weather": "해당 시기 일반적인 날씨 요약",
  "events": ["행사 또는 축제 후보 1", "행사 또는 축제 후보 2"],
  "reason": "추천 근거를 2~4문장으로 작성"
}}
"""

    retry_prompt = f"""
이전 응답을 JSON으로 파싱할 수 없었습니다.
아래 형식을 반드시 지켜 JSON만 다시 출력하세요.

{{
  "recommended_city": "도시명",
  "weather": "날씨 요약",
  "events": ["행사 또는 축제 후보 1", "행사 또는 축제 후보 2"],
  "reason": "추천 근거 2~4문장"
}}

여행 날짜: {date_text}
"""

    for attempt in range(2):
        try:
            current_prompt = prompt if attempt == 0 else retry_prompt

            response = client.models.generate_content(
                model=model_name,
                contents=current_prompt
            )

            data = extract_json_from_text(response.text)
            validate_recommendation_schema(data)
            return data

        except Exception as error:
            errors.append({
                "step": "llm_recommendation",
                "type": "PARSE_OR_API_ERROR",
                "message": str(error),
                "attempt": attempt + 1
            })

            if attempt == 1:
                print("[오류] LLM 추천 JSON 생성에 실패했습니다.")
                print("원인:", error)
                sys.exit(1)


def search_restaurants(city, kakao_key, errors, size=5):
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"

    headers = {
        "Authorization": f"KakaoAK {kakao_key}"
    }

    params = {
        "query": f"{city} 맛집",
        "size": size
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=10
        )

        if response.status_code in [401, 403]:
            errors.append({
                "step": "place_search",
                "type": "AUTH_ERROR",
                "message": f"HTTP {response.status_code}"
            })
            print(f"  - 오류: 인증 실패({response.status_code}). 키 설정을 확인하세요.")
            print("  - 맛집 섹션은 '데이터 없음'으로 처리하고 계속 진행합니다.")
            return []

        if response.status_code == 429:
            errors.append({
                "step": "place_search",
                "type": "QUOTA_ERROR",
                "message": "HTTP 429"
            })
            print("  - 오류: API 호출 한도 초과(429).")
            print("  - 맛집 섹션은 '데이터 없음'으로 처리하고 계속 진행합니다.")
            return []

        response.raise_for_status()

        data = response.json()
        documents = data.get("documents", [])

        if not documents:
            errors.append({
                "step": "place_search",
                "type": "EMPTY_RESULT",
                "message": f"0 results for query={city} 맛집"
            })
            print("  - 검색 결과 0건")
            return []

        restaurants = []

        for item in documents[:size]:
            restaurants.append({
                "name": item.get("place_name", ""),
                "address": item.get("road_address_name") or item.get("address_name", ""),
                "category": item.get("category_name", ""),
                "url": item.get("place_url", ""),
                "x": item.get("x", ""),
                "y": item.get("y", "")
            })

        return restaurants

    except requests.exceptions.RequestException as error:
        errors.append({
            "step": "place_search",
            "type": "NETWORK_OR_HTTP_ERROR",
            "message": str(error)
        })
        print("  - 지도/장소 API 호출 오류가 발생했습니다.")
        print("  - 맛집 섹션은 '데이터 없음'으로 처리하고 계속 진행합니다.")
        return []

    except Exception as error:
        errors.append({
            "step": "place_search",
            "type": "UNKNOWN_ERROR",
            "message": str(error)
        })
        print("  - 알 수 없는 오류가 발생했습니다.")
        print("  - 맛집 섹션은 '데이터 없음'으로 처리하고 계속 진행합니다.")
        return []


def generate_markdown_report(date_text, recommendation, restaurants, errors, gemini_key):
    client = genai.Client(api_key=gemini_key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    restaurants_text = json.dumps(restaurants, ensure_ascii=False, indent=2)
    recommendation_text = json.dumps(recommendation, ensure_ascii=False, indent=2)
    errors_text = json.dumps(errors, ensure_ascii=False, indent=2)

    prompt = f"""
너는 국내 여행 리포트를 작성하는 여행 플래너야.

아래 데이터를 바탕으로 Markdown 형식의 최종 여행 리포트를 작성해줘.

[여행 날짜]
{date_text}

[1차 추천 JSON]
{recommendation_text}

[맛집 검색 결과]
{restaurants_text}

[오류 목록]
{errors_text}

리포트에는 반드시 아래 항목을 포함해.

# {date_text} 국내 여행 추천 리포트
## 추천 지역
## 추천 이유
## 날씨 요약
## 행사/축제
## 맛집 추천
## 1일 일정 제안
## 오류 요약(errors)

맛집 검색 결과가 0건이면 맛집 추천 섹션에 "데이터 없음"이라고 적어.
오류 목록이 비어 있으면 오류 요약에 "오류 없음"이라고 적어.
"""

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt
        )
        return response.text.strip()

    except Exception as error:
        errors.append({
            "step": "final_report",
            "type": "LLM_REPORT_ERROR",
            "message": str(error)
        })

        return generate_fallback_report(date_text, recommendation, restaurants, errors)


def generate_fallback_report(date_text, recommendation, restaurants, errors):
    city = recommendation.get("recommended_city", "추천 지역 없음")
    weather = recommendation.get("weather", "날씨 정보 없음")
    events = recommendation.get("events", [])
    reason = recommendation.get("reason", "추천 이유 없음")

    lines = []

    lines.append(f"# {date_text} 국내 여행 추천 리포트")
    lines.append("")
    lines.append("## 추천 지역")
    lines.append("")
    lines.append(f"- {city}")
    lines.append("")
    lines.append("## 추천 이유")
    lines.append("")
    lines.append(reason)
    lines.append("")
    lines.append("## 날씨 요약")
    lines.append("")
    lines.append(weather)
    lines.append("")
    lines.append("## 행사/축제")
    lines.append("")

    if events:
        for event in events:
            lines.append(f"- {event}")
    else:
        lines.append("- 데이터 없음")

    lines.append("")
    lines.append("## 맛집 추천")
    lines.append("")

    if restaurants:
        for idx, place in enumerate(restaurants, start=1):
            lines.append(f"{idx}. {place.get('name', '이름 없음')}")
            lines.append(f"   - 주소: {place.get('address', '주소 없음')}")
            lines.append(f"   - 카테고리: {place.get('category', '카테고리 없음')}")
            lines.append(f"   - URL: {place.get('url', 'URL 없음')}")
    else:
        lines.append("- 데이터 없음")

    lines.append("")
    lines.append("## 1일 일정 제안")
    lines.append("")
    lines.append("- 오전: 추천 지역의 주요 명소를 가볍게 둘러봅니다.")
    lines.append("- 오후: 지역 행사나 축제를 방문합니다.")
    lines.append("- 저녁: 검색된 맛집 또는 주변 식당에서 식사합니다.")
    lines.append("")
    lines.append("## 오류 요약(errors)")
    lines.append("")

    if errors:
        for error in errors:
            lines.append(f"- [{error.get('step')}] {error.get('type')}: {error.get('message')}")
    else:
        lines.append("- 오류 없음")

    return "\n".join(lines)


def save_results(date_text, recommendation, restaurants, report, errors):
    RESULTS_DIR.mkdir(exist_ok=True)

    raw_data = {
        "date": date_text,
        "recommendation": recommendation,
        "restaurants": restaurants,
        "errors": errors
    }

    raw_json_path = RESULTS_DIR / f"{date_text}_raw_data.json"
    report_path = RESULTS_DIR / f"{date_text}_travel_plan.md"

    with open(raw_json_path, "w", encoding="utf-8") as file:
        json.dump(raw_data, file, ensure_ascii=False, indent=2)

    with open(report_path, "w", encoding="utf-8") as file:
        file.write(report)

    return raw_json_path, report_path


def main():
    load_dotenv()

    args = parse_args()
    date_text = args.date

    if not validate_date(date_text):
        print("[오류] 날짜 형식이 올바르지 않습니다.")
        print('사용 예시: python travel_planner.py -date "2026-03-15"')
        sys.exit(1)

    gemini_key, kakao_key = check_api_keys()
    errors = []

    print("[1/3] 1차 추천 생성 중(LLM)...")
    recommendation = get_travel_recommendation(date_text, gemini_key, errors)
    city = recommendation.get("recommended_city", "")
    print(f'  - recommended_city: "{city}"')

    print("[2/3] 맛집 검색 중(지도/장소 API)...")
    restaurants = search_restaurants(city, kakao_key, errors, size=5)

    if restaurants:
        print(f"  - 맛집 {len(restaurants)}곳 검색 완료")
    else:
        print("  - 맛집 데이터 없음")

    print("[3/3] 최종 리포트 생성 중(LLM)...")
    report = generate_markdown_report(
        date_text=date_text,
        recommendation=recommendation,
        restaurants=restaurants,
        errors=errors,
        gemini_key=gemini_key
    )
    print("  - 리포트 생성 완료")

    raw_json_path, report_path = save_results(
        date_text=date_text,
        recommendation=recommendation,
        restaurants=restaurants,
        report=report,
        errors=errors
    )

    print()
    print("완료!")
    print(f"원본 데이터 JSON: {raw_json_path}")
    print(f"최종 여행 리포트: {report_path}")


if __name__ == "__main__":
    main()