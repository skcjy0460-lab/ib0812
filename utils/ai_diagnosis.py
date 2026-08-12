# -*- coding: utf-8 -*-
"""
ai_diagnosis.py
================
심평원 급여기준 원문 + 청구 케이스 정보를 Gemini(3.6 Flash / 3.5 Flash-Lite)에
전달하여 구조화된 심사 가이드(JSON)를 받아온다.

핵심 설계
---------
1. **구조화 출력**: response_mime_type=application/json + response_schema를 사용해
   모델이 자유서술을 하지 않고 정해진 필드로만 답하도록 강제한다. 청구심사처럼
   실수가 곧 손실로 이어지는 업무에서는 "AI가 어디를 봤는지"가 사람이 읽을 수 있는
   고정된 틀로 나와야 검수가 가능하기 때문이다.
2. **모델 폴백**: 1차 모델(기본 gemini-3.6-flash) 호출이 쿼터 초과 등으로 실패하면
   2차 모델(기본 gemini-3.5-flash-lite)로 자동 재시도한다.
3. **면책/검증 유도**: 시스템 프롬프트에 "AI 요약은 참고용이며 최종 판단은 원문·
   심평원 공식 자료로 재확인해야 한다"는 원칙을 못박아, 모델이 과신하는 답을
   내놓지 않도록 유도하고, 결과 화면에도 동일 문구를 고정 노출한다 (report_generator 참고).
4. **온도 등 샘플링 파라미터 미사용**: Gemini 3.6 Flash / 3.5 Flash-Lite부터는
   temperature/top_p/top_k가 지원 중단(deprecated)되었으므로 의도적으로 설정하지 않는다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_PRIMARY_MODEL = "gemini-3.6-flash"
DEFAULT_FALLBACK_MODEL = "gemini-3.5-flash-lite"

QUOTA_ERROR_MARKERS = (
    "RESOURCE_EXHAUSTED",
    "quota",
    "429",
    "rate limit",
    "RateLimit",
)

SYSTEM_INSTRUCTION = """\
당신은 대한민국 건강보험심사평가원(HIRA)의 요양급여 심사기준을 검토하는
'청구심사 보조 전문가'입니다. 사용자는 병원에서 실제로 보험 청구 심사·삭감 방어
업무를 담당하는 전문 인력(심사 담당자, 원무/보험 담당자, 개원 컨설턴트)입니다.

다음 원칙을 반드시 지키세요.

1. 오직 제공된 급여기준 원문에 실제로 근거해서만 답변하십시오. 원문에 없는 내용을
   추측하거나 일반 상식으로 채워 넣지 마십시오. 원문에서 확인할 수 없는 항목은
   반드시 "원문에서 확인되지 않음"이라고 명시하십시오.
2. 인정 횟수, 인정 기간, 연령/부위/상병 제한, 병용 금지, 처방/시행 자격 요건,
   필수 첨부서류·기록, 고시번호와 시행일자 등 '삭감으로 직결되는 조건'을 절대
   누락하지 말고 최우선으로 짚으십시오.
3. source_quotes에는 반드시 원문에 실제로 존재하는 문장(또는 그 일부)만 그대로
   인용하십시오. 원문에 없는 문장을 인용문으로 만들어내면 안 됩니다. 인용은
   짧고 핵심적인 근거 문구 위주로 3~8개만 선별하십시오.
4. 청구 케이스 정보가 함께 제공된 경우, 그 케이스가 급여기준을 충족하는지
   "적합 / 주의필요 / 부적합 / 판단불가" 중 하나로 판정하고 그 근거를 원문 조항에
   연결지어 설명하십시오. 정보가 부족해 판단할 수 없으면 반드시 "판단불가"를
   선택하고 부족한 정보가 무엇인지 명시하십시오.
5. 이 진단 결과는 참고용 보조 자료이며, 최종 청구/심사 판단은 반드시 사람이
   원문 및 심평원 공식 자료로 재확인해야 한다는 점을 알고 있어야 합니다
   (이 경고 문구 자체는 앱 화면에 별도로 고정 표시되므로 confidence_note에는
   당신이 판단하기에 특별히 주의가 필요한 부분만 간결히 적으십시오).
