import io
import mimetypes
import os
import re
import shutil
import zipfile
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, after_this_request, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)

BASE_TEMP_DIR = Path(os.getenv("ORGNZR_TEMP_DIR", "temp_organizer")).resolve()

EXTENSION_CATEGORIES = {
    "Documents": {".pdf", ".doc", ".docx", ".rtf", ".txt", ".md", ".odt", ".pages"},
    "Spreadsheets": {".csv", ".xls", ".xlsx", ".ods", ".tsv"},
    "Presentations": {".ppt", ".pptx", ".odp", ".key"},
    "Python Scripts": {".py", ".ipynb"},
    "Web Files": {".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".json", ".xml"},
    "Source Code": {
        ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".php",
        ".rb", ".swift", ".kt", ".sql", ".sh", ".bat", ".ps1", ".yml", ".yaml",
    },
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".ico"},
    "Audio": {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"},
    "Video": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".m4v"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "Executables": {".exe", ".msi", ".app", ".dmg", ".apk"},
}


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def safe_category_name(raw_name: str) -> str:
    cleaned = (raw_name or "").strip().replace("_", " ").replace("-", " ")
    cleaned = re.sub(r'[<>:"/\\|?*]+', " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Unsorted"


def unique_destination(path: Path) -> Path:
    if not path.exists():
        return path

    if path.suffix:
        stem = path.stem
        suffix = path.suffix
    else:
        stem = path.name
        suffix = ""
    counter = 1

    while True:
        candidate = path.with_name(f"{stem} ({counter + 1}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def save_uploaded_files(files, upload_dir: Path) -> tuple[list[str], list[str]]:
    saved_files: list[str] = []
    duplicate_notes: list[str] = []
    seen_names: set[str] = set()

    for storage in files:
        original_name = secure_filename(storage.filename or "")
        if not original_name:
            continue

        if original_name in seen_names:
            duplicate_notes.append(f"Duplicate upload detected: {original_name}")
        seen_names.add(original_name)

        destination = unique_destination(upload_dir / original_name)
        storage.save(destination)
        saved_files.append(destination.name)

        if destination.name != original_name:
            duplicate_notes.append(f"{original_name} renamed to {destination.name}")

    return saved_files, duplicate_notes


def category_for_file(path: Path) -> str:
    extension = path.suffix.lower()

    for category, extensions in EXTENSION_CATEGORIES.items():
        if extension in extensions:
            return category

    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type:
        if mime_type.startswith("image/"):
            return "Images"
        if mime_type.startswith("audio/"):
            return "Audio"
        if mime_type.startswith("video/"):
            return "Video"
        if mime_type.startswith("text/"):
            return "Text Files"
        if "zip" in mime_type or "compressed" in mime_type:
            return "Archives"

    return "Others"


def organize_files_by_extension(upload_dir: Path, organized_dir: Path) -> tuple[str, list[str]]:
    moved_count = 0
    duplicate_notes: list[str] = []

    for item in sorted(upload_dir.iterdir(), key=lambda entry: entry.name.lower()):
        if not item.is_file():
            continue

        category = safe_category_name(category_for_file(item))
        destination_dir = organized_dir / category
        ensure_directory(destination_dir)

        destination = unique_destination(destination_dir / item.name)
        if destination.name != item.name:
            duplicate_notes.append(
                f"{item.name} renamed to {destination.name} in {category}"
            )

        shutil.move(str(item), str(destination))
        moved_count += 1

    return f"Organized {moved_count} file(s) by extension.", duplicate_notes


def move_unorganized_files(upload_dir: Path, organized_dir: Path) -> None:
    fallback_dir = organized_dir / "Unsorted"
    ensure_directory(fallback_dir)

    for item in upload_dir.iterdir():
        if item.is_file():
            shutil.move(str(item), str(unique_destination(fallback_dir / item.name)))


def create_zip_bytes(source_dir: Path) -> io.BytesIO:
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in source_dir.rglob("*"):
            if file_path.is_file():
                archive.write(file_path, arcname=file_path.relative_to(source_dir))
    zip_buffer.seek(0)
    return zip_buffer


def build_download_name() -> str:
    return "organized_files (orgnzr).zip"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/organize", methods=["POST"])
def organize():
    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        return jsonify({"error": "Please upload at least one file."}), 400

    ensure_directory(BASE_TEMP_DIR)

    session_id = uuid4().hex
    session_root = BASE_TEMP_DIR / session_id
    upload_dir = session_root / "uploads"
    organized_dir = session_root / "organized"

    ensure_directory(upload_dir)
    ensure_directory(organized_dir)

    saved_files, upload_duplicate_notes = save_uploaded_files(uploaded_files, upload_dir)
    if not saved_files:
        shutil.rmtree(session_root, ignore_errors=True)
        return jsonify({"error": "No valid files were uploaded."}), 400

    try:
        summary, organization_duplicate_notes = organize_files_by_extension(upload_dir, organized_dir)
        move_unorganized_files(upload_dir, organized_dir)
        zip_bytes = create_zip_bytes(organized_dir)
    except Exception as exc:
        shutil.rmtree(session_root, ignore_errors=True)
        return jsonify({"error": f"Organization failed: {exc}"}), 500

    duplicate_notes = upload_duplicate_notes + organization_duplicate_notes
    if duplicate_notes:
        summary = f"{summary} {' '.join(duplicate_notes[:10])}"

    @after_this_request
    def cleanup(response):
        shutil.rmtree(session_root, ignore_errors=True)
        return response

    response = send_file(
        zip_bytes,
        as_attachment=True,
        download_name=build_download_name(),
        mimetype="application/zip",
        max_age=0,
    )
    response.headers["X-Session-Id"] = session_id
    return response


if __name__ == "__main__":
    ensure_directory(BASE_TEMP_DIR)
    app.run(
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", os.getenv("FLASK_PORT", "5000"))),
        debug=False,
    )
