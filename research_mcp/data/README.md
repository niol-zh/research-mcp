# Scimago fallback table

`get_journal_metrics` prefers the Scopus Serial Title API. That endpoint is
entitlement-gated, so when a key does not carry it — or the journal is missing —
the tool falls back to a Scimago journal-rank table placed here as `scimago.csv`.

The file is not committed: it is ~5 MB, changes yearly, and its data is licensed
**CC BY-NC**, so redistribution needs attribution and rules out commercial reuse.

## Obtaining it

1. Go to https://www.scimagojr.com/journalrank.php
2. Choose the year you want, then the download icon (⭳) above the table
3. Save the file here as `scimago.csv`

The export is semicolon-separated. The loader reads the `Issn`, `Title`,
`Publisher`, `SJR`, `SJR Best Quartile` and `Categories` columns, and tolerates
both quoted and unquoted `Categories` values.

Without this file the fallback is simply unavailable: `get_journal_metrics` still
answers from Scopus, and logs a warning if it ever needs the fallback.
