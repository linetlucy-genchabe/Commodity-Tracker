# eCHIS "Commodities Order" form — reference notes

Captured 2026-09-14 from CHIS Kenya (`kisumu.echis.go.ke`), on Lynne's
"West Othany B Community Health Unit" record, for future planning —
not yet built into Commodity Tracker.

**How to get there:** People tab → open a Community Health Unit →
blue "+" button on the CHU record → "Commodities Order".

**Form's own description:** "This form shows how much quantity is
required during ordering and used to record quantities received." —
so a single eCHIS "order" actually captures both the request AND what
was received against it, entered together in one pass, not as two
separate order/receipt events.

## Step 1 — category & item picker

- **Select the category of what you want to order*** (checkbox list):
  Commodities, Equipment
- **List of commodities*** (checkbox list, multi-select) — shown once
  "Commodities" is checked: Malaria, Child health, Reproductive and
  Maternal Health Services, WASH, NCDs (Non-Communicable Diseases),
  Campaigns, Medical Supplies, Others
- For each category checked, a matching sub-list of items appears
  (all checkboxes, multi-select, required). Under **Malaria**:
  - RDTs (Malaria Test Kit)
  - First Line Anti-Malarial (ACT/Coartem) 6 Pack
  - First Line Anti-Malarial (ACT/Coartem) 12 Pack
  - First Line Anti-Malarial (ACT/Coartem) 18 Pack
  - First Line Anti-Malarial (ACT/Coartem) 24 Pack
  - Insecticide Treated Nets (ITN)
- Buttons: Cancel / Next >

## Step 2+ — one screen per selected item

The form then steps through the checked items one at a time (Prev/Next
between them). Each item's screen — e.g. for RDTs (Malaria Test Kit) —
shows:

- **Balance on hand:** `<number>` — "Total commodities that the CHPs
  in this CHU have"
- **Quantity required:** `<number>` — "Total commodities that the
  CHPs in this CHU need for the next 6 weeks"
- **Weeks of stock:** `<number>` — "Number of weeks that the balance
  on hand of the CHPs in this CHU can last"
- **Quantity requested*** — free-entry number field
- **Quantity received*** — free-entry number field
- Buttons: Cancel / < Prev / Next >

The remaining checked Malaria items (the ACT/Coartem pack sizes, ITN)
follow the identical layout — same five fields, just their own
balance/required/weeks numbers.

## Notes for later

- This is CHU-scoped (not per-CHP), same rollup level as the "CHU"
  view in Commodity Tracker's CHP Commodity Stock Flow pages.
- eCHIS's own "Weeks of stock" here is computed CHU-side from
  CHP-aggregated balance against a 6-week requirement window — worth
  comparing against how `weeks_of_stock` is computed in
  `core/views.py`/`core/models.py` if this ever gets built out.
- Because "requested" and "received" are captured together on one
  screen, an eCHIS order isn't really separable into a distinct
  "order placed" record and a later "order fulfilled" record — both
  numbers land at once, at whatever time the CHU records this.
- Screenshots of the live form are attached to the Cowork conversation
  where this was captured, if the exact visual layout is needed again.
