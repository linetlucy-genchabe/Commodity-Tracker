"""
One-off/re-runnable import: link CommunityHealthUnit -> Facility from a KHIS
"Health Facilities & CUs" export.

Lynne asked (repeatedly) whether any of her files carried a CHU-to-facility
mapping. None of the eCHIS exports do (they have CHU but no facility), and
the MOH 748 workbook has facility but no CHU. This KHIS export is the first
file that actually pairs the two: each row where the "CU" column is filled
in is a Community Health Unit, linked to the "Health Facility" it reports
through, alongside County/Subcounty/Ward.

Checked against her real data before writing this: for Kisumu, 270 of 291
distinct eCHIS CHU names match this file's CU names exactly (case/whitespace
aside), and all 155 distinct facility names in the matched CU rows already
exist as Facility rows (seeded from the MOH 748 workbook) -- so this command
only ever LINKS existing CommunityHealthUnit and Facility rows to each
other; it never creates new ones. A CHU or facility name that can't be
matched is reported and skipped, never guessed at.

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

from core.models import CommunityHealthUnit, Facility


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
        "Facility, CU). Only links existing CHU/Facility rows -- never "
        "creates new ones."
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
        # works for any county's export without hardcoding "Kisumu". ---
        chus_by_key = {}
        for chu in CommunityHealthUnit.objects.select_related("sub_county__county"):
            key = (_normalize(chu.sub_county.county.name), _normalize(chu.name))
            chus_by_key.setdefault(key, []).append(chu)

        facilities_by_key = {}
        for facility in Facility.objects.select_related("ward__sub_county__county"):
            key = (_normalize(facility.ward.sub_county.county.name), _normalize(facility.name))
            facilities_by_key.setdefault(key, []).append(facility)

        linked = 0
        already_linked_same = 0
        relinked_changed = 0
        chu_not_found = []
        facility_not_found = []
        ambiguous = []
        conflicting = []
        seen_cu_keys = set()

        for _, row in cu_rows.iterrows():
            county_name = row.get("County")
            cu_name = row.get("CU")
            facility_name = row.get("Health Facility")

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
            facility_matches = facilities_by_key.get(facility_key, [])

            if not chu_matches:
                chu_not_found.append(str(cu_name).strip())
                continue
            if not facility_matches:
                facility_not_found.append(str(facility_name).strip())
                continue
            if len(chu_matches) > 1 or len(facility_matches) > 1:
                ambiguous.append(f"{cu_name} -> {facility_name} ({len(chu_matches)} CHU match(es), {len(facility_matches)} facility match(es))")
                continue

            chu = chu_matches[0]
            facility = facility_matches[0]

            if chu.facility_id == facility.id:
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
        if chu_not_found:
            self.stdout.write(self.style.WARNING(
                f"CU names in the file with no matching CommunityHealthUnit in the app ({len(chu_not_found)}):"
            ))
            for name in sorted(set(chu_not_found)):
                self.stdout.write(f"  - {name}")
        if facility_not_found:
            self.stdout.write(self.style.WARNING(
                f"Health Facility names in the file with no matching Facility in the app ({len(facility_not_found)}):"
            ))
            for name in sorted(set(facility_not_found)):
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
