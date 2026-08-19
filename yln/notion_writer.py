"""Notion REST API client (plain `requests`, no official SDK)."""

from __future__ import annotations

import datetime
import time
from typing import Iterable, Iterator, List, Optional, Tuple

import requests

NOTION_VERSION = "2022-06-28"
API_BASE = "https://api.notion.com/v1"
MAX_BLOCKS_PER_REQUEST = 100
MAX_RICH_TEXT_LEN = 2000


class NotionError(Exception):
    """Raised on unrecoverable Notion API failures. Message includes the response body."""


def _chunk_text(text: str, size: int = MAX_RICH_TEXT_LEN) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i : i + size]


def _batched(items: List[dict], size: int = MAX_BLOCKS_PER_REQUEST) -> Iterator[List[dict]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class NotionWriter:
    def __init__(self, token: str):
        self.token = token

    def _headers(self, json_content: bool = True) -> dict:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
        }
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(self, method: str, url: str, headers: Optional[dict] = None, **kwargs) -> requests.Response:
        headers = headers if headers is not None else self._headers()
        last_error: Optional[NotionError] = None

        for attempt in range(3):
            try:
                resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
            except requests.RequestException as e:
                last_error = NotionError(f"Notion API 요청 실패: {e}")
                if attempt < 2:
                    time.sleep((2**attempt) * 2)
                    continue
                raise last_error

            if resp.status_code < 400:
                return resp

            retryable = resp.status_code == 429 or resp.status_code >= 500
            last_error = NotionError(f"Notion API 오류 {resp.status_code}: {resp.text}")
            if retryable and attempt < 2:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else (2**attempt) * 2
                time.sleep(wait)
                continue
            raise last_error

        assert last_error is not None
        raise last_error

    def create_lecture_page(self, parent_page_id: str, title: str) -> str:
        today = datetime.date.today().isoformat()
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": title}}]}
            },
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {"type": "text", "text": {"content": f"🎥 자동 생성된 강의 노트 · {today}"}}
                        ]
                    },
                }
            ],
        }
        resp = self._request("POST", f"{API_BASE}/pages", json=payload)
        return resp.json()["id"]

    def upload_image(self, jpeg_bytes: bytes, filename: str) -> str:
        create_payload = {"filename": filename, "content_type": "image/jpeg"}
        resp = self._request("POST", f"{API_BASE}/file_uploads", json=create_payload)
        file_upload_id = resp.json()["id"]

        send_headers = self._headers(json_content=False)
        files = {"file": (filename, jpeg_bytes, "image/jpeg")}
        self._request(
            "POST",
            f"{API_BASE}/file_uploads/{file_upload_id}/send",
            headers=send_headers,
            files=files,
        )
        return file_upload_id

    def append_section(
        self,
        page_id: str,
        slide_no: int,
        time_label: str,
        image_upload_ids: List[str],
        bullets: Iterable[str],
    ) -> None:
        children: List[dict] = [
            {
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [
                        {"type": "text", "text": {"content": f"📌 Slide {slide_no} · {time_label}"}}
                    ]
                },
            },
        ]
        for file_upload_id in image_upload_ids:
            children.append(
                {
                    "object": "block",
                    "type": "image",
                    "image": {"type": "file_upload", "file_upload": {"id": file_upload_id}},
                }
            )
        for bullet in bullets:
            children.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": bullet}}]
                    },
                }
            )

        for batch in _batched(children):
            self._request("PATCH", f"{API_BASE}/blocks/{page_id}/children", json={"children": batch})

    def append_images(self, page_id: str, image_upload_ids: List[str]) -> None:
        """Append standalone image blocks after the previous section (e.g. silent carry-over slides)."""
        children: List[dict] = [
            {
                "object": "block",
                "type": "image",
                "image": {"type": "file_upload", "file_upload": {"id": file_upload_id}},
            }
            for file_upload_id in image_upload_ids
        ]
        if not children:
            return

        for batch in _batched(children):
            self._request("PATCH", f"{API_BASE}/blocks/{page_id}/children", json={"children": batch})

    def create_transcript_subpage(
        self,
        page_id: str,
        title: str,
        entries: Iterable[Tuple[int, str, str]],
    ) -> str:
        """Create a child page under `page_id` with the full raw transcript of every slide.

        `entries` is an ordered iterable of (slide_no, hhmmss, transcript). Notion caps
        both page-create children and blocks-append children at 100, so the first batch
        is sent with the page-create call and the rest are appended afterwards.
        """
        children: List[dict] = []
        for slide_no, hhmmss, transcript in entries:
            children.append(
                {
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {
                        "rich_text": [{"type": "text", "text": {"content": f"Slide {slide_no} · {hhmmss}"}}]
                    },
                }
            )
            text = (transcript or "").strip()
            if not text:
                children.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": "(음성 없음)"}}]},
                    }
                )
            else:
                for chunk in _chunk_text(text):
                    children.append(
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
                        }
                    )

        first_batch = children[:MAX_BLOCKS_PER_REQUEST]
        remaining = children[MAX_BLOCKS_PER_REQUEST:]

        payload = {
            "parent": {"type": "page_id", "page_id": page_id},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": f"📜 전체 스크립트 — {title}"}}]}
            },
            "children": first_batch,
        }
        resp = self._request("POST", f"{API_BASE}/pages", json=payload)
        subpage_id = resp.json()["id"]

        for batch in _batched(remaining):
            self._request("PATCH", f"{API_BASE}/blocks/{subpage_id}/children", json={"children": batch})

        return subpage_id

    def append_final_summary(self, page_id: str, summary_text: str) -> None:
        children: List[dict] = [
            {"object": "block", "type": "divider", "divider": {}},
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": "📝 전체 요약"}}]},
            },
        ]

        for para in summary_text.split("\n\n"):
            para = para.strip()
            if not para:
                continue
            for chunk in _chunk_text(para):
                children.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
                    }
                )

        for batch in _batched(children):
            self._request("PATCH", f"{API_BASE}/blocks/{page_id}/children", json={"children": batch})
