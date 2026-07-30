"""
Outlook '한국모니터링' 폴더 메일 형식 확인용 스크립트.
결과를 UTF-8 파일로 저장합니다.
"""
import sys
import io
import os

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

try:
    import win32com.client
except ImportError:
    sys.exit("pywin32가 설치되지 않았습니다: pip install pywin32")

FOLDER_NAME   = "한국모니터링"
PREVIEW_COUNT = 3
OUTPUT_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outlook_sample.txt")


def get_body_text(mail) -> str:
    try:
        body = mail.Body or ""
    except Exception:
        body = ""
    # 제로폭 공백 등 정리
    for ch in ["​", "‌", "‍", "﻿"]:
        body = body.replace(ch, "")
    return body.replace("\r\n", "\n").replace("\r", "\n").strip()


def find_subfolder(parent, name):
    try:
        for f in parent.Folders:
            if f.Name == name:
                return f
    except Exception:
        pass
    return None


def log(msg):
    try:
        print(msg)
    except Exception:
        pass


def main():
    log("Outlook 연결 중...")
    outlook = win32com.client.Dispatch("Outlook.Application")
    ns      = outlook.GetNamespace("MAPI")

    target_folder = None
    account_name  = ""

    for store in ns.Stores:
        try:
            root = store.GetRootFolder()
            for top_folder in root.Folders:
                sub = find_subfolder(top_folder, FOLDER_NAME)
                if sub:
                    log(f"폴더 발견: {sub.Name} ({sub.Items.Count}개)")
                    if target_folder is None or sub.Items.Count > target_folder.Items.Count:
                        target_folder = sub
                        account_name  = store.DisplayName
        except Exception as e:
            log(f"계정 오류: {e}")

    if target_folder is None:
        log(f"'{FOLDER_NAME}' 폴더를 찾지 못했습니다.")
        return

    log(f"사용할 폴더: {FOLDER_NAME} ({target_folder.Items.Count}개)")

    items = target_folder.Items
    items.Sort("[ReceivedTime]", True)

    with io.open(OUTPUT_FILE, "w", encoding="utf-8", errors="replace") as out:
        out.write(f"폴더: {FOLDER_NAME}  총 {target_folder.Items.Count}개\n")
        out.write("=" * 70 + "\n\n")

        count = 0
        for mail in items:
            if count >= PREVIEW_COUNT:
                break
            try:
                subject  = mail.Subject or ""
                received = mail.ReceivedTime.strftime("%Y-%m-%d")
                sender   = mail.SenderName
                body     = get_body_text(mail)
                lines    = [l for l in body.split("\n") if l.strip()]

                out.write(f"[메일 {count+1}]\n")
                out.write(f"  날짜   : {received}\n")
                out.write(f"  발신자 : {sender}\n")
                out.write(f"  제목   : {subject}\n")
                out.write(f"  줄수   : {len(lines)}줄\n\n")
                for line in lines:
                    out.write(f"  {line}\n")
                out.write("\n" + "=" * 70 + "\n\n")

                log(f"[{count+1}] {received} {subject[:50]} → {len(lines)}줄 저장")
                count += 1
            except Exception as e:
                log(f"오류: {e}")

    log(f"\n완료: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
