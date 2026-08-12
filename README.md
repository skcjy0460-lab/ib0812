# 🩺 청구심사 AI 가이드

병원 청구심사(보험청구/삭감 방어) 담당자가 건강보험심사평가원(HIRA)의 급여기준
자료를 업로드하면, Gemini AI가 핵심 인정요건·인정횟수·필수서류·주의사항을
짚어주고 실제 청구 케이스와 대조해 **적합 / 주의필요 / 부적합**을 판정한 뒤,
인쇄 가능한 HTML 보고서로 저장할 수 있게 해주는 도구입니다.

---

## 1. 주요 기능

| 단계 | 기능 |
|---|---|
| ① 자료 입력 | 급여기준 파일(PDF·HWP·HWPX·DOCX·TXT) 다중 업로드 + 텍스트 직접 입력. 추출된 텍스트는 AI 진단 전 화면에서 직접 검수/수정 가능 |
| ② 케이스 정보 | 진료과·상병명·수가코드·시행횟수 등 실제 청구 건 정보 입력 (선택) |
| ③ AI 진단 | Gemini 3.6 Flash(기본) → 쿼터 초과 시 Gemini 3.5 Flash-Lite로 자동 폴백. 구조화된 JSON 스키마로 응답을 강제해 항목 누락 방지 |
| ④ 결과·보고서 | 판정 배지, 근거 인용문의 원문 대조 자동 검증(✅/⚠️), 체크리스트, 건별/일괄 HTML 보고서 다운로드 |

## 2. 로컬 실행

```bash
pip install -r requirements.txt
streamlit run app.py
```

브라우저에서 `http://localhost:8501` 접속 후, 사이드바에 Gemini API 키를 입력하면
바로 사용할 수 있습니다. (API 키는 서버/파일에 저장되지 않고 세션에만 유지됩니다.)

Gemini API 키는 [Google AI Studio](https://aistudio.google.com/apikey)에서
무료로 발급받을 수 있습니다.

## 3. Streamlit Cloud 배포 (GitHub 드래그앤드롭)

1. 이 폴더(app.py, utils/, requirements.txt, .streamlit/)를 그대로 GitHub
   저장소에 업로드합니다.
2. [share.streamlit.io](https://share.streamlit.io)에서 새 앱을 생성하고
   저장소/브랜치/`app.py`를 지정합니다.
3. (선택) 유료 전용 배포로 접근을 제한하려면 Streamlit Cloud의
   **App settings → Secrets**에 아래와 같이 접근 코드를 등록하세요.
   등록하지 않으면 접근코드 입력 화면 없이 바로 사용됩니다.

   ```toml
   ACCESS_CODE = "고객에게 배포할 임의의 코드"
   ```

## 4. HWP 파일 처리에 대한 참고사항

심평원 배포 `.hwp` 파일 중 일부는 표준 `pyhwp` 라이브러리가 요구하는
문서 요약정보 스트림이 빠져 있어 일반적인 변환 도구로 열리지 않는 경우가
있습니다. 이 앱은 `utils/hwp_parser.py`에서 OLE 구조를 직접 파싱하는
전용 로직으로 이런 파일도 처리하도록 만들어졌습니다. 다만 표·각주 등 복잡한
서식은 완전히 재현되지 않을 수 있으므로, **AI 진단 실행 전 반드시 추출된
텍스트를 화면에서 원문과 대조 확인**하시길 권장합니다. (앱 내 '① 자료 입력'
탭에서 추출 텍스트를 직접 수정할 수 있습니다.)

## 5. 폴더 구조

```
claimreview_app/
├── app.py                     # 메인 Streamlit 앱
├── requirements.txt
├── README.md
├── .streamlit/
│   └── config.toml            # 테마/업로드 용량 설정
└── utils/
    ├── file_extractor.py      # PDF/HWP/HWPX/DOCX/TXT 텍스트 추출 라우터
    ├── hwp_parser.py          # HWP OLE 저수준 파서
    ├── ai_diagnosis.py        # Gemini 구조화 진단 호출 + 모델 폴백
    └── report_generator.py    # HTML 보고서 생성 (건별/일괄)
```

## 6. 중요 유의사항

본 도구가 생성하는 AI 진단 결과 및 보고서는 **참고용 보조 자료**입니다.
실제 요양급여 인정 여부와 청구 최종 판단은 반드시 심평원 공식 고시·심사기준
원문 및 담당자의 전문적 검토를 통해 확정해야 합니다. 이 원칙은 앱 화면과
생성되는 모든 보고서 하단에 고정 문구로 함께 표시됩니다.
