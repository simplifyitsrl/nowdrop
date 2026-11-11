# Implementation notes

Things that differ from, or were not specified in, the build spec.

- **Currency.** Each show uses `DEFAULT_CURRENCY`. If the seller names a different currency in speech ("twenty euros" on a USD show), the drop is not published and the console explains why. The PayPal JS SDK is loaded once per page with a fixed currency, so mixed currencies per show are out of scope.
- **Forced tool use.** Extraction and recap use Claude structured outputs (`output_config.format` with a JSON schema) instead of a forced tool call; the assistant uses normal tool calling with `tool_choice: auto`.
- **Missing values in the extraction schema.** To keep the schema strict without nullable types, "no price" is `price: 0` plus `"price"` in `missing`. The server never publishes a drop with a price of 0.
- **Recap figures.** The LLM gets the figures as JSON and writes text only. Every number in its text must appear in the figures for that buyer (or the show); otherwise a deterministic template is used.
- **Capture failure.** Besides giving the unit back, the server voids the failed authorization (best effort) so the buyer's hold is released immediately.
- **Authorization failure** (e.g. instrument declined at authorize time) marks the attempt `abandoned` and tells the buyer they were not charged; no unit is taken.
- **Seller-only routes** are protected by the unguessable show id (no login), including refunds, which must send the show id.
- **Extra columns:** `shows.currency`, `shows.recap_json`, `orders.refund_id`, `paypal_log.show_id`.
- **Extraction latency.** Target is under 3 s from trigger word to live. Extraction runs at `effort: "low"` with adaptive thinking. If a live run is slow, the next lever is disabling thinking for extraction (Claude Haiku 5.5 accepts `thinking: {"type": "disabled"}` at effort high or below), not a smaller model.
- **Speech language.** Spanish uses `es-419`; if Chrome reports `language-not-supported`, the console retries with `es-MX`.
- **Sandbox behaviour to verify on the first live run** (adapt and record here if it differs): authorize response contains `payer.name` and `payer.email_address`; void returns `204 No Content`; capture with `Prefer: return=representation` returns status `COMPLETED`.
