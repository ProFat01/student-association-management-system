"""
Optimise passport photographs that are ALREADY stored (uploaded before the
upload-time optimisation existed), to free storage quota.

    python manage.py optimise_existing_passports --dry-run
    python manage.py optimise_existing_passports --dry-run --limit 10
    python manage.py optimise_existing_passports --confirm --limit 10
    python manage.py optimise_existing_passports --confirm

Nothing is changed unless --confirm is given. --dry-run reads files and
reports, and writes nothing at all.

Safety rules (each one is enforced in code and covered by tests):

* The image work is done by the existing ``build_optimised_passport()`` --
  there is no second image pipeline here.
* Member rows are only READ. No database record is ever written, so every
  ``passport_photo`` reference, filename, extension and path stays exactly
  as it is. Files are never deleted or renamed.
* A photograph is replaced only if the optimised version is meaningfully
  smaller (at least MIN_SAVING_BYTES). Otherwise the original is kept: this
  command never makes a file bigger.
* The optimised bytes are written to a temporary file in the same folder,
  re-opened and fully decoded from disk, checked (real image, same format,
  within 600 x 600, really smaller), given the original file's permissions
  and only then swapped in with ``os.replace()`` (atomic). A failure at any
  step leaves the original untouched and removes the temporary file.
* Unsupported, unreadable, missing or extension-mismatched files (for
  example PNG data stored as ``.jpg``) are skipped and listed, never
  modified -- renaming would break the database reference.
* One photograph is held in memory at a time.

``--limit N`` means "optimise at most N photographs in this run". Photographs
that gain nothing (including ones optimised by an earlier run) are skipped
and do not count towards N, so running the same command again simply
continues with the remaining photographs.
"""

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.members.image_services import MAX_PASSPORT_DIMENSION, build_optimised_passport
from apps.members.models import Member

# A photograph is only replaced if this many bytes (or more) are saved.
# Re-encoding an already optimised JPEG can shave a few bytes while adding
# generation loss; 1 KiB keeps such pointless rewrites out.
MIN_SAVING_BYTES = 1024

_EXTENSION_ALIASES = {".jpeg": ".jpg", ".jpe": ".jpg"}
_EXTENSION_FOR_FORMAT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

STATUS_CANDIDATE = "candidate"
STATUS_NO_BENEFIT = "no_benefit"
STATUS_SKIPPED = "skipped"


def _normalise_extension(extension):
    extension = extension.lower()
    return _EXTENSION_ALIASES.get(extension, extension)


def format_bytes(size):
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.2f} {unit}"
        size /= 1024


@dataclass
class Analysis:
    status: str
    original_size: int = 0
    new_size: int = 0
    reason: str = ""
    result: object = None  # OptimisedPassport, only for STATUS_CANDIDATE


@dataclass
class Report:
    discovered: int = 0
    examined: int = 0
    original_total: int = 0
    expected_total: int = 0
    candidates: int = 0
    optimised: int = 0
    actual_saved: int = 0
    no_benefit: int = 0
    skipped: list = field(default_factory=list)   # (name, reason)
    failures: list = field(default_factory=list)  # (name, reason)
    unreferenced_files: int = 0

    @property
    def not_examined(self):
        return self.discovered - self.examined

    @property
    def expected_saving(self):
        return self.original_total - self.expected_total


def analyse_photo(path):
    """
    Decide what could be done with one stored photograph, without changing
    anything. Never raises for a bad photograph; returns a skip/no-benefit
    Analysis instead.
    """
    if path is None:
        return Analysis(STATUS_SKIPPED, reason="not a local file inside the media folder")
    path = Path(path)
    if path.is_symlink():
        return Analysis(STATUS_SKIPPED, reason="symbolic link (left untouched)")
    if not path.is_file():
        return Analysis(STATUS_SKIPPED, reason="file is missing on disk")

    original_size = path.stat().st_size
    try:
        with open(path, "rb") as handle:
            result = build_optimised_passport(handle, path.name)
    except ValidationError as exc:
        reason = "; ".join(exc.messages) if getattr(exc, "messages", None) else "unreadable"
        return Analysis(
            STATUS_SKIPPED,
            original_size=original_size,
            reason=f"unsupported, unreadable or too large: {reason}",
        )
    except OSError as exc:
        return Analysis(
            STATUS_SKIPPED, original_size=original_size, reason=f"could not be read: {exc}"
        )

    if _normalise_extension(path.suffix) != _normalise_extension(Path(result.name).suffix):
        return Analysis(
            STATUS_SKIPPED,
            original_size=original_size,
            reason=(
                f"extension mismatch: file is named '{path.suffix}' but contains "
                f"'{Path(result.name).suffix}' image data (not renamed, to keep the "
                "database reference valid)"
            ),
        )

    new_size = len(result.content)
    if original_size - new_size < MIN_SAVING_BYTES:
        return Analysis(
            STATUS_NO_BENEFIT,
            original_size=original_size,
            new_size=new_size,
            reason="optimising would not save meaningful space; original kept",
        )

    return Analysis(
        STATUS_CANDIDATE, original_size=original_size, new_size=new_size, result=result
    )


