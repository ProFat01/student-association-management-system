"""
Tests for passport photograph optimisation.

Layout
------
* PassportImageServiceTests   -- the image functions in isolation
* MemberSaveIntegrationTests  -- the real Member.save() / Member.clean()
* RegistrationAndAdminTests   -- public form and Django admin: bad images
                                 give normal form errors, never HTTP 500
"""

import io
import os
import shutil
import tempfile
from unittest import mock

from PIL import Image, ImageDraw, PngImagePlugin

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.forms import modelform_factory
from django.test import override_settings
from django.urls import reverse

from apps.core.models import Association
from apps.members import image_services
from apps.members.image_services import (
    MAX_PASSPORT_DIMENSION,
    optimise_passport_photo,
)
from apps.members.models import Member

from .helpers import MediaIsolatedTestCase


class PerTestMediaTestCase(MediaIsolatedTestCase):
    """
    MediaIsolatedTestCase keeps one temporary media folder for a whole test
    class, which is fine for most tests. These tests count the files on disk,
    so each test here gets its own empty folder instead.
    """

    def setUp(self):
        super().setUp()
        self._test_media_root = tempfile.mkdtemp(prefix="sams_img_test_")
        self.addCleanup(shutil.rmtree, self._test_media_root, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=self._test_media_root))

    def stored_files(self):
        return sorted(
            os.path.join(root, name)
            for root, _dirs, names in os.walk(self._test_media_root)
            for name in names
        )

EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
CONTENT_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def image_bytes(image_format, size, mode="RGB", exif_orientation=None, **save_kwargs):
    buffer = io.BytesIO()
    image = Image.new(mode, size, color="white")
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        exif[0x010F] = "TestCamera"  # a tag we expect to be stripped
        save_kwargs["exif"] = exif.tobytes()
    image.save(buffer, format=image_format, **save_kwargs)
    return buffer.getvalue()


