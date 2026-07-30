# -*- coding: utf-8 -*-
"""Outlook 한국모니터링 폴더 매체별 기사 수 분석 — 결과를 source_analysis.txt 에 저장."""
import sys, io, re, os
from collections import Counter

try:
    import win32com.client
except ImportError:
    sys.exit("pip install pywin32")

FOLDER_NAME = "한국모니터링"
OUT_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "source_analysis.txt")


def parse_sources(body):
    body  = re.sub(r"[​‌‍﻿\t]", " ", body)
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    found = []
    for line in lines:
        if re.search(r"<https?://[^>]+>", line):
            m = re.search(r"\(([^\)]+?)\s+\d{4}/\d{2}/\d{2}\)", line)
            if m:
                found.append(m.group(1).strip())
    return found


def find_folder(ns, name):
    for store in ns.Stores:
        try:
            for top in store.GetRootFolder().Folders:
                for f in top.Folders:
                    if f.Name == name:
                        return f
        except Exception:
            pass
    return None


def main():
    print("Outlook 연결 중...")
    outlook = win32com.client.Dispatch("Outlook.Application")
    ns      = outlook.GetNamespace("MAPI")

    folder = find_folder(ns, FOLDER_NAME)
    if not folder:
        print(f"'{FOLDER_NAME}' 폴더를 찾지 못했습니다.")
        return

    total = folder.Items.Count
    print(f"폴더 확인: {FOLDER_NAME} ({total}개 메일) - 파싱 시작...")

    items = folder.Items
    items.Sort("[ReceivedTime]", True)

    counter    = Counter()
    mail_count = 0

    for i in range(1, total + 1):
        try:
            mail = items[i]
            if getattr(mail, "Class", 0) != 43:
                continue
            body = (mail.Body or "").replace("\r\n", "\n").replace("\r", "\n")
            counter.update(parse_sources(body))
            mail_count += 1
            if mail_count % 100 == 0:
                print(f"  {mail_count}/{total} 처리 완료...")
        except Exception:
            pass

    # 파일 저장
    with io.open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(f"매체별 기사 수 (총 {len(counter)}개 매체, {mail_count}개 메일 분석)\n")
        f.write("=" * 55 + "\n")
        for src, cnt in counter.most_common():
            f.write(f"{cnt:5d}건  {src}\n")

    print(f"\n완료! 결과 파일: {OUT_FILE}")
    print(f"총 매체: {len(counter)}개\n")
    print("상위 50개:")
    for src, cnt in counter.most_common(50):
        print(f"  {cnt:5d}건  {src}")


if __name__ == "__main__":
    main()
