# gaia_utils.py
#
# GAIA dataset loading, final-answer extraction, and answer scoring.
#
# GAIA ("gaia-benchmark/GAIA" on Hugging Face Hub) is a gated dataset --
# you'll need to accept its terms on the Hub and run `huggingface-cli login`
# (or set HF_TOKEN) before load_gaia_questions(source="huggingface") will work.
# Alternatively, point local_path at a local .json/.jsonl export of the
# validation split (the split that actually ships ground-truth answers; the
# test split does not, so scoring against it isn't possible).
#
# --- File attachments ---------------------------------------------------
# GAIA tasks with a non-empty file_name reference an attachment shipped in
# the dataset repo. load_gaia_questions resolves each attachment to a local
# file path (downloading/caching the repo via huggingface_hub), and
# load_attachment() reads that file into a form langgraph_debate.py's
# init_agents can hand to the model, aiming to cover what Gemma 4 itself
# understands natively (text, image, video, audio) plus a few common office
# formats via cheap text extraction:
#   - images (.png/.jpg/.jpeg/.gif/.webp/.bmp)   -> base64, native image input
#   - PDFs                                       -> rasterized to page images
#     (requires PyMuPDF: pip install pymupdf)
#   - video (.mp4/.mov/.avi/.webm/.mkv)          -> sampled frames as images,
#     matching how Gemma 4 itself processes video (sequences of frames)
#     (requires opencv-python: pip install opencv-python)
#   - audio (.mp3/.wav/.m4a/.flac/.ogg/.aac)     -> raw bytes, base64, sent via
#     Ollama's "audio" message field. NOTE: as of writing, audio input support
#     in Ollama's standard chat API is still new/evolving across models and
#     server versions -- if your server/model rejects it, that error will
#     surface directly (we deliberately don't swallow it) rather than fail
#     silently, so you'll know to either update Ollama or drop the file.
#   - plain text (.txt/.csv/.json/.md/.py/...)   -> inlined into the prompt
#   - DOCX   -> paragraph + table text extracted (requires python-docx)
#   - XLSX/XLS -> cell values extracted per sheet (requires openpyxl)
#   - PPTX   -> slide text extracted (requires python-pptx)
#   - anything else (archives, proprietary formats, ...) -> not supported;
#     noted in the returned dict rather than silently dropped.
# Each optional dependency is only imported when actually needed, and a
# missing one degrades to "unsupported" with an explanatory note instead of
# crashing the run.
# Set text_only=True to go back to skipping every file-attached question.
# ---------------------------------------------------------------------------

import base64
import json
import os
import re
from typing import Any, Dict, List, Optional

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TEXT_EXTENSIONS = {".txt", ".csv", ".json", ".md", ".py", ".xml", ".html", ".htm", ".yaml", ".yml"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".webm", ".mkv"}
DOCX_EXTENSIONS = {".docx"}
XLSX_EXTENSIONS = {".xlsx", ".xls"}
PPTX_EXTENSIONS = {".pptx"}

_ATTACHMENT_INDEX_CACHE: Dict[str, Dict[str, str]] = {}


def _get_gaia_attachment_index(split: str) -> Dict[str, str]:
    """
    Downloads/caches the GAIA dataset repo via huggingface_hub and returns a
    {filename: local_path} index built by walking it once, rather than
    re-globbing the tree per question.
    """
    if split in _ATTACHMENT_INDEX_CACHE:
        return _ATTACHMENT_INDEX_CACHE[split]

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("[warn] huggingface_hub not available; cannot resolve GAIA attachment files "
              "(pip install huggingface_hub)")
        _ATTACHMENT_INDEX_CACHE[split] = {}
        return {}

    try:
        # NOTE: this downloads the whole repo (all splits/subsets) the first
        # time -- it's cached by huggingface_hub afterwards. If you know the
        # exact folder layout for your subset and want to avoid pulling
        # everything, add an `allow_patterns=[...]` here.
        root = snapshot_download(repo_id="gaia-benchmark/GAIA", repo_type="dataset")
    except Exception as e:
        print(f"[warn] failed to download GAIA attachments: {e}")
        _ATTACHMENT_INDEX_CACHE[split] = {}
        return {}

    index: Dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            index.setdefault(fn, os.path.join(dirpath, fn))

    _ATTACHMENT_INDEX_CACHE[split] = index
    return index


