"""A local PDF reader with an optional Markdown study library.

Run: python server.py --port 8765 (add --study for question bank mode)
The desktop server binds to loopback only. An explicitly selected private
IPv4 listener can be started separately with a pairing-token file. Both modes
share the Markdown records under ``我的复习记录`` and their version/write locks.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import ipaddress
import json
import mimetypes
import os
import re
import stat
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit


APP_VERSION = "3"
MAX_JSON = 2 * 1024 * 1024
MAX_BACKUP = 512 * 1024 * 1024
MAX_IMAGE = 10 * 1024 * 1024
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
RECORDS_DIR = "我的复习记录"
CONFLICTS_DIR = "恢复冲突"
MANIFEST = "__review_hub_backup__.json"
BACKUP_DIRS = {"content", "web", "vendor", RECORDS_DIR, CONFLICTS_DIR}
BACKUP_ROOT_EXTENSIONS = {".py", ".pyw", ".vbs", ".ps1", ".cmd", ".bat", ".md", ".txt", ".json"}
REQUIRED_FIELDS = {"复习中心格式", "题目ID", "学科ID", "题目", "学科", "还不会", "更新时间"}
OPTIONAL_FIELDS = {"本轮已复习"}
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def private_ipv4(value: str) -> ipaddress.IPv4Address:
    """Accept RFC 1918 only; ipaddress.is_private also includes other ranges."""
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise ValueError("手机访问地址必须是本机局域网 IPv4 地址。") from exc
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError("手机访问只支持 10.x、172.16–31.x 或 192.168.x 私有地址。")
    return address


class MobileAccess:
    """An explicit LAN listener, with a separate secret from the desktop app."""
    def __init__(self, host: str, token: str, root: Path, network: str | None = None):
        self.host = str(private_ipv4(host))
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise ValueError("配对口令需要至少 32 个 URL-safe 字符，请用 secrets.token_urlsafe(32) 生成。")
        self.token = token
        self.network = None
        if network is not None:
            try:
                self.network = ipaddress.IPv4Network(network, strict=False)
            except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, TypeError) as exc:
                raise ValueError("手机访问网段格式不正确。") from exc
            if (ipaddress.IPv4Address(self.host) not in self.network or
                    not any(self.network.subnet_of(item) for item in PRIVATE_NETWORKS)):
                raise ValueError("网段必须包含本机地址，并完整位于一个 RFC 1918 私有网段内。")
        # A stable cookie survives a local service restart, but not a token reset.
        self.session = hmac.new(token.encode("ascii"),
                                ("review-hub-mobile-v1:" + str(root)).encode("utf-8"),
                                hashlib.sha256).hexdigest()

    def allows_peer(self, peer: str) -> bool:
        try:
            address = private_ipv4(peer)
            return self.network is None or address in self.network
        except ValueError:
            return False


class AppError(Exception):
    def __init__(self, status: int, error: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"error": error, "message": message, **extra}


def require_id(value: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise AppError(400, "invalid_id", "无效的学科或题目 ID。")
    return value


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def yaml_string(value: str) -> str:
    # JSON's string syntax is a YAML subset and safely escapes multiline titles.
    return json.dumps(value, ensure_ascii=False)


def parse_scalar(value: str):
    value = value.strip()
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("属性的双引号或转义格式不正确") from exc
        if not isinstance(parsed, str):
            raise ValueError("需要文字属性")
        return parsed
    if value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise ValueError("属性的单引号不完整")
        return value[1:-1].replace("''", "'")
    if value in ("true", "false"):
        return value == "true"
    if value == "1":
        return 1
    if not value or value[0] in "[{>|&*!%@`" or " #" in value:
        raise ValueError("不支持此属性格式，请保留简单的文字或 true/false 属性")
    return value


def parse_record(data: bytes, subject_id: str, question_id: str):
    """Read our flat YAML fields, preserving all other header lines verbatim."""
    try:
        text = data.decode("utf-8-sig").replace("\r\n", "\n")
        if not text.startswith("---\n"):
            raise ValueError("缺少 YAML 属性开头 ---")
        end = text.find("\n---\n", 3)
        if end == -1:
            # A closing delimiter at EOF is valid for an empty note.
            if text.endswith("\n---"):
                end = len(text) - 4
                body = ""
            else:
                raise ValueError("缺少 YAML 属性结束 ---")
        else:
            body = text[end + 5:]
        header = text[4:end].split("\n")
        values = {}
        positions = {}
        for position, line in enumerate(header):
            if not line or line.lstrip().startswith("#") or line.startswith((" ", "\t")):
                continue
            match = re.match(r"([^:]+):[ \t]*(.*)\Z", line)
            if not match:
                raise ValueError("YAML 属性行格式不正确")
            key, value = match.groups()
            if key in REQUIRED_FIELDS | OPTIONAL_FIELDS:
                if key in values:
                    raise ValueError(f"属性 {key} 重复")
                values[key] = parse_scalar(value)
                positions[key] = position
        if not REQUIRED_FIELDS.issubset(values):
            raise ValueError("缺少必需属性：" + "、".join(sorted(REQUIRED_FIELDS - values.keys())))
        if values["复习中心格式"] != 1:
            raise ValueError("不支持此记录格式版本")
        if values["题目ID"] != question_id or values["学科ID"] != subject_id:
            raise ValueError("记录内部的题目ID或学科ID与文件位置不符")
        if not isinstance(values["还不会"], bool):
            raise ValueError("还不会 属性必须是 true 或 false")
        if "本轮已复习" in values and not isinstance(values["本轮已复习"], bool):
            raise ValueError("本轮已复习 属性必须是 true 或 false")
        if not all(isinstance(values[key], str) for key in ("题目", "学科", "更新时间")):
            raise ValueError("题目、学科和更新时间必须是文字")
        record = {"needs_review": values["还不会"], "reviewed": values.get("本轮已复习", False), "notes": body,
                  "version": digest(data), "updated_at": values["更新时间"]}
        return record, header, positions
    except (UnicodeError, ValueError) as exc:
        raise AppError(422, "invalid_record", f"记录文件格式无法确认，已保留原文件：{exc}") from exc


def default_record():
    return {"needs_review": False, "reviewed": False, "notes": "", "version": "", "updated_at": ""}


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".review-tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        # Windows does not generally permit opening a directory this way.
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


class ReviewStore:
    def __init__(self, app_root: str | Path):
        self.root = Path(app_root).resolve()
        self.lock = threading.RLock()

    @contextmanager
    def _write_lock(self):
        """Serialize cooperating server processes as well as request threads."""
        with self.lock:
            lock_path = self._safe(".runtime/write.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as stream:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if time.monotonic() >= deadline:
                            raise AppError(503, "busy", "其他保存操作仍在进行，请保留笔记后重试。") from exc
                        time.sleep(0.025)
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _safe(self, relative: str) -> Path:
        if "\\" in relative or "\x00" in relative:
            raise AppError(400, "invalid_path", "无效的文件路径。")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or any(":" in part for part in pure.parts):
            raise AppError(400, "invalid_path", "文件路径不能离开复习中心。")
        if any(part.endswith((" ", ".")) or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part) for part in pure.parts):
            raise AppError(400, "invalid_path", "文件路径含有 Windows 保留名称。")
        path = self.root.joinpath(*pure.parts)
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise AppError(400, "invalid_path", "文件路径不能离开复习中心。") from exc
        # Do not follow symlinks or junctions, even if they happen to point inside.
        for part in [path, *path.parents]:
            if part == self.root:
                break
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise AppError(400, "invalid_path", "不支持符号链接或目录联接。")
        return path

    def book(self, subject_id: str):
        require_id(subject_id)
        path = self._safe(f"content/{subject_id}/book.json")
        if not path.is_file():
            raise AppError(404, "unknown_subject", "找不到此学科题库。")
        try:
            book = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(book, dict) or book.get("id") != subject_id:
                raise ValueError("题库 ID 与目录不匹配")
            if not isinstance(book.get("title"), str) or not isinstance(book.get("questions"), list):
                raise ValueError("缺少题库标题或 questions 数组")
            seen = set()
            for question in book["questions"]:
                if not isinstance(question, dict):
                    raise ValueError("题目必须是对象")
                question_id = require_id(question.get("id"))
                if question_id in seen:
                    raise ValueError("题目 ID 重复")
                seen.add(question_id)
                if not isinstance(question.get("title"), str):
                    raise ValueError("缺少题目标题")
            return book
        except (ValueError, UnicodeError, AppError) as exc:
            raise AppError(422, "invalid_book", f"题库文件无法读取：{exc}") from exc

    def _record_path(self, subject_id: str, question_id: str, question_title: str) -> Path:
        directory = self._safe(f"{RECORDS_DIR}/{subject_id}")
        candidates = []
        if directory.is_dir():
            for path in directory.iterdir():
                if path.name == f"{question_id}.md" or (path.name.startswith(question_id + "--") and path.suffix.lower() == ".md"):
                    candidates.append(self._safe(path.relative_to(self.root).as_posix()))
        if len(candidates) > 1:
            raise AppError(422, "duplicate_record", f"题目 {question_id} 有多份记录，已保留全部文件，请先合并。")
        if candidates:
            if not candidates[0].is_file():
                raise AppError(422, "invalid_record", "记录路径不是文件。")
            return candidates[0]
        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", question_title).strip(" .")[:60] or "题目"
        return self._safe(f"{RECORDS_DIR}/{subject_id}/{question_id}--{safe_title}.md")

    def _question(self, subject_id: str, question_id: str):
        require_id(question_id)
        book = self.book(subject_id)
        question = next((q for q in book["questions"] if q["id"] == question_id), None)
        if question is None:
            raise AppError(404, "unknown_question", "找不到此题目，已有记录不会被删除。")
        return book, question

    def media_path(self, subject_id: str, question_id: str, filename: str) -> Path:
        require_id(subject_id)
        require_id(question_id)
        if not re.fullmatch(r"[0-9a-f]{64}\.png", filename):
            raise AppError(400, "invalid_image_name", "图片文件名无效。")
        return self._safe(f"{RECORDS_DIR}/{subject_id}/attachments/{question_id}/{filename}")

    def upload_image(self, subject_id: str, question_id: str, data: bytes):
        """Store clipboard PNG separately from portable Markdown notes."""
        self._question(subject_id, question_id)
        if not data or len(data) > MAX_IMAGE:
            raise AppError(413, "image_too_large", "单张图片请控制在10MB以内。")
        import struct
        import zlib
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AppError(415, "invalid_image", "需要有效的PNG图片。")
        position = 8
        width = height = 0
        has_pixels = ended = False
        while position + 12 <= len(data):
            length = struct.unpack_from(">I", data, position)[0]
            kind = data[position + 4:position + 8]
            end = position + 12 + length
            if end > len(data):
                raise AppError(415, "invalid_image", "图片数据不完整。")
            payload = data[position + 8:position + 8 + length]
            expected_crc = struct.unpack_from(">I", data, position + 8 + length)[0]
            if zlib.crc32(kind + payload) & 0xffffffff != expected_crc:
                raise AppError(415, "invalid_image", "图片校验失败，请重新粘贴。")
            if position == 8:
                if kind != b"IHDR" or length != 13:
                    raise AppError(415, "invalid_image", "图片头无效。")
                width, height = struct.unpack_from(">II", payload)
                if not 0 < width <= 16000 or not 0 < height <= 16000 or width * height > 40_000_000:
                    raise AppError(413, "image_dimensions", "图片尺寸过大，请截取需要的部分。")
            if kind == b"IDAT":
                has_pixels = True
            if kind == b"IEND":
                ended = length == 0 and end == len(data)
                break
            position = end
        if not ended or not has_pixels:
            raise AppError(415, "invalid_image", "图片数据不完整。")
        filename = digest(data) + ".png"
        path = self.media_path(subject_id, question_id, filename)
        with self._write_lock():
            if not path.exists():
                atomic_write(path, data)
        return {"url": f"/media/{subject_id}/{question_id}/{filename}",
                "markdown": f"![截图](attachments/{question_id}/{filename})",
                "filename": filename, "width": width, "height": height, "bytes": len(data)}

    def state(self, subject_id: str):
        with self.lock:
            book = self.book(subject_id)
            records = {}
            record_errors = []
            for question in book["questions"]:
                path = None
                try:
                    path = self._record_path(subject_id, question["id"], question["title"])
                    records[question["id"]] = (parse_record(path.read_bytes(), subject_id, question["id"])[0]
                                               if path.exists() else default_record())
                except (AppError, OSError) as exc:
                    # A damaged note must not hide the book or the other notes.
                    # PATCH still performs its own strict read and rejects it.
                    records[question["id"]] = default_record()
                    record_errors.append({"question_id": question["id"],
                                          "path": path.relative_to(self.root).as_posix() if path else f"{RECORDS_DIR}/{subject_id}/{question['id']}--*.md",
                                          "message": str(exc),
                                          "error": exc.payload["error"] if isinstance(exc, AppError) else "filesystem_error"})
            return {"subject_id": subject_id, "records": records, "record_errors": record_errors}

    def patch(self, subject_id: str, question_id: str, update: dict):
        if not isinstance(update, dict) or set(update) - {"base_version", "needs_review", "reviewed", "notes"}:
            raise AppError(400, "invalid_update", "仅支持 base_version、needs_review、reviewed、notes 字段。")
        base_version = update.get("base_version")
        if not isinstance(base_version, str) or (base_version and not re.fullmatch(r"[0-9a-f]{64}", base_version)):
            raise AppError(400, "invalid_version", "请提交读取时的 base_version。")
        if not {"needs_review", "reviewed", "notes"}.intersection(update):
            raise AppError(400, "invalid_update", "没有需要保存的字段。")
        if "needs_review" in update and not isinstance(update["needs_review"], bool):
            raise AppError(400, "invalid_update", "needs_review 必须是布尔值。")
        if "reviewed" in update and not isinstance(update["reviewed"], bool):
            raise AppError(400, "invalid_update", "reviewed 必须是布尔值。")
        if "notes" in update and (not isinstance(update["notes"], str) or len(update["notes"].encode("utf-8")) > MAX_JSON // 2):
            raise AppError(400, "invalid_update", "笔记必须是文字且不超过 1 MB。")
        with self._write_lock():
            book, question = self._question(subject_id, question_id)
            path = self._record_path(subject_id, question_id, question["title"])
            original = path.read_bytes() if path.exists() else None
            if original is not None:
                current, header, positions = parse_record(original, subject_id, question_id)
            else:
                current = default_record()
                header = ["复习中心格式: 1", f"题目ID: {yaml_string(question_id)}",
                          f"学科ID: {yaml_string(subject_id)}", f"题目: {yaml_string(question['title'])}",
                          f"学科: {yaml_string(book['title'])}", "还不会: false", '更新时间: ""']
                positions = {"还不会": 5, "更新时间": 6}
            if base_version != current["version"]:
                raise AppError(409, "conflict", "此题记录已在另一页面或 Obsidian 中更新，请先核对新版。", record=current)
            updated_at = now_utc()
            if "needs_review" in update:
                header[positions["还不会"]] = "还不会: " + ("true" if update["needs_review"] else "false")
            reviewed_line = "本轮已复习: " + ("true" if update.get("reviewed", current["reviewed"]) else "false")
            if "本轮已复习" in positions:
                header[positions["本轮已复习"]] = reviewed_line
            else:
                # This is optional on read so pre-progress notes stay untouched
                # until the user explicitly saves a change to this record.
                header.append(reviewed_line)
            header[positions["更新时间"]] = "更新时间: " + yaml_string(updated_at)
            notes = update.get("notes", current["notes"])
            content = ("---\n" + "\n".join(header) + "\n---\n" + notes).encode("utf-8")
            # Catch direct edits which happened while the save was being prepared.
            latest = path.read_bytes() if path.exists() else None
            if latest != original:
                new_record = parse_record(latest, subject_id, question_id)[0] if latest is not None else default_record()
                raise AppError(409, "conflict", "记录刚刚被其他程序修改，请先核对新版。", record=new_record)
            atomic_write(path, content)
            return {"record": parse_record(path.read_bytes(), subject_id, question_id)[0]}

    def library(self):
        with self.lock:
            content_dir = self._safe("content")
            subjects = []
            known = {}
            unreadable_subjects = set()
            diagnostics = []
            if content_dir.is_dir():
                for directory in sorted(content_dir.iterdir()):
                    if directory.is_dir() and ID_PATTERN.fullmatch(directory.name) and (directory / "book.json").is_file():
                        try:
                            book = self.book(directory.name)
                            subject_state = self.state(directory.name)
                        except (AppError, OSError) as exc:
                            unreadable_subjects.add(directory.name)
                            diagnostics.append({"subject_id": directory.name,
                                                "path": f"content/{directory.name}/book.json", "message": str(exc),
                                                "error": exc.payload["error"] if isinstance(exc, AppError) else "filesystem_error"})
                            continue
                        state = subject_state["records"]
                        diagnostics.extend({"subject_id": directory.name, **error} for error in subject_state["record_errors"])
                        known[directory.name] = {q["id"] for q in book["questions"]}
                        subjects.append({"id": book["id"], "title": book["title"], "subtitle": book.get("subtitle", ""),
                                         "question_count": len(book["questions"]),
                                         "marked_count": sum(r["needs_review"] for r in state.values()),
                                         "reviewed_count": sum(r["reviewed"] for r in state.values()),
                                         "note_count": sum(bool(r["notes"].strip()) for r in state.values()),
                                         "uncertain_count": sum(q.get("confidence") not in (None, "", "high", "confirmed", "verified", "明确", "确定") for q in book["questions"])})
            orphans = []
            records_root = self._safe(RECORDS_DIR)
            if records_root.is_dir():
                for path in sorted(records_root.rglob("*.md")):
                    relative = path.relative_to(records_root)
                    if len(relative.parts) != 2:
                        orphans.append({"path": f"{RECORDS_DIR}/{relative.as_posix()}", "reason": "unrecognized_location"})
                        continue
                    subject_id = relative.parts[0]
                    question_id = path.name.split("--", 1)[0] if "--" in path.name else path.stem
                    if subject_id not in known or question_id not in known[subject_id]:
                        orphans.append({"subject_id": subject_id, "question_id": question_id,
                                        "path": f"{RECORDS_DIR}/{relative.as_posix()}",
                                        "reason": "unreadable_subject" if subject_id in unreadable_subjects else "not_in_current_book"})
            return {"subjects": subjects, "orphaned_records": orphans, "diagnostics": diagnostics}

    @staticmethod
    def _backup_allowed(name: str) -> bool:
        parts = PurePosixPath(name).parts
        if not parts or name == MANIFEST or any(p.startswith(".") or p.lower() in {"tmp", "backup", "backups", "__pycache__"} for p in parts):
            return False
        return (parts[0] in BACKUP_DIRS and len(parts) > 1) or (len(parts) == 1 and Path(parts[0]).suffix.lower() in BACKUP_ROOT_EXTENSIONS)

    def backup(self) -> bytes:
        with self._write_lock():
            buffer = io.BytesIO()
            total_size = 0
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(MANIFEST, json.dumps({"app": "review-hub", "format": 1, "created_at": now_utc()}, ensure_ascii=False))
                for path in sorted(self.root.rglob("*")):
                    name = path.relative_to(self.root).as_posix()
                    if path.is_file() and self._backup_allowed(name):
                        safe = self._safe(name)
                        payload = safe.read_bytes()
                        total_size += len(payload)
                        if total_size > MAX_BACKUP - 4096:
                            raise AppError(413, "backup_too_large", "备份内容超过 512 MB，请复制整个复习中心目录进行备份。")
                        archive.writestr(name, payload)
            data = buffer.getvalue()
            if len(data) > MAX_BACKUP:
                raise AppError(413, "backup_too_large", "备份超过 512 MB，请复制整个复习中心目录进行备份。")
            return data

    def restore(self, data: bytes):
        if len(data) > MAX_BACKUP:
            raise AppError(413, "backup_too_large", "备份文件不能超过 512 MB。")
        # Validate every member before making any filesystem changes.
        validated = []
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
                entries = archive.infolist()
                if len(entries) > 20000 or sum(e.file_size for e in entries) > MAX_BACKUP:
                    raise AppError(413, "backup_too_large", "备份解压内容超过安全上限 512 MB。")
                seen = set()
                manifest = None
                for entry in entries:
                    # ZipInfo normalizes backslashes on Windows; inspect the
                    # original central-directory filename before normalization.
                    name = entry.orig_filename
                    pure = PurePosixPath(name)
                    if not name or "\\" in name or "\x00" in name or pure.is_absolute() or ".." in pure.parts or any(":" in p for p in pure.parts):
                        raise AppError(400, "invalid_archive", "备份包含不安全路径，未导入任何文件。")
                    canonical = pure.as_posix().rstrip("/")
                    if canonical.casefold() in seen:
                        raise AppError(400, "invalid_archive", "备份包含重复文件名。")
                    seen.add(canonical.casefold())
                    mode = (entry.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                        raise AppError(400, "invalid_archive", "备份包含符号链接或特殊文件，未导入任何文件。")
                    if entry.is_dir():
                        continue
                    payload = archive.read(entry)
                    if name == MANIFEST:
                        manifest = json.loads(payload.decode("utf-8"))
                        continue
                    if not self._backup_allowed(name):
                        raise AppError(400, "invalid_archive", "备份包含非复习中心文件，未导入任何文件。")
                    path = self._safe(name)
                    validated.append((name, path, payload))
                if not isinstance(manifest, dict) or manifest.get("app") != "review-hub" or manifest.get("format") != 1:
                    raise AppError(400, "invalid_archive", "这不是可识别的复习中心备份。")
                file_names = {name.casefold() for name, _, _ in validated}
                for name, _, _ in validated:
                    if any(parent.as_posix().casefold() in file_names for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")):
                        raise AppError(400, "invalid_archive", "备份中文件与文件夹路径相互冲突，未导入任何文件。")
        except AppError:
            raise
        except (zipfile.BadZipFile, RuntimeError, UnicodeError, ValueError, NotImplementedError, OSError) as exc:
            raise AppError(400, "invalid_archive", "无法读取此备份，未导入任何文件。") from exc
        with self._write_lock():
            restored = 0
            conflicts = []
            batch = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            for name, path, payload in validated:
                parent_blocked = any(parent.exists() and not parent.is_dir() for parent in path.parents if parent != self.root)
                if parent_blocked or path.exists():
                    if path.is_file() and path.read_bytes() == payload:
                        continue
                    conflict_path = self._safe(f"{CONFLICTS_DIR}/{batch}/{name}")
                    atomic_write(conflict_path, payload)
                    conflicts.append({"path": name, "incoming_path": conflict_path.relative_to(self.root).as_posix()})
                else:
                    atomic_write(path, payload)
                    restored += 1
            return {"restored_count": restored, "conflicts": conflicts}


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store: ReviewStore, mobile: MobileAccess | None = None,
                 *, study: bool = False):
        self.store = store
        self.mobile = mobile
        self.study = study
        super().__init__(address, ReviewHandler)


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "ReviewHub/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        if self.server.mobile is not None:
            # Request lines may contain a pairing token. Do not log query strings
            # or untrusted headers, including on malformed/unsupported requests.
            if format.startswith('"%s"') and len(args) >= 2:
                route = "/pair" if urlsplit(self.path).path == "/pair" else "request"
                print(f"[{now_utc()}] {self.address_string()} {route} {args[1]}")
            else:
                print(f"[{now_utc()}] {self.address_string()} mobile request error")
            return
        print(f"[{now_utc()}] {self.address_string()} {format % args}")

    @property
    def store(self):
        return self.server.store

    def _send(self, status, data: bytes, content_type="application/json; charset=utf-8", extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if self.server.mobile and getattr(self, "mobile_authenticated", False):
            # iOS copies login cookies at installation, then uses an independent
            # store. Active use renews that store's cookie without exposing a
            # permanent pairing token through the manifest or JavaScript.
            self.send_header("Set-Cookie", self._mobile_cookie())
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _mobile_cookie(self):
        return (f"review_hub_mobile_{self.server.server_port}={self.server.mobile.session}; "
                "Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")

    def _pairing_page(self):
        # This page contains no token or saved data. It also lets an installed
        # Home Screen app re-pair in its own cookie store after a long absence.
        page = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer"><title>重新连接 · 复习中心</title>
<style>html{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;background:#faf9f5;color:#263b31}
body{margin:0;padding:max(28px,env(safe-area-inset-top)) 20px 32px}
main{max-width:420px;margin:10vh auto;background:white;border:1px solid #e4e7e0;border-radius:22px;padding:28px;box-shadow:0 12px 38px #263b310b}
h1{font-size:26px;margin:0 0 18px}p{font-size:16px;line-height:1.8;color:#5a685f}
label{display:block;font-weight:600;font-size:15px;margin-top:24px}input{box-sizing:border-box;width:100%;font:inherit;font-size:16px;padding:13px;border:1px solid #b6c4b8;border-radius:10px;margin:9px 0 14px}
button{border:0;border-radius:10px;padding:13px;width:100%;background:#316b50;color:white;font:inherit;font-weight:600;cursor:pointer}
#pair-error{color:#9e392b;font-size:14px;min-height:1.5em}small{display:block;color:#69766e;line-height:1.7;margin-top:20px}
</style></head><body><main><h1>连接复习中心</h1>
<p>此入口需要重新配对。请在电脑打开“手机访问”，将页面中的完整配对链接复制到下面。</p>
<form action="/pair" method="get" id="pair-form"><label for="pair-code">配对链接或配对口令</label>
<input id="pair-code" name="token" type="text" inputmode="url" autocomplete="off" autocapitalize="off" spellcheck="false" required aria-describedby="pair-error">
<button type="submit">连接并继续复习</button><p id="pair-error" role="status"></p></form>
<small>请保持电脑开机，并让手机与电脑连接同一个 Wi-Fi。已保存的笔记仍在电脑上。</small></main>
<script>document.getElementById('pair-form').addEventListener('submit',function(event){
var field=document.getElementById('pair-code'),value=field.value.trim();
try{if(/^https?:\\/\\//i.test(value)){var link=new URL(value);if(link.origin!==location.origin)throw Error();value=link.searchParams.get('token')||'';}
if(!/^[A-Za-z0-9_-]{32,256}$/.test(value))throw Error();field.value=value;
}catch(error){event.preventDefault();document.getElementById('pair-error').textContent='请粘贴这台电脑提供的完整配对链接或口令。';}});</script>
</body></html>'''
        self._send(401, page.encode("utf-8"), "text/html; charset=utf-8")

    def _host_check(self):
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("http://" + host)
            allowed = {self.server.mobile.host} if self.server.mobile else {"127.0.0.1", "localhost"}
            if (len(self.headers.get_all("Host", [])) != 1 or parsed.hostname not in allowed or
                    parsed.port != self.server.server_port or parsed.path or
                    parsed.query or parsed.fragment or parsed.username or parsed.password):
                raise ValueError
        except ValueError:
            message = "请使用手机访问页面给出的局域网地址。" if self.server.mobile else "仅允许通过此电脑的本地地址访问。"
            raise AppError(403, "invalid_host", message)

    def _mobile_check(self, path):
        mobile = self.server.mobile
        if mobile is None:
            return False
        if not mobile.allows_peer(self.client_address[0]):
            raise AppError(403, "invalid_peer", "仅允许此电脑所在局域网的设备访问。")
        # Applies to reads as well as writes: authenticated resources must not be
        # embeddable/readable from another origin even if a client sends a cookie.
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{mobile.host}:{self.server.server_port}":
            raise AppError(403, "invalid_origin", "请从已配对的复习中心页面操作。")
        if path == "/pair":
            if self.command != "GET":
                raise AppError(405, "method_not_allowed", "请直接打开配对链接。")
            try:
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
            except ValueError as exc:
                raise AppError(401, "pairing_required", "配对链接无效，请重新扫描电脑上的二维码。") from exc
            candidates = query.get("token", [])
            if (set(query) != {"token"} or len(candidates) != 1 or
                    not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", candidates[0]) or
                    not hmac.compare_digest(candidates[0], mobile.token)):
                raise AppError(401, "pairing_required", "配对链接无效，请重新扫描电脑上的二维码。")
            self._send(303, b"", "text/plain; charset=utf-8", {"Location": "/", "Set-Cookie": self._mobile_cookie()})
            return True
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            supplied = cookies.get(f"review_hub_mobile_{self.server.server_port}")
            authenticated = supplied is not None and hmac.compare_digest(supplied.value, mobile.session)
        except (CookieError, TypeError, ValueError):
            authenticated = False
        if not authenticated:
            raise AppError(401, "pairing_required", "请先用手机扫描电脑“手机访问”中的二维码，或打开完整的配对链接。")
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise AppError(403, "invalid_origin", "请直接打开复习中心，拒绝其他网站的访问。")
        self.mobile_authenticated = True
        return False

    def _write_check(self, content_type):
        self._host_check()
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                allowed = {self.server.mobile.host} if self.server.mobile else {"127.0.0.1", "localhost"}
                if parsed.scheme != "http" or parsed.hostname not in allowed or parsed.port != self.server.server_port or parsed.path or parsed.query or parsed.fragment or parsed.username:
                    raise ValueError
            except ValueError:
                raise AppError(403, "invalid_origin", "此操作仅允许从复习中心本地页面发起。")
        elif self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise AppError(403, "invalid_origin", "拒绝来自其他网站的写入。")
        if self.headers.get_content_type() != content_type:
            raise AppError(415, "invalid_content_type", f"此接口需要 {content_type}。")

    def _body(self, limit):
        if self.headers.get("Transfer-Encoding"):
            raise AppError(400, "invalid_body", "不支持分块上传。")
        try:
            size = int(self.headers.get("Content-Length", ""))
            if size < 0:
                raise ValueError
        except ValueError:
            raise AppError(411, "length_required", "请提供正确的 Content-Length。")
        if size > limit:
            raise AppError(413, "body_too_large", "请求内容超过大小限制。")
        self.connection.settimeout(30)
        content = self.rfile.read(size)
        if len(content) != size:
            raise AppError(400, "invalid_body", "上传内容不完整。")
        return content

    def _dispatch(self):
        self._host_check()
        raw_path = urlsplit(self.path).path
        try:
            path = unquote(raw_path, errors="strict")
        except UnicodeError:
            raise AppError(400, "invalid_path", "请求路径编码不正确。")
        if "\\" in path or "\x00" in path or ".." in PurePosixPath(path).parts:
            raise AppError(400, "invalid_path", "无效的请求路径。")
        path = "/" + "/".join(part for part in path.split("/") if part not in {"", "."})
        if self._mobile_check(path):
            return
        if not self.server.study:
            if self.command not in {"GET", "HEAD"}:
                raise AppError(405, "method_not_allowed", "PDF 阅读器只读取本地界面资源。")
            if path.startswith(("/api/", "/content/", "/media/")) or path.lower() == "/assets/study.html":
                raise AppError(404, "not_found", "找不到此页面。")
        parts = path.strip("/").split("/")
        if self.command in {"GET", "HEAD"}:
            if len(parts) == 4 and parts[0] == "media":
                image_path = self.store.media_path(parts[1], parts[2], parts[3])
                if not image_path.is_file():
                    raise AppError(404, "not_found", "找不到这张笔记图片。")
                return self._send(200, image_path.read_bytes(), "image/png")
            if path == "/api/health":
                if self.server.mobile:
                    return self._json(200, {"app": "review-hub", "version": APP_VERSION, "mode": "mobile"})
                return self._json(200, {"app": "review-hub", "version": APP_VERSION, "root": str(self.store.root)})
            if path == "/api/library":
                return self._json(200, self.store.library())
            if len(parts) == 3 and parts[:2] == ["api", "books"]:
                return self._json(200, self.store.book(parts[2]))
            if len(parts) == 3 and parts[:2] == ["api", "state"]:
                return self._json(200, self.store.state(parts[2]))
            if path == "/api/backup":
                filename = "review-hub-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip"
                return self._send(200, self.store.backup(), "application/zip", {"Content-Disposition": f'attachment; filename="{filename}"'})
            if path.startswith("/api/"):
                raise AppError(404, "not_found", "找不到此接口。")
            cover = "web/assets/local/cover.png"
            if path == "/assets/local-theme.css":
                css = b""
                if self.store._safe(cover).is_file():
                    css = b".hero-art .hero-scene{opacity:.88;filter:none}.hero-art .hero-figure{display:none}"
                return self._send(200, css, "text/css; charset=utf-8")
            if path == "/":
                relative = "web/study.html" if self.server.study else "web/index.html"
            elif path == "/manifest.webmanifest":
                relative = "web/manifest.webmanifest"
            elif path == "/apple-touch-icon.png":
                relative = "web/assets/icons/apple-touch-icon.png"
            elif path.startswith("/assets/"):
                relative = "web/" + path[len("/assets/"):]
                # Local theme files are ignored by Git. Only these explicit
                # substitutions are available through the normal asset URLs.
                overrides = {
                    "/assets/assets/shaosong/hero-scene.png": "web/assets/local/hero-scene.png",
                    "/assets/assets/shaosong/hero-figure.png": "web/assets/local/hero-figure.png",
                }
                if self.server.study:
                    overrides["/assets/campaign-data.json"] = "web/campaign.local.json"
                replacement = overrides.get(path)
                if path == "/assets/assets/shaosong/hero-scene.png" and self.store._safe(cover).is_file():
                    replacement = cover
                if replacement and self.store._safe(replacement).is_file():
                    relative = replacement
                elif relative.lower().startswith("web/assets/local/") or relative.lower() == "web/campaign.local.json":
                    raise AppError(404, "not_found", "找不到此文件。")
            elif path.startswith("/vendor/"):
                relative = path.lstrip("/")
            elif path.startswith("/content/"):
                relative = path.lstrip("/")
            else:
                raise AppError(404, "not_found", "找不到此页面。")
            file_path = self.store._safe(relative)
            if not file_path.is_file() or any(p.startswith(".") for p in PurePosixPath(relative).parts):
                raise AppError(404, "not_found", "找不到此文件。")
            types = {".mjs": "text/javascript", ".wasm": "application/wasm"}
            mime = types.get(file_path.suffix.lower()) or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
                mime += "; charset=utf-8"
            return self._send(200, file_path.read_bytes(), mime)
        if self.command == "PATCH" and len(parts) == 4 and parts[:2] == ["api", "state"]:
            self._write_check("application/json")
            try:
                update = json.loads(self._body(MAX_JSON).decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise AppError(400, "invalid_json", "无法读取提交的 JSON。") from exc
            return self._json(200, self.store.patch(parts[2], parts[3], update))
        if self.command == "POST" and len(parts) == 4 and parts[:2] == ["api", "media"]:
            self._write_check("image/png")
            return self._json(201, self.store.upload_image(parts[2], parts[3], self._body(MAX_IMAGE)))
        if self.command == "POST" and path == "/api/restore":
            self._write_check("application/zip")
            return self._json(200, self.store.restore(self._body(MAX_BACKUP)))
        raise AppError(405, "method_not_allowed", "此地址不支持该操作。")

    def _handle(self):
        # BaseHTTPRequestHandler reuses a handler for HTTP keep-alive requests.
        self.mobile_authenticated = False
        try:
            self._dispatch()
        except AppError as exc:
            self.close_connection = True
            if (self.server.mobile and exc.payload["error"] == "pairing_required" and
                    self.command in {"GET", "HEAD"} and "text/html" in self.headers.get("Accept", "")):
                return self._pairing_page()
            self._json(exc.status, exc.payload)
        except (OSError, TimeoutError) as exc:
            self.close_connection = True
            detail = "请保留页面中的笔记并重试。" if self.server.mobile else f"请保留页面中的笔记：{exc}"
            self._json(500, {"error": "filesystem_error", "message": "文件读取或保存失败，" + detail})
        except Exception as exc:
            self.close_connection = True
            self.log_error("Unexpected error: %s", exc)
            self._json(500, {"error": "internal_error", "message": "处理失败，请保留页面中的笔记并重试。"})

    do_GET = _handle
    do_HEAD = _handle
    do_PATCH = _handle
    do_POST = _handle
    do_DELETE = _handle
    do_PUT = _handle


def create_server(app_root: str | Path | None = None, port: int = 8765,
                  *, study: bool = False) -> ReviewHTTPServer:
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535")
    return ReviewHTTPServer(("127.0.0.1", port), ReviewStore(app_root or Path(__file__).parent), study=study)


def create_mobile_server(app_root: str | Path | None = None, port: int = 8766, *,
                         host: str, token: str, network: str | None = None) -> ReviewHTTPServer:
    """Opt in to a paired LAN server. Never expands the desktop listener."""
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535")
    store = ReviewStore(app_root or Path(__file__).parent)
    mobile = MobileAccess(host, token, store.root, network)
    # Binding the selected address itself also verifies it is on this computer.
    return ReviewHTTPServer((mobile.host, port), store, mobile, study=True)


def main():
    parser = argparse.ArgumentParser(description="本地 PDF 阅读器与复习中心")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--study", action="store_true", help="启用题库、笔记和山河复归图")
    parser.add_argument("--mobile-host", help="显式开启手机访问，绑定指定的本机私有 IPv4 地址")
    parser.add_argument("--mobile-network", help="此网卡的实际 CIDR 网段，例如 192.168.1.0/24")
    parser.add_argument("--pair-token-file", type=Path, help=".runtime 内保存的随机配对口令文件")
    args = parser.parse_args()
    if args.mobile_host:
        if not args.study:
            parser.error("手机配对仅用于题库模式，请加 --study。")
        if args.pair_token_file is None:
            parser.error("手机访问需要 --pair-token-file，不能把口令直接放在命令行。")
        root = Path(__file__).resolve().parent
        try:
            token_file = args.pair_token_file.resolve(strict=True)
            token_file.relative_to(root / ".runtime")
            if args.pair_token_file.is_symlink():
                raise ValueError("不支持链接口令文件")
            if token_file.stat().st_size > 512:
                raise ValueError("口令文件过大")
            token = token_file.read_text(encoding="utf-8-sig").strip()
            server = create_mobile_server(root, args.port, host=args.mobile_host,
                                          token=token, network=args.mobile_network)
        except (OSError, ValueError) as exc:
            parser.error(f"无法开启手机访问：{exc}")
        print(f"复习中心手机访问已启动：http://{args.mobile_host}:{server.server_port}")
        print("请使用电脑上的手机访问入口配对；本服务只接受局域网内已配对的设备。")
    else:
        if args.mobile_network or args.pair_token_file:
            parser.error("--mobile-network 和 --pair-token-file 只用于 --mobile-host 模式。")
        server = create_server(port=args.port, study=args.study)
        title = "复习中心" if args.study else "山河阅卷 · PDF 阅读器"
        print(f"{title}已启动：http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