def _validate_written_file(temp_path, final_path, content_length, original_size):
    """
    Re-open the temporary file from disk and prove it is a good, smaller
    photograph. Raises ValueError (original untouched) if anything is off.
    """
    if os.path.getsize(temp_path) != content_length:
        raise ValueError("temporary file was not written completely")

    with Image.open(temp_path) as probe:
        probe.verify()  # structural check; the image must be re-opened to decode
    with Image.open(temp_path) as image:
        image.load()  # full decode: catches truncated / corrupt pixel data
        image_format = (image.format or "").upper()
        width, height = image.size

    expected_extension = _EXTENSION_FOR_FORMAT.get(image_format)
    if expected_extension is None:
        raise ValueError(f"optimised file is not a supported image ({image_format or 'unknown'})")
    if _normalise_extension(final_path.suffix) != expected_extension:
        raise ValueError("optimised file format does not match the original extension")
    if width < 1 or height < 1 or width > MAX_PASSPORT_DIMENSION[0] or height > MAX_PASSPORT_DIMENSION[1]:
        raise ValueError(f"optimised file has unexpected dimensions {width}x{height}")
    if original_size - content_length < MIN_SAVING_BYTES:
        raise ValueError("optimised file is not meaningfully smaller than the original")


def replace_photo(path, result, original_size):
    """
    Safely swap ``path`` for the optimised bytes in ``result``.

    Temp file (same folder, so the final swap is a same-filesystem atomic
    rename) -> validate from disk -> copy original permissions -> os.replace.
    On ANY failure the temp file is removed and the original is untouched.
    Returns the new size on disk.
    """
    path = Path(path)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".optimising-", suffix=path.suffix, dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(result.content)
            handle.flush()
            os.fsync(handle.fileno())

        _validate_written_file(temp_name, path, len(result.content), original_size)

        # mkstemp creates the file private (0600); keep the original's
        # permissions so the web server can still read the photograph.
        shutil.copymode(path, temp_name)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise

    return path.stat().st_size


