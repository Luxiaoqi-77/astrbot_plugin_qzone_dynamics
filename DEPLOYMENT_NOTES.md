# Deployment notes

## 2026-10-10 — current activity selection and concise Qzone drafts

- The production patch updates source selection to choose one current eligible VirtualLife activity or the most recently ended eligible activity within 90 minutes; sleep, future-only, malformed, stale, or missing activities yield no draft input. Outfit enumeration is removed.
- The model input includes activity status, interval, and local time. The prompt requests one or two short first-person sentences about a single moment or thought, with tense matching the activity status, no daily ledger, and no invented facts. Existing public-source whitelist and privacy constraints remain in force.
- Per the parent’s correction, today's VirtualLife schedule is valid and draft `79d44697db31` was successfully generated and remains pending review. This patch does not inspect, edit, replace, or regenerate that draft.
- Production config stays unchanged: auto draft off, publish off, images off, mood off, audience scope empty, and target UINs empty. No model or QQ/Qzone call was made.
- Production AstrBot virtual environment test run: 48/48 tests passed, including all 4 integration tests with `ASTRBOT_ROOT` set to the production checkout. Provider and action calls in those tests are test doubles.
- The production files require a UI single-plugin reload before runtime verification. This task does not reload or restart AstrBot.
