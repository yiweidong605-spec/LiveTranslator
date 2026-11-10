# Test set changelog

## v1.1 — 2026-11-06
After the first full run (8 models), every keyword miss was reviewed by hand.
- Added accepted variants to `expect` for 27 items where models were right but the
  keyword list was too narrow (e.g. 最后期限 for "dernier délai", 一千两百 for "1 200",
  白等 for "poser un lapin", délivrer for issuing an invoice).
- `slang-frzh-07`: reference changed to 这没什么大不了的。
- Removed `amb05c` ("faire un pot" + tomato context): the context does not really
  disambiguate it, even for native speakers.
- Kept as real errors: 三楼 → troisième étage (should be 2e étage), 不用找了 →
  "Ne cherchez pas", 七分熟 → "à sept parts", "sous l'eau" → 在水下, thèse vs mémoire.

## v1.0 — 2026-11-01
247 sentences, drafted by Claude; references not yet reviewed by a native speaker.
