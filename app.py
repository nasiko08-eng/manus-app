from __future__ import annotations

import io
import os
import re
from typing import Any
from urllib.parse import urlparse

import requests
import streamlit as st
from PIL import Image
from playwright.sync_api import sync_playwright
from weasyprint import HTML

MANUS_BASE_URL = "https://api.manus.ai"
NTA_BASE_URL = "https://api.houjin-bangou.nta.go.jp/4"


MANUS_SCHEMA = {
    "type": "object",
    "properties": {
        "official_homepage_url": {"type": ["string", "null"]},
        "instagram_url": {"type": ["string", "null"]},
        "menu_urls": {"type": "array", "items": {"type": "string"}},
        "menu_text": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["official_homepage_url", "instagram_url", "menu_urls", "menu_text", "notes"],
    "additionalProperties": False,
}


def manus_headers() -> dict[str, str]:
    key = os.environ.get("MANUS_API_KEY")
    if not key:
        raise RuntimeError("MANUS_API_KEY が設定されていません")
    return {"x-manus-api-key": key, "Content-Type": "application/json"}


def call_manus(store_name: str, address_hint: str = "") -> dict[str, Any]:
    prompt = f"""店舗名「{store_name}」について、公開情報だけを調査してください。所在地の手掛かりは「{address_hint}」です。

目的は社内確認用レポートの作成です。公式ホームページ、店舗公式Instagramの公開プロフィール、公式メニュー（PDF・画像・HTML）を優先してください。
ログイン、CAPTCHA回避、robots.txt違反、アクセス制限の回避、非公開情報の取得はしないでください。Instagramは公開URLのみを返してください。
各URLは実際に確認できたものだけ返し、推測で補完しないでください。メニューが見つからない場合は空配列・空文字にしてください。
"""
    payload = {
        "message": {"content": prompt},
        "agent_profile": "standard",
        "structured_output_schema": MANUS_SCHEMA,
    }
    r = requests.post(f"{MANUS_BASE_URL}/v2/task.create", headers=manus_headers(), json=payload, timeout=60)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok", True):
        raise RuntimeError(body)
    task_id = body.get("task_id") or body.get("task", {}).get("id")
    if not task_id:
        raise RuntimeError(f"Manus task_id が見つかりません: {body}")

    for _ in range(90):
        m = requests.get(
            f"{MANUS_BASE_URL}/v2/task.listMessages",
            headers={"x-manus-api-key": os.environ["MANUS_API_KEY"]},
            params={"task_id": task_id, "order": "asc"},
            timeout=30,
        )
        m.raise_for_status()
        messages = m.json().get("messages", [])
        for event in messages:
            if event.get("type") == "structured_output_result":
                result = event.get("structured_output_result", {})
                if not result.get("success"):
                    raise RuntimeError(result.get("error", "構造化出力に失敗しました"))
                return result["value"]
        import time
        time.sleep(2)
    raise TimeoutError("Manus APIの調査がタイムアウトしました")


def nta_search(company_name: str) -> list[dict[str, str]]:
    app_id = os.environ.get("NTA_APP_ID")
    if not app_id:
        return []
    params = {"id": app_id, "name": company_name, "type": "12", "cnt": "20", "mode": "2"}
    r = requests.get(f"{NTA_BASE_URL}/name", params=params, timeout=30)
    r.raise_for_status()
    # NTAのtype=12はCSV。仕様変更に備え、文字コードを順に試す。
    text = r.content.decode("utf-8-sig", errors="replace")
    rows = []
    for line in text.splitlines():
        cols = line.split(",")
        if len(cols) >= 3 and re.fullmatch(r"\d{13}", cols[0].strip('"')):
            rows.append({"corporate_number": cols[0].strip('"'), "name": cols[1].strip('"'), "address": cols[2].strip('"')})
    return rows


def safe_url(value: str | None) -> str | None:
    if not value:
        return None
    p = urlparse(value)
    if p.scheme not in {"http", "https"} or not p.netloc:
        return None
    return value


def screenshot_url(url: str, label: str) -> bytes | None:
    url = safe_url(url)
    if not url:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1500)
            data = page.screenshot(full_page=True, type="png")
            browser.close()
            return data
    except Exception as e:
        st.warning(f"{label}のスクリーンショットを取得できませんでした: {e}")
        return None