def photo_like_bytes(size, quality=90):
    """A JPEG with real texture so compression behaves like a photo."""
    noise = Image.effect_noise((max(size[0] // 4, 1), max(size[1] // 4, 1)), 40)
    image = noise.convert("RGB").resize(size, Image.Resampling.BICUBIC)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def _textured_image(size):
    noise = Image.effect_noise((max(size[0] // 4, 1), max(size[1] // 4, 1)), 40)
    return noise.convert("RGB").resize(size, Image.Resampling.BICUBIC)


def small_upright_jpeg_with_camera_exif(size=(300, 300), quality=30):
    """
    A small, upright (no orientation tag) JPEG that is already compressed hard
    and carries camera EXIF. At quality 30 the service's own re-encode
    (quality 82, progressive) is roughly 30-50% larger.
    """
    exif = Image.Exif()
    exif[0x010F] = "TestCamera"        # Make
    exif[0x0110] = "TestModel-X1"      # Model
    buffer = io.BytesIO()
    _textured_image(size).save(
        buffer, format="JPEG", quality=quality, optimize=True, exif=exif.tobytes()
    )
    return buffer.getvalue()


def small_upright_jpeg_without_exif(size=(300, 300), quality=30):
    buffer = io.BytesIO()
    _textured_image(size).save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def small_image_with_exif(image_format, size=(100, 100)):
    exif = Image.Exif()
    exif[0x010F] = "TestCamera"
    exif[0x0110] = "TestModel-X1"
    return image_bytes(image_format, size, exif=exif.tobytes())


def assert_no_exif(test_case, data):
    """No parsed EXIF tags and no raw EXIF marker anywhere in the file bytes."""
    with Image.open(io.BytesIO(data)) as img:
        test_case.assertEqual(dict(img.getexif()), {})
        test_case.assertFalse(img.info.get("exif"))
    test_case.assertNotIn(b"Exif\x00\x00", data)
    test_case.assertNotIn(b"TestCamera", data)
    test_case.assertNotIn(b"TestModel-X1", data)


def make_upload(image_format="JPEG", size=(1200, 800), name=None, mode="RGB", **kwargs):
    name = name or f"passport.{EXTENSIONS[image_format]}"
    return SimpleUploadedFile(
        name,
        image_bytes(image_format, size, mode=mode, **kwargs),
        content_type=CONTENT_TYPES[image_format],
    )


def read_stored(name):
    with default_storage.open(name, "rb") as stored:
        return stored.read()


def stored_image_info(name):
    with Image.open(io.BytesIO(read_stored(name))) as image:
        return {"size": image.size, "format": image.format, "mode": image.mode, "exif": dict(image.getexif())}


# ---------------------------------------------------------------------------
# 1. The image functions on their own
# ---------------------------------------------------------------------------
class PassportImageServiceTests(MediaIsolatedTestCase):
    def passport_field(self, upload):
        """An uncommitted FieldFile, exactly what Member.save() receives."""
        return Member(passport_photo=upload).passport_photo

    def optimise(self, upload):
        field = self.passport_field(upload)
        optimise_passport_photo(field)
        return field

    def test_large_jpeg_is_resized_to_maximum_600_pixels(self):
        field = self.optimise(make_upload("JPEG", (2400, 1600)))
        info = stored_image_info(field.name)
        self.assertEqual(info["size"], (600, 400))
        self.assertEqual(info["format"], "JPEG")

    def test_portrait_jpeg_is_limited_by_height(self):
        field = self.optimise(make_upload("JPEG", (1500, 2000)))
        self.assertEqual(stored_image_info(field.name)["size"], (450, 600))

    def test_small_image_is_not_enlarged(self):
        field = self.optimise(make_upload("JPEG", (200, 100)))
        self.assertEqual(stored_image_info(field.name)["size"], (200, 100))

    def test_image_exactly_at_the_limit_is_left_at_that_size(self):
        field = self.optimise(make_upload("PNG", MAX_PASSPORT_DIMENSION))
        self.assertEqual(stored_image_info(field.name)["size"], (600, 600))

    def test_png_format_is_preserved(self):
        field = self.optimise(make_upload("PNG", (1200, 800)))
        info = stored_image_info(field.name)
        self.assertEqual(info["size"], (600, 400))
        self.assertEqual(info["format"], "PNG")
        self.assertTrue(field.name.endswith(".png"))

    def test_png_transparency_is_preserved(self):
        image = Image.new("RGBA", (1200, 800), (255, 0, 0, 255))
        ImageDraw.Draw(image).rectangle((0, 0, 199, 199), fill=(0, 0, 0, 0))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        field = self.optimise(
            SimpleUploadedFile("transparent.png", buffer.getvalue(), content_type="image/png")
        )

        with Image.open(io.BytesIO(read_stored(field.name))) as result:
            self.assertEqual(result.format, "PNG")
            self.assertEqual(result.size, (600, 400))
            self.assertEqual(result.mode, "RGBA")
            self.assertEqual(result.getpixel((40, 40))[3], 0)      # still transparent
            self.assertEqual(result.getpixel((400, 300))[3], 255)  # still opaque

    def test_palette_png_with_transparency_keeps_transparency_after_resize(self):
        palette = Image.new("P", (1200, 800), 1)
        palette.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
        ImageDraw.Draw(palette).rectangle((0, 0, 399, 399), fill=0)
        buffer = io.BytesIO()
        palette.save(buffer, format="PNG", transparency=0)

        field = self.optimise(
            SimpleUploadedFile("palette.png", buffer.getvalue(), content_type="image/png")
        )

        with Image.open(io.BytesIO(read_stored(field.name))) as result:
            self.assertEqual(result.size, (600, 400))
            rgba = result.convert("RGBA")
            self.assertEqual(rgba.getpixel((50, 50))[3], 0)
            self.assertEqual(rgba.getpixel((500, 350))[3], 255)

    def test_webp_format_is_preserved(self):
        field = self.optimise(make_upload("WEBP", (1200, 800)))
        info = stored_image_info(field.name)
        self.assertEqual(info["size"], (600, 400))
        self.assertEqual(info["format"], "WEBP")
        self.assertTrue(field.name.endswith(".webp"))

    def test_exif_orientation_is_applied_before_resizing_and_metadata_removed(self):
        # Stored as 1600x1200 landscape, but tagged "rotate 90 degrees".
        upload = SimpleUploadedFile(
            "rotated.jpg",
            image_bytes("JPEG", (1600, 1200), exif_orientation=6),
            content_type="image/jpeg",
        )
        field = self.optimise(upload)
        info = stored_image_info(field.name)

        self.assertEqual(info["size"], (450, 600))        # now portrait, fits 600 high
        self.assertNotIn(0x0112, info["exif"])            # orientation tag gone
        self.assertNotIn(0x010F, info["exif"])            # camera tag gone

    def test_filename_stem_is_preserved_and_extension_follows_real_format(self):
        field = self.optimise(make_upload("JPEG", (900, 900), name="ada_lovelace.JPEG"))
        self.assertEqual(os.path.basename(field.name), "ada_lovelace.jpg")

    def test_file_labelled_png_but_containing_jpeg_is_stored_as_jpeg(self):
        # What register.js produces: it re-encodes to JPEG but keeps the name.
        upload = SimpleUploadedFile(
            "photo.png", image_bytes("JPEG", (1200, 800)), content_type="image/png"
        )
        field = self.optimise(upload)
        self.assertEqual(os.path.basename(field.name), "photo.jpg")
        self.assertEqual(stored_image_info(field.name)["format"], "JPEG")

    def test_optimisation_never_makes_an_already_small_file_bigger(self):
        original = image_bytes("JPEG", (300, 300), quality=30)
        upload = SimpleUploadedFile("small.jpg", original, content_type="image/jpeg")
        field = self.optimise(upload)
        self.assertLessEqual(len(read_stored(field.name)), len(original))

    # -- EXIF privacy: the "keep the original if re-encoding is bigger" -------
    # -- fallback must never hand back the uploaded bytes when they carry EXIF.

    def test_small_upright_jpeg_with_exif_is_reencoded_even_when_larger(self):
        """
        Regression test for the EXIF leak in the size fallback.

        The upload is small (inside 600x600), upright (no orientation tag), and
        already tightly compressed, so re-encoding it at the service's quality
        settings makes it BIGGER. Before the fix the original bytes were stored,
        camera EXIF and all.
        """
        original = small_upright_jpeg_with_camera_exif()

        # Preconditions that make this the exact scenario from the review.
        with Image.open(io.BytesIO(original)) as probe:
            self.assertLessEqual(probe.size[0], MAX_PASSPORT_DIMENSION[0])
            self.assertLessEqual(probe.size[1], MAX_PASSPORT_DIMENSION[1])
            self.assertNotIn(0x0112, probe.getexif())          # upright
            self.assertEqual(probe.getexif().get(0x010F), "TestCamera")
            self.assertEqual(probe.getexif().get(0x0110), "TestModel-X1")
            reencoded = image_services._encode(probe, "JPEG")
        self.assertGreater(len(reencoded), len(original))       # re-encode is larger

        field = self.optimise(
            SimpleUploadedFile("camera.jpg", original, content_type="image/jpeg")
        )

        stored = read_stored(field.name)
        self.assertNotEqual(stored, original)                   # not the original bytes
        self.assertGreater(len(stored), len(original))          # larger one was used
        self.assertEqual(stored_image_info(field.name)["exif"], {})
        assert_no_exif(self, stored)

    def test_build_optimised_passport_returns_exif_free_bytes_for_small_exif_jpeg(self):
        original = small_upright_jpeg_with_camera_exif()
        result = image_services.build_optimised_passport(
            io.BytesIO(original), "camera.jpg"
        )
        self.assertNotEqual(result.content, original)
        assert_no_exif(self, result.content)
        self.assertEqual(result.name, "camera.jpg")
        self.assertEqual(result.content_type, "image/jpeg")

    def test_exif_is_detected_in_jpeg_png_and_webp(self):
        for image_format in ("JPEG", "PNG", "WEBP"):
            with self.subTest(image_format=image_format):
                with Image.open(io.BytesIO(small_image_with_exif(image_format))) as img:
                    self.assertTrue(image_services._has_exif(img))
                with Image.open(io.BytesIO(image_bytes(image_format, (100, 100)))) as img:
                    self.assertFalse(image_services._has_exif(img))

    def test_png_with_text_chunk_style_exif_is_detected(self):
        exif_block = Image.Exif()
        exif_block[0x010F] = "TestCamera"
        info = PngImagePlugin.PngInfo()
        info.add_text(
            "Raw profile type exif",
            "\nexif\n%8d\n%s\n" % (len(exif_block.tobytes()), exif_block.tobytes().hex()),
        )
        data = image_bytes("PNG", (100, 100), pnginfo=info)
        with Image.open(io.BytesIO(data)) as img:
            self.assertTrue(image_services._has_exif(img))

    def test_exif_segment_without_readable_tags_is_still_treated_as_exif(self):
        data = image_bytes("JPEG", (100, 100), exif=Image.Exif().tobytes())
        with Image.open(io.BytesIO(data)) as img:
            self.assertTrue(image_services._has_exif(img))

    def test_exif_is_removed_from_png_and_webp_uploads(self):
        for image_format in ("PNG", "WEBP"):
            with self.subTest(image_format=image_format):
                original = small_image_with_exif(image_format)
                result = image_services.build_optimised_passport(
                    io.BytesIO(original), f"photo.{EXTENSIONS[image_format]}"
                )
                with Image.open(io.BytesIO(result.content)) as img:
                    self.assertEqual(img.format, image_format)
                    self.assertEqual(dict(img.getexif()), {})
                    self.assertFalse(img.info.get("exif"))

    def test_small_upright_jpeg_without_exif_keeps_the_size_fallback(self):
        """Images with NO EXIF still get the original bytes back if re-encoding is bigger."""
        original = small_upright_jpeg_without_exif()
        with Image.open(io.BytesIO(original)) as probe:
            self.assertFalse(image_services._has_exif(probe))
            reencoded = image_services._encode(probe, "JPEG")
        self.assertGreaterEqual(len(reencoded), len(original))  # precondition

        result = image_services.build_optimised_passport(
            io.BytesIO(original), "plain.jpg"
        )
        self.assertEqual(result.content, original)

    def test_large_upload_becomes_much_smaller_on_disk(self):
        original = photo_like_bytes((3120, 4160))
        upload = SimpleUploadedFile("big.jpg", original, content_type="image/jpeg")
        field = self.optimise(upload)
        self.assertEqual(stored_image_info(field.name)["size"], (450, 600))
        self.assertLess(len(read_stored(field.name)), len(original) * 0.2)

    def test_existing_committed_image_is_not_reprocessed(self):
        field = self.passport_field(make_upload("JPEG", (1200, 800)))
        stored_content = b"existing stored image content"
        field.save("existing-passport.jpg", ContentFile(stored_content), save=False)
        stored_name = field.name

        optimise_passport_photo(field)

        self.assertEqual(field.name, stored_name)
        self.assertEqual(read_stored(field.name), stored_content)

    def test_empty_field_is_ignored(self):
        optimise_passport_photo(Member().passport_photo)  # must not raise

    # -- invalid input ------------------------------------------------------
    def assertRejected(self, upload, code):
        field = self.passport_field(upload)
        with self.assertRaises(ValidationError) as caught:
            optimise_passport_photo(field)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(field._committed, "nothing may be stored for a rejected image")

    def test_garbage_bytes_are_rejected_with_validation_error(self):
        self.assertRejected(
            SimpleUploadedFile("bad.jpg", b"definitely not an image", content_type="image/jpeg"),
            "passport_unreadable",
        )

    def test_truncated_jpeg_is_rejected_with_validation_error(self):
        good = photo_like_bytes((800, 800))
        self.assertRejected(
            SimpleUploadedFile("cut.jpg", good[: len(good) // 2], content_type="image/jpeg"),
            "passport_unreadable",
        )

    def test_unsupported_but_valid_image_format_is_rejected(self):
        self.assertRejected(
            SimpleUploadedFile("anim.gif", image_bytes("GIF", (50, 50)), content_type="image/gif"),
            "passport_unsupported_format",
        )

    def test_image_above_pixel_limit_is_rejected_before_decoding(self):
        upload = make_upload("JPEG", (200, 100))  # 20,000 pixels
        with mock.patch.dict(image_services.MAX_PASSPORT_PIXELS, {"JPEG": 10_000}):
            self.assertRejected(upload, "passport_too_large")

    def test_png_and_webp_have_their_own_tighter_limit_than_jpeg(self):
        limits = {"JPEG": 1_000_000, "PNG": 10_000, "WEBP": 10_000}   # 200x100 = 20,000 px
        with mock.patch.dict(image_services.MAX_PASSPORT_PIXELS, limits):
            self.assertRejected(make_upload("PNG", (200, 100)), "passport_too_large")
            self.assertRejected(make_upload("WEBP", (200, 100)), "passport_too_large")
            self.assertTrue(self.optimise(make_upload("JPEG", (200, 100)))._committed)

    def test_default_limits_are_tighter_for_formats_that_decode_in_full(self):
        limits = image_services.MAX_PASSPORT_PIXELS
        self.assertEqual(set(limits), {"JPEG", "PNG", "WEBP"})
        self.assertLessEqual(limits["PNG"], 25_000_000)
        self.assertLessEqual(limits["WEBP"], 25_000_000)
        self.assertGreater(limits["JPEG"], limits["PNG"])

    def test_image_just_inside_pixel_limit_is_accepted(self):
        upload = make_upload("JPEG", (200, 100))
        with mock.patch.dict(image_services.MAX_PASSPORT_PIXELS, {"JPEG": 20_000}):
            field = self.optimise(upload)
        self.assertTrue(field._committed)

    def test_encoder_failure_midway_is_a_validation_error_and_stores_nothing(self):
        with mock.patch.object(image_services, "_encode", side_effect=OSError("encoder died")):
            self.assertRejected(make_upload("JPEG", (1200, 800)), "passport_unreadable")

    def test_decompression_bomb_error_becomes_validation_error(self):
        upload = make_upload("PNG", (50, 50))
        with mock.patch.object(
            image_services.Image, "open", side_effect=Image.DecompressionBombError("boom")
        ):
            self.assertRejected(upload, "passport_too_large")


# ---------------------------------------------------------------------------
# 2. The real Member model
# ---------------------------------------------------------------------------
class MemberSaveIntegrationTests(PerTestMediaTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(
            name="Malam Sidi Students Association", short_name="MSA", slug="msa"
        )

    def member_kwargs(self, **overrides):
        data = dict(
            association=self.association,
            full_name="Aisha Bello",
            phone_number="08012345678",
            nin_number="12345678901",
            date_of_birth="2002-05-14",
            institution="Malam Sidi College",
            course="Computer Science",
            category=Member.Category.UNDERGRADUATE,
        )
        data.update(overrides)
        return data

    def create_member(self, **overrides):
        overrides.setdefault("passport_photo", make_upload("JPEG", (2400, 1600), name="large-passport.jpg"))
        return Member.objects.create(**self.member_kwargs(**overrides))

    def test_member_save_optimises_a_new_upload(self):
        member = self.create_member()
        member.refresh_from_db()

        info = stored_image_info(member.passport_photo.name)
        self.assertEqual(info["size"], (600, 400))
        self.assertEqual(info["format"], "JPEG")
        self.assertTrue(member.passport_photo.name.startswith("members/passports/"))
        self.assertEqual(len(self.stored_files()), 1, "exactly one file, no leftover original")

    def test_existing_business_logic_still_runs_alongside_optimisation(self):
        member = self.create_member(approval_status=Member.ApprovalStatus.APPROVED)
        member.refresh_from_db()

        self.assertEqual(stored_image_info(member.passport_photo.name)["size"], (600, 400))
        self.assertTrue(member.membership_id and member.membership_id.startswith("MSA-"))
        self.assertTrue(member.voting_status)

    def test_deliberate_voting_suspension_is_still_respected(self):
        member = self.create_member(approval_status=Member.ApprovalStatus.APPROVED)
        member.voting_status = False
        member.save(update_fields=["voting_status"])
        member.refresh_from_db()
        self.assertFalse(member.voting_status)

    def test_updating_unrelated_fields_does_not_touch_the_stored_passport(self):
        member = self.create_member()
        name_before = member.passport_photo.name
        content_before = read_stored(name_before)
        files_before = self.stored_files()

        member.full_name = "Aisha Bello Updated"
        member.save()                                   # full save, no update_fields
        member.refresh_from_db()
        member.full_name = "Aisha Again"
        member.save(update_fields=["full_name"])        # restricted save
        member.refresh_from_db()

        self.assertEqual(member.passport_photo.name, name_before)
        self.assertEqual(read_stored(name_before), content_before)
        self.assertEqual(self.stored_files(), files_before)

    def test_stored_photo_is_not_decoded_again_on_later_saves(self):
        member = self.create_member()
        member.refresh_from_db()
        with mock.patch.object(image_services, "build_optimised_passport") as build:
            member.full_name = "Changed"
            member.save()
        build.assert_not_called()

    def test_update_fields_without_passport_photo_never_processes_a_pending_upload(self):
        member = self.create_member()
        stored_before = self.stored_files()

        member.passport_photo = make_upload("JPEG", (2400, 1600), name="replacement.jpg")
        member.full_name = "Only The Name"
        with mock.patch.object(image_services, "build_optimised_passport") as build:
            member.save(update_fields=["full_name"])

        build.assert_not_called()
        self.assertEqual(self.stored_files(), stored_before)
        self.assertFalse(member.passport_photo._committed)
        member.refresh_from_db()
        self.assertEqual(member.full_name, "Only The Name")

    def test_update_fields_including_passport_photo_processes_the_new_upload(self):
        member = self.create_member()
        member.passport_photo = make_upload("JPEG", (1500, 2000), name="replacement.jpg")
        member.save(update_fields=["passport_photo"])
        member.refresh_from_db()

        self.assertEqual(stored_image_info(member.passport_photo.name)["size"], (450, 600))
        self.assertIn("replacement", member.passport_photo.name)

    def test_invalid_image_in_save_raises_validation_error_and_stores_nothing(self):
        bad = SimpleUploadedFile("bad.jpg", b"not an image", content_type="image/jpeg")
        with self.assertRaises(ValidationError):
            Member.objects.create(**self.member_kwargs(passport_photo=bad))
        self.assertEqual(self.stored_files(), [])
        self.assertFalse(Member.objects.exists())

    def test_full_clean_reports_a_bad_image_as_a_passport_photo_field_error(self):
        bad = SimpleUploadedFile("bad.jpg", b"not an image", content_type="image/jpeg")
        member = Member(**self.member_kwargs(passport_photo=bad))
        with self.assertRaises(ValidationError) as caught:
            member.full_clean(exclude=["membership_id"], validate_unique=False)
        self.assertIn("passport_photo", caught.exception.message_dict)
        self.assertEqual(self.stored_files(), [], "validation must not write files")

    def test_clean_optimises_in_memory_and_save_does_not_repeat_the_work(self):
        member = Member(**self.member_kwargs(passport_photo=make_upload("JPEG", (2400, 1600))))
        with mock.patch.object(
            image_services, "build_optimised_passport", wraps=image_services.build_optimised_passport
        ) as build:
            member.full_clean(exclude=["membership_id"], validate_unique=False)
            self.assertEqual(self.stored_files(), [], "clean() must not write to disk")
            member.save()
        self.assertEqual(build.call_count, 1)
        self.assertEqual(stored_image_info(member.passport_photo.name)["size"], (600, 400))

    # -- the "already prepared" marker --------------------------------------
    def test_prepared_marker_survives_the_imagefield_descriptor(self):
        member = Member(**self.member_kwargs(passport_photo=make_upload("JPEG", (2400, 1600))))
        image_services.prepare_passport_upload(member)

        first = member.passport_photo
        second = member.passport_photo
        self.assertIs(first, second, "descriptor must hand back the same FieldFile each time")
        self.assertFalse(first._committed)
        self.assertIsInstance(first._file, SimpleUploadedFile)
        self.assertTrue(image_services._is_prepared(first))

        # Re-assigning the in-memory file (what Django's form code does) keeps it.
        member.passport_photo = first._file
        self.assertTrue(image_services._is_prepared(member.passport_photo))

        # A brand-new, untouched upload is NOT considered prepared.
        fresh = Member(**self.member_kwargs(passport_photo=make_upload("JPEG", (2400, 1600))))
        self.assertFalse(image_services._is_prepared(fresh.passport_photo))

    def test_full_clean_twice_then_save_still_decodes_only_once(self):
        member = Member(**self.member_kwargs(passport_photo=make_upload("JPEG", (2400, 1600))))
        with mock.patch.object(
            image_services, "build_optimised_passport", wraps=image_services.build_optimised_passport
        ) as build:
            member.full_clean(exclude=["membership_id"], validate_unique=False)
            member.full_clean(exclude=["membership_id"], validate_unique=False)
            member.save()
        self.assertEqual(build.call_count, 1)

    def test_save_without_prior_clean_decodes_exactly_once(self):
        with mock.patch.object(
            image_services, "build_optimised_passport", wraps=image_services.build_optimised_passport
        ) as build:
            self.create_member()
        self.assertEqual(build.call_count, 1)

    # -- no orphaned files ---------------------------------------------------
    def test_database_failure_after_upload_leaves_no_new_file_programmatic_save(self):
        self.create_member()
        before = self.stored_files()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Member.objects.create(**self.member_kwargs(
                nin_number="22222222222",                      # phone number is a duplicate
                passport_photo=make_upload("JPEG", (2400, 1600), name="dup.jpg"),
            ))
        self.assertEqual(self.stored_files(), before)

    def test_database_failure_after_form_style_validation_leaves_no_new_file(self):
        self.create_member()
        before = self.stored_files()
        member = Member(**self.member_kwargs(
            nin_number="33333333333",
            passport_photo=make_upload("JPEG", (2400, 1600), name="race.jpg"),
        ))
        # validate_unique=False reproduces a race: the form saw no duplicate.
        member.full_clean(exclude=["membership_id"], validate_unique=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            member.save()
        self.assertEqual(self.stored_files(), before)

    def test_failed_replacement_keeps_the_old_photo_and_leaves_no_new_file(self):
        first = self.create_member()
        second = self.create_member(phone_number="08099999999", nin_number="99999999999",
                                    passport_photo=make_upload("PNG", (300, 300), name="second.png"))
        old_name = second.passport_photo.name
        before = self.stored_files()

        second.phone_number = first.phone_number              # will violate UNIQUE
        second.passport_photo = make_upload("JPEG", (2400, 1600), name="replacement.jpg")
        with self.assertRaises(IntegrityError), transaction.atomic():
            second.save()

        self.assertEqual(self.stored_files(), before)
        self.assertTrue(default_storage.exists(old_name), "the previously stored photo must survive")

    def test_failed_save_with_no_new_upload_never_deletes_the_stored_photo(self):
        first = self.create_member()
        second = self.create_member(phone_number="08099999999", nin_number="99999999999")
        stored = second.passport_photo.name
        second.refresh_from_db()
        second.phone_number = first.phone_number
        with self.assertRaises(IntegrityError), transaction.atomic():
            second.save()
        self.assertTrue(default_storage.exists(stored))

    def test_processing_error_during_save_writes_nothing(self):
        with mock.patch.object(image_services, "_encode", side_effect=OSError("boom")):
            with self.assertRaises(ValidationError):
                self.create_member()
        self.assertEqual(self.stored_files(), [])

    def test_a_modelform_shows_an_error_for_a_bad_image(self):
        # The Django admin builds its form the same way modelform_factory does.
        Form = modelform_factory(Member, fields=["full_name", "passport_photo"])
        good = photo_like_bytes((800, 800))
        form = Form(
            {"full_name": "X"},
            {"passport_photo": SimpleUploadedFile("cut.jpg", good[: len(good) // 2], content_type="image/jpeg")},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("passport_photo", form.errors)


# ---------------------------------------------------------------------------
# 3. Public registration and Django admin
# ---------------------------------------------------------------------------
def registration_data(**overrides):
    data = {
        "full_name": "Aisha Bello",
        "phone_number": "08012345678",
        "nin_number": "12345678901",
        "date_of_birth": "2002-05-14",
        "gender": Member.Gender.FEMALE,
        "institution": "Gombe State University (GSU), Tudun Wada",
        "course": "Computer Science",
        "category": Member.Category.UNDERGRADUATE,
        "passport_photo": make_upload("JPEG", (2400, 1600), name="photo.jpg"),
        "receipt_image": make_upload("PNG", (10, 10), name="receipt.png"),
    }
    data.update(overrides)
    return data


@override_settings(DEFAULT_ASSOCIATION_SLUG="msa")
class RegistrationAndAdminTests(PerTestMediaTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(
            name="Malam Sidi Students Association", short_name="MSA", slug="msa"
        )

    def passport_files(self):
        return [f for f in self.stored_files() if f"{os.sep}passports{os.sep}" in f]

    def test_registration_with_large_photo_stores_an_optimised_passport(self):
        response = self.client.post(reverse("members:register"), registration_data())
        self.assertEqual(response.status_code, 302)

        member = Member.objects.get()
        self.assertEqual(stored_image_info(member.passport_photo.name)["size"], (600, 400))
        self.assertEqual(len(self.passport_files()), 1)

    def test_registration_decodes_the_photo_only_once(self):
        with mock.patch.object(
            image_services, "build_optimised_passport", wraps=image_services.build_optimised_passport
        ) as build:
            response = self.client.post(reverse("members:register"), registration_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(build.call_count, 1)

    def test_registration_with_truncated_jpeg_is_a_form_error_not_a_500(self):
        good = photo_like_bytes((800, 800))
        broken = SimpleUploadedFile("cut.jpg", good[: len(good) // 2], content_type="image/jpeg")

        response = self.client.post(
            reverse("members:register"), registration_data(passport_photo=broken)
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("passport_photo", response.context["form"].errors)
        self.assertContains(response, "could not be processed")
        self.assertFalse(Member.objects.exists())
        self.assertEqual(self.passport_files(), [])

    def test_registration_with_unsupported_format_is_a_form_error(self):
        gif = SimpleUploadedFile("anim.gif", image_bytes("GIF", (50, 50)), content_type="image/gif")
        response = self.client.post(
            reverse("members:register"), registration_data(passport_photo=gif)
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "JPEG, PNG or WebP")
        self.assertFalse(Member.objects.exists())

    def test_registration_with_image_over_pixel_limit_is_a_form_error(self):
        with mock.patch.dict(image_services.MAX_PASSPORT_PIXELS, {"JPEG": 10_000}):
            response = self.client.post(reverse("members:register"), registration_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "too large in dimensions")
        self.assertFalse(Member.objects.exists())

    def test_registration_still_rejects_files_over_5_mb(self):
        huge = SimpleUploadedFile("huge.jpg", photo_like_bytes((300, 300)) + b"0" * (6 * 1024 * 1024),
                                  content_type="image/jpeg")
        with mock.patch.object(image_services, "build_optimised_passport") as build:
            response = self.client.post(
                reverse("members:register"), registration_data(passport_photo=huge)
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("passport_photo", response.context["form"].errors)
        self.assertFalse(Member.objects.exists())
        build.assert_not_called()  # never spend CPU decoding an oversize upload

    def test_failed_registration_leaves_no_passport_file_behind(self):
        # Valid photo, but duplicate phone => the form fails elsewhere.
        Member.objects.create(
            association=self.association, full_name="Existing", phone_number="08012345678",
            nin_number="99999999999", date_of_birth="2000-01-01", institution="X", course="Y",
            category=Member.Category.UNDERGRADUATE,
            passport_photo=make_upload("PNG", (10, 10), name="existing.png"),
        )
        before = self.passport_files()
        response = self.client.post(reverse("members:register"), registration_data())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.passport_files(), before)

    # -- Django admin --------------------------------------------------------
    def admin_add_post(self, passport):
        return {
            "association": self.association.pk,
            "full_name": "Admin Added",
            "date_of_birth": "2001-01-01",
            "gender": "male",
            "email": "",
            "passport_photo": passport,
            "phone_number": "08033333333",
            "nin_number": "33333333333",
            "institution": "X", "course": "Y", "faculty": "", "department": "", "level": "",
            "category": Member.Category.UNDERGRADUATE,
            "approval_status": Member.ApprovalStatus.PENDING,
            "applications-TOTAL_FORMS": "0", "applications-INITIAL_FORMS": "0",
            "applications-MIN_NUM_FORMS": "0", "applications-MAX_NUM_FORMS": "1000",
            "alumni_record-TOTAL_FORMS": "0", "alumni_record-INITIAL_FORMS": "0",
            "alumni_record-MIN_NUM_FORMS": "0", "alumni_record-MAX_NUM_FORMS": "1",
        }

    def login_admin(self):
        user = get_user_model().objects.create_superuser("root", "root@example.com", "pw-12345-long")
        self.client.force_login(user)

    def test_admin_upload_of_a_bad_image_is_a_form_error_not_a_500(self):
        self.login_admin()
        # A truncated JPEG passes Django's own image-field check, so only our
        # optimiser can catch it -- this is the case that used to be a 500.
        good = photo_like_bytes((800, 800))
        bad = SimpleUploadedFile("cut.jpg", good[: len(good) // 2], content_type="image/jpeg")
        response = self.client.post(reverse("admin:members_member_add"), self.admin_add_post(bad))
        self.assertEqual(response.status_code, 200)
        self.assertIn("passport_photo", response.context["adminform"].form.errors)
        self.assertFalse(Member.objects.exists())
        self.assertEqual(self.passport_files(), [])

    def test_admin_upload_of_a_large_photo_is_optimised(self):
        self.login_admin()
        photo = make_upload("JPEG", (2400, 1600), name="admin.jpg")
        response = self.client.post(reverse("admin:members_member_add"), self.admin_add_post(photo))
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["adminform"].form.errors)
        member = Member.objects.get()
        self.assertEqual(stored_image_info(member.passport_photo.name)["size"], (600, 400))

    def test_registration_processing_error_midway_is_a_form_error_and_leaves_nothing(self):
        with mock.patch.object(image_services, "_encode", side_effect=OSError("boom")):
            response = self.client.post(reverse("members:register"), registration_data())
        self.assertEqual(response.status_code, 200)
        self.assertIn("passport_photo", response.context["form"].errors)
        self.assertFalse(Member.objects.exists())
        self.assertEqual(self.passport_files(), [])

    def test_admin_upload_decodes_the_photo_only_once(self):
        self.login_admin()
        with mock.patch.object(
            image_services, "build_optimised_passport", wraps=image_services.build_optimised_passport
        ) as build:
            response = self.client.post(
                reverse("admin:members_member_add"),
                self.admin_add_post(make_upload("JPEG", (2400, 1600), name="admin.jpg")),
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(build.call_count, 1)

    def test_admin_edit_without_a_new_photo_does_not_touch_the_stored_photo(self):
        self.login_admin()
        member = Member.objects.create(
            association=self.association, full_name="Existing", phone_number="08033333333",
            nin_number="33333333333", date_of_birth="2001-01-01", institution="X", course="Y",
            category=Member.Category.UNDERGRADUATE,
            passport_photo=make_upload("JPEG", (2400, 1600), name="existing.jpg"),
        )
        name_before = member.passport_photo.name
        content_before = read_stored(name_before)
        files_before = self.stored_files()

        data = self.admin_add_post(None)
        data.pop("passport_photo")
        data["full_name"] = "Renamed In Admin"
        with mock.patch.object(image_services, "build_optimised_passport") as build:
            response = self.client.post(reverse("admin:members_member_change", args=[member.pk]), data)

        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["adminform"].form.errors)
        build.assert_not_called()
        member.refresh_from_db()
        self.assertEqual(member.full_name, "Renamed In Admin")
        self.assertEqual(member.passport_photo.name, name_before)
        self.assertEqual(read_stored(name_before), content_before)
        self.assertEqual(self.stored_files(), files_before)
