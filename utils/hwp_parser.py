# -*- coding: utf-8 -*-
"""
hwp_parser.py
==============
한글(.hwp, HWP 5.0 OLE 포맷) 파일에서 순수 텍스트를 추출하는 저수준 파서.

배경
----
심평원(HIRA)에서 배포하는 급여기준/고시 파일 중 상당수는 표준 pyhwp 라이브러리가
요구하는 '\\005HwpSummaryInformation' 스트림이 누락되어 있어 pyhwp(hwp5txt)로
바로 열리지 않는 경우가 실무에서 자주 발견된다. 이 모듈은 그런 파일도 안정적으로
처리하기 위해 OLE 컴파운드 파일 구조를 직접 순회하며 BodyText 섹션의
PARA_TEXT(문단 텍스트) 레코드만 뽑아 UTF-16LE로 디코딩한다.

주의: 이 파서는 "레코드 기반 텍스트 추출"에 특화되어 있으며 표/그림/각주 등
복잡한 서식 구조는 재현하지 않는다. 급여기준 문서와 같이 텍스트 위주의 공문서에는
충분하지만, 추출 결과는 반드시 화면에서 검수 후 사용해야 한다 (앱 내 검수 단계 참고).
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import List, Tuple

import olefile

HWPTAG_PARA_TEXT = 67  # HWPTAG_BEGIN(0x10=16) + 51 = 67  (문단 텍스트 레코드)

# 문단 텍스트 안에서 만나는 제어문자 중, 문자 코드 뒤에 '추가 데이터 7워드'가
# 뒤따르는 인라인 컨트롤(표/그림/필드/글자겹침 등)의 코드 목록.
# 그 외 제어문자(줄바꿈 등)는 추가 데이터 없이 1워드로 처리한다.
_EXTENDED_CONTROL_CODES = {
    0, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
}


@dataclass
class HwpParagraph:
    section: str
    order: int
    text: str


class HwpParseError(Exception):
    pass


def _read_stream(ole: "olefile.OleFileIO", path) -> bytes:
    try:
        return ole.openstream(path).read()
    except Exception as exc:  # noqa: BLE001
        raise HwpParseError(f"스트림을 읽을 수 없습니다: {path} ({exc})") from exc


def _is_compressed(ole: "olefile.OleFileIO") -> bool:
    """FileHeader 스트림의 속성 플래그 비트0(압축 여부)를 확인한다."""
    header = _read_stream(ole, "FileHeader")
    if len(header) < 40:
        return True  # 정보가 없으면 안전하게 '압축됨'으로 가정 후 시도/폴백
    flags = struct.unpack("<I", header[36:40])[0]
    return bool(flags & 0x1)


def _decompress_section(raw: bytes) -> bytes:
    """BodyText/SectionN 스트림 압축 해제. Raw DEFLATE(zlib, wbits=-15) 사용."""
    try:
        return zlib.decompress(raw, -15)
    except zlib.error:
        # 일부 파일은 표준 zlib 헤더를 포함하는 경우가 있어 재시도
        try:
            return zlib.decompress(raw)
        except zlib.error as exc:
            raise HwpParseError(f"BodyText 압축 해제 실패: {exc}") from exc


def _iter_records(data: bytes) -> List[Tuple[int, bytes]]:
    """레코드 스트림을 (tag, payload) 리스트로 분해한다."""
    records: List[Tuple[int, bytes]] = []
    i, n = 0, len(data)
    while i + 4 <= n:
        header = struct.unpack("<I", data[i : i + 4])[0]
        tag = header & 0x3FF
        size = (header >> 20) & 0xFFF
        i += 4
        if size == 0xFFF:  # 확장 크기 필드 사용
            if i + 4 > n:
                break
            size = struct.unpack("<I", data[i : i + 4])[0]
            i += 4
        if i + size > n:
            break
        records.append((tag, data[i : i + size]))
        i += size
    return records


def _extract_para_text(payload: bytes) -> str:
    """PARA_TEXT 레코드 payload(UTF-16LE)에서 사람이 읽는 텍스트만 뽑는다."""
    try:
        s = payload.decode("utf-16le", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""

    out: List[str] = []
    i, n = 0, len(s)
    while i < n:
        code = ord(s[i])
        if code in (10, 13):  # 줄바꿈류
            out.append("\n")
            i += 1
        elif code == 9:  # 탭
            out.append("\t")
            i += 1
        elif code < 32:
            # 제어문자: 확장 컨트롤이면 총 8워드(문자 1 + 부가 7) 소비
            i += 8 if code in _EXTENDED_CONTROL_CODES else 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def parse_hwp(file_path: str) -> List[HwpParagraph]:
    """HWP 파일을 열어 섹션별 문단 텍스트 리스트를 반환한다."""
    if not olefile.isOleFile(file_path):
        raise HwpParseError("올바른 HWP(OLE 컴파운드) 파일이 아닙니다.")

    ole = olefile.OleFileIO(file_path)
    try:
        compressed = _is_compressed(ole)

        # BodyText 하위 Section 스트림 목록을 자연 순서(0,1,2...)로 정렬
        section_entries = []
        for entry in ole.listdir():
            if len(entry) == 2 and entry[0] == "BodyText" and entry[1].startswith("Section"):
                try:
                    idx = int(entry[1].replace("Section", ""))
                except ValueError:
                    idx = 0
                section_entries.append((idx, entry))
        section_entries.sort(key=lambda x: x[0])

        if not section_entries:
            raise HwpParseError("BodyText 섹션을 찾을 수 없습니다.")

        paragraphs: List[HwpParagraph] = []
        for idx, entry in section_entries:
            raw = _read_stream(ole, entry)
            data = _decompress_section(raw) if compressed else raw
            records = _iter_records(data)
            order = 0
            for tag, payload in records:
                if tag != HWPTAG_PARA_TEXT:
                    continue
                text = _extract_para_text(payload).strip()
                if text:
                    order += 1
                    paragraphs.append(HwpParagraph(section=f"Section{idx}", order=order, text=text))
        return paragraphs
    finally:
        ole.close()


def hwp_to_text(file_path: str) -> str:
    """HWP 파일 전체를 하나의 텍스트(문단을 개행으로 연결)로 반환한다."""
    paragraphs = parse_hwp(file_path)
    return "\n".join(p.text for p in paragraphs)
