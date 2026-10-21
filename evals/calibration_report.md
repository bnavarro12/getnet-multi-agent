# Judge calibration against human labels

12 cases labelled by a person (blind to the judges' scores) · report of 2026-10-21 21:37

| Judge | Metric | n | Exact | Within 1 | Mean abs. diff | Weighted κ |
|---|---|---|---|---|---|---|
| external:openai/gpt-oss-120b | groundedness | 12 | 0.833 | 0.917 | 0.417 | 0.0 |
| external:openai/gpt-oss-120b | helpfulness | 12 | 0.75 | 0.833 | 0.5 | 0.488 |
| gemini:gemini-flash-latest | groundedness | 12 | 0.917 | 0.917 | 0.333 | 0.0 |
| gemini:gemini-flash-latest | helpfulness | 12 | 0.583 | 0.917 | 0.583 | 0.435 |
| panel (mean) | groundedness | 12 | 0.833 | 0.917 | 0.417 | 0.0 |
| panel (mean) | helpfulness | 12 | 0.75 | 0.833 | 0.417 | 0.609 |

Reading: within-1 ≥ 0.9 and κ ≥ 0.6 → the judge can be trusted for regression testing; lower → fix the judge prompt or change the judge before trusting its averages.

## Largest disagreements (groundedness, ≥ 2 points)

- esc-01: human 5 vs panel 1 — 
