from django.core.management.base import BaseCommand

from core.models import FormDefinition


class Command(BaseCommand):
    help = "Seed the FormDefinition registry (MOH 748 active; 721/S11 inactive placeholders)."

    def handle(self, *args, **options):
        forms = [
            dict(slug="moh748", name="MOH 748 — Commodity Stock Status", short_name="MOH 748",
                 category="Commodity", is_active=True, display_order=1),
            dict(slug="moh721", name="MOH 721 — Facility Summary", short_name="MOH 721",
                 category="Facility", is_active=False, display_order=2),
            dict(slug="mohs11", name="MOH S11 — Service Summary", short_name="MOH S11",
                 category="Service", is_active=False, display_order=3),
        ]
        for data in forms:
            slug = data.pop("slug")
            obj, created = FormDefinition.objects.update_or_create(slug=slug, defaults=data)
            action = "Created" if created else "Updated"
            self.stdout.write(self.style.SUCCESS(f"{action} {obj}"))
