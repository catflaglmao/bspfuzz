# bspfuzz (trimmed)

Tools that generate the malformed Quake BSP maps used to test the map-loading fixes in
ezQuake ([QW-Group/ezquake-source#1187](https://github.com/QW-Group/ezquake-source/pull/1187)) and
mvdsv ([QW-Group/mvdsv#243](https://github.com/QW-Group/mvdsv/pull/243)).

No map here comes from a real map or from id Software data. Every test map is a small synthetic room
written from scratch by `tools/bspgen.py`, with one field changed.

## Make the maps

Python 3, standard library only:

    python tools/make_maps.py [out dir]      # default: maps

It writes the 18 maps and checks each one against the SHA-256 below. They should be the same files as
`maps.zip` attached to the PRs.

## Tools

| File | What it does |
|---|---|
| `tools/bsplib.py` | Reads and writes the BSP header and lumps for v29, HL BSP30, BSP2 and 2PSB. |
| `tools/bspgen.py` | Builds the base map: a box room split into `cells` empty leafs along x, plus `subs` door-like submodels. Hull 0 is a node tree, hulls 1 and 2 are clipnode boxes for the player sizes, the PVS lets each cell see its neighbours. `python tools/bspgen.py <dir>` writes one valid base map per format. |
| `tools/bspmut.py` | Structure-aware mutations of the base map: 293 regression cases, each a known defect or boundary, with the loader outcome expected after the fix. `python tools/bspmut.py cases <dir>` writes them all plus `expected.tsv`. Its `check` and `table` commands run a loader test harness that is not part of this trimmed repo. |
| `tools/make_maps.py` | Writes exactly the 18 maps below and checks their SHA-256. |
| `tools/bspinfo.py` | Prints a map's header, lumps and counts, for checking what a map contains. |

## The maps

"Base" is `Map.room(fmt)` from `bspgen.py` with default arguments unless stated. `FAR` is
`0x10000000`, an index far outside any array. Rows marked bspmut come from `bspmut.py`'s
regression cases (the function name says which family); the others are in `make_maps.py`.

| # | Map | How it is made | What it hits | SHA-256 |
|---|---|---|---|---|
| 1 | `alloc_window_planes_v29.bsp` | Base v29; the header's planes lump length set to 2147483632. (`make_maps.py`) | `CM_CalcChecksum` allocates the planes length before checking it; the hunk's rounding wraps it negative. | `142c177ecdb841222200d7692caebf6809e66cab5e4967cd570b1cfe511bbc40` |
| 2 | `node_child_far_bsp2.bsp` | Base BSP2; node 2's second child set to `FAR`. | `CM_SetParent` writes through a child index past the nodes. | `a2009ea8a0143533cf7667afe6bc96419e628adfad548b3716e1f4aae003821a` |
| 3 | `leaf_contents0_v29.bsp` | bspmut `fam_ranges`: a leaf under a node gets contents 0. | A leaf with contents `>= 0` is handled as a node. | `0e5a96f05c5a933bf3e7e2b6e7c1efbf58e799ef61b1d67a380d9e70dc1922f9` |
| 4 | `node_plane_far_v29.bsp` | Base v29; the root node's plane index set to `FAR`. | The first point lookup reads a plane past the array. | `46d6a94e0cce79e198e5cbaca69abb692267f7e61c50e6895a60e845bbdc1837` |
| 5 | `clip_plane_far_v29.bsp` | Base v29; clipnode 3's plane index set to `FAR`. | `CM_LoadPhysicsNormals` reads a plane past the array. | `730d393f44cd49fd95d66cfe30de3a6c51ce289032949ccf2fe8fad3e05df5ea` |
| 6 | `zero_nodes_v29.bsp` | bspmut `fam_layout`: the nodes lump is empty. | `CM_SetParent` runs on a zero-size array. | `9f45c006e4bf2905850cd08249b86b0587a953ec4c7419ac5ec3632f7033d075` |
| 7 | `zero_leafs_v29.bsp` | bspmut `fam_layout`: the leafs lump is empty. | Node children point into a zero-size array. | `85bf1055bc726db5d691577a191607e73008fea4e1f18f3a815942459236d543` |
| 8 | `visleafs10000_v29.bsp` | Base v29 (10 leafs); model 0's `visleafs` set to 10000. | The vis build reads leaf records past the lump. | `d744feeabec5fd348091bd1ea512f609527a69d3b5e7caaf716154cf71b09b54` |
| 9 | `models4096.bsp` | bspmut `fam_ranges`: 4096 submodels (`MAX_MAP_MODELS`). | `SV_SpawnServer` writes one past `sv.model_precache` and `sv.models`. | `c33039236949ca23f453c3be679f33df245c79bfa8a74f80c4adf8af46178ccb` |
| 10 | `node_cycle_v29.bsp` | bspmut `fam_trees`: the node tree loops back to node 0. | `CM_SetParent` recurses until the stack overflows. | `f053a9698e99d8debaeb651dfae5d7ffc3c2cb3dbf073f4e7adbf8b16dafb24f` |
| 11 | `node_dag_v29.bsp` | bspmut `fam_trees`: 40 levels of nodes whose two children are the same next node. | The tree walk visits 2^40 paths (hang). | `5df076c577576c6f2dbda6837a982bfc2c307b6b9aa05a3bfeff5d0011572336` |
| 12 | `node_deep100000_bsp2.bsp` | Base BSP2; a valid chain of 100000 nodes inserted through `children[0]` in front of the plane x = -4096. | Stack overflow in the recursive tree walk (ezQuake's 1 MB stack). | `d6476bbb5c6b7c4d8a9a01e71306cd5d2500194bd33b97dfbb02084deac4ece7` |
| 13 | `clip_cycle_v29.bsp` | bspmut `fam_trees`: a clipnode cycle in world hull 1. | Point contents in that hull never ends (hang while spawning). | `1092e150ccd10213a7ad383fcecb565fefaba521d7ca2363a7dd8efaf1e5260f` |
| 14 | `visofs_far_v29.bsp` | Base v29; leaf 1's vis offset set to `FAR`. | `DecompressVis` reads far past the vis lump. | `fa9a8bbc8a4e6fd34dda5e95590776748a946783df61687649c1fe4199be39d5` |
| 15 | `visleafs185364_novis_v29.bsp` | Base v29, no submodels, no vis lump; leafs copied up to 185365 (unreferenced), `visleafs` set to 185364. | `rowbytes * visleafs` overflows `int`; the PHS build copies about 4 GB. | `29941b90e6b8daa7552c46ef8f7768a1bc2118ca4c7520323c1f13ad4e031e58` |
| 16 | `visleafs40000_bsp2.bsp` | `Map.room("bsp2", cells=40000, subs=0, vis="all")`: a valid map with 5000-byte vis rows. | ezQuake's 4095-byte vis buffers overflow. | `4ed0b8acc3a992887dcaaa8d223e158c9c32a95ca169f6a02c294308eb1eaf33` |
| 17 | `vis_run_long_v29.bsp` | bspmut `fam_vis`: a 255-byte zero run in a 1-byte vis row. | `DecompressVis` writes 255 bytes into a row-sized buffer (mvdsv). | `96a198d4de2523de7e926aab840dd3aab72fc2af2dfd1a594d53e74127ced122` |
| 18 | `bspx_numlumps_8000001.bsp` | bspmut `fam_bspx`: a BSPX directory with lump count 0x08000001. | `32 * count` wraps to 32; `CM_BSPX_LoadLumps` walks past the buffer. | `763aa5c6acd64bc8a1e1c39fac47286f31d45eae15aa54e00ff4393f32403b47` |
| 19 | (none) | `map dm4`, then `map visleafs185364_novis_v29`, in ezQuake. | A map rejected late in loading after another map was loaded: the client walks the freed brush models of the previous map. | — |

## License

GPL-2.0-or-later, see `LICENSE`.
