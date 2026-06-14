"""OCR text extraction from screen captures, optimized for terminal content."""

import logging
import os
import re
from pathlib import Path

import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

logger = logging.getLogger(__name__)


class TerminalOCR:
    """OCR engine optimized for terminal/console text."""

    def __init__(self, tesseract_cmd: str | None = None):
        local_tesseract_root = Path(
            os.environ.get(
                "MCP_TESSERACT_ROOT",
                r"C:\work\work-git\nanokvm-usb-edid\tools\tesseract-5.5.2",
            )
        )
        local_tessdata_best = Path(
            os.environ.get(
                "TESSDATA_PREFIX",
                r"C:\work\work-git\nanokvm-usb-edid\tools\tessdata-best",
            )
        )

        if local_tesseract_root.exists():
            path_parts = [
                str(local_tesseract_root),
                str(local_tesseract_root / "Library" / "bin"),
                str(local_tesseract_root / "Library" / "usr" / "bin"),
                str(local_tesseract_root / "Scripts"),
                os.environ.get("PATH", ""),
            ]
            os.environ["PATH"] = os.pathsep.join(path_parts)

        if local_tessdata_best.exists() and not os.environ.get("TESSDATA_PREFIX"):
            os.environ["TESSDATA_PREFIX"] = str(local_tessdata_best)

        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        else:
            possible_paths = [
                os.environ.get("MCP_TESSERACT_CMD", ""),
                str(local_tesseract_root / "Library" / "bin" / "tesseract.exe"),
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]
            for path in possible_paths:
                if path and Path(path).exists():
                    pytesseract.pytesseract.tesseract_cmd = path
                    break

        self.tesseract_lang = os.environ.get("MCP_TESSERACT_LANG", "eng+jpn+chi_sim")
        self.tesseract_config = os.environ.get("MCP_TESSERACT_CONFIG", r"--oem 1 --psm 6")

    def preprocess_image(self, image: Image.Image) -> Image.Image:
        """Preprocess image for better OCR accuracy."""
        img = image.convert("L")

        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)

        img = img.filter(ImageFilter.SHARPEN)

        avg_brightness = sum(img.getdata()) / (img.width * img.height)
        if avg_brightness < 128:
            img = ImageOps.invert(img)

        threshold = 180
        img = img.point(lambda x: 255 if x > threshold else 0, "L")

        new_size = (img.width * 2, img.height * 2)
        img = img.resize(new_size, Image.Resampling.LANCZOS)

        return img

    def extract_text(self, image: Image.Image, preprocess: bool = True) -> str:
        """Extract text from a screen capture.

        Args:
            image: PIL Image
            preprocess: Whether to apply preprocessing

        Returns:
            Extracted text
        """
        if preprocess:
            processed = self.preprocess_image(image)
        else:
            processed = image

        try:
            text = pytesseract.image_to_string(
                processed,
                lang=self.tesseract_lang,
                config=self.tesseract_config,
            )
            return self._postprocess_text(text)
        except Exception as e:
            logger.error(f"OCR failed: {e}")
            return f"[OCR Error: {str(e)}]"

    def _postprocess_text(self, text: str) -> str:
        """Clean up OCR output."""
        lines = text.split("\n")
        cleaned_lines = [line.rstrip() for line in lines]

        result = "\n".join(cleaned_lines)
        result = re.sub(r"\n{4,}", "\n\n\n", result)

        safe_corrections = {
            " |s ": " ls ",
            " |s\n": " ls\n",
            "\n|s ": "\nls ",
        }
        for wrong, correct in safe_corrections.items():
            result = result.replace(wrong, correct)

        return result.strip()
