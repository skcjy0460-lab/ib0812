# -*- coding: utf-8 -*-
"""
app.py
======
청구심사 AI 가이드 (급여기준 AI 진단 & 보고서 생성 도구)

병원 청구심사(보험청구/삭감 방어) 담당자가 심평원 급여기준 자료(파일 또는 텍스트)를
입력하면, Gemini AI가 핵심 인정요건·인정횟수·필수서류·주의사항을 짚어주고
실제 청구 케이스와 대조해 적합/주의/부적합을 판정한 뒤, 인쇄 가능한 HTML
보고서로 저장할 수 있게 해주는 유료 전용 도구입니다.

실행:
    streamlit run app.py
"""
from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime

import streamlit as st

from utils.ai_diagnosis import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_PRIMARY_MODEL,
    run_diagnosis,
    verify_quotes_against_source,
)
from utils.file_extractor import SUPPORTED_EXTENSIONS, extract_text_from_file
from utils.report_generator import (
    ReportContext,
    generate_batch_html_report,
    generate_blog_summary_html,
    generate_html_report,
)

# ----------------------------------------------------------------------------
# 기본 설정
# ----------------------------------------------------------------------------
st.set_page_config(
    page_title="청구심사 AI 가이드",
    page_icon="🩺",
    layout="wide",
    initial_sidebar_state="expanded",
)

AVAILABLE_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

CASE_FORM_GROUPS = [
    {
        "title": "진단 정보",
        "icon": "🩺",
        "fields": [
            ("department", "진료과", "text"),
            ("primary_diagnosis", "주상병명 / 상병코드", "text"),
            ("diagnosis_date", "진단일", "text"),
            ("secondary_diagnosis", "부상병명 / 상병코드", "text"),
        ],
    },
    {
        "title": "시행 내역",
        "icon": "📋",
        "fields": [
            ("procedure_code", "시행 수가코드 / 명칭", "text"),
            ("treatment_period", "시행 기간 / 최근 시행일", "text"),
            ("treatment_count", "금회 청구 시행횟수", "text"),
            ("cumulative_count", "누적(연간) 시행횟수", "text"),
            ("prescriber", "처방의 / 전문과목", "text"),
        ],
    },
    {
        "title": "청구 정보",
        "icon": "🧾",
        "fields": [
            ("claim_type", "청구 구분 (외래 / 입원)", "text"),
            ("special_code", "특정기호", "text"),
            ("prior_claim", "전월 동일항목 청구 여부", "text"),
        ],
    },
]

# 모든 텍스트 필드 + 메모(memo)를 합친 case_info 기본 키 목록
CASE_FIELD_DEFS = [f for g in CASE_FORM_GROUPS for f in g["fields"]] + [("memo", "특이사항 / 메모", "area")]

ATTACHMENT_CHECKLIST_LABELS = ["진단서", "영상자료(X-ray/MRI 등)", "치료기록지", "소견서", "기타 증빙자료"]


