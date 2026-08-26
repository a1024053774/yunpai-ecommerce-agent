from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .schemas import ChatImageInput
from .storage_refs import validate_controlled_storage_ref

if TYPE_CHECKING:
    from .database import Database


MESSAGE_MEDIA_KIND = "customer_image"
MEDIA_VISION_DESCRIPTION_MAX_CHARS = 2000
_MEDIA_ID = re.compile(r"^media-[a-f0-9]{24}$")
_MIME_SUFFIX = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_HISTORY_MEDIA_NOTE_PREFIX = "[图片观察｜多模态模型转录，非顾客原话，未经业务核验]"


def parse_message_media(value: str | list[Any] | None) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except ValueError:
            return []
    else:
        parsed = value
    if not isinstance(parsed, list):
        return []

    media: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict) or item.get("kind") != MESSAGE_MEDIA_KIND:
            continue
        media_id = item.get("id")
        mime_type = item.get("mime_type")
        storage_ref = item.get("storage_ref")
        size_bytes = item.get("size_bytes")
        if (
            not isinstance(media_id, str)
            or not _MEDIA_ID.fullmatch(media_id)
            or not isinstance(mime_type, str)
            or mime_type not in _MIME_SUFFIX
            or not isinstance(storage_ref, str)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 1
        ):
            continue
        try:
            validate_controlled_storage_ref(
                storage_ref,
                required_subpath="chat-media",
            )
        except ValueError:
            continue
        entry: dict[str, Any] = {
            "kind": MESSAGE_MEDIA_KIND,
            "id": media_id,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "storage_ref": storage_ref,
        }
        description = item.get("vision_description")
        if isinstance(description, str) and description.strip():
            entry["vision_description"] = description.strip()[
                :MEDIA_VISION_DESCRIPTION_MAX_CHARS
            ]
        media.append(entry)
    return media


def attach_vision_description(
    media: list[dict[str, Any]],
    description: str,
) -> None:
    """Record the redacted multimodal observation on persisted media metadata."""

    text = description.strip()[:MEDIA_VISION_DESCRIPTION_MAX_CHARS]
    if not text:
        return
    for item in media:
        if isinstance(item, dict) and item.get("kind") == MESSAGE_MEDIA_KIND:
            item["vision_description"] = text


def annotate_history_content(
    content: str,
    sources_json: str | list[Any] | None,
) -> str:
    """Append persisted image observations to model-facing history content.

    观察文本只是多模态模型的非权威转录，标记后并入历史，供后续轮次的模型
    理解此前图片内容；不改变消息正文本身的存储。
    """

    notes = [
        str(item["vision_description"])
        for item in parse_message_media(sources_json)
        if item.get("vision_description")
    ]
    if not notes:
        return content
    note_text = "；".join(notes)
    return f"{content}\n{_HISTORY_MEDIA_NOTE_PREFIX} {note_text}"


def non_media_sources(value: str | list[Any] | None) -> list[Any]:
    """Return citation sources with internal media metadata stripped."""

    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except ValueError:
            return []
    else:
        parsed = value
    if not isinstance(parsed, list):
        return []
    return [
        source
        for source in parsed
        if not (isinstance(source, dict) and source.get("kind") == MESSAGE_MEDIA_KIND)
    ]


def public_message_media(
    value: str | list[Any] | None,
    *,
    url_prefix: str,
) -> list[dict[str, Any]]:
    prefix = url_prefix.rstrip("/")
    return [
        {
            "id": item["id"],
            "mime_type": item["mime_type"],
            "size_bytes": item["size_bytes"],
            "url": f"{prefix}/{quote(item['id'], safe='')}",
        }
        for item in parse_message_media(value)
    ]


class MessageMediaStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.root = (self.data_dir / "objects" / "chat-media").resolve()

    def persist(self, message_id: str, image: ChatImageInput) -> dict[str, Any]:
        payload = image.decoded_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        media_id = f"media-{digest[:24]}"
        message_directory = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
        suffix = _MIME_SUFFIX[image.mime_type]
        storage_ref = (
            f"objects/chat-media/{message_directory}/{digest}.{suffix}"
        )
        target = self._resolve(storage_ref)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.exists():
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as handle:
                    os.chmod(temporary, 0o600)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "kind": MESSAGE_MEDIA_KIND,
            "id": media_id,
            "mime_type": image.mime_type,
            "size_bytes": len(payload),
            "storage_ref": storage_ref,
        }

    def resolve_for_message(
        self,
        db: Database,
        *,
        tenant_id: str,
        session_id: str,
        message_id: str,
        media_id: str,
        subject_hash: str | None = None,
    ) -> tuple[Path, str] | None:
        conditions = [
            "m.id=?",
            "m.session_id=?",
            "m.tenant_id=?",
            "m.role='user'",
        ]
        params: list[Any] = [message_id, session_id, tenant_id]
        if subject_hash is not None:
            conditions.append("s.subject_hash=?")
            params.append(subject_hash)
        with db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT m.sources_json
                FROM messages m JOIN sessions s ON s.id=m.session_id
                WHERE {' AND '.join(conditions)}
                """,
                tuple(params),
            ).fetchone()
        if row is None:
            return None
        attachment = next(
            (
                item
                for item in parse_message_media(row["sources_json"])
                if item["id"] == media_id
            ),
            None,
        )
        if attachment is None:
            return None
        path = self._resolve(str(attachment["storage_ref"]))
        if not path.is_file():
            return None
        return path, str(attachment["mime_type"])

    def remove(self, value: str | list[Any] | None) -> int:
        removed = 0
        for item in parse_message_media(value):
            path = self._resolve(str(item["storage_ref"]))
            if path.is_file():
                path.unlink()
                removed += 1
            try:
                path.parent.rmdir()
            except OSError:
                pass
        return removed

    def _resolve(self, storage_ref: str) -> Path:
        validate_controlled_storage_ref(
            storage_ref,
            required_subpath="chat-media",
        )
        path = (self.data_dir / Path(storage_ref)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("storage_ref_not_approved")
        return path