def build_pdf(store_name: str, corp_rows: list[dict[str, str]], research: dict[str, Any], images: list[tuple[str, bytes]]) -> bytes:
    def esc(s: Any) -> str:
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    corp_html = "".join(f"<tr><td>{esc(x['corporate_number'])}</td><td>{esc(x['name'])}</td><td>{esc(x['address'])}</td></tr>" for x in corp_rows)
    image_html = "".join(f"<h2>{esc(label)}</h2><img src='data:image/png;base64,{__import__('base64').b64encode(data).decode()}' />" for label, data in images)
    menu_urls = "".join(f"<li>{esc(u)}</li>" for u in research.get("menu_urls", []))
    html = f"""<!doctype html><html lang='ja'><head><meta charset='utf-8'><style>
    @page {{ size: A4; margin: 16mm; }} body {{ font-family: sans-serif; color:#222; }}
    h1 {{ font-size:24px; border-bottom:2px solid #333; padding-bottom:8px; }} h2 {{ page-break-before:always; font-size:18px; }}
    table {{ border-collapse:collapse; width:100%; font-size:10px; }} th,td {{ border:1px solid #999; padding:5px; vertical-align:top; }}
    th {{ background:#eee; }} img {{ max-width:100%; border:1px solid #ccc; }} .small {{ font-size:9px; color:#555; word-break:break-all; }}
    </style></head><body>
    <h1>{esc(store_name)} 店舗情報レポート</h1><p>作成日時: {__import__('datetime').datetime.now().isoformat(timespec='seconds')}</p>
    <h2 style='page-break-before:auto'>法人番号候補</h2><table><tr><th>法人番号</th><th>名称</th><th>所在地</th></tr>{corp_html or '<tr><td colspan="3">候補なし（NTA_APP_ID未設定または検索結果なし）</td></tr>'}</table>
    <h2>調査結果</h2><p>公式HP: {esc(research.get('official_homepage_url'))}</p><p>Instagram: {esc(research.get('instagram_url'))}</p><p>{esc(research.get('notes'))}</p>
    <h3>メニューURL</h3><ul>{menu_urls or '<li>見つかりませんでした</li>'}</ul><h3>メニュー概要</h3><pre style='white-space:pre-wrap'>{esc(research.get('menu_text'))}</pre>
    {image_html}
    <p class='small'>注意: 本レポートは公開ページの確認結果を整理したもので、法人の同一性・情報の最新性・権利処理を保証するものではありません。掲載元URLを必ず確認してください。</p>
    </body></html>"""
    return HTML(string=html).write_pdf()


st.set_page_config(page_title="店舗情報レポート", page_icon="📄", layout="centered")
st.title("店舗情報レポート作成")
st.caption("店舗名から公開情報を調査し、法人番号候補・公式HP・公開Instagram・メニュー情報をPDFにまとめます。")
store_name = st.text_input("店舗名", placeholder="例：〇〇食堂")
address_hint = st.text_input("所在地の手掛かり（任意）", placeholder="例：東京都渋谷区")

if st.button("調査してPDFを作成", type="primary", disabled=not store_name):
    with st.spinner("Manusで公開情報を調査しています…"):
        try:
            research = call_manus(store_name, address_hint)
            corp_rows = nta_search(store_name)
            images: list[tuple[str, bytes]] = []
            for label, url in [("公式ホームページ", research.get("official_homepage_url")), ("公開Instagram", research.get("instagram_url"))]:
                shot = screenshot_url(url, label) if url else None
                if shot:
                    images.append((label, shot))
            pdf = build_pdf(store_name, corp_rows, research, images)
            st.success("PDFを作成しました。")
            st.json({"corporate_candidates": corp_rows, "research": research})
            st.download_button("PDFをダウンロード", data=pdf, file_name=f"{store_name}_report.pdf", mime="application/pdf", type="primary")
        except Exception as e:
            st.error(f"処理に失敗しました: {e}")

st.divider()
st.caption("利用前に、対象サイトの利用規約・robots.txt・著作権・個人情報の扱いを確認してください。ログインが必要なページや非公開情報は取得しません。")
