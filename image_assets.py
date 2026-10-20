"""Validate and stage an existing camera output for a reviewed Qzone post."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from urllib.parse import unquote, urlparse

from .core import _reject_health_terms, clean_source_text

MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_SIGNATURES = {
    ".jpg": ((b"\xff\xd8\xff", "image/jpeg"),),
    ".jpeg": ((b"\xff\xd8\xff", "image/jpeg"),),
    ".png": ((b"\x89PNG\r\n\x1a\n", "image/png"),),
    ".webp": ((b"RIFF", "image/webp"),),
}

# 2026-10-20：和 Moment 同一套关键词主体。必须写出**一个具体主体**，
# 绝不写成“请从 A、B、C 中选择”——菜单式表述会让相机的 classify_subject()
# 返回 unknown，图片通道会被永久卡死。
QZONE_SUBJECT_HINTS: tuple[tuple[str, str], ...] = (
    ("猫", "路边一只慵懒的猫"),
    ("狗", "一只摇着尾巴的小狗"),
    ("小动物", "一只可爱的小动物"),
    ("咖啡", "桌上一杯冒着热气的咖啡"),
    ("奶茶", "手边放着一杯奶茶"),
    ("面馆", "一碗刚端上来的面"),
    ("一碗面", "一碗刚端上来的面"),
    ("面条", "一碗刚端上来的面"),
    ("吃面", "一碗刚端上来的面"),
    ("拉面", "一碗刚端上来的面"),
    ("饭", "桌上简单的家常菜"),
    ("吃", "桌上的一餐饭"),
    ("书", "摊开的一本书"),
    ("雨", "雨后湿漉漉的街道"),
    ("雪", "覆着薄雪的街景"),
    ("夜", "窗外的夜色"),
    ("花丛", "路边开着的一丛花"),
    ("开花", "路边开着的一丛花"),
    ("鲜花", "路边开着的一丛花"),
    ("一朵花", "路边开着的一丛花"),
    ("街", "窗外安静的街景"),
    ("天空", "抬头看到的天空"),
    ("云", "天上的云"),
    ("海", "远处的海面"),
    ("路灯", "亮起来的路灯"),
)


def qzone_image_subject(content: str) -> str:
    """Pick one explicit image subject from the draft text."""
    text = str(content or "")
    for keyword, subject in QZONE_SUBJECT_HINTS:
        if keyword in text:
            return subject
    return "沈星回本人"


def build_qzone_image_request(content: str) -> str:
    """Build a camera request with exactly one explicit subject."""
    clip = str(content or "").strip()[:300].rstrip("。！？!?…，,、 ")
    subject = qzone_image_subject(content)
    if subject == "沈星回本人":
        return (
            f"沈星回本人的生活随拍，画面主体是沈星回本人，用来配这条动态：{clip}。"
            "不出现用户或其他真实人物，不加文字、水印或拼贴。"
        )
    return f"{subject}的照片，用来配这条动态：{clip}。不加文字、水印或拼贴。"



def stage_camera_image(filename: str, camera_images: Path, asset_dir: Path) -> str:
    """Copy one camera output into plugin-owned storage and return a NapCat file URI.

    The source is selected by basename from the camera plugin's generated image
    directory. No camera request or image-generation operation occurs here.
    """
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise ValueError("image must be selected by a plain filename")
    root = camera_images.resolve(strict=True)
    source = root / filename
    if source.parent != root or source.is_symlink():
        raise ValueError("image must be a regular file directly inside camera images")
    suffix = source.suffix.lower()
    signatures = IMAGE_SIGNATURES.get(suffix)
    if signatures is None:
        raise ValueError("only JPEG, PNG, and WebP images are allowed")
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(fd)
        raise ValueError("image must be a regular file directly inside camera images")
    size = file_stat.st_size
    if size <= 0 or size > MAX_IMAGE_BYTES:
        os.close(fd)
        raise ValueError("image must be between 1 byte and 8 MiB")
    with os.fdopen(os.dup(fd), "rb") as stream:
        header = stream.read(12)
    if not any(header.startswith(signature) for signature, _ in signatures):
        os.close(fd)
        raise ValueError("image file signature does not match its extension")
    if suffix == ".webp" and (len(header) < 12 or header[8:12] != b"WEBP"):
        os.close(fd)
        raise ValueError("WebP image signature is invalid")
    try:
        image_hash = hashlib.sha256()
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                image_hash.update(chunk)
        digest = image_hash.hexdigest()
        destination = asset_dir / f"{digest}{suffix}"
        asset_dir.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb") as source_stream, temporary.open("wb") as destination_stream:
            for chunk in iter(lambda: source_stream.read(64 * 1024), b""):
                destination_stream.write(chunk)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
        os.replace(temporary, destination)
    finally:
        os.close(fd)
    return destination.resolve(strict=True).as_uri()


def verify_staged_image(uri: str, asset_dir: Path) -> bool:
    """Verify that an approved file URI still names its immutable content digest."""
    try:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc:
            return False
        path = Path(unquote(parsed.path)).resolve(strict=True)
        root = asset_dir.resolve(strict=True)
        if path.parent != root or path.is_symlink() or not path.is_file():
            return False
        suffix = path.suffix.lower()
        if suffix not in IMAGE_SIGNATURES or path.stat().st_size > MAX_IMAGE_BYTES:
            return False
        expected = path.stem
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected
    except (OSError, ValueError):
        return False


async def generate_qzone_camera_asset(camera: object, caption: str, asset_dir: Path) -> str:
    """Ask the existing camera generator for one local image and stage its returned file.

    This method uses only ``generate_moment_file`` and never calls the camera's
    QQ send tool. The caller must persist a one-shot claim before invoking it.
    """
    safe_caption = clean_source_text(caption, "Qzone caption", required=True, max_chars=1200)
    _reject_health_terms(safe_caption)
    generator = getattr(camera, "generate_moment_file", None)
    image_dir = getattr(camera, "image_dir", None)
    if not callable(generator) or image_dir is None:
        raise ValueError("the existing camera file generator is unavailable")
    prompt = build_qzone_image_request(safe_caption)
    result = await generator(prompt, persona_context="")
    if not isinstance(result, Path) or result.is_symlink():
        raise ValueError("camera did not return a safe local image path")
    root = Path(image_dir).resolve(strict=True)
    source = result.resolve(strict=True)
    if source.parent != root or source.suffix.lower() != ".png" or not source.is_file():
        raise ValueError("camera image is outside its configured output directory")
    if source.stat().st_size <= 0 or source.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("camera image must be between 1 byte and 8 MiB")
    return stage_camera_image(source.name, root, asset_dir)