def _empty_attachment(kind: str, note: Optional[str] = None) -> Dict[str, Any]:
    return {"kind": kind, "text": None, "images_b64": [], "audio_b64": [], "note": note}


def _load_image(file_path: str) -> Dict[str, Any]:
    try:
        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        return _empty_attachment("unsupported", f"failed to read image: {e}")
    result = _empty_attachment("image")
    result["images_b64"] = [b64]
    return result


def _load_text(file_path: str, max_text_chars: int) -> Dict[str, Any]:
    try:
        with open(file_path, "r", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return _empty_attachment("unsupported", f"failed to read text file: {e}")
    note = None if len(text) <= max_text_chars else f"(truncated from {len(text)} to {max_text_chars} chars)"
    result = _empty_attachment("text", note)
    result["text"] = text[:max_text_chars]
    return result


def _load_pdf(file_path: str, max_pdf_pages: int) -> Dict[str, Any]:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return _empty_attachment(
            "unsupported",
            "PDF attachment present but PyMuPDF not installed (pip install pymupdf) -- skipped",
        )
    try:
        doc = fitz.open(file_path)
        images_b64 = []
        for page_index in range(min(max_pdf_pages, doc.page_count)):
            pix = doc.load_page(page_index).get_pixmap(dpi=150)
            images_b64.append(base64.b64encode(pix.tobytes("png")).decode("utf-8"))
        note = (
            None if doc.page_count <= max_pdf_pages
            else f"(only first {max_pdf_pages} of {doc.page_count} pages rendered)"
        )
        result = _empty_attachment("image", note)
        result["images_b64"] = images_b64
        return result
    except Exception as e:
        return _empty_attachment("unsupported", f"failed to render PDF: {e}")


def _load_audio(file_path: str) -> Dict[str, Any]:
    try:
        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        return _empty_attachment("unsupported", f"failed to read audio: {e}")
    result = _empty_attachment(
        "audio",
        "audio sent via Ollama's 'audio' message field -- this part of the API is still "
        "new/evolving, so if the model/server rejects it the error will surface directly",
    )
    result["audio_b64"] = [b64]
    return result


def _load_video(file_path: str, max_frames: int) -> Dict[str, Any]:
    try:
        import cv2
    except ImportError:
        return _empty_attachment(
            "unsupported",
            "video attachment present but opencv-python not installed (pip install opencv-python) -- skipped",
        )
    try:
        cap = cv2.VideoCapture(file_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        if total <= 0:
            cap.release()
            return _empty_attachment("unsupported", "could not read video (zero frames reported)")

        n = max(1, min(max_frames, total))
        images_b64 = []
        for i in range(n):
            frame_idx = int(i * total / n)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok:
                continue
            ok2, buf = cv2.imencode(".png", frame)
            if ok2:
                images_b64.append(base64.b64encode(buf.tobytes()).decode("utf-8"))
        cap.release()

        if not images_b64:
            return _empty_attachment("unsupported", "failed to extract any frames from video")
        result = _empty_attachment("image", f"({len(images_b64)} of {total} frames sampled)")
        result["images_b64"] = images_b64
        return result
    except Exception as e:
        return _empty_attachment("unsupported", f"failed to process video: {e}")


def _load_docx(file_path: str, max_text_chars: int) -> Dict[str, Any]:
    try:
        import docx  # python-docx
    except ImportError:
        return _empty_attachment(
            "unsupported",
            "DOCX attachment present but python-docx not installed (pip install python-docx) -- skipped",
        )
    try:
        document = docx.Document(file_path)
        lines = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                lines.append(" | ".join(cell.text for cell in row.cells))
        text = "\n".join(lines)
        note = None if len(text) <= max_text_chars else f"(truncated from {len(text)} to {max_text_chars} chars)"
        result = _empty_attachment("text", note)
        result["text"] = text[:max_text_chars]
        return result
    except Exception as e:
        return _empty_attachment("unsupported", f"failed to read DOCX: {e}")


def _load_xlsx(file_path: str, max_text_chars: int) -> Dict[str, Any]:
    try:
        import openpyxl
    except ImportError:
        return _empty_attachment(
            "unsupported",
            "XLSX attachment present but openpyxl not installed (pip install openpyxl) -- skipped",
        )
    try:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        lines = []
        for sheet in wb.worksheets:
            lines.append(f"# Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                lines.append(", ".join("" if v is None else str(v) for v in row))
        text = "\n".join(lines)
        note = None if len(text) <= max_text_chars else f"(truncated from {len(text)} to {max_text_chars} chars)"
        result = _empty_attachment("text", note)
        result["text"] = text[:max_text_chars]
        return result
    except Exception as e:
        return _empty_attachment("unsupported", f"failed to read XLSX: {e}")


def _load_pptx(file_path: str, max_text_chars: int) -> Dict[str, Any]:
    try:
        from pptx import Presentation
    except ImportError:
        return _empty_attachment(
            "unsupported",
            "PPTX attachment present but python-pptx not installed (pip install python-pptx) -- skipped",
        )
    try:
        prs = Presentation(file_path)
        lines = []
        for i, slide in enumerate(prs.slides, 1):
            lines.append(f"# Slide {i}")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    lines.append(shape.text_frame.text)
        text = "\n".join(lines)
        note = None if len(text) <= max_text_chars else f"(truncated from {len(text)} to {max_text_chars} chars)"
        result = _empty_attachment("text", note)
        result["text"] = text[:max_text_chars]
        return result
    except Exception as e:
        return _empty_attachment("unsupported", f"failed to read PPTX: {e}")


def load_attachment(
    file_path: str,
    max_text_chars: int = 20000,
    max_pdf_pages: int = 5,
    max_video_frames: int = 8,
) -> Dict[str, Any]:
    """
    Reads one attachment file into a dict init_agents can consume directly:
        {"kind": "image" | "text" | "audio" | "unsupported",
         "text": Optional[str],
         "images_b64": List[str],
         "audio_b64": List[str],
         "note": Optional[str]}
    "kind": "image" covers real images, PDF pages, and sampled video frames --
    all just lists of base64 PNGs from init_agents' point of view.
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext in IMAGE_EXTENSIONS:
        return _load_image(file_path)
    if ext in TEXT_EXTENSIONS:
        return _load_text(file_path, max_text_chars)
    if ext == ".pdf":
        return _load_pdf(file_path, max_pdf_pages)
    if ext in AUDIO_EXTENSIONS:
        return _load_audio(file_path)
    if ext in VIDEO_EXTENSIONS:
        return _load_video(file_path, max_video_frames)
    if ext in DOCX_EXTENSIONS:
        return _load_docx(file_path, max_text_chars)
    if ext in XLSX_EXTENSIONS:
        return _load_xlsx(file_path, max_text_chars)
    if ext in PPTX_EXTENSIONS:
        return _load_pptx(file_path, max_text_chars)

    return _empty_attachment("unsupported", f"file type '{ext}' not yet supported by this pipeline")


def load_gaia_questions(
    source: str = "huggingface",
    subset: str = "2023_all",
    split: str = "validation",
    local_path: Optional[str] = None,
    text_only: bool = False,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Returns a list of dicts: {"task_id", "query", "ground_truth", "level", "file_name", "file_path"}.
    file_path is the resolved local path to the attachment (None if there is
    no attachment, or if it couldn't be located/downloaded).

    source: "huggingface" (default) or "local" (requires local_path pointing
        at a .json array or .jsonl file with the same field names GAIA uses:
        task_id / Question / Final answer / Level / file_name; may also
        include "file_path" directly to skip attachment resolution).
    text_only: skip every question with a file attachment. Defaults to False
        now that attachments can actually be fed to the model -- set True to
        get the old text-only behavior back.
    limit: take only the first `limit` questions -- this is the "pre-select
        how many questions to run" knob. None = all available (after filtering).
    """
    use_hub = not (source == "local" or local_path is not None)

    if not use_hub:
        if local_path is None:
            raise ValueError("local_path is required when source='local'")
        with open(local_path, "r") as f:
            if local_path.endswith(".jsonl"):
                raw = [json.loads(line) for line in f if line.strip()]
            else:
                raw = json.load(f)
    else:
        try:
            from datasets import load_dataset
        except ImportError as e:
            raise ImportError(
                "The 'datasets' package is required to load GAIA from Hugging "
                "Face. Install with: pip install datasets"
            ) from e
        hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        try:
            ds = load_dataset("gaia-benchmark/GAIA", subset, split=split, token=hf_token)
        except Exception as e:
            hint = (
                "Could not load 'gaia-benchmark/GAIA' from the Hub. This dataset is gated: "
                "(1) log into huggingface.co with the account HF_TOKEN belongs to, open "
                "https://huggingface.co/datasets/gaia-benchmark/GAIA and click 'Agree and access "
                "repository' -- a token alone does not grant access until that's done; "
                "(2) make sure HF_TOKEN is actually set in *this* process (`python -c \"import os; "
                "print(bool(os.environ.get('HF_TOKEN')))\"`) -- exporting it in a different shell/tab "
                "won't carry over. Original error below."
            )
            raise RuntimeError(f"{hint}\n{e}") from e
        raw = list(ds)

    attachment_index = _get_gaia_attachment_index(split) if (use_hub and not text_only) else {}

    questions = []
    for item in raw:
        file_name = item.get("file_name", "") or ""
        if text_only and file_name.strip():
            continue

        file_path = item.get("file_path")  # local jsonl may specify this directly
        if not file_path and file_name.strip():
            file_path = attachment_index.get(file_name)
            if file_path is None and use_hub:
                print(f"[warn] could not locate attachment '{file_name}' for task {item.get('task_id')}")

        questions.append({
            "task_id": item.get("task_id"),
            "query": item.get("Question"),
            "ground_truth": item.get("Final answer"),
            "level": item.get("Level"),
            "file_name": file_name,
            "file_path": file_path,
        })

    if limit is not None:
        questions = questions[:limit]

    return questions


# --------------------------------------------------------------------------- #
# Final-answer extraction. We ask the aggregate node (via
# general_config["answer_format_instruction"]) to end with a line like
# "FINAL ANSWER: <answer>", the convention most GAIA agent baselines use.
# --------------------------------------------------------------------------- #

GAIA_ANSWER_FORMAT_INSTRUCTION = (
    "Finish your response with exactly one line in this format (no extra "
    "punctuation or explanation after it):\nFINAL ANSWER: [YOUR ANSWER]"
)

_FINAL_ANSWER_RE = re.compile(r"final answer\s*:\s*(.+)", re.IGNORECASE)


def extract_gaia_answer(raw_text: str) -> str:
    if not raw_text:
        return ""
    matches = _FINAL_ANSWER_RE.findall(raw_text)
    if matches:
        return matches[-1].strip().strip(".").strip()
    return raw_text.strip()


# --------------------------------------------------------------------------- #
# GAIA scoring -- reimplements the standard GAIA quasi-exact-match logic:
# numeric comparison when the ground truth is a number, element-wise
# comparison when it's a comma/semicolon-separated list, and normalized
# string comparison otherwise.
# --------------------------------------------------------------------------- #

def _is_float(x: str) -> bool:
    try:
        float(x)
        return True
    except (ValueError, TypeError):
        return False


def _normalize_number(x: str) -> Optional[float]:
    cleaned = x
    for ch in ("$", "%", ","):
        cleaned = cleaned.replace(ch, "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_str(x: str, remove_punct: bool = True) -> str:
    text = x.strip().lower()
    if remove_punct:
        text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _split_list(x: str) -> List[str]:
    return [part.strip() for part in re.split(r"[,;]", x)]


def gaia_question_scorer(model_answer: str, ground_truth: str) -> bool:
    if ground_truth is None:
        return False
    model_answer = model_answer or ""

    if _is_float(ground_truth):
        normalized = _normalize_number(model_answer)
        return normalized is not None and normalized == float(ground_truth)

    if any(ch in ground_truth for ch in (",", ";")):
        gt_parts = _split_list(ground_truth)
        ma_parts = _split_list(model_answer)
        if len(gt_parts) != len(ma_parts):
            return False
        for ma, gt in zip(ma_parts, gt_parts):
            if _is_float(gt):
                ma_num = _normalize_number(ma)
                if ma_num is None or ma_num != float(gt):
                    return False
            else:
                if _normalize_str(ma, remove_punct=False) != _normalize_str(gt, remove_punct=False):
                    return False
        return True

    return _normalize_str(model_answer) == _normalize_str(ground_truth)