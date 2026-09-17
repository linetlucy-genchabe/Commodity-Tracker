"""
One-off/re-runnable import: link CommunityHealthUnit -> Facility from a KHIS
"Health Facilities & CUs" export, creating the Facility (and its Ward, if
needed) directly from this file when it doesn't exist yet.

Lynne asked (repeatedly) whether any of her files carried a CHU-to-facility
mapping. None of the eCHIS exports do (they have CHU but no facility), and
the original MOH 748 workbook has facility but no CHU. This KHIS export is
the first file that actually pairs the two: each row where the "CU" column
is filled in is a Community Health Unit, linked to the "Health Facility" it
reports through, alongside County/Subcounty/Ward.

Facility used to only ever get created by uploading that original MOH 748
workbook (see parse_moh748_workbook) -- but Lynne has confirmed that
workbook won't be used anywhere in this system going forward; all data is
meant to come from eCHIS-derived files. Production never had an MOH 748
workbook uploaded, so its Facility table came up empty, and this command's
first real run there failed to match all 272 distinct facility names for
exactly that reason. So this command now creates a Facility itself,
straight from this file's own County/Subcounty/Ward/Health Facility columns,
whenever a CU's linked facility doesn't already exist -- no MOH 748 upload
required, ever. The new facility is filed under its matched CommunityHealthUnit's
own Sub-County (the authoritative one, already established by the eCHIS CHP
Commodity Stock Flow upload) rather than re-deriving/creating a Sub-County
from this file, so it can never fragment the Sub-County list used elsewhere
in the app; only its Ward is looked up or created here, scoped to that same
Sub-County.

This command still never creates a CommunityHealthUnit. A CU name in this
file with no matching CommunityHealthUnit already in the app is a genuine
naming inconsistency to fix at the source (a retired/duplicate CU entry, or
similar) -- not something to paper over by inventing a new CHU record. Lynne
looked at that "not found" list and chose to leave those alone.

Written generically (matches whichever County/Subcounty/CU/Facility names
are in the file) so the same command can be re-run later for Busia,
Bungoma, and Vihiga once their own KHIS exports are on hand -- not
hardcoded to Kisumu.

Usage:
    python manage.py import_facility_cu_map path/to/khis_export.xls
    python manage.py import_facility_cu_map path/to/khis_export.xls --dry-run
"""

import re

import pandas as pd
from django.core.management.base import BaseCommand, CommandError

from core.models import CommunityHealthUnit, Facility, Ward
from core.parsing import _strip_admin_suffix


def _normalize(value):
    """Whitespace/case/trailing-punctuation-insensitive key for matching
    names across two independently-maintained spreadsheets (extra spaces,
    a stray trailing period, "(old)" suffixes on retired CU names, etc.)."""
    text = ("" if value is None else str(value)).strip().lower()
    if text == "nan":
        return ""
    text = re.sub(r"\s+", " ", text)
    text = text.rstrip(".")
    return text


