import os
import time
from typing import Any
from urllib.parse import urlparse

import requests
import streamlit as st

MANUS_API_BASE = "https://api.manus.ai"

# Manusの抽出結果をアプリで扱うための最小スキーマです。
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "store_name": {"type": "string"},
        "corporate_number": {"type": ["string", "null"]},
        "official_homepage_url": {"type": ["string", "null"]},
        "instagram_url": {"type": ["string", "null"]},
        "menu_summary": {"type": "string"},
        "pdf_url": {"type": ["string", "null"]},
        "notes": {"type": "string"},
    },
    "required": [
        "store_name",
        "corporate_number",
        "official_homepage_url",
        "instagram_url",
        "menu_summary",
        "pdf_url",
        "notes",
    ],
    "additionalProperties": False,
}


def api_key() -> str:
    key = os.environ.get("MANUS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("MANUS_API_KEY が未設定です。Streamlit CloudのSecretsに設定してください。")
    return key


def headers() -> dict[str, str]:
    return {
        "x-manus-api-key": api_key(),
        "Content-Type": "application/json",
    }


def create_task(store_name: str, address_hint: str) -> str:
    instruction = f"""
店舗名「{store_name}」について、公開情報だけを調査し、最終的にPDFファイルを1つ作成してください。
所在地の手掛かりは「{address_hint or 'なし'}」です。

調査対象:
1. 国税庁法人番号公表サイト等の信頼できる公開情報から、該当する法人番号を調べる。同名候補が複数ある場合は候補と不確実性を明記する。
2. 店舗の公式ホームページを探す。
3. 店舗公式の公開Instagramを探す。ログイン、非公開情報、CAPTCHA回避、アクセス制限の回避はしない。
4. 公式メニュー表を探し、見つかった内容を要約する。
5. 公式ホームページと公開Instagramの画面を、可能な範囲でスクリーンショットとしてPDFに含める。

PDF要件:
- 日本語の見出しを付ける。
- 店舗名、調査日時、法人番号または候補、公式HP URL、Instagram URL、メニュー概要、各情報の出典URLを含める。
- 「公開情報に基づく参考資料であり、情報の正確性・最新性・法人同一性を保証しない」旨を記載する。
- PDF生成後、ダウンロード可能な直接URLを最終回答に明記する。

重要:
- 推測したURLや情報は書かず、確認できた公開情報だけを使う。
- ログインが必要なページや非公開ページは取得しない。
- PDFを作成できない場合は、理由を明記し、pdf_urlはnullにする。
"""
    payload = {
        "message": {"content": instruction},
        "agent_profile": "standard",
        "structured_output_schema": RESULT_SCHEMA,
    }
    response = requests.post(
        f"{MANUS_API_BASE}/v2/task.create",
        headers=headers(),
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("ok") is False:
        raise RuntimeError(body.get("error", body))
    task_id = body.get("task_id") or body.get("task", {}).get("id")
    if not task_id:
        raise RuntimeError(f"Manusのtask_idを取得できませんでした: {body}")
    return task_id


def find_structured_result(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in messages:
        if event.get("type") != "structured_output_result":
            continue
        result = event.get("structured_output_result", {})
        if result.get("success") is True:
            return result.get("value", {})
        if result.get("success") is False:
            raise RuntimeError(result.get("error", "Manusの構造化出力に失敗しました"))
    return None


def poll_task(task_id: str, max_seconds: int = 900) -> dict[str, Any]:
    deadline = time.time() + max_seconds
    last_status = ""
    while time.time() < deadline:
        response = requests.get(
            f"{MANUS_API_BASE}/v2/task.listMessages",
            headers={"x-manus-api-key": api_key()},
            params={"task_id": task_id, "order": "asc"},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        messages = body.get("messages", [])

        for event in messages:
            if event.get("type") == "status_update":
                status = event.get("status_update", {}).get("agent_status", "")
                if status and status != last_status:
                    last_status = status
                    st.info(f"Manusの処理状態: {status}")
                if status == "error":
                    raise RuntimeError(event.get("status_update", {}).get("status_detail", "Manusタスクが失敗しました"))
                if status == "waiting":
                    detail = event.get("status_update", {}).get("status_detail", {})
                    raise RuntimeError("Manusが追加操作を待機しています: " + str(detail.get("waiting_description", detail)))

        result = find_structured_result(messages)
        if result is not None:
            return result
        time.sleep(3)
    raise TimeoutError("Manusの処理がタイムアウトしました。タスクはManus側で継続している可能性があります。")


def download_pdf(pdf_url: str) -> bytes:
    parsed = urlparse(pdf_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Manusが返したPDF URLが不正です")
    response = requests.get(pdf_url, timeout=120)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "pdf" not in content_type.lower() and not response.content.startswith(b"%PDF"):
        raise ValueError("指定URLのレスポンスがPDFではありません")
    return response.content


st.set_page_config(page_title="店舗情報PDF作成", page_icon="📄")
st.title("店舗情報PDF作成")
st.write("店舗名をManusに渡し、調査・スクリーンショット・PDF作成をManus側で実行します。")

store_name = st.text_input("店舗名", placeholder="例：〇〇食堂")
address_hint = st.text_input("所在地の手掛かり（任意）", placeholder="例：東京都渋谷区")

if st.button("Manusで調査してPDFを作成", type="primary", disabled=not store_name.strip()):
    try:
        with st.status("Manusへ依頼しています…", expanded=True) as status:
            task_id = create_task(store_name.strip(), address_hint.strip())
            st.write(f"タスクを作成しました: `{task_id}`")
            result = poll_task(task_id)
            status.update(label="調査とPDF作成が完了しました", state="complete")

        st.subheader("調査結果")
        st.json(result)
        pdf_url = result.get("pdf_url")
        if not pdf_url:
            st.error("ManusからPDF URLが返りませんでした。Manusの最終回答を確認してください。")
        else:
            pdf_bytes = download_pdf(pdf_url)
            st.download_button(
                "完成したPDFをダウンロード",
                data=pdf_bytes,
                file_name=f"{store_name.strip()}_report.pdf",
                mime="application/pdf",
                type="primary",
            )
    except Exception as error:
        st.error(str(error))

st.caption("公開情報のみを対象にしてください。サイトの利用規約、著作権、肖像権、個人情報の扱いを確認してください。")
