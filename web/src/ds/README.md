# Arenos design system

This directory implements the **Arenos** brand standards (v1.0 draft,
September 2026), ported from the `arenos-design` skill's token files
(`tokens/colors.css`, `neutrals.css`, `semantic.css`, `typography.css`,
`spacing.css`, `elevation.css`) — every value in `styles.css` traces to one of
those, not to a redraw. It replaces the earlier Modernist system this app
shipped with; nothing here derives from Modernist any longer.

## How to use this

- `styles.css` is linked once from `index.html` and is the source of truth for
  every colour, font, space, radius and shadow (`var(--color-*)`,
  `var(--font-*)`, `var(--space-*)`, `var(--radius-*)`, `var(--shadow-*)`).
  `app.css` never hard-codes a hex, a font name or a px value the tokens
  already carry — see its own header comment for the two literals that remain
  and why.
- The system has two themes, not one hard-coded look: `arenos-dark` (the
  product default — this file's `:root` block *is* Arenos Dark, so that theme
  is an empty overlay) and `arenos-light` (a full peer, applied as a token
  overlay in `web/src/settings.js`). See that file for how a theme is added.

## Colour

Arenos Amber `#D4881A` is the only accent, and it is scarce: the standards cap
it around one part in seven overall and expect far less than that on any one
screen. Everything else is neutral (a warm 7-step ramp in light, a cool 7-step
ramp in dark) plus four meaning hues spaced by the golden angle — verdigris
(structure), olive (success), indigo (information), plum (innovation) — and
brick for alert, which is this app's `--color-accent-2`. Amber never sets type
on a light ground (2.86:1, fails AA); `--color-on-accent` (`#111111`) is what
button text uses instead of a literal white, in both themes.

## Type

Geist for brand and product, Geist Mono for anything a model produced —
timecodes, confidence scores, moment IDs — vendored as TTF files in
`../assets/fonts/` rather than pulled from a font CDN. Headings are weight 500
(Geist has no 800; asking for one synthesizes a fake bold, so nothing in this
app does). Tabular numerals throughout.

## Radii, rules, spacing

The brand standards specify no radius, on purpose — the system's geometry is
circles and squares, not pills — so the values here are small and derived:
8px on cards and panels, 5px on inputs and chips, 3px on meters and controls.
One hairline weight, 1px, everywhere; there is no separate heavier "structural"
rule the way the old Modernist system had one. Spacing follows the Fibonacci
ladder the standards derive from φ: 3 · 5 · 8 · 13 · 21 · 34, with 21px doing
double duty as both a spacing step and the mark's fixed clear space.

See the `arenos-design` skill (`.claude/skills/arenos-design/`) for the full
brand standards, the component library, and the two UI kits (`platform`,
`marketing`) this port was checked against.
