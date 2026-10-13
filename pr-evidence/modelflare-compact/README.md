# Modelflare visual evidence

Captured on 2026-10-13 for product commit `850d46e32976fff9d26b8ce2ce5501b73272da5d`.

- README screenshots: actual GitHub rendering of that commit in Simplified Chinese, Traditional Chinese and English. Default expanded state, collapse/reopen verified. Desktop viewport: 1274 × 900; mobile viewport: 390 × 844.
- Before screenshot: original product commit `7c7608cde85cbf5f2d943978ab433e7b769cc46b`, same desktop viewport, old sponsor details expanded. Captures are cropped to the affected sponsor section.
- Web screenshots: real application build containing current product changes, using the same deterministic API boundary as `apps/dsa-web/e2e/fixtures/settings-guard.spec.ts`. They demonstrate actual UI rendering and conditional registration visibility, not backend connectivity. No credentials were supplied to the page.
- `web-config-validation-*` and `web-channel-registration.png`: current build connected to the real locally running backend; only the configuration-list GET is replaced with an empty editor to keep credentials out of screenshots. Preview/status use the actual backend. Adding a channel with no Key returns HTTP 400 `validation_failed`; the page displays `LLM_MODELFLARE_API_KEY` and the corrective instruction instead of the generic English failure. No configuration is saved. The channel details registration link uses the supplied app UTM.
- `web-config-validation-recovered.png`: filling the Key field with a fictional test value makes the real read-only preview return HTTP 200 and clears the validation alert. This checks configuration readiness only; no external model call or credential authentication is performed. Clearing the field restores the HTTP 400 field guidance. Real credential authentication is covered separately by the prior live channel check.
- Backend connectivity was checked separately through real DSA `/config/llm/discover-models` and `/config/llm/test-channel` requests. API Key was provided only in process memory and is not part of these artifacts.
- Reproduce README: open the product commit's README on GitHub, confirm the sponsor section starts open, click its summary to collapse and reopen.
- Reproduce Web: build Web, open Settings → AI models, select Modelflare and Anspire under Quick add; registration is visible only for Modelflare. Inspect desktop and 390px mobile views.

This branch contains review artifacts only. These images are excluded from the product PR diff.
