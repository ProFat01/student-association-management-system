"""
Tests for the ``optimise_existing_passports`` management command.

Photographs that were stored before upload-time optimisation existed are
simulated by creating a Member normally and then overwriting its stored
file on disk with a large image. Every test uses its own empty media folder.
"""
import io
import os
import shutil
import stat
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from PIL import Image

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.core.models import Association
from apps.members.management.commands import optimise_existing_passports as command_module
from apps.members.models import Member

from .helpers import MediaIsolatedTestCase


def noisy_image_bytes(size, image_format="JPEG", **save_kwargs):
    """A textured image, so it compresses like a real photograph."""
    noise = Image.effect_noise((max(size[0] // 4, 1), max(size[1] // 4, 1)), 40)
    image = noise.convert("RGB").resize(size, Image.Resampling.BICUBIC)
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **save_kwargs)
    return buffer.getvalue()


def tiny_upload(image_format="JPEG"):
    """A tiny valid upload. Member.save() stores it under the real format's extension."""
    extension = {"JPEG": "jpg", "PNG": "png"}[image_format]
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(buffer, format=image_format)
    return SimpleUploadedFile(f"photo.{extension}", buffer.getvalue())


class OptimiseExistingPassportsTests(MediaIsolatedTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(
            name="Malam Sidi Students Association", short_name="MSA", slug="msa"
        )

    def setUp(self):
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="sams_opt_test_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media_root))
        self.media_root = Path(media_root)
        self._counter = 0

    # -- helpers ---------------------------------------------------------
    def make_member(self, content, image_format="JPEG"):
        """A Member whose stored passport file is overwritten with ``content``."""
        self._counter += 1
        n = self._counter
        member = Member.objects.create(
            association=self.association,
            full_name=f"Member {n}",
            phone_number=f"0801234{n:04d}",
            nin_number=f"1234567{n:04d}",
            date_of_birth="2002-05-14",
            institution="Malam Sidi College",
            course="Computer Science",
            category=Member.Category.UNDERGRADUATE,
            passport_photo=tiny_upload(image_format),
        )
        path = Path(member.passport_photo.path)
        path.write_bytes(content)
        return member, path

    def run_command(self, *args):
        out = StringIO()
        call_command("optimise_existing_passports", *args, stdout=out)
        return out.getvalue()

    def media_files(self):
        return sorted(p for p in self.media_root.rglob("*") if p.is_file())

    def large_jpeg(self):
        return noisy_image_bytes((2400, 1600), quality=95)

    # -- modes -----------------------------------------------------------
    def test_a_mode_must_be_chosen(self):
        with self.assertRaises(CommandError):
            self.run_command()

    def test_dry_run_and_confirm_cannot_be_combined(self):
        with self.assertRaises(CommandError):
            self.run_command("--dry-run", "--confirm")

    def test_limit_must_be_positive(self):
        with self.assertRaises(CommandError):
            self.run_command("--dry-run", "--limit", "0")

    # -- dry run -----------------------------------------------------------
    def test_dry_run_changes_nothing_and_reports_expected_saving(self):
        member, path = self.make_member(self.large_jpeg())
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns
        before_files = self.media_files()
        before_rows = list(Member.objects.values())

        output = self.run_command("--dry-run")

        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self.media_files(), before_files)
        self.assertEqual(list(Member.objects.values()), before_rows)
        self.assertIn("DRY RUN", output)
        self.assertIn("Total photographs discovered:        1", output)
        self.assertIn("Can be optimised:                    1", output)
        self.assertIn("Original total size", output)
        self.assertIn("Expected size after optimisation", output)
        self.assertIn("Actual storage saved:                0 B", output)

    # -- confirmed run -------------------------------------------------------
    def test_confirm_replaces_the_photo_in_place_with_a_smaller_valid_image(self):
        member, path = self.make_member(self.large_jpeg())
        original_size = path.stat().st_size
        original_name = member.passport_photo.name
        rows_before = list(Member.objects.values())

        output = self.run_command("--confirm")

        self.assertEqual(Path(member.passport_photo.path), path, "same path")
        self.assertLess(path.stat().st_size, original_size)
        with Image.open(path) as image:
            image.load()
            self.assertEqual(image.format, "JPEG")
            self.assertLessEqual(max(image.size), 600)
        member.refresh_from_db()
        self.assertEqual(member.passport_photo.name, original_name)
        self.assertEqual(list(Member.objects.values()), rows_before, "database untouched")
        self.assertEqual(len(self.media_files()), 1, "no temp or extra files left behind")
        self.assertIn("Photographs optimised:               1", output)
        self.assertIn("Actual storage saved:", output)
        self.assertNotIn("Actual storage saved:                0 B", output)

    def test_png_photo_stays_a_png_with_the_same_name(self):
        member, path = self.make_member(noisy_image_bytes((1400, 1400), "PNG"), image_format="PNG")
        self.assertEqual(path.suffix, ".png")
        original_size = path.stat().st_size

        self.run_command("--confirm")

        self.assertLess(path.stat().st_size, original_size)
        with Image.open(path) as image:
            self.assertEqual(image.format, "PNG")
            self.assertLessEqual(max(image.size), 600)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_original_file_permissions_are_preserved(self):
        _member, path = self.make_member(self.large_jpeg())
        os.chmod(path, 0o644)

        self.run_command("--confirm")

        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_photo_that_cannot_be_improved_is_left_byte_identical(self):
        already_small = noisy_image_bytes((300, 300), quality=30)
        _member, path = self.make_member(already_small)

        output = self.run_command("--confirm")

        self.assertEqual(path.read_bytes(), already_small)
        self.assertIn("Photographs optimised:               0", output)

    def test_a_second_run_does_not_touch_photos_optimised_by_the_first(self):
        _member, path = self.make_member(self.large_jpeg())
        self.run_command("--confirm")
        after_first = path.read_bytes()

        output = self.run_command("--confirm")

        self.assertEqual(path.read_bytes(), after_first)
        self.assertIn("Photographs optimised:               0", output)

    # -- skipped / failed files ------------------------------------------------
    def test_extension_mismatch_is_skipped_and_reported(self):
        png_data = noisy_image_bytes((1200, 1200), "PNG")
        _member, path = self.make_member(png_data)  # stored as .jpg, now holds PNG data

        output = self.run_command("--confirm")

        self.assertEqual(path.read_bytes(), png_data)
        self.assertIn("extension mismatch", output)
        self.assertIn("Skipped (unsupported/unreadable...): 1", output)

    def test_unreadable_file_is_skipped_and_reported(self):
        junk = b"this is not an image" * 50
        _member, path = self.make_member(junk)

        output = self.run_command("--confirm")

        self.assertEqual(path.read_bytes(), junk)
        self.assertIn("Skipped (unsupported/unreadable...): 1", output)

    def test_unsupported_image_format_is_skipped_and_reported(self):
        gif = io.BytesIO()
        Image.new("P", (50, 50)).save(gif, format="GIF")
        _member, path = self.make_member(gif.getvalue())

        output = self.run_command("--confirm")

        self.assertEqual(path.read_bytes(), gif.getvalue())
        self.assertIn("Skipped (unsupported/unreadable...): 1", output)

    def test_missing_file_is_skipped_and_the_member_record_is_kept(self):
        member, path = self.make_member(self.large_jpeg())
        path.unlink()
        name = member.passport_photo.name

        output = self.run_command("--confirm")

        self.assertIn("file is missing on disk", output)
        member.refresh_from_db()
        self.assertEqual(member.passport_photo.name, name)

    def test_one_bad_photo_does_not_stop_the_others(self):
        _bad, bad_path = self.make_member(b"junk" * 100)
        _good, good_path = self.make_member(self.large_jpeg())
        good_before = good_path.stat().st_size

        self.run_command("--confirm")

        self.assertEqual(bad_path.read_bytes(), b"junk" * 100)
        self.assertLess(good_path.stat().st_size, good_before)

    # -- the optimised result is validated before replacing --------------------
    def _build_then(self, transform):
        real = command_module.build_optimised_passport

        def patched(file_obj, filename):
            result = real(file_obj, filename)
            return type(result)(result.name, transform(result.content), result.content_type)

        return mock.patch.object(command_module, "build_optimised_passport", patched)

    def test_a_corrupt_optimised_image_never_replaces_the_original(self):
        _member, path = self.make_member(self.large_jpeg())
        original = path.read_bytes()

        with self._build_then(lambda content: content[: len(content) // 2]):
            output = self.run_command("--confirm")

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual([p.name for p in self.media_files()], [path.name], "temp file removed")
        self.assertIn("Failures:                            1", output)

    def test_a_result_that_is_not_smaller_never_replaces_the_original(self):
        _member, path = self.make_member(self.large_jpeg())
        original = path.read_bytes()

        with self._build_then(lambda content: original + b"\0" * 5000):
            self.run_command("--confirm")

        self.assertEqual(path.read_bytes(), original)

    def test_a_failed_swap_leaves_the_original_and_no_temp_file(self):
        _member, path = self.make_member(self.large_jpeg())
        original = path.read_bytes()

        with mock.patch.object(command_module.os, "replace", side_effect=OSError("disk error")):
            output = self.run_command("--confirm")

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual([p.name for p in self.media_files()], [path.name])
        self.assertIn("Failures:                            1", output)

    # -- batches -----------------------------------------------------------
    def test_limit_processes_a_batch_and_a_rerun_continues_with_the_rest(self):
        photos = [self.make_member(self.large_jpeg())[1] for _ in range(3)]
        originals = [p.read_bytes() for p in photos]

        def changed():
            return [p.read_bytes() != original for p, original in zip(photos, originals)]

        output = self.run_command("--confirm", "--limit", "2")
        self.assertEqual(sum(changed()), 2)
        self.assertIn("Photographs optimised:               2", output)
        self.assertIn("Not examined", output)

        output = self.run_command("--confirm", "--limit", "2")
        self.assertEqual(sum(changed()), 3)
        self.assertIn("Photographs optimised:               1", output)

        self.run_command("--confirm", "--limit", "2")
        self.assertEqual(sum(changed()), 3)

    def test_dry_run_limit_only_looks_at_the_requested_number(self):
        photos = [self.make_member(self.large_jpeg())[1] for _ in range(3)]

        output = self.run_command("--dry-run", "--limit", "2")

        self.assertIn("Total photographs discovered:        3", output)
        self.assertIn("Can be optimised:                    2", output)
        self.assertTrue(all(p.exists() for p in photos))

    def test_unreferenced_files_are_reported_but_never_touched(self):
        self.make_member(self.large_jpeg())
        orphan = self.media_root / "members" / "passports" / "orphan.jpg"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(self.large_jpeg())
        orphan_before = orphan.read_bytes()

        output = self.run_command("--confirm")

        self.assertEqual(orphan.read_bytes(), orphan_before)
        self.assertIn("not referenced by any member", output)

    def test_no_members_is_reported_cleanly(self):
        output = self.run_command("--dry-run")
        self.assertIn("Total photographs discovered:        0", output)
