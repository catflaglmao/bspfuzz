#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Write the 19 malformed maps of the BSP loading fixes and check their SHA-256.

    python tools/make_maps.py [out dir]      (default: maps)

Every map is the synthetic base room of tools/bspgen.py with one thing changed.
Nine come from the regression cases of tools/bspmut.py, the other ten are
built here. The output is byte-identical on every run.
"""
import hashlib
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bspmut  # noqa: E402
from bspgen import Map  # noqa: E402

FAR = 0x10000000   # an index far outside any array: a release build faults on it
LUMP_PLANES = 1

FROM_BSPMUT = ("leaf_contents0_v29", "zero_nodes_v29", "zero_leafs_v29", "models4096", "node_cycle_v29",
               "node_dag_v29", "clip_cycle_v29", "vis_run_long_v29", "bspx_numlumps_8000001")

SHA256 = {
	"alloc_window_planes_v29": "142c177ecdb841222200d7692caebf6809e66cab5e4967cd570b1cfe511bbc40",
	"bspx_numlumps_8000001": "763aa5c6acd64bc8a1e1c39fac47286f31d45eae15aa54e00ff4393f32403b47",
	"clip_cycle_v29": "1092e150ccd10213a7ad383fcecb565fefaba521d7ca2363a7dd8efaf1e5260f",
	"clip_plane_far_v29": "730d393f44cd49fd95d66cfe30de3a6c51ce289032949ccf2fe8fad3e05df5ea",
	"leaf_contents0_v29": "0e5a96f05c5a933bf3e7e2b6e7c1efbf58e799ef61b1d67a380d9e70dc1922f9",
	"models4096": "c33039236949ca23f453c3be679f33df245c79bfa8a74f80c4adf8af46178ccb",
	"node_child_far_bsp2": "a2009ea8a0143533cf7667afe6bc96419e628adfad548b3716e1f4aae003821a",
	"node_cycle_v29": "f053a9698e99d8debaeb651dfae5d7ffc3c2cb3dbf073f4e7adbf8b16dafb24f",
	"node_dag_v29": "5df076c577576c6f2dbda6837a982bfc2c307b6b9aa05a3bfeff5d0011572336",
	"node_deep100000_bsp2": "d6476bbb5c6b7c4d8a9a01e71306cd5d2500194bd33b97dfbb02084deac4ece7",
	"node_plane_far_v29": "46d6a94e0cce79e198e5cbaca69abb692267f7e61c50e6895a60e845bbdc1837",
	"vis_run_long_v29": "96a198d4de2523de7e926aab840dd3aab72fc2af2dfd1a594d53e74127ced122",
	"visleafs10000_v29": "d744feeabec5fd348091bd1ea512f609527a69d3b5e7caaf716154cf71b09b54",
	"visleafs185364_novis_v29": "29941b90e6b8daa7552c46ef8f7768a1bc2118ca4c7520323c1f13ad4e031e58",
	"visleafs40000_bsp2": "4ed0b8acc3a992887dcaaa8d223e158c9c32a95ca169f6a02c294308eb1eaf33",
	"visofs_far_v29": "fa9a8bbc8a4e6fd34dda5e95590776748a946783df61687649c1fe4199be39d5",
	"visofs_far_big_bsp2": "11b80eb09a83469764d48e0a719b175714cb82608055c8219c8ab4026a44f8a0",
	"zero_leafs_v29": "85bf1055bc726db5d691577a191607e73008fea4e1f18f3a815942459236d543",
	"zero_nodes_v29": "9f45c006e4bf2905850cd08249b86b0587a953ec4c7419ac5ec3632f7033d075",
}


def bspmut_maps():
	want = set(FROM_BSPMUT) | {"base_v29"}
	cases = {c.name: c.data for c in bspmut.all_cases() if c.name in want}
	for name in FROM_BSPMUT:
		yield name, cases[name]

	b = bytearray(cases["base_v29"])                    # the planes lump length in the header
	struct.pack_into("<i", b, 4 + 8 * LUMP_PLANES + 4, 2147483632)
	yield "alloc_window_planes_v29", bytes(b)


def built_maps():
	m = Map.room("bsp2")
	m.nodes[2][2] = FAR                                  # a node child far past the nodes
	yield "node_child_far_bsp2", m

	m = Map.room("v29")
	m.nodes[0][0] = FAR                                  # the root node's plane index
	yield "node_plane_far_v29", m

	m = Map.room("v29")
	m.clipnodes[3][0] = FAR                              # a clipnode's plane index
	yield "clip_plane_far_v29", m

	m = Map.room("v29")
	m.leafs[1][1] = FAR                                  # leaf 1's vis offset
	yield "visofs_far_v29", m

	m = Map.room("v29")
	m.models[0][4] = 10000                               # visleafs 10000, 10 leafs in the file
	yield "visleafs10000_v29", m

	m = Map.room("v29", subs=0, vis="none")              # rowbytes * visleafs = 2^32 + 287312:
	while len(m.leafs) < 185365:                         # unreferenced leafs, so that visleafs
		m.leafs.append(list(m.leafs[1]))                 # 185364 < numleafs; the tree is the base room
	m.models[0][4] = 185364
	yield "visleafs185364_novis_v29", m

	m = Map.room("bsp2", cells=40000, subs=0, vis="all")  # 5000-byte vis rows, 4095-byte buffers
	yield "visleafs40000_bsp2", m

	m = Map.room("bsp2", cells=40000, subs=0, vis="all")  # #19: 4 MB loaded, then leaf 1's vis offset
	m.leafs[1][1] = FAR                                  # is rejected; overwrites the previous map's
	yield "visofs_far_big_bsp2", m                       # freed brush models first

	m = Map.room("bsp2")                                 # 100000 nodes deep through children[0],
	p = m.plane(0, -4096)                                # the recursive (non-tail) call of CM_SetParent;
	first, inner = len(m.nodes), m.nodes[5][1]           # the room is in front of x = -4096
	for i in range(100000):
		m.nodes.append([p, first + i + 1 if i < 99999 else inner, -1, [0] * 3, [0] * 3])
	m.nodes[5][1] = first
	yield "node_deep100000_bsp2", m


def main():
	out = sys.argv[1] if len(sys.argv) > 1 else "maps"
	os.makedirs(out, exist_ok=True)
	maps = list(bspmut_maps()) + [(name, m.serialize()) for name, m in built_maps()]
	bad = 0
	for name, data in sorted(maps):
		with open(os.path.join(out, name + ".bsp"), "wb") as f:
			f.write(data)
		ok = hashlib.sha256(data).hexdigest() == SHA256[name]
		bad += not ok
		print("%-28s %9d  %s" % (name + ".bsp", len(data), "ok" if ok else "SHA-256 MISMATCH"))
	print("%d maps in %s, %d mismatches" % (len(maps), out, bad))
	sys.exit(1 if bad else 0)


if __name__ == "__main__":
	main()
