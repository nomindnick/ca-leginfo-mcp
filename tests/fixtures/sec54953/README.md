# § 54953 version-chain fixtures (V2 engine, SPEC §10/§14)

Real pubinfo data for Gov. Code § 54953 (Brown Act) — the deliberately
messy case the V2 redline engine is contracted against: three amending
chapters in one session, sunset/operative-date branches left by AB 2449
(Stats. 2022, ch. 285), and double-jointing (later bills cite
"Section 1 of Chapter 534" for the block that printed as `SEC. 1.5.`).

All lobs come from `pubinfo_2023.zip` (2023–2024 session bulk export),
named by `bill_version_id`:

| file | source zip entry | why it's here |
| --- | --- | --- |
| `20230AB175497CHP_excerpt.lob` | `BILL_VERSION_TBL_7659.lob` | AB 1754 (Stats. 2023, ch. 131) chaptered. 2 MB omnibus, excerpted: document prefix + `SECTION 1.` + `SEC. 88.`–`SEC. 92.` + tail. Three § 54953 variant blocks (SEC. 88–90); excerpt verified block-identical to the full lob. |
| `20230AB55795CHP.lob` | `BILL_VERSION_TBL_9921.lob` | AB 557 (Stats. 2023, ch. 534) chaptered, whole. Four § 54953 blocks: `SECTION 1.`, `SEC. 1.5.` (double-joint operative), `SEC. 2.`, and a repealed block (`SEC. 3.`, no body). |
| `20230AB230297CHP.lob` | `BILL_VERSION_TBL_20068.lob` | AB 2302 (Stats. 2024, ch. 389) chaptered, whole. One block whose heading follows `:` with no newline after flattening (`…do enact as follows:SECTION 1.`). |
| `20230AB230299INT.lob` | `BILL_VERSION_TBL_11239.lob` | AB 2302 introduced, whole — a same-bill print pair with the chaptered lob for bill-version comparison. |
| `current_54953.txt` | live server `get_section` (2026-08) | Current law text (SB 707, Stats. 2025, ch. 327) from the law-lob source — the cross-source endpoint proving whitespace-insensitive tokenization yields zero phantom hunks. |

`golden_redlines/` holds the engine's expected markdown for the version
chain (AB 1754 SEC. 89 → AB 557 SEC. 2 → current; AB 557 SEC. 1.5 →
AB 2302), pinned in `tests/test_redline.py`.