def _init_state() -> None:
    defaults = {
        "authorized": False,
        "api_key": "",
        "primary_model": DEFAULT_PRIMARY_MODEL,
        "fallback_model": DEFAULT_FALLBACK_MODEL,
        "hospital_name": "",
        "author_name": "",
        "case_title": "",
        "source_docs": [],  # list of ExtractedDocument-like dict
        "manual_text": "",
        "case_info": {k: "" for k, _, _ in CASE_FIELD_DEFS},
        "case_attachments": [],  # list of {"filename": str, "size": int, "category": str}
        "attachment_uploader_key": 0,
        "case_form_version": 0,
        "last_result": None,  # dict: diagnosis + meta
        "history": [],  # list of same shape as last_result
        "uploader_key": 0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


# ----------------------------------------------------------------------------
# 접근 코드 게이트 (유료 전용 배포용 - st.secrets["ACCESS_CODE"]가 설정된 경우만 동작)
# ----------------------------------------------------------------------------
def _check_access_gate() -> bool:
    required_code = st.secrets.get("ACCESS_CODE", "") if hasattr(st, "secrets") else ""
    if not required_code:
        return True  # 접근코드 미설정 시 게이트 없이 통과 (개발/내부용)
    if st.session_state.authorized:
        return True

    st.markdown("## 🔒 청구심사 AI 가이드 - 이용 인증")
    st.caption("본 프로그램은 유료 구독자 전용입니다. 컨설턴트로부터 발급받은 접근 코드를 입력하세요.")
    code = st.text_input("접근 코드", type="password", key="access_code_input")
    if st.button("입장하기", type="primary"):
        if code == required_code:
            st.session_state.authorized = True
            st.rerun()
        else:
            st.error("접근 코드가 올바르지 않습니다.")
    return False


# ----------------------------------------------------------------------------
# 사이드바
# ----------------------------------------------------------------------------
def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown("### 🩺 청구심사 AI 가이드")
        st.caption("심평원 급여기준 AI 진단 · 보고서 생성 도구")
        st.divider()

        st.markdown("#### ⚙️ AI 설정")
        st.session_state.api_key = st.text_input(
            "Gemini API 키",
            value=st.session_state.api_key,
            type="password",
            help="이 키는 서버에 저장되지 않으며 현재 세션에서만 사용됩니다.",
        )
        st.session_state.primary_model = st.selectbox(
            "1차 모델", AVAILABLE_MODELS,
            index=AVAILABLE_MODELS.index(st.session_state.primary_model)
            if st.session_state.primary_model in AVAILABLE_MODELS else 0,
        )
        fallback_options = ["(사용 안 함)"] + AVAILABLE_MODELS
        current_fallback = st.session_state.fallback_model or "(사용 안 함)"
        st.session_state.fallback_model = st.selectbox(
            "2차(폴백) 모델 - 1차 모델 쿼터 초과 시 자동 전환",
            fallback_options,
            index=fallback_options.index(current_fallback) if current_fallback in fallback_options else 0,
        )
        if st.session_state.fallback_model == "(사용 안 함)":
            st.session_state.fallback_model = ""

        st.divider()
        st.markdown("#### 🏥 보고서 표기 정보")
        st.session_state.hospital_name = st.text_input("병원/기관명", value=st.session_state.hospital_name)
        st.session_state.author_name = st.text_input("작성자", value=st.session_state.author_name)

        st.divider()
        st.markdown(f"#### 🗂 이력 ({len(st.session_state.history)}건)")
        if st.session_state.history:
            for item in reversed(st.session_state.history[-10:]):
                verdict = (item["diagnosis"].get("case_match") or {}).get("verdict", "-")
                st.caption(f"• {item['case_title']} — {verdict} ({item['timestamp']})")
            if st.button("이력 전체 삭제", use_container_width=True):
                st.session_state.history = []
                st.rerun()
        else:
            st.caption("아직 저장된 진단 이력이 없습니다.")

        st.divider()
        st.caption(
            "⚠ 본 도구의 AI 진단 결과는 참고용입니다. 최종 청구 판단은 반드시 "
            "심평원 공식 원문과 담당자 검토를 통해 확정하세요."
        )


# ----------------------------------------------------------------------------
# 1. 자료 입력
# ----------------------------------------------------------------------------
def _render_input_tab() -> None:
    st.subheader("1️⃣ 급여기준 자료 입력")
    st.caption(
        "심평원(HIRA)에서 다운로드한 급여기준 파일을 업로드하거나, 텍스트를 직접 붙여넣으세요. "
        f"지원 형식: {', '.join(SUPPORTED_EXTENSIONS)} · 여러 파일을 동시에 업로드하면 하나로 합쳐 분석합니다."
    )

    uploaded_files = st.file_uploader(
        "급여기준 파일 업로드 (여러 개 선택 가능)",
        type=[ext.lstrip(".") for ext in SUPPORTED_EXTENSIONS],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state.uploader_key}",
    )

    col_a, col_b = st.columns([1, 1])
    with col_a:
        process_clicked = st.button("📥 업로드 파일에서 텍스트 추출", use_container_width=True, disabled=not uploaded_files)
    with col_b:
        if st.button("🗑 업로드 자료 초기화", use_container_width=True):
            st.session_state.source_docs = []
            st.session_state.uploader_key += 1
            st.rerun()

    if process_clicked and uploaded_files:
        results = []
        with st.spinner("파일에서 텍스트를 추출하는 중입니다..."):
            for uf in uploaded_files:
                suffix = os.path.splitext(uf.name)[1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(uf.getbuffer())
                    tmp_path = tmp.name
                try:
                    doc = extract_text_from_file(tmp_path, uf.name)
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                results.append({
                    "filename": doc.filename,
                    "text": doc.text,
                    "warnings": doc.warnings,
                    "page_count": doc.page_count,
                })
        st.session_state.source_docs = results

    if st.session_state.source_docs:
        st.markdown("##### 📄 추출 결과 (검수 및 수정 가능)")
        st.info(
            "AI 진단 정확도는 원문 추출 품질에 직결됩니다. HWP/PDF는 표·특수문자 변환 과정에서 "
            "오탈자가 생길 수 있으니, 분석 전 아래 텍스트를 원문과 대조하여 필요 시 직접 수정하세요.",
            icon="🔍",
        )
        for i, doc in enumerate(st.session_state.source_docs):
            for w in doc.get("warnings", []):
                st.warning(f"[{doc['filename']}] {w}")
            with st.expander(f"📄 {doc['filename']}" + (f" ({doc['page_count']}페이지)" if doc.get("page_count") else ""), expanded=(i == 0)):
                edited = st.text_area(
                    "추출된 텍스트 (수정 가능)",
                    value=doc["text"],
                    height=280,
                    key=f"doc_text_{i}",
                    label_visibility="collapsed",
                )
                st.session_state.source_docs[i]["text"] = edited

    st.markdown("##### ✏️ 텍스트 직접 입력 / 추가")
    st.caption("파일 업로드 없이 급여기준 문구를 바로 붙여넣거나, 업로드 자료에 보충 설명을 추가할 수 있습니다.")
    st.session_state.manual_text = st.text_area(
        "직접 입력 텍스트",
        value=st.session_state.manual_text,
        height=180,
        placeholder="예) 심평원 홈페이지에서 복사한 심사기준 조회 결과, 관련 유권해석 등",
        label_visibility="collapsed",
    )

    combined_len = len(_combined_source_text())
    st.caption(f"현재 분석 대상 총 글자 수: **{combined_len:,}자**")


def _combined_source_text() -> str:
    parts = []
    for doc in st.session_state.source_docs:
        if doc.get("text", "").strip():
            parts.append(f"===== 📄 {doc['filename']} =====\n{doc['text'].strip()}")
    if st.session_state.manual_text.strip():
        parts.append(f"===== ✏️ 직접 입력 텍스트 =====\n{st.session_state.manual_text.strip()}")
    return "\n\n".join(parts)


def _combined_filenames() -> list:
    return [doc["filename"] for doc in st.session_state.source_docs]


# ----------------------------------------------------------------------------
# 2. 케이스 정보
# ----------------------------------------------------------------------------
def _reset_case_info() -> None:
    st.session_state.case_title = ""
    st.session_state.case_info = {k: "" for k, _, _ in CASE_FIELD_DEFS}
    st.session_state.case_attachments = []
    st.session_state.attachment_uploader_key += 1
    st.session_state.case_form_version += 1  # 위젯 key 버전을 올려 입력값을 강제로 비움


def _render_case_tab() -> None:
    header_cols = st.columns([5, 1.4])
    with header_cols[0]:
        st.subheader("2️⃣ 청구 케이스 정보 (선택 입력)")
    with header_cols[1]:
        st.write("")
        if st.button("🔄 케이스 정보 초기화", use_container_width=True):
            _reset_case_info()
            st.rerun()

    st.caption(
        "실제 청구 건 정보를 입력하면 AI가 급여기준과 케이스를 대조해 적합/주의/부적합을 판정합니다. "
        "비워두면 급여기준 자체에 대한 요약·체크리스트만 생성됩니다. 환자 개인식별정보(이름 등)는 "
        "입력하지 않는 것을 권장합니다."
    )
    st.session_state.case_title = st.text_input(
        "케이스 제목 (보고서 상단에 표시됩니다)",
        value=st.session_state.case_title,
        placeholder="예) 도수치료 15회 초과 청구 검토",
        key=f"case_title_{st.session_state.case_form_version}",
    )

    fv = st.session_state.case_form_version
    for group in CASE_FORM_GROUPS:
        st.markdown(f"##### {group['icon']} {group['title']}")
        cols = st.columns(len(group["fields"]) if len(group["fields"]) <= 3 else 3)
        for idx, (key, label, kind) in enumerate(group["fields"]):
            with cols[idx % len(cols)]:
                st.session_state.case_info[key] = st.text_input(
                    label, value=st.session_state.case_info.get(key, ""), key=f"case_{key}_{fv}"
                )
        st.markdown("")

    st.markdown("##### 📎 첨부 · 특이사항")
    st.caption(
        "진단서, 영상자료, 치료기록지 등 청구 근거 자료를 첨부해 두면 보고서에 첨부 목록이 함께 기록됩니다. "
        "(파일 내용은 AI 분석에 사용되지 않고 파일명만 기록·보관됩니다.)"
    )

    attach_cols = st.columns([3, 1])
    with attach_cols[0]:
        new_files = st.file_uploader(
            "첨부 자료 업로드 (여러 개 선택 가능)",
            accept_multiple_files=True,
            key=f"attachment_uploader_{st.session_state.attachment_uploader_key}",
            label_visibility="collapsed",
        )
    with attach_cols[1]:
        attach_category = st.selectbox("구분", ATTACHMENT_CHECKLIST_LABELS, label_visibility="collapsed")

    if st.button("➕ 첨부 자료 추가", disabled=not new_files):
        for uf in new_files or []:
            st.session_state.case_attachments.append({
                "filename": uf.name,
                "size": uf.size,
                "category": attach_category,
            })
        st.session_state.attachment_uploader_key += 1
        st.rerun()

    if st.session_state.case_attachments:
        for i, att in enumerate(st.session_state.case_attachments):
            row = st.columns([5, 2, 1])
            row[0].markdown(f"📄 {att['filename']}")
            row[1].caption(att["category"])
            if row[2].button("삭제", key=f"del_attach_{i}"):
                st.session_state.case_attachments.pop(i)
                st.rerun()
    else:
        st.caption("첨부된 자료가 없습니다.")

    st.session_state.case_info["memo"] = st.text_area(
        "특이사항 / 메모",
        value=st.session_state.case_info.get("memo", ""),
        height=100,
        key=f"case_memo_{fv}",
    )


# ----------------------------------------------------------------------------
# 3. AI 진단
# ----------------------------------------------------------------------------
def _render_diagnosis_tab() -> None:
    st.subheader("3️⃣ AI 진단 실행")

    source_text = _combined_source_text()
    if not source_text.strip():
        st.warning("먼저 [1️⃣ 급여기준 자료 입력] 탭에서 파일을 업로드하거나 텍스트를 입력해 주세요.")
        return
    if not st.session_state.api_key:
        st.warning("사이드바에 Gemini API 키를 입력해 주세요.")

    with st.expander("🔎 분석에 사용될 원문 미리보기", expanded=False):
        st.text(source_text[:5000] + ("\n...(생략)..." if len(source_text) > 5000 else ""))

    run_clicked = st.button(
        "🤖 AI 진단 실행",
        type="primary",
        use_container_width=True,
        disabled=not (source_text.strip() and st.session_state.api_key),
    )

    if run_clicked:
        with st.spinner("AI가 급여기준을 분석하는 중입니다..."):
            result = run_diagnosis(
                api_key=st.session_state.api_key,
                criteria_text=source_text,
                case_info=st.session_state.case_info,
                primary_model=st.session_state.primary_model,
                fallback_model=st.session_state.fallback_model,
            )

        if not result.ok:
            st.error(f"AI 진단에 실패했습니다: {result.error}")
            if result.raw_text:
                with st.expander("모델 원본 응답 (디버그용)"):
                    st.code(result.raw_text)
            return

        verified_quotes = verify_quotes_against_source(result.data.get("source_quotes", []), source_text)

        record = {
            "id": str(uuid.uuid4()),
            "case_title": st.session_state.case_title or f"미제목 케이스 ({datetime.now().strftime('%H:%M:%S')})",
            "case_info": dict(st.session_state.case_info),
            "case_attachments": list(st.session_state.case_attachments),
            "source_filenames": _combined_filenames(),
            "source_text": source_text,
            "model_used": result.model_used,
            "diagnosis": result.data,
            "verified_quotes": verified_quotes,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        st.session_state.last_result = record
        st.session_state.history.append(record)
        st.success(f"AI 진단이 완료되었습니다. (소요 {result.elapsed_seconds:.1f}초)")
        if result.model_used != st.session_state.primary_model:
            st.info(f"1차 모델 대신 폴백 모델({result.model_used})이 사용되었습니다.")


# ----------------------------------------------------------------------------
# 4. 결과 & 보고서
# ----------------------------------------------------------------------------
_VERDICT_ICON = {
    "적합": "🟢", "주의필요": "🟡", "부적합": "🔴", "판단불가": "⚪", "케이스정보없음": "⚪",
}


def _render_result_block(record: dict) -> None:
    d = record["diagnosis"]
    case_match = d.get("case_match", {}) or {}
    verdict = case_match.get("verdict", "판단불가")

    top = st.columns([1, 3])
    with top[0]:
        st.metric("케이스 판정", f"{_VERDICT_ICON.get(verdict,'⚪')} {verdict}")
    with top[1]:
        st.caption(f"모델: `{record['model_used']}` · 분석 시각: {record['timestamp']} · 참조 파일: {', '.join(record['source_filenames']) or '직접 입력'}")
        st.write(case_match.get("reasoning", ""))

    if case_match.get("risk_points"):
        st.markdown("**⚠ 위험 요소**")
        for r in case_match["risk_points"]:
            st.markdown(f"- {r}")
    if case_match.get("missing_information"):
        st.markdown("**❓ 판단에 추가로 필요한 정보**")
        for m in case_match["missing_information"]:
            st.markdown(f"- {m}")

    if record.get("case_attachments"):
        st.markdown("**📎 첨부 자료**")
        for att in record["case_attachments"]:
            st.caption(f"📄 {att['filename']} ({att['category']})")

    st.divider()
    st.markdown("#### 📋 급여기준 핵심 요약")
    st.write(d.get("summary", ""))
    meta_cols = st.columns(2)
    meta_cols[0].markdown(f"**관련 고시번호**: {d.get('notice_reference') or '원문에서 확인되지 않음'}")
    meta_cols[1].markdown(f"**시행일자**: {d.get('effective_date') or '원문에서 확인되지 않음'}")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### ✅ 핵심 인정 요건")
        for item in d.get("key_criteria", []) or ["원문에서 확인되지 않음"]:
            st.markdown(f"- {item}")
        st.markdown("#### 🔢 인정 횟수 · 기간 제한")
        for item in d.get("frequency_limits", []) or ["원문에서 확인되지 않음"]:
            st.markdown(f"- {item}")
    with c2:
        st.markdown("#### 📎 필수 서류 · 자격 요건")
        for item in d.get("required_documentation", []) or ["원문에서 확인되지 않음"]:
            st.markdown(f"- {item}")
        st.markdown("#### 🚫 제외 사유 · 주의사항")
        for item in d.get("exclusions_or_cautions", []) or ["원문에서 확인되지 않음"]:
            st.markdown(f"- {item}")

    st.markdown("#### 🧾 청구 전 체크리스트")
    for item in d.get("checklist", []) or []:
        st.checkbox(item, key=f"chk_{record['id']}_{hash(item)}")

    st.markdown("#### 📖 원문 근거 인용")
    for q in record["verified_quotes"]:
        icon = "✅" if q["verified_in_source"] else "⚠️"
        st.markdown(f"> {icon} “{q['quote']}”  \n<sub>{q.get('context','')}</sub>", unsafe_allow_html=True)
        if not q["verified_in_source"]:
            st.caption("원문에서 정확히 일치하는 문구를 찾지 못했습니다. 반드시 원문을 재확인하세요.")

    if d.get("confidence_note"):
        st.markdown("#### 💡 AI 참고 코멘트")
        st.info(d["confidence_note"])


def _build_report_context(record: dict) -> ReportContext:
    return ReportContext(
        hospital_name=st.session_state.hospital_name,
        author_name=st.session_state.author_name,
        case_title=record["case_title"],
        case_info=record["case_info"],
        case_attachments=record.get("case_attachments", []),
        source_filenames=record["source_filenames"],
        source_text=record["source_text"],
        model_used=record["model_used"],
        diagnosis=record["diagnosis"],
        verified_quotes=record["verified_quotes"],
    )


def _render_report_tab() -> None:
    st.subheader("4️⃣ 결과 · 보고서")

    if not st.session_state.history:
        st.info("아직 AI 진단 결과가 없습니다. [3️⃣ AI 진단 실행] 탭에서 먼저 진단을 실행하세요.")
        return

    titles = [f"{i+1}. {h['case_title']} ({h['timestamp']})" for i, h in enumerate(st.session_state.history)]
    selected_idx = st.selectbox("조회할 진단 결과 선택", range(len(titles)), format_func=lambda i: titles[i], index=len(titles) - 1)
    record = st.session_state.history[selected_idx]

    _render_result_block(record)

    st.divider()
    st.markdown("#### 💾 HTML 보고서 저장")
    dl_cols = st.columns(2)
    with dl_cols[0]:
        single_html = generate_html_report(_build_report_context(record), include_source=True)
        st.download_button(
            "📄 이 건 보고서 다운로드 (HTML)",
            data=single_html.encode("utf-8"),
            file_name=f"청구심사_AI진단_{record['case_title'][:20]}_{datetime.now().strftime('%Y%m%d_%H%M')}.html",
            mime="text/html",
            use_container_width=True,
        )
    with dl_cols[1]:
        if len(st.session_state.history) > 1:
            batch_html = generate_batch_html_report([_build_report_context(h) for h in st.session_state.history])
            st.download_button(
                f"🗂 전체 {len(st.session_state.history)}건 일괄 보고서 다운로드 (HTML)",
                data=batch_html.encode("utf-8"),
                file_name=f"청구심사_AI진단_일괄보고서_{datetime.now().strftime('%Y%m%d_%H%M')}.html",
                mime="text/html",
                use_container_width=True,
            )
        else:
            st.caption("진단 이력이 2건 이상일 때 일괄 보고서를 생성할 수 있습니다.")

    st.divider()
    st.markdown("#### 📝 블로그용 요약 카드")
    st.caption(
        "청구 케이스 정보·원문 전체 없이, 급여기준 핵심 내용만 담은 스크린샷용 요약 카드입니다. "
        "블로그·SNS 등 공개 채널에 올리기 전 개별 청구 건 정보가 섞이지 않도록 설계했습니다."
    )
    blog_html = generate_blog_summary_html(_build_report_context(record))
    with st.expander("🔍 미리보기", expanded=False):
        st.components.v1.html(blog_html, height=760, scrolling=True)
    st.download_button(
        "📝 블로그용 요약 카드 다운로드 (HTML)",
        data=blog_html.encode("utf-8"),
        file_name=f"블로그요약_{record['case_title'][:20]}_{datetime.now().strftime('%Y%m%d_%H%M')}.html",
        mime="text/html",
        use_container_width=True,
    )
    st.caption("💡 다운로드한 HTML을 브라우저로 열면 [🖼 이미지(JPG)로 저장] 버튼으로 카드 전체를 한 장의 이미지로 저장할 수 있습니다. (인터넷 연결 필요, 버튼이 안 되면 화면을 직접 캡처하세요.)")


# ----------------------------------------------------------------------------
# 메인
# ----------------------------------------------------------------------------
def main() -> None:
    if not _check_access_gate():
        return

    _render_sidebar()

    st.title("🩺 청구심사 AI 가이드")
    st.caption("심평원 급여기준 → AI 핵심 요약·체크리스트·케이스 판정 → 인쇄용 HTML 보고서")

    tab1, tab2, tab3, tab4 = st.tabs([
        "1️⃣ 급여기준 자료 입력",
        "2️⃣ 청구 케이스 정보",
        "3️⃣ AI 진단 실행",
        "4️⃣ 결과 · 보고서",
    ])
    with tab1:
        _render_input_tab()
    with tab2:
        _render_case_tab()
    with tab3:
        _render_diagnosis_tab()
    with tab4:
        _render_report_tab()


if __name__ == "__main__":
    main()
