# -*- coding: utf-8 -*-
"""
report_generator.py
====================
AI 진단 결과(및 원문 근거)를 저장/인쇄 가능한 단일 HTML 파일로 변환한다.

- 외부 리소스 없이 단일 파일로 완결(폰트/CSS 인라인)되어 병원 내부망에서도
  바로 열람/보관/출력이 가능하다.
- 병원명/작성자 등 헤더 커스터마이즈를 지원한다.
- 보고서 하단에 AI 참고용 고지문을 고정 삽입한다 (책임 소재 명확화).
- 여러 건을 모아 '일괄 보고서'로도 생성할 수 있다.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

DISCLAIMER_TEXT = (
    "본 보고서는 입력된 급여기준 원문을 바탕으로 AI가 생성한 "
    "참고용 요약·분석 자료입니다. 실제 요양급여 인정 여부, 삭감 위험 판단 및 "
    "최종 청구 결정은 반드시 건강보험심사평가원(HIRA)의 최신 공식 고시·심사기준 "
    "원문과 담당자의 전문적 검토를 통해 이루어져야 하며, 본 보고서 내용과 실제 "
    "심사 결과가 다를 수 있습니다. 본 자료의 사용으로 발생하는 결과에 대해 "
    "작성 프로그램 제공자는 책임을 지지 않습니다."
)

_BASE_CSS = """
:root{
  --navy:#0f2a4a; --navy-2:#16385f; --accent:#1c6fd6; --accent-soft:#eaf2fd;
  --ink:#1b2430; --muted:#5b6672; --line:#e1e6ec; --ok:#1a7f4b; --warn:#b8860b;
  --danger:#b3261e; --bg:#f5f7fa;
}
*{box-sizing:border-box;}
body{
  margin:0; padding:0; background:var(--bg); color:var(--ink);
  font-family:'Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',sans-serif;
  line-height:1.62; font-size:14.5px;
}
.page{max-width:900px;margin:0 auto;padding:36px 40px 60px;background:#fff;}
.report-header{
  display:flex;justify-content:space-between;align-items:flex-start;
  border-bottom:3px solid var(--navy);padding-bottom:18px;margin-bottom:26px;
}
.report-header h1{font-size:22px;margin:0 0 6px;color:var(--navy);}
.report-header .org{font-size:13px;color:var(--muted);}
.report-header .meta{text-align:right;font-size:12.5px;color:var(--muted);line-height:1.8;}
.badge{
  display:inline-block;padding:3px 11px;border-radius:20px;font-size:12px;
  font-weight:600;letter-spacing:.2px;
}
.badge-ok{background:#e6f4ec;color:var(--ok);}
.badge-warn{background:#fbf1dc;color:var(--warn);}
.badge-danger{background:#fbe9e7;color:var(--danger);}
.badge-neutral{background:var(--accent-soft);color:var(--accent);}
.section{margin:26px 0;}
.section h2{
  font-size:15.5px;color:#fff;background:var(--navy-2);
  padding:8px 14px;border-radius:6px 6px 0 0;margin:0;
}
.section .body{
  border:1px solid var(--line);border-top:none;border-radius:0 0 6px 6px;
  padding:16px 18px;background:#fff;
}
.section .body ul{margin:6px 0;padding-left:20px;}
.section .body li{margin:5px 0;}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
.kv-table{width:100%;border-collapse:collapse;font-size:13.5px;}
.kv-table th{
  text-align:left;width:150px;color:var(--muted);font-weight:600;
  padding:6px 10px;background:#fafbfc;border:1px solid var(--line);vertical-align:top;
}
.kv-table td{padding:6px 10px;border:1px solid var(--line);}
.quote-box{
  border-left:3px solid var(--accent);background:var(--accent-soft);
  padding:10px 14px;margin:8px 0;font-size:13.5px;border-radius:0 4px 4px 0;
}
.quote-box.unverified{border-left-color:var(--warn);background:#fbf1dc;}
.quote-tag{font-size:11px;color:var(--muted);margin-top:4px;}
.checklist li{list-style:none;margin:6px 0;padding-left:26px;position:relative;}
.checklist li:before{
  content:'\\2610'; position:absolute; left:0; color:var(--accent); font-size:15px;
}
.source-text{
  white-space:pre-wrap;font-family:'Consolas','Malgun Gothic',monospace;
  font-size:12.5px;line-height:1.7;max-height:520px;overflow:auto;
  background:#fbfbfc;border:1px solid var(--line);border-radius:6px;padding:14px;
}
.disclaimer{
  margin-top:34px;padding:14px 16px;border:1px dashed var(--danger);
  border-radius:6px;background:#fff7f6;font-size:12.5px;color:#7a2b26;line-height:1.7;
}
.footer{margin-top:22px;font-size:11.5px;color:var(--muted);text-align:center;}
.toc{margin:18px 0;padding:14px 18px;background:#fafbfc;border:1px solid var(--line);border-radius:6px;}
.toc a{color:var(--accent);text-decoration:none;font-size:13px;}
.toc li{margin:4px 0;}
@media print{
  body{background:#fff;}
  .page{box-shadow:none;padding:0;max-width:100%;}
  .no-print{display:none;}
  .section{page-break-inside:avoid;}
}
.print-btn{
  display:inline-block;margin-bottom:16px;padding:9px 18px;background:var(--accent);
  color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer;
}
"""

VERDICT_BADGE = {
    "적합": "badge-ok",
    "주의필요": "badge-warn",
    "부적합": "badge-danger",
    "판단불가": "badge-neutral",
    "케이스정보없음": "badge-neutral",
}


def _esc(text: Optional[str]) -> str:
    return html.escape(text or "", quote=True)


def _list_to_ul(items: Optional[List[str]], css_class: str = "") -> str:
    items = items or []
    if not items:
        return "<p style='color:var(--muted);margin:4px 0;'>원문에서 확인되지 않음</p>"
    lis = "".join(f"<li>{_esc(item)}</li>" for item in items)
    cls = f" class='{css_class}'" if css_class else ""
    return f"<ul{cls}>{lis}</ul>"


def _quotes_html(quotes: List[Dict[str, Any]]) -> str:
    if not quotes:
        return "<p style='color:var(--muted);'>추출된 근거 인용문이 없습니다.</p>"
    out = []
    for q in quotes:
        verified = q.get("verified_in_source", True)
        cls = "quote-box" if verified else "quote-box unverified"
        tag = "원문 대조 확인됨" if verified else "⚠ 원문에서 정확히 일치하는 문구를 찾지 못함 (반드시 원문 재확인)"
        context = q.get("context", "")
        out.append(
            f"<div class='{cls}'>\u201c{_esc(q.get('quote',''))}\u201d"
            f"<div class='quote-tag'>{_esc(context)} · {tag}</div></div>"
        )
    return "".join(out)


@dataclass
class ReportContext:
    hospital_name: str = ""
    author_name: str = ""
    case_title: str = ""
    case_info: Dict[str, str] = field(default_factory=dict)
    case_attachments: List[Dict[str, Any]] = field(default_factory=list)
    source_filenames: List[str] = field(default_factory=list)
    source_text: str = ""
    model_used: str = ""
    diagnosis: Dict[str, Any] = field(default_factory=dict)
    verified_quotes: List[Dict[str, Any]] = field(default_factory=list)
    generated_at: Optional[datetime] = None


_CASE_LABELS = {
    "department": "진료과",
    "primary_diagnosis": "주상병명 / 상병코드",
    "diagnosis_date": "진단일",
    "secondary_diagnosis": "부상병명 / 상병코드",
    "procedure_code": "시행 수가코드 / 명칭",
    "treatment_period": "시행 기간 / 최근 시행일",
    "treatment_count": "금회 청구 시행횟수",
    "cumulative_count": "누적(연간) 시행횟수",
    "prescriber": "처방의 / 전문과목",
    "claim_type": "청구 구분",
    "special_code": "특정기호",
    "prior_claim": "전월 동일항목 청구 여부",
    "memo": "특이사항 / 메모",
}


def _case_info_table(case_info: Dict[str, str]) -> str:
    rows = []
    for key, label in _CASE_LABELS.items():
        val = (case_info or {}).get(key, "").strip()
        if val:
            rows.append(f"<tr><th>{_esc(label)}</th><td>{_esc(val)}</td></tr>")
    if not rows:
        return "<p style='color:var(--muted);'>입력된 청구 케이스 정보가 없습니다 (급여기준 자체 요약만 수행됨).</p>"
    return f"<table class='kv-table'>{''.join(rows)}</table>"


def _attachments_html(attachments: List[Dict[str, Any]]) -> str:
    if not attachments:
        return ""
    items = "".join(
        f"<li>{_esc(a.get('filename',''))} <span style='color:var(--muted);'>({_esc(a.get('category',''))})</span></li>"
        for a in attachments
    )
    return f"<h3 style='font-size:13.5px;margin:14px 0 4px;'>첨부 자료</h3><ul>{items}</ul>"


def _single_case_section(ctx: ReportContext, anchor_id: str, include_source: bool) -> str:
    d = ctx.diagnosis or {}
    case_match = d.get("case_match", {}) or {}
    verdict = case_match.get("verdict", "판단불가")
    badge_cls = VERDICT_BADGE.get(verdict, "badge-neutral")
    generated_at = ctx.generated_at or datetime.now()

    quotes_html = _quotes_html(ctx.verified_quotes)
    source_block = ""
    if include_source and ctx.source_text:
        source_block = f"""
        <div class="section">
          <h2>참고 - 급여기준 원문 전체</h2>
          <div class="body"><div class="source-text">{_esc(ctx.source_text)}</div></div>
        </div>"""

    risk_html = (
        "<h3 style='font-size:13.5px;margin:14px 0 4px;'>위험 요소</h3>" + _list_to_ul(case_match.get("risk_points"))
        if case_match.get("risk_points") else ""
    )
    missing_html = (
        "<h3 style='font-size:13.5px;margin:14px 0 4px;'>판단에 추가로 필요한 정보</h3>" + _list_to_ul(case_match.get("missing_information"))
        if case_match.get("missing_information") else ""
    )
    comment_html = (
        f'<div class="section"><h2>10. AI 참고 코멘트</h2><div class="body"><p>{_esc(d.get("confidence_note",""))}</p></div></div>'
        if d.get("confidence_note") else ""
    )

    return f"""
    <div id="{anchor_id}">
    <div class="report-header">
      <div>
        <h1>{_esc(ctx.case_title or '청구심사 AI 진단 보고서')}</h1>
        <div class="org">{_esc(ctx.hospital_name)}</div>
      </div>
      <div class="meta">
        생성일시: {generated_at.strftime('%Y-%m-%d %H:%M')}<br/>
        작성자: {_esc(ctx.author_name) or '-'}<br/>
        참조 파일: {_esc(', '.join(ctx.source_filenames)) or '직접 입력'}
      </div>
    </div>

    <div class="section">
      <h2>1. 케이스 판정</h2>
      <div class="body">
        <span class="badge {badge_cls}">{_esc(verdict)}</span>
        <p style="margin-top:12px;">{_esc(case_match.get('reasoning',''))}</p>
        {risk_html}
        {missing_html}
      </div>
    </div>

    <div class="section">
      <h2>2. 청구 케이스 정보</h2>
      <div class="body">{_case_info_table(ctx.case_info)}{_attachments_html(ctx.case_attachments)}</div>
    </div>

    <div class="section">
      <h2>3. 급여기준 핵심 요약</h2>
      <div class="body">
        <p>{_esc(d.get('summary',''))}</p>
        <table class="kv-table" style="margin-top:10px;">
          <tr><th>관련 고시번호</th><td>{_esc(d.get('notice_reference','')) or '원문에서 확인되지 않음'}</td></tr>
          <tr><th>시행일자</th><td>{_esc(d.get('effective_date','')) or '원문에서 확인되지 않음'}</td></tr>
        </table>
      </div>
    </div>

    <div class="section">
      <h2>4. 핵심 인정 요건</h2>
      <div class="body">{_list_to_ul(d.get('key_criteria'))}</div>
    </div>

    <div class="grid-2">
      <div class="section" style="margin-top:0;">
        <h2>5. 인정 횟수·기간 제한</h2>
        <div class="body">{_list_to_ul(d.get('frequency_limits'))}</div>
      </div>
      <div class="section" style="margin-top:0;">
        <h2>6. 필수 서류·기록·자격 요건</h2>
        <div class="body">{_list_to_ul(d.get('required_documentation'))}</div>
      </div>
    </div>

    <div class="section">
      <h2>7. 제외 사유 및 주의사항 (삭감 위험)</h2>
      <div class="body">{_list_to_ul(d.get('exclusions_or_cautions'))}</div>
    </div>

    <div class="section">
      <h2>8. 청구 전 체크리스트</h2>
      <div class="body">{_list_to_ul(d.get('checklist'), css_class='checklist')}</div>
    </div>

    <div class="section">
      <h2>9. 원문 근거 인용</h2>
      <div class="body">{quotes_html}</div>
    </div>

    {comment_html}

    {source_block}
    </div>
    """


def generate_html_report(ctx: ReportContext, include_source: bool = True) -> str:
    """단일 심사 건에 대한 완결된 HTML 보고서 문자열을 생성한다."""
    body = _single_case_section(ctx, anchor_id="case-0", include_source=include_source)
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{_esc(ctx.case_title or '청구심사 AI 진단 보고서')}</title>
<style>{_BASE_CSS}</style>
</head>
<body>
<div class="page">
  <button class="print-btn no-print" onclick="window.print()">🖨 인쇄 / PDF로 저장</button>
  {body}
  <div class="disclaimer"><strong>⚠ 유의사항</strong><br/>{_esc(DISCLAIMER_TEXT)}</div>
  <div class="footer">청구심사 AI 가이드 &middot; 자동 생성 보고서 &middot; {(ctx.generated_at or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')}</div>
</div>
</body>
</html>"""


def generate_batch_html_report(contexts: List[ReportContext], batch_title: str = "청구심사 AI 진단 - 일괄 보고서") -> str:
    """여러 건의 심사 결과를 하나의 HTML 파일(목차 포함)로 묶어 생성한다."""
    toc_items = []
    sections = []
    for i, ctx in enumerate(contexts):
        anchor = f"case-{i}"
        title = ctx.case_title or f"케이스 {i+1}"
        toc_items.append(f"<li><a href='#{anchor}'>{i+1}. {_esc(title)}</a></li>")
        sections.append(_single_case_section(ctx, anchor_id=anchor, include_source=False))

    generated_at = datetime.now()
    toc_html = f"<div class='toc'><strong>목차</strong><ul>{''.join(toc_items)}</ul></div>"
    sections_html = "<hr style='margin:40px 0;border:none;border-top:2px solid var(--line);'/>".join(sections)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{_esc(batch_title)}</title>
<style>{_BASE_CSS}</style>
</head>
<body>
<div class="page">
  <button class="print-btn no-print" onclick="window.print()">🖨 인쇄 / PDF로 저장</button>
  <div class="report-header">
    <div><h1>{_esc(batch_title)}</h1></div>
    <div class="meta">생성일시: {generated_at.strftime('%Y-%m-%d %H:%M')}<br/>총 {len(contexts)}건</div>
  </div>
  {toc_html}
  {sections_html}
  <div class="disclaimer"><strong>⚠ 유의사항</strong><br/>{_esc(DISCLAIMER_TEXT)}</div>
  <div class="footer">청구심사 AI 가이드 &middot; 자동 생성 일괄 보고서 &middot; {generated_at.strftime('%Y-%m-%d %H:%M:%S')}</div>
</div>
</body>
</html>"""
