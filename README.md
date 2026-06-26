# ca-records-index

Research tooling for indexing **public** California county recorder document
metadata (document number, recorded date, document type, grantor/grantee names,
APN). Reads only publicly available recorder index data; no authentication, no
personal data beyond what the county publishes in its open index, no outreach.

- `la_county_index.py` — authoritative LA County recorder index client.
- `lead_class.py` — classifies a county document Type string into a lead class.
- `doc_taxonomy.py` — CA foreclosure-stage taxonomy with negative traps.
- `harvest_index_shard.py` — harvests a contiguous doc# range to CSV.
- `.github/workflows/` — sharded distributed harvest across runners.

Public-records research only.