class Command(BaseCommand):
    help = (
        "Safely optimise passport photographs that are already stored. "
        "Use --dry-run to preview (changes nothing) or --confirm to apply."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Analyse and report only. Changes absolutely nothing.",
        )
        mode.add_argument(
            "--confirm",
            action="store_true",
            help="Actually replace photographs with their optimised versions.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Optimise at most this many photographs in this run (default: all). "
                "Photographs that gain nothing do not count, so re-running continues "
                "with the remaining ones."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        confirm = options["confirm"]
        limit = options["limit"]

        if not dry_run and not confirm:
            raise CommandError(
                "Nothing was done. Choose a mode: --dry-run (preview, changes nothing) "
                "or --confirm (apply the optimisation)."
            )
        if limit is not None and limit < 1:
            raise CommandError("--limit must be a positive number.")

        verbosity = options["verbosity"]
        storage_paths = self._discover()
        report = Report(discovered=len(storage_paths))
        mode_label = "DRY RUN (nothing will be changed)" if dry_run else "CONFIRMED RUN"
        self.stdout.write(self.style.MIGRATE_HEADING(f"Passport optimisation - {mode_label}"))

        handled = 0  # photographs selected (dry run) / successfully optimised (confirm)
        for name, path in storage_paths:
            if limit is not None and handled >= limit:
                break

            report.examined += 1
            analysis = analyse_photo(path)
            report.original_total += analysis.original_size

            if analysis.status == STATUS_SKIPPED:
                report.expected_total += analysis.original_size
                report.skipped.append((name, analysis.reason))
                continue
            if analysis.status == STATUS_NO_BENEFIT:
                report.expected_total += analysis.original_size
                report.no_benefit += 1
                continue

            # A real candidate.
            report.candidates += 1
            report.expected_total += analysis.new_size
            if dry_run:
                handled += 1
                if verbosity >= 2:
                    self.stdout.write(
                        f"  would optimise {name}: {format_bytes(analysis.original_size)} -> "
                        f"{format_bytes(analysis.new_size)}"
                    )
                continue

            try:
                new_size_on_disk = replace_photo(path, analysis.result, analysis.original_size)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
                # The original is untouched (see replace_photo). Its size
                # stays in the expected total as the original size.
                report.expected_total += analysis.original_size - analysis.new_size
                report.failures.append((name, f"{type(exc).__name__}: {exc}"))
                continue

            handled += 1
            report.optimised += 1
            report.actual_saved += analysis.original_size - new_size_on_disk
            if verbosity >= 2:
                self.stdout.write(
                    f"  optimised {name}: {format_bytes(analysis.original_size)} -> "
                    f"{format_bytes(new_size_on_disk)}"
                )
            analysis.result = None  # release the image bytes before the next photograph

        report.unreferenced_files = self._count_unreferenced(storage_paths)
        self._print_report(report, dry_run=dry_run, limit=limit)

    # ------------------------------------------------------------------
    def _discover(self):
        """
        (stored name, absolute path) for every distinct passport photograph
        referenced by a Member, oldest member first. Read-only.
        """
        storage = Member._meta.get_field("passport_photo").storage
        seen = set()
        found = []
        queryset = (
            Member.objects.exclude(passport_photo="")
            .order_by("pk")
            .values_list("passport_photo", flat=True)
        )
        for name in queryset.iterator():
            if not name or name in seen:
                continue
            seen.add(name)
            try:
                found.append((name, Path(storage.path(name))))
            except (SuspiciousFileOperation, NotImplementedError):
                # Outside the media folder, or storage without local paths:
                # never touch it.
                found.append((name, None))
        return found

    def _count_unreferenced(self, storage_paths):
        """Informational only: image files in the passport folder no Member points to."""
        folder = Path(settings.MEDIA_ROOT) / "members" / "passports"
        if not folder.is_dir():
            return 0
        referenced = {
            os.path.normcase(os.path.abspath(path))
            for _name, path in storage_paths
            if path is not None
        }
        return sum(
            1
            for candidate in folder.rglob("*")
            if candidate.is_file()
            and not candidate.name.startswith(".optimising-")
            and os.path.normcase(os.path.abspath(candidate)) not in referenced
        )

    def _print_report(self, report, dry_run, limit):
        out = self.stdout.write
        out("")
        out(self.style.MIGRATE_HEADING("Report"))
        out(f"  Total photographs discovered:        {report.discovered}")
        if report.not_examined:
            out(
                f"  Not examined (limit {limit} reached):".ljust(39)
                + f"{report.not_examined}  (run again to continue)"
            )
        out(f"  Photographs examined:                {report.examined}")
        out(f"  Can be optimised:                    {report.candidates}")
        out(f"  Already optimal / no benefit (kept): {report.no_benefit}")
        out(f"  Skipped (unsupported/unreadable...): {len(report.skipped)}")
        out(f"  Failures:                            {len(report.failures)}")
        out(f"  Original total size (examined):      {format_bytes(report.original_total)}")
        out(f"  Expected size after optimisation:    {format_bytes(report.expected_total)}")
        out(f"  Expected saving:                     {format_bytes(report.expected_saving)}"
            f" ({self._percent(report.expected_saving, report.original_total)})")
        if dry_run:
            out("  Actual storage saved:                0 B (dry run - nothing was changed)")
        else:
            out(f"  Photographs optimised:               {report.optimised}")
            out(f"  Actual storage saved:                {format_bytes(report.actual_saved)}"
                f" ({self._percent(report.actual_saved, report.original_total)})")
        if report.unreferenced_files:
            out(
                f"  Note: {report.unreferenced_files} image file(s) in the passport folder are not "
                "referenced by any member. They were left untouched."
            )

        if report.skipped:
            out("")
            out(self.style.WARNING("Skipped photographs (left unchanged):"))
            for name, reason in report.skipped:
                out(f"  - {name}: {reason}")
        if report.failures:
            out("")
            out(self.style.ERROR("Failures (originals left unchanged):"))
            for name, reason in report.failures:
                out(f"  - {name}: {reason}")

        out("")
        if dry_run:
            out(self.style.SUCCESS(
                "Dry run complete. No file or database record was changed. "
                "Re-run with --confirm (optionally --limit N) to apply."
            ))
        else:
            out(self.style.SUCCESS("Run complete. Database records were not touched."))

    @staticmethod
    def _percent(part, whole):
        return f"{(part / whole * 100):.1f}%" if whole else "0.0%"