class Command(BaseCommand):
    help = (
        "Link CommunityHealthUnit.facility from a KHIS Health Facilities & "
        "CUs export (columns: Country, County, Subcounty, Ward, Health "
        "Facility, CU). Creates the Facility (and its Ward, if needed) from "
        "this file when it doesn't already exist -- never creates a "
        "CommunityHealthUnit."
    )

    def add_arguments(self, parser):
        parser.add_argument("khis_file", help="Path to the KHIS .xls/.xlsx export.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--sheet",
            default=0,
            help="Sheet name or index to read (default: the first sheet).",
        )

    def handle(self, *args, **options):
        path = options["khis_file"]
        dry_run = options["dry_run"]

        try:
            df = pd.read_excel(path, sheet_name=options["sheet"])
        except FileNotFoundError:
            raise CommandError(f"File not found: {path}")
        except Exception as exc:
            raise CommandError(f"Could not read '{path}': {exc}")

        required_cols = {"County", "Subcounty", "Ward", "Health Facility", "CU"}
        missing_cols = required_cols - set(df.columns)
        if missing_cols:
            raise CommandError(
                f"'{path}' is missing expected column(s): {', '.join(sorted(missing_cols))}. "
                f"Found: {', '.join(str(c) for c in df.columns)}"
            )

        cu_rows = df[df["CU"].notna() & (df["CU"].astype(str).str.strip() != "")]
        self.stdout.write(f"Read {len(df)} row(s); {len(cu_rows)} have a CU (Community Health Unit) value.")

        # --- Pass 1: group the file's own rows by (county, CU) key first. ---
        # The KHIS export itself isn't perfectly clean -- a handful of CU
        # names appear on two rows pointing at two different facilities
        # (e.g. an old/renamed entry left behind). Resolving that by
        # whichever row happens to come last in the spreadsheet would be an
        # arbitrary, order-dependent guess -- and worse, re-running the
        # command would toggle between the two answers every time (each row
        # "changing" it back), which is exactly the kind of silent flakiness
        # a data import should never have. So: collect every distinct
        # facility name seen per CU key across the whole file first, and
        # only proceed for keys with exactly one answer. Keys with more
        # than one are reported as an in-file conflict and skipped, same as
        # any other unresolvable row.
        def county_key_of(value):
            # KHIS county names come as "Kisumu County" -- the app's County
            # rows are just "Kisumu". Strip a trailing " County" so this
            # still matches.
            return _normalize(re.sub(r"\s+county\s*$", "", str(value or ""), flags=re.I))

        facility_names_by_cu_key = {}
        for _, row in cu_rows.iterrows():
            cu_key = (county_key_of(row.get("County")), _normalize(row.get("CU")))
            facility_names_by_cu_key.setdefault(cu_key, set()).add(str(row.get("Health Facility")).strip())

        conflicting_keys = {k for k, names in facility_names_by_cu_key.items() if len(names) > 1}

        # --- Pass 2: index existing CHUs and Facilities by a normalized name
        # key, scoped by county name (also normalized) so the same command
        # works for any county's export without hardcoding "Kisumu". Wards
        # are indexed scoped by their Sub-County id instead, since a new
        # Facility is always filed under its CHU's own (already-correct)
        # Sub-County -- see the module docstring. ---
        chus_by_key = {}
        for chu in CommunityHealthUnit.objects.select_related("sub_county__county"):
            key = (_normalize(chu.sub_county.county.name), _normalize(chu.name))
            chus_by_key.setdefault(key, []).append(chu)

        facilities_by_key = {}
        for facility in Facility.objects.select_related("ward__sub_county__county"):
            key = (_normalize(facility.ward.sub_county.county.name), _normalize(facility.name))
            facilities_by_key.setdefault(key, []).append(facility)

        wards_by_key = {}
        for ward in Ward.objects.all():
            wards_by_key[(ward.sub_county_id, _normalize(ward.name))] = ward

        linked = 0
        already_linked_same = 0
        relinked_changed = 0
        facilities_created = 0
        wards_created = 0
        chu_not_found = []
        ambiguous = []
        conflicting = []
        seen_cu_keys = set()

        for _, row in cu_rows.iterrows():
            county_name = row.get("County")
            cu_name = row.get("CU")
            facility_name = str(row.get("Health Facility")).strip()
            ward_name = _strip_admin_suffix(row.get("Ward"))

            county_key = county_key_of(county_name)
            cu_key = (county_key, _normalize(cu_name))
            facility_key = (county_key, _normalize(facility_name))

            if cu_key in conflicting_keys:
                if cu_key not in seen_cu_keys:
                    seen_cu_keys.add(cu_key)
                    names = ", ".join(sorted(facility_names_by_cu_key[cu_key]))
                    conflicting.append(f"{cu_name} -> {names} (the file itself disagrees; skipped)")
                continue
            if cu_key in seen_cu_keys:
                # Already processed this CU key once this run (a duplicate
                # row with the SAME facility, which is fine -- nothing left
                # to do).
                continue
            seen_cu_keys.add(cu_key)

            chu_matches = chus_by_key.get(cu_key, [])
            if not chu_matches:
                chu_not_found.append(str(cu_name).strip())
                continue
            if len(chu_matches) > 1:
                ambiguous.append(f"{cu_name} -> {facility_name} ({len(chu_matches)} CHU match(es))")
                continue
            chu = chu_matches[0]

            facility_matches = facilities_by_key.get(facility_key, [])
            if len(facility_matches) > 1:
                ambiguous.append(f"{cu_name} -> {facility_name} ({len(facility_matches)} facility match(es))")
                continue

            if facility_matches:
                facility = facility_matches[0]
            else:
                # No existing Facility with this name in this county -- file
                # it under the CHU's own (already-correct) Sub-County,
                # finding or creating that Sub-County's Ward from this row.
                sub_county = chu.sub_county
                ward_key = (sub_county.id, _normalize(ward_name))
                ward = wards_by_key.get(ward_key)
                if ward is None:
                    if not dry_run:
                        ward, ward_was_created = Ward.objects.get_or_create(
                            sub_county=sub_county, name=ward_name or "Unspecified"
                        )
                    else:
                        ward_was_created = True  # would be created
                        ward = Ward(sub_county=sub_county, name=ward_name or "Unspecified")
                    wards_by_key[ward_key] = ward
                    if ward_was_created:
                        wards_created += 1

                if not dry_run:
                    facility, facility_was_created = Facility.objects.get_or_create(
                        ward=ward, name=facility_name
                    )
                else:
                    facility_was_created = True  # would be created
                    facility = Facility(ward=ward, name=facility_name)
                facilities_by_key.setdefault(facility_key, []).append(facility)
                if facility_was_created:
                    facilities_created += 1

            # facility.pk is None only for a --dry-run "would create" facility
            # that doesn't exist in the database yet -- a CHU can never
            # already be linked to a facility that doesn't exist, so that
            # case always falls through to "would link" below, correctly.
            if facility.pk is not None and chu.facility_id == facility.pk:
                already_linked_same += 1
                continue

            changed = chu.facility_id is not None
            if not dry_run:
                chu.facility = facility
                chu.save(update_fields=["facility"])
            linked += 1
            if changed:
                relinked_changed += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"{'Would link' if dry_run else 'Linked'} {linked} CHU(s) to a facility "
            f"({relinked_changed} of those replaced a different existing link)."
        ))
        self.stdout.write(f"Already linked to the same facility (no change needed): {already_linked_same}")
        self.stdout.write(
            f"{'Would create' if dry_run else 'Created'} {facilities_created} new Facility record(s) "
            f"({wards_created} new Ward record(s) along with them)."
        )
        if chu_not_found:
            self.stdout.write(self.style.WARNING(
                f"CU names in the file with no matching CommunityHealthUnit in the app ({len(chu_not_found)}):"
            ))
            for name in sorted(set(chu_not_found)):
                self.stdout.write(f"  - {name}")
        if ambiguous:
            self.stdout.write(self.style.WARNING(f"Ambiguous (skipped, {len(ambiguous)}):"))
            for line in ambiguous:
                self.stdout.write(f"  - {line}")
        if conflicting:
            self.stdout.write(self.style.WARNING(
                f"CU names the file itself links to more than one facility (skipped, {len(conflicting)}):"
            ))
            for line in conflicting:
                self.stdout.write(f"  - {line}")

        if dry_run:
            self.stdout.write("")
            self.stdout.write(self.style.NOTICE("Dry run -- nothing was written. Re-run without --dry-run to apply."))
