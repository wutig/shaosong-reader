"""Import prepared study material without modifying personal Markdown records."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")

def validate_book(book: dict) -> None:
    if not isinstance(book, dict) or not ID.fullmatch(str(book.get("id", ""))):
        raise ValueError("学科ID须由字母、数字、下划线或短横线组成")
    if not isinstance(book.get("title"), str) or not book["title"].strip():
        raise ValueError("缺少学科名称")
    questions = book.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("题库至少需要一个条目")
    seen = set()
    for q in questions:
        if not isinstance(q, dict):
            raise ValueError("每个题目须为对象")
        qid = str(q.get("id", ""))
        if not ID.fullmatch(qid) or qid in seen:
            raise ValueError(f"题目ID无效或重复：{qid}")
        seen.add(qid)
        for field in ("title", "question", "answer"):
            if not isinstance(q.get(field), str):
                raise ValueError(f"{qid} 缺少文本字段 {field}")
        q.setdefault("number", len(seen))
        q.setdefault("topic", "其他")
        q.setdefault("confidence", "确定")
        q.setdefault("source_pages", [])
        q.setdefault("followups", [])
        q.setdefault("notes", [])
    book.setdefault("schema_version", 1)
    book.setdefault("sources", [])
    book.setdefault("subtitle", "原题与详解")
    book.setdefault("scope", "根据提供材料整理，保留识别存疑与原图来源。")

def contained(file: Path, root: Path) -> Path:
    p = file.resolve()
    if not p.is_relative_to(root.resolve()) or file.is_symlink():
        raise ValueError(f"资料路径超出指定目录：{file}")
    return p

def commit(staging: Path, book: dict, app_root: Path) -> dict:
    validate_book(book)
    content = (app_root / "content").resolve()
    content.mkdir(parents=True, exist_ok=True)
    target = (content / book["id"]).resolve()
    if target.parent != content:
        raise ValueError("非法导入目标")
    old_ids = set()
    history = None
    if target.exists():
        old = json.loads((target / "book.json").read_text(encoding="utf-8"))
        old_ids = {q["id"] for q in old.get("questions", [])}
    book["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    (staging / "book.json").write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    if target.exists():
        history_root = (app_root / ".history" / "content" / book["id"]).resolve()
        if not history_root.is_relative_to(app_root.resolve()):
            raise ValueError("非法历史目录")
        history_root.mkdir(parents=True, exist_ok=True)
        history = history_root / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        # Both resolved targets are explicitly confined to this application's folders.
        os.replace(target, history)
    try:
        os.replace(staging, target)
    except OSError:
        if history is not None and history.exists() and not target.exists():
            os.replace(history, target)
        raise
    new_ids = {q["id"] for q in book["questions"]}
    return {"subject_id": book["id"], "questions": len(new_ids), "removed_question_ids_preserved_in_records": sorted(old_ids-new_ids), "path": str(target)}

def from_review(review: Path, subject_id: str, title: str, subtitle: str, app_root: Path) -> dict:
    review = review.resolve()
    questions = json.loads((review / "source/organized.json").read_text(encoding="utf-8"))
    questions.sort(key=lambda q: q.get("number", 0))
    pages = json.loads((review / "source/pages.json").read_text(encoding="utf-8"))
    images = json.loads((review / "source/images.json").read_text(encoding="utf-8"))
    meta_path = review / "source/review_metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    book = {"schema_version": 1, "id": subject_id, "title": title, "subtitle": subtitle,
            "scope": metadata.get("scope", f"根据提供材料整理，共{len(questions)}个条目。未记录的历史追问不补造，识别存疑处保留原图。"),
            "questions": questions, "sources": []}
    validate_book(book)
    runtime = app_root / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="import-", dir=runtime))
    try:
        (staging / "images").mkdir()
        by_page = {}
        rotation = {int(k): v for k, v in metadata.get("rotations", {}).items()}
        for im in images:
            source = contained(Path(im["file"]), review)
            dest = staging / "images" / source.name
            shutil.copy2(source, dest)
            by_page.setdefault(im["page"], []).append({"url": f"/content/{subject_id}/images/{source.name}", "width": im["width"], "height": im["height"], "rotation": rotation.get(im["page"], 0)})
        for p in pages:
            if p["page"] < metadata.get("first_source_page", 1):
                continue
            book["sources"].append({"page": p["page"], "title": p["text"].split("\n")[0], "text": p["text"], "images": by_page.get(p["page"], [])})
        if metadata.get("pdf"):
            pdf = contained(review / metadata["pdf"], review)
        else:
            pdfs = sorted((review / "output/pdf").glob("*.pdf"))
            if len(pdfs) > 1:
                raise ValueError("存在多个PDF，请在 source/review_metadata.json 中指定pdf相对路径")
            pdf = contained(pdfs[0], review) if pdfs else None
        if pdf:
            shutil.copy2(pdf, staging / "原题与详解.pdf")
            book["pdf_url"] = f"/content/{subject_id}/原题与详解.pdf"
        return commit(staging, book, app_root)
    finally:
        if staging.exists():
            assert staging.resolve().is_relative_to(runtime.resolve())
            shutil.rmtree(staging)

def from_package(package: Path, app_root: Path) -> dict:
    package = package.resolve()
    book = json.loads((package / "book.json").read_text(encoding="utf-8"))
    validate_book(book)
    runtime = app_root / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="import-", dir=runtime))
    try:
        for p in package.rglob("*"):
            contained(p, package)
            if p.is_dir():
                continue
            relative = p.relative_to(package)
            if any(part.startswith(".") for part in relative.parts):
                continue
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, destination)
        return commit(staging, book, app_root)
    finally:
        if staging.exists():
            assert staging.resolve().is_relative_to(runtime.resolve())
            shutil.rmtree(staging)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepared-review", type=Path)
    group.add_argument("--package", type=Path)
    parser.add_argument("--subject-id", default="my-course")
    parser.add_argument("--title", default="我的课程")
    parser.add_argument("--subtitle", default="原题与详解")
    parser.add_argument("--app-root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.app_root.resolve()
    if args.prepared_review:
        result = from_review(args.prepared_review, args.subject_id, args.title, args.subtitle, root)
    else:
        result = from_package(args.package, root)
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
