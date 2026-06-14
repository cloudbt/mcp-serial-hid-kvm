"""OCR text extraction from screen captures, optimized for terminal content."""

import logging
import os
import re
from pathlib import Path

import pytesseract
from pytesseract import Output
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

    def extract_elements(
        self,
        image: Image.Image,
        preprocess: bool = True,
        min_confidence: float = 0.0,
    ) -> list[dict]:
        """Extract text elements with bounding boxes using Tesseract TSV output.

        Coordinates are mapped back to the *original* ``image`` pixel space, so
        callers can use them directly for click targeting.

        Args:
            image: PIL Image (capture frame, original resolution).
            preprocess: Apply the same preprocessing used by ``extract_text``.
            min_confidence: Drop elements below this confidence (0.0-1.0).

        Returns:
            List of dicts: ``{"text", "x", "y", "w", "h", "confidence"}`` where
            confidence is 0.0-1.0. Empty/whitespace-only tokens are skipped.
        """
        if preprocess:
            processed = self.preprocess_image(image)
        else:
            processed = image

        # preprocess_image upscales the frame; map coordinates back to the
        # original frame so click targeting matches screen pixels.
        scale_x = image.width / processed.width if processed.width else 1.0
        scale_y = image.height / processed.height if processed.height else 1.0

        try:
            data = pytesseract.image_to_data(
                processed,
                lang=self.tesseract_lang,
                config=self.tesseract_config,
                output_type=Output.DICT,
            )
        except Exception as e:
            logger.error(f"OCR (elements) failed: {e}")
            return []

        elements: list[dict] = []
        count = len(data.get("text", []))
        for i in range(count):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf_raw = float(data["conf"][i])
            except (ValueError, TypeError):
                conf_raw = -1.0
            confidence = conf_raw / 100.0 if conf_raw >= 0 else 0.0
            if confidence < min_confidence:
                continue
            elements.append({
                "text": text,
                "x": int(round(data["left"][i] * scale_x)),
                "y": int(round(data["top"][i] * scale_y)),
                "w": int(round(data["width"][i] * scale_x)),
                "h": int(round(data["height"][i] * scale_y)),
                "confidence": round(confidence, 2),
            })
        return elements

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
