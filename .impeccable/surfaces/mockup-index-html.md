---
version: 1
slug: "mockup-index-html"
primary_target: "mockup/index.html"
related_targets: ["gui.py"]
---

# Binday desktop app (all screens)

Scope: every screen of the Flet desktop app (Connect, Scan, Review, Confirm dialog, Results). Mode: Operate. Mockup at mockup/index.html is the composition gui.py must match; built code-led.

Audience and job: self-hosted Mattermost Team Edition admins reclaiming disk space; they must see exactly what goes, choose, confirm, and read an honest result. Constraint: every element buildable from Flet/Flutter Material 3 controls (NavigationRail, DataTable, Checkbox, Switch, TextField, Dropdown, AlertDialog, ProgressBar, Container); light and dark themes; Windows, macOS, Linux.

Memorable moment: the load bar, sorted by bin colour, that shrinks as you untick rows and is "collected" on the Results screen.

## Direction contract

THESIS: Cleanup is civic sorting. Every item goes in its bin and you see the load before the truck comes. Refuses the stock admin panel of stat tiles plus a table with a blue accent.

OWN-WORLD: Mattermost navy rail (#1E325C) and Mattermost blue (#1C58D9) for every action (user-pinned), cool off-white ground (dark: blue-black), ink text. Three bin colours: teal for archived channels, collection yellow for deactivated DMs, earth brown for orphaned files. Destructive red is reserved for delete. Public Sans throughout, tabular figures, Material Symbols pictograms in square bin chips. Colour is quarantined to bin chips, the load bar and the delete red.

STORY: The admin connects, scans read-only, sees the load split by bin, unticks anything to keep, confirms by typing DELETE, then reads what was collected, skipped and failed, by plan number.

FIRST VIEWPORT: Review. Rail on the left with four numbered steps. Title and server line on top, then the full-width load bar with a figure in figures and a legend per bin, kind filters, a dense table (checkbox, plan #, name, bin chip, location, files, a size bar whose length is the size), and a footer with the VACUUM option and the red delete button bottom right.

FORM: Collection Calendar (municipal waste-sorting calendar), position 4 of the ordered grounded list; seed:8e01455551bb0a3f. Raises: colour quarantine (lexicon), length is quantity (labanotation), stable plan numbers (catalog sleeve), red owns destruction (metro), tabular figures (datamatics).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
