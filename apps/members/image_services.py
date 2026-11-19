"""
Image processing services for member passport photographs.

Design in one paragraph
-----------------------
All of the real work happens in ``build_optimised_passport()``, a pure
function: bytes in, optimised bytes out, ``ValidationError`` on bad input.
It is used in two places:

* ``prepare_passport_upload()`` -- called from ``Member.clean()`` so that
  both the public registration form and the Django admin turn a bad image
  into a normal field error (not an HTTP 500). It swaps the upload for the
  optimised in-memory version *without writing anything to disk*, so a form
  that fails some other validation leaves no stray file behind.
* ``optimise_passport_photo()`` -- called from ``Member.save()`` for code
  paths that never run validation (scripts, shell, tests). It writes the
  optimised file through the normal Django storage API.

An upload that ``clean()`` already optimised is tagged, so ``save()`` does
not decode and re-encode it a second time (that would cost CPU twice and
degrade a JPEG twice). Images that are already stored (``_committed``) are
never touched.
"""

import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile

logger = logging.getLogger("apps.members.image_services")

MAX_PASSPORT_DIMENSION = (600, 600)

# Hard ceiling on decoded size, per format, checked from the image header
# BEFORE any pixel data is decoded. This is about RAM, not file size: a
# 1 MB PNG or a 0.2 MB WebP can describe a 40-megapixel image.
#
# Measured peak RAM of build_optimised_passport() on this code (Pillow 11):
#   JPEG  40 MP:   ~8 MB   (libjpeg decodes at reduced size, see draft())
#   PNG   20 MP:  ~85 MB (RGB)  /  ~165 MB (RGBA)
#   WebP  20 MP: ~310 MB       (25 MP: ~390 MB, 40 MP: ~615 MB)
# PythonAnywhere kills a process above 3 GB, so none of these would be
# fatal on their own -- but PNG/WebP are decoded in full, a typical 12-24 MP
# phone JPEG is the only large photo format people really upload, and nobody
# needs a >20 MP PNG/WebP for a passport. So JPEG keeps a generous cap and
# PNG/WebP get a tight one.
MAX_PASSPORT_PIXELS = {
    "JPEG": 40_000_000,
    "PNG": 20_000_000,
    "WEBP": 20_000_000,
}

# Encoder settings, chosen from measurements on 450x600 outputs (see the
# change report). The extra effort of PNG level 9 / optimize=True and WebP
# method 6 cost 3-6x the CPU for ~10% smaller files, which is the wrong
# trade on a CPU-metered free account.
JPEG_QUALITY = 82
WEBP_QUALITY = 82
WEBP_METHOD = 4          # Pillow's own default; method 6 is up to ~4x slower
PNG_COMPRESS_LEVEL = 6   # zlib default; level 9 / optimize=True ~4-6x slower

_EXIF_ORIENTATION_TAG = 0x0112
_PREPARED_FLAG = "_sams_passport_prepared"

_SUPPORTED_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
}

MSG_UNSUPPORTED_FORMAT = "Passport photo must be a JPEG, PNG or WebP image."
MSG_TOO_LARGE = (
    "This passport photograph is too large in dimensions. "
    "Please choose a smaller photo."
)
MSG_UNREADABLE = (
    "The uploaded passport photograph could not be processed. "
    "Please upload a different JPEG, PNG or WebP image."
)


@dataclass(frozen=True)
class OptimisedPassport:
    name: str
    content: bytes
    content_type: str


