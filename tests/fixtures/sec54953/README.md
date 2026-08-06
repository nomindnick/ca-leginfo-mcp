# § 54953 version-chain fixtures (V2 engine, SPEC §10/§14)

Real pubinfo data for Gov. Code § 54953 (Brown Act) — the deliberately
messy case the V2 redline engine is contracted against: three amending
chapters in one session, sunset/operative-date branches left by AB 2449
(Stats. 2022, ch. 285), and a contingent double-jointed print: AB 557
printed both `SECTION 1.` and `SEC. 1.5.`, with SEC. 1.5 operative only
if SB 537 also amended § 54953 by 1/1/2024. SB 537 was gutted into a
memorials bill (Stats. 2024, ch. 859), so **SECTION 1 became operative
and SEC. 1.5 never did** — and AB 2302's intro citing "Section 1 of
Chapter 534" is a literal citation of the operative block. (An earlier
version of this README called SEC. 1.5 the operative print; the
verification pass proved that wrong from this very data.) Version-graph
resolution must therefore evaluate operativeness via the history-note
chain, never assume a print number.

All lobs come from `pubinfo_2023.zip` (2023–2024 session bulk export),
named by `bill_version_id`:

| file | source zip entry | why it's here |
| --- | --- | --- |
| `20230AB175497CHP_excerpt.lob` | `BILL_VERSION_TBL_7659.lob` | AB 1754 (Stats. 2023, ch. 131) chaptered. 2 MB omnibus, excerpted: document prefix + `SECTION 1.` + `SEC. 88.`–`SEC. 92.` + tail. Three § 54953 variant blocks (SEC. 88–90); excerpt verified block-identical to the full lob. |
| `20230AB55795CHP.lob` | `BILL_VERSION_TBL_9921.lob` | AB 557 (Stats. 2023, ch. 534) chaptered, whole. Four § 54953 blocks: `SECTION 1.` (operative), `SEC. 1.5.` (contingent double-joint print, never operative), `SEC. 2.`, and a repealed block (`SEC. 3.`, no body). |
| `20230AB230297CHP.lob` | `BILL_VERSION_TBL_20068.lob` | AB 2302 (Stats. 2024, ch. 389) chaptered, whole. One block whose heading follows `:` with no newline after flattening (`…do enact as follows:SECTION 1.`); its intro's "Section 1 of Chapter 534" lineage names AB 557's operative block. |
| `20230AB230299INT.lob` | `BILL_VERSION_TBL_11239.lob` | AB 2302 introduced, whole — a same-bill print pair with the chaptered lob for bill-version comparison. |
| `current_54953.txt` | live server `get_section` (2026-08) | Current law text (SB 707, Stats. 2025, ch. 327) from the law-lob source — the cross-source endpoint proving whitespace-insensitive tokenization yields zero phantom hunks. |

`golden_redlines/` holds the engine's expected markdown for the version
chain — AB 1754 `SEC. 89` → AB 557 `SEC. 2` → current, and the
operative edge AB 557 `SECTION 1` → AB 2302 — pinned in
`tests/test_redline.py`.