6. 모든 답변은 한국어, 실무자가 바로 읽고 쓸 수 있는 간결한 문체로 작성하십시오.
"""

RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "summary": {
            "type": "STRING",
            "description": "급여기준 핵심을 3~6문장으로 요약",
        },
        "notice_reference": {
            "type": "STRING",
            "description": "관련 고시번호 (예: 고시 제2026-136호). 원문에 없으면 빈 문자열",
        },
        "effective_date": {
            "type": "STRING",
            "description": "시행일자. 원문에 없으면 빈 문자열",
        },
        "key_criteria": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "급여 인정을 위한 핵심 요건 목록 (대상 질환, 적응증 등)",
        },
        "frequency_limits": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "인정 횟수·기간·연령 등 정량적 제한 사항",
        },
        "required_documentation": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "필수 첨부서류, 기록·보존 의무, 처방/시행 자격 요건 등",
        },
        "exclusions_or_cautions": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "병용 금지, 제외 사유, 삭감 위험이 큰 주의사항",
        },
        "case_match": {
            "type": "OBJECT",
            "properties": {
                "verdict": {
                    "type": "STRING",
                    "enum": ["적합", "주의필요", "부적합", "판단불가", "케이스정보없음"],
                },
                "reasoning": {"type": "STRING"},
                "risk_points": {"type": "ARRAY", "items": {"type": "STRING"}},
                "missing_information": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "판단에 추가로 필요한 정보",
                },
            },
            "required": ["verdict", "reasoning"],
        },
        "checklist": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "청구 전 실무자가 순서대로 확인할 체크리스트 (행동 지시형 문장)",
        },
        "source_quotes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "quote": {"type": "STRING"},
                    "context": {"type": "STRING", "description": "이 인용문이 어떤 항목과 관련있는지"},
                },
                "required": ["quote"],
            },
        },
        "confidence_note": {
            "type": "STRING",
            "description": "AI가 특별히 불확실하다고 느끼는 부분에 대한 간결한 코멘트",
        },
    },
    "required": [
        "summary",
        "key_criteria",
        "frequency_limits",
        "required_documentation",
        "exclusions_or_cautions",
        "case_match",
        "checklist",
        "source_quotes",
    ],
}


@dataclass
class DiagnosisResult:
    ok: bool
    model_used: str = ""
    data: Optional[Dict[str, Any]] = None
    raw_text: str = ""
    error: str = ""
    attempts: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


def _build_user_prompt(criteria_text: str, case_info: Dict[str, str]) -> str:
    case_lines = []
    labels = {
        "department": "진료과",
        "primary_diagnosis": "주상병(명/코드)",
        "secondary_diagnosis": "부상병(명/코드)",
        "procedure_code": "시행 수가코드/명칭",
        "treatment_count": "청구(예정) 시행횟수",
        "treatment_period": "시행 기간/최근 시행일",
        "patient_age": "환자 연령",
        "memo": "특이사항/메모",
    }
    for key, label in labels.items():
        val = (case_info or {}).get(key, "").strip()
        if val:
            case_lines.append(f"- {label}: {val}")
    case_block = "\n".join(case_lines) if case_lines else "(청구 케이스 정보 미입력 - 급여기준 자체 요약만 수행)"

    return f"""[급여기준 원문]
{criteria_text}

[청구 케이스 정보]
{case_block}

위 급여기준 원문을 검토하여 지정된 JSON 스키마에 맞게 심사 가이드를 작성하십시오.
청구 케이스 정보가 있다면 case_match를 반드시 채우고, 없다면 verdict를
"케이스정보없음"으로 설정하십시오."""


def _looks_like_quota_error(exc: Exception) -> bool:
    msg = str(exc)
    return any(marker.lower() in msg.lower() for marker in QUOTA_ERROR_MARKERS)


def _call_gemini(api_key: str, model: str, criteria_text: str, case_info: Dict[str, str]):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    prompt = _build_user_prompt(criteria_text, case_info)

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        max_output_tokens=8192,
        # 참고: gemini-3.6-flash / gemini-3.5-flash-lite부터 temperature/top_p/top_k는
        # 지원 중단되어 의도적으로 설정하지 않습니다.
    )
    response = client.models.generate_content(model=model, contents=prompt, config=config)
    return response


def run_diagnosis(
    api_key: str,
    criteria_text: str,
    case_info: Dict[str, str],
    primary_model: str = DEFAULT_PRIMARY_MODEL,
    fallback_model: str = DEFAULT_FALLBACK_MODEL,
) -> DiagnosisResult:
    if not api_key:
        return DiagnosisResult(ok=False, error="Gemini API 키가 입력되지 않았습니다.")
    if not criteria_text or not criteria_text.strip():
        return DiagnosisResult(ok=False, error="분석할 급여기준 원문이 없습니다. 자료를 먼저 입력해 주세요.")

    start = time.time()
    attempts: List[str] = []
    models_to_try = [m for m in [primary_model, fallback_model] if m]
    last_error: Optional[Exception] = None

    for idx, model in enumerate(models_to_try):
        attempts.append(model)
        try:
            response = _call_gemini(api_key, model, criteria_text, case_info)
            raw_text = getattr(response, "text", None) or ""
            if not raw_text:
                # 후보가 비어있거나 안전필터에 걸린 경우 등
                raise RuntimeError("모델이 빈 응답을 반환했습니다 (안전 필터 또는 응답 길이 초과 가능).")
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as jde:
                return DiagnosisResult(
                    ok=False,
                    model_used=model,
                    raw_text=raw_text,
                    error=f"모델 응답을 JSON으로 해석하지 못했습니다: {jde}",
                    attempts=attempts,
                    elapsed_seconds=time.time() - start,
                )
            return DiagnosisResult(
                ok=True,
                model_used=model,
                data=data,
                raw_text=raw_text,
                attempts=attempts,
                elapsed_seconds=time.time() - start,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            is_last = idx == len(models_to_try) - 1
            if is_last or not _looks_like_quota_error(exc):
                break
            # 쿼터 초과로 보이면 다음 모델로 폴백 계속
            continue

    return DiagnosisResult(
        ok=False,
        error=f"AI 진단 호출에 실패했습니다: {last_error}",
        attempts=attempts,
        elapsed_seconds=time.time() - start,
    )


def verify_quotes_against_source(quotes: List[Dict[str, str]], source_text: str) -> List[Dict[str, Any]]:
    """모델이 만든 인용문이 실제 원문에 존재하는지 문자열 포함 여부로 교차검증한다.

    완전히 일치하지 않더라도(공백/개행 차이 등) 정규화 후 재확인하여
    '원문 미확인' 오탐을 줄인다.
    """

    def _normalize(s: str) -> str:
        return "".join(s.split())

    normalized_source = _normalize(source_text)
    results = []
    for q in quotes or []:
        quote = (q.get("quote") or "").strip()
        if not quote:
            continue
        found = _normalize(quote) in normalized_source
        results.append({
            "quote": quote,
            "context": q.get("context", ""),
            "verified_in_source": found,
        })
    return results