def build_optimised_passport(file_obj, filename):
    """
    Read ``file_obj`` and return an ``OptimisedPassport``.

    - Rejects anything that is not a readable JPEG, PNG or WebP.
    - Rejects images above the per-format MAX_PASSPORT_PIXELS before
      decoding them.
    - Applies EXIF orientation, then shrinks to fit 600 x 600. Never enlarges.
    - Keeps the format (JPEG stays JPEG, PNG stays PNG, WebP stays WebP) and
      PNG/WebP transparency. Output carries no EXIF (so no GPS/camera data).
    - Keeps the original filename stem; the extension becomes the canonical
      one for the detected format (what the bytes really are, not what the
      browser or user called the file).
    - For an image that is already within bounds and upright AND has no EXIF
      metadata, the re-encoded result is used only if it is actually smaller;
      otherwise the original bytes are kept, so optimisation can never make
      such a file worse.
    - If the upload has EXIF metadata (JPEG, PNG or WebP), the original bytes
      are never returned: the re-encoded, EXIF-free output is always used,
      even when it is slightly larger. Privacy beats a few bytes.

    Raises ValidationError with a user-presentable message.
    """
    try:
        data = file_obj.read()

        with Image.open(io.BytesIO(data)) as original:
            image_format = (original.format or "").upper()
            if image_format not in _SUPPORTED_FORMATS:
                raise ValidationError(
                    MSG_UNSUPPORTED_FORMAT, code="passport_unsupported_format"
                )

            width, height = original.size
            if width * height > MAX_PASSPORT_PIXELS[image_format]:
                raise ValidationError(MSG_TOO_LARGE, code="passport_too_large")

            exif = original.getexif()
            orientation = exif.get(_EXIF_ORIENTATION_TAG, 1)
            has_exif = _has_exif(original, exif)
            needs_resize = (
                width > MAX_PASSPORT_DIMENSION[0]
                or height > MAX_PASSPORT_DIMENSION[1]
            )
            needs_rotation = orientation not in (None, 1)

            if image_format == "JPEG" and needs_resize:
                # Let libjpeg decode at 1/2, 1/4 or 1/8 scale. A 12 MP phone
                # photo is then decoded at ~1-3 MP: far less CPU and RAM,
                # and the quality is fine because we shrink it anyway.
                original.draft(None, MAX_PASSPORT_DIMENSION)

            # exif_transpose() always returns a full-size COPY. Only pay for
            # that copy when the photo really is rotated; otherwise resize
            # the opened image directly (halves peak RAM for PNG/WebP).
            image = ImageOps.exif_transpose(original) if needs_rotation else original

            if needs_resize and image.mode in ("P", "1"):
                # Pillow resizes palette images with nearest-neighbour,
                # which looks blocky. Convert first (keeping any
                # transparency), then resize properly.
                image = image.convert(
                    "RGBA" if "transparency" in image.info else "RGB"
                )

            image.thumbnail(MAX_PASSPORT_DIMENSION, Image.Resampling.LANCZOS)

            extension, content_type = _SUPPORTED_FORMATS[image_format]
            reencoded = _encode(image, image_format)

            unchanged_geometry = not (needs_resize or needs_rotation)
            jpeg_needs_conversion = (
                image_format == "JPEG" and original.mode not in ("RGB", "L")
            )
            # The "keep the original if re-encoding is bigger" shortcut returns
            # the uploaded bytes untouched, so it is only safe when those bytes
            # carry no EXIF (GPS position, camera and device details). If EXIF
            # is present we always use the re-encoded output, which has none,
            # even when it is slightly larger.
            if (
                unchanged_geometry
                and not jpeg_needs_conversion
                and not has_exif
                and len(reencoded) >= len(data)
            ):
                content = data
            else:
                content = reencoded

        stem = Path(filename or "").stem or "passport"
        return OptimisedPassport(f"{stem}{extension}", content, content_type)

    except ValidationError:
        raise
    except (Image.DecompressionBombError, MemoryError) as exc:
        raise ValidationError(MSG_TOO_LARGE, code="passport_too_large") from exc
    except Exception as exc:  # Pillow raises OSError, SyntaxError, ValueError...
        logger.warning("Passport photo could not be processed: %r", exc)
        raise ValidationError(MSG_UNREADABLE, code="passport_unreadable") from exc


def _has_exif(image, exif=None):
    """
    True if an opened JPEG, PNG or WebP image carries EXIF metadata.

    ``exif`` is the result of ``image.getexif()`` if the caller already has
    it. ``getexif()`` is called first on purpose: for PNG, it loads the image
    so that an ``eXIf`` chunk stored after the pixel data is seen too.

    Checks, in order:
    * parsed EXIF tags (JPEG APP1, PNG eXIf chunk, WebP EXIF chunk);
    * the raw EXIF block in ``image.info["exif"]``, which catches an EXIF
      segment that is present but has no readable tags;
    * the older PNG text-chunk form, "Raw profile type exif".
    """
    if exif is None:
        exif = image.getexif()
    return bool(
        len(exif)
        or image.info.get("exif")
        or image.info.get("Raw profile type exif")
    )


def _encode(image, image_format):
    output = io.BytesIO()

    if image_format == "JPEG":
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(
            output,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
    elif image_format == "PNG":
        image.save(output, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
    else:
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        image.save(output, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)

    return output.getvalue()


def _is_prepared(field_file):
    # Look at the private _file attribute on purpose: the public ``.file``
    # property would open a stored file from disk if there is none in memory.
    return bool(getattr(getattr(field_file, "_file", None), _PREPARED_FLAG, False))


def prepare_passport_upload(instance, field_name="passport_photo", max_bytes=None):
    """
    Validate and optimise a freshly uploaded passport, in memory only.

    Replaces the upload on ``instance`` with an optimised in-memory file
    (tagged so ``optimise_passport_photo`` will not repeat the work). Writes
    nothing to storage. Does nothing for an empty field, an already stored
    file, or an upload that has already been prepared.

    ``max_bytes``: if the upload is bigger, skip -- the model's own size
    validator already reports that, and we must not spend CPU decoding it.

    Raises ValidationError (used by Member.clean() to build a field error).
    """
    field_file = getattr(instance, field_name)

    if not field_file or field_file._committed or _is_prepared(field_file):
        return
    if max_bytes is not None and field_file.size > max_bytes:
        return

    field_file.open("rb")
    result = build_optimised_passport(field_file, field_file.name)

    upload = SimpleUploadedFile(
        result.name, result.content, content_type=result.content_type
    )
    setattr(upload, _PREPARED_FLAG, True)
    setattr(instance, field_name, upload)


def optimise_passport_photo(image_field):
    """
    Optimise a newly uploaded passport photograph and store it.

    Used by Member.save(). Does nothing if the field is empty, already
    stored, or was already prepared by Member.clean(). Raises
    ValidationError if the image is unusable (only reachable by code that
    skipped form validation).
    """
    if not image_field or image_field._committed or _is_prepared(image_field):
        return

    image_field.open("rb")
    result = build_optimised_passport(image_field, image_field.name)

    image_field.save(result.name, ContentFile(result.content), save=False)
